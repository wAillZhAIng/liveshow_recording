from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whatnot_monitor.config import Settings
from whatnot_monitor.runtime import MonitorRuntime
from whatnot_monitor.store import Store
from whatnot_monitor.whatnot import LiveShow, WhatnotVerificationRequired


class FakePage:
    def close(self):
        pass


def test_owner_loop_returns_quickly_after_start_then_reports_capacity(tmp_path, monkeypatch):
    shows = [live_show(str(index)) for index in range(1, 4)]
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": shows}, capacity=2)
    waits = []
    try:
        store.upsert_keyword("all")
        runtime.scan_once()
        assert len(runtime._recorders) == 1
        monkeypatch.setattr(store, "interrupt_orphaned_recordings", lambda: None)
        monkeypatch.setattr(runtime, "_check_feishu", lambda: None)
        monkeypatch.setattr(runtime, "_resume_recoverable", lambda: None)
        monkeypatch.setattr(runtime, "_monitor_recordings", lambda: None)
        monkeypatch.setattr(runtime, "_check_due_audience", lambda: None)
        runtime._paused.set()
        def wait(seconds, **kwargs):
            waits.append(seconds)
            if len(waits) == 2:
                runtime._stop.set()
        monkeypatch.setattr(runtime, "_wait_with_browser_events", wait)
        monkeypatch.setattr(runtime._browser, "close", lambda: None, raising=False)
        runtime._run()
        assert waits == [0.1, 5]
        assert any(s.status == "CAPACITY_INSUFFICIENT" for s in store.list_sessions(limit=10))
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


class FakeRecorder:
    def __init__(self, page, output_path):
        self.page = page
        self.output_path = output_path

    def start(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(b"synthetic audio")

    def stop(self):
        pass


class FakeAVRecorder:
    def __init__(
        self,
        page,
        video_path,
        audio_path,
        *,
        video_mime_type=None,
        video_bitrate_bps=None,
    ):
        self.page = page
        self.video_path = video_path
        self.audio_path = audio_path
        self.video_mime_type = video_mime_type
        self.video_bitrate_bps = video_bitrate_bps

    def start(self, *, timeslice_ms=1000):
        self.video_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_path.write_bytes(b"synthetic video")
        self.audio_path.write_bytes(b"synthetic audio")
        return {"videoMimeType": "video/webm", "audioMimeType": "audio/webm"}

    def stop(self):
        return {
            "videoPath": str(self.video_path),
            "audioPath": str(self.audio_path),
            "videoBytes": self.video_path.stat().st_size,
            "audioBytes": self.audio_path.stat().st_size,
        }


class FakeBrowser:
    def __init__(self, shows_by_keyword):
        self.shows_by_keyword = shows_by_keyword
        self.calls = {}
        self.live_state = {"ended": False, "live": True}

    def scan_keyword(self, phrase):
        self.calls[phrase] = self.calls.get(phrase, 0) + 1
        result = self.shows_by_keyword[phrase]
        if isinstance(result, Exception):
            raise result
        return result

    def new_room_page(self, _url, **_kwargs):
        return FakePage()

    def inspect_live(self, _url):
        if isinstance(self.live_state, Exception):
            raise self.live_state
        return dict(self.live_state)

    def inspect_page(self, _page):
        if isinstance(self.live_state, Exception):
            raise self.live_state
        return dict(self.live_state)


def live_show(show_id: str, *, first_seen: str = "2026-11-11T10:00:00+00:00"):
    return LiveShow(
        session_id=f"whatnot-show:{show_id}",
        room_id="whatnot-seller:seller-1",
        name=f"Seller show {show_id}",
        url=f"https://www.whatnot.com/show/{show_id}",
        keyword="",
        started_at=None,
        start_time_source="first_seen_estimate",
        first_seen_at=first_seen,
        viewers=12,
        viewers_raw="12 viewers",
        identity_basis="whatnot_show_link",
    )


def test_reusable_room_keeps_keyword_source_and_resolves_distinct_broadcasts(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {})
    runtime.platform = "bilibili"
    room = "bilibili:12345"
    show = replace(live_show("candidate"), session_id=f"platform-live:{room}:candidate",
                   room_id=room, url="https://live.bilibili.com/12345", identity_basis="platform_show_id")
    try:
        keywords = {"游戏": store.upsert_keyword("游戏")}
        def resolve(start):
            candidate, _ = runtime._persist_discovered_show(show, ("游戏",), keywords)
            return runtime._resolve_live_episode(candidate, room, {"broadcastStart": start})

        first = resolve("2026-11-15T20:00:00+00:00")
        assert resolve("2026-11-15T20:00:00+00:00").session_id == first.session_id
        store.mark_room_ended(room, first.session_id)
        second = resolve("2026-11-15T21:00:00+00:00")
        assert first.session_id != second.session_id
        assert [source.keyword_id for source in store.session_keyword_sources(second.session_id)] == list(keywords.values())
        assert resolve("2026-11-15T20:00:00+00:00") is None
    finally:
        runtime.shutdown()
        store.close()


def make_runtime(tmp_path, monkeypatch, shows_by_keyword, *, capacity=10):
    monkeypatch.setattr(
        "whatnot_monitor.recording.browser_audio.BrowserAudioRecorder", FakeRecorder
    )
    monkeypatch.setattr(
        "whatnot_monitor.recording.browser_av.BrowserAVRecorder", FakeAVRecorder
    )
    store = Store(tmp_path / "monitor.sqlite3")
    settings = Settings(
        max_concurrent_recordings=capacity,
        recordings_dir=str(tmp_path / "recordings"),
        video_dir=str(tmp_path / "video"),
        audio_dir=str(tmp_path / "audio"),
        disk_warning_gib=0,
    )
    runtime = MonitorRuntime(store, settings)
    runtime._browser = FakeBrowser(shows_by_keyword)
    runtime._ensure_browser = lambda: runtime._browser
    return store, runtime


def _save_finalized_capture(store, session_id, audio_path=None):
    """Synthetic fixture evidence for tests starting at the postprocess boundary."""
    session = store.get_session(session_id)
    source = str(audio_path) if audio_path else session.audio_path or session.recording_path or str(Path(store._db_path).parent / "synthetic.wav")
    if not Path(source).exists():
        Path(source).write_bytes(b"synthetic finalized audio")
    segment_id = f"finalized:{session_id}"
    timestamp = datetime.now(UTC).isoformat()
    store.add_recording_segment(segment_id, session_id, session.video_path, source)
    store.update_recording_segment(segment_id, "recording", timestamp=timestamp)
    store.update_recording_segment(segment_id, "completed", timestamp=timestamp, reason="natural_end")
    store.save_recording(session_id, source, ended_at=timestamp, duration_seconds=1)
    store.save_recording_assets(session_id, video_path=session.video_path, audio_path=source)


def test_ensure_browser_reconnects_when_context_was_closed(tmp_path, monkeypatch):
    store = Store(tmp_path / "monitor.sqlite3")
    runtime = MonitorRuntime(store, Settings())

    class ClosedBrowser:
        def is_usable(self):
            return False

    class Driver:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    old_driver = Driver()
    new_driver = Driver()
    replacement = object()
    runtime._browser = ClosedBrowser()
    runtime._playwright = old_driver
    monkeypatch.setattr(
        "whatnot_monitor.runtime.WhatnotBrowser.launch_persistent",
        lambda *_args, **_kwargs: (replacement, new_driver),
    )

    try:
        assert runtime._ensure_browser() is replacement
        assert old_driver.stopped
        assert runtime._playwright is new_driver
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_login_flow_pauses_scanning_until_monitor_resumes(tmp_path):
    store = Store(tmp_path / "monitor.sqlite3")
    runtime = MonitorRuntime(store, Settings())
    owner_started = threading.Event()

    def idle_owner():
        owner_started.set()
        runtime._stop.wait()

    runtime._run = idle_owner
    try:
        runtime.open_login()

        assert owner_started.wait(1)
        assert runtime._paused.is_set()
        assert runtime._login_requested.is_set()

        runtime.start()
        assert not runtime._paused.is_set()
    finally:
        runtime.shutdown()
        store.close()


def test_start_after_manual_verification_resumes_and_requests_scan(tmp_path):
    store = Store(tmp_path / "monitor.sqlite3")
    runtime = MonitorRuntime(store, Settings())
    owner_started = threading.Event()

    def idle_owner():
        owner_started.set()
        runtime._stop.wait()

    runtime._run = idle_owner
    runtime._verification_required.set()
    runtime._paused.set()
    try:
        runtime.start()

        assert owner_started.wait(1)
        assert runtime.scan_status["state"] == "starting"
        assert not runtime._verification_required.is_set()
        assert not runtime._paused.is_set()
        assert runtime._scan_requested.is_set()
    finally:
        runtime.shutdown()
        store.close()


def test_start_status_waits_for_a_scan_instead_of_claiming_running(tmp_path):
    store = Store(tmp_path / "monitor.sqlite3")
    runtime = MonitorRuntime(store, Settings())
    owner_started = threading.Event()

    def idle_owner():
        owner_started.set()
        runtime._stop.wait()

    runtime._run = idle_owner
    try:
        runtime.start()

        assert owner_started.wait(1)
        assert runtime.running
        assert runtime.scan_status["state"] == "starting"
    finally:
        runtime.shutdown()
        store.close()


def test_scan_round_merges_overlapping_keyword_results_once(tmp_path, monkeypatch):
    a, b, c, d, e = [live_show(str(i)) for i in range(1, 6)]
    store, runtime = make_runtime(
        tmp_path,
        monkeypatch,
        {
            "A": [a, b, c],
            "B": [b, c, d],
            "C": [a, d, e],
        },
    )
    try:
        keyword_ids = {
            phrase: store.upsert_keyword(phrase) for phrase in ("A", "B", "C")
        }
        runtime.scan_once()

        sessions = store.list_sessions(limit=20)
        assert len(sessions) == 5
        assert sum(session.status == "RECORDING" for session in sessions) == 1
        assert sum(session.status == "WAITING" for session in sessions) == 4
        for _ in range(4):
            runtime._start_waiting_sessions()
        sessions = store.list_sessions(limit=20)
        assert all(session.status == "RECORDING" for session in sessions), [
            (session.session_id, session.status, session.error) for session in sessions
        ]
        assert len({session.recording_task_id for session in sessions}) == 5
        assert runtime.scan_status["new_rooms"] == 5
        assert runtime.scan_status["duplicate_results"] == 4

        expected_sources = {
            a.session_id: {"A", "C"},
            b.session_id: {"A", "B"},
            c.session_id: {"A", "B"},
            d.session_id: {"B", "C"},
            e.session_id: {"C"},
        }
        for session_id, expected in expected_sources.items():
            sources = store.session_keyword_sources(session_id)
            assert {source.phrase_snapshot for source in sources} == expected
            assert {source.keyword_id for source in sources} == {
                keyword_ids[phrase] for phrase in expected
            }
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_repeated_fallback_url_reuses_active_session_and_recorder(
    tmp_path, monkeypatch
):
    first = LiveShow(
        session_id="url-fallback:abc:2026-11-11T10:00:00+00:00",
        room_id="unresolved-seller:abc",
        name="Fallback show",
        url="https://www.whatnot.com/show/abc",
        keyword="all",
        started_at=None,
        start_time_source="first_seen_estimate",
        first_seen_at="2026-11-11T10:00:00+00:00",
        viewers=12,
        viewers_raw="12 viewers",
        identity_basis="url_first_seen_fallback",
    )
    later_observation = replace(
        first,
        session_id="url-fallback:abc:2026-11-11T10:05:00+00:00",
        first_seen_at="2026-11-11T10:05:00+00:00",
    )
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": [first]})
    try:
        store.upsert_keyword("all")
        runtime.scan_once()
        original_task = store.list_sessions(limit=10)[0].recording_task_id

        runtime._browser.shows_by_keyword["all"] = [later_observation]
        runtime.scan_once()

        sessions = store.list_sessions(limit=10)
        assert len(sessions) == 1
        assert sessions[0].status == "RECORDING"
        assert sessions[0].recording_task_id == original_task
        assert len(runtime._recorders) == 1
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_capacity_overflow_is_visible_and_revalidated_after_limit_increase(
    tmp_path, monkeypatch
):
    shows = [live_show(str(index)) for index in range(1, 6)]
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": shows}, capacity=2)
    try:
        store.upsert_keyword("all")
        runtime.scan_once()
        assert runtime.scan_status["recordings_started"] == 1
        runtime._start_waiting_sessions()
        runtime._start_waiting_sessions()

        sessions = store.list_sessions(limit=20)
        assert sum(session.status == "RECORDING" for session in sessions) == 2, [
            (session.session_id, session.status, session.error) for session in sessions
        ]
        overflow = [
            session for session in sessions if session.status == "CAPACITY_INSUFFICIENT"
        ]
        assert len(overflow) == 3
        assert all("音频未录制" in (session.error or "") for session in overflow)
        assert runtime.scan_status["recordings_started"] == 1

        runtime.settings = replace(runtime.settings, max_concurrent_recordings=5)
        for _ in range(3):
            runtime._start_waiting_sessions()
        sessions = store.list_sessions(limit=20)
        assert sum(session.status == "RECORDING" for session in sessions) == 5
        by_id = {session.session_id: session for session in sessions}
        assert all(by_id[session.session_id].capacity_since for session in overflow)
        assert all(
            by_id[session.session_id].capacity_released_at for session in overflow
        )
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_waiting_queue_is_bounded_and_unknown_live_state_is_not_capacity_failure(
    tmp_path, monkeypatch
):
    shows = [live_show(str(index)) for index in range(1, 5)]
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": shows}, capacity=1)
    try:
        store.upsert_keyword("all")
        runtime.scan_once()
        runtime.settings = replace(runtime.settings, max_concurrent_recordings=5)
        runtime._browser.live_state = {"ended": False, "live": False}
        runtime._live_search_evidence.clear()  # no recent explicit LIVE sighting
        before = {s.session_id: s for s in store.list_sessions(limit=20)}
        runtime._start_waiting_sessions()
        after = {s.session_id: s for s in store.list_sessions(limit=20)}
        delayed = [s for s in after.values() if s.retry_at]
        assert len(delayed) == 1
        assert delayed[0].status == before[delayed[0].session_id].status
        assert "无法确认" in delayed[0].error
        runtime._start_waiting_sessions()
        assert sum(bool(s.retry_at) for s in store.list_sessions(limit=20)) == 2
        assert len(runtime._recorders) == 1
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_failed_capture_without_media_retries_same_show_and_task_id(
    tmp_path, monkeypatch
):
    show = live_show("retry-capture")
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": [show]})
    try:
        keyword_id = store.upsert_keyword("all")
        failed, created = runtime._persist_discovered_show(
            show, ("all",), {"all": keyword_id}
        )
        assert created
        task_id = failed.recording_task_id
        store.transition(failed.session_id, "FAILED", error="player not ready")

        runtime.scan_once()

        sessions = store.list_sessions(limit=10)
        assert len(sessions) == 1
        assert sessions[0].session_id == show.session_id
        assert sessions[0].recording_task_id == task_id
        assert sessions[0].status == "RECORDING"
        assert sessions[0].video_path and Path(sessions[0].video_path).is_file()
        assert sessions[0].audio_path and Path(sessions[0].audio_path).is_file()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_failed_capture_with_existing_media_is_not_replaced(
    tmp_path, monkeypatch
):
    show = live_show("keep-capture")
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": [show]})
    try:
        keyword_id = store.upsert_keyword("all")
        session, _ = runtime._persist_discovered_show(
            show, ("all",), {"all": keyword_id}
        )
        audio_path = tmp_path / "preserved.webm"
        audio_path.write_bytes(b"preserve this media")
        store.save_recording_assets(
            session.session_id,
            video_path=None,
            audio_path=str(audio_path),
        )
        store.transition(session.session_id, "FAILED", error="transcription failed")

        runtime.scan_once()

        updated = store.get_session(session.session_id)
        assert updated is not None
        assert updated.status == "FAILED"
        assert updated.audio_path == str(audio_path)
        assert audio_path.read_bytes() == b"preserve this media"
        assert not runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_recording_size_limit_stops_only_affected_capture_and_retains_media(
    tmp_path, monkeypatch
):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime.settings = replace(runtime.settings, max_recording_size_gib=1e-9)

        runtime._monitor_recordings()

        updated = store.get_session(session.session_id)
        assert updated.status == "FAILED"
        assert "达到大小上限" in (updated.error or "")
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
        assert session.session_id not in runtime._recorders
        assert any(
            event.component == "storage" and event.session_id == session.session_id
            for event in store.recent_events()
        )
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_low_free_space_stops_active_capture_and_retains_media(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime.settings = replace(runtime.settings, disk_warning_gib=1)
        monkeypatch.setattr(
            "whatnot_monitor.runtime.shutil.disk_usage",
            lambda _path: SimpleNamespace(free=0),
        )

        runtime._monitor_recordings()

        updated = store.get_session(session.session_id)
        assert updated.status == "FAILED"
        assert "可用空间已降至" in (updated.error or "")
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
        assert session.session_id not in runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_keyword_failure_retries_without_aborting_later_keywords(tmp_path, monkeypatch):
    store, runtime = make_runtime(
        tmp_path,
        monkeypatch,
        {
            "broken": RuntimeError("synthetic search failure"),
            "healthy": [live_show("healthy")],
        },
    )
    try:
        failed_id = store.upsert_keyword("broken")
        store.upsert_keyword("healthy")
        runtime.scan_once()

        assert runtime._browser.calls == {"broken": 2, "healthy": 1}
        failed = next(row for row in store.list_keywords() if row.id == failed_id)
        assert failed.scan_status == "ERROR"
        assert store.list_sessions(limit=10)[0].session_id == "whatnot-show:healthy"
        assert runtime.scan_status["scanned_keywords"] == 2
        assert runtime.scan_status["failed_keywords"] == 1
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_security_verification_pauses_round_without_retrying(tmp_path, monkeypatch):
    store, runtime = make_runtime(
        tmp_path,
        monkeypatch,
        {
            "a-healthy": [live_show("must-not-be-recorded")],
            "b-blocked": WhatnotVerificationRequired("security verification required"),
            "c-not-scanned": [live_show("not-scanned")],
        },
    )
    try:
        healthy_id = store.upsert_keyword("a-healthy")
        blocked_id = store.upsert_keyword("b-blocked")
        store.upsert_keyword("c-not-scanned")

        runtime.scan_once()

        blocked = next(row for row in store.list_keywords() if row.id == blocked_id)
        assert runtime._browser.calls == {"a-healthy": 1, "b-blocked": 1}
        assert runtime._paused.is_set()
        assert runtime.scan_status["state"] == "verification_required"
        assert runtime.scan_status["scanned_keywords"] == 2
        assert runtime.scan_status["failed_keywords"] == 1
        assert blocked.scan_status == "ERROR"
        sessions = store.list_sessions(limit=10)
        assert len(sessions) == 1
        assert sessions[0].session_id == "whatnot-show:must-not-be-recorded"
        assert sessions[0].status == "WAITING"
        assert not runtime._recorders
        sources = store.session_keyword_sources(sessions[0].session_id)
        assert {(s.keyword_id, s.phrase_snapshot) for s in sources} == {
            (healthy_id, "a-healthy")
        }
        assert runtime.scan_status["new_rooms"] == 1
        assert runtime.scan_status["recordings_started"] == 0
        healthy = next(row for row in store.list_keywords() if row.id == healthy_id)
        assert healthy.discovered_count == 1
        assert healthy.recording_count == 0
        messages = []
        while not runtime.events.empty():
            messages.append(runtime.events.get_nowait()[1])
        assert any("扫描已暂停" in message for message in messages)
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_verification_preserves_deduplicated_sources_from_multiple_keywords(tmp_path, monkeypatch):
    shared = live_show("shared")
    store, runtime = make_runtime(tmp_path, monkeypatch, {
        "a": [shared, live_show("unique")],
        "b": [shared],
        "c": WhatnotVerificationRequired("verification required"),
    })
    try:
        ids = {phrase: store.upsert_keyword(phrase) for phrase in ("a", "b", "c")}
        runtime.scan_once()
        sessions = store.list_sessions(limit=20)
        assert len(sessions) == 2
        assert all(s.status == "WAITING" for s in sessions)
        assert not runtime._recorders
        sources = store.session_keyword_sources(shared.session_id)
        assert {(s.keyword_id, s.phrase_snapshot) for s in sources} == {
            (ids["a"], "a"), (ids["b"], "b")
        }
        assert runtime.scan_status["new_rooms"] == 2
        assert runtime.scan_status["duplicate_results"] == 1
        assert runtime.scan_status["state"] == "verification_required"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_discovery_preserves_platform_start_but_never_starts_recording_clock(
    tmp_path, monkeypatch
):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, qualification_minutes=30)
    try:
        keyword_id = store.upsert_keyword("all")
        first_seen = datetime.now(UTC).isoformat()
        platform_start = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
        actual = replace(
            live_show("platform", first_seen=first_seen),
            started_at=platform_start,
            start_time_source="platform_started_at",
        )
        session, _ = runtime._persist_discovered_show(
            actual, ("all",), {"all": keyword_id}
        )
        assert session.started_at == datetime.fromisoformat(platform_start).astimezone(UTC).isoformat()
        assert session.start_time_source == "platform_started_at"
        assert session.audience_due_at is None
        assert session.audience_basis is None
        assert session.qualification_minutes is None

        estimated = replace(
            live_show("estimate", first_seen=first_seen),
            started_at="2020-01-01T00:00:00+00:00",
            start_time_source="first_seen_estimate",
        )
        estimated_session, _ = runtime._persist_discovered_show(
            estimated, ("all",), {"all": keyword_id}
        )
        assert estimated_session.started_at is None
        assert estimated_session.start_time_source == "first_seen_estimate"
        assert estimated_session.audience_due_at is None
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def _make_due_show(runtime, store):
    first_seen = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    show = replace(live_show("due", first_seen=first_seen), started_at=first_seen,
                   start_time_source="platform_started_at")
    session, created = runtime._persist_discovered_show(
        show, ("all",), {"all": store.upsert_keyword("all")}
    )
    assert created
    # Start the synthetic recorder in the past rather than deriving a local
    # recording deadline from the platform's earlier start.
    with patch("whatnot_monitor.runtime._now", return_value=datetime.fromisoformat(first_seen)):
        runtime._start_recording(session)
    started = store.get_session(session.session_id)
    due = datetime.fromisoformat(started.recording_started_at) + timedelta(minutes=started.qualification_minutes)
    store.set_audience_schedule(session.session_id, due.isoformat(), "recording_start")
    return store.get_session(session.session_id)


def test_late_audience_is_missed_and_media_preserved(tmp_path, monkeypatch):
    for viewers in (1, 999):
        case = tmp_path / str(viewers)
        case.mkdir()
        store, runtime = make_runtime(case, monkeypatch, {"all": []})
        try:
            session = _make_due_show(runtime, store)
            due = datetime.fromisoformat(session.audience_due_at)
            monkeypatch.setattr("whatnot_monitor.runtime._now", lambda due=due: due + timedelta(seconds=121))
            runtime._browser.live_state = {"ended": False, "viewers": viewers}
            runtime._check_due_audience()
            updated = store.get_session(session.session_id)
            assert updated.audience_outcome == "missed"
            assert updated.qualification_status == "viewer_check_failed"
            assert updated.audience_value is None
            assert Path(updated.audio_path).exists() and Path(updated.video_path).exists()
            assert session.session_id in runtime._recorders
        finally:
            runtime._stop_all_recordings()
            runtime._postprocess.shutdown(wait=True)
            store.close()


def test_estimated_start_uses_recording_time_and_can_qualify(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        first_seen = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
        show = live_show("estimated_due", first_seen=first_seen)
        session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
        runtime._start_recording(session)
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.start_time_source == "first_seen_estimate"
        assert updated.audience_basis == "recording_start_estimate"
        due = datetime.fromisoformat(updated.audience_due_at)
        assert due == datetime.fromisoformat(updated.recording_started_at) + timedelta(minutes=30)
        assert updated.qualification_status == "pending"
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        runtime._browser.live_state = {"ended": False, "viewers": 101}
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "qualified"
        assert updated.audience_outcome == "captured" and updated.audience_value == 101
        assert Path(updated.video_path).is_file() and Path(updated.audio_path).is_file()
        assert session.session_id in runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_estimate_waits_for_nonempty_recording_and_is_durable(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        show = live_show("estimate-empty", first_seen=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
        session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
        runtime._start_recording(session)
        session = store.get_session(session.session_id)
        Path(session.audio_path).write_bytes(b"")
        runtime._check_due_audience()
        assert store.get_session(session.session_id).audience_basis is None
        assert store.get_session(session.session_id).audience_due_at is None
        Path(session.audio_path).write_bytes(b"recorded bytes")
        runtime._check_due_audience()
        saved = store.get_session(session.session_id)
        assert saved.audience_basis == "recording_start_estimate"
        assert datetime.fromisoformat(saved.audience_due_at) == datetime.fromisoformat(saved.recording_started_at) + timedelta(minutes=30)
        # A later scan cannot rewrite the recorded sampling basis.
        later = replace(show, started_at=datetime.now(UTC).isoformat(), start_time_source="platform_started_at")
        runtime._persist_discovered_show(later, ("all",), {"all": store.upsert_keyword("all")})
        assert store.get_session(session.session_id).audience_basis == "recording_start_estimate"
        assert store.get_session(session.session_id).audience_due_at == saved.audience_due_at
        reopened = Store(tmp_path / "monitor.sqlite3")
        try:
            restored = reopened.get_session(session.session_id)
            assert restored.audience_basis == "recording_start_estimate"
            assert restored.audience_due_at == saved.audience_due_at
            assert restored.recording_started_at == saved.recording_started_at
        finally:
            reopened.close()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_estimated_low_count_filters_and_late_count_preserves(tmp_path, monkeypatch):
    for late in (False, True):
        case = tmp_path / str(late)
        case.mkdir()
        store, runtime = make_runtime(case, monkeypatch, {"all": []})
        try:
            show = live_show("estimate-low", first_seen=datetime.now(UTC).isoformat())
            session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
            runtime._start_recording(session)
            runtime._check_due_audience()
            capture = store.get_session(session.session_id)
            due = datetime.fromisoformat(capture.audience_due_at)
            monkeypatch.setattr("whatnot_monitor.runtime._now", lambda due=due, late=late: due + timedelta(seconds=121 if late else 1))
            runtime._browser.live_state = {"ended": False, "viewers": 1}
            runtime._check_due_audience()
            updated = store.get_session(session.session_id)
            if late:
                assert updated.qualification_status == "viewer_check_failed"
                assert updated.audience_basis == "recording_start_estimate"
                assert updated.audience_outcome == "missed"
            else:
                assert updated is None
            assert Path(capture.audio_path).exists() == late
        finally:
            runtime._stop_all_recordings()
            runtime._postprocess.shutdown(wait=True)
            store.close()


def test_estimated_result_is_labeled_in_ui_and_feishu(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.app import MonitorApp
    from whatnot_monitor.integrations import feishu

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    sent = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            sent.append(fields)
            return {"record_id": "synthetic"}
    try:
        show = live_show("label", first_seen=datetime.now(UTC).isoformat())
        session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
        runtime._start_recording(session)
        runtime._check_due_audience()
        session = store.get_session(session.session_id)
        due = datetime.fromisoformat(session.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        runtime._browser.live_state = {"ended": False, "viewers": 101}
        runtime._check_due_audience()
        session = store.get_session(session.session_id)
        assert "估算" in MonitorApp._audience_display(session)
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("id", "secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        monkeypatch.setattr(store, "mark_uploaded", lambda *_: None)
        monkeypatch.setattr(store, "finalize_uploaded", lambda *_: None)
        monkeypatch.setattr(store, "transition", lambda *_args, **_kwargs: None)
        runtime._upload_once(session.session_id, "转写内容")
        assert len(sent) == 1
        assert sent[0]["在线人数"] == 101
        assert "开播30分钟在线人数" not in sent[0]
        assert "实际记录人数：101" in sent[0][feishu.TRANSCRIPT_FIELD]
        assert "人数估算说明" in sent[0][feishu.TRANSCRIPT_FIELD]
        assert "录制开始" in sent[0][feishu.TRANSCRIPT_FIELD]
        assert "转写内容" in sent[0][feishu.TRANSCRIPT_FIELD]
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_interrupted_recording_resumes_as_distinct_segment_without_false_archive(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        old_audio, old_video = Path(session.audio_path), Path(session.video_path)
        old_audio_bytes, old_video_bytes = old_audio.read_bytes(), old_video.read_bytes()
        runtime._stop_all_recordings()
        interrupted = store.get_session(session.session_id)
        assert interrupted.status == "INTERRUPTED"
        runtime._resume_recoverable()
        assert store.get_session(session.session_id).status == "WAITING"
        runtime._browser.live_state = {"live": True, "ended": False}
        runtime._start_waiting_sessions()
        resumed = store.get_session(session.session_id)
        assert resumed.status == "RECORDING"
        assert resumed.audio_path != str(old_audio) and resumed.video_path != str(old_video)
        assert old_audio.read_bytes() == old_audio_bytes and old_video.read_bytes() == old_video_bytes
        runtime._check_due_audience()
        resumed = store.get_session(session.session_id)
        assert resumed.audience_basis == "recording_start"
        assert datetime.fromisoformat(resumed.audience_due_at) == datetime.fromisoformat(resumed.recording_started_at) + timedelta(minutes=30)
        segments = store.list_recording_segments(session.session_id)
        assert len(segments) == 2 and segments[0]["status"] == "interrupted"
        store.set_qualification_status(session.session_id, "qualified")
        runtime._finish_recording(resumed, *runtime._recorders[session.session_id])
        assert store.get_session(session.session_id).status == "FAILED"
        assert Path(resumed.audio_path).exists() and old_audio.exists()
        assert not queued
        runtime._retry_session(session.session_id)
        assert not queued
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_interrupted_legacy_media_cannot_be_deleted_by_late_audience_check(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        old_audio, old_video = Path(session.audio_path), Path(session.video_path)
        runtime._stop_all_recordings()
        runtime._reject_low_viewers(store.get_session(session.session_id), 1, "1")
        assert old_audio.exists() and old_video.exists()
        assert store.get_session(session.session_id).qualification_status == "viewer_check_failed"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_orphaned_legacy_recording_is_backfilled_and_resumed_after_database_reopen(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    session = _make_due_show(runtime, store)
    old_audio, old_video = Path(session.audio_path), Path(session.video_path)
    original_audio = old_audio.read_bytes()
    # Simulate a pre-migration database and an abruptly terminated process.
    with store._conn:
        store._conn.execute("DELETE FROM recording_segments WHERE session_id=?", (session.session_id,))
    runtime._postprocess.shutdown(wait=True)
    store.close()
    reopened = Store(tmp_path / "monitor.sqlite3")
    replacement = MonitorRuntime(reopened, runtime.settings)
    replacement._browser = FakeBrowser({"all": []})
    try:
        reopened.interrupt_orphaned_recordings()
        replacement._resume_recoverable()
        previous = reopened.list_recording_segments(session.session_id)
        assert len(previous) == 1 and previous[0]["status"] == "interrupted"
        assert previous[0]["audio_path"] == str(old_audio)
        replacement._browser.live_state = {"live": True, "ended": False}
        replacement._start_waiting_sessions()
        resumed = reopened.get_session(session.session_id)
        assert resumed.status == "RECORDING" and resumed.audio_path != str(old_audio)
        assert old_audio.read_bytes() == original_audio and old_video.exists()
        assert len(reopened.list_recording_segments(session.session_id)) == 2
    finally:
        replacement._stop_all_recordings()
        replacement._postprocess.shutdown(wait=True)
        reopened.close()


def test_audience_sample_that_finishes_after_tolerance_is_missed(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        due = datetime.fromisoformat(session.audience_due_at)
        clock = [due + timedelta(seconds=119)]
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: clock[0])
        def inspect(_page):
            clock[0] += timedelta(seconds=2)
            return {"ended": False, "viewers": 1}
        runtime._browser.inspect_page = inspect
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.audience_outcome == "missed"
        assert updated.qualification_status == "viewer_check_failed"
        assert Path(updated.video_path).exists() and Path(updated.audio_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_recovery_rejects_stale_persisted_sample_without_deletion(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime._stop_all_recordings()
        store.transition(session.session_id, "AUDIENCE_PENDING")
        due = datetime.fromisoformat(session.audience_due_at)
        store.save_audience(session.session_id, value=1, raw_value="1",
                            captured_at=(due + timedelta(seconds=121)).isoformat(), outcome="captured")
        runtime._resume_recoverable()
        updated = store.get_session(session.session_id)
        assert updated.audience_outcome == "missed"
        assert updated.qualification_status == "viewer_check_failed"
        assert Path(updated.video_path).exists() and Path(updated.audio_path).exists()
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_audience_snapshot_saves_actual_observation_time_and_is_not_current_viewers(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        clock = [datetime.now(UTC)]
        started = clock[0]
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: clock[0])

        def inspect(_url):
            clock[0] += timedelta(seconds=12)
            return {"ended": False, "viewers": 123, "viewers_raw": "123 viewers"}

        runtime._browser.inspect_page = inspect
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.audience_value == 123
        assert datetime.fromisoformat(updated.audience_captured_at) == started + timedelta(seconds=12)
        store.update_live_observation(session.session_id, current_viewers=999,
                                      current_viewers_raw="999 viewers", observed_at=clock[0].isoformat())
        assert store.get_session(session.session_id).audience_value == 123
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_audience_threshold_99_100_101(tmp_path, monkeypatch):
    for value in (99, 100, 101):
        case_dir = tmp_path / str(value)
        case_dir.mkdir()
        store, runtime = make_runtime(case_dir, monkeypatch, {"all": []})
        runtime.settings = replace(runtime.settings, minimum_viewers=100)
        try:
            session = _make_due_show(runtime, store)
            assert session is not None
            capture = store.get_session(session.session_id)
            runtime._browser.live_state = {"ended": False, "viewers": value, "viewers_raw": str(value)}
            runtime._check_due_audience()
            updated = store.get_session(session.session_id)
            if value <= 100:
                assert updated is None
                assert not Path(capture.video_path).exists()
                assert not Path(capture.audio_path).exists()
                assert session.session_id not in runtime._recorders
            else:
                assert updated.qualification_status == "qualified"
                assert updated.audience_value == value
                assert updated.video_path and Path(updated.video_path).is_file()
                assert updated.audio_path and Path(updated.audio_path).is_file()
                assert updated.session_id in runtime._recorders
        finally:
            runtime._stop_all_recordings()
            runtime._postprocess.shutdown(wait=True)
            store.close()


def test_missing_viewer_count_retries_then_fails_without_deleting_media(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, audience_retry_count=1)
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = {"ended": False, "viewers": None, "viewers_raw": None}
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "viewer_check_failed"
        assert updated.audience_outcome == "failed"
        assert updated.audience_attempts == 1
        assert updated.audience_value is None
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_audience_check_exception_has_bounded_retry_and_persists_retry_at(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, audience_retry_count=2, audience_retry_interval_seconds=30)
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = RuntimeError("synthetic page failure")
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "pending"
        assert updated.audience_attempts == 1
        assert updated.audience_retry_at
        assert updated.audience_value is None
        assert not store.sessions_due_for_audience(datetime.now(UTC).isoformat())
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_ended_before_threshold_preserves_media_and_does_not_reject(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._transcribe_and_upload = lambda session_id, path: queued.append((session_id, path))
    try:
        show = live_show("early", first_seen=datetime.now(UTC).isoformat())
        session, _ = runtime._persist_discovered_show(
            show, ("all",), {"all": store.upsert_keyword("all")}
        )
        runtime._start_recording(session)
        runtime._finish_recording(session, *runtime._recorders[session.session_id])
        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "ended_before_threshold"
        assert updated.audience_outcome == "ended_before_due"
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
        assert updated.status == "FAILED"
        runtime._postprocess.shutdown(wait=True)
        assert not queued
    finally:
        runtime._stop_all_recordings()
        store.close()


def test_cleanup_error_preserves_files_and_never_transcribes(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    submitted = []
    runtime._delete_session_media = lambda session: "synthetic permission error"
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = {"ended": False, "viewers": 99, "viewers_raw": "99"}
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "cleanup_failed"
        assert updated.cleanup_error == "synthetic permission error"
        assert __import__("pathlib").Path(updated.video_path).is_file()
        assert __import__("pathlib").Path(updated.audio_path).is_file()
        assert not submitted
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_recovery_defers_pending_capture_until_qualification_resolves(
    tmp_path, monkeypatch
):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    transcribed = []
    runtime._transcribe_and_upload = lambda session_id, path: transcribed.append(
        (session_id, path)
    )
    try:
        session = _make_due_show(runtime, store)
        runtime._stop_all_recordings()
        runtime._resume_recoverable()
        runtime._postprocess.shutdown(wait=True)

        updated = store.get_session(session.session_id)
        assert updated.status == "WAITING"
        assert updated.qualification_status == "pending"
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
        assert not transcribed
    finally:
        runtime._stop_all_recordings()
        store.close()


def test_recovery_reconciles_saved_low_audience_before_transcription(
    tmp_path, monkeypatch
):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    uploaded = []
    try:
        session = _make_due_show(runtime, store)
        runtime._stop_all_recordings()
        store.transition(session.session_id, "AUDIENCE_PENDING")
        store.save_audience(
            session.session_id,
            value=99,
            raw_value="99",
            captured_at=datetime.now(UTC).isoformat(),
            outcome="captured",
        )
        runtime._resume_recoverable()
        runtime._postprocess.shutdown(wait=True)

        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "pending"
        assert Path(updated.video_path).exists()
        assert Path(updated.audio_path).exists()
        assert uploaded == []
    finally:
        runtime._stop_all_recordings()
        store.close()


def test_room_ending_when_due_viewer_check_fails_preserves_media(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._transcribe_and_upload = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = {"ended": True, "ended_candidate": True, "viewers": None}
        runtime._check_due_audience()
        pending = store.get_session(session.session_id)
        assert pending.status == "RECORDING"
        assert pending.audience_attempts == 0
        for _ in range(3):
            runtime._monitor_recordings()
        updated = store.get_session(session.session_id)
        assert updated.qualification_status == "viewer_check_failed"
        assert updated.audience_outcome == "missed"
        assert Path(updated.video_path).is_file()
        assert Path(updated.audio_path).is_file()
        assert updated.status == "FAILED"
        runtime._postprocess.shutdown(wait=True)
        assert not queued
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_unqualified_existing_transcript_cannot_be_retried_or_recovered(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "viewer_check_failed")
        store.save_transcript(session.session_id, "old content", str(tmp_path / "old.txt"))
        runtime._stop_all_recordings()
        runtime._retry_session(session.session_id)
        runtime._resume_recoverable()
        runtime._transcribe_and_upload(session.session_id, Path(session.audio_path))
        assert not queued
        assert Path(session.audio_path).is_file()
        assert store.get_session(session.session_id).status == "WAITING"
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_page_read_timeout_does_not_stop_other_recordings_or_delete_media(tmp_path, monkeypatch):
    from whatnot_monitor.bounded_browser_read import BrowserReadTimeout
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        sessions = []
        for suffix in ("first", "second"):
            show = live_show(suffix, first_seen=datetime.now(UTC).isoformat())
            session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
            runtime._start_recording(session)
            sessions.append(store.get_session(session.session_id))
        first_page = runtime._recorders[sessions[0].session_id][1]
        def inspect(page):
            if page is first_page:
                raise BrowserReadTimeout("synthetic timeout")
            return {"live": True, "ended_candidate": False, "viewers": 9}
        runtime._browser.inspect_page = inspect
        runtime._monitor_recordings()
        assert len(runtime._recorders) == 2
        assert store.get_session(sessions[1].session_id).current_viewers == 9
        assert all(Path(s.audio_path).exists() for s in sessions)
        runtime._audience_capture_is_current = lambda *_: (_ for _ in ()).throw(BrowserReadTimeout("synthetic timeout"))
        runtime._reject_low_viewers(sessions[0], 1, "1")
        assert store.get_session(sessions[0].session_id).qualification_status == "viewer_check_failed"
        assert len(runtime._recorders) == 2
        assert all(Path(s.audio_path).exists() for s in sessions)
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_due_audience_page_timeout_preserves_recording_and_media(tmp_path, monkeypatch):
    from whatnot_monitor.bounded_browser_read import BrowserReadTimeout
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, audience_retry_count=1)
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.inspect_page = lambda _page: (_ for _ in ()).throw(BrowserReadTimeout("synthetic timeout"))
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.audience_value is None and updated.audience_outcome is None
        assert updated.qualification_status == "pending"
        assert runtime.scan_status["state"] == "page_state_unknown"
        assert session.session_id in runtime._recorders
        assert Path(updated.audio_path).exists() and Path(updated.video_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_disconnected_browser_with_unknown_page_is_not_replaced(tmp_path, monkeypatch):
    from whatnot_monitor.whatnot import WhatnotPageStateUnknown
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    class Disconnected:
        _uncertain_page = object()
        def is_usable(self): return False
    previous = Disconnected()
    runtime._browser = previous
    runtime._ensure_browser = MonitorRuntime._ensure_browser.__get__(runtime)
    runtime._stop_all_recordings = lambda: (_ for _ in ()).throw(AssertionError("must retain captures"))
    try:
        with __import__("pytest").raises(WhatnotPageStateUnknown):
            runtime._ensure_browser()
        assert runtime._browser is previous
        assert runtime.paused and runtime.scan_status["state"] == "page_state_unknown"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_windows_safe_transcript_filenames_are_stable_unique_and_trigger_upload(tmp_path, monkeypatch):
    from whatnot_monitor.integrations import asr
    from whatnot_monitor.integrations.asr import TranscriptResult, TranscriptSegment

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, transcripts_dir=str(tmp_path / "transcripts"))
    calls = []
    runtime._upload = lambda session_id, transcript: calls.append((session_id, transcript))
    class FakeTranscriber:
        def __init__(self, *_args, **_kwargs): pass
        def transcribe(self, _path):
            return TranscriptResult((TranscriptSegment(0, 1, "hello"),))
    monkeypatch.setattr(asr, "FasterWhisperTranscriber", FakeTranscriber)
    source = tmp_path / "input.wav"
    source.write_bytes(b"synthetic audio")
    try:
        ids = ("whatnot-show:example", "whatnot-show/example")
        paths = []
        for session_id in ids:
            store.create_session(
                session_id, "room", "room", "https://www.whatnot.com/show/id",
                started_at=None, start_time_source="first_seen_estimate",
                first_seen_at=datetime.now(UTC).isoformat(),
            )
            store.transition(session_id, "RECORDING")
            store.set_qualification_status(session_id, "qualified")
            _save_finalized_capture(store, session_id, source)
            store.transition(session_id, "TRANSCRIBING")
            runtime._transcribe_and_upload(session_id, source)
            saved = store.get_session(session_id)
            path = Path(saved.transcript_path)
            assert path.is_file() and "hello" in path.read_text(encoding="utf-8")
            assert ':' not in path.name and '/' not in path.name and '\\' not in path.name
            paths.append(path)
        assert paths[0] != paths[1]
        runtime._transcribe_and_upload(ids[0], source)
        assert Path(store.get_session(ids[0]).transcript_path) == paths[0]
        assert [item[0] for item in calls] == [ids[0], ids[1], ids[0]]
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_existing_historical_transcript_path_is_preserved_on_upload_retry(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    legacy = tmp_path / "legacy-transcript.txt"
    legacy.write_text("existing", encoding="utf-8")
    try:
        session, _ = store.create_session("legacy:id", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.save_transcript(session.session_id, "existing", str(legacy))
        runtime._retry_session(session.session_id)
        assert store.get_session(session.session_id).transcript_path == str(legacy)
        assert legacy.read_text(encoding="utf-8") == "existing"
        assert len(queued) == 1 and queued[0][1:] == (session.session_id, "existing")
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_missing_feishu_credentials_recovers_after_settings_are_saved(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    calls = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            calls.append(fields)
            return {"record_id": "synthetic-record"}
    try:
        session, _ = store.create_session("test:retry", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.save_transcript(session.session_id, "", str(tmp_path / "empty.txt"))
        store.transition(session.session_id, "UPLOADING")
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("", ""))
        runtime._upload(session.session_id, "")
        assert store.get_session(session.session_id).status == "UPLOADING"
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("fake-id", "fake-secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        assert runtime.resume_pending_feishu_uploads() == 1
        # Repeated saves may queue work twice; only one creates a row.
        runtime.resume_pending_feishu_uploads()
        runtime._postprocess.shutdown(wait=True)
        saved = store.get_session(session.session_id)
        assert saved.status == "COMPLETED" and saved.feishu_record_id == "synthetic-record"
        assert len(calls) == 1
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_empty_record_id_cannot_mark_session_completed(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, *_args, **_kwargs): return {"record_id": ""}
    try:
        session, _ = store.create_session("test:noid", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.transition(session.session_id, "UPLOADING")
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("fake-id", "fake-secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        runtime._retry_feishu_upload(session.session_id, "text")
        saved = store.get_session(session.session_id)
        assert saved.status == "FAILED" and not saved.feishu_record_id
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_historical_transcript_file_without_database_text_recovers_upload(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    fields_sent = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            fields_sent.append(fields)
            return {"record_id": "rec-history"}
    try:
        session, _ = store.create_session("legacy:path", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        legacy = tmp_path / "legacy-transcript.txt"
        legacy.write_text("历史转写内容", encoding="utf-8")
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.save_transcript(session.session_id, None, str(legacy))
        store.transition(session.session_id, "UPLOADING")
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("fake-id", "fake-secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        runtime._resume_recoverable()
        runtime._postprocess.shutdown(wait=True)
        saved = store.get_session(session.session_id)
        assert saved.status == "COMPLETED" and saved.feishu_record_id == "rec-history"
        assert saved.transcript_path == str(legacy)
        assert fields_sent[0][feishu.TRANSCRIPT_FIELD] == "历史转写内容"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_saved_remote_id_recovers_without_second_api_call_or_credentials(tmp_path, monkeypatch):
    from whatnot_monitor import config
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session, _ = store.create_session("test:known-id", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.transition(session.session_id, "UPLOADING")
        store.mark_uploaded(session.session_id, "rec-already-created")
        store.transition(session.session_id, "FAILED", error="simulated local failure")
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: (_ for _ in ()).throw(AssertionError("network/auth should not be needed")))
        assert runtime.resume_pending_feishu_uploads() == 1
        runtime._postprocess.shutdown(wait=True)
        saved = store.get_session(session.session_id)
        assert saved.status == "COMPLETED" and saved.feishu_record_id == "rec-already-created"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_remote_success_local_commit_failure_retries_same_client_token(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    tokens = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, _fields, **kwargs):
            tokens.append(kwargs["client_token"])
            return {"record_id": "rec-simulated-idempotency"}
    try:
        session, _ = store.create_session("test:local-failure", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.save_transcript(session.session_id, "content", str(tmp_path / "saved.txt"))
        store.transition(session.session_id, "UPLOADING")
        with store._conn:
            store._conn.execute("CREATE TRIGGER simulate_local_failure BEFORE UPDATE ON sessions WHEN NEW.status='COMPLETED' BEGIN SELECT RAISE(ABORT, 'local failure'); END")
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("fake-id", "fake-secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        runtime._retry_feishu_upload(session.session_id, "content")
        failed = store.get_session(session.session_id)
        assert failed.status == "FAILED" and not failed.feishu_record_id
        with store._conn:
            store._conn.execute("DROP TRIGGER simulate_local_failure")
        assert runtime.resume_pending_feishu_uploads() == 1
        runtime._postprocess.shutdown(wait=True)
        assert len(tokens) == 2 and tokens[0] == tokens[1]
        assert store.get_session(session.session_id).status == "COMPLETED"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_unreadable_historical_transcript_never_retranscribes_or_replaces_path(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session, _ = store.create_session("legacy:unreadable", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        transcript = tmp_path / "old.txt"
        transcript.write_text("keep", encoding="utf-8")
        audio = tmp_path / "audio.webm"
        audio.write_bytes(b"keep audio")
        store.transition(session.session_id, "RECORDING")
        store.save_recording_assets(session.session_id, audio_path=str(audio), video_path=None)
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.save_transcript(session.session_id, None, str(transcript))
        store.transition(session.session_id, "FAILED", error="prior upload failure")
        original = Path.read_text
        def guarded_read(path, *args, **kwargs):
            if path == transcript:
                raise PermissionError("simulated read failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", guarded_read)
        runtime._resume_recoverable()
        runtime._retry_session(session.session_id)
        assert runtime.resume_pending_feishu_uploads() == 0
        saved = store.get_session(session.session_id)
        assert saved.status == "FAILED" and saved.transcript_path == str(transcript)
        assert saved.transcript_text is None and not queued
        assert audio.read_bytes() == b"keep audio"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_upload_failure_and_concurrent_retry_do_not_race_status(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    first_inflight = threading.Event()
    release_first = threading.Event()
    failure_transition = threading.Event()
    release_failure = threading.Event()
    second_inflight = threading.Event()
    calls = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, _fields, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                first_inflight.set()
                assert release_first.wait(timeout=5)
                raise RuntimeError("simulated first network failure")
            second_inflight.set()
            return {"record_id": "rec-retry"}
    try:
        session, _ = store.create_session("test:race", "room", "room", "https://www.whatnot.com/show/id",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id)
        store.transition(session.session_id, "UPLOADING")
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("fake-id", "fake-secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        transition = store.transition
        def delayed_failed_transition(session_id, status, **kwargs):
            if status == "FAILED":
                failure_transition.set()
                assert release_failure.wait(timeout=5)
            return transition(session_id, status, **kwargs)
        monkeypatch.setattr(store, "transition", delayed_failed_transition)
        first = threading.Thread(target=runtime._retry_feishu_upload, args=(session.session_id, "text"))
        second = threading.Thread(target=runtime._retry_feishu_upload, args=(session.session_id, "text"))
        first.start()
        assert first_inflight.wait(timeout=5)
        release_first.set()
        assert failure_transition.wait(timeout=5)
        second.start()
        assert not second_inflight.wait(timeout=0.2)
        release_failure.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()
        saved = store.get_session(session.session_id)
        assert saved.status == "COMPLETED" and saved.feishu_record_id == "rec-retry"
        assert len(calls) == 2
    finally:
        release_first.set()
        release_failure.set()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_late_check_preserves_resumed_and_old_segments(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        old_audio, old_video = Path(session.audio_path), Path(session.video_path)
        runtime._stop_all_recordings()
        store.prepare_interrupted_resume(session.session_id)
        runtime._start_recording(store.get_session(session.session_id))
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert old_audio != Path(current.audio_path)
        due = datetime.fromisoformat(current.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=121))
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.qualification_status == "viewer_check_failed"
        assert current.audience_outcome == "missed"
        assert old_audio.exists() and old_video.exists()
        assert Path(current.audio_path).exists() and Path(current.video_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_on_time_low_count_preserves_previous_recording_segment(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        old_audio, old_video = Path(session.audio_path), Path(session.video_path)
        runtime._stop_all_recordings()
        store.prepare_interrupted_resume(session.session_id)
        runtime._start_recording(store.get_session(session.session_id))
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        due = datetime.fromisoformat(current.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.qualification_status == "viewer_check_failed"
        assert old_audio.exists() and old_video.exists()
        assert Path(current.audio_path).exists() and Path(current.video_path).exists()
        assert session.session_id in runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_late_check_does_not_use_stale_viewers_when_own_page_verification_active(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        due = datetime.fromisoformat(session.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(hours=1))
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        # A challenge on this recording, not an unrelated tab, blocks sampling.
        # The stale observation must not consume an attempt or trigger cleanup.
        runtime._pause_for_verification("recording page challenge", session.session_id)
        runtime._browser.inspect_page = lambda *_: (_ for _ in ()).throw(
            AssertionError("blocked recording must await explicit Start")
        )
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.audience_attempts == 0
        assert current.audience_value is None
        assert Path(current.audio_path).exists() and Path(current.video_path).exists()
        assert session.session_id in runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_missed_sample_is_retried_fresh_after_validation_resumes(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        due = datetime.fromisoformat(session.audience_due_at)
        store.save_audience(session.session_id, value=None, raw_value=None, captured_at=None, outcome="missed")
        store.set_qualification_status(session.session_id, "viewer_check_failed")
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(hours=1))
        runtime._browser.live_state = {"ended": False, "viewers": 150}
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.audience_outcome == "missed"
        assert current.qualification_status == "viewer_check_failed"
        assert current.audience_value is None
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_cleanup_refuses_media_referenced_by_another_session(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        first = _make_due_show(runtime, store)
        second, _ = runtime._persist_discovered_show(live_show("other"), ("all",), {"all": store.upsert_keyword("all")})
        store.save_recording_assets(second.session_id, audio_path=first.audio_path, video_path=second.video_path)
        assert runtime._delete_session_media(first)
        assert Path(first.audio_path).exists() and Path(first.video_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_late_low_after_stop_challenge_preserves_media_and_tab(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        due = datetime.fromisoformat(session.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        _recorder, page = runtime._recorders[session.session_id]
        closed = []
        page.close = lambda: closed.append(True)
        calls = []
        def inspect(_page):
            calls.append(1)
            if len(calls) == 1:
                return {"ended": False, "viewers": 1}
            raise WhatnotVerificationRequired("challenge")
        runtime._browser.inspect_page = inspect
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.status == "INTERRUPTED" and updated.qualification_status == "cleanup_failed"
        assert Path(updated.video_path).exists() and Path(updated.audio_path).exists()
        assert runtime._verification_required.is_set()
        assert not closed
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_late_low_stop_failure_retains_recorder_and_media(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    recorder = None
    try:
        session = _make_due_show(runtime, store)
        due = datetime.fromisoformat(session.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        recorder, _page = runtime._recorders[session.session_id]
        stop = recorder.stop
        def fail():
            raise RuntimeError("stop failed")
        recorder.stop = fail
        runtime._check_due_audience()
        updated = store.get_session(session.session_id)
        assert updated.status == "INTERRUPTED" and updated.qualification_status == "cleanup_failed"
        assert session.session_id in runtime._recorders
        assert Path(updated.video_path).exists() and Path(updated.audio_path).exists()
        recorder.stop = stop
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_cleanup_refuses_backup_and_reparse_paths(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        original = Path(session.audio_path)
        monkeypatch.setattr(runtime, "_path_has_reparse_point", lambda _path: True)
        assert "重解析" in runtime._delete_session_media(session)
        assert original.exists()
        monkeypatch.setattr(runtime, "_path_has_reparse_point", lambda _path: False)
        backup = tmp_path / "backup" / "audio.webm"
        backup.parent.mkdir()
        backup.write_bytes(b"keep")
        updated = replace(session, audio_path=str(backup), recording_path=str(backup))
        assert "备份" in runtime._delete_session_media(updated)
        assert backup.exists() and original.exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_late_feishu_record_does_not_claim_30_minute_count(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    sent = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            sent.append(fields)
            return {"record_id": "synthetic"}
    try:
        session = _make_due_show(runtime, store)
        due = datetime.fromisoformat(session.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(minutes=5))
        runtime._browser.live_state = {"ended": False, "viewers": 150}
        runtime._check_due_audience()
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("id", "secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        monkeypatch.setattr(store, "finalize_uploaded", lambda *_: None)
        runtime._upload_once(session.session_id, "转写内容")
        assert feishu.AUDIENCE_FIELD not in sent[0]
        text = sent[0][feishu.TRANSCRIPT_FIELD]
        assert "实际延迟人数：150" not in text
        assert store.get_session(session.session_id).audience_outcome == "missed"
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()

def test_feishu_estimated_start_uses_recording_start_not_first_seen(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    sent = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            sent.append(fields)
            return {"record_id": "synthetic"}
    try:
        first_seen = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        show = live_show("late-first-seen", first_seen=first_seen)
        session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
        runtime._start_recording(session)
        runtime._check_due_audience()
        session = store.get_session(session.session_id)
        due = datetime.fromisoformat(session.audience_due_at)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(minutes=5))
        runtime._browser.live_state = {"ended": False, "viewers": 150}
        runtime._check_due_audience()
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("id", "secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        monkeypatch.setattr(store, "finalize_uploaded", lambda *_: None)
        runtime._upload_once(session.session_id, "转写内容")
        submitted_ms = sent[0][feishu.START_TIME_FIELD]
        recording_ms = datetime.fromisoformat(session.recording_started_at).timestamp() * 1000
        first_seen_ms = datetime.fromisoformat(first_seen).timestamp() * 1000
        assert abs(submitted_ms - recording_ms) <= 1
        assert abs(submitted_ms - first_seen_ms) > 60_000
        assert feishu.AUDIENCE_FIELD not in sent[0]
        assert "录制开始时间" in sent[0][feishu.TRANSCRIPT_FIELD]
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()

def test_interrupted_manual_and_automatic_rejection_have_truthful_recovery_events(tmp_path, monkeypatch):
    for outcome, expected in (("manual_confirmed_low", "用户确认"), ("captured_late", "延迟采样"), ("captured", "人数采样"), (None, "请查看历史事件")):
        case = tmp_path / str(outcome)
        case.mkdir()
        store, runtime = make_runtime(case, monkeypatch, {"all": []})
        queued = []
        runtime._submit_postprocess = lambda *args, queued=queued: queued.append(args)
        try:
            session = _make_due_show(runtime, store)
            runtime._stop_all_recordings()
            with store._lock, store._conn:
                store._conn.execute("UPDATE sessions SET qualification_status='rejected_low_viewers', audience_outcome=? WHERE session_id=?", (outcome, session.session_id))
            old = store.get_session(session.session_id)
            runtime._resume_recoverable()
            current = store.get_session(session.session_id)
            events = [event.message for event in store.recent_events(limit=20) if event.session_id == session.session_id and event.component == "recovery"]
            assert events and expected in events[0]
            assert "现有媒体作为不完整片段保留" not in events[0]
            assert current.status == old.status == "INTERRUPTED"
            assert current.audience_value == old.audience_value
            assert current.error == old.error
            assert not queued
        finally:
            runtime._postprocess.shutdown(wait=True)
            store.close()


def test_qualified_manual_stop_is_partial_and_never_submits_postprocess(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "qualified")
        runtime.stop_recording(session.session_id)
        runtime._monitor_recordings()
        saved = store.get_session(session.session_id)
        assert saved.status == "INTERRUPTED"
        assert "手动停止" in saved.error
        assert store.list_recording_segments(session.session_id)[0]["status"] == "interrupted"
        assert Path(saved.audio_path).is_file() and Path(saved.video_path).is_file()
        assert not queued and session.session_id not in runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_confirmed_natural_end_queues_only_finalized_capture(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "qualified")
        runtime._browser.live_state = {"ended_candidate": True}
        for _ in range(2):
            runtime._monitor_recordings()
            assert not queued
        runtime._monitor_recordings()
        saved = store.get_session(session.session_id)
        assert saved.status == "TRANSCRIBING" and len(queued) == 1
        assert runtime._capture_ready_for_postprocess(saved)
        assert store.list_recording_segments(session.session_id)[0]["reason"] == "natural_end"
        assert queued[0][1:] == (session.session_id, Path(saved.audio_path))
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_mp4_export_waits_for_finalized_capture(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    exported = []
    monkeypatch.setattr("whatnot_monitor.recording.video_export.export_mp4", exported.append)
    try:
        session = _make_due_show(runtime, store)
        runtime._export_finalized_video(session.session_id)
        assert exported == []
        store.set_qualification_status(session.session_id, "qualified")
        runtime._browser.live_state = {"ended_candidate": True}
        monkeypatch.setattr(runtime, "_submit_postprocess", lambda *args: None)
        for _ in range(3):
            runtime._monitor_recordings()
        saved = store.get_session(session.session_id)
        runtime._export_finalized_video(saved.session_id)
        assert exported == [Path(saved.video_path)]
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_qualified_active_capture_cannot_retry_transcribe_or_upload(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    monkeypatch.setattr(runtime, "_convert_to_wav", lambda *_: (_ for _ in ()).throw(AssertionError("must not read growing file")))
    monkeypatch.setattr(runtime, "_upload_once", lambda *_: (_ for _ in ()).throw(AssertionError("must not upload active capture")))
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "qualified")
        runtime._retry_session(session.session_id)
        runtime._transcribe_and_upload(session.session_id, Path(session.audio_path))
        runtime._upload(session.session_id, "not a completed transcript")
        assert store.get_session(session.session_id).status == "RECORDING"
        assert session.session_id in runtime._recorders
        assert not queued
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_failed_or_unproven_segment_cannot_resume_postprocessing(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session, _ = store.create_session("unproven", "room", "room", "https://example.invalid/live/x",
            started_at=None, start_time_source="first_seen_estimate", first_seen_at=datetime.now(UTC).isoformat())
        audio = tmp_path / "partial.webm"
        audio.write_bytes(b"partial")
        store.transition(session.session_id, "RECORDING")
        store.set_qualification_status(session.session_id, "qualified")
        _save_finalized_capture(store, session.session_id, audio)
        segment_id = store.list_recording_segments(session.session_id)[0]["segment_id"]
        store.update_recording_segment(segment_id, "failed", timestamp=datetime.now(UTC).isoformat())
        store.transition(session.session_id, "FAILED", error="finalization failed")
        runtime._retry_session(session.session_id)
        runtime._resume_recoverable()
        runtime._transcribe_and_upload(session.session_id, audio)
        assert not queued and audio.read_bytes() == b"partial"
        assert store.get_session(session.session_id).transcript_text is None
        store.update_recording_segment(segment_id, "completed", timestamp=datetime.now(UTC).isoformat(), reason="natural_end")
        audio.write_bytes(b"")
        runtime._retry_session(session.session_id)
        assert not runtime._capture_ready_for_postprocess(store.get_session(session.session_id))
        assert not queued
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_transient_end_during_audience_check_can_return_to_live(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = {"ended_candidate": True, "viewers": None}
        runtime._check_due_audience()
        runtime._monitor_recordings()
        pending = store.get_session(session.session_id)
        assert pending.status == "RECORDING" and pending.audience_attempts == 0
        runtime._browser.live_state = {"live": True, "viewers": 150}
        runtime._monitor_recordings()
        runtime._check_due_audience()
        saved = store.get_session(session.session_id)
        assert saved.qualification_status == "qualified" and saved.audience_value == 150
        assert saved.status == "RECORDING"
        assert session.session_id not in runtime._end_confirmations
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_only_trusted_on_time_thirty_minute_sample_fills_legacy_numeric_field(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        anchor = datetime.now(UTC)
        for basis, interval, seconds, expected in (
            ("platform_start", 1, 1, None),
            ("platform_start", 30, 1, 150),
            ("recording_start_estimate", 30, 1, None),
            ("platform_start", 30, 121, None),
            ("platform_start", 30, -1, None),
        ):
            due = anchor + timedelta(minutes=interval)
            sample = replace(session, started_at=anchor.isoformat(), recording_started_at=anchor.isoformat(),
                start_time_source="platform_started_at", audience_basis=basis, audience_due_at=due.isoformat(),
                audience_captured_at=(due + timedelta(seconds=seconds)).isoformat(), audience_outcome="captured", audience_value=150)
            assert runtime._reported_30_minute_audience(sample) == expected
        # Reporting uses the persisted schedule, not a later settings edit.
        runtime.settings = replace(runtime.settings, qualification_minutes=1)
        assert runtime._saved_audience_interval_minutes(sample) == 30
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_historical_platform_sample_reporting_is_read_only_and_requires_evidence(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        anchor = datetime.now(UTC) - timedelta(minutes=31)
        due = anchor + timedelta(minutes=30)
        sample = replace(session, started_at=anchor.isoformat(), start_time_source="platform_started_at",
                         audience_basis="platform_start", audience_due_at=due.isoformat(),
                         audience_captured_at=(due + timedelta(seconds=2)).isoformat(),
                         audience_outcome="captured", audience_value=80, qualification_minutes=30)
        assert runtime._reported_audience(sample) == 80
        assert runtime._reported_audience(replace(sample, qualification_minutes=None)) == 80
        assert runtime._audience_timing_error(sample, sample.audience_captured_at) is not None
        invalid = (
            replace(sample, start_time_source="first_seen_estimate"),
            replace(sample, started_at=anchor.replace(tzinfo=None).isoformat()),
            replace(sample, audience_due_at=anchor.isoformat()),
            replace(sample, qualification_minutes=10),
            replace(sample, audience_captured_at=(due + timedelta(seconds=121)).isoformat()),
            replace(sample, audience_captured_at=(due - timedelta(seconds=1)).isoformat()),
            replace(sample, audience_outcome="captured_late"),
            replace(sample, audience_value=-1),
        )
        for item in invalid:
            assert runtime._reported_audience(item) is None
        assert Path(session.audio_path).exists() and Path(session.video_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_feishu_upload_fields_preserve_only_verifiable_historical_platform_audience(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    sent = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            sent.append(fields)
            return {"record_id": "mock-record"}
    try:
        session = _make_due_show(runtime, store)
        anchor = datetime.now(UTC) - timedelta(minutes=31)
        due = anchor + timedelta(minutes=30)
        valid = replace(session, started_at=anchor.isoformat(), start_time_source="platform_started_at",
                        audience_basis="platform_start", audience_due_at=due.isoformat(),
                        audience_captured_at=(due + timedelta(seconds=2)).isoformat(),
                        audience_outcome="captured", audience_value=80, qualification_minutes=30)
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("id", "secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        monkeypatch.setattr(store, "finalize_uploaded", lambda *_args: None)
        cases = (
            (valid, 80),
            (replace(valid, qualification_minutes=None), 80),
            (replace(valid, audience_captured_at=(due + timedelta(seconds=121)).isoformat()), None),
            (replace(valid, start_time_source="first_seen_estimate"), None),
            (replace(valid, qualification_minutes=10), None),
        )
        for item, expected in cases:
            monkeypatch.setattr(store, "get_session", lambda _session_id, item=item: item)
            runtime._upload_once(session.session_id, "转写内容")
            fields = sent[-1]
            if expected is None:
                assert feishu.AUDIENCE_FIELD not in fields
            else:
                assert fields[feishu.AUDIENCE_FIELD] == expected
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_feishu_upload_never_invents_show_end_time_for_rejected_recording(tmp_path, monkeypatch):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    sent = []
    class Client:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def create_record(self, fields, **_kwargs):
            sent.append(fields)
            return {"record_id": "mock-record"}
    try:
        session = _make_due_show(runtime, store)
        monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("id", "secret"))
        monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
        monkeypatch.setattr(store, "finalize_uploaded", lambda *_args: None)
        ended = datetime(2026, 11, 15, 14, 0, tzinfo=UTC)
        for ended_at in (None, ended.isoformat()):
            sample = replace(session, ended_at=ended_at, qualification_status="rejected_low_viewers")
            monkeypatch.setattr(store, "get_session", lambda _id, item=sample: item)
            runtime._upload_once(session.session_id, "筛选未达标")
        assert feishu.END_TIME_FIELD not in sent[0]
        assert sent[1][feishu.END_TIME_FIELD] == int(ended.timestamp() * 1000)
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()
