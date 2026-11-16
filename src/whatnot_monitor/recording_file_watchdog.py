"""Independently observe recording files and persist alerts without Store locks."""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class _Channel:
    path: Path
    size: int = 0
    changed_at: float = 0


@dataclass
class _Entry:
    name: str
    channels: dict[str, _Channel]
    warning: str | None = None
    revision: int = 0
    queued_revision: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class RecordingFileWatchdog:
    """Only stat files; an independent writer owns its SQLite connections."""

    def __init__(self, db_path, events, *, timeout_seconds: float = 180,
                 interval_seconds: float = 5, clock: Callable[[], float] = time.monotonic,
                 max_pending: int = 128, busy_timeout_seconds: float = 0.2,
                 shutdown_timeout_seconds: float = 2):
        self.db_path = Path(db_path) if db_path is not None else None
        self.events = events
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.busy_timeout_seconds = busy_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._alerts: queue.Queue = queue.Queue(maxsize=max_pending)
        self._queue_full_notified = False
        self._last_writer_error = float('-inf')
        self._shutdown_loss_notified = False
        self._thread: threading.Thread | None = None
        self._writer: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if ((self._thread and self._thread.is_alive()) or
                    (self._writer and self._writer.is_alive())):
                if self._stop.is_set():
                    self.events.put(('error', '文件看门狗尚未完全退出，无法重新启动；请检查磁盘与数据库状态。'))
                return
            # Discard old-generation entries. For a still-current alert that
            # was never written, clear the dedupe marker so the next poll can
            # retry it after restarting this same watchdog instance.
            while True:
                try:
                    session_id, entry, revision, _warning, _observed = self._alerts.get_nowait()
                except queue.Empty:
                    break
                if (self._entries.get(session_id) is entry
                        and entry.revision == revision
                        and entry.queued_revision == revision):
                    entry.queued_revision = None
            self._shutdown_loss_notified = False
            self._stop.clear()
            self._writer = threading.Thread(target=self._write_alerts, name='recording-alert-writer', daemon=True)
            self._thread = threading.Thread(target=self._run, name='recording-file-watchdog', daemon=True)
            self._writer.start()
            self._thread.start()

    def stop(self) -> None:
        # Do not block shutdown on a stalled filesystem or SQLite device.
        with self._lock:
            self._stop.set()
        deadline = time.monotonic() + self.shutdown_timeout_seconds
        for thread in (self._thread, self._writer):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        # No thread uses the application's Store or its SQLite connection.
        # An in-flight writer closes its own connection in its own finally.
        if (not self._alerts.empty() or (self._writer and self._writer.is_alive())):
            with self._lock:
                if not self._shutdown_loss_notified:
                    self._shutdown_loss_notified = True
                    self.events.put(('warning', '看门狗关闭时仍有未完成的告警，部分事件可能未能写入数据库。'))

    @property
    def shutdown_loss(self) -> bool:
        with self._lock:
            return self._shutdown_loss_notified

    def register(self, session_id: str, name: str, paths: dict[str, Path]) -> None:
        now = self.clock()
        channels = {key: _Channel(Path(path), changed_at=now) for key, path in paths.items() if path}
        with self._lock:
            self._entries[session_id] = _Entry(name, channels)

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)

    def warning(self, session_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(session_id)
            return bool(entry and entry.warning)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.poll_once()

    def _persist(self, session_id: str, message: str, observed_at: str) -> None:
        if self.db_path is None:
            raise RuntimeError('未配置持久化数据库路径')
        uri = self.db_path.resolve().as_uri() + '?mode=rw'
        connection = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_seconds)
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute(
                'INSERT INTO events(created_at,level,component,message,session_id) VALUES(?,?,?,?,?)',
                (observed_at, 'WARNING', 'recording', message, session_id),
            )
            connection.commit()
        finally:
            connection.close()

    def _write_alerts(self) -> None:
        while not self._stop.is_set():
            try:
                alert = self._alerts.get(timeout=0.1)
            except queue.Empty:
                continue
            session_id, entry, revision, warning, observed_at = alert
            with self._lock:
                if (self._entries.get(session_id) is not entry or
                        entry.revision != revision or entry.warning != warning):
                    continue
            message = (f'{entry.name} 于 {observed_at} 观察到：{warning}；'
                       '当时未自动停止录制或删除文件。当前状态请查看直播总览。')
            try:
                self._persist(session_id, message, observed_at)
            except Exception as exc:  # noqa: BLE001 - keep the monitor alive
                with self._lock:
                    if self._entries.get(session_id) is entry and entry.revision == revision:
                        entry.queued_revision = None  # Retry at a later poll.
                    now = time.monotonic()
                    if not self._stop.is_set() and now - self._last_writer_error >= 60:
                        self._last_writer_error = now
                        self.events.put(('error', f'录制异常事件未能写入数据库，将重试：{exc}'))

    def poll_once(self) -> None:
        with self._lock:
            snapshot = list(self._entries.items())
        for session_id, entry in snapshot:
            now = self.clock()
            stale = []
            with entry.lock:
                if not entry.channels:
                    stale.append('未配置媒体路径')
                for channel_name, channel in entry.channels.items():
                    try:
                        try:
                            size = channel.path.stat().st_size
                        except FileNotFoundError:
                            size = 0
                    except OSError:
                        stale.append(f'{channel_name}无法检查')
                        continue
                    if size > channel.size:
                        channel.size, channel.changed_at = size, now
                    elif size < channel.size:
                        stale.append(f'{channel_name}文件大小异常减少')
                    elif now - channel.changed_at >= self.timeout_seconds:
                        stale.append(f'{channel_name}文件无增长')
                warning = '、'.join(stale) if stale else None
                with self._lock:
                    if self._entries.get(session_id) is not entry:
                        continue
                    if entry.warning != warning:
                        entry.warning = warning
                        entry.revision += 1
                    if warning and entry.queued_revision != entry.revision and not self._stop.is_set():
                        alert = (session_id, entry, entry.revision, warning, datetime.now(UTC).isoformat())
                        try:
                            self._alerts.put_nowait(alert)
                        except queue.Full:
                            if not self._queue_full_notified:
                                self._queue_full_notified = True
                                self.events.put(('warning', '录制告警队列已满，未写入的异常将在后续检查重试。'))
                        else:
                            entry.queued_revision = entry.revision
                            self._queue_full_notified = False
