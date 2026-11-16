"""Lost-document cleanup never commands a challenge or waits for browser JS."""

import base64

import pytest
from whatnot_monitor.recording.browser_audio import (
    BrowserAudioCaptureError,
    BrowserAudioRecorder,
)
from whatnot_monitor.recording.browser_av import (
    BrowserAVCaptureError,
    BrowserAVRecorder,
)


class BindingPage:
    def __init__(self):
        self.bindings = {}
        self.evaluations = 0

    def expose_binding(self, name, callback):
        self.bindings[name] = callback

    def evaluate(self, expression, arg=None):
        self.evaluations += 1
        if self.evaluations > 1:
            raise AssertionError("Lost-document cleanup must not evaluate the new page")
        return {}


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_abort_keeps_partials_ignores_late_chunks_and_never_reads_browser(kind, tmp_path):
    page = BindingPage()
    audio = tmp_path / "audio.webm"
    if kind == "av":
        recorder = BrowserAVRecorder(page, tmp_path / "video.webm", audio)
    else:
        recorder = BrowserAudioRecorder(page, audio)
    recorder.start()
    prefix = b"partial recording bytes"
    for callback in page.bindings.values():
        callback(None, base64.b64encode(prefix).decode())

    recorder.abort_lost_document()
    recorder.abort_lost_document()  # Idempotent local cleanup.
    for callback in page.bindings.values():
        callback(None, base64.b64encode(b"late bytes").decode())
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="state was lost"):
        recorder.stop()

    assert page.evaluations == 1
    assert audio.read_bytes() == prefix
    outputs = recorder._files.values() if kind == "av" else [recorder._file]
    assert all(output.closed for output in outputs)
    if kind == "av":
        assert recorder.video_path.read_bytes() == prefix


def _recorder_with_snapshot(kind, tmp_path, monkeypatch):
    from whatnot_monitor.recording import browser_audio

    page = BindingPage()
    recorder = (BrowserAVRecorder(page, tmp_path / "video.webm", tmp_path / "audio.webm")
                if kind == "av" else BrowserAudioRecorder(page, tmp_path / "audio.webm"))
    recorder._start_metadata = {"documentToken": "document-a", "showId": "show-a"}
    snapshot = {
        "href": "https://www.whatnot.com/live/show-a?ref=card#player",
        "documentToken": "document-a", "present": True,
        "captureToken": recorder._capture_token, "captureDocumentToken": "document-a",
        "showId": "show-a", "recorderStates": ["recording"] * (2 if kind == "av" else 1),
        "errors": [False] * (2 if kind == "av" else 1),
        "requiredTracks": [["live"] for _ in range(3 if kind == "av" else 1)],
    }
    def read(_page, expression, arg):
        assert _page is page
        assert "captureStream" not in expression and "stop(" not in expression
        assert arg["av"] is (kind == "av")
        return dict(snapshot)
    monkeypatch.setattr(browser_audio, "bounded_evaluate", read)
    return recorder, snapshot


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_health_checks_show_document_instance_and_required_tracks(kind, tmp_path, monkeypatch):
    recorder, snapshot = _recorder_with_snapshot(kind, tmp_path, monkeypatch)
    assert recorder.capture_health()["status"] == "active"
    snapshot["href"] = "https://whatnot.com/shows/SHOW-A?other=1"
    assert recorder.capture_health()["status"] == "active"
    snapshot["href"] = "https://www.whatnot.com/live/show-b"
    assert recorder.capture_health()["status"] == "drifted"
    snapshot["href"] = "https://www.whatnot.com/live/show-a"
    snapshot["captureToken"] = "newer-capture"
    assert recorder.capture_health()["status"] == "replaced"
    snapshot["captureToken"] = recorder._capture_token
    snapshot["documentToken"] = "document-b"
    assert recorder.capture_health()["status"] == "lost"


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_recording_state_with_ended_or_missing_tracks_is_not_active(kind, tmp_path, monkeypatch):
    recorder, snapshot = _recorder_with_snapshot(kind, tmp_path, monkeypatch)
    snapshot["requiredTracks"][0] = ["ended"]
    assert recorder.capture_health()["status"] == "terminated"
    snapshot["requiredTracks"][0] = []
    assert recorder.capture_health()["status"] == "terminated"
    snapshot["requiredTracks"][0] = ["live"]
    snapshot["recorderStates"][0] = "inactive"
    assert recorder.capture_health()["status"] == "terminated"
    snapshot["recorderStates"][0] = "recording"
    snapshot["errors"][0] = True
    assert recorder.capture_health()["status"] == "terminated"


@pytest.mark.parametrize("kind", ["audio", "av"])
@pytest.mark.parametrize("missing", ["href", "showId", "captureToken", "captureDocumentToken"])
def test_missing_capture_identity_is_unknown_not_active_or_lost(kind, missing, tmp_path, monkeypatch):
    recorder, snapshot = _recorder_with_snapshot(kind, tmp_path, monkeypatch)
    snapshot.pop(missing)
    assert recorder.capture_health()["status"] == "unknown"


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_finalization_requires_same_show_document_and_no_newer_capture(kind, tmp_path, monkeypatch):
    recorder, snapshot = _recorder_with_snapshot(kind, tmp_path, monkeypatch)
    snapshot["present"] = False
    assert recorder.capture_health()["status"] == "lost"
    assert recorder.finalization_identity()["status"] == "finalized"
    snapshot["href"] = "https://whatnot.com/live/show-b"
    assert recorder.finalization_identity()["status"] == "drifted"
    snapshot["href"] = "https://whatnot.com/live/show-a"
    snapshot["present"] = True
    snapshot["captureToken"] = "newer"
    assert recorder.finalization_identity()["status"] == "replaced"


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_stop_is_token_scoped_and_failed_stop_quarantines_late_chunks(kind, tmp_path):
    class ScopedPage(BindingPage):
        def evaluate(self, expression, arg=None):
            self.evaluations += 1
            if self.evaluations == 1:
                assert arg["expectedShowId"] == "show-a"
                self.token = arg["captureToken"]
                return {"documentToken": "document-a", "showId": "show-a"}
            assert arg == {"captureToken": self.token, "documentToken": "document-a"}
            assert expression.index("requireOwnedCapture(state);") < expression.index("await stopRecorderStates(")
            assert "=== state" in expression  # Never delete a newer global after await.
            raise RuntimeError("Capture ownership changed; replacement preserved")

    page = ScopedPage()
    recorder = (BrowserAVRecorder(page, tmp_path / "video.webm", tmp_path / "audio.webm")
                if kind == "av" else BrowserAudioRecorder(page, tmp_path / "audio.webm"))
    recorder.expected_show_id = "show-a"
    recorder.start()
    for callback in page.bindings.values():
        callback(None, base64.b64encode(b"prefix").decode())
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="ownership changed"):
        recorder.stop()
    for callback in page.bindings.values():
        callback(None, base64.b64encode(b"late").decode())
    assert (tmp_path / "audio.webm").read_bytes() == b"prefix"
    assert page.evaluations == 2
