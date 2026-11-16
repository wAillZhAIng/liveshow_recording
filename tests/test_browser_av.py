from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from array import array
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from whatnot_monitor.recording.browser_av import (
    BrowserAVCaptureError,
    BrowserAVRecorder,
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


def _ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which("ffmpeg")


def _make_clip(
    ffmpeg: str,
    path: Path,
    color: str,
    frequency: int,
    *,
    include_audio: bool = True,
) -> None:
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=160x120:r=15",
    ]
    if include_audio:
        command.extend(
            ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=16000"]
        )
    command.extend(
        [
            "-t",
            "4",
            "-c:v",
            "libvpx",
            "-deadline",
            "realtime",
            "-cpu-used",
            "8",
            "-b:v",
            "180k",
        ]
    )
    if include_audio:
        command.extend(["-c:a", "libopus", "-b:a", "32k", "-shortest"])
    command.append(str(path))
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=30,
    )


def _make_audio(ffmpeg: str, path: Path) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "4",
            "-c:a",
            "libopus",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _decode_audio(ffmpeg: str, path: Path) -> array:
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    samples = array("f")
    samples.frombytes(result.stdout)
    return samples


def _tone_power(samples: array, frequency: int, sample_rate: int = 8000) -> float:
    omega = 2 * math.pi * frequency / sample_rate
    coefficient = 2 * math.cos(omega)
    previous = previous2 = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous2
        previous2, previous = previous, current
    return previous * previous + previous2 * previous2 - coefficient * previous * previous2


class BrowserAVRecorderTests(unittest.TestCase):
    MULTI_PAGE_MEDIA = (
        ("red", 440, (255, 0, 0)),
        ("blue", 523, (0, 0, 255)),
        ("lime", 617, (0, 255, 0)),
        ("yellow", 733, (255, 255, 0)),
        ("magenta", 859, (255, 0, 255)),
        ("cyan", 947, (0, 255, 255)),
        ("orange", 1061, (255, 165, 0)),
        ("purple", 1187, (128, 0, 128)),
        ("deepskyblue", 1291, (0, 191, 255)),
        ("chartreuse", 1439, (127, 255, 0)),
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = _ffmpeg_path()
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is required to generate and inspect A/V clips")
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        _make_clip(cls.ffmpeg, cls.root / "red-440.webm", "red", 440)
        _make_clip(cls.ffmpeg, cls.root / "blue-880.webm", "blue", 880)
        _make_clip(
            cls.ffmpeg,
            cls.root / "video-only.webm",
            "red",
            440,
            include_audio=False,
        )
        _make_audio(cls.ffmpeg, cls.root / "audio-only.webm")
        multi_page_sources = []
        for index, (color, frequency, expected_rgb) in enumerate(cls.MULTI_PAGE_MEDIA):
            source = cls.root / f"multi-page-{index}.webm"
            _make_clip(cls.ffmpeg, source, color, frequency)
            multi_page_sources.append((source, frequency, expected_rgb))
        cls.multi_page_sources = tuple(multi_page_sources)
        handler = lambda *args, **kwargs: _QuietHandler(
            *args, directory=str(cls.root), **kwargs
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
        except Exception as exc:
            cls.server.shutdown()
            cls.server.server_close()
            cls.server_thread.join(timeout=2)
            cls.temp.cleanup()
            cls.playwright.stop()
            raise unittest.SkipTest(f"Playwright Chromium unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()
        if getattr(cls, "server", None):
            cls.server.shutdown()
            cls.server.server_close()
            cls.server_thread.join(timeout=2)
        if getattr(cls, "temp", None):
            cls.temp.cleanup()

    def _page_with_video(self, context, clip_name: str):
        page = context.new_page()
        page.goto(self.base_url)
        page.evaluate(
            """(src) => {
                const video = document.createElement('video');
                video.src = src;
                video.autoplay = true;
                video.loop = true;
                video.playsInline = true;
                video.muted = false;
                document.body.append(video);
                return video.play();
            }""",
            f"{self.base_url}/{clip_name}",
        )
        page.wait_for_function(
            "document.querySelector('video')?.readyState >= 2 && "
            "!document.querySelector('video')?.paused && "
            "document.querySelector('video')?.videoWidth > 0"
        )
        return page

    def _decode_video_pixels(self, path: Path) -> tuple[int, int, int]:
        decoded = subprocess.run(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "2",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        pixels = array("B", decoded.stdout)
        self.assertGreaterEqual(len(pixels), 160 * 120 * 3)
        count = len(pixels) // 3
        return tuple(sum(pixels[channel::3]) // count for channel in range(3))

    def test_two_independent_pages_keep_video_and_audio_isolated(self) -> None:
        context = self.browser.new_context()
        pages = [
            self._page_with_video(context, "red-440.webm"),
            self._page_with_video(context, "blue-880.webm"),
        ]
        outputs = [
            (self.root / "captured-red-video.webm", self.root / "captured-red-audio.webm"),
            (self.root / "captured-blue-video.webm", self.root / "captured-blue-audio.webm"),
        ]
        recording_options = [
            ("video/webm;codecs=vp8,opus", 650_000),
            ("video/webm;codecs=unsupported,opus", 720_000),
        ]
        recorders = [
            BrowserAVRecorder(
                page,
                video_path,
                audio_path,
                video_mime_type=mime_type,
                video_bitrate_bps=bitrate,
            )
            for page, (video_path, audio_path), (mime_type, bitrate) in zip(
                pages, outputs, recording_options, strict=True
            )
        ]
        try:
            started = [recorder.start() for recorder in recorders]
            time.sleep(2)
            results = [recorder.stop() for recorder in recorders]
        finally:
            context.close()

        for start, result, (video_path, audio_path), (requested_mime, requested_bitrate) in zip(
            started, results, outputs, recording_options, strict=True
        ):
            self.assertEqual(start["videoTracks"], 1)
            self.assertEqual(start["videoAudioTracks"], 1)
            self.assertEqual(start["audioTracks"], 1)
            self.assertEqual(start["videoElement"], "video")
            self.assertTrue(start["videoMimeType"].startswith("video/webm"))
            self.assertEqual(start["requestedVideoMimeType"], requested_mime)
            self.assertEqual(start["requestedVideoBitrateBps"], requested_bitrate)
            self.assertEqual(start["actualVideoBitsPerSecond"], requested_bitrate)
            self.assertEqual(
                start["videoMimeTypeFallback"],
                start["videoMimeType"] != requested_mime,
            )
            self.assertEqual(result["videoBytes"], video_path.stat().st_size)
            self.assertEqual(result["audioBytes"], audio_path.stat().st_size)
            self.assertGreater(result["durationMs"], 1000)
            self.assertGreater(video_path.stat().st_size, 1000)
            self.assertGreater(audio_path.stat().st_size, 1000)

        red_video_mean = self._decode_video_pixels(outputs[0][0])
        blue_video_mean = self._decode_video_pixels(outputs[1][0])
        self.assertGreater(red_video_mean[0], red_video_mean[2] * 2)
        self.assertGreater(blue_video_mean[2], blue_video_mean[0] * 2)

        red_audio = _decode_audio(self.ffmpeg, outputs[0][1])
        blue_audio = _decode_audio(self.ffmpeg, outputs[1][1])
        self.assertGreater(len(red_audio), 4000)
        self.assertGreater(len(blue_audio), 4000)
        self.assertGreater(_tone_power(red_audio, 440), _tone_power(red_audio, 880) * 50)
        self.assertGreater(_tone_power(blue_audio, 880), _tone_power(blue_audio, 440) * 50)

        # The combined A/V outputs must also be valid and carry the matching audio.
        red_muxed_audio = _decode_audio(self.ffmpeg, outputs[0][0])
        blue_muxed_audio = _decode_audio(self.ffmpeg, outputs[1][0])
        self.assertGreater(_tone_power(red_muxed_audio, 440), _tone_power(red_muxed_audio, 880) * 50)
        self.assertGreater(_tone_power(blue_muxed_audio, 880), _tone_power(blue_muxed_audio, 440) * 50)

    def test_five_and_ten_independent_pages_keep_av_isolated(self) -> None:
        media = self.multi_page_sources
        for page_count in (5, 10):
            with self.subTest(page_count=page_count):
                context = self.browser.new_context()
                pages = [
                    self._page_with_video(context, source.name)
                    for source, _, _ in media[:page_count]
                ]
                outputs = [
                    (
                        self.root / f"{page_count}-page-{index}-video.webm",
                        self.root / f"{page_count}-page-{index}-audio.webm",
                    )
                    for index in range(page_count)
                ]
                recorders = [
                    BrowserAVRecorder(
                        page,
                        video_path,
                        audio_path,
                        video_mime_type="video/webm;codecs=vp8,opus",
                        video_bitrate_bps=550_000,
                    )
                    for page, (video_path, audio_path) in zip(
                        pages, outputs, strict=True
                    )
                ]
                try:
                    starts = [recorder.start() for recorder in recorders]
                    time.sleep(2)
                    results = [recorder.stop() for recorder in recorders]
                finally:
                    for recorder in recorders:
                        if recorder._started and not recorder._stopped:
                            try:
                                recorder.stop()
                            except BrowserAVCaptureError:
                                pass
                    context.close()

                expected_colors = [item[2] for item in media[:page_count]]
                for index, ((video_path, audio_path), start, result) in enumerate(
                    zip(outputs, starts, results, strict=True)
                ):
                    with self.subTest(page_count=page_count, page=index):
                        self.assertEqual(start["videoTracks"], 1)
                        self.assertEqual(start["videoAudioTracks"], 1)
                        self.assertEqual(start["audioTracks"], 1)
                        self.assertGreater(result["videoBytes"], 1000)
                        self.assertGreater(result["audioBytes"], 1000)
                        subprocess.run(
                            [self.ffmpeg, "-v", "error", "-i", str(video_path), "-f", "null", "-"],
                            check=True,
                            capture_output=True,
                            timeout=30,
                        )
                        actual_rgb = self._decode_video_pixels(video_path)
                        distances = [
                            sum((actual - expected) ** 2 for actual, expected in zip(actual_rgb, color, strict=True))
                            for color in expected_colors
                        ]
                        self.assertEqual(distances.index(min(distances)), index)

                        expected_frequency = media[index][1]
                        candidate_frequencies = [item[1] for item in media[:page_count]]
                        isolated_audio = _decode_audio(self.ffmpeg, audio_path)
                        muxed_audio = _decode_audio(self.ffmpeg, video_path)
                        for samples in (isolated_audio, muxed_audio):
                            powers = {
                                frequency: _tone_power(samples, frequency)
                                for frequency in candidate_frequencies
                            }
                            strongest_other = max(
                                power
                                for frequency, power in powers.items()
                                if frequency != expected_frequency
                            )
                            self.assertGreater(
                                powers[expected_frequency], strongest_other * 20
                            )

    def test_missing_synchronized_audio_video_fails_explicitly(self) -> None:
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(self.base_url)
        page.evaluate(
            """(src) => {
                const audio = document.createElement('audio');
                audio.src = src;
                audio.autoplay = true;
                audio.loop = true;
                document.body.append(audio);
                return audio.play();
            }""",
            f"{self.base_url}/audio-only.webm",
        )
        page.wait_for_function(
            "document.querySelector('audio')?.readyState >= 2 && "
            "!document.querySelector('audio')?.paused"
        )
        recorder = BrowserAVRecorder(
            page,
            self.root / "missing-video.webm",
            self.root / "missing-audio.webm",
        )
        try:
            with self.assertRaisesRegex(BrowserAVCaptureError, "video track"):
                recorder.start()
        finally:
            context.close()

    def test_separate_video_and_audio_elements_are_combined(self) -> None:
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(self.base_url)
        page.evaluate(
            """({videoSrc, audioSrc}) => {
                const video = document.createElement('video');
                video.src = videoSrc;
                video.autoplay = true;
                video.loop = true;
                video.playsInline = true;
                video.muted = true;
                document.body.append(video);
                const audio = document.createElement('audio');
                audio.src = audioSrc;
                audio.autoplay = true;
                audio.loop = true;
                document.body.append(audio);
                return Promise.all([video.play(), audio.play()]);
            }""",
            {
                "videoSrc": f"{self.base_url}/video-only.webm",
                "audioSrc": f"{self.base_url}/audio-only.webm",
            },
        )
        page.wait_for_function(
            """() => {
                const [video, audio] = document.querySelectorAll('video, audio');
                return video?.readyState >= 2 && !video.paused
                    && audio?.readyState >= 2 && !audio.paused;
            }"""
        )
        video_path = self.root / "separate-elements-video.webm"
        audio_path = self.root / "separate-elements-audio.webm"
        recorder = BrowserAVRecorder(page, video_path, audio_path)
        try:
            started = recorder.start()
            time.sleep(2)
            result = recorder.stop()
        finally:
            context.close()

        self.assertEqual(started["videoElement"], "video")
        self.assertEqual(started["audioElement"], "audio")
        self.assertEqual(started["videoTracks"], 1)
        self.assertEqual(started["videoAudioTracks"], 1)
        self.assertEqual(started["audioTracks"], 1)
        self.assertGreater(result["videoBytes"], 1000)
        self.assertGreater(result["audioBytes"], 1000)
        self.assertGreater(result["durationMs"], 1000)
        self.assertGreater(_tone_power(_decode_audio(self.ffmpeg, audio_path), 440), 0)

    def test_no_compatible_video_format_fails_clearly(self) -> None:
        context = self.browser.new_context()
        page = self._page_with_video(context, "red-440.webm")
        page.evaluate(
            """() => {
                const original = MediaRecorder.isTypeSupported.bind(MediaRecorder);
                MediaRecorder.isTypeSupported = (type) =>
                    type.startsWith('audio/') ? original(type) : false;
            }"""
        )
        recorder = BrowserAVRecorder(
            page,
            self.root / "no-video-format.webm",
            self.root / "no-video-format-audio.webm",
            video_mime_type="video/webm;codecs=vp8,opus",
            video_bitrate_bps=500_000,
        )
        try:
            with self.assertRaisesRegex(
                BrowserAVCaptureError,
                "No compatible WebM video format.*video/webm;codecs=vp8,opus",
            ):
                recorder.start()
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
