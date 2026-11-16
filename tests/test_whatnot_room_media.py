import base64
import subprocess
from array import array

import pytest
from playwright.sync_api import sync_playwright
from whatnot_monitor.recording.browser_av import BrowserAVRecorder
from whatnot_monitor.whatnot import WhatnotBrowser, WhatnotPageChanged


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as manager:
        browser = manager.chromium.launch(
            channel="chrome", headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        yield browser
        browser.close()


@pytest.fixture
def context(browser):
    context = browser.new_context()
    yield context
    context.close()


ROOM = """<!doctype html><meta charset='utf-8'>
<canvas width='64' height='64'></canvas><video autoplay muted></video>
<button aria-label='Sound'>Sound</button><button id='bid'>Bid</button>
<script>
const canvas = document.querySelector('canvas');
const drawing = canvas.getContext('2d');
setInterval(() => {drawing.fillStyle='red';drawing.fillRect(0,0,64,64)},50);
document.querySelector('video').srcObject = canvas.captureStream(20);
window.soundClicks=0; window.bidClicks=0;
document.querySelector('#bid').onclick=()=>window.bidClicks++;
document.querySelector('[aria-label=Sound]').onclick=()=>{
  window.soundClicks++;
  setTimeout(()=>{
    const context = new AudioContext();
    const oscillator = context.createOscillator();
    const destination = context.createMediaStreamDestination();
    oscillator.connect(destination);oscillator.start();
    window.audioContext=context;
    window.__whatnotRoomRemoteTracks.add(destination.stream.getAudioTracks()[0]);
    setTimeout(()=>window.__whatnotRoomDecodedAudioTracks.add(
      destination.stream.getAudioTracks()[0]),250);
  },300);
};
</script>"""


def test_room_initializes_sound_once_and_waits_for_real_audio_track(context):
    context.route("**/*", lambda route: route.fulfill(body=ROOM, content_type="text/html"))
    page = WhatnotBrowser(context).new_room_page(
        "https://www.whatnot.com/live/local-room", media_timeout_ms=5000
    )
    assert page.evaluate("window.soundClicks") == 1
    assert page.evaluate("window.bidClicks") == 0
    assert page.locator("audio").count() == 0
    assert page.evaluate("[...window.__whatnotRoomRemoteTracks].some(t=>t.kind==='audio'&&t.readyState==='live')")
    page.close()


def test_account_recording_reuses_its_own_inspection_tab(context):
    context.route("**/*", lambda route: route.fulfill(
        body="<h1>Show is live</h1>" + ROOM, content_type="text/html",
    ))
    browser = WhatnotBrowser(context)
    browser.reuse_inspection_for_recording = True
    url = "https://www.whatnot.com/live/local-room"
    assert browser.inspect_live(url)["live"]
    [inspection] = context.pages
    page = browser.new_room_page(url, media_timeout_ms=5000)
    assert page is inspection
    assert context.pages == [inspection]
    assert browser._inspection_page is None
    assert page.evaluate("window.soundClicks") == 1


def test_video_only_does_not_pass_audio_requirement(context):
    body = ROOM.replace("window.__whatnotRoomRemoteTracks.add(destination.stream.getAudioTracks()[0]);", "")
    context.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html"))
    with pytest.raises(WhatnotPageChanged, match="未提供所需的可播放媒体轨道"):
        WhatnotBrowser(context).new_room_page(
            "https://www.whatnot.com/live/local-room", media_timeout_ms=1000
        )
    assert context.pages == []


def test_video_only_mode_does_not_click_sound(context):
    context.route("**/*", lambda route: route.fulfill(body=ROOM, content_type="text/html"))
    page = WhatnotBrowser(context).new_room_page(
        "https://www.whatnot.com/live/local-room", require_audio=False, media_timeout_ms=5000
    )
    assert page.evaluate("window.soundClicks") == 0
    assert page.evaluate("window.bidClicks") == 0
    assert page.locator("video").evaluate("element => element.muted")
    page.close()


@pytest.fixture(scope="module")
def audio_source(tmp_path_factory):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    path = tmp_path_factory.mktemp("room-sound") / "source.webm"
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=15",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "8",
        "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
        "-c:a", "libopus", str(path),
    ], check=True, capture_output=True, timeout=30)
    return ffmpeg, "data:video/webm;base64," + base64.b64encode(path.read_bytes()).decode()


def _sound_room(source, *, muted=True, volume=1, label="Sound", control=True, replace=False, stuck=False):
    import json

    return f"""<!doctype html><meta charset='utf-8'>
    <h1>Show is live</h1><video autoplay loop playsinline></video>
    <button aria-label='{label}' id='sound' {'hidden' if not control else ''}>{label}</button>
    <button id='bid'>Bid</button><script>
    const source = {json.dumps(source)};
    function player() {{
      const video = document.querySelector('video');
      video.muted = {str(muted).lower()};
      video.defaultMuted = {str(muted).lower()};
      video.volume = {volume}; video.src = source;
      void video.play().catch(() => {{}});
      return video;
    }}
    const original = player();
    if ({str(stuck).lower()}) {{
      Object.defineProperty(original, 'muted', {{get:()=>true,set:()=>{{}}}});
    }}
    window.soundClicks=0; window.bidClicks=0;
    document.querySelector('#bid').onclick=()=>window.bidClicks++;
    document.querySelector('#sound').onclick=()=>{{
      window.soundClicks++;
      if ({str(replace).lower()}) {{
        original.remove();
        setTimeout(()=>{{
          document.querySelector('h1').insertAdjacentHTML('afterend','<video autoplay loop playsinline></video>');
          player();
        }},200);
      }} else {{
        original.muted=!original.muted;
        if (original.volume===0) original.volume=1;
        void original.play().catch(()=>{{}});
      }}
    }};
    </script>"""


@pytest.mark.parametrize("options,clicks", [
    ({"muted": True}, 1),
    ({"muted": False, "volume": 0}, 1),
    ({"muted": False, "volume": 0.35}, 0),
    ({"muted": True, "label": "Unmute"}, 1),
    ({"muted": True, "label": "开启声音"}, 1),
    ({"muted": True, "control": False}, 0),
    ({"muted": True, "replace": True}, 1),
])
def test_room_sound_is_enabled_before_av_capture(context, audio_source, tmp_path, options, clicks):
    body = _sound_room(audio_source[1], **options)
    context.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html"))
    page = WhatnotBrowser(context).new_room_page(
        "https://www.whatnot.com/live/local-audio", media_timeout_ms=7000,
    )
    assert page.evaluate("window.soundClicks") == clicks
    assert page.evaluate("window.bidClicks") == 0
    state = page.locator("video").evaluate("v=>({muted:v.muted,volume:v.volume,paused:v.paused})")
    assert state["muted"] is False
    assert state["paused"] is False
    assert state["volume"] > 0
    if not options.get("muted", True) and options.get("volume", 1) > 0:
        assert state["volume"] == 0.35  # Do not turn up a valid existing volume.
    from test_recording_browser_regressions import _DeadlinePage

    recorder = BrowserAVRecorder(_DeadlinePage(page), tmp_path / "video.webm", tmp_path / "audio.webm")
    try:
        recorder.start()
        page.locator("video").evaluate("v => { v.muted = true; }")
        page.wait_for_function("!document.querySelector('video').muted", timeout=3000)
        page.wait_for_timeout(1400)
    finally:
        if recorder._started:
            recorder.stop()
    samples = array("f")
    decoded = subprocess.run([
        audio_source[0], "-v", "error", "-i", str(recorder.audio_path),
        "-f", "f32le", "-ac", "1", "-ar", "8000", "pipe:1",
    ], check=True, capture_output=True, timeout=30)
    samples.frombytes(decoded.stdout)
    assert len(samples) > 100
    assert sum(value * value for value in samples) / len(samples) > 0.00001
    assert recorder.video_path.stat().st_size > 0


@pytest.mark.parametrize("decoded_receiver", [False, True])
def test_sound_that_stays_muted_is_not_reported_ready(context, audio_source, decoded_receiver):
    body = _sound_room(audio_source[1], stuck=True)
    if decoded_receiver:
        body += """<script>
        const graph = new AudioContext();
        const destination = graph.createMediaStreamDestination();
        const track = destination.stream.getAudioTracks()[0];
        window.__whatnotRoomRemoteTracks.add(track);
        window.__whatnotRoomDecodedAudioTracks.add(track);
        window.fixtureAudioGraph=graph;
        </script>"""
    context.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html"))
    with pytest.raises(WhatnotPageChanged, match="非静音且音量大于 0") as error:
        WhatnotBrowser(context).new_room_page(
            "https://www.whatnot.com/live/stuck-audio", media_timeout_ms=1200,
        )
    assert '"muted": true' in str(error.value)
    assert context.pages == []
