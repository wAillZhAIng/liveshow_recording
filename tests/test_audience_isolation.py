"""An unrelated navigation pause cannot consume safe recording due windows."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_runtime import live_show, make_runtime
from whatnot_monitor.bounded_browser_read import BrowserReadTimeout
from whatnot_monitor.store import Store
from whatnot_monitor.whatnot import WhatnotPageStateUnknown, WhatnotVerificationRequired


def due_show(store, runtime, name):
    runtime.settings.qualification_minutes = 10
    runtime.settings.minimum_viewers = 80
    start = datetime.now(UTC) - timedelta(minutes=10, seconds=5)
    session, _ = runtime._persist_discovered_show(
        replace(live_show(name), started_at=None), (), {}
    )
    with patch("whatnot_monitor.runtime._now", return_value=start):
        runtime._start_recording(session)
    store.set_audience_schedule(
        session.session_id, (start + timedelta(minutes=10)).isoformat(),
        "recording_start_estimate",
    )
    return store.get_session(session.session_id)


def dispose(store, runtime):
    runtime._stop_all_recordings()
    runtime._postprocess.shutdown(wait=True)
    store.close()


@pytest.mark.parametrize("challenge", [False, True])
def test_unrelated_pause_still_qualifies_six_healthy_pages(tmp_path, monkeypatch, challenge):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        sessions = [due_show(store, runtime, str(i)) for i in range(6)]
        waiting, _ = runtime._persist_discovered_show(live_show("waiting"), (), {})
        if challenge:
            runtime._pause_for_verification("unrelated waiting page", waiting.session_id)
        else:
            runtime._pause_for_unknown_page(waiting.session_id, "unrelated read timeout")
        runtime._browser.live_state = {"viewers": 81, "viewers_raw": "81 viewers"}
        runtime._browser.inspect_live = lambda *_: pytest.fail("new navigation")
        runtime._ensure_browser = lambda: pytest.fail("browser attachment")
        runtime._check_due_audience()
        for session in sessions:
            current = store.get_session(session.session_id)
            assert current.audience_value == 81
            assert current.audience_outcome == "captured"
            assert current.qualification_status == "qualified"
            assert current.status == "RECORDING"
        assert runtime._verification_required.is_set() and runtime.paused
        runtime._start_waiting_sessions()
        runtime.scan_once()  # Navigation remains gated, without clearing safety.
        assert runtime._verification_required.is_set()
    finally:
        dispose(store, runtime)


@pytest.mark.parametrize("error", [
    WhatnotPageStateUnknown("unreadable"),
    WhatnotVerificationRequired("verify you are human"),
    BrowserReadTimeout("read deadline"),
])
def test_one_blocked_recorder_does_not_prevent_other_due_samples(tmp_path, monkeypatch, error):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        blocked = due_show(store, runtime, "blocked")
        healthy = due_show(store, runtime, "healthy")
        blocked_page = runtime._recorders[blocked.session_id][1]
        calls = []

        def inspect(page):
            calls.append(page)
            if page is blocked_page:
                raise error
            return {"viewers": 90, "viewers_raw": "90 viewers"}

        runtime._browser.inspect_page = inspect
        runtime._check_due_audience()
        runtime._check_due_audience()
        current = store.get_session(blocked.session_id)
        assert current.audience_attempts == 0 and current.audience_value is None
        assert current.status == "RECORDING"
        assert Path(current.audio_path).exists()
        assert calls.count(blocked_page) == 1  # No automatic challenge retries.
        assert store.get_session(healthy.session_id).qualification_status == "qualified"
        assert runtime._verification_required.is_set()
    finally:
        dispose(store, runtime)


def test_low_sample_is_saved_but_mutation_is_deferred_until_start(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = due_show(store, runtime, "low")
        waiting, _ = runtime._persist_discovered_show(live_show("waiting"), (), {})
        runtime._pause_for_unknown_page(waiting.session_id, "unrelated page")
        runtime._browser.live_state = {"viewers": 79, "viewers_raw": "79 viewers"}
        rejected = []
        runtime._reject_low_viewers = lambda *args: rejected.append(args)
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.audience_value == 79 and current.audience_outcome == "captured"
        assert current.qualification_status == "low_viewers_deferred"
        reopened = Store(store._db_path)
        try:
            saved = reopened.get_session(session.session_id)
            assert saved.qualification_status == "low_viewers_deferred"
            assert saved.audience_value == 79 and saved.audience_outcome == "captured"
        finally:
            reopened.close()
        assert current.status == "RECORDING" and rejected == []
        assert Path(current.audio_path).exists()
        runtime._check_due_audience()
        assert rejected == []
        runtime._thread = SimpleNamespace(is_alive=lambda: True)
        runtime.start()
        runtime._check_due_audience()
        assert len(rejected) == 1
        assert rejected[0][1:] == (79, "79 viewers")
    finally:
        dispose(store, runtime)


def test_browser_local_block_skips_only_that_page_and_never_attaches(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        blocked = due_show(store, runtime, "blocked")
        healthy = due_show(store, runtime, "healthy")
        page = runtime._recorders[blocked.session_id][1]
        runtime._browser.recording_page_observation_allowed = lambda actual: actual is not page
        runtime._browser.live_state = {"viewers": 99}
        runtime._check_due_audience()
        assert store.get_session(blocked.session_id).audience_attempts == 0
        assert store.get_session(healthy.session_id).qualification_status == "qualified"
        runtime._browser = None
        runtime._ensure_browser = lambda: pytest.fail("must not attach for observation")
        runtime._check_due_audience()
        assert store.get_session(blocked.session_id).audience_attempts == 0
    finally:
        dispose(store, runtime)
