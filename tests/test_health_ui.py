from pathlib import Path

from test_runtime import live_show, make_runtime
from whatnot_monitor.app import MonitorApp


def test_stalled_capture_is_visible_but_not_stopped_and_recovers(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {'A': [live_show('1')]})
    store.upsert_keyword('A')
    try:
        runtime.scan_once()
        session_id = next(iter(runtime._recorders))
        session = store.get_session(session_id)
        # Test the UI's running-state branch without starting browser threads.
        monkeypatch.setattr(type(runtime), 'running', property(lambda _self: True))
        app = object.__new__(MonitorApp)
        app.runtime = runtime
        assert app._recording_status_display(session) == '录制中'
        runtime._file_watchdog.timeout_seconds = 0
        runtime._file_watchdog.poll_once()
        runtime._file_watchdog.poll_once()
        assert '待核实' in app._recording_status_display(store.get_session(session_id))
        assert session_id in runtime._recorders
        assert store.get_session(session_id).status == 'RECORDING'
        for path in (session.audio_path, session.video_path):
            with Path(path).open('ab') as stream:
                stream.write(b'more')
        runtime._file_watchdog.poll_once()
        assert app._recording_status_display(store.get_session(session_id)) == '录制中'
        assert session_id in runtime._recorders
    finally:
        runtime._stop_all_recordings()
        runtime._postprocess.shutdown(wait=True)
        store.close()


def test_stale_warning_does_not_override_terminal_or_recovery_status(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {'A': [live_show('1')]})
    store.upsert_keyword('A')
    try:
        runtime.scan_once()
        session_id = next(iter(runtime._recorders))
        session = store.get_session(session_id)
        runtime._file_watchdog.timeout_seconds = 0
        runtime._file_watchdog.poll_once()
        runtime._file_watchdog.poll_once()
        assert runtime.recording_health_warning(session_id)
        app = object.__new__(MonitorApp)
        app.runtime = runtime
        monkeypatch.setattr(type(runtime), 'running', property(lambda _self: False))
        assert app._recording_status_display(session) == '待恢复核验'
        runtime._stop_all_recordings()
        assert app._recording_status_display(store.get_session(session_id)) == '已中断'
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()
