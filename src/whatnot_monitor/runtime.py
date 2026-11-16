from __future__ import annotations

import hashlib
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .accounts import BrowserAccount
from .bounded_browser_read import BrowserReadTimeout
from .browser_pool import AccountBrowserDisconnected, BrowserPool
from .config import Settings
from .live_identity import live_room_identity
from .whatnot import (
    SearchDiagnosticError,
    WhatnotBrowserProfileInUse,
    WhatnotBrowser,
    WhatnotPageStateUnknown,
    WhatnotVerificationRequired,
)

log = logging.getLogger(__name__)

_MAX_RECORDING_START_ATTEMPTS = 3


class TranscriptRecoveryError(RuntimeError):
    """The saved transcript cannot be read and must not be replaced."""


def _search_failure_detail(exc: Exception) -> str:
    # The page diagnostic type is constructed only after strict redaction.
    # Other errors may include Playwright call logs, page content or URLs.
    if type(exc) in (SearchDiagnosticError, WhatnotBrowserProfileInUse, AccountBrowserDisconnected):
        return str(exc)
    if isinstance(exc, WhatnotPageStateUnknown):
        return "页面状态未知，已保留原页面并暂停导航。"
    if isinstance(exc, WhatnotVerificationRequired):
        return "Whatnot 安全验证待处理。"
    return "搜索失败，可能包含页面信息的错误详情已隐藏。"


def _now() -> datetime:
    return datetime.now(UTC)


class MonitorRuntime:
    """Run all synchronous Playwright calls on one owner thread."""

    def __init__(self, store, settings: Settings):
        self.store = store
        self.settings = settings
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._active_segments: dict[str, str] = {}
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._verification_required = threading.Event()
        # A blocked recording itself needs an explicit Start/recheck. An
        # unrelated blocked tab must not suppress safe read-only due samples.
        self._audience_blocked_sessions: set[str] = set()
        self._scan_requested = threading.Event()
        self._scan_lock = threading.Lock()
        self._history_lock = threading.RLock()
        self._upload_lock = threading.RLock()
        self._clear_history_requested = threading.Event()
        self._postprocess_futures = set()
        self._scan_request_lock = threading.Lock()
        self._requested_keyword_ids: set[int] = set()
        self._scan_status_lock = threading.Lock()
        self._scan_status = {
            "state": "idle",
            "blocking_reason": None,
            "interval_seconds": settings.poll_interval_seconds,
            "total_keywords": 0,
            "scanned_keywords": 0,
            "failed_keywords": 0,
            "current_keyword": None,
            "new_rooms": 0,
            "duplicate_results": 0,
            "recordings_started": 0,
            "round_started_at": None,
            "round_finished_at": None,
        }
        self._login_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self.browser_accounts: list[BrowserAccount] = []
        self.platform = "whatnot"
        self.load_browser_accounts = None
        self._browser: WhatnotBrowser | BrowserPool | None = None
        self._playwright = None
        self._recorders: dict[str, tuple[object, object]] = {}
        # Process-local lower bounds for explicit live search-card sightings.
        # Never persist monotonic times: a restart requires a new observation.
        self._live_search_evidence: dict[str, float] = {}
        self._recording_started: dict[str, datetime] = {}
        from .recording_file_watchdog import RecordingFileWatchdog
        self._file_watchdog = RecordingFileWatchdog(self.store._db_path, self.events)
        self._manual_stop_requests: set[str] = set()
        self._end_confirmations: dict[str, int] = {}
        self._postprocess = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="postprocess"
        )
        self._last_scan = 0.0

    @property
    def running(self) -> bool:
        return bool(
            self._thread and self._thread.is_alive() and not self._stop.is_set()
        )

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def start(self, *, pause_scans: bool = False) -> None:
        with self._history_lock:
            self._start(pause_scans=pause_scans)

    def _start(self, *, pause_scans: bool = False) -> None:
        if not pause_scans:
            # Starting from the UI is the user's signal that they finished any
            # browser verification and want a fresh scan attempt.
            self._verification_required.clear()
            self._audience_blocked_sessions.clear()
            self._scan_requested.set()
            self._set_scan_status(
                state="starting",
                current_keyword=None,
                blocking_reason=None,
            )
        else:
            self._set_scan_status(
                state="opening_login",
                current_keyword=None,
                blocking_reason=None,
            )
        if self.running:
            if pause_scans:
                self._paused.set()
                self.events.put(("info", "登录验证期间已暂停新的关键词扫描；正在进行的录制会继续。"))
            else:
                self._paused.clear()
                self._scan_requested.set()
                self.events.put(("info", "监控已恢复。"))
            return
        self._stop.clear()
        if pause_scans:
            self._paused.set()
        else:
            self._paused.clear()
        self._thread = threading.Thread(
            target=self._run, name="playwright-owner", daemon=True
        )
        self._file_watchdog.start()
        self._thread.start()
        if pause_scans:
            message = "监控已启动，登录验证期间暂停关键词扫描。"
        else:
            message = "监控已启动。"
        self.events.put(("info", message))

    def _verification_status(self) -> dict:
        info = getattr(self._browser, "verification_info", {})
        return {key: info[key] for key in (
            "verification_phase", "verification_url", "verification_observed_at",
            "verification_account",
        ) if key in info}

    def _verification_pause_message(self) -> str:
        details = self._verification_status()
        phase = details.get("verification_phase", "unknown")
        account = details.get("verification_account")
        browser = f"账号「{account}」的专用 Chrome" if account else "程序打开的 Chrome"
        instruction = (
            "验证组件仍在加载，尚未检测到可操作控件；程序不会自动点击、刷新或关闭验证页。"
            if phase == "loading" else
            f"请在{browser}窗口中按页面实际提示手动处理验证。"
        )
        return (
            "Whatnot 显示了安全验证，关键词扫描已暂停，新开直播间也已暂停；"
            "受影响录制页的人数核验暂停，其他已核实健康页仅继续只读核验。"
            "仍正常工作的录制继续；若录制页被替换，已保存片段会保留并标记中断。"
            + instruction + "完成后回到这里点击“启动”重新检查。"
        )

    def _pause_for_verification(self, source: str, session_id: str | None = None) -> None:
        already_paused = self._verification_required.is_set()
        if session_id is not None:
            self._audience_blocked_sessions.add(session_id)
        self._verification_required.set()
        self._paused.set()
        self._scan_requested.clear()
        with self._scan_request_lock:
            self._requested_keyword_ids.clear()
        message = self._verification_pause_message()
        self._set_scan_status(
            state="verification_required", current_keyword=None,
            blocking_reason=message, round_finished_at=_now().isoformat(),
            **self._verification_status(),
        )
        if not already_paused:
            self.store.add_event("warning", "whatnot", f"{source}。{message}", session_id=session_id)
            self.events.put(("warning", f"{source}。{message}"))

    def _pause_for_unknown_page(self, session_id: str | None = None, detail: str | None = None) -> None:
        already_paused = self._verification_required.is_set()
        if session_id is not None:
            self._audience_blocked_sessions.add(session_id)
        self._verification_required.set()
        self._paused.set()
        self._scan_requested.clear()
        with self._scan_request_lock:
            self._requested_keyword_ids.clear()
        message = ("页面状态未知；已暂停搜索和新页面导航，"
                   "保留现有录制和仍可用的页面。请检查浏览器，只读复核通过后才能继续。")
        if detail:
            message += f" 读取原因：{detail}"
        self._set_scan_status(state="page_state_unknown", current_keyword=None,
                              blocking_reason=message, round_finished_at=_now().isoformat())
        if not already_paused:
            self.store.add_event("warning", "whatnot", message, session_id=session_id)
            self.events.put(("warning", message))

    def pause(self) -> None:
        self._paused.set()
        self.events.put(
            ("info", "监控已暂停；正在进行的浏览器录制会继续。")
        )

    def shutdown(self, timeout: float = 20) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.events.put(
                    (
                        "warning",
                        "正在等待浏览器请求完成后再关闭程序。",
                    )
                )
                self._thread.join()
        self._file_watchdog.stop()
        self._postprocess.shutdown(wait=True, cancel_futures=False)

    def _submit_postprocess(self, function, *args):
        with self._history_lock:
            self._postprocess_futures = {
                future for future in self._postprocess_futures if not future.done()
            }
            future = self._postprocess.submit(function, *args)
            self._postprocess_futures.add(future)
            return future

    def request_clear_history(self) -> dict | None:
        """Queue on the browser owner so no scan/start can race with file deletion."""
        with self._history_lock:
            if self._thread and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("程序正在停止，请等待录制收尾完成后再清除。")
                self._clear_history_requested.set()
                return None
            return self._clear_room_history()

    def _clear_room_history(self) -> dict:
        from .history import delete_session_artifacts

        with self._history_lock:
            if any(not future.done() for future in self._postprocess_futures):
                raise RuntimeError("转写、上传或后台检查尚未结束，请等待完成后再清除；现有录制不受影响。")
            active_paths = {
                Path(path).resolve()
                for recorder, _page in self._recorders.values()
                for path in (
                    getattr(recorder, "video_path", None),
                    getattr(recorder, "audio_path", getattr(recorder, "output_path", None)),
                ) if path
            }
            result = self.store.clear_non_recording_sessions(
                lambda session, retained: delete_session_artifacts(
                    session, retained, self.settings, active_paths=active_paths
                ),
                protected_ids=set(self._recorders),
            )
            message = (
                f"历史清除完成：已清除 {result['deleted']} 条非录制中记录及对应文件，"
                f"保留 {result['retained']} 条录制中记录。"
            )
            if result["failures"]:
                details = "; ".join(f"{key}: {value}" for key, value in result["failures"].items())
                message += f" {len(result['failures'])} 条清理失败，已保留记录供重试：{details}"
            self.store.add_event("warning" if result["failures"] else "info", "storage", message)
            self.events.put(("warning" if result["failures"] else "info", message))
            return result

    def open_login(self) -> None:
        self.start(pause_scans=True)
        self._login_requested.set()
        self.events.put(
            (
                "info",
                "正在打开独立的 Whatnot 浏览器配置，关键词扫描已暂停。此配置与普通浏览器的登录状态分开保存；若持续出现安全验证，程序不会自动绕过或反复重试。",
            )
        )

    def request_scan_now(self) -> None:
        if self._scan_lock.locked():
            self.events.put(("info", "关键词扫描正在进行中。"))
            return
        with self._scan_request_lock:
            self._requested_keyword_ids.clear()
        self._scan_requested.set()
        self.events.put(("info", "已排队，将扫描所有启用的关键词。"))

    def request_keyword_scan(self, keyword_id: int) -> None:
        keyword = next(
            (
                item
                for item in self.store.list_keywords(enabled_only=True)
                if item.id == keyword_id
            ),
            None,
        )
        if keyword is None:
            self.events.put(
                ("warning", "只能手动扫描已启用的关键词。")
            )
            return
        if self._scan_lock.locked():
            self.events.put(("info", "关键词扫描正在进行中。"))
            return
        with self._scan_request_lock:
            self._requested_keyword_ids.add(keyword_id)
        self._scan_requested.set()
        self.events.put(("info", f"已为关键词“{keyword.phrase}”排队执行手动扫描。"))

    @property
    def scan_status(self) -> dict[str, object]:
        with self._scan_status_lock:
            status = dict(self._scan_status)
        status["interval_seconds"] = self.settings.poll_interval_seconds
        return status

    def _ensure_browser(self):
        if self._browser is not None:
            is_usable = getattr(self._browser, "is_usable", None)
            if callable(is_usable) and not is_usable():
                if getattr(self._browser, "_uncertain_page", None) is not None:
                    self._pause_for_unknown_page()
                    raise WhatnotPageStateUnknown("浏览器连接失效且页面状态未知；已阻止自动重连和导航。")
                self.events.put(
                    ("warning", "Whatnot 浏览器上下文已关闭，正在重新连接。")
                )
                self.store.add_event(
                    "warning",
                    "whatnot",
                    "检测到 Whatnot 浏览器上下文已关闭；已中断受影响的录制并准备重连。",
                )
                self._stop_all_recordings()
                self._browser = None
                if self._playwright is not None:
                    try:
                        self._playwright.stop()
                    except Exception:
                        log.exception("Error stopping disconnected Playwright driver")
                    self._playwright = None
        if self._browser is None:
            if self.browser_accounts:
                self._browser = BrowserPool(
                    self.browser_accounts, headless=self.settings.browser_headless,
                    load_accounts=self.load_browser_accounts,
                )
            else:
                self._browser, self._playwright = WhatnotBrowser.launch_persistent(
                    self.settings.browser_profile_dir,
                    self.settings.browser_channel,
                    headless=self.settings.browser_headless,
                )
        if isinstance(self._browser, (WhatnotBrowser, BrowserPool)):
            self._browser.service_recordings = self._service_active_recordings
        return self._browser

    def _recording_capacity(self) -> int:
        configured = self.settings.max_concurrent_recordings
        if isinstance(self._browser, BrowserPool):
            self._browser.refresh_account_settings()
            return min(configured, len(self._recorders) + self._browser.available_slots(self._recorders))
        return configured

    def _service_active_recordings(self) -> None:
        """Cooperative owner-thread checkpoint; never open new rooms here."""
        now = time.monotonic()
        if now - getattr(self, "_last_active_service", float("-inf")) < 5:
            return
        self._last_active_service = now
        self._monitor_recordings()
        self._check_due_audience()

    def _wait_with_browser_events(
        self, seconds: float, *, wake_for_requests: bool = False
    ) -> None:
        """Wait interruptibly without starving browser-to-Python media writes.

        Sync Playwright dispatches exposed bindings only while its owner thread
        is inside a Playwright call. Event.wait alone delays every recorded chunk.
        """
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            if wake_for_requests and (
                self._clear_history_requested.is_set()
                or self._login_requested.is_set()
                or (self._scan_requested.is_set() and not self._paused.is_set())
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            quantum = min(0.1, remaining)
            pump = getattr(self._browser, "pump_events", None)
            if callable(pump):
                try:
                    pump(quantum * 1000)
                except Exception:  # Owner loop recovers a closed browser.
                    log.exception("Could not pump browser events during wait")
                    self._stop.wait(remaining)
                    return
            else:
                self._stop.wait(quantum)

    def _run(self) -> None:
        self.store.interrupt_orphaned_recordings()
        self._check_feishu()
        self._resume_recoverable()
        try:
            while not self._stop.is_set():
                if self._clear_history_requested.is_set():
                    self._clear_history_requested.clear()
                    try:
                        self._clear_room_history()
                    except Exception as exc:  # noqa: BLE001 - surface queued cleanup failure
                        self.events.put(("error", f"历史清除未执行：{exc}"))
                if self._login_requested.is_set():
                    self._login_requested.clear()
                    try:
                        self._ensure_browser().open_login()
                        self._set_scan_status(
                            state="login_ready",
                            current_keyword=None,
                            blocking_reason=(
                                "登录/验证状态由程序专用 Chrome 页面确认；"
                                "完成后点击启动重新扫描。"
                            ),
                        )
                        self.events.put(
                            (
                                "info",
                                "Whatnot 页面已打开。请在程序专用 Chrome 中完成登录或验证，然后点击“启动”重新扫描。",
                            )
                        )
                    except WhatnotPageStateUnknown as exc:
                        self._pause_for_unknown_page(detail=str(exc))
                    except WhatnotVerificationRequired as exc:
                        self._verification_required.set()
                        self._paused.set()
                        self._scan_requested.clear()
                        with self._scan_request_lock:
                            self._requested_keyword_ids.clear()
                        self._set_scan_status(
                            state="verification_required",
                            current_keyword=None,
                            blocking_reason=self._verification_pause_message(),
                            round_finished_at=_now().isoformat(),
                            **self._verification_status(),
                        )
                        message = self._verification_pause_message()
                        self.store.add_event("error", "whatnot", f"登录页检测到安全验证：{exc}。{message}")
                        self.events.put(("error", f"登录页检测到安全验证。{message}"))
                    except Exception as exc:
                        log.exception("Could not open Whatnot login")
                        self._set_scan_status(
                            state="error",
                            current_keyword=None,
                            blocking_reason=f"无法打开 Whatnot 登录页面：{exc}",
                        )
                        self.events.put(
                            ("error", f"无法打开 Whatnot 登录页面：{exc}")
                        )
                # Due sampling and existing capture health precede optional
                # search/new-player operations, which can take tens of seconds.
                self._monitor_recordings()
                self._check_due_audience()
                now_monotonic = time.monotonic()
                if not self._paused.is_set() and (
                    self._scan_requested.is_set()
                    or now_monotonic - self._last_scan
                    >= max(15, self.settings.poll_interval_seconds)
                ):
                    self._scan_requested.clear()
                    self._last_scan = now_monotonic
                    try:
                        self.scan_once()
                    except Exception as exc:  # noqa: BLE001 - sanitize search boundary
                        detail = _search_failure_detail(exc)
                        log.error("Scan cycle failed: %s", detail)
                        self.events.put(("error", f"扫描轮次失败：{detail}"))
                self._monitor_recordings()
                self._check_due_audience()
                recorders_before = len(self._recorders)
                self._start_waiting_sessions()
                self._check_due_audience()
                # When a room just started, promptly return to the top of the
                # owner loop to service other queued rooms (or publish capacity
                # warnings). Keep a short browser pump for recording callbacks.
                # A failed attempt keeps the normal wait and avoids a busy loop.
                wait_seconds = 0.1 if len(self._recorders) > recorders_before else 5
                self._wait_with_browser_events(wait_seconds, wake_for_requests=True)
        finally:
            self._stop_all_recordings()
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    log.exception("Error closing browser context")
            if self._playwright:
                self._playwright.stop()
            self._browser = None
            self._playwright = None

    def _check_feishu(self) -> None:
        from .config import feishu_credentials
        from .integrations.feishu import FeishuBitableClient, FeishuConfig

        settings = self.settings
        try:
            app_id, app_secret = feishu_credentials(settings)
        except Exception as exc:  # noqa: BLE001 - report local credential failures
            self.store.add_event("error", "feishu", f"飞书凭据读取失败：{exc}")
            self.events.put(("error", f"飞书凭据读取失败：{exc}"))
            return
        if not app_id or not app_secret:
            self.store.add_event(
                "warning",
                "feishu",
                "尚未配置飞书凭据。",
            )
            self.events.put(("warning", "尚未配置飞书凭据。"))
            return
        try:
            config = FeishuConfig(
                app_id=app_id,
                app_secret=app_secret,
                app_token=settings.feishu_app_token,
                table_id=settings.feishu_table_id,
            )
            with FeishuBitableClient(config) as client:
                client.validate_access()
            self.events.put(
                (
                    "info",
                    "飞书认证、数据表和五个字段的结构检查已通过。",
                )
            )
        except Exception as exc:  # noqa: BLE001 - report configuration/API failures
            self.store.add_event(
                "error", "feishu", f"飞书启动连接检查失败：{exc}"
            )
            self.events.put(("error", f"飞书启动检查失败：{exc}"))

    def test_feishu(self) -> None:
        self._submit_postprocess(self._check_feishu)

    def _saved_transcript(self, session) -> str | None:
        if session.transcript_text is not None:
            return session.transcript_text
        if session.transcript_path:
            try:
                return Path(session.transcript_path).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                message = f"无法读取历史转写文件，保留任务以便重试：{exc}"
                self.store.add_event("warning", "recovery", message, session_id=session.session_id)
                self.events.put(("warning", message))
                raise TranscriptRecoveryError(message) from exc
        return None

    def resume_pending_feishu_uploads(self) -> int:
        """Retry recoverable uploads after settings are saved."""
        queued = 0
        for session in self.store.list_sessions(statuses={"UPLOADING", "FAILED"}, limit=10000):
            if session.qualification_status != "qualified":
                continue
            if not session.feishu_record_id and not self._capture_ready_for_postprocess(session):
                continue
            if any(s["status"] == "interrupted" for s in self.store.list_recording_segments(session.session_id)):
                continue
            try:
                transcript = self._saved_transcript(session) if not session.feishu_record_id else None
            except TranscriptRecoveryError:
                continue
            if transcript is None and not session.feishu_record_id:
                continue
            self._submit_postprocess(self._retry_feishu_upload, session.session_id, transcript or "")
            queued += 1
        return queued

    def _retry_feishu_upload(self, session_id: str, transcript: str) -> None:
        self._export_finalized_video(session_id)
        self._upload(session_id, transcript)

    def _resume_recoverable(self) -> None:
        for session in self.store.recoverable_sessions():
            if self.platform == "whatnot":
                if session.session_id.startswith("platform-live:"):
                    continue
            elif not session.session_id.startswith(f"platform-live:{self.platform}:"):
                continue
            if session.qualification_status == "low_viewers_deferred" and session.status == "INTERRUPTED":
                if self.discard_deferred_low_recording(session.session_id):
                    continue
            if (session.status == "FAILED" and session.qualification_status
                    in {"ended_before_threshold", "viewer_check_failed"}
                    and self.recover_finished_recording(session.session_id)):
                continue
            if session.qualification_status == "rejected_low_viewers":
                if (not session.feishu_record_id and type(session.audience_value) is int
                        and session.audience_value <= self._minimum_viewers_for(session)):
                    self._remove_low_viewer_recording(session)
                elif session.status == "INTERRUPTED":
                    details = {
                        "manual_confirmed_low": "用户确认低人数并清理媒体。",
                        "captured_late": "此前已按延迟采样结果处理。",
                        "captured": "此前已按人数采样结果处理。",
                    }
                    self.store.add_event(
                        "info", "recovery", details.get(session.audience_outcome, "请查看历史事件。")
                        + "未自动续录。", session_id=session.session_id,
                    )
                continue
            if session.status == "INTERRUPTED":
                if (session.video_path or session.audio_path or session.recording_path) and session.qualification_status not in {"rejected_low_viewers", "cleanup_failed"}:
                    self.store.prepare_interrupted_resume(session.session_id)
                    self.store.add_event(
                        "warning", "recovery", "旧片段已保留；待核实直播后创建新片段续录，中断期间内容缺失。",
                        session_id=session.session_id,
                    )
                    continue
                self.store.add_event(
                    "warning", "recovery",
                    "录制已中断，现有媒体作为不完整片段保留，未自动转写或上传；无法补录缺失内容。",
                    session_id=session.session_id,
                )
                continue
            if any(s["status"] == "interrupted" for s in self.store.list_recording_segments(session.session_id)):
                if session.audience_outcome in {"captured", "captured_late"}:
                    reason = self._audience_timing_error(session, session.audience_captured_at)
                    if reason:
                        self._mark_audience_missed(session, reason)
                continue
            audio_value = session.audio_path or session.recording_path
            audio = Path(audio_value) if audio_value else None
            if session.qualification_status == "pending":
                if (
                    session.audience_outcome in {"captured", "captured_late"}
                    and session.audience_value is not None
                ):
                    reason = self._audience_timing_error(
                        session, session.audience_captured_at
                    )
                    if reason:
                        self._mark_audience_missed(session, reason)
                        continue
                    if session.audience_value > self._minimum_viewers_for(session):
                        session = self.store.set_qualification_status(
                            session.session_id, "qualified"
                        )
                    else:
                        self._reject_low_viewers(
                            session,
                            session.audience_value,
                            session.audience_raw,
                        )
                        continue
                else:
                    self.store.add_event(
                        "warning",
                        "recovery",
                        "计划的观众人数检查完成前，后处理任务暂缓；录制媒体文件已保留。",
                        session_id=session.session_id,
                    )
                    continue
            if session.qualification_status != "qualified":
                continue
            if not session.feishu_record_id and not self._capture_ready_for_postprocess(session):
                self.store.add_event(
                    "warning", "recovery", "缺少已完成的完整录制片段证据，未恢复转写或归档；媒体已保留。",
                    session_id=session.session_id,
                )
                continue
            try:
                transcript = None if session.feishu_record_id else self._saved_transcript(session)
            except TranscriptRecoveryError:
                continue
            if transcript is not None or session.feishu_record_id:
                if session.status != "UPLOADING":
                    try:
                        self.store.transition(session.session_id, "UPLOADING")
                    except ValueError:
                        continue
                self._submit_postprocess(
                    self._retry_feishu_upload, session.session_id, transcript or ""
                )
            elif (
                session.status in {"TRANSCRIBING", "INTERRUPTED", "FAILED"}
                and audio
                and audio.is_file()
            ):
                try:
                    self.store.transition(session.session_id, "TRANSCRIBING")
                except ValueError:
                    pass
                self._submit_postprocess(
                    self._transcribe_and_upload, session.session_id, audio
                )

    def scan_once(self) -> None:
        if self._verification_required.is_set():
            self._paused.set()
            self._scan_requested.clear()
            with self._scan_request_lock:
                self._requested_keyword_ids.clear()
            self.events.put(("warning", self.scan_status.get("blocking_reason") or self._verification_pause_message()))
            return
        if not self._scan_lock.acquire(blocking=False):
            self.events.put(("info", "关键词扫描正在进行中。"))
            return
        self._scan_requested.clear()
        with self._scan_request_lock:
            keyword_ids = set(self._requested_keyword_ids)
            self._requested_keyword_ids.clear()
        try:
            self._scan_round(keyword_ids or None)
        finally:
            self._scan_lock.release()

    def _scan_round(self, keyword_ids: set[int] | None = None) -> None:
        keywords = self.store.list_keywords(enabled_only=True)
        if keyword_ids is not None:
            keywords = [keyword for keyword in keywords if keyword.id in keyword_ids]
        started_at = _now().isoformat()
        self._set_scan_status(
            state="scanning",
            interval_seconds=self.settings.poll_interval_seconds,
            total_keywords=len(keywords),
            scanned_keywords=0,
            failed_keywords=0,
            current_keyword=None,
            new_rooms=0,
            duplicate_results=0,
            recordings_started=0,
            round_started_at=started_at,
            round_finished_at=None,
        )
        self.events.put(("info", f"开始扫描，本轮共 {len(keywords)} 个关键词。"))
        if not keywords:
            self._set_scan_status(
                state="complete", round_finished_at=_now().isoformat()
            )
            return

        found: dict[str, dict[str, object]] = {}
        scan_results: dict[int, dict[str, object]] = {}
        failed_keywords = 0
        duplicate_results = 0
        verification_blocked = False
        unknown_blocked = False
        try:
            browser = self._ensure_browser()
            pool = browser if isinstance(browser, BrowserPool) else None
            if pool:
                pool.refresh_account_settings()
                keywords = [keyword for keyword in keywords if pool.can_scan(keyword.id)]
                self._set_scan_status(total_keywords=len(keywords))
                if not keywords:
                    self._set_scan_status(state="complete", round_finished_at=_now().isoformat())
                    return
            for index, keyword_row in enumerate(keywords):
                self._service_active_recordings()
                if self._stop.is_set():
                    break
                phrase = keyword_row.phrase
                keyword_started = _now().isoformat()
                self._set_scan_status(current_keyword=phrase)
                self.events.put(
                    ("info", f"正在扫描关键词 {index + 1}/{len(keywords)}：{phrase}")
                )
                self.store.begin_keyword_scan(keyword_row.id, keyword_started)
                shows = None
                last_error: Exception | None = None
                for attempt in range(2):
                    try:
                        search_started_monotonic = time.monotonic()
                        shows = pool.scan_keyword(phrase, keyword_id=keyword_row.id) if pool else browser.scan_keyword(phrase)
                        last_error = None
                        break
                    except WhatnotVerificationRequired as exc:
                        last_error = exc
                        verification_blocked = True
                        unknown_blocked = isinstance(exc, WhatnotPageStateUnknown)
                        break
                    except Exception as exc:  # noqa: BLE001 - retry each keyword independently
                        last_error = exc
                        if attempt == 0 and not self._stop.is_set():
                            self.store.add_event(
                                "warning",
                                "whatnot",
                                f"关键词 ID {keyword_row.id} 扫描失败，正在重试：{_search_failure_detail(exc)}",
                            )
                            self.events.put(("warning", f"正在重试关键词 ID {keyword_row.id}：{_search_failure_detail(exc)}"))
                            self._wait_with_browser_events(1.0)

                keyword_ended = _now().isoformat()
                if last_error is not None:
                    error_detail = _search_failure_detail(last_error)
                    failed_keywords += 1
                    self.store.finish_keyword_scan(
                        keyword_row.id,
                        started_at=keyword_started,
                        ended_at=keyword_ended,
                        result=f"扫描失败：{error_detail}",
                        discovered_count=0,
                        recording_count=0,
                        status="ERROR",
                    )
                    scan_results[keyword_row.id] = {
                        "started_at": keyword_started,
                        "ended_at": keyword_ended,
                        "result": f"扫描失败：{error_detail}",
                        "discovered_count": 0,
                        "status": "ERROR",
                    }
                    self.store.add_event(
                        "error", "whatnot", f"关键词 ID {keyword_row.id} 扫描出错：{error_detail}"
                    )
                    self.events.put(
                        ("error", f"关键词 ID {keyword_row.id} 扫描失败：{error_detail}")
                    )
                else:
                    shows = shows or []
                    self.store.finish_keyword_scan(
                        keyword_row.id,
                        started_at=keyword_started,
                        ended_at=keyword_ended,
                        result=f"发现 {len(shows)} 个直播间",
                        discovered_count=len(shows),
                        recording_count=0,
                        status="OK" if shows else "EMPTY",
                    )
                    scan_results[keyword_row.id] = {
                        "started_at": keyword_started,
                        "ended_at": keyword_ended,
                        "result": f"发现 {len(shows)} 个直播间",
                        "discovered_count": len(shows),
                        "status": "OK" if shows else "EMPTY",
                    }
                    for show in shows:
                        key = self._canonical_show_url(show.url)
                        if key in found:
                            duplicate_results += 1
                            entry = found[key]
                            entry["phrases"].add(phrase)
                            entry["live_seen"] = max(entry["live_seen"], search_started_monotonic)
                            # Prefer the record with an explicit platform show ID.
                            identity_basis = getattr(
                                show,
                                "identity_basis",
                                getattr(show, "session_identity_source", ""),
                            )
                            if identity_basis in {
                                "platform_show_id",
                                "whatnot_show_id",
                                "whatnot_show_link",
                            }:
                                entry["show"] = show
                        else:
                            found[key] = {"show": show, "phrases": {phrase},
                                          "live_seen": search_started_monotonic}
                self._set_scan_status(
                    scanned_keywords=index + 1,
                    failed_keywords=failed_keywords,
                    duplicate_results=duplicate_results,
                )
                if (
                    index + 1 < len(keywords)
                    and not self._stop.is_set()
                    and not verification_blocked
                ):
                    # One browser context is owned by this thread; serialized page
                    # requests keep Whatnot access paced while all keywords complete.
                    self._wait_with_browser_events(0.5)

                if verification_blocked:
                    break

            if verification_blocked:
                self._verification_required.set()
                self._paused.set()
                self._scan_requested.clear()
                with self._scan_request_lock:
                    self._requested_keyword_ids.clear()
                # Results returned before the challenge are real observations.
                # Save them and their sources without making any more browser
                # requests or starting/retrying recorders while verification is active.
                created_count = 0
                session_by_phrase: dict[str, list[str]] = {
                    row.phrase: [] for row in keywords
                }
                keyword_map = {row.phrase: row.id for row in keywords}
                for entry in found.values():
                    show = entry["show"]
                    phrases = tuple(sorted(entry["phrases"], key=str.casefold))
                    try:
                        persisted = self._persist_discovered_show(show, phrases, keyword_map)
                    except Exception as exc:  # noqa: BLE001 - preserve other observations
                        self.store.add_event("error", "whatnot", f"验证中保存已发现直播间失败：{exc}")
                        self.events.put(("error", f"保存已发现直播间失败：{exc}"))
                        continue
                    if persisted is None:
                        continue
                    session, created = persisted
                    created_count += int(created)
                    for phrase in phrases:
                        session_by_phrase.setdefault(phrase, []).append(session.session_id)
                for keyword_row in keywords:
                    result = scan_results.get(keyword_row.id)
                    if not result:
                        continue
                    recording_count = sum(
                        1 for session_id in session_by_phrase.get(keyword_row.phrase, [])
                        if (session := self.store.get_session(session_id))
                        and session.status == "RECORDING"
                    )
                    self.store.finish_keyword_scan(
                        keyword_row.id,
                        started_at=result["started_at"],
                        ended_at=result["ended_at"],
                        result=result["result"],
                        discovered_count=result["discovered_count"],
                        recording_count=recording_count,
                        status=result["status"],
                    )
                finished_at = _now().isoformat()
                unknown_message = f"页面状态未知；已暂停搜索和新页面导航，保留现有录制和仍可用的页面。原因：{_search_failure_detail(last_error)}"
                message = unknown_message if unknown_blocked else self._verification_pause_message()
                self._set_scan_status(
                    state="page_state_unknown" if unknown_blocked else "verification_required",
                    scanned_keywords=min(len(keywords), index + 1),
                    failed_keywords=failed_keywords,
                    current_keyword=None,
                    new_rooms=created_count,
                    duplicate_results=duplicate_results,
                    recordings_started=0,
                    blocking_reason=message,
                    round_finished_at=finished_at,
                    **({} if unknown_blocked else self._verification_status()),
                )
                self.store.add_event("error", "whatnot", message)
                self.events.put(("error", message))
                self.events.put(("info", f"暂停前已保存 {created_count} 个新直播间；新录制尚未启动。"))
                return

            created_count = 0
            started_count = 0
            recording_verification_blocked = False
            session_by_phrase: dict[str, list[str]] = {
                row.phrase: [] for row in keywords
            }
            # Commit every observation before opening any recording page.
            # A slow or blocked player must not keep the remaining shows only
            # in memory, where a crash or restart would lose them.
            persisted_shows = []
            keyword_map = {row.phrase: row.id for row in keywords}
            for entry in found.values():
                show = entry["show"]
                phrases = tuple(sorted(entry["phrases"], key=str.casefold))
                session = self._persist_discovered_show(
                    show,
                    phrases,
                    keyword_map,
                )
                if session is None:
                    continue
                created_count += int(session[1])
                live_session = session[0]
                self._live_search_evidence[live_session.session_id] = entry["live_seen"]
                for phrase in phrases:
                    session_by_phrase.setdefault(phrase, []).append(
                        live_session.session_id
                    )
                if session[1]:
                    self.events.put(("info", f"发现直播间：{show.name}"))
                persisted_shows.append((show, live_session))

            # Failed captures eligible for retry must re-enter the durable
            # waiting queue even if this round cannot attempt them immediately.
            for _show, session in persisted_shows:
                self._retry_failed_capture_if_due(session)

            # Start through the same live-verification path used by the queue,
            # including the first result. Search collection itself can be slow.
            before = len(self._recorders)
            self._start_waiting_sessions()
            started_count = max(0, len(self._recorders) - before)
            recording_verification_blocked = self._verification_required.is_set()
            if (not recording_verification_blocked
                    and len(self._recorders) >= self._recording_capacity()):
                # This call only marks overflow; it performs no browser work.
                self._start_waiting_sessions()

            for keyword_row in keywords:
                ids = session_by_phrase.get(keyword_row.phrase, [])
                recording_count = sum(
                    1
                    for session_id in ids
                    if (session := self.store.get_session(session_id))
                    and session.status == "RECORDING"
                )
                scan_result = scan_results.get(keyword_row.id)
                if scan_result:
                    self.store.finish_keyword_scan(
                        keyword_row.id,
                        started_at=scan_result["started_at"],
                        ended_at=scan_result["ended_at"],
                        result=scan_result["result"],
                        discovered_count=scan_result["discovered_count"],
                        recording_count=recording_count,
                        status=scan_result["status"],
                    )

            summary = {
                "state": (
                    ("page_state_unknown" if self.scan_status.get("state") == "page_state_unknown" else "verification_required")
                    if recording_verification_blocked
                    else "error"
                    if failed_keywords == len(keywords)
                    else "partial_error"
                    if failed_keywords
                    else "complete"
                ),
                "failed_keywords": failed_keywords,
                "new_rooms": created_count,
                "duplicate_results": duplicate_results,
                "recordings_started": started_count,
                "round_finished_at": _now().isoformat(),
            }
            if self._stop.is_set():
                summary["state"] = "cancelled"
            self._set_scan_status(**summary, current_keyword=None)
            round_label = "已取消" if self._stop.is_set() else "已完成"
            summary_message = (
                f"扫描轮次{round_label}：新发现 {created_count} 个直播间，"
                f"重复结果 {duplicate_results} 条，已启动录制 {started_count} 个，"
                f"失败关键词 {failed_keywords} 个。"
            )
            self.events.put(("info", summary_message))
        except WhatnotPageStateUnknown:
            self._pause_for_unknown_page()
            raise
        except Exception as exc:
            self._set_scan_status(
                state="error",
                current_keyword=None,
                blocking_reason=(
                    str(exc) if type(exc) is WhatnotBrowserProfileInUse
                    else "扫描轮次异常；请查看事件日志。"
                ),
                round_finished_at=_now().isoformat(),
            )
            raise

    @staticmethod
    def _canonical_show_url(url: str) -> str:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        host = host.removeprefix("www.")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower() or "https", host, path, "", ""))

    def _persist_discovered_show(
        self, show, phrases: tuple[str, ...], keyword_ids: dict[str, int]
    ):
        show_key = self._canonical_show_url(show.url)
        if self.platform == "whatnot" and self.store.show_was_rejected(self._rejection_key(show.url)):
            return None
        identity = getattr(show, "session_id", None) or show_key
        session_id = str(identity)
        # Fallback identities include first-seen time. Reuse only a matching live
        # capture from the same canonical room URL; never attach to postprocessing.
        source = getattr(
            show, "identity_basis", getattr(show, "session_identity_source", "")
        )
        if source and source not in {
            "platform_show_id",
            "whatnot_show_id",
            "whatnot_show_link",
        }:
            for active in self.store.list_sessions(
                statuses={"WAITING", "RECORDING", "CAPACITY_INSUFFICIENT"}, limit=1000
            ):
                if self._canonical_show_url(active.url) == show_key:
                    session_id = active.session_id
                    break
        first_seen = show.first_seen_at
        start_source = getattr(show, "start_time_source", "") or ""
        started_at = self._normalized_platform_start(
            getattr(show, "started_at", None), start_source
        )
        if started_at is None:
            start_source = "first_seen_estimate"
        if self.platform == "whatnot" and started_at:
            previous = self.store.get_session(session_id)
            if previous and previous.ended_at and previous.started_at and started_at > previous.started_at:
                session_id += ":" + hashlib.sha256(started_at.encode()).hexdigest()[:12]
        # Discovery is not recording. The qualification clock starts only when
        # this application's capture starts, even when platform start is known.
        audience_due = None
        session, created = self.store.create_session(
            session_id=session_id,
            room_id=show.room_id,
            name=show.name,
            url=show.url,
            started_at=started_at,
            start_time_source=start_source,
            first_seen_at=first_seen,
            keyword_phrases=phrases,
            audience_due_at=audience_due,
            identity_basis=source or "platform_show_id",
        )
        for phrase in phrases:
            keyword_id = keyword_ids.get(phrase)
            try:
                self.store.add_session_keyword_source(
                    session.session_id, keyword_id, phrase
                )
            except KeyError:
                # A keyword can be deleted from the UI while its scan is in flight.
                self.store.add_session_keyword_source(session.session_id, None, phrase)
        if session.status in {"WAITING", "CAPACITY_INSUFFICIENT"}:
            self.store.update_live_observation(
                session.session_id,
                current_viewers=show.viewers,
                current_viewers_raw=show.viewers_raw,
                observed_at=first_seen,
            )
        return session, created

    def _retry_failed_capture_if_due(self, session):
        """Retry a discoverable live session only when no capture ever began."""
        if (
            session.status != "FAILED"
            or session.video_path
            or session.audio_path
            or session.recording_path
            or session.transcript_text
            or session.qualification_status not in {"pending", "legacy"}
            or session.attempt_count >= _MAX_RECORDING_START_ATTEMPTS
        ):
            return session
        if session.retry_at:
            try:
                if datetime.fromisoformat(session.retry_at) > _now():
                    return session
            except ValueError:
                pass
        retried = self.store.transition(
            session.session_id,
            "WAITING",
            error=None,
        )
        self.store.add_event(
            "warning",
            "recording",
            "直播间仍出现在当前 Live Shows 搜索结果中；"
            f"将重试录制启动（失败 {session.attempt_count}/"
            f"{_MAX_RECORDING_START_ATTEMPTS} 次）。",
            session_id=session.session_id,
        )
        return retried

    @staticmethod
    def _normalized_platform_start(value: str | None, source: str) -> str | None:
        """Use a start time only when the parser identifies it as platform data."""
        source_key = source.casefold()
        if not value or "estimate" in source_key:
            return None
        if not any(token in source_key for token in ("platform", "whatnot")):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()

    def _set_scan_status(self, **updates: object) -> None:
        with self._scan_status_lock:
            self._scan_status.update(updates)

    def _start_recording(self, session, *, browser=None) -> None:
        if self._stop.is_set() or self._verification_required.is_set():
            return
        current = self.store.get_session(session.session_id)
        if current is None or current.status not in {"WAITING", "CAPACITY_INSUFFICIENT"}:
            return
        if session.session_id in self._recorders:
            return
        limit = self._recording_capacity()
        if len(self._recorders) >= limit:
            discovered_at = session.first_seen_at
            message = (
                f"发现直播间时录制容量已满（上限 {limit}，"
                f"发现时间 {discovered_at}）；从该时刻起的音频未录制。"
                "容量释放后会先确认直播仍在进行，再尝试启动录制。"
            )
            current = self.store.get_session(session.session_id)
            if current and current.status in {"WAITING", "CAPACITY_INSUFFICIENT"}:
                self.store.transition(
                    session.session_id, "CAPACITY_INSUFFICIENT", error=message
                )
            self.store.add_event(
                "warning",
                "recording",
                message,
                session_id=session.session_id,
            )
            self.events.put(
                ("warning", f"录制容量不足：{session.name}。{message}")
            )
            return
        if browser is None:
            browser = self._browser
        if browser is None:
            raise RuntimeError("Whatnot 浏览器不可用于录制")
        if isinstance(self._browser, BrowserPool):
            enabled = {keyword.id for keyword in self.store.list_keywords(enabled_only=True)}
            if not self._browser.has_slot(
                browser, self._recorders, keyword_ids=self._session_keyword_ids(session.session_id, enabled)
            ):
                return
        folder_name = _now().strftime("%Y-%m-%d")
        safe = (
            "".join(ch for ch in session.name if ch.isalnum() or ch in "-_ ")
            .strip()
            .replace(" ", "_")[:50]
        )
        safe_session_id = "".join(
            ch for ch in session.recording_task_id if ch.isalnum() or ch in "-_"
        )[:80]
        folder_name = f"{safe[:24] or 'show'}_{safe_session_id[-12:] or 'session'}"
        segment_id = uuid.uuid4().hex
        segment_filename = segment_id[:8]
        video_path = (
            Path(self.settings.video_dir) / _now().strftime("%Y-%m-%d") / folder_name / f"v-{segment_filename}.webm"
            if self.settings.video_recording_enabled
            else None
        )
        audio_path = (
            Path(self.settings.audio_dir) / _now().strftime("%Y-%m-%d") / folder_name / f"a-{segment_filename}.webm"
            if self.settings.audio_recording_enabled or self.settings.video_recording_enabled
            else None
        )
        if not self.settings.video_recording_enabled and not self.settings.audio_recording_enabled:
            raise RuntimeError("请至少启用视频录制或音频录制")
        for output_path in (video_path, audio_path):
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
        storage_error = self._recording_free_space_error(
            [path for path in (video_path, audio_path) if path is not None]
        )
        if storage_error:
            self.store.add_event(
                "error", "storage", storage_error, session_id=session.session_id
            )
            self.events.put(("error", storage_error))
            current = self.store.get_session(session.session_id)
            if current and current.status in {"WAITING", "CAPACITY_INSUFFICIENT"}:
                self.store.transition(
                    current.session_id, "FAILED", error=storage_error
                )
            raise RuntimeError(storage_error)
        expected_show_id = self._show_id_from_url(session.url)
        if not expected_show_id:
            self._pause_for_unknown_page(session.session_id, "待录制场次身份未知，未打开录制页面")
            raise WhatnotPageStateUnknown("待录制场次身份未知，未启动采集")
        page = browser.new_room_page(
            session.url,
            require_audio=True,
            require_video=self.settings.video_recording_enabled,
        )
        if self.settings.video_recording_enabled:
            from .recording.browser_av import BrowserAVRecorder

            recorder = BrowserAVRecorder(
                page,
                video_path,
                audio_path,
                video_mime_type=self.settings.video_mime_type,
                video_bitrate_bps=self.settings.video_bitrate_bps,
            )
        else:
            from .recording.browser_audio import BrowserAudioRecorder

            recorder = BrowserAudioRecorder(page, audio_path)
        # Bind capture startup to the requested show, not a possible SPA redirect.
        recorder.expected_show_id = expected_show_id
        try:
            if self._stop.is_set():
                raise RuntimeError("程序正在停止，未启动新的录制。")
            if any(p.exists() for p in (video_path, audio_path) if p):
                raise RuntimeError("录制片段文件已存在，拒绝覆盖")
            self.store.add_recording_segment(segment_id, session.session_id,
                str(video_path) if video_path else None, str(audio_path) if audio_path else None)
            if self.settings.video_recording_enabled:
                recorder.start(timeslice_ms=1000)
            else:
                recorder.start()
            from .recording_health import recording_paths
            self._file_watchdog.register(session.session_id, session.name, recording_paths(recorder))
            self.store.update_recording_segment(segment_id, "recording", timestamp=_now().isoformat())
            if self._stop.is_set():
                self.store.save_recording_assets(
                    session.session_id,
                    video_path=str(video_path) if video_path else None,
                    audio_path=str(audio_path) if audio_path else None,
                )
                raise RuntimeError("程序停止时录制刚启动，已停止并保留媒体。")
            self.store.transition(session.session_id, "RECORDING")
            self.store.save_recording_assets(
                session.session_id,
                video_path=str(video_path) if video_path else None,
                audio_path=str(audio_path) if audio_path else None,
            )
            self.store.save_recording_started(
                session.session_id, _now().isoformat(),
                qualification_minutes=max(1, int(self.settings.qualification_minutes)),
                minimum_viewers=max(0, int(self.settings.minimum_viewers)),
            )
        except Exception:
            # The page may have become a challenge after new_room_page returned.
            # Classify before cleanup; unknown is not permission to close it or
            # send a stop command into a replacement document.
            blocked = None
            try:
                browser.inspect_page(page)
            except WhatnotVerificationRequired as exc:
                blocked = exc
            except Exception as exc:  # noqa: BLE001 - retain unclassified pages
                marker = getattr(browser, "mark_page_state_unknown", None)
                if callable(marker):
                    marker(page)
                blocked = WhatnotPageStateUnknown(f"录制启动失败后页面状态未知：{exc}")
            if (blocked is None and getattr(page, "url", None)
                    and not self._audience_page_matches(session, page)):
                blocked = WhatnotPageStateUnknown("录制启动期间场次身份已变化；保留页面与现有片段")
            if blocked is not None:
                self.store.save_recording_assets(
                    session.session_id,
                    video_path=str(video_path) if video_path and video_path.exists() else None,
                    audio_path=str(audio_path) if audio_path and audio_path.exists() else None,
                )
                if getattr(recorder, "_started", False):
                    # Retain ownership if start succeeded but a later step failed.
                    # The normal monitor can still inspect/interrupt this capture.
                    self._active_segments[session.session_id] = segment_id
                    self._recorders[session.session_id] = (recorder, page)
                    self._recording_started[session.session_id] = _now()
                    self.store.transition(session.session_id, "RECORDING")
                    self.store.save_recording_started(
                        session.session_id, _now().isoformat(),
                        qualification_minutes=max(1, int(self.settings.qualification_minutes)),
                        minimum_viewers=max(0, int(self.settings.minimum_viewers)),
                    )
                else:
                    self._file_watchdog.unregister(session.session_id)
                    self.store.update_recording_segment(segment_id, "failed", timestamp=_now().isoformat(), reason="录制启动受验证或未知页面阻断；片段与页面已保留")
                if isinstance(blocked, WhatnotPageStateUnknown):
                    self._pause_for_unknown_page(session.session_id, str(blocked))
                else:
                    self._pause_for_verification("录制启动时检测到安全验证", session.session_id)
                raise blocked
            self._file_watchdog.unregister(session.session_id)
            self.store.update_recording_segment(segment_id, "failed", timestamp=_now().isoformat(), reason="录制启动失败")
            try:
                if getattr(recorder, "_started", True):
                    if self._recording_document_lost(recorder, page):
                        recorder.abort_lost_document()
                    else:
                        recorder.stop()
            except Exception:
                log.exception(
                    "Could not stop partially started recorder for %s",
                    session.session_id,
                )
            try:
                page.close()
            except Exception:
                log.exception(
                    "Could not close failed recording page for %s", session.session_id
                )
            raise
        self._active_segments[session.session_id] = segment_id
        self._recorders[session.session_id] = (recorder, page)
        self._recording_started[session.session_id] = _now()
        message = f"已开始录制：{session.name}"
        if isinstance(self._browser, BrowserPool):
            message += f"（账号：{self._browser.account_name(page)}）"
            self.store.add_event("info", "recording", message, session_id=session.session_id)
        self.events.put(("info", message))

    def _session_keyword_ids(self, session_id, enabled):
        return {source.keyword_id for source in self.store.session_keyword_sources(session_id)
                if source.keyword_id in enabled}

    def _resolve_live_episode(self, candidate, room_key, state):
        observed_start = self._normalized_platform_start(state.get("broadcastStart"), "platform_started_at")
        episode_id, start = self.store.live_episode(room_key, observed_start)
        if observed_start and start and observed_start < start:
            return None
        rejected = self.store.show_was_rejected(self._rejection_key(candidate.url, episode_id))
        legacy_rejected = episode_id == f"platform-live:{room_key}" and self.store.show_was_rejected(
            self._rejection_key(candidate.url)
        )
        if rejected or legacy_rejected:
            return None
        sources = self.store.session_keyword_sources(candidate.session_id)
        episode, _ = self.store.create_session(
            episode_id, room_key, candidate.name, candidate.url,
            started_at=start, start_time_source="platform_started_at" if start else "first_seen_estimate",
            first_seen_at=candidate.first_seen_at, identity_basis="platform_show_id",
            keyword_phrases=(source.phrase_snapshot for source in sources),
        )
        for source in sources:
            self.store.add_session_keyword_source(episode_id, source.keyword_id, source.phrase_snapshot)
        if candidate.session_id.endswith(":candidate"):
            self.store.remove_room_candidate(candidate.session_id)
        elif candidate.session_id != episode_id:
            self.store.transition(
                candidate.session_id, "INTERRUPTED", error="等候期间该房间已切换到新的直播场次。"
            )
        return episode

    def _start_waiting_sessions(self) -> None:
        if self._stop.is_set() or self._browser is None or self._verification_required.is_set():
            return
        limit = self._recording_capacity()
        capacity = max(0, limit - len(self._recorders))
        sessions = self.store.waiting_sessions(limit=1000, platform=self.platform)
        if capacity == 0:
            for session in sessions:
                if session.status == "WAITING":
                    message = (
                        f"发现直播间时录制容量已满（上限 {limit}，"
                        f"发现时间 {session.first_seen_at}）；从该时刻起的音频未录制。"
                    )
                    self.store.transition(
                        session.session_id, "CAPACITY_INSUFFICIENT", error=message
                    )
                    self.events.put(
                        ("warning", f"录制容量不足：{session.name}。{message}")
                    )
            return
        # Browser operations can each take tens of seconds. Process at most one
        # room per pass so scans and audience checks regain the owner thread.
        now = _now()
        pool = self._browser if isinstance(self._browser, BrowserPool) else None
        enabled = {keyword.id for keyword in self.store.list_keywords(enabled_only=True)} if pool else set()
        active_rooms = {self._show_id_from_url(page.url) for _, page in self._recorders.values()
                        if getattr(page, "url", None)}
        for session in sessions:
            if session.retry_at:
                try:
                    retry_at = datetime.fromisoformat(session.retry_at)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    if retry_at > now:
                        continue
                except ValueError:
                    pass
            try:
                room_key = self._show_id_from_url(session.url)
                if self.platform != "whatnot" and room_key in active_rooms:
                    continue
                browser = pool.room_browser(
                    self._recorders, keyword_ids=self._session_keyword_ids(session.session_id, enabled)
                ) if pool else self._browser
                if browser is None:
                    continue
                state = browser.inspect_live(session.url)
                if self._stop.is_set():
                    return
                if state.get("ended"):
                    if self.platform != "whatnot" and room_key:
                        self.store.mark_room_ended(room_key)
                        if session.session_id.endswith(":candidate"):
                            self.store.remove_room_candidate(session.session_id)
                            continue
                    self.store.transition(
                        session.session_id,
                        "INTERRUPTED",
                        error=(
                            "等待核实时直播已结束；"
                            f"错过的时间段从 {session.first_seen_at} 开始。"
                        ),
                    )
                    self.events.put(
                        (
                            "warning",
                            f"错过直播间 {session.name}：等待核实时直播已结束。",
                        )
                    )
                elif state.get("live") is True or (
                    self.platform == "whatnot" and session.status == "WAITING"
                    and (seen := self._live_search_evidence.get(session.session_id)) is not None
                    and 0 <= time.monotonic() - seen <= 90
                ):
                    # Search cards are selected only when explicitly marked LIVE.
                    # The search-start timestamp is a conservative lower bound;
                    # explicit current end-state always wins.
                    if self.platform != "whatnot":
                        if room_key is None or state.get("live") is not True:
                            continue
                        episode = self._resolve_live_episode(session, room_key, state)
                        if episode is None:
                            self.store.transition(
                                session.session_id, session.status,
                                retry_at=(_now() + timedelta(seconds=60)).isoformat(),
                            )
                            continue
                        if episode.status not in {"WAITING", "CAPACITY_INSUFFICIENT"}:
                            continue
                        session = episode
                    if pool is None:
                        self._start_recording(session)
                    else:
                        self._start_recording(session, browser=browser)
                else:
                    raise RuntimeError("无法确认直播间仍在直播，稍后重试。")
            except WhatnotPageStateUnknown as exc:
                self._pause_for_unknown_page(session.session_id, str(exc))
            except WhatnotVerificationRequired:
                self._pause_for_verification("核实等待中的直播间时遇到安全验证", session.session_id)
            except Exception as exc:  # noqa: BLE001 - surface retryable room errors
                current = self.store.get_session(session.session_id)
                if current and current.status == "RECORDING":
                    self.store.transition(
                        current.session_id,
                        "FAILED",
                        error=f"无法恢复录制：{exc}",
                    )
                elif current and current.status in {"WAITING", "CAPACITY_INSUFFICIENT"}:
                    self.store.transition(
                        current.session_id,
                        current.status,
                        error=f"等待录制，暂时无法核实或启动：{exc}",
                        retry_at=(_now() + timedelta(seconds=60)).isoformat(),
                    )
                self.store.add_event(
                    "warning",
                    "recording",
                    f"无法恢复等待中的直播间：{exc}",
                    session_id=session.session_id,
                )
            break

    def _capture_ready_for_postprocess(self, session) -> bool:
        """Require durable finalization, not merely a qualified/growing file."""
        if session.session_id in self._recorders or session.status == "RECORDING":
            return False
        segments = self.store.list_recording_segments(session.session_id)
        paths = [Path(value) for value in (
            session.video_path, session.audio_path or session.recording_path,
        ) if value]
        try:
            if not paths or any(not path.is_file() or path.stat().st_size <= 0 for path in paths):
                return False
        except OSError:
            return False
        return bool(
            session.ended_at
            and segments
            and all(
                segment["status"] == "completed" and segment["reason"] == "natural_end"
                for segment in segments
            )
        )

    def retry_session(self, session_id: str) -> None:
        with self._history_lock:
            self._retry_session(session_id)

    def _retry_session(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        if session_id in self._recorders or session.status in {"RECORDING", "TRANSCRIBING", "UPLOADING"}:
            self.events.put(("warning", "任务仍在录制或后处理中，未重复安排转写或上传。"))
            return
        if self.recover_finished_recording(session_id):
            return
        if any(s["status"] == "interrupted" for s in self.store.list_recording_segments(session_id)):
            self.events.put(("warning", "该场次有中断片段，未将单个片段作为完整直播归档。"))
            return
        if session.qualification_status == "rejected_low_viewers":
            if session.audience_outcome != "manual_confirmed_low":
                self._remove_low_viewer_recording(session)
            return
        if session.qualification_status == "cleanup_failed":
            self.store.add_event(
                "error",
                "recovery",
                "未能清理未达人数门槛的媒体文件，因此未安排转写重试。",
                session_id=session_id,
            )
            return
        if session.qualification_status != "qualified":
            self.store.add_event(
                "warning", "recovery", "观众人数未确认达标，不能重试转写或上传；媒体文件已保留。",
                session_id=session_id,
            )
            self.events.put(("warning", "观众人数未确认达标，未安排转写或上传。"))
            return
        if not session.feishu_record_id and not self._capture_ready_for_postprocess(session):
            self.events.put(("warning", "缺少自然结束且已完整收尾的录制片段证据；媒体已保留，未安排后处理。"))
            return
        try:
            transcript = None if session.feishu_record_id else self._saved_transcript(session)
        except TranscriptRecoveryError:
            return
        if transcript is not None or session.feishu_record_id:
            self.store.transition(session_id, "UPLOADING")
            self._submit_postprocess(self._retry_feishu_upload, session_id, transcript or "")
        elif (session.audio_path or session.recording_path) and Path(
            session.audio_path or session.recording_path
        ).is_file():
            self.store.transition(session_id, "TRANSCRIBING")
            self._submit_postprocess(
                self._transcribe_and_upload,
                session_id,
                Path(session.audio_path or session.recording_path),
            )
        else:
            self.store.add_event(
                "error",
                "recovery",
                "没有可用于重试的音频或转写文本。",
                session_id=session_id,
            )
            return
        self.events.put(("info", f"已为 {session.name} 排队重试后处理。"))

    def _monitor_recordings(self) -> None:
        if self._browser is None:
            return
        for session_id, (recorder, page) in list(self._recorders.items()):
            session = self.store.get_session(session_id)
            if session is None or session.status == "INTERRUPTED":
                continue
            try:
                if callable(getattr(recorder, "capture_health", None)):
                    health = self._recording_capture_health(session, recorder, page)
                    if self._interrupt_unsafe_capture(session, recorder, health):
                        continue
                state = self._browser.inspect_page(page)
                observed_start = self._normalized_platform_start(state.get("broadcastStart"), "platform_started_at")
                if (session.session_id.startswith("platform-live:") and session.started_at
                        and observed_start and observed_start > session.started_at):
                    self._interrupt_recording(
                        session, recorder, "同一房间已切换到下一场开播；原录制片段已保留。",
                        local_abort=True,
                    )
                    continue
                # SPA transitions need not destroy the capture global. Recheck
                # ownership before attributing the DOM read to the original show.
                health = self._recording_capture_health(session, recorder, page)
                if self._interrupt_unsafe_capture(session, recorder, health):
                    continue
                storage_error = self._recording_storage_error(recorder)
                if storage_error:
                    self._fail_recording_for_storage(session, recorder, page, storage_error)
                    continue
                if health["status"] == "terminated" and not state.get("ended_candidate"):
                    self._interrupt_recording(
                        session, recorder,
                        "原采集轨道或录制器已终止，但没有同场直播自然结束证据；保留不完整片段。",
                        local_abort=True,
                    )
                    continue
                viewers = state.get("viewers")
                if health["status"] == "active" and type(viewers) is int and viewers >= 0 and not state.get("ended_candidate"):
                    self.store.update_live_observation(
                        session_id,
                        current_viewers=viewers,
                        current_viewers_raw=state.get("viewers_raw"),
                        observed_at=_now().isoformat(),
                    )
                if session_id in self._manual_stop_requests:
                    self._interrupt_recording(
                        session, recorder,
                        "录制被手动停止；已保留不完整片段，未按自然结束转写或归档。",
                    )
                elif state.get("ended_candidate"):
                    confirmations = self._end_confirmations.get(session_id, 0) + 1
                    self._end_confirmations[session_id] = confirmations
                    if confirmations >= 3:
                        self._finish_recording(session, recorder, page)
                else:
                    self._end_confirmations.pop(session_id, None)
            except BrowserReadTimeout as exc:
                self._end_confirmations.pop(session_id, None)
                marker = getattr(self._browser, "mark_page_state_unknown", None)
                if callable(marker):
                    marker(page)
                self._pause_for_unknown_page(session_id, str(exc))
            except WhatnotPageStateUnknown as exc:
                self._end_confirmations.pop(session_id, None)
                marker = getattr(self._browser, "mark_page_state_unknown", None)
                if callable(marker):
                    marker(page)
                self._pause_for_unknown_page(session_id, str(exc))
            except WhatnotVerificationRequired:
                self._end_confirmations.pop(session_id, None)
                self._pause_for_verification("录制期间检测到安全验证", session_id)
                try:
                    document_lost = self._recording_document_lost(recorder, page)
                    if document_lost or session_id in self._manual_stop_requests:
                        self._interrupt_recording(
                            session, recorder,
                            "录制期间遇到安全验证，原采集已丢失或被手动停止；"
                            "验证页已保留供人工处理，现有媒体已保留，未按直播结束或低人数处理。",
                            document_lost=document_lost,
                        )
                except Exception:  # A document can be between navigation contexts.
                    log.debug("Waiting for recording document to settle", exc_info=True)
            except Exception as exc:  # noqa: BLE001 - retain retryable audience failure
                self._end_confirmations.pop(session_id, None)
                if callable(getattr(page, "is_closed", None)) and page.is_closed():
                    self._interrupt_recording(session, recorder, "录制页面已关闭；已保留现有媒体。", document_lost=True)
                    continue
                self.store.add_event(
                    "warning",
                    "whatnot",
                    f"直播间状态检查失败：{exc}",
                    session_id=session_id,
                )

    def recording_health_warning(self, session_id: str) -> bool:
        """Thread-safe snapshot for the desktop UI; this does not stop capture."""
        return self._file_watchdog.warning(session_id)

    @staticmethod
    def _recording_document_lost(recorder, page) -> bool:
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            return True
        health_check = getattr(recorder, "capture_health", None)
        if callable(health_check):
            status = health_check().get("status")
            if status == "unknown":
                raise WhatnotPageStateUnknown("录制采集身份未知；保留页面与媒体")
            return status in {"lost", "replaced"}
        check = getattr(recorder, "has_capture_state", None)
        return callable(check) and not check()

    def _recording_capture_health(self, session, recorder, page, *, finalized=False) -> dict:
        """Read-only ownership check shared by monitoring and due qualification."""
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            return {"status": "lost"}
        check = getattr(recorder, "finalization_identity" if finalized else "capture_health", None)
        if callable(check):
            health = check()
            status = health.get("status")
            if status in {"lost", "replaced"}:
                return health
            # Structured snapshots never get the URL-less test-adapter exemption.
            href = health.get("href")
            if not href or not self._show_id_from_url(href):
                raise WhatnotPageStateUnknown("录制页面场次身份未知；保留页面与媒体")
            if not self._audience_page_matches_url(session, href):
                return {**health, "status": "drifted"}
            allowed = {"active", "terminated", "drifted"} | ({"finalized"} if finalized else set())
            if status not in allowed:
                raise WhatnotPageStateUnknown("录制采集身份或轨道状态未知；保留页面与媒体")
            return health
        # Compatibility for lightweight mock/legacy recorder adapters only.
        if self._recording_document_lost(recorder, page):
            return {"status": "lost"}
        if not self._audience_page_matches(session, page):
            return {"status": "drifted"}
        return {"status": "active"}

    def _interrupt_unsafe_capture(self, session, recorder, health) -> bool:
        status = health["status"]
        if status not in {"lost", "replaced", "drifted"}:
            return False
        reason = (
            "录制页面已切换为其他直播场次；原场次仅保留不完整片段。"
            if status == "drifted" else
            "录制页的原采集状态已丢失或被替换；已保留不完整片段，缺失内容无法补录。"
        )
        self._interrupt_recording(
            session, recorder, reason, document_lost=status == "lost", local_abort=True,
        )
        return True

    def _interrupt_recording(
        self, session, recorder, reason: str, *, document_lost: bool = False,
        local_abort: bool = False,
    ) -> None:
        """Persist partial capture even when its original document is gone.

        Do not close/navigate the page: it may contain a human-verification flow.
        Do not restart into the same paths, classify as ended, or enqueue ASR.
        """
        try:
            abort = getattr(recorder, "abort_lost_document", None)
            if document_lost and callable(abort):
                abort()
                reason += " 原录制文档已丢失，仅本地关闭文件；保留的是不完整片段。"
            elif local_abort:
                quarantine = getattr(recorder, "abort_stale_capture", abort)
                if callable(quarantine):
                    quarantine()
                # No generic stop: it might command the newer global/document.
                reason += " 仅隔离原录制文件，未操作当前页面或替换后的采集。"
            else:
                recorder.stop()
        except Exception as exc:  # noqa: BLE001 - stop must not prevent saving partial media
            reason += f" 收尾未能完整完成：{exc}；文件可能是不完整片段。"
        ended_at = _now().isoformat()
        segment_id = self._active_segments.pop(session.session_id, None)
        if segment_id:
            self.store.update_recording_segment(segment_id, "interrupted", timestamp=ended_at, reason=reason)
        started = self._recording_started.pop(session.session_id, _now())
        video_path = getattr(recorder, "video_path", None)
        audio_path = getattr(recorder, "audio_path", getattr(recorder, "output_path", None))
        self.store.save_recording_assets(
            session.session_id,
            video_path=str(video_path) if video_path else None,
            audio_path=str(audio_path) if audio_path else None,
        )
        if audio_path:
            self.store.save_recording(
                session.session_id, str(audio_path), ended_at=ended_at,
                duration_seconds=max(0, (_now() - started).total_seconds()),
            )
        self.store.transition(session.session_id, "INTERRUPTED", error=reason)
        self._file_watchdog.unregister(session.session_id)
        self._recorders.pop(session.session_id, None)
        self._end_confirmations.pop(session.session_id, None)
        self._manual_stop_requests.discard(session.session_id)
        self.store.add_event("warning", "recording", reason, session_id=session.session_id)
        self.events.put(("warning", f"{session.name}：{reason}"))

    def _recording_storage_error(self, recorder) -> str | None:
        media_paths = [
            Path(value)
            for value in (
                getattr(recorder, "video_path", None),
                getattr(recorder, "audio_path", getattr(recorder, "output_path", None)),
            )
            if value
        ]
        sizes: list[tuple[Path, int]] = []
        for path in media_paths:
            try:
                sizes.append((path, path.stat().st_size if path.is_file() else 0))
            except OSError as exc:
                return f"无法检查录制文件 {path}：{exc}"

        max_size_gib = float(self.settings.max_recording_size_gib)
        total_bytes = sum(size for _, size in sizes)
        max_size_bytes = max_size_gib * (1024**3)
        if max_size_gib > 0 and total_bytes >= max_size_bytes:
            return (
                f"单场录制文件已达到大小上限：{total_bytes / (1024**3):.3f} / "
                f"{max_size_gib:.3f} GiB。已保留已录制的媒体文件。"
            )

        return self._recording_free_space_error(media_paths)

    def _recording_free_space_error(self, media_paths: list[Path]) -> str | None:
        warning_gib = float(self.settings.disk_warning_gib)
        if warning_gib <= 0:
            return None
        roots = [path.parent for path in media_paths]
        if not roots:
            roots = [Path(self.settings.video_dir), Path(self.settings.audio_dir)]
        distinct_roots: list[Path] = []
        seen_devices: set[int] = set()
        for root in roots:
            try:
                device = root.stat().st_dev
            except OSError as exc:
                return f"无法检查录制所在磁盘 {root}：{exc}"
            if device not in seen_devices:
                seen_devices.add(device)
                distinct_roots.append(root)
        for root in distinct_roots:
            try:
                free_gib = shutil.disk_usage(root).free / (1024**3)
            except OSError as exc:
                return f"无法检查 {root} 的可用磁盘空间：{exc}"
            if free_gib < warning_gib:
                return (
                    f"{root} 的可用空间已降至 {free_gib:.2f} GiB，"
                    f"低于录制安全阈值 {warning_gib:.2f} GiB。已保留已录制的媒体文件。"
                )
        return None

    def _fail_recording_for_storage(self, session, recorder, page, reason: str) -> None:
        segment_id = self._active_segments.pop(session.session_id, None)
        if segment_id:
            self.store.update_recording_segment(segment_id, "interrupted", timestamp=_now().isoformat(), reason=reason)
        stop_errors: list[str] = []
        try:
            recorder.stop()
        except Exception as exc:  # noqa: BLE001 - retain data and surface stop failure
            stop_errors.append(f"录制器停止失败：{exc}")
        # Keep the page: stop() may reject ownership after an intervening SPA
        # transition, and closing now could destroy a replacement/verification.

        ended_at = _now().isoformat()
        started = self._recording_started.pop(session.session_id, _now())
        video_path = getattr(recorder, "video_path", None)
        audio_path = getattr(
            recorder,
            "audio_path",
            getattr(recorder, "output_path", None),
        )
        try:
            self.store.save_recording_assets(
                session.session_id,
                video_path=str(video_path) if video_path else None,
                audio_path=str(audio_path) if audio_path else None,
            )
            if audio_path:
                self.store.save_recording(
                    session.session_id,
                    str(audio_path),
                    ended_at=ended_at,
                    duration_seconds=max(0, (_now() - started).total_seconds()),
                )
        except Exception as exc:  # noqa: BLE001 - failed state still needs recording
            stop_errors.append(f"无法保存媒体信息：{exc}")

        if stop_errors:
            reason += " " + " ".join(stop_errors)
        current = self.store.get_session(session.session_id)
        if current and current.status not in {"FAILED", "COMPLETED"}:
            self.store.transition(session.session_id, "FAILED", error=reason)
        self.store.add_event(
            "error", "storage", reason, session_id=session.session_id
        )
        self.events.put(("error", f"{session.name} 的录制已停止：{reason}"))
        self._file_watchdog.unregister(session.session_id)
        self._recorders.pop(session.session_id, None)
        self._end_confirmations.pop(session.session_id, None)

    def _finish_recording(self, session, recorder, page) -> None:
        structured = callable(getattr(recorder, "capture_health", None))
        if structured:
            try:
                health = self._recording_capture_health(session, recorder, page)
                if self._interrupt_unsafe_capture(session, recorder, health):
                    return
                state = self._browser.inspect_page(page)
                health = self._recording_capture_health(session, recorder, page)
                if self._interrupt_unsafe_capture(session, recorder, health):
                    return
                if (not state.get("ended_candidate")
                        or self._end_confirmations.get(session.session_id, 0) < 3):
                    self._end_confirmations.pop(session.session_id, None)
                    if health["status"] == "terminated":
                        self._interrupt_recording(
                            session, recorder, "采集已终止但无法确认同场直播自然结束；保留不完整片段。",
                            local_abort=True,
                        )
                    return
            except (BrowserReadTimeout, WhatnotPageStateUnknown) as exc:
                self._end_confirmations.pop(session.session_id, None)
                self._pause_for_unknown_page(session.session_id, str(exc))
                return
            except WhatnotVerificationRequired:
                self._end_confirmations.pop(session.session_id, None)
                self._pause_for_verification("收尾前检测到安全验证", session.session_id)
                return
        try:
            recorder.stop()
            if structured:
                state = self._browser.inspect_page(page)
                health = self._recording_capture_health(session, recorder, page, finalized=True)
                if self._interrupt_unsafe_capture(session, recorder, health):
                    return
                if health["status"] != "finalized" or not state.get("ended_candidate"):
                    raise RuntimeError("采集停止后身份或自然结束状态未知；仅保留不完整片段")
            ended_at = _now().isoformat()
            segment_id = self._active_segments.pop(session.session_id, None)
            if segment_id:
                self.store.update_recording_segment(
                    segment_id, "completed", timestamp=ended_at, reason="natural_end",
                )
                room_key = self._show_id_from_url(session.url)
                if room_key and session.session_id.startswith("platform-live:"):
                    self.store.mark_room_ended(room_key, session.session_id)
            started = self._recording_started.pop(session.session_id, _now())
            video_path = getattr(recorder, "video_path", None)
            audio_path = getattr(
                recorder,
                "audio_path",
                getattr(recorder, "output_path", None),
            )
            self.store.save_recording_assets(
                session.session_id,
                video_path=str(video_path) if video_path else None,
                audio_path=str(audio_path) if audio_path else None,
            )
            if audio_path:
                self.store.save_recording(
                    session.session_id,
                    str(audio_path),
                    ended_at=ended_at,
                    duration_seconds=max(0, (_now() - started).total_seconds()),
                )
            if structured and self._audience_page_matches(session, page):
                try:
                    page.close()
                except Exception:
                    log.warning("Could not close the finished recording tab", exc_info=True)
            if any(s["status"] == "interrupted" for s in self.store.list_recording_segments(session.session_id)):
                self._file_watchdog.unregister(session.session_id)
                self._recorders.pop(session.session_id, None)
                self._end_confirmations.pop(session.session_id, None)
                reason = "录制存在中断与多个片段；已保留各片段，未将单个片段作为完整直播转写或归档。"
                self.store.transition(session.session_id, "FAILED", error=reason)
                self.store.add_event("warning", "recovery", reason, session_id=session.session_id)
                self.events.put(("warning", f"{session.name}：{reason}"))
                return
            current = self.store.get_session(session.session_id)
            if current:
                current = self._apply_end_audience(current)
            if current and current.qualification_status == "pending":
                end_time = datetime.fromisoformat(ended_at)
                try:
                    due_time = datetime.fromisoformat(current.audience_due_at or "")
                    if due_time.tzinfo is None:
                        due_time = due_time.replace(tzinfo=UTC)
                except ValueError:
                    try:
                        if not current.recording_started_at:
                            raise ValueError("no recording start")
                        start = datetime.fromisoformat(current.recording_started_at)
                        if start.tzinfo is None:
                            raise ValueError("no timezone")
                        minutes = current.qualification_minutes or max(1, int(self.settings.qualification_minutes))
                        due_time = start + timedelta(minutes=minutes)
                    except ValueError:
                        due_time = end_time
                ended_early = due_time > end_time
                self.store.save_audience(
                    session.session_id,
                    value=None,
                    raw_value=None,
                    captured_at=ended_at,
                    outcome="ended_before_due" if ended_early else "missed",
                )
                self.store.set_qualification_status(
                    session.session_id,
                    "ended_before_threshold" if ended_early else "viewer_check_failed",
                )
            current = self.store.get_session(session.session_id)
            if current and current.qualification_status == "rejected_low_viewers":
                self._file_watchdog.unregister(session.session_id)
                self._recorders.pop(session.session_id, None)
                self._end_confirmations.pop(session.session_id, None)
                self._remove_low_viewer_recording(current)
                return
            if current is None or current.qualification_status != "qualified":
                self._file_watchdog.unregister(session.session_id)
                self._recorders.pop(session.session_id, None)
                self._end_confirmations.pop(session.session_id, None)
                if current:
                    reason = "直播结束时观众人数未确认达标；已保留媒体，未进行转写或上传。"
                    self.store.transition(session.session_id, "FAILED", error=reason)
                    self.store.add_event("warning", "audience", reason, session_id=session.session_id)
                    self.events.put(("warning", f"{session.name}：{reason}"))
                    if structured and current.video_path:
                        self._submit_postprocess(self._export_finalized_video, session.session_id)
                return
            from .recording_health import empty_recording_channels
            empty = empty_recording_channels(recorder)
            if empty:
                raise RuntimeError(
                    "录制结束后发现空文件：" + "、".join(empty)
                    + "；已保留现有媒体，未进行转写或上传。"
                )
            self.store.transition(session.session_id, "TRANSCRIBING")
            self._file_watchdog.unregister(session.session_id)
            self._recorders.pop(session.session_id, None)
            self._end_confirmations.pop(session.session_id, None)
            if audio_path:
                self._submit_postprocess(
                    self._transcribe_and_upload,
                    session.session_id,
                    Path(audio_path),
                )
            else:
                self.store.transition(session.session_id, "UPLOADING")
                self._submit_postprocess(
                    self._retry_feishu_upload,
                    session.session_id,
                    "此录制任务未启用音频转写。",
                )
        except Exception as exc:
            log.exception("Recording finalization failed for %s", session.session_id)
            if structured:
                if isinstance(exc, (BrowserReadTimeout, WhatnotPageStateUnknown)):
                    self._pause_for_unknown_page(session.session_id, str(exc))
                elif isinstance(exc, WhatnotVerificationRequired):
                    self._pause_for_verification("收尾后检测到安全验证", session.session_id)
                self._interrupt_recording(
                    session, recorder, f"录制收尾或身份复核未完成：{exc}；保留不完整片段。",
                    local_abort=True,
                )
                return
            current = self.store.get_session(session.session_id)
            if current and current.status not in {"FAILED", "COMPLETED"}:
                self.store.transition(session.session_id, "FAILED", error=str(exc))
            self._file_watchdog.unregister(session.session_id)
            self._recorders.pop(session.session_id, None)
            self.events.put(
                ("error", f"{session.name} 的录制收尾处理失败：{exc}")
            )

    @staticmethod
    def _is_recent_end_sample(session, timestamp) -> bool:
        try:
            start = datetime.fromisoformat(session.recording_started_at or "")
            end = datetime.fromisoformat(session.ended_at or "")
            sample = datetime.fromisoformat(timestamp or "")
            return start <= sample <= end and (end - sample).total_seconds() <= 120
        except (TypeError, ValueError):
            return False

    def _apply_end_audience(self, session):
        value = session.audience_value
        if session.audience_outcome not in {"captured", "captured_late", "captured_at_end"} or type(value) is not int:
            value = session.current_viewers
            if type(value) is not int or value < 0 or not self._is_recent_end_sample(session, session.observed_at):
                return session
            session = self.store.save_audience(
                session.session_id, value=value, raw_value=session.current_viewers_raw,
                captured_at=session.observed_at, outcome="captured_at_end",
            )
        status = "qualified" if value > self._minimum_viewers_for(session) else "rejected_low_viewers"
        return self.store.set_qualification_status(session.session_id, status)

    def recover_finished_recording(self, session_id: str) -> bool:
        with self._history_lock:
            session = self.store.get_session(session_id)
            if (session is None or session.status != "FAILED" or session.feishu_record_id
                    or session.qualification_status not in {"ended_before_threshold", "viewer_check_failed"}
                    or not self._capture_ready_for_postprocess(session)):
                return False
            session = self._apply_end_audience(session)
            if session.qualification_status == "rejected_low_viewers":
                self._remove_low_viewer_recording(session)
                return True
            audio = session.audio_path or session.recording_path
            if session.qualification_status != "qualified" or not audio:
                return False
            self.store.transition(session_id, "TRANSCRIBING")
            self._submit_postprocess(self._transcribe_and_upload, session_id, Path(audio))
            return True

    def _stop_all_recordings(self) -> None:
        for session_id, (recorder, page) in list(self._recorders.items()):
            session = self.store.get_session(session_id)
            try:
                if session:
                    # Save paths/timestamps even if navigation destroyed the JS
                    # capture and stop() can only close its local file handles.
                    self._interrupt_recording(session, recorder, "程序在录制期间关闭")
                else:
                    recorder.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must mark incomplete work
                if session:
                    self.store.transition(
                        session_id, "INTERRUPTED", error=f"程序关闭时发生错误：{exc}"
                    )
            finally:
                try:
                    page.close()  # Explicit application shutdown, not challenge handling.
                except Exception:
                    log.debug("Recording page already closed during shutdown", exc_info=True)
                self._file_watchdog.unregister(session_id)
                self._recorders.pop(session_id, None)
                self._recording_started.pop(session_id, None)
                self._end_confirmations.pop(session_id, None)

    def _minimum_viewers_for(self, session) -> int:
        return max(0, int(
            session.minimum_viewers if session.minimum_viewers is not None
            else self.settings.minimum_viewers
        ))

    def _audience_timing_error(self, session, sampled_at: str | None) -> str | None:
        """Only use a sample taken within 120 seconds of a trusted due time."""
        if session.audience_basis == "platform_start":
            return "旧排期使用平台开播时间，无法确认录制后的检查时点"
        elif session.audience_basis in {"recording_start_estimate", "recording_start"}:
            if not session.recording_started_at:
                return "缺少实际录制开始时间"
        else:
            return "人数采样依据未知"
        try:
            due = datetime.fromisoformat(session.audience_due_at or "")
            sampled = datetime.fromisoformat(sampled_at or "")
            if due.tzinfo is None or sampled.tzinfo is None:
                raise ValueError("timezone missing")
            anchor_value = (session.started_at if session.audience_basis == "platform_start"
                            else session.recording_started_at)
            anchor = datetime.fromisoformat(anchor_value or "")
            if anchor.tzinfo is None:
                raise ValueError("anchor timezone missing")
            minutes = session.qualification_minutes
            if minutes is not None:
                expected = anchor + timedelta(minutes=minutes)
                if minutes < 1 or abs((due - expected).total_seconds()) > 1:
                    return "人数检查排期与已保存的起算时间不一致，不能安全筛选"
            elif due <= anchor:
                return "历史人数检查排期无效，不能安全筛选"
            lateness = (sampled - due).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return "人数检查时间缺失或无效，无法核实采样时点"
        if lateness < 0 or lateness > 120:
            return f"人数采样距预定检查时间 {lateness:.0f} 秒，超出 120 秒容差"
        return None

    def _mark_audience_missed(self, session, reason: str) -> None:
        self.store.save_audience(
            session.session_id, value=None, raw_value=None, captured_at=None, outcome="missed"
        )
        self.store.set_qualification_status(session.session_id, "viewer_check_failed")
        message = f"{session.name} 的人数检查无法确认：{reason}；继续保留录制媒体。"
        self.store.add_event("warning", "audience", message, session_id=session.session_id)
        self.events.put(("warning", message))

    def _audience_observation_allowed(self, session_id: str, active) -> bool:
        if self._browser is None or not active:
            return False  # Observation must never attach or create a browser.
        if session_id in self._audience_blocked_sessions:
            return False
        allowed = getattr(self._browser, "recording_page_observation_allowed", None)
        return not callable(allowed) or allowed(active[1])

    def _check_due_audience(self) -> None:
        # The navigation barrier remains set. Only this recorder's own retained
        # page can be read, and blocked/challenged pages await explicit recheck.
        # Persisted low-viewer samples never trigger stop/delete during a global
        # safety pause; resume revalidates capture ownership before any mutation.
        if not self._verification_required.is_set():
            for session_id, active in list(self._recorders.items()):
                session = self.store.get_session(session_id)
                if (session and session.qualification_status == "low_viewers_deferred"
                        and self._audience_observation_allowed(session_id, active)):
                    value = session.audience_value
                    if (type(value) is int and 0 <= value <= self._minimum_viewers_for(session)
                            and session.audience_outcome in {"captured", "captured_late"}
                            and not self._audience_timing_error(
                                session, session.audience_captured_at)):
                        self._reject_low_viewers(session, value, session.audience_raw)
        # A recorder can start before its first asynchronous MediaRecorder chunk.
        # Only establish an estimated anchor once a real nonempty file exists.
        for session_id in list(self._recorders):
            session = self.store.get_session(session_id)
            if not session or session.audience_basis or not session.recording_started_at:
                continue
            paths = [Path(p) for p in (session.video_path, session.audio_path) if p]
            try:
                valid_files = bool(paths) and all(p.is_file() and p.stat().st_size > 0 for p in paths)
                start = datetime.fromisoformat(session.recording_started_at)
                if start.tzinfo is None:
                    valid_files = False
            except (ValueError, OSError):
                valid_files = False
            if valid_files:
                minutes = session.qualification_minutes
                if minutes is None:
                    # Legacy captures retain their historical interpretation.
                    minutes = max(1, int(self.settings.qualification_minutes))
                due = start + timedelta(minutes=minutes)
                basis = (
                    "recording_start" if self._normalized_platform_start(
                        session.started_at, session.start_time_source or ""
                    ) else "recording_start_estimate"
                )
                self.store.set_audience_schedule(session_id, due.isoformat(), basis)
        now = _now()
        for session in self.store.sessions_due_for_audience(now.isoformat()):
            if not self._audience_observation_allowed(
                session.session_id, self._recorders.get(session.session_id)
            ):
                continue
            if not session.audience_due_at:
                continue
            if not session.audience_basis:
                # Legacy rows may have a first-seen schedule; never treat it as a
                # recording anchor or a reliable platform start.
                if self._normalized_platform_start(session.started_at, session.start_time_source or ""):
                    session = self.store.set_audience_schedule(session.session_id, session.audience_due_at, "platform_start")
                else:
                    continue
            reason = self._audience_timing_error(session, _now().isoformat())
            if reason:
                if session.audience_outcome != "missed":
                    self._mark_audience_missed(session, reason)
                continue
            if session.audience_outcome == "missed":
                self.store.prepare_missed_audience_retry(session.session_id)
                session = self.store.get_session(session.session_id)
                if session is None or session.audience_outcome is not None:
                    continue
            active = None
            try:
                active = self._recorders.get(session.session_id)
                if not active or not self._audience_capture_is_current(session, active):
                    self._mark_audience_missed(session, "录制页面或采集状态已丢失，无法安全核验")
                    continue
                state = self._browser.inspect_page(active[1])
                if not self._audience_capture_is_current(session, active):
                    self._mark_audience_missed(session, "采样期间录制页面或采集状态已变化")
                    continue
            except BrowserReadTimeout as exc:
                marker = getattr(self._browser, "mark_page_state_unknown", None)
                if active and callable(marker):
                    marker(active[1])
                self._pause_for_unknown_page(session.session_id, str(exc))
                continue
            except WhatnotPageStateUnknown as exc:
                self._pause_for_unknown_page(session.session_id, str(exc))
                continue
            except WhatnotVerificationRequired:
                self._pause_for_verification("人数核验时检测到安全验证", session.session_id)
                continue  # Other healthy pages still get their read-only checks.
            except Exception as exc:  # noqa: BLE001 - bounded durable retry
                attempt = self.store.begin_audience_attempt(session.session_id)
                self._record_audience_failure(session, attempt, now, exc)
                continue

            if state.get("ended_candidate") or (state.get("ended") and not state.get("live")):
                # A single audience read is not a confirmed natural end. Leave
                # finalization to the monitor's three-observation state machine;
                # a transient marker must not consume the qualification attempt.
                continue
            attempt = self.store.begin_audience_attempt(session.session_id)
            captured_at = _now().isoformat()
            reason = self._audience_timing_error(session, captured_at)
            if reason:
                self._mark_audience_missed(session, reason)
                continue
            value = state.get("viewers")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                self._record_audience_failure(
                    session,
                    attempt,
                    now,
                    RuntimeError("Whatnot 页面未提供有效的当前观众人数"),
                )
                continue

            self.store.save_audience(
                session.session_id,
                value=value,
                raw_value=state.get("viewers_raw"),
                captured_at=captured_at,
                outcome="captured",
            )
            minimum = self._minimum_viewers_for(session)
            if value > minimum:
                self.store.set_qualification_status(session.session_id, "qualified")
                self.store.add_event(
                    "info",
                    "audience",
                    f"当前观众人数为 {value}，超过门槛 {minimum}；继续录制。",
                    session_id=session.session_id,
                )
                self.events.put(("info", f"{session.name} 当前有 {value} 人观看，已达到人数门槛。"))
            elif self._verification_required.is_set():
                self.store.set_qualification_status(session.session_id, "low_viewers_deferred")
                self.store.add_event(
                    "warning", "audience",
                    "已保存可靠的低人数样本；其他页面仍处于保护暂停，停止与清理延后至人工启动复核。",
                    session_id=session.session_id,
                )
            else:
                self._reject_low_viewers(session, value, state.get("viewers_raw"))

    def _record_audience_failure(
        self, session, attempt: int, now: datetime, exc: Exception
    ) -> None:
        maximum = max(1, int(self.settings.audience_retry_count))
        exhausted = attempt >= maximum
        retry_at = (
            None
            if exhausted
            else (
                now
                + timedelta(
                    seconds=max(1, int(self.settings.audience_retry_interval_seconds))
                )
            ).isoformat()
        )
        self.store.record_audience_failure(
            session.session_id,
            attempt=attempt,
            retry_at=retry_at,
            exhausted=exhausted,
            captured_at=now.isoformat(),
        )
        message = (
            f"{session.name} 的观众人数检查失败（第 {attempt}/{maximum} 次）：{exc}"
        )
        self.events.put(("error" if exhausted else "warning", message))

    def _audience_capture_is_current(self, session, active) -> bool:
        if not active or self._recorders.get(session.session_id) is not active:
            return False
        recorder, page = active
        return self._recording_capture_health(session, recorder, page)["status"] == "active"

    @staticmethod
    def _show_id_from_url(url):
        return live_room_identity(url)

    def _audience_page_matches(self, session, page) -> bool:
        page_url = getattr(page, "url", None)
        if not page_url:  # Lightweight test adapters may not expose a URL.
            return True
        return self._audience_page_matches_url(session, page_url)

    def _audience_page_matches_url(self, session, page_url) -> bool:
        current_id = self._show_id_from_url(page_url)
        expected_id = self._show_id_from_url(session.url)
        if session.session_id.startswith("whatnot-show:"):
            expected_id = session.session_id.removeprefix("whatnot-show:").split(":", 1)[0].casefold()
        if current_id and expected_id:
            return current_id == expected_id
        return self._canonical_show_url(page_url) == self._canonical_show_url(session.url)

    def _reject_low_viewers(
        self, session, value: int, raw_value: str | None
    ) -> None:
        """Stop capture, then remove only this session's validated media artifacts."""
        current = self.store.get_session(session.session_id)
        if current is None:
            return
        reason = self._audience_timing_error(current, current.audience_captured_at)
        if reason:
            self._mark_audience_missed(current, reason)
            return
        segments = self.store.list_recording_segments(session.session_id)
        active_segment = self._active_segments.get(session.session_id)
        if any(segment["status"] == "interrupted" or segment["segment_id"] != active_segment for segment in segments):
            self.store.set_qualification_status(session.session_id, "viewer_check_failed")
            self.store.add_event("warning", "audience", "存在历史或中断的录制片段；保留所有媒体，未自动停止和删除。", session_id=session.session_id)
            self.events.put(("warning", f"{session.name} 存在历史或中断片段，已保留全部媒体。"))
            return
        if current.status == "INTERRUPTED":
            self.store.set_qualification_status(session.session_id, "viewer_check_failed")
            self.store.add_event("warning", "audience", "场次已中断，不能核实正在录制；保留媒体。", session_id=session.session_id)
            return
        active = self._recorders.get(session.session_id)
        try:
            current_capture = self._audience_capture_is_current(session, active)
        except (BrowserReadTimeout, WhatnotPageStateUnknown) as exc:
            self.store.set_qualification_status(session.session_id, "viewer_check_failed")
            self.store.add_event("warning", "audience", "录制页面检查超时；状态未知，已保留媒体。", session_id=session.session_id)
            marker = getattr(self._browser, "mark_page_state_unknown", None)
            if active and callable(marker):
                marker(active[1])
            self._pause_for_unknown_page(session.session_id, str(exc))
            return
        if not current_capture:
            self.store.add_event(
                "warning", "audience", "录制已中断或页面不匹配，保留媒体并跳过低人数清理。",
                session_id=session.session_id,
            )
            return
        self.store.add_event(
            "warning",
            "audience",
            f"当前观众人数为 {value}，未超过门槛 {self._minimum_viewers_for(session)}。",
            session_id=session.session_id,
        )
        active = self._recorders.get(session.session_id)
        stop_error = None
        segment_id = None
        if active:
            segment_id = self._active_segments.pop(session.session_id, None)
            recorder, page = active
            stopped = False
            try:
                recorder.stop()
                stopped = True
                # Verify the stopped tab still belongs to the original show.
                if not self._audience_page_matches(session, page):
                    raise RuntimeError("停止后页面身份已变化，无法安全清理媒体")
                after_stop = self._browser.inspect_page(page)
                if not self._audience_page_matches(session, page) or after_stop.get("ended_candidate"):
                    raise RuntimeError("停止后页面状态或直播场次已变化，无法安全清理媒体")
                try:
                    page.close()
                except Exception:
                    log.warning("Could not close a rejected recording tab", exc_info=True)
            except (BrowserReadTimeout, WhatnotPageStateUnknown) as exc:
                stop_error = f"停止后页面状态未知，已保留媒体：{exc}"
                marker = getattr(self._browser, "mark_page_state_unknown", None)
                if callable(marker):
                    marker(page)
                self._pause_for_unknown_page(session.session_id, str(exc))
            except WhatnotVerificationRequired as exc:
                stop_error = f"停止后出现安全验证，已保留媒体：{exc}"
                self._pause_for_verification("停止后检查页面时遇到安全验证", session.session_id)
            except Exception as exc:  # noqa: BLE001 - never delete if safety cannot be established
                stop_error = f"录制停止或页面核实失败：{exc}"
            finally:
                if stopped:
                    self._file_watchdog.unregister(session.session_id)
                    self._recorders.pop(session.session_id, None)
                    self._recording_started.pop(session.session_id, None)
                    self._end_confirmations.pop(session.session_id, None)
            if stop_error:
                # Failed stop keeps its recorder registered; a human can resolve it
                # and shutdown can retry. The media may still be actively written.
                self.store.transition(session.session_id, "INTERRUPTED", error=stop_error)
                if segment_id:
                    self.store.update_recording_segment(segment_id, "interrupted", timestamp=_now().isoformat(), reason=stop_error)
            video_path = getattr(recorder, "video_path", None)
            audio_path = getattr(
                recorder,
                "audio_path",
                getattr(recorder, "output_path", None),
            )
            try:
                self.store.save_recording_assets(
                    session.session_id,
                    video_path=str(video_path) if video_path else session.video_path,
                    audio_path=str(audio_path) if audio_path else session.audio_path,
                )
                if audio_path:
                    self.store.save_recording(session.session_id, str(audio_path))
            except Exception as exc:  # noqa: BLE001
                stop_error = stop_error or f"无法保存媒体文件路径：{exc}"

        current = self.store.get_session(session.session_id) or session
        if stop_error:
            cleanup_error = stop_error
            current = self.store.get_session(session.session_id)
            if current and current.status == "RECORDING":
                self.store.transition(session.session_id, "INTERRUPTED", error=cleanup_error)
            if segment_id:
                self.store.update_recording_segment(segment_id, "interrupted", timestamp=_now().isoformat(), reason=cleanup_error)
            self.store.set_qualification_status(
                session.session_id,
                "cleanup_failed",
                cleanup_error=cleanup_error,
            )
            self.store.add_event(
                "error",
                "storage",
                f"未达到人数门槛的媒体文件清理失败：{cleanup_error}",
                session_id=session.session_id,
            )
            self.events.put(
                ("error", f"无法安全删除 {session.name} 未达人数门槛的媒体文件：{cleanup_error}")
            )
            return

        current = self.store.set_qualification_status(session.session_id, "rejected_low_viewers")
        self._remove_low_viewer_recording(current)

    @classmethod
    def _rejection_key(cls, url: str, session_id: str | None = None) -> str:
        value = (session_id if session_id and session_id.startswith("platform-live:")
                 else cls._show_id_from_url(url) or cls._canonical_show_url(url))
        return hashlib.sha256(value.encode()).hexdigest()

    def _remove_low_viewer_recording(self, session) -> None:
        with self.store._lock:
            current = self.store.get_session(session.session_id)
            if (current is None or current.session_id in self._recorders
                    or current.feishu_record_id or current.status == "COMPLETED"
                    or current.qualification_status != "rejected_low_viewers"
                    or type(current.audience_value) is not int
                    or not 0 <= current.audience_value <= self._minimum_viewers_for(current)):
                return
            error = self._delete_session_media(current)
            if error:
                if current.status != "FAILED":
                    self.store.transition(current.session_id, "FAILED", error=error)
                self.store.set_qualification_status(current.session_id, "cleanup_failed", cleanup_error=error)
                self.events.put(("error", f"{current.name} 清理失败：{error}"))
                return
            self.store.remove_rejected_session(current.session_id, self._rejection_key(current.url, current.session_id))
        self.events.put(("info", f"{current.name} 未超过人数门槛；已删除本地记录与录制文件。"))

    def discard_deferred_low_recording(self, session_id: str) -> bool:
        session = self.store.get_session(session_id)
        if (session is None or session.status != "INTERRUPTED"
                or session.qualification_status != "low_viewers_deferred"
                or session.feishu_record_id or session_id in self._recorders
                or session.audience_outcome not in {"captured", "captured_late"}
                or type(session.audience_value) is not int
                or not 0 <= session.audience_value <= self._minimum_viewers_for(session)
                or self._audience_timing_error(session, session.audience_captured_at)):
            return False
        rejected = self.store.set_qualification_status(session_id, "rejected_low_viewers")
        self._remove_low_viewer_recording(rejected)
        return True

    def _delete_session_media(self, session) -> str | None:
        # Serialize ownership checks and unlink with all Store mutations.
        with self.store._lock:
            return self._delete_session_media_locked(session)

    @staticmethod
    def _path_has_reparse_point(path: Path) -> bool:
        for part in (path, *path.parents):
            try:
                metadata = part.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if part.is_symlink() or (getattr(metadata, "st_file_attributes", 0) & 0x400):
                return True
        return False

    def _delete_session_media_locked(self, session) -> str | None:
        roots = [
            Path(self.settings.recordings_dir),
            Path(self.settings.audio_dir),
            Path(self.settings.video_dir),
        ]
        resolved_roots = [root.resolve() for root in roots]
        known_paths = {
            value
            for value in (session.video_path, session.audio_path, session.recording_path)
            if value
        }
        segments = self.store.list_recording_segments(session.session_id)
        known_paths.update(p for segment in segments for p in (segment["video_path"], segment["audio_path"]) if p)
        candidates: set[Path] = set()
        for raw_path in known_paths:
            original = Path(raw_path).absolute()
            if self._path_has_reparse_point(original):
                return f"媒体路径含符号链接或重解析点，拒绝删除：{original}"
            if any(part.casefold() in {"backup", "backups", "recording_rescue"} for part in original.parts):
                return f"拒绝删除备份目录中的文件：{original}"
            path = original.resolve()
            if not any(path.is_relative_to(root) for root in resolved_roots):
                return f"拒绝删除媒体目录以外的文件：{path}"
            allowed_new = bool(re.fullmatch(r"[av]-[0-9a-f]{8}\.(?:webm|wav)", path.name))
            segment_paths = {str(Path(p).resolve()) for s in self.store.list_recording_segments(session.session_id) for p in (s["video_path"], s["audio_path"]) if p}
            if path.name not in {"video.webm", "audio.webm", "audio.wav", "audio.tmp.wav"} and not (allowed_new and str(path) in segment_paths):
                return f"拒绝删除非预期媒体文件：{path.name}"
            if self.store.media_path_referenced_elsewhere(session.session_id, path):
                return f"媒体路径同时属于其他场次，拒绝删除：{path}"
            candidates.add(path)
        try:
            for path in candidates:
                if self._path_has_reparse_point(path):
                    return f"删除前路径变为重解析点，已停止清理：{path}"
                path.unlink(missing_ok=True)
            remaining = [str(path) for path in candidates if path.exists()]
        except OSError as exc:
            return str(exc)
        if remaining:
            return "清理后仍有媒体文件残留：" + ", ".join(remaining)
        return None

    def _export_finalized_video(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if not session or not session.video_path or not self._capture_ready_for_postprocess(session):
            return
        source = Path(session.video_path)
        if source.suffix.lower() != ".webm":
            return
        try:
            from .recording.video_export import export_mp4

            export_mp4(source)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            message = f"{session.name} 的 MP4 转换失败，原录像已保留：{exc}"
            self.store.add_event("warning", "recording", message, session_id=session_id)
            self.events.put(("warning", message))

    def _transcribe_and_upload(self, session_id: str, audio_path: Path) -> None:
        try:
            session = self.store.get_session(session_id)
            if session is None or session.qualification_status != "qualified":
                return
            if not self._capture_ready_for_postprocess(session):
                self.store.add_event(
                    "warning", "transcription", "录制尚未完整收尾或含不完整片段，未读取媒体或开始转写。",
                    session_id=session_id,
                )
                return
            source = session.audio_path or session.recording_path
            if not source or Path(source).resolve() != audio_path.resolve() or not audio_path.is_file():
                raise RuntimeError("转写输入不是该场次已登记的最终音频文件")
            self._export_finalized_video(session_id)
            if audio_path.suffix.lower() == ".webm":
                audio_path = self._convert_to_wav(audio_path)
                with wave.open(str(audio_path), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                    if duration <= 0:
                        raise RuntimeError("转换后的音频文件时长为零")
                self.store.save_recording(
                    session_id, str(audio_path), duration_seconds=duration
                )
            from .integrations.asr import FasterWhisperTranscriber

            result = FasterWhisperTranscriber(
                self.settings.asr_model_path, device=self.settings.asr_device
            ).transcribe(audio_path)
            # Platform session IDs can contain ':' and other Windows-reserved
            # characters. The 128-bit digest keeps paths short on Windows and
            # makes accidental filename collisions negligibly unlikely.
            safe_id = hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()[:32]
            output = Path(self.settings.transcripts_dir) / f"transcript_{safe_id}.txt"
            output.parent.mkdir(parents=True, exist_ok=True)
            result.write_txt(output)
            self.store.save_transcript(session_id, result.text, str(output))
            self.store.transition(session_id, "UPLOADING")
            self._upload(session_id, result.text)
        except Exception as exc:
            log.exception("Postprocessing failed for %s", session_id)
            try:
                with self._upload_lock:
                    current = self.store.get_session(session_id)
                    if current and current.status == "TRANSCRIBING":
                        self.store.transition(session_id, "FAILED", error=str(exc))
            except Exception:
                log.exception("Could not persist postprocessing failure")
            self.events.put(("error", f"转写或上传失败：{exc}"))

    def _convert_to_wav(self, source: Path) -> Path:
        import imageio_ffmpeg

        destination = source.with_suffix(".wav")
        temporary = source.with_name("audio.tmp.wav")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            str(min(2, max(1, self.settings.audio_channels))),
            "-ar",
            str(max(8000, self.settings.audio_sample_rate)),
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=1800)
        with wave.open(str(temporary), "rb") as audio:
            if audio.getnframes() <= 0:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("转换后的 WAV 文件为空")
        temporary.replace(destination)
        return destination

    def _upload(self, session_id: str, transcript: str) -> None:
        # A settings save can queue the same pending work while another upload
        # is in flight. Serialize and re-check completion before retrying.
        with self._upload_lock:
            try:
                session = self.store.get_session(session_id)
                if session is None or session.status == "COMPLETED":
                    return
                if session_id in self._recorders or (
                    session.qualification_status == "qualified"
                    and not session.feishu_record_id
                    and not self._capture_ready_for_postprocess(session)
                ):
                    self.store.add_event(
                        "warning", "feishu", "缺少自然结束的完整录制证据，未上传；媒体及已有转写已保留。",
                        session_id=session_id,
                    )
                    return
                if session.status != "UPLOADING":
                    self.store.transition(session_id, "UPLOADING")
                if session.feishu_record_id:
                    self.store.finalize_uploaded(session_id, session.feishu_record_id)
                    return
                self._upload_once(session_id, transcript)
            except Exception as exc:  # noqa: BLE001 - persist failures at the upload worker boundary
                current = self.store.get_session(session_id)
                if current and current.status == "UPLOADING":
                    self.store.transition(session_id, "FAILED", error=str(exc))
                self.events.put(("error", f"飞书上传失败：{exc}"))

    @staticmethod
    def _saved_audience_interval_minutes(session) -> float | None:
        anchor = (
            session.recording_started_at if session.audience_basis in {"recording_start_estimate", "recording_start"}
            else session.started_at if session.audience_basis == "platform_start" else None
        )
        try:
            start = datetime.fromisoformat(anchor or "")
            due = datetime.fromisoformat(session.audience_due_at or "")
            if start.tzinfo is None or due.tzinfo is None:
                return None
            minutes = (due - start).total_seconds() / 60
            return minutes if minutes > 0 else None
        except (TypeError, ValueError):
            return None

    def _reported_30_minute_audience(self, session) -> int | None:
        """Only a timely +30m sample anchored to a trusted platform start."""
        interval = self._saved_audience_interval_minutes(session)
        if (
            session.audience_basis != "platform_start"
            or not self._normalized_platform_start(session.started_at, session.start_time_source or "")
            or session.audience_outcome != "captured"
            or interval is None or abs(interval - 30) > 1 / 60
            or not isinstance(session.audience_value, int)
            or isinstance(session.audience_value, bool) or session.audience_value < 0
        ):
            return None
        try:
            captured = datetime.fromisoformat(session.audience_captured_at or "")
            due = datetime.fromisoformat(session.audience_due_at or "")
            if captured.tzinfo is None or due.tzinfo is None:
                return None
            return session.audience_value if 0 <= (captured - due).total_seconds() <= 120 else None
        except (TypeError, ValueError):
            return None

    def _reported_audience(self, session) -> int | None:
        """Use only an on-time captured sample with a verifiable saved schedule."""
        if (session.audience_outcome == "captured_at_end" and type(session.audience_value) is int
                and session.audience_value >= 0
                and self._is_recent_end_sample(session, session.audience_captured_at)):
            return session.audience_value
        if (
            session.audience_outcome != "captured"
            or not isinstance(session.audience_value, int)
            or isinstance(session.audience_value, bool)
            or session.audience_value < 0
            or session.audience_basis not in {
                "recording_start", "recording_start_estimate", "platform_start"
            }
        ):
            return None
        minutes = session.qualification_minutes
        # New local samples must retain their explicit start-time policy snapshot.
        # A legacy platform sample can still be verified from its saved schedule.
        if minutes is None:
            if session.audience_basis != "platform_start":
                return None
        elif not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            return None
        if session.audience_basis == "platform_start":
            # Read-only compatibility for historical reports. This does not make
            # a platform-anchored sample valid for stopping or deleting media.
            if not self._normalized_platform_start(session.started_at, session.start_time_source or ""):
                return None
            try:
                anchor = datetime.fromisoformat(session.started_at or "")
                due = datetime.fromisoformat(session.audience_due_at or "")
                captured = datetime.fromisoformat(session.audience_captured_at or "")
                if any(value.tzinfo is None or value.utcoffset() is None for value in (anchor, due, captured)):
                    return None
                interval = (due - anchor).total_seconds()
                if interval <= 0 or (minutes is not None and abs(interval - minutes * 60) > 1):
                    return None
                if not 0 <= (captured - due).total_seconds() <= 120:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
        elif self._audience_timing_error(session, session.audience_captured_at):
            return None
        return session.audience_value

    def _upload_once(self, session_id: str, transcript: str) -> None:
        from .config import feishu_credentials
        from .integrations.feishu import (
            FeishuBitableClient,
            FeishuConfig,
            build_record_fields,
        )

        settings = self.settings
        app_id, app_secret = feishu_credentials(settings)
        if not app_id or not app_secret:
            self.store.add_event(
                "warning",
                "feishu",
                "尚未配置飞书凭据。",
                session_id=session_id,
            )
            self.events.put(
                (
                    "warning",
                    "转写已保存在本地；尚未配置飞书凭据。",
                )
            )
            return
        session = self.store.get_session(session_id)
        if session is None:
            return
        start = datetime.fromisoformat((session.recording_started_at if session.audience_basis == "recording_start_estimate" else session.started_at) or session.recording_started_at or session.first_seen_at)
        # A rejected recording may finish while the live show continues.  Its
        # stop time (and the time of a retry) is not the show's end time.
        end = datetime.fromisoformat(session.ended_at) if session.ended_at else None
        if session.audience_outcome == "captured_late":
            transcript = ("【延迟采样说明】人数为计划检查时间之后的补判，不能视为计划时刻人数。"
                          f"计划时间：{session.audience_due_at}；实际采样时间：{session.audience_captured_at}；"
                          f"实际延迟人数：{session.audience_value}。\n\n" + transcript)
        interval = self._saved_audience_interval_minutes(session)
        interval_text = f"{interval:g}分钟" if interval is not None else "未知间隔"
        audience_value = self._reported_audience(session)
        if session.audience_value is not None:
            reporting_note = (
                "使用直播结束前最后一次有效采样，填写“在线人数”字段。"
                if audience_value is not None and session.audience_outcome == "captured_at_end"
                else "已核实按计划采样，填写“在线人数”字段。" if audience_value is not None
                else "采样时点或依据无法按计划核实，不填写“在线人数”字段。"
            )
            transcript = (
                f"【人数采样说明】{reporting_note}"
                f"计划间隔：{interval_text}；计时依据：{session.audience_basis or '未知'}；"
                f"计划时间：{session.audience_due_at}；实际采样时间：{session.audience_captured_at}；"
                f"该场门槛：{self._minimum_viewers_for(session)}人；"
                f"实际记录人数：{session.audience_value}。\n\n" + transcript
            )
        if session.audience_basis == "recording_start" and session.audience_outcome != "captured_at_end":
            transcript = (
                "【录制计时说明】人数按本程序录制开始后"
                f"{interval_text}核验，而非平台开播后计时。"
                f"录制开始时间：{session.recording_started_at}；"
                f"该场门槛：{self._minimum_viewers_for(session)}人。\n\n" + transcript
            )
        if session.audience_basis == "recording_start_estimate" and session.audience_outcome == "captured_at_end":
            transcript = "【开播时间说明】平台实际开播时间未知，表格开播时间填写录制开始时间。\n\n" + transcript
        elif session.audience_basis == "recording_start_estimate":
            transcript = (
                "【人数估算说明】平台实际开播时间未知；人数按录制开始后"
                f"{interval_text}设为检查时间，无法推算从平台开播时刻起的观众人数。"
                f"录制开始时间：{session.recording_started_at}；采样时间：{session.audience_captured_at}。"
                "表格开播时间字段填写录制开始时间。\n\n" + transcript
            )
        fields = build_record_fields(
            room_name=session.name,
            start_time=start,
            end_time=end,
            audience_value=audience_value,
            transcript=transcript,
        )
        config = FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            app_token=settings.feishu_app_token,
            table_id=settings.feishu_table_id,
        )
        with FeishuBitableClient(config) as client:
            record = client.create_record(
                fields,
                client_token=self.store.get_or_create_upload_token(session_id),
            )
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise RuntimeError("飞书未返回有效记录 ID，上传状态未标记为成功。")
        self.store.finalize_uploaded(session_id, record_id)

    def stop_recording(self, session_id: str) -> None:
        self._manual_stop_requests.add(session_id)
        self.store.add_event(
            "warning",
            "recording",
            "已向 Playwright 所属线程提交手动停止请求。",
            session_id=session_id,
        )
        self.events.put(
            ("warning", "手动停止请求已排队，将在下一次录制检查时处理。")
        )
