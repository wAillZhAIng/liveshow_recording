from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_runtime import live_show, make_runtime
from whatnot_monitor.store import Store
from whatnot_monitor.whatnot import WhatnotBrowser


def test_recording_clock_and_policy_survive_settings_edits(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, qualification_minutes=10, minimum_viewers=80)
    try:
        show = replace(live_show("clock"), started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                       start_time_source="platform_started_at")
        session, _ = runtime._persist_discovered_show(show, ("all",), {"all": store.upsert_keyword("all")})
        assert session.audience_due_at is None
        runtime._start_recording(session)
        started = store.get_session(session.session_id)
        store.save_recording_started(session.session_id, (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                                     qualification_minutes=1, minimum_viewers=999)
        assert store.get_session(session.session_id).recording_started_at == started.recording_started_at
        # Change settings before the first real media check: the start snapshot wins.
        runtime.settings = replace(runtime.settings, qualification_minutes=1, minimum_viewers=100)
        runtime._check_due_audience()
        saved = store.get_session(session.session_id)
        due = datetime.fromisoformat(saved.audience_due_at)
        assert due == datetime.fromisoformat(saved.recording_started_at) + timedelta(minutes=10)
        assert saved.audience_basis == "recording_start"
        assert saved.qualification_minutes == 10 and saved.minimum_viewers == 80
        assert saved.audience_attempts == 0
        assert runtime._audience_timing_error(saved, due.isoformat()) is None
        assert runtime._audience_timing_error(replace(saved, audience_due_at=(due + timedelta(minutes=1)).isoformat()), due.isoformat())
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        runtime._browser.live_state = {"live": True, "ended": False, "viewers": 81}
        runtime._check_due_audience()
        qualified = store.get_session(session.session_id)
        assert qualified.qualification_status == "qualified"
        assert qualified.audience_value == 81
        assert runtime._reported_30_minute_audience(qualified) is None
        # Duplicate persistence cannot reset the clock or the threshold.
        store.save_recording_started(session.session_id, (due + timedelta(hours=1)).isoformat(),
                                     qualification_minutes=1, minimum_viewers=999)
        assert store.get_session(session.session_id).recording_started_at == saved.recording_started_at
        assert store.get_session(session.session_id).minimum_viewers == 80
        reopened = Store(tmp_path / "monitor.sqlite3")
        try:
            durable = reopened.get_session(session.session_id)
            assert durable.qualification_minutes == 10 and durable.minimum_viewers == 80
            assert durable.audience_due_at == saved.audience_due_at
        finally:
            reopened.close()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_saved_eighty_viewer_threshold_rejects_seventy_nine(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, qualification_minutes=10, minimum_viewers=80)
    try:
        session, _ = runtime._persist_discovered_show(live_show("low"), ("all",), {"all": store.upsert_keyword("all")})
        runtime._start_recording(session)
        runtime._check_due_audience()
        saved = store.get_session(session.session_id)
        due = datetime.fromisoformat(saved.audience_due_at)
        # Lowering the default to zero cannot retroactively qualify this capture.
        runtime.settings = replace(runtime.settings, minimum_viewers=0)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(seconds=1))
        runtime._browser.live_state = {"live": True, "ended": False, "viewers": 79}
        runtime._check_due_audience()
        assert store.get_session(session.session_id) is None
        assert store.show_was_rejected(runtime._rejection_key(session.url))
        assert not Path(saved.audio_path).exists() and not Path(saved.video_path).exists()
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_search_checkpoint_services_captures_between_verification_checks():
    browser = object.__new__(WhatnotBrowser)
    calls = []
    browser._check_pending_verification = lambda: calls.append("verification")
    browser.service_recordings = lambda: calls.append("capture")
    browser._search_checkpoint()
    assert calls == ["verification", "capture", "verification"]


@pytest.mark.parametrize("basis", ["recording_start", "recording_start_estimate"])
def test_saved_ten_minute_sample_above_gate_uploads_observed_column(
    tmp_path, monkeypatch, basis,
):
    from whatnot_monitor import config
    from whatnot_monitor.integrations import feishu

    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    runtime.settings = replace(runtime.settings, qualification_minutes=10, minimum_viewers=80)
    sent = []
    queued = []

    class Client:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def create_record(self, fields, **_kwargs):
            sent.append(fields)
            return {"record_id": "mock-record-ten-eighty"}

    monkeypatch.setattr(config, "feishu_credentials", lambda _settings: ("id", "secret"))
    monkeypatch.setattr(feishu, "FeishuBitableClient", Client)
    monkeypatch.setattr(runtime, "_submit_postprocess", lambda *args: queued.append(args))
    try:
        anchor = datetime(2026, 11, 14, 12, tzinfo=UTC)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: anchor)
        show = live_show("mapping")
        if basis == "recording_start":
            show = replace(show, started_at=(anchor - timedelta(hours=2)).isoformat(),
                           start_time_source="platform_started_at")
        session, _ = runtime._persist_discovered_show(
            show, ("all",), {"all": store.upsert_keyword("all")},
        )
        runtime._start_recording(session)
        runtime._check_due_audience()
        due = anchor + timedelta(minutes=10)
        sampled = due + timedelta(seconds=1)
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: sampled)
        runtime._browser.live_state = {"live": True, "ended": False, "viewers": 81}
        runtime._check_due_audience()
        reopened = Store(tmp_path / "monitor.sqlite3")
        try:
            saved = reopened.get_session(session.session_id)
            assert saved.audience_basis == basis
            assert saved.qualification_minutes == 10 and saved.minimum_viewers == 80
            assert saved.audience_due_at == due.isoformat()
            assert saved.audience_captured_at == sampled.isoformat()
            assert saved.qualification_status == "qualified"
            assert runtime._reported_audience(saved) == 81
            assert runtime._reported_30_minute_audience(saved) is None
        finally:
            reopened.close()
        # Later defaults do not reinterpret this persisted sample or threshold.
        runtime.settings = replace(runtime.settings, qualification_minutes=30, minimum_viewers=100)
        runtime._upload(session.session_id, "mock full transcript")
        assert not sent  # Active media must not upload.
        recorder, page = runtime._recorders[session.session_id]
        monkeypatch.setattr("whatnot_monitor.runtime._now", lambda: due + timedelta(minutes=1))
        runtime._finish_recording(store.get_session(session.session_id), recorder, page)
        assert queued and queued[0][0] == runtime._transcribe_and_upload
        # Substitute a transcript only at the upload boundary: no ASR or remote calls.
        runtime._upload(session.session_id, "mock full transcript")
        assert len(sent) == 1
        assert sent[0]["在线人数"] == 81
        assert "开播30分钟在线人数" not in sent[0]
        text = sent[0]["直播讲解内容"]
        for expected in ("10分钟", basis, due.isoformat(), sampled.isoformat(),
                         "该场门槛：80人", "实际记录人数：81", "mock full transcript"):
            assert expected in text
        completed = store.get_session(session.session_id)
        assert completed.status == "COMPLETED"
        assert completed.feishu_record_id == "mock-record-ten-eighty"
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


@pytest.mark.parametrize("changes", [
    {"audience_outcome": "captured_late"},
    {"audience_outcome": "manual_confirmed_low"},
    {"audience_outcome": "unknown"},
    {"audience_outcome": None},
    {"audience_basis": "first_seen_estimate"},
    {"audience_basis": "manual"},
    {"audience_basis": None},
    {"audience_value": -1},
    {"audience_value": True},
    {"audience_value": False},
    {"audience_value": 80.0},
    {"audience_value": "80"},
    {"audience_value": None},
    {"qualification_minutes": None},
    {"qualification_minutes": True},
    {"qualification_minutes": 0},
    {"qualification_minutes": -10},
    {"qualification_minutes": 10.5},
    {"qualification_minutes": "10"},
    {"qualification_minutes": 30},
    {"audience_due_at": None},
    {"audience_due_at": "invalid"},
    {"audience_due_at": "2026-11-14T12:10:00"},
    {"recording_started_at": None},
    {"recording_started_at": "invalid"},
    {"recording_started_at": "2026-11-14T12:00:00"},
    {"audience_captured_at": None},
    {"audience_captured_at": "invalid"},
    {"audience_captured_at": "2026-11-14T12:10:01"},
    {"audience_captured_at": "2026-11-14T12:09:59+00:00"},
    {"audience_captured_at": "2026-11-14T12:12:01+00:00"},
    {"audience_basis": "platform_start", "started_at": "2026-11-14T12:00:00",
     "start_time_source": "platform_started_at"},
    {"audience_basis": "platform_start", "started_at": "2026-11-14T12:00:00+00:00",
     "start_time_source": "first_seen_estimate"},
])
def test_generic_audience_selector_omits_unverified_samples(tmp_path, monkeypatch, changes):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session, _ = runtime._persist_discovered_show(
            live_show("invalid-mapping"), ("all",), {"all": store.upsert_keyword("all")},
        )
        sample = replace(
            session, recording_started_at="2026-11-14T12:00:00+00:00",
            audience_basis="recording_start", qualification_minutes=10, minimum_viewers=80,
            audience_due_at="2026-11-14T12:10:00+00:00",
            audience_captured_at="2026-11-14T12:10:01+00:00",
            audience_outcome="captured", audience_value=80,
        )
        assert runtime._reported_audience(replace(sample, **changes)) is None
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


@pytest.mark.parametrize("basis, minutes", [
    ("recording_start", 10), ("recording_start_estimate", 10),
    ("platform_start", 10), ("platform_start", 30), ("platform_start", None),
])
@pytest.mark.parametrize("seconds, value", [(0, 0), (120, 80)])
def test_generic_audience_selector_accepts_verified_saved_boundaries(
    tmp_path, monkeypatch, basis, minutes, seconds, value,
):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        session, _ = runtime._persist_discovered_show(
            live_show("mapping-boundary"), ("all",), {"all": store.upsert_keyword("all")},
        )
        anchor = datetime(2026, 11, 14, 12, tzinfo=UTC)
        due = anchor + timedelta(minutes=minutes or 30)
        sample = replace(
            session, recording_started_at=anchor.isoformat(), started_at=anchor.isoformat(),
            start_time_source="platform_started_at", audience_basis=basis,
            qualification_minutes=minutes, audience_due_at=due.isoformat(),
            audience_captured_at=(due + timedelta(seconds=seconds)).isoformat(),
            audience_outcome="captured", audience_value=value,
        )
        assert runtime._reported_audience(sample) == value
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_capture_queue_prefers_oldest_discovery_not_latest_update(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    try:
        keyword = store.upsert_keyword("all")
        old, _ = runtime._persist_discovered_show(live_show("old", first_seen="2026-11-01T00:00:00+00:00"), ("all",), {"all": keyword})
        new, _ = runtime._persist_discovered_show(live_show("new", first_seen="2026-11-02T00:00:00+00:00"), ("all",), {"all": keyword})
        store.transition(new.session_id, "CAPACITY_INSUFFICIENT")
        assert [s.session_id for s in store.waiting_sessions(limit=1)] == [old.session_id]
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()


@pytest.mark.parametrize("viewers", [80, 85])
def test_early_end_closes_room_and_refills_after_a_verified_live_check(tmp_path, monkeypatch, viewers):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []}, capacity=1)
    runtime.settings = replace(runtime.settings, qualification_minutes=10, minimum_viewers=80)
    queued = []
    monkeypatch.setattr(runtime, "_submit_postprocess", lambda *args: queued.append(args))
    try:
        keyword = {"all": store.upsert_keyword("all")}
        first, _ = runtime._persist_discovered_show(live_show("first"), ("all",), keyword)
        next_show = live_show("next")
        second, _ = runtime._persist_discovered_show(next_show, ("all",), keyword)
        runtime._start_recording(first)
        runtime._start_waiting_sessions()
        assert store.get_session(second.session_id).status == "CAPACITY_INSUFFICIENT"
        recorder, page = runtime._recorders[first.session_id]
        closed = []
        page.close = lambda: closed.append(True)
        page.url = first.url
        recorder.capture_health = lambda: {}
        monkeypatch.setattr(
            runtime, "_recording_capture_health",
            lambda *_args, finalized=False: {"status": "finalized" if finalized else "active"},
        )
        runtime._browser.live_state = {"viewers": viewers, "viewers_raw": str(viewers)}
        runtime._monitor_recordings()
        original = store.get_session(first.session_id)
        runtime._browser.live_state = {"ended_candidate": True}
        for _ in range(3):
            runtime._monitor_recordings()
        assert closed == [True]
        assert first.session_id not in runtime._recorders
        result = store.get_session(first.session_id)
        if viewers == 80:
            assert result is None and queued == []
            assert not Path(original.audio_path).exists()
            assert not Path(original.video_path).exists()
            assert runtime._persist_discovered_show(live_show("first"), ("all",), keyword) is None
        else:
            assert result.status == "TRANSCRIBING"
            assert result.audience_outcome == "captured_at_end"
            assert runtime._reported_audience(result) == 85
            assert Path(original.audio_path).is_file() and Path(original.video_path).is_file()
            assert queued[0][1:] == (first.session_id, Path(original.audio_path))

        runtime._browser.live_state = {"live": True}
        runtime._start_waiting_sessions()
        assert store.get_session(second.session_id).status == "RECORDING"
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()
