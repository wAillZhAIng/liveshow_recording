"""Local Chrome regressions for recorder ordering, shutdown, and I/O failures.

Media uses FFmpeg-generated data URLs and an in-page WebRTC loopback pair.
No real site, application UI, persistent profile, or external media is used.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
from array import array
from typing import Any

import pytest
from playwright.sync_api import sync_playwright
from whatnot_monitor.recording.browser_audio import (
    BrowserAudioCaptureError,
    BrowserAudioRecorder,
)
from whatnot_monitor.recording.browser_av import (
    BrowserAVCaptureError,
    BrowserAVRecorder,
)
from whatnot_monitor.recording.media_sources import ROOM_CAPTURE_INIT_SCRIPT


class _DeadlinePage:
    """Bound evaluate so a regressed partial-start hang fails instead of hanging pytest."""

    def __init__(self, page: Any) -> None:
        self.page = page

    def expose_binding(self, *args: Any) -> None:
        self.page.expose_binding(*args)

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        return self.page.evaluate(
            """async ({expression, arg}) => {
                let timer;
                try {
                    return await Promise.race([
                        (0, eval)(`(${expression})`)(arg),
                        new Promise((_, reject) => {
                            timer = setTimeout(() => reject(new Error('regression evaluate deadline')), 5000);
                        })
                    ]);
                } finally { clearTimeout(timer); }
            }""",
            {"expression": expression, "arg": arg},
        )


@pytest.fixture(scope="module")
def local_media(tmp_path_factory):
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is required for local recording regression media")
    path = tmp_path_factory.mktemp("browser-regression-media") / "source.webm"
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
            "color=c=red:s=160x120:r=15", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=16000", "-t", "4",
            "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
            "-b:v", "180k", "-c:a", "libopus", "-b:a", "32k", str(path),
        ],
        check=True, capture_output=True, timeout=30,
    )
    return ffmpeg, "data:video/webm;base64," + base64.b64encode(path.read_bytes()).decode()


@pytest.fixture(scope="module")
def chrome():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        yield browser
        browser.close()


@pytest.mark.parametrize(
    "capture_global", ["__whatnotBrowserAVCapture", "__whatnotAudioCapture"]
)
def test_recording_tab_rejects_automatic_handoff_to_another_show(chrome, capture_global):
    context = chrome.new_context()
    try:
        context.route(
            "https://www.whatnot.com/**",
            lambda route: route.fulfill(
                content_type="text/html", body='<a href="/live/other">Other show</a>'
            ),
        )
        page = context.new_page()
        page.add_init_script(ROOM_CAPTURE_INIT_SCRIPT)
        source = "https://www.whatnot.com/live/original"
        page.goto(source)
        page.evaluate("(name) => { window[name] = {showId: 'original'}; }", capture_global)

        page.evaluate("location.assign('/live/other')")
        assert page.evaluate("location.href") == source
        page.evaluate("history.pushState(null, '', '/live/other')")
        assert page.evaluate("location.href") == source
        page.evaluate("history.replaceState(null, '', '/live/original?state=refresh')")
        assert page.evaluate("location.href") == f"{source}?state=refresh"

        page.get_by_role("link", name="Other show").click()
        assert page.url == "https://www.whatnot.com/live/other"
        page.evaluate("location.assign('/live/original')")
        page.wait_for_url(source)
    finally:
        context.close()


@pytest.fixture
def media_page(chrome, local_media):
    context = chrome.new_context()
    # The fixture only needs about:blank and a data: URL. Fail closed on HTTP.
    context.route("http://**/*", lambda route: route.abort())
    context.route("https://**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_content("<video autoplay loop playsinline></video>")
    page.evaluate(
        """async (src) => {
            const video = document.querySelector('video');
            video.src = src;
            await video.play();
        }""", local_media[1],
    )
    page.wait_for_function("document.querySelector('video').readyState >= 2")
    page.evaluate(
        """() => {
            window.__captureTracks = [];
            window.__recorders = [];
            window.__stopEvents = [];
            window.__startFailureAt = 0;
            window.__constructorFailureAt = 0;
            const nativeCapture = HTMLMediaElement.prototype.captureStream;
            HTMLMediaElement.prototype.captureStream = function () {
                const stream = nativeCapture.call(this);
                window.__captureTracks.push(...stream.getTracks());
                return stream;
            };
            const nativeClone = MediaStreamTrack.prototype.clone;
            MediaStreamTrack.prototype.clone = function () {
                const track = nativeClone.call(this);
                window.__captureTracks.push(track);
                return track;
            };
            const NativeRecorder = MediaRecorder;
            let ordinal = 0;
            window.MediaRecorder = class extends NativeRecorder {
                constructor(stream, options) {
                    const index = ++ordinal;
                    if (index === window.__constructorFailureAt) {
                        throw new Error('injected constructor failure');
                    }
                    super(stream, options);
                    this.__index = index;
                    window.__recorders.push(this);
                    this.addEventListener('stop', () => window.__stopEvents.push(index));
                }
                start(...args) {
                    if (this.__index === window.__startFailureAt) {
                        throw new Error('injected start failure');
                    }
                    return super.start(...args);
                }
            };
        }"""
    )
    yield page
    context.close()


def _make_recorder(kind, page, root):
    page = _DeadlinePage(page)
    if kind == "av":
        return BrowserAVRecorder(page, root / "video.webm", root / "audio.webm")
    return BrowserAudioRecorder(page, root / "audio.webm")


def _state_expression(kind):
    if kind == "av":
        return "window.__whatnotBrowserAVCapture.videoState"
    return "window.__whatnotAudioCapture"


@pytest.mark.parametrize("kind", ["audio", "av"])
@pytest.mark.parametrize("timeslice_ms", [100, 1000])
@pytest.mark.parametrize("local_abort", [False, True])
def test_capture_document_replacement_is_detected_and_keeps_partial_files(kind, timeslice_ms, local_abort, media_page, tmp_path, monkeypatch):
    recorder = _make_recorder(kind, media_page, tmp_path)
    recorder.start(timeslice_ms=timeslice_ms)
    media_page.wait_for_timeout(timeslice_ms + 700)
    assert recorder.has_capture_state()
    audio = tmp_path / "audio.webm"
    prefix = audio.read_bytes()
    assert prefix
    # A real navigation destroys the window globals, unlike set_content().
    from urllib.parse import quote

    challenge = "<title>Local verification fixture</title><h1>请验证您是真人</h1>"
    media_page.goto("data:text/html;charset=utf-8," + quote(challenge))
    assert not recorder.has_capture_state()
    if local_abort:
        def unexpected_evaluate(*_args, **_kwargs):
            raise AssertionError("Abort must not send a command to the replacement page")
        monkeypatch.setattr(recorder.page, "evaluate", unexpected_evaluate)
        recorder.abort_lost_document()
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="state was lost"):
        recorder.stop()
    assert not media_page.is_closed()
    assert "请验证您是真人" in media_page.inner_text("body")
    assert audio.read_bytes().startswith(prefix)
    files = recorder._files.values() if kind == "av" else [recorder._file]
    assert all(output.closed for output in files)
    if kind == "av":
        assert (tmp_path / "video.webm").stat().st_size > 0


@pytest.mark.parametrize("fixture_routes", [False, True])
def test_default_av_capture_writes_while_pumping_another_tab(chrome, local_media, tmp_path, fixture_routes):
    from whatnot_monitor.whatnot import WhatnotBrowser

    context = chrome.new_context()
    if fixture_routes:
        context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body="<h1>Show is live</h1><p>123 viewers</p><video autoplay loop playsinline></video>",
        ) if route.request.url.startswith("https://example.invalid/") else route.abort())
    else:
        context.route("http://**/*", lambda route: route.abort())
        context.route("https://**/*", lambda route: route.abort())
    inspection = context.new_page()
    if fixture_routes:
        inspection.goto("https://example.invalid/live/fixture")
    else:
        inspection.set_content("<title>Inspection tab</title>")
    room = context.new_page()
    room.set_content("<video autoplay loop playsinline></video>")
    source = local_media[1]
    if fixture_routes:
        source_path = tmp_path / "native-source.webm"
        subprocess.run([
            local_media[0], "-y", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=320x240:r=15", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=16000", "-t", "60", "-c:v", "libvpx",
            "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", str(source_path),
        ], check=True, capture_output=True, timeout=30)
        source = "data:video/webm;base64," + base64.b64encode(source_path.read_bytes()).decode()
    room.evaluate("""async src => {
        const video = document.querySelector('video');
        video.src = src;
        await video.play();
    }""", source)
    room.wait_for_function("document.querySelector('video').currentTime > 0.5")
    recorder = BrowserAVRecorder(
        room, tmp_path / "video.webm", tmp_path / "audio.webm",
        video_mime_type="video/webm;codecs=vp8,opus", video_bitrate_bps=2_500_000,
    )
    browser = WhatnotBrowser(context)
    room.evaluate("""() => {
        window.__inspectionCaptureCalls = 0;
        const capture = HTMLMediaElement.prototype.captureStream;
        HTMLMediaElement.prototype.captureStream = function (...args) {
            ++window.__inspectionCaptureCalls;
            return capture.apply(this, args);
        };
    }""")
    try:
        recorder.start()
        capture_calls = room.evaluate("window.__inspectionCaptureCalls")
        assert capture_calls > 0
        browser.inspect_page(room)
        for index in range(30):
            browser.pump_events(100)
            if index % 10 == 0:
                browser.inspect_page(room)
        assert room.evaluate("window.__inspectionCaptureCalls") == capture_calls
        assert recorder.video_path.stat().st_size > 0
        assert recorder.audio_path.stat().st_size > 0
    finally:
        if recorder._started:
            recorder.stop()
        context.close()


def _assert_clean(page):
    state = page.evaluate(
        """() => ({
            tracks: window.__captureTracks.map((track) => track.readyState),
            recorders: window.__recorders.map((recorder) => recorder.state),
            avState: Boolean(window.__whatnotBrowserAVCapture),
            audioState: Boolean(window.__whatnotAudioCapture)
        })"""
    )
    assert state["tracks"] and set(state["tracks"]) == {"ended"}
    assert all(item == "inactive" for item in state["recorders"])
    assert not state["avState"] and not state["audioState"]


def _assert_media(ffmpeg, path, *, video=False):
    decoded = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-f", "f32le", "-ac", "1", "-ar", "8000", "pipe:1"],
        check=True, capture_output=True, timeout=30,
    )
    samples = array("f")
    samples.frombytes(decoded.stdout)
    assert len(samples) > 100
    assert sum(value * value for value in samples) / len(samples) > 0.00001
    if video:
        pixels = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            check=True, capture_output=True, timeout=30,
        ).stdout
        assert len(pixels) == 160 * 120 * 3
        assert sum(pixels[0::3]) > 2 * sum(pixels[2::3])


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_local_chrome_outputs_decode_and_cleanup(kind, media_page, local_media, tmp_path):
    recorder = _make_recorder(kind, media_page, tmp_path)
    recorder.start(timeslice_ms=100)
    media_page.wait_for_timeout(700)
    result = recorder.stop()
    assert recorder.stop() == result
    _assert_media(local_media[0], tmp_path / "audio.webm")
    if kind == "av":
        _assert_media(local_media[0], tmp_path / "video.webm", video=True)
    _assert_clean(media_page)


@pytest.mark.parametrize("kind,index", [("audio", 1), ("av", 1), ("av", 2)])
@pytest.mark.parametrize("failure", ["start", "constructor"])
def test_partial_start_failure_does_not_hang_or_leak(kind, index, failure, media_page, tmp_path):
    media_page.evaluate(
        "({failure, index}) => { window[failure === 'start' ? '__startFailureAt' : '__constructorFailureAt'] = index; }",
        {"failure": failure, "index": index},
    )
    recorder = _make_recorder(kind, media_page, tmp_path)
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match=f"injected {failure} failure"):
        recorder.start()
    _assert_clean(media_page)
    files = recorder._files.values() if kind == "av" else [recorder._file]
    assert all(output.closed for output in files)
    # Only the first A/V recorder started when the second start failed.
    expected_stops = [1] if kind == "av" and index == 2 and failure == "start" else []
    assert media_page.evaluate("window.__stopEvents") == expected_stops


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_format_failure_stops_already_captured_tracks(kind, media_page, tmp_path):
    media_page.evaluate("MediaRecorder.isTypeSupported = () => false")
    recorder = _make_recorder(kind, media_page, tmp_path)
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="format"):
        recorder.start()
    _assert_clean(media_page)


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_slow_first_blob_cannot_reorder_writes(kind, media_page, tmp_path):
    recorder = _make_recorder(kind, media_page, tmp_path)
    recorder.start(timeslice_ms=1000)
    media_page.evaluate(
        """(expression) => {
            const state = (0, eval)(expression);
            const first = new Blob(['first']);
            const nativeRead = first.arrayBuffer.bind(first);
            first.arrayBuffer = async () => {
                await new Promise((resolve) => setTimeout(resolve, 200));
                return nativeRead();
            };
            state.recorder.dispatchEvent(new BlobEvent('dataavailable', {data: first}));
            state.recorder.dispatchEvent(new BlobEvent('dataavailable', {data: new Blob(['second'])}));
        }""", _state_expression(kind),
    )
    try:
        media_page.wait_for_timeout(350)
        output = tmp_path / ("video.webm" if kind == "av" else "audio.webm")
        assert output.read_bytes().startswith(b"firstsecond")
    finally:
        recorder.stop()
    _assert_clean(media_page)


class _FileProbe:
    def __init__(self, output, *, fail_write=False, fail_close=False):
        self.output = output
        self.fail_write = fail_write
        self.fail_close = fail_close
        self.write_attempts = 0
        self.writes_after_close = 0
        self.close_attempts = 0

    @property
    def closed(self):
        return self.output.closed

    def write(self, chunk):
        self.write_attempts += 1
        if self.closed:
            self.writes_after_close += 1
        if self.fail_write and self.write_attempts == 1:
            raise OSError("injected write failure")
        return self.output.write(chunk)

    def flush(self):
        return self.output.flush()

    def close(self):
        self.close_attempts += 1
        self.output.close()
        if self.fail_close:
            raise OSError("injected close failure")


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_failed_write_still_drains_final_chunks_and_preserves_original_error(kind, media_page, local_media, tmp_path):
    recorder = _make_recorder(kind, media_page, tmp_path)
    recorder.start(timeslice_ms=1000)
    if kind == "av":
        broken = _FileProbe(recorder._files["video"], fail_write=True, fail_close=True)
        healthy = _FileProbe(recorder._files["audio"])
        recorder._files.update(video=broken, audio=healthy)
    else:
        broken = _FileProbe(recorder._file, fail_write=True, fail_close=True)
        healthy = None
        recorder._file = broken
    media_page.evaluate(
        """(expression) => {
            const nativeRead = Blob.prototype.arrayBuffer;
            Blob.prototype.arrayBuffer = async function () {
                await new Promise((resolve) => setTimeout(resolve, 120));
                return nativeRead.call(this);
            };
            (0, eval)(expression).recorder.dispatchEvent(
                new BlobEvent('dataavailable', {data: new Blob(['injected failed chunk'])})
            );
        }""", _state_expression(kind),
    )
    media_page.wait_for_timeout(250)
    assert broken.write_attempts == 1
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="injected write failure") as error:
        recorder.stop()
    assert "injected close failure" not in str(error.value)
    assert broken.write_attempts >= 2  # The genuine final WebM blob was not abandoned.
    assert broken.closed and broken.writes_after_close == 0
    assert broken.close_attempts == 1
    if healthy:
        assert healthy.closed and healthy.write_attempts >= 1
        assert healthy.writes_after_close == 0
        _assert_media(local_media[0], tmp_path / "video.webm", video=True)
    _assert_media(local_media[0], tmp_path / "audio.webm")
    _assert_clean(media_page)
    assert len(media_page.evaluate("window.__stopEvents")) == (2 if kind == "av" else 1)
    with pytest.raises(type(error.value), match="injected write failure"):
        recorder.stop()


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_recorder_error_does_not_skip_stop_events_or_final_writes(kind, media_page, local_media, tmp_path):
    recorder = _make_recorder(kind, media_page, tmp_path)
    recorder.start(timeslice_ms=1000)
    media_page.wait_for_timeout(250)
    media_page.evaluate(
        """(expression) => {
            const event = new Event('error');
            event.error = new DOMException('injected encoder failure');
            (0, eval)(expression).recorder.dispatchEvent(event);
        }""", _state_expression(kind),
    )
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="injected encoder failure"):
        recorder.stop()
    _assert_clean(media_page)
    assert len(media_page.evaluate("window.__stopEvents")) == (2 if kind == "av" else 1)
    _assert_media(local_media[0], tmp_path / "audio.webm")
    if kind == "av":
        _assert_media(local_media[0], tmp_path / "video.webm", video=True)


class _ShutdownPage:
    def __init__(self, fail):
        self.fail = fail
        self.calls = 0

    def evaluate(self, _expression, _arg=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected evaluation failure")
        return {"startedAt": "start", "stoppedAt": "stop", "durationMs": 1}


@pytest.mark.parametrize("kind", ["audio", "av"])
@pytest.mark.parametrize("evaluation_fails", [True, False])
def test_close_failure_attempts_every_file_and_does_not_mask_primary_error(kind, evaluation_fails, tmp_path):
    page = _ShutdownPage(evaluation_fails)
    if kind == "av":
        recorder = BrowserAVRecorder(page, tmp_path / "video.webm", tmp_path / "audio.webm")
        probes = [_FileProbe(path.open("wb"), fail_close=True) for path in (recorder.video_path, recorder.audio_path)]
        recorder._files = dict(zip(("video", "audio"), probes))
    else:
        recorder = BrowserAudioRecorder(page, tmp_path / "audio.webm")
        probes = [_FileProbe(recorder.output_path.open("wb"), fail_close=True)]
        recorder._file = probes[0]
    recorder._started = True
    expected = "injected evaluation failure" if evaluation_fails else "injected close failure"
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match=expected):
        recorder.stop()
    assert all(probe.closed and probe.close_attempts == 1 for probe in probes)
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match=expected):
        recorder.stop()
    assert page.calls == 1


def _prepare_local_webrtc(page, topology):
    """Receive real local RTP audio with Web Audio and no audio DOM element."""
    page.evaluate(ROOM_CAPTURE_INIT_SCRIPT)
    page.evaluate(
        """async (topology) => {
            const video = document.querySelector('video');
            if (topology === 'no-media') {
                video.remove();
            } else if (topology === 'video-only') {
                video.pause();
                video.removeAttribute('src');
                const canvas = document.createElement('canvas');
                canvas.width = 160;
                canvas.height = 120;
                const paint = () => {
                    const context = canvas.getContext('2d');
                    context.fillStyle = 'red';
                    context.fillRect(0, 0, canvas.width, canvas.height);
                };
                paint();
                window.__paintTimer = setInterval(paint, 50);
                video.muted = true;
                video.srcObject = canvas.captureStream(15);
                await video.play();
            }
            const audio = new AudioContext();
            await audio.resume();
            const tone = audio.createOscillator();
            tone.frequency.value = 660;
            const gain = audio.createGain();
            gain.gain.value = 0.25;
            const destination = audio.createMediaStreamDestination();
            tone.connect(gain).connect(destination);
            tone.start();
            const sender = new RTCPeerConnection({iceServers: []});
            const receiver = new RTCPeerConnection({iceServers: []});
            window.__rtc = {audio, tone, sender, receiver};
            receiver.addEventListener('track', ({track}) => {
                window.__remoteAudio = track;
                window.__remoteCloneCalls = 0;
                const nativeClone = track.clone.bind(track);
                track.clone = () => {
                    window.__remoteCloneCalls++;
                    return nativeClone();
                };
                // Deliberately no <audio>: mirror a player using Web Audio.
                window.__rtc.remoteSource = audio.createMediaStreamSource(new MediaStream([track]));
                window.__rtc.remoteSource.connect(audio.destination);
                // An off-DOM media sink activates Chromium's remote audio decoder.
                const sink = new Audio();
                sink.srcObject = new MediaStream([track]);
                sink.volume = 0;
                window.__rtc.remoteSink = sink;
                window.__rtc.sinkReady = sink.play();
            });
            sender.addTrack(destination.stream.getAudioTracks()[0], destination.stream);
            const gathered = async (connection) => {
                if (connection.iceGatheringState === 'complete') return;
                await new Promise((resolve, reject) => {
                    const timer = setTimeout(() => reject(new Error('local ICE gathering timed out')), 5000);
                    connection.addEventListener('icegatheringstatechange', () => {
                        if (connection.iceGatheringState === 'complete') {
                            clearTimeout(timer);
                            resolve();
                        }
                    });
                });
            };
            await sender.setLocalDescription(await sender.createOffer());
            await gathered(sender);
            await receiver.setRemoteDescription(sender.localDescription);
            await receiver.setLocalDescription(await receiver.createAnswer());
            await gathered(receiver);
            await sender.setRemoteDescription(receiver.localDescription);
            await window.__rtc.sinkReady;
        }""", topology,
    )
    page.wait_for_function(
        "window.__remoteAudio?.readyState === 'live' && !window.__remoteAudio.muted && "
        "window.__rtc.receiver.connectionState === 'connected'"
    )
    # Exercise the production hook's asynchronous stats observer with a
    # synchronous predicate, not an immediately truthy pending Promise.
    page.wait_for_function(
        "window.__whatnotRoomDecodedAudioTracks.has(window.__remoteAudio)"
    )
    assert page.evaluate("document.querySelectorAll('audio').length") == 0
    assert page.evaluate("window.__whatnotRoomRemoteTracks.has(window.__remoteAudio)")


def _assert_original_receiver_live(page, expected_clones):
    assert page.evaluate("window.__remoteAudio.readyState") == "live"
    assert page.evaluate("window.__remoteAudio.enabled && !window.__remoteAudio.muted")
    assert page.evaluate("window.__rtc.receiver.connectionState") == "connected"
    assert page.evaluate("window.__remoteCloneCalls") == expected_clones


@pytest.mark.parametrize(
    "kind,topology",
    [("av", "video-only"), ("audio", "video-only"), ("audio", "no-media"),
     ("av", "dom-av"), ("audio", "dom-av")],
)
def test_local_webrtc_without_audio_dom_and_dom_priority(kind, topology, media_page, local_media, tmp_path):
    _prepare_local_webrtc(media_page, topology)
    recorder = _make_recorder(kind, media_page, tmp_path)
    metadata = recorder.start(timeslice_ms=100)
    expected_source = "video" if topology == "dom-av" else "webrtc"
    assert metadata["audioElement" if kind == "av" else "elementTag"] == expected_source
    media_page.wait_for_timeout(700)
    diagnostics = media_page.evaluate("""async () => ({
        audioContext: window.__rtc.audio.state,
        inbound: [...(await window.__rtc.receiver.getStats()).values()].filter(item => item.type === 'inbound-rtp'),
        outbound: [...(await window.__rtc.sender.getStats()).values()].filter(item => item.type === 'outbound-rtp'),
        capture: window.__captureTracks.map(track => ({kind: track.kind, muted: track.muted, enabled: track.enabled}))
    })""")
    recorder.stop()
    assert (tmp_path / "audio.webm").stat().st_size > 0, diagnostics
    _assert_media(local_media[0], tmp_path / "audio.webm")
    if kind == "av":
        _assert_media(local_media[0], tmp_path / "video.webm", video=True)
    _assert_clean(media_page)
    _assert_original_receiver_live(media_page, 0 if topology == "dom-av" else 1)


@pytest.mark.parametrize("kind", ["audio", "av"])
def test_remote_audio_original_survives_partial_start_cleanup(kind, media_page, tmp_path):
    _prepare_local_webrtc(media_page, "video-only")
    media_page.evaluate("(index) => { window.__startFailureAt = index; }", 2 if kind == "av" else 1)
    recorder = _make_recorder(kind, media_page, tmp_path)
    with pytest.raises((BrowserAudioCaptureError, BrowserAVCaptureError), match="injected start failure"):
        recorder.start()
    _assert_clean(media_page)
    _assert_original_receiver_live(media_page, 1)
