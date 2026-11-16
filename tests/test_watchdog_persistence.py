import queue
import sqlite3
import threading
import time

from whatnot_monitor.recording_file_watchdog import RecordingFileWatchdog
from whatnot_monitor.store import Store


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_session(store, session_id):
    store.create_session(session_id, 'room', session_id, 'https://www.whatnot.com/show/actual',
                         started_at=None, start_time_source='first_seen_estimate',
                         first_seen_at='2026-11-14T00:00:00+00:00')


def test_slow_writer_does_not_block_watchdog_or_persist_stale_queued_alert(tmp_path):
    entered, resume = threading.Event(), threading.Event()
    writes = []
    w = RecordingFileWatchdog(None, queue.Queue(), timeout_seconds=0, interval_seconds=60)
    def persist(session_id, message, _at):
        entered.set()
        assert resume.wait(3)
        writes.append((session_id, message))
    w._persist = persist
    path = tmp_path / 'audio.webm'
    path.write_bytes(b'')
    w.register('first', 'first', {'音频': path})
    w.start()
    w.poll_once()
    assert entered.wait(2)
    w.register('second', 'second', {'音频': path})
    w.poll_once()
    assert w.warning('second')
    w.unregister('second')
    w.unregister('first')
    resume.set()
    assert wait_for(lambda: bool(writes))
    w.stop()
    assert [sid for sid, _ in writes] == ['first']
    assert '观察到' in writes[0][1] and '当前状态请查看' in writes[0][1]


def test_writer_shutdown_is_bounded_even_if_own_io_hangs(tmp_path):
    entered, resume = threading.Event(), threading.Event()
    w = RecordingFileWatchdog(None, queue.Queue(), timeout_seconds=0, interval_seconds=60,
                              shutdown_timeout_seconds=0.1)
    def persist(*_args):
        entered.set()
        assert resume.wait(3)
    w._persist = persist
    path = tmp_path / 'audio.webm'
    path.write_bytes(b'')
    w.register('first', 'first', {'音频': path})
    w.start()
    w.poll_once()
    assert entered.wait(2)
    start = time.monotonic()
    w.stop()
    assert time.monotonic() - start < 0.8
    assert not hasattr(w, 'store')
    resume.set()
    w._writer.join(timeout=2)
    assert not w._writer.is_alive()


def test_bounded_queue_deduplicates_and_warns_on_overflow(tmp_path):
    events = queue.Queue()
    w = RecordingFileWatchdog(None, events, timeout_seconds=0, interval_seconds=60, max_pending=2)
    path = tmp_path / 'audio.webm'
    path.write_bytes(b'')
    for i in range(4):
        w.register(str(i), str(i), {'音频': path})
    for _ in range(5):
        w.poll_once()
    assert w._alerts.qsize() == 2
    assert events.qsize() == 1
    assert '队列已满' in events.get_nowait()[1]
    assert all(w.warning(str(i)) for i in range(4))


def test_locked_sqlite_does_not_take_main_store_lock_and_later_retries(tmp_path):
    path = tmp_path / 'monitor.sqlite3'
    store = Store(path)
    make_session(store, 'actual')
    events = queue.Queue()
    w = RecordingFileWatchdog(path, events, timeout_seconds=0, interval_seconds=0.02,
                              busy_timeout_seconds=0.05)
    media = tmp_path / 'audio.webm'
    media.write_bytes(b'')
    blocker = sqlite3.connect(path, timeout=0.1)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        w.register('actual', 'actual', {'音频': media})
        w.start()
        assert wait_for(lambda: not events.empty())
        assert '未能写入数据库' in events.get_nowait()[1]
        # SQLite write lock is external; watchdog must not hold Store's lock.
        assert store._lock.acquire(timeout=0.2)
        store._lock.release()
        assert store.get_session('actual') is not None
        blocker.rollback()
        assert wait_for(lambda: any('文件无增长' in e.message for e in store.recent_events()))
        w.stop()
        assert not w._writer.is_alive()
    finally:
        blocker.close()
        w.stop()
        store.close()


def test_real_sqlite_warning_persisted_with_observation_time(tmp_path):
    path = tmp_path / 'monitor.sqlite3'
    store = Store(path)
    make_session(store, 'actual')
    w = RecordingFileWatchdog(path, queue.Queue(), timeout_seconds=0, interval_seconds=60)
    media = tmp_path / 'audio.webm'
    media.write_bytes(b'')
    try:
        w.register('actual', 'actual', {'音频': media})
        w.start()
        w.poll_once()
        assert wait_for(lambda: any(e.session_id == 'actual' and '文件无增长' in e.message
                                    for e in store.recent_events()))
        record = next(e for e in store.recent_events() if '文件无增长' in e.message)
        assert '观察到' in record.message and '当前状态请查看' in record.message
    finally:
        w.stop()
        store.close()


def test_restart_requeues_current_alert_discarded_during_stop(tmp_path):
    events = queue.Queue()
    persisted = []
    w = RecordingFileWatchdog(None, events, timeout_seconds=0, interval_seconds=60)
    w._persist = lambda sid, message, _at: persisted.append((sid, message))
    media = tmp_path / 'restart.webm'
    media.write_bytes(b'')
    w.register('same', 'room', {'音频': media})
    w.poll_once()
    assert w._alerts.qsize() == 1
    w.stop()
    assert any('可能未能写入数据库' in item[1] for item in list(events.queue))
    w.start()
    try:
        assert w._alerts.empty()
        w.poll_once()
        assert wait_for(lambda: len(persisted) == 1)
        assert persisted[0][0] == 'same'
    finally:
        w.stop()


def test_shutdown_warning_for_unfinished_queue_is_not_repeated(tmp_path):
    events = queue.Queue()
    w = RecordingFileWatchdog(None, events, timeout_seconds=0)
    media = tmp_path / 'queued.webm'
    media.write_bytes(b'')
    w.register('same', 'room', {'音频': media})
    w.poll_once()
    w.stop()
    w.stop()
    messages = [item[1] for item in list(events.queue)]
    assert sum('可能未能写入数据库' in message for message in messages) == 1
