from pathlib import Path

from test_runtime import live_show, make_runtime

from whatnot_monitor.whatnot import WhatnotVerificationRequired


def test_all_discoveries_are_durable_before_first_recording_attempt(tmp_path, monkeypatch):
    shows = {"A": [live_show("1"), live_show("2"), live_show("3")],
             "B": [live_show("2"), live_show("3"), live_show("4")],
             "C": [live_show("1"), live_show("4"), live_show("5")]}
    store, runtime = make_runtime(tmp_path, monkeypatch, shows)
    for name in shows:
        store.upsert_keyword(name)
    runtime._wait_with_browser_events = lambda *_args, **_kwargs: None
    attempted = []

    def blocked_start(session):
        attempted.append(session.session_id)
        # The first potentially slow or interrupted browser operation must only
        # occur once all five distinct shows and all their sources are durable.
        sessions = store.list_sessions()
        assert len(sessions) == 5
        sources = {s.session_id.rsplit(':', 1)[-1]:
                   {x.phrase_snapshot for x in store.session_keyword_sources(s.session_id)}
                   for s in sessions}
        assert sources == {'1': {'A', 'C'}, '2': {'A', 'B'},
                           '3': {'A', 'B'}, '4': {'B', 'C'}, '5': {'C'}}
        raise WhatnotVerificationRequired('verification')

    runtime._start_recording = blocked_start
    try:
        runtime.scan_once()
        assert len(attempted) == 1
        assert len(store.list_sessions()) == 5
        assert runtime.scan_status['state'] == 'verification_required'
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_fresh_explicit_search_evidence_starts_queued_show_but_expired_does_not(tmp_path, monkeypatch):
    import time

    store, runtime = make_runtime(tmp_path, monkeypatch, {"A": [live_show("1"), live_show("2")]})
    store.upsert_keyword("A")
    attempted = []
    runtime._start_recording = lambda session: attempted.append(session.session_id)
    try:
        runtime.scan_once()
        queued = store.list_sessions(statuses={"WAITING"})
        runtime._browser.live_state = {"live": False, "ended": False}
        runtime._start_waiting_sessions()
        assert attempted[-1] in {s.session_id for s in queued}
        attempted.clear()
        for session in queued:
            runtime._live_search_evidence[session.session_id] = time.monotonic() - 91
        runtime._start_waiting_sessions()
        assert attempted == []
        assert any(s.retry_at for s in store.list_sessions(statuses={"WAITING"}))
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_explicit_ended_overrides_fresh_search_and_live_state(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"A": [live_show("1")]})
    store.upsert_keyword("A")
    runtime._start_recording = lambda session: None
    try:
        runtime.scan_once()
        session = store.list_sessions(statuses={"WAITING"})[0]
        attempted = []
        runtime._start_recording = lambda item: attempted.append(item.session_id)
        runtime._browser.live_state = {"live": True, "ended": True}
        runtime._start_waiting_sessions()
        assert attempted == []
        assert store.get_session(session.session_id).status == "INTERRUPTED"
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_stop_during_queue_inspection_never_opens_recorder(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"A": [live_show("1")]})
    store.upsert_keyword("A")
    runtime._start_recording = lambda session: None
    try:
        runtime.scan_once()
        session = store.list_sessions(statuses={"WAITING"})[0]
        attempted = []
        runtime._start_recording = lambda item: attempted.append(item.session_id)
        def inspecting(_url):
            runtime._stop.set()
            return {"live": True, "ended": False}
        runtime._browser.inspect_live = inspecting
        runtime._start_waiting_sessions()
        assert attempted == []
        assert store.get_session(session.session_id).status == "WAITING"
        runtime._start_waiting_sessions()
        assert attempted == []
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_stalled_file_warns_without_interrupting_capture(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"A": [live_show("1")]})
    store.upsert_keyword("A")
    try:
        runtime.scan_once()
        session_id = next(iter(runtime._recorders))
        runtime._file_watchdog.timeout_seconds = 0
        runtime._file_watchdog.start()
        runtime._file_watchdog.poll_once()
        runtime._file_watchdog.poll_once()
        import time
        deadline = time.monotonic() + 3
        while not any("文件无增长" in e.message for e in store.recent_events()) and time.monotonic() < deadline:
            time.sleep(0.01)
        runtime._file_watchdog.stop()
        assert session_id in runtime._recorders
        assert store.get_session(session_id).status == "RECORDING"
        assert any("文件无增长" in e.message for e in store.recent_events())
    finally:
        runtime._file_watchdog.stop()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_interrupted_qualified_capture_is_not_processed_as_complete(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"A": [live_show("1")]})
    store.upsert_keyword("A")
    try:
        runtime.scan_once()
        session_id = next(iter(runtime._recorders))
        session = store.get_session(session_id)
        original_path = session.audio_path
        store.set_qualification_status(session_id, "qualified")
        store.transition(session_id, "INTERRUPTED", error="process stopped")
        submitted = []
        runtime._submit_postprocess = lambda *args, **kwargs: submitted.append(args)
        runtime._resume_recoverable()
        assert submitted == []
        assert store.get_session(session_id).audio_path == original_path
        assert Path(original_path).is_file()
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_interrupted_due_low_viewers_never_deletes_partial_media(tmp_path, monkeypatch):
    from test_runtime import _make_due_show

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        runtime._stop_all_recordings()
        session = store.get_session(session.session_id)
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        runtime._check_due_audience()
        runtime._reject_low_viewers(session, 1, "1")
        assert store.get_session(session.session_id).status == "INTERRUPTED"
        assert Path(session.video_path).exists() and Path(session.audio_path).exists()
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_audience_due_mismatch_never_deletes_media(tmp_path, monkeypatch):
    from test_runtime import _make_due_show

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        # A later global edit is no longer a mismatch: corrupt the saved
        # policy instead to exercise the actual persisted-integrity guard.
        store._update_session_artifacts(session.session_id, "qualification_minutes=?", (31,))
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.qualification_status == "viewer_check_failed"
        assert Path(current.video_path).exists() and Path(current.audio_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_changed_recording_page_never_deletes_media(tmp_path, monkeypatch):
    from test_runtime import _make_due_show

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        _recorder, page = runtime._recorders[session.session_id]
        page.url = "https://www.whatnot.com/show/another"
        runtime._browser.live_state = {"ended": False, "viewers": 1}
        runtime._check_due_audience()
        current = store.get_session(session.session_id)
        assert current.qualification_status == "viewer_check_failed"
        assert Path(current.video_path).exists() and Path(current.audio_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_same_show_redirect_is_accepted_but_other_show_and_domain_are_not(tmp_path, monkeypatch):
    from test_runtime import _make_due_show

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session = _make_due_show(runtime, store)
        active = runtime._recorders[session.session_id]
        page = active[1]
        page.url = "https://whatnot.com/live/due/?ref=abc"
        assert runtime._audience_capture_is_current(session, active)
        page.url = "https://www.whatnot.com/shows/another"
        assert not runtime._audience_capture_is_current(session, active)
        page.url = "https://example.com/live/due"
        assert not runtime._audience_capture_is_current(session, active)
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()
