"""Verification is a manual pause, never a zero/ended audience observation."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime import FakeAVRecorder, _make_due_show, live_show, make_runtime
from whatnot_monitor.whatnot import WhatnotVerificationRequired


def dispose(store, runtime):
    runtime._stop_all_recordings()
    runtime._postprocess.shutdown(wait=True)
    store.close()


def test_due_audience_reads_active_page_without_opening_room(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        page = runtime._recorders[session.session_id][1]
        observed = []

        def inspect(actual):
            assert actual is page
            observed.append(actual)
            return {"viewers": 123, "viewers_raw": "123 viewers", "ended_candidate": False}

        runtime._browser.inspect_page = inspect
        runtime._browser.inspect_live = lambda _: (_ for _ in ()).throw(AssertionError("new navigation"))
        runtime._check_due_audience()
        assert observed == [page]
        assert store.get_session(session.session_id).audience_value == 123
    finally:
        dispose(store, runtime)


def test_audience_challenge_pauses_without_consuming_attempts_or_media(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = WhatnotVerificationRequired("请验证您是真人")
        for _ in range(3):
            runtime._check_due_audience()
            runtime._start_waiting_sessions()
            runtime.scan_once()
        current = store.get_session(session.session_id)
        assert current.status == "RECORDING"
        assert current.audience_attempts == 0
        assert current.audience_value is None
        assert current.qualification_status == "pending"
        assert Path(current.video_path).is_file()
        assert Path(current.audio_path).is_file()
        assert runtime._verification_required.is_set()
        assert runtime.paused
        assert runtime.scan_status["state"] == "verification_required"
    finally:
        dispose(store, runtime)


def test_recording_challenge_retains_page_and_healthy_recorder(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        recorder, page = runtime._recorders[session.session_id]
        recorder.has_capture_state = lambda: True
        page.close = lambda: (_ for _ in ()).throw(AssertionError("challenge page closed"))
        runtime._end_confirmations[session.session_id] = 2
        runtime._browser.live_state = WhatnotVerificationRequired("verify you are human")
        for _ in range(3):
            runtime._monitor_recordings()
        assert session.session_id in runtime._recorders
        assert store.get_session(session.session_id).status == "RECORDING"
        assert session.session_id not in runtime._end_confirmations
        events = [event for event in store.recent_events() if event.component == "whatnot"]
        assert len(events) == 1
    finally:
        dispose(store, runtime)


def test_replaced_recording_document_is_interrupted_and_never_closed(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        recorder, page = runtime._recorders[session.session_id]
        recorder.has_capture_state = lambda: False
        aborted = []
        recorder.abort_lost_document = lambda: aborted.append(True)
        recorder.stop = lambda: (_ for _ in ()).throw(AssertionError("stop commands replacement page"))
        page.close = lambda: (_ for _ in ()).throw(AssertionError("challenge page closed"))
        runtime._browser.live_state = WhatnotVerificationRequired("安全验证")
        runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED"
        assert aborted == [True]
        assert "安全验证" in current.error and "不完整片段" in current.error
        assert current.ended_at
        assert current.qualification_status == "pending"
        assert current.audience_attempts == 0
        assert current.audience_outcome not in {"missed", "ended_before_due", "failed"}
        assert Path(current.video_path).read_bytes() == b"synthetic video"
        assert Path(current.audio_path).read_bytes() == b"synthetic audio"
        assert not runtime._recorders
        assert not runtime._postprocess_futures
    finally:
        dispose(store, runtime)


def test_non_challenge_navigation_cannot_masquerade_as_successful_end(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        recorder, _ = runtime._recorders[session.session_id]
        recorder.has_capture_state = lambda: False
        runtime._browser.live_state = {"ended_candidate": True}
        runtime._end_confirmations[session.session_id] = 2
        runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED"
        assert current.qualification_status == "pending"
        assert Path(current.audio_path).is_file()
        assert not runtime._postprocess_futures
    finally:
        dispose(store, runtime)


def test_manual_verification_resume_can_sample_healthy_capture_without_lost_attempt(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime._browser.live_state = WhatnotVerificationRequired("安全验证")
        runtime._check_due_audience()
        assert store.get_session(session.session_id).audience_attempts == 0
        # The UI Start signal is required; merely waiting never retries a challenge.
        runtime._thread = SimpleNamespace(is_alive=lambda: True)
        runtime._browser.live_state = {"ended_candidate": False, "viewers": 123}
        runtime.start()
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.audience_attempts == 1
        assert current.audience_value == 123
        assert current.qualification_status == "qualified"
        assert current.status == "RECORDING"
        assert not runtime._verification_required.is_set()
    finally:
        dispose(store, runtime)


def test_waiting_room_challenge_stops_further_navigation(tmp_path, monkeypatch):
    from test_runtime import live_show

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        for index in range(2):
            runtime._persist_discovered_show(live_show(f"wait-{index}"), (), {})
        calls = []

        def inspect(url):
            calls.append(url)
            raise WhatnotVerificationRequired("安全验证")

        runtime._browser.inspect_live = inspect
        runtime._start_waiting_sessions()
        runtime._start_waiting_sessions()
        assert len(calls) == 1
        assert runtime._verification_required.is_set()
        assert all(session.status == "WAITING" and not session.retry_at for session in store.list_sessions())
    finally:
        dispose(store, runtime)


@pytest.mark.parametrize("phase", ["loading", "action_visible", "unknown"])
def test_runtime_preserves_verification_phase_for_ui(tmp_path, monkeypatch, phase):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        runtime._browser.verification_info = {"verification_phase": phase}
        runtime._pause_for_verification("test")
        assert runtime.scan_status["verification_phase"] == phase
        if phase == "loading":
            assert "尚未检测到可操作控件" in runtime.scan_status["blocking_reason"]
    finally:
        dispose(store, runtime)


@pytest.mark.parametrize("unknown", [False, True])
def test_failed_recorder_start_retains_late_challenge_or_unknown_page(tmp_path, monkeypatch, unknown):
    from whatnot_monitor.whatnot import WhatnotPageStateUnknown

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    closed = []
    class FailingRecorder(FakeAVRecorder):
        def start(self, **kwargs):
            super().start(**kwargs)
            raise RuntimeError("document replaced during start")

        def stop(self):
            raise AssertionError("must not command the challenge")

    monkeypatch.setattr("whatnot_monitor.recording.browser_av.BrowserAVRecorder", FailingRecorder)
    runtime._browser.new_room_page = lambda *_args, **_kwargs: SimpleNamespace(close=lambda: closed.append(True))
    error = WhatnotPageStateUnknown("unreadable") if unknown else WhatnotVerificationRequired("安全验证")
    runtime._browser.live_state = error
    try:
        session, _ = runtime._persist_discovered_show(live_show("late-challenge"), (), {})
        with pytest.raises(type(error)):
            runtime._start_recording(session)
        assert closed == []
        assert runtime.paused and runtime._verification_required.is_set()
        current = store.get_session(session.session_id)
        assert Path(current.video_path).read_bytes() == b"synthetic video"
        assert Path(current.audio_path).read_bytes() == b"synthetic audio"
        assert not runtime._recorders
    finally:
        dispose(store, runtime)


def test_shutdown_saves_partial_metadata_even_when_stop_loses_document(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        recorder, _ = runtime._recorders[session.session_id]
        recorder.stop = lambda: (_ for _ in ()).throw(RuntimeError("capture state was lost"))
        runtime._stop_all_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED"
        assert current.ended_at
        assert "capture state was lost" in current.error
        assert Path(current.audio_path).is_file()
    finally:
        dispose(store, runtime)


def _structured_health(runtime, session, *, status="active", href=None):
    recorder, page = runtime._recorders[session.session_id]
    page.url = session.url
    snapshot = {"status": status, "href": href or session.url}
    recorder.capture_health = lambda: dict(snapshot)
    recorder.finalization_identity = lambda: {"status": "finalized", "href": snapshot["href"]}
    aborted = []
    recorder.abort_stale_capture = lambda: aborted.append(True)
    return recorder, page, snapshot, aborted


@pytest.mark.parametrize("replacement_ended", [False, True])
def test_spa_show_drift_never_updates_original_or_finalizes_replacement(tmp_path, monkeypatch, replacement_ended):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        other, _ = runtime._persist_discovered_show(live_show("replacement"), (), {})
        recorder, page, _snapshot, aborted = _structured_health(
            runtime, session, href=other.url,
        )
        page.url = other.url
        recorder.stop = lambda: (_ for _ in ()).throw(AssertionError("must not stop newer global"))
        page.close = lambda: (_ for _ in ()).throw(AssertionError("must not close replacement"))
        runtime._browser.live_state = {"ended_candidate": replacement_ended, "viewers": 999}
        runtime._end_confirmations[session.session_id] = 2
        before = store.get_session(session.session_id)
        runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED" and aborted == [True]
        assert current.current_viewers == before.current_viewers
        assert current.observed_at == before.observed_at
        assert store.get_session(other.session_id).status == "WAITING"
        assert store.list_recording_segments(other.session_id) == []
        assert all(segment["status"] == "interrupted" for segment in store.list_recording_segments(session.session_id))
        assert Path(current.audio_path).is_file() and Path(current.video_path).is_file()
        assert session.session_id not in runtime._end_confirmations
        assert not runtime._postprocess_futures
    finally:
        dispose(store, runtime)


def test_ended_capture_tracks_do_not_qualify_or_claim_new_dom_media(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        recorder, page, _snapshot, aborted = _structured_health(runtime, session, status="terminated")
        recorder.stop = lambda: (_ for _ in ()).throw(AssertionError("no unbounded stale stop"))
        page.close = lambda: (_ for _ in ()).throw(AssertionError("page preserved"))
        runtime._browser.live_state = {"ended_candidate": False, "live": True, "viewers": 999}
        assert not runtime._audience_capture_is_current(session, runtime._recorders[session.session_id])
        runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED" and aborted == [True]
        assert current.audience_value is None and current.qualification_status == "pending"
        assert not runtime._postprocess_futures
    finally:
        dispose(store, runtime)


@pytest.mark.parametrize("missing_href", [False, True])
def test_unknown_identity_preserves_capture_resets_end_and_services_healthy_page(tmp_path, monkeypatch, missing_href):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        unknown = _make_due_show(runtime, store)
        _recorder, _page, snapshot, aborted = _structured_health(runtime, unknown, status="unknown")
        if missing_href:
            snapshot.pop("href")
        runtime._end_confirmations[unknown.session_id] = 2
        healthy, _ = runtime._persist_discovered_show(live_show("healthy"), (), {})
        runtime._start_recording(healthy)
        healthy = store.get_session(healthy.session_id)
        _structured_health(runtime, healthy)
        runtime._browser.live_state = {"live": True, "viewers": 81, "ended_candidate": False}
        runtime._monitor_recordings()
        assert store.get_session(unknown.session_id).status == "RECORDING"
        assert not aborted and unknown.session_id in runtime._recorders
        assert unknown.session_id not in runtime._end_confirmations
        assert runtime.paused
        assert store.get_session(healthy.session_id).current_viewers == 81
    finally:
        dispose(store, runtime)


def test_same_show_terminated_tracks_can_finish_only_after_three_end_confirmations(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "qualified")
        _recorder, page, _snapshot, aborted = _structured_health(runtime, session, status="terminated")
        page.close = lambda: (_ for _ in ()).throw(AssertionError("no close-after-read race"))
        runtime._browser.live_state = {"ended_candidate": True}
        for _ in range(2):
            runtime._monitor_recordings()
            assert not queued
        runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "TRANSCRIBING" and len(queued) == 1
        assert not aborted
        assert runtime._capture_ready_for_postprocess(current)
        assert all(segment["reason"] == "natural_end" for segment in store.list_recording_segments(session.session_id))
    finally:
        dispose(store, runtime)


def test_identity_change_during_dom_read_discards_observation(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        _recorder, _page, snapshot, aborted = _structured_health(runtime, session)
        def inspect(_page):
            snapshot["href"] = "https://whatnot.com/live/another-show"
            return {"ended_candidate": True, "viewers": 999}
        runtime._browser.inspect_page = inspect
        runtime._end_confirmations[session.session_id] = 2
        runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED" and aborted == [True]
        assert current.current_viewers != 999
        assert not runtime._postprocess_futures
    finally:
        dispose(store, runtime)


@pytest.mark.parametrize("after_stop_status", ["drifted", "replaced", "unknown"])
def test_finalization_identity_race_retains_interrupted_partial(tmp_path, monkeypatch, after_stop_status):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "qualified")
        recorder, page, _snapshot, aborted = _structured_health(runtime, session)
        recorder.finalization_identity = lambda: {"status": after_stop_status, "href": session.url}
        page.close = lambda: (_ for _ in ()).throw(AssertionError("must preserve replacement"))
        runtime._browser.live_state = {"ended_candidate": True}
        for _ in range(3):
            runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED" and aborted == [True]
        assert not queued
        assert all(segment["status"] == "interrupted" for segment in store.list_recording_segments(session.session_id))
        assert Path(current.audio_path).is_file()
    finally:
        dispose(store, runtime)


def test_challenge_arriving_during_stop_prevents_natural_completion(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    queued = []
    runtime._submit_postprocess = lambda *args: queued.append(args)
    try:
        session = _make_due_show(runtime, store)
        store.set_qualification_status(session.session_id, "qualified")
        recorder, page, _snapshot, aborted = _structured_health(runtime, session)
        recorder.stop = lambda: setattr(runtime._browser, "live_state", WhatnotVerificationRequired("challenge"))
        page.close = lambda: (_ for _ in ()).throw(AssertionError("must preserve challenge"))
        runtime._browser.live_state = {"ended_candidate": True}
        for _ in range(3):
            runtime._monitor_recordings()
        current = store.get_session(session.session_id)
        assert current.status == "INTERRUPTED" and aborted == [True]
        assert runtime._verification_required.is_set() and not queued
        assert all(segment["status"] == "interrupted" for segment in store.list_recording_segments(session.session_id))
    finally:
        dispose(store, runtime)


def test_missing_requested_show_identity_does_not_open_recording_page(tmp_path, monkeypatch):
    from dataclasses import replace

    from whatnot_monitor.whatnot import WhatnotPageStateUnknown

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session, _ = runtime._persist_discovered_show(
            replace(live_show("no-url-identity"), url="https://whatnot.com/"), (), {},
        )
        runtime._browser.new_room_page = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unknown target navigated"))
        with pytest.raises(WhatnotPageStateUnknown):
            runtime._start_recording(session)
        assert runtime.paused and not runtime._recorders
        assert store.list_recording_segments(session.session_id) == []
    finally:
        dispose(store, runtime)
