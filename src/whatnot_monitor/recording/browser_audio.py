"""Capture the audio track rendered by one browser page to an isolated file."""

from __future__ import annotations

import base64
import binascii
import threading
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from ..bounded_browser_read import bounded_evaluate
from ..live_identity import ROOM_IDENTITY_JS, live_room_identity
from .media_sources import CAPTURE_SOURCES_JS

# Shared with the A/V recorder. A write failure must not settle the stop-event
# promise: final dataavailable events can still enqueue writes after that failure.
_RECORDER_HELPERS_JS = """
    const makeRecorderState = (recorder, bindingName, stream) => {
        const state = {
            recorder, stream, started: false, error: null, trackEnded: false,
            writeQueue: Promise.resolve(), stopped: null
        };
        const rememberError = (error) => { state.error ||= error; };
        state.stopped = new Promise((resolve) => {
            recorder.addEventListener('stop', resolve, {once: true});
        });
        recorder.addEventListener('dataavailable', (event) => {
            if (!event.data || event.data.size === 0) return;
            const blob = event.data;
            // Queue the Blob before reading it so slow arrayBuffer conversions
            // cannot reorder MediaRecorder chunks (including the WebM header).
            state.writeQueue = state.writeQueue.then(async () => {
                const bytes = new Uint8Array(await blob.arrayBuffer());
                let binary = '';
                const blockSize = 0x8000;
                for (let offset = 0; offset < bytes.length; offset += blockSize) {
                    binary += String.fromCharCode(...bytes.subarray(offset, offset + blockSize));
                }
                await window[bindingName](btoa(binary));
            }).catch(rememberError);
        });
        recorder.addEventListener('error', (event) => rememberError(
            new Error(event.error?.message || 'MediaRecorder failed')
        ));
        for (const track of stream.getTracks()) {
            track.addEventListener('ended', () => { state.trackEnded = true; }, {once: true});
        }
        return state;
    };
    const stopCaptureTracks = (streams) => {
        let firstError = null;
        const tracks = new Set(streams.filter(Boolean).flatMap((stream) => stream.getTracks()));
        for (const track of tracks) {
            try { track.stop(); } catch (error) { firstError ||= error; }
        }
        return firstError;
    };
    const stopRecorderStates = async (states) => {
        const started = states.filter((state) => state.started);
        for (const state of started) {
            if (state.recorder.state !== 'inactive') {
                try {
                    state.recorder.stop();
                } catch (error) {
                    state.error ||= error;
                    // Ending its inputs also asks a live recorder to finalize.
                    stopCaptureTracks([state.stream]);
                }
            }
        }
        // An inactive recorder may still have its final data/stop events queued.
        // Never wait for a recorder whose start() threw without starting it.
        await Promise.all(started.map(async (state) => {
            await state.stopped;
            await state.writeQueue;
        }));
        const failed = states.find((state) => state.error);
        if (failed) throw failed.error;
    };
"""


# No media creation/mutation in this snapshot. A SPA can retain a stale global.
_CAPTURE_HEALTH_JS = """({globalName, av}) => {
    const state = window[globalName];
    const tracks = (stream, kind) => stream?.getTracks?.()
        .filter(track => track.kind === kind).map(track => track.readyState) || [];
    const states = state ? (av ? [state.videoState, state.audioState] : [state]) : [];
    return {
        href: location.href,
        documentToken: window.__whatnotCaptureDocumentToken || null,
        present: Boolean(state),
        captureToken: state?.captureToken || null,
        captureDocumentToken: state?.documentToken || null,
        showId: state?.showId || null,
        recorderStates: states.map(item => item?.recorder?.state || null),
        errors: states.map(item => Boolean(item?.error)),
        requiredTracks: !state ? [] : av ? [
            tracks(state.videoState?.stream, 'video'),
            tracks(state.videoState?.stream, 'audio'),
            tracks(state.audioState?.stream, 'audio')
        ] : [tracks(state.stream, 'audio')]
    };
}"""

_CAPTURE_IDENTITY_JS = ROOM_IDENTITY_JS + r"""
    const currentShowId = () => showId(new URL(location.href));
    const identifyCapture = (state) => {
        if (!window.__whatnotCaptureDocumentToken) {
            Object.defineProperty(window, '__whatnotCaptureDocumentToken', {
                value: captureToken, configurable: false, writable: false
            });
        }
        Object.defineProperties(state, {
            captureToken: {value: captureToken, enumerable: true},
            documentToken: {value: window.__whatnotCaptureDocumentToken, enumerable: true},
            showId: {value: currentShowId(), enumerable: true}
        });
    };
    const requireOwnedCapture = (state) => {
        if (!state || state.captureToken !== captureToken ||
            state.documentToken !== documentToken ||
            window.__whatnotCaptureDocumentToken !== documentToken) {
            throw new Error('Capture ownership changed; replacement preserved');
        }
        if (state.showId && currentShowId() !== state.showId) {
            throw new Error('Capture show identity changed; replacement preserved');
        }
    };
"""


def _capture_health(
    recorder, global_name: str, *, av: bool, finalized: bool = False,
) -> dict[str, Any]:
    """Classify only confirmed ownership/track evidence; missing identity is unknown."""
    snapshot = bounded_evaluate(
        recorder.page, _CAPTURE_HEALTH_JS, {"globalName": global_name, "av": av},
    )
    if not isinstance(snapshot, dict):
        return {"status": "unknown"}
    health = {**snapshot, "status": "unknown"}
    metadata = recorder._start_metadata
    document_token = metadata.get("documentToken")
    if not document_token:
        return health
    if snapshot.get("documentToken") != document_token:
        health["status"] = "lost"
        return health
    present = snapshot.get("present")
    if present is False and not finalized:
        health["status"] = "lost"
        return health
    if present is not False:
        if present is not True or not snapshot.get("captureToken"):
            return health
        if snapshot.get("captureToken") != recorder._capture_token:
            health["status"] = "replaced"
            return health
        if snapshot.get("captureDocumentToken") != document_token:
            return health
    show_id = metadata.get("showId")
    if not show_id or (present is not False and snapshot.get("showId") != show_id):
        return health
    current_id = live_room_identity(snapshot.get("href") or "")
    if current_id is None:
        return health
    if current_id != show_id:
        health["status"] = "drifted"
        return health
    if finalized and present is False:
        health["status"] = "finalized"
        return health
    groups = snapshot.get("requiredTracks", [])
    states = snapshot.get("recorderStates", [])
    errors = snapshot.get("errors", [])
    if (not isinstance(groups, list) or not all(isinstance(group, list) for group in groups)
            or not isinstance(states, list) or not isinstance(errors, list)
            or len(groups) != (3 if av else 1) or len(states) != (2 if av else 1)
            or len(errors) != len(states)):
        return health
    if any(state not in {"recording", "paused", "inactive"} for state in states):
        return health
    if any(track not in {"live", "ended"} for group in groups for track in group):
        return health
    # Recorder.state can remain 'recording' after its captured inputs end.
    health["status"] = "active" if (
        all(group and all(track == "live" for track in group) for group in groups)
        and all(state == "recording" for state in states)
        and not any(snapshot.get("errors", []))
    ) else "terminated"
    return health


class BrowserAudioCaptureError(RuntimeError):
    """Raised when page-local media capture cannot start or finish safely."""


class BrowserAudioRecorder:
    """Record audible media elements on a single Playwright page.

    Each instance owns one output file and one MediaRecorder. HTML media audio
    is preferred; otherwise a clone of the same page's received WebRTC audio is
    used. It never accesses a system audio device or a network media URL.
    """

    def __init__(self, page: Page, output_path: str | Path) -> None:
        self.page = page
        self.output_path = Path(output_path)
        self._file: Any = None
        self._write_lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._aborted = False
        self._stop_error: BrowserAudioCaptureError | None = None
        self._binding_name = f"__whatnot_audio_chunk_{id(self):x}"
        self._capture_token = uuid.uuid4().hex
        self.expected_show_id: str | None = None
        self._start_metadata: dict[str, Any] = {}

    def start(self, *, timeslice_ms: int = 1000) -> dict[str, Any]:
        """Start capture from the page's currently playing media element."""
        if self._started:
            raise BrowserAudioCaptureError("Capture has already been started")
        if timeslice_ms < 100:
            raise ValueError("timeslice_ms must be at least 100")

        def write_chunk(_source: Any, encoded: str) -> None:
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise BrowserAudioCaptureError(
                    "Browser sent an invalid audio chunk"
                ) from exc
            with self._write_lock:
                if self._aborted:
                    return  # Ignore late bindings from the lost document.
                self._file.write(chunk)
                self._file.flush()

        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.output_path.open("wb")
            self.page.expose_binding(self._binding_name, write_chunk)
            result = self.page.evaluate(
                """async ({bindingName, timesliceMs, captureToken, expectedShowId}) => {"""
                + _RECORDER_HELPERS_JS
                + _CAPTURE_IDENTITY_JS
                + CAPTURE_SOURCES_JS
                + """
                    if (expectedShowId && currentShowId() !== expectedShowId) {
                        throw new Error('Requested show identity changed before capture start');
                    }
                    if (window.__whatnotAudioCapture) {
                        throw new Error('An existing capture is present; refusing to replace it');
                    }
                    if (typeof MediaRecorder === 'undefined') {
                        throw new Error('MediaRecorder is unavailable in this browser');
                    }
                    const elements = [...document.querySelectorAll('audio, video')];
                    const candidates = elements.filter((element) =>
                        !element.paused && !element.ended && element.readyState >= 2
                    );
                    const capture = HTMLMediaElement.prototype.captureStream
                        || HTMLMediaElement.prototype.mozCaptureStream;
                    if (typeof capture !== 'function') {
                        throw new Error('HTMLMediaElement.captureStream is unavailable');
                    }

                    let selected = null;
                    let captured = null;
                    const sourceStreams = [];
                    const states = [];
                    try {
                        const sources = captureRoomSources(candidates, capture);
                        sourceStreams.push(...sources.map(({stream}) => stream));
                        for (const {element, stream} of sources) {
                            const tracks = stream.getAudioTracks().filter((track) => track.readyState === 'live');
                            if (tracks.length) {
                                selected = element;
                                captured = new MediaStream(tracks);
                                break;
                            }
                        }
                        if (!captured) {
                            throw new Error('No live audio track could be captured from playing media');
                        }

                        const preferred = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm'];
                        const mimeType = preferred.find((type) => MediaRecorder.isTypeSupported(type));
                        if (!mimeType) {
                            throw new Error('No supported audio-only MediaRecorder format is available');
                        }

                        const recorder = new MediaRecorder(captured, {mimeType});
                        const state = makeRecorderState(recorder, bindingName, captured);
                        Object.assign(state, {captured, selected, mimeType, sourceStreams});
                        identifyCapture(state);
                        states.push(state);
                        window.__whatnotAudioCapture = state;
                        recorder.start(timesliceMs);
                        state.started = true;
                        return {mimeType, elementTag: selected.tagName.toLowerCase(),
                            audioTracks: captured.getAudioTracks().length,
                            documentToken: state.documentToken, showId: state.showId};
                    } catch (error) {
                        try { await stopRecorderStates(states); } catch (_) {
                            // Preserve the original start failure after draining started recorders.
                        } finally {
                            stopCaptureTracks([...sourceStreams, captured]);
                            if (window.__whatnotAudioCapture?.captureToken === captureToken) {
                                delete window.__whatnotAudioCapture;
                            }
                        }
                        throw error;
                    }
                }""",
                {"bindingName": self._binding_name, "timesliceMs": timeslice_ms,
                 "captureToken": self._capture_token, "expectedShowId": self.expected_show_id},
            )
        except Exception as exc:
            self._aborted = True
            self._close_file()
            raise BrowserAudioCaptureError(
                f"Could not start page audio capture: {exc}"
            ) from exc

        self._started = True
        self._start_metadata = dict(result)
        return result

    def capture_health(self) -> dict[str, Any]:
        return _capture_health(self, "__whatnotAudioCapture", av=False)

    def finalization_identity(self) -> dict[str, Any]:
        return _capture_health(self, "__whatnotAudioCapture", av=False, finalized=True)

    def has_capture_state(self) -> bool:
        """Detect document replacement without navigating or restarting capture."""
        return bool(bounded_evaluate(self.page, "() => Boolean(window.__whatnotAudioCapture)"))

    def abort_lost_document(self) -> None:
        """Close local partials only after capture-document loss is confirmed."""
        self.abort_stale_capture()

    def abort_stale_capture(self) -> None:
        """Quarantine original partials locally; never command a replacement page."""
        if self._stopped:
            return
        self._aborted = True
        failure = self._close_file()
        self._stopped = True
        self._stop_error = BrowserAudioCaptureError(
            "Page capture state was lost; local partial file retained"
        )
        if failure is not None:
            raise self._stop_error from failure

    def stop(self) -> Path:
        """Stop capture, flush the final chunk, and return the file path."""
        if not self._started:
            raise BrowserAudioCaptureError("Capture has not been started")
        if self._stopped:
            if self._stop_error is not None:
                raise self._stop_error
            return self.output_path

        failure: Exception | None = None
        try:
            self.page.evaluate(
                """async ({captureToken, documentToken}) => {"""
                + _RECORDER_HELPERS_JS
                + _CAPTURE_IDENTITY_JS
                + """
                    const state = window.__whatnotAudioCapture;
                    if (!state) throw new Error('Page capture state was lost');
                    requireOwnedCapture(state);
                    let failure = null;
                    try {
                        await stopRecorderStates([state]);
                    } catch (error) {
                        failure = error;
                    } finally {
                        const cleanupError = stopCaptureTracks([...state.sourceStreams, state.captured]);
                        failure ||= cleanupError;
                        if (window.__whatnotAudioCapture === state) {
                            delete window.__whatnotAudioCapture;
                        }
                    }
                    if (failure) throw failure;
                    if (state.showId && currentShowId() !== state.showId) {
                        throw new Error('Show identity changed while stopping; partial retained');
                    }
                }""",
                {"captureToken": self._capture_token,
                 "documentToken": self._start_metadata.get("documentToken")},
            )
            with self._write_lock:
                self._file.flush()
        except Exception as exc:  # noqa: BLE001 - preserve the primary failure through cleanup
            failure = exc
        finally:
            self._aborted = True  # No late delivery may write to a closed output.
            close_error = self._close_file()
            if failure is None:
                failure = close_error
            self._stopped = True
        if failure is not None:
            self._stop_error = BrowserAudioCaptureError(
                f"Could not stop page audio capture cleanly: {failure}"
            )
            raise self._stop_error from failure
        return self.output_path

    def _close_file(self) -> Exception | None:
        """Attempt close without masking an earlier capture/write failure."""
        with self._write_lock:
            if self._file is not None and not self._file.closed:
                try:
                    self._file.close()
                except Exception as exc:  # noqa: BLE001 - return cleanup failure without masking capture error
                    return exc
        return None
