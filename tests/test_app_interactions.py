from types import SimpleNamespace

from whatnot_monitor.app import MonitorApp
from whatnot_monitor.config import Settings


def test_login_is_nonmodal_and_does_not_claim_browser_opened(monkeypatch):
    calls = []
    runtime = SimpleNamespace(
        running=True, paused=True, scan_status={"state": "opening_login"},
        open_login=lambda: calls.append("requested"),
    )
    app = object.__new__(MonitorApp)
    app.runtime = runtime
    app.status_var = SimpleNamespace(set=lambda text: calls.append(text))
    app.refresh = lambda: calls.append("refresh")

    def unexpected_dialog(*_args, **_kwargs):
        raise AssertionError("Async login must not show a premature success dialog")

    monkeypatch.setattr("whatnot_monitor.app.messagebox.showinfo", unexpected_dialog)
    app.login()
    assert calls == ["requested", "正在打开 Whatnot 页面", "refresh"]


def test_dashboard_keeps_existing_row_selection_and_drops_filtered_rows():
    selected = []
    tree = SimpleNamespace(
        exists=lambda item: item in {"show-one", "show-two"},
        selection_set=lambda items: selected.extend(items),
    )
    MonitorApp._restore_tree_selection(tree, ("show-one", "removed-show"))
    assert selected == ["show-one"]


def test_clear_history_cancel_has_no_side_effects(monkeypatch):
    app = object.__new__(MonitorApp)
    calls = []
    app.runtime = SimpleNamespace(request_clear_history=lambda: calls.append("delete"))
    app.clear_room_filters = lambda: calls.append("reset")
    monkeypatch.setattr("whatnot_monitor.app.messagebox.askyesno", lambda *_: False)
    app.clear_room_history()
    assert calls == []


def test_clear_history_confirms_scope_and_resets_filters_after_success(monkeypatch):
    app = object.__new__(MonitorApp)
    calls = []
    confirmations = []
    app.runtime = SimpleNamespace(request_clear_history=lambda: {"deleted": 5, "retained": 2, "failures": {}})
    app.clear_room_filters = lambda: calls.append("reset")
    monkeypatch.setattr("whatnot_monitor.app.messagebox.askyesno", lambda title, text: confirmations.append(text) or True)
    monkeypatch.setattr("whatnot_monitor.app.messagebox.showinfo", lambda *args: calls.append(args))
    app.clear_room_history()
    assert "不受当前筛选" in confirmations[0]
    assert "删除不可撤销" in confirmations[0]
    assert calls[0] == "reset"
    assert "5" in calls[1][1] and "2" in calls[1][1]


def test_clear_history_surfaces_runtime_guard_without_resetting_filters(monkeypatch):
    app = object.__new__(MonitorApp)
    calls = []

    def reject():
        raise RuntimeError("后台尚未结束")

    app.runtime = SimpleNamespace(request_clear_history=reject)
    app.clear_room_filters = lambda: calls.append("reset")
    monkeypatch.setattr("whatnot_monitor.app.messagebox.askyesno", lambda *_: True)
    monkeypatch.setattr("whatnot_monitor.app.messagebox.showerror", lambda *args: calls.append(args))
    app.clear_room_history()
    assert calls == [("清除未完成", "后台尚未结束")]


def test_audience_column_uses_persisted_snapshot_preserves_zero_and_honors_configuration():
    app = object.__new__(MonitorApp)
    app.settings = Settings()
    assert app._audience_column_title() == "核验在线人数（30分钟）"
    for value in (0, 99, 100):
        assert app._audience_display(SimpleNamespace(audience_value=value, audience_basis=None, current_viewers=9999)) == str(value)
    assert app._audience_display(SimpleNamespace(audience_value=None, audience_basis=None, current_viewers=9999)) == "—"
    app.settings.qualification_minutes = 15
    assert app._audience_column_title() == "核验在线人数（15分钟）"


def test_late_audience_snapshot_is_labeled_and_keeps_zero():
    snapshot = SimpleNamespace(audience_value=0, audience_basis="recording_start_estimate", audience_outcome="captured_late", current_viewers=9999)
    rendered = MonitorApp._audience_display(snapshot)
    assert rendered.startswith("0")
    assert "延迟采样" in rendered and "非30分钟" in rendered
    assert "录制起算估算" in rendered
    assert "9999" not in rendered


def test_overdue_verification_block_is_display_only_and_recovers():
    from datetime import UTC, datetime, timedelta
    app = object.__new__(MonitorApp)
    app.settings = Settings()
    app.runtime = SimpleNamespace(running=True, scan_status={"state": "verification_required"})
    now = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    session = SimpleNamespace(status="RECORDING", qualification_status="pending", audience_outcome=None,
                              audience_due_at=(now - timedelta(seconds=1)).isoformat(), audience_basis="platform_start",
                              start_time_source="platform_started_at", started_at=(now - timedelta(minutes=31)).isoformat(), recording_started_at=None)
    assert app._qualification_status_display(session, now=now) == "验证阻断，已逾期待补判"
    assert session.qualification_status == "pending" and session.audience_outcome is None
    app.runtime.scan_status["state"] = "scanning"
    assert app._qualification_status_display(session, now=now) == "待核验"
    app.runtime.scan_status["state"] = "page_state_unknown"
    assert app._qualification_status_display(session, now=now) == "页面状态未知，已逾期待补判"
    session.started_at = (now - timedelta(minutes=29)).isoformat()
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.started_at = "invalid"
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.status = "WAITING"
    session.started_at = (now - timedelta(minutes=31)).isoformat()
    assert app._qualification_status_display(session, now=now) == "待核验"


def test_blocked_unpersisted_estimated_schedule_uses_recording_start():
    from datetime import UTC, datetime, timedelta
    app = object.__new__(MonitorApp)
    app.settings = Settings()
    app.runtime = SimpleNamespace(running=True, scan_status={"state": "verification_required"})
    now = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    session = SimpleNamespace(status="RECORDING", qualification_status="pending", audience_outcome=None,
                              audience_due_at=None, audience_basis=None, start_time_source="first_seen_estimate",
                              recording_started_at=(now - timedelta(minutes=31)).isoformat())
    assert app._qualification_status_display(session, now=now) == "验证阻断，已逾期待补判"
    session.recording_started_at = (now - timedelta(minutes=29)).isoformat()
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.recording_started_at = "2026-11-14T07:00:00"
    assert app._qualification_status_display(session, now=now) == "待核验"


def test_legacy_first_seen_due_does_not_override_actual_recording_start():
    from datetime import UTC, datetime, timedelta
    app = object.__new__(MonitorApp)
    app.settings = Settings()
    app.runtime = SimpleNamespace(running=True, scan_status={"state": "verification_required"})
    now = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    session = SimpleNamespace(status="RECORDING", qualification_status="pending", audience_outcome=None,
                              audience_due_at=(now - timedelta(minutes=90)).isoformat(), audience_basis=None,
                              start_time_source="first_seen_estimate",
                              recording_started_at=(now - timedelta(minutes=1)).isoformat())
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.recording_started_at = None
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.recording_started_at = (now - timedelta(minutes=31)).isoformat()
    assert app._qualification_status_display(session, now=now) == "验证阻断，已逾期待补判"


def test_captured_results_are_never_labeled_as_needing_recheck():
    from datetime import UTC, datetime, timedelta
    app = object.__new__(MonitorApp)
    app.settings = Settings()
    app.runtime = SimpleNamespace(running=True, scan_status={"state": "verification_required"})
    now = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    session = SimpleNamespace(status="RECORDING", qualification_status="pending", audience_outcome="captured_late",
                              audience_due_at=(now - timedelta(hours=1)).isoformat(), audience_basis="recording_start_estimate",
                              start_time_source="first_seen_estimate", recording_started_at=(now - timedelta(hours=2)).isoformat())
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.audience_outcome = "captured"
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.qualification_status = "viewer_check_failed"
    session.audience_outcome = "missed"
    assert app._qualification_status_display(session, now=now) == "验证阻断，已逾期待补判"


def test_unknown_legacy_source_does_not_trust_discovery_schedule():
    from datetime import UTC, datetime, timedelta
    app = object.__new__(MonitorApp)
    app.settings = Settings()
    app.runtime = SimpleNamespace(running=True, scan_status={"state": "verification_required"})
    now = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    session = SimpleNamespace(status="RECORDING", qualification_status="pending", audience_outcome=None,
                              audience_due_at=(now - timedelta(hours=1)).isoformat(), audience_basis=None,
                              start_time_source="", started_at=(now - timedelta(hours=2)).isoformat(),
                              recording_started_at=(now - timedelta(minutes=1)).isoformat())
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.recording_started_at = None
    assert app._qualification_status_display(session, now=now) == "待核验"
    session.start_time_source = "platform_started_at"
    assert app._qualification_status_display(session, now=now) == "验证阻断，已逾期待补判"


def test_user_confirmed_cleanup_displays_unknown_count_and_current_truth():
    app = object.__new__(MonitorApp)
    app.runtime = SimpleNamespace(running=False)
    session = SimpleNamespace(audience_value=42, current_viewers=33, audience_basis="recording_start_estimate", audience_outcome="manual_confirmed_low", qualification_status="rejected_low_viewers", error="旧事件：尚未低人数处理")
    assert app._audience_display(session) == "用户确认（具体人数未知）"
    assert app._qualification_status_display(session) == "用户确认已清理（人数未知）"
    assert "用户确认已清理" in app._session_error_display(session)
    assert "具体人数未知" in app._session_error_display(session)
    assert session.error == "旧事件：尚未低人数处理"
    session.audience_outcome = "captured_late"
    assert app._session_error_display(session) == session.error


def test_manual_confirmation_does_not_claim_old_basis_or_sample_time():
    old = "2026-11-14T00:10:00+00:00"
    session = SimpleNamespace(audience_outcome="manual_confirmed_low", audience_basis="recording_start_estimate", audience_captured_at=old)
    basis, when = MonitorApp._audience_evidence_display(session)
    assert basis == "用户人工确认，未采集人数"
    assert when == "人工确认时间未在本字段记录" and old not in when
    session.audience_outcome = "captured_late"
    basis, when = MonitorApp._audience_evidence_display(session)
    assert basis == "录制开始起算（估算）" and when == old
