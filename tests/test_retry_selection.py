from types import SimpleNamespace

import pytest

from whatnot_monitor.app import MonitorApp


def make_app(recording_selection, sessions):
    app = object.__new__(MonitorApp)
    app.root = object()
    app.recording_tree = SimpleNamespace(selection=lambda: recording_selection)
    # A stale overview selection must never be used by the recording-page button.
    app.session_tree = SimpleNamespace(selection=lambda: ('stale-overview',))
    app.store = SimpleNamespace(get_session=lambda session_id: sessions.get(session_id))
    calls = []
    app.runtime = SimpleNamespace(retry_session=lambda session_id: calls.append(session_id))
    app.refresh = lambda: None
    return app, calls


def test_retry_uses_recording_selection_not_stale_overview():
    app, calls = make_app(('selected-recording',), {'selected-recording': SimpleNamespace(status='FAILED')})
    app.retry_selected()
    assert calls == ['selected-recording']


@pytest.mark.parametrize('selection', [(), ('one', 'two')])
def test_retry_rejects_missing_or_ambiguous_recording_selection(monkeypatch, selection):
    warnings = []
    monkeypatch.setattr('whatnot_monitor.app.messagebox.showwarning', lambda *args, **kwargs: warnings.append(args))
    app, calls = make_app(selection, {})
    app.retry_selected()
    assert calls == []
    assert warnings


def test_retry_rejects_deleted_or_running_task(monkeypatch):
    warnings = []
    monkeypatch.setattr('whatnot_monitor.app.messagebox.showwarning', lambda *args, **kwargs: warnings.append(args))
    for sessions in ({}, {'selected': SimpleNamespace(status='RECORDING')}):
        app, calls = make_app(('selected',), sessions)
        app.retry_selected()
        assert calls == []
    assert len(warnings) == 2
