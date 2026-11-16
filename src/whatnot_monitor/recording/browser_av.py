"""Capture one page-local HTML media source to synchronized A/V and audio files."""

from __future__ import annotations

import base64
import binascii
import threading
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from ..bounded_browser_read import bounded_evaluate
from .browser_audio import _CAPTURE_IDENTITY_JS, _RECORDER_HELPERS_JS, _capture_health
from .media_sources import CAPTURE_SOURCES_JS


class BrowserAVCaptureError(RuntimeError):
    """Raised when independent page-local A/V capture cannot be completed."""


class BrowserAVRecorder:
    """Record one playing page's video and audio without using system loopback.

    Whatnot can expose separate HTML media elements or render received WebRTC
    audio via Web Audio. HTML audio is preferred, with a same-page receiver
    clone as fallback. The video WebM combines one live video and audio track;
    a second MediaRecorder writes an audio clone. Each instance owns two files.
    """

    def __init__(
        self,
        page: Page,
        video_path: str | Path,
        audio_path: str | Path,
        *,
        video_mime_type: str | None = None,
        video_bitrate_bps: int | None = None,
    ) -> None:
        self.page = page
        self.video_path = Path(video_path)
        self.audio_path = Path(audio_path)
        self.video_mime_type = (video_mime_type or "").strip()
        self.video_bitrate_bps = video_bitrate_bps
        self._files: dict[str, Any] = {}
        self._write_locks = {"video": threading.Lock(), "audio": threading.Lock()}
        self._started = False
        self._stopped = False
        self._aborted = False
        self._stop_error: BrowserAVCaptureError | None = None
        instance = id(self)
        self._binding_names = {
            "video": f"__whatnot_av_video_{instance:x}",
            "audio": f"__whatnot_av_audio_{instance:x}",
        }
        self._start_metadata: dict[str, Any] = {}
        self._capture_token = uuid.uuid4().hex
        self.expected_show_id: str | None = None

    def start(self, *, timeslice_ms: int = 1000) -> dict[str, Any]:
        """Start synchronized A/V and audio-only capture from the same player."""
        if self._started:
            raise BrowserAVCaptureError("Capture has already been started")
        if timeslice_ms < 100:
            raise ValueError("timeslice_ms must be at least 100")
        if self.video_bitrate_bps is not None and self.video_bitrate_bps <= 0:
            raise ValueError("video_bitrate_bps must be a positive integer")
        if self.video_path.resolve() == self.audio_path.resolve():
            raise ValueError("Video and audio outputs must be different files")

        self.video_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._files["video"] = self.video_path.open("wb")
            self._files["audio"] = self.audio_path.open("wb")
            for channel, binding_name in self._binding_names.items():

                def write_chunk(
                    _source: Any, encoded: str, *, output_channel: str = channel
                ) -> None:
                    try:
                        chunk = base64.b64decode(encoded, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise BrowserAVCaptureError(
                            f"Browser sent an invalid {output_channel} chunk"
                        ) from exc
                    with self._write_locks[output_channel]:
                        if self._aborted:
                            return  # Ignore late bindings from the lost document.
                        output = self._files[output_channel]
                        output.write(chunk)
                        output.flush()

                self.page.expose_binding(binding_name, write_chunk)

            metadata = self.page.evaluate(
                """async ({videoBinding, audioBinding, timesliceMs,
                            requestedVideoMimeType, requestedVideoBitrateBps,
                            captureToken, expectedShowId}) => {"""
                + _RECORDER_HELPERS_JS
                + _CAPTURE_IDENTITY_JS
                + CAPTURE_SOURCES_JS
                + """
                    if (expectedShowId && currentShowId() !== expectedShowId) {
                        throw new Error('Requested show identity changed before capture start');
                    }
                    if (window.__whatnotBrowserAVCapture) {
                        throw new Error('An existing capture is present; refusing to replace it');
                    }
                    if (typeof MediaRecorder === 'undefined') {
                        throw new Error('MediaRecorder is unavailable in this browser');
                    }
                    const mediaElements = [...document.querySelectorAll('audio, video')];
                    const elements = mediaElements.filter((element) =>
                        !element.paused && !element.ended && element.readyState >= 2
                    );
                    if (!elements.length) {
                        const state = mediaElements.map((element) => ({
                            tag: element.tagName.toLowerCase(),
                            paused: element.paused,
                            ended: element.ended,
                            readyState: element.readyState,
                            currentSrc: element.currentSrc || element.src || ''
                        }));
                        throw new Error(
                            `No playing audio/video element is ready on ${location.href} ` +
                            `(title: ${document.title}; media: ${JSON.stringify(state)})`
                        );
                    }
                    const capture = HTMLMediaElement.prototype.captureStream
                        || HTMLMediaElement.prototype.mozCaptureStream;
                    if (typeof capture !== 'function') {
                        throw new Error('HTMLMediaElement.captureStream is unavailable');
                    }

                    const sources = [];
                    const states = [];
                    let audioOnlyStream = null;
                    try {
                    sources.push(...captureRoomSources(elements, capture));
                    const liveTracks = (stream, kind) => stream.getTracks().filter(
                        (track) => track.kind === kind && track.readyState === 'live'
                    );
                    const combinedSource = sources.find(({stream}) =>
                        liveTracks(stream, 'video').length > 0
                        && liveTracks(stream, 'audio').length > 0
                    );
                    const videoSource = combinedSource || sources.find(({stream}) =>
                        liveTracks(stream, 'video').length > 0
                    );
                    const audioSource = combinedSource || sources.find(({stream}) =>
                        liveTracks(stream, 'audio').length > 0
                    );
                    if (!videoSource) throw new Error('No live video track could be captured from the page');
                    if (!audioSource) throw new Error('No live audio track could be captured from the page');
                    const videoTrack = liveTracks(videoSource.stream, 'video')[0];
                    const audioTrack = liveTracks(audioSource.stream, 'audio')[0];
                    const combinedStream = new MediaStream([videoTrack, audioTrack]);
                    audioOnlyStream = new MediaStream([audioTrack.clone()]);

                    const videoTypes = [
                        requestedVideoMimeType,
                        'video/webm;codecs=vp8,opus',
                        'video/webm;codecs=vp9,opus',
                        'video/webm;codecs=vp8',
                        'video/webm'
                    ].filter((type, index, values) =>
                        typeof type === 'string'
                        && type.toLowerCase().startsWith('video/webm')
                        && values.indexOf(type) === index
                    );
                    const audioTypes = [
                        'audio/webm;codecs=opus',
                        'audio/ogg;codecs=opus',
                        'audio/webm'
                    ];
                    const videoMimeType = videoTypes.find(
                        (type) => MediaRecorder.isTypeSupported(type)
                    );
                    const audioMimeType = audioTypes.find(
                        (type) => MediaRecorder.isTypeSupported(type)
                    );
                    if (!videoMimeType) {
                        throw new Error(
                            `No compatible WebM video format is supported` +
                            (requestedVideoMimeType
                                ? ` (requested: ${requestedVideoMimeType})`
                                : '')
                        );
                    }
                    if (!audioMimeType) {
                        throw new Error('No supported audio-only recording format is available');
                    }

                    const videoOptions = {mimeType: videoMimeType};
                    if (requestedVideoBitrateBps !== null) {
                        videoOptions.videoBitsPerSecond = requestedVideoBitrateBps;
                    }
                    const videoRecorder = new MediaRecorder(combinedStream, videoOptions);
                    const videoState = makeRecorderState(videoRecorder, videoBinding, combinedStream);
                    states.push(videoState);
                    const audioRecorder = new MediaRecorder(audioOnlyStream, {mimeType: audioMimeType});
                    const audioState = makeRecorderState(audioRecorder, audioBinding, audioOnlyStream);
                    states.push(audioState);
                    const startedAt = new Date().toISOString();
                    const startedMonotonicMs = performance.now();
                    const captureState = {
                        videoState, audioState, startedAt, startedMonotonicMs,
                        videoMimeType, audioMimeType, videoSource, audioSource,
                        sourceStreams: sources.map(({stream}) => stream),
                        requestedVideoMimeType, requestedVideoBitrateBps,
                        actualVideoBitsPerSecond: videoRecorder.videoBitsPerSecond
                    };
                    identifyCapture(captureState);
                    window.__whatnotBrowserAVCapture = captureState;
                    videoRecorder.start(timesliceMs);
                    videoState.started = true;
                    audioRecorder.start(timesliceMs);
                    audioState.started = true;
                    return {
                        startedAt, videoMimeType, audioMimeType,
                        documentToken: captureState.documentToken, showId: captureState.showId,
                        requestedVideoMimeType,
                        videoMimeTypeFallback: Boolean(
                            requestedVideoMimeType
                            && requestedVideoMimeType !== videoMimeType
                        ),
                        requestedVideoBitrateBps,
                        actualVideoBitsPerSecond: videoRecorder.videoBitsPerSecond,
                        videoTracks: combinedStream.getVideoTracks().length,
                        videoAudioTracks: combinedStream.getAudioTracks().length,
                        audioTracks: audioOnlyStream.getAudioTracks().length,
                        videoElement: videoSource.element.tagName.toLowerCase(),
                        audioElement: audioSource.element.tagName.toLowerCase()
                    };
                    } catch (error) {
                        try { await stopRecorderStates(states); } catch (_) {
                            // Preserve the original start failure after draining started recorders.
                        } finally {
                            stopCaptureTracks([...sources.map(({stream}) => stream), audioOnlyStream]);
                            if (window.__whatnotBrowserAVCapture?.captureToken === captureToken) {
                                delete window.__whatnotBrowserAVCapture;
                            }
                        }
                        throw error;
                    }
                }""",
                {
                    "expectedShowId": self.expected_show_id,
                    "captureToken": self._capture_token,
                    "videoBinding": self._binding_names["video"],
                    "audioBinding": self._binding_names["audio"],
                    "timesliceMs": timeslice_ms,
                    "requestedVideoMimeType": self.video_mime_type,
                    "requestedVideoBitrateBps": self.video_bitrate_bps,
                },
            )
        except Exception as exc:
            self._aborted = True
            self._close_files()
            raise BrowserAVCaptureError(
                f"Could not start page A/V capture: {exc}"
            ) from exc

        self._started = True
        self._start_metadata = metadata
        return dict(metadata)

    def capture_health(self) -> dict[str, Any]:
        return _capture_health(self, "__whatnotBrowserAVCapture", av=True)

    def finalization_identity(self) -> dict[str, Any]:
        return _capture_health(self, "__whatnotBrowserAVCapture", av=True, finalized=True)

    def has_capture_state(self) -> bool:
        """Detect document replacement without navigating or restarting capture."""
        return bool(bounded_evaluate(self.page, "() => Boolean(window.__whatnotBrowserAVCapture)"))

    def abort_lost_document(self) -> None:
        """Retain local partials without sending commands to a replaced document.

        Only use after the owner has confirmed that the capture document is gone.
        This is not a normal stop: no final WebM chunk or successful completion
        is claimed, and late binding deliveries cannot write into closed files.
        """
        self.abort_stale_capture()

    def abort_stale_capture(self) -> None:
        """Quarantine partials locally without mutating any replacement capture."""
        if self._stopped:
            return
        self._aborted = True
        failure = self._close_files()
        self._stopped = True
        self._stop_error = BrowserAVCaptureError(
            "Page A/V capture state was lost; local partial files retained"
        )
        if failure is not None:
            raise self._stop_error from failure

    def stop(self) -> dict[str, Any]:
        """Stop both recorders, flush final chunks, close files, and report outputs."""
        if not self._started:
            raise BrowserAVCaptureError("Capture has not been started")
        if self._stopped:
            if self._stop_error is not None:
                raise self._stop_error
            return dict(self._result_metadata)

        failure: Exception | None = None
        try:
            result = self.page.evaluate(
                """async ({captureToken, documentToken}) => {"""
                + _RECORDER_HELPERS_JS
                + _CAPTURE_IDENTITY_JS
                + """
                    const state = window.__whatnotBrowserAVCapture;
                    if (!state) throw new Error('Page A/V capture state was lost');
                    requireOwnedCapture(state);
                    const trackEndedUnexpectedly = [
                        ...state.videoState.stream.getTracks(),
                        ...state.audioState.stream.getTracks()
                    ].some((track) => track.readyState === 'ended');
                    let failure = null;
                    let output;
                    try {
                        await stopRecorderStates([state.videoState, state.audioState]);
                        const stoppedAt = new Date().toISOString();
                        const durationMs = Math.max(0, performance.now() - state.startedMonotonicMs);
                        output = {
                            startedAt: state.startedAt, stoppedAt, durationMs,
                            videoMimeType: state.videoMimeType,
                            audioMimeType: state.audioMimeType,
                            requestedVideoMimeType: state.requestedVideoMimeType,
                            requestedVideoBitrateBps: state.requestedVideoBitrateBps,
                            actualVideoBitsPerSecond: state.actualVideoBitsPerSecond,
                            videoTrackEnded: state.videoState.trackEnded,
                            audioTrackEnded: state.audioState.trackEnded,
                            trackEndedUnexpectedly
                        };
                    } catch (error) {
                        failure = error;
                    } finally {
                        const cleanupError = stopCaptureTracks([
                            ...state.sourceStreams, state.videoState.stream, state.audioState.stream
                        ]);
                        failure ||= cleanupError;
                        if (window.__whatnotBrowserAVCapture === state) {
                            delete window.__whatnotBrowserAVCapture;
                        }
                    }
                    if (failure) throw failure;
                    if (state.showId && currentShowId() !== state.showId) {
                        throw new Error('Show identity changed while stopping; partial retained');
                    }
                    return output;
                }""",
                {"captureToken": self._capture_token,
                 "documentToken": self._start_metadata.get("documentToken")},
            )
            for channel in self._files:
                with self._write_locks[channel]:
                    self._files[channel].flush()
        except Exception as exc:  # noqa: BLE001 - preserve the primary failure through cleanup
            failure = exc
        finally:
            self._aborted = True  # Quarantine late deliveries before closing outputs.
            close_error = self._close_files()
            if failure is None:
                failure = close_error
            self._stopped = True
        if failure is not None:
            self._stop_error = BrowserAVCaptureError(
                f"Could not stop page A/V capture cleanly: {failure}"
            )
            raise self._stop_error from failure

        self._result_metadata = {
            **result,
            **self._start_metadata,
            "startedAt": result["startedAt"],
            "stoppedAt": result["stoppedAt"],
            "durationMs": result["durationMs"],
            "videoPath": str(self.video_path),
            "audioPath": str(self.audio_path),
            "videoBytes": self.video_path.stat().st_size if self.video_path.exists() else 0,
            "audioBytes": self.audio_path.stat().st_size if self.audio_path.exists() else 0,
        }
        return dict(self._result_metadata)

    def _close_files(self) -> Exception | None:
        """Attempt every close, retaining the first cleanup failure for the caller."""
        first_error: Exception | None = None
        for channel, output in self._files.items():
            with self._write_locks[channel]:
                if output is not None and not output.closed:
                    try:
                        output.close()
                    except Exception as exc:  # noqa: BLE001 - close every output before reporting failure
                        if first_error is None:
                            first_error = exc
        return first_error
