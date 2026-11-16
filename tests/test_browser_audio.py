from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave
from array import array
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from whatnot_monitor.config import Settings
from whatnot_monitor.recording.browser_audio import BrowserAudioRecorder
from whatnot_monitor.runtime import MonitorRuntime


def _write_tone(path: Path, frequency: int) -> None:
    sample_rate = 16_000
    duration = 4
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_rate * duration):
            sample = int(9000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
        output.writeframes(frames)


def _power(samples: array, frequency: int, sample_rate: int) -> float:
    omega = 2 * math.pi * frequency / sample_rate
    coefficient = 2 * math.cos(omega)
    previous = previous2 = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous2
        previous2, previous = previous, current
    return (
        previous * previous + previous2 * previous2 - coefficient * previous * previous2
    )


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


class BrowserAudioIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _write_tone(self.root / "tone440.wav", 440)
        _write_tone(self.root / "tone880.wav", 880)
        handler = lambda *args, **kwargs: _QuietHandler(
            *args, directory=str(self.root), **kwargs
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
        except Exception as exc:  # noqa: BLE001 - skip when browser runtime is unavailable
            self.server.shutdown()
            self.server.server_close()
            self.temp.cleanup()
            self.playwright.stop()
            self.skipTest(f"Playwright Chromium unavailable: {exc}")

    def tearDown(self) -> None:
        self.browser.close()
        self.playwright.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp.cleanup()

    def _page_with_audio(self, context, source: str):
        page = context.new_page()
        page.goto(f"{self.base_url}/{source}")
        page.evaluate(
            """(src) => {
                const audio = document.createElement('audio');
                audio.src = src;
                audio.autoplay = true;
                audio.loop = true;
                document.body.append(audio);
                return audio.play();
            }""",
            f"{self.base_url}/{source}",
        )
        page.wait_for_function(
            "document.querySelector('audio')?.readyState >= 2 && !document.querySelector('audio')?.paused"
        )
        return page

    def test_two_pages_are_recorded_without_cross_room_audio(self) -> None:
        ffmpeg = None
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest(
                "FFmpeg is required to decode captured WebM for tone verification"
            )

        context = self.browser.new_context()
        page_a = self._page_with_audio(context, "tone440.wav")
        page_b = self._page_with_audio(context, "tone880.wav")
        path_a = self.root / "room-a.webm"
        path_b = self.root / "room-b.webm"
        recorder_a = BrowserAudioRecorder(page_a, path_a)
        recorder_b = BrowserAudioRecorder(page_b, path_b)

        result_a = recorder_a.start()
        result_b = recorder_b.start()
        import time

        time.sleep(2)
        recorder_a.stop()
        recorder_b.stop()
        context.close()

        self.assertEqual(result_a["audioTracks"], 1)
        self.assertEqual(result_b["audioTracks"], 1)
        self.assertTrue(path_a.is_file() and path_a.stat().st_size > 1000)
        self.assertTrue(path_b.is_file() and path_b.stat().st_size > 1000)

        def decode(path: Path) -> array:
            process = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(path),
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
            )
            samples = array("f")
            samples.frombytes(process.stdout)
            return samples

        samples_a, samples_b = decode(path_a), decode(path_b)
        self.assertGreater(len(samples_a), 4000)
        self.assertGreater(len(samples_b), 4000)
        power_a_440 = _power(samples_a, 440, 8000)
        power_a_880 = _power(samples_a, 880, 8000)
        power_b_440 = _power(samples_b, 440, 8000)
        power_b_880 = _power(samples_b, 880, 8000)
        self.assertGreater(power_a_440, power_a_880 * 50)
        self.assertGreater(power_b_880, power_b_440 * 50)

        runtime = object.__new__(MonitorRuntime)
        runtime.settings = Settings(audio_sample_rate=16_000, audio_channels=1)
        wav_a = runtime._convert_to_wav(path_a)
        with wave.open(str(wav_a), "rb") as converted:
            self.assertEqual(converted.getframerate(), 16_000)
            self.assertEqual(converted.getnchannels(), 1)
            self.assertGreater(converted.getnframes(), 8_000)

    def test_five_and_ten_pages_keep_audio_isolated(self) -> None:
        ffmpeg = None
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest(
                "FFmpeg is required to decode captured WebM for tone verification"
            )

        def decode(path: Path) -> array:
            process = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(path),
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
            )
            samples = array("f")
            samples.frombytes(process.stdout)
            return samples

        import time

        for source_count in (5, 10):
            with self.subTest(source_count=source_count):
                frequencies = [440 + 320 * index for index in range(source_count)]
                for index, frequency in enumerate(frequencies):
                    _write_tone(self.root / f"tone-{index}.wav", frequency)

                context = self.browser.new_context()
                pages = [
                    self._page_with_audio(context, f"tone-{index}.wav")
                    for index in range(source_count)
                ]
                paths = [
                    self.root / f"room-{index}.webm" for index in range(source_count)
                ]
                recorders = [
                    BrowserAudioRecorder(page, path)
                    for page, path in zip(pages, paths, strict=True)
                ]
                results = [recorder.start() for recorder in recorders]
                time.sleep(2)
                for recorder in recorders:
                    recorder.stop()
                context.close()

                for result, path in zip(results, paths, strict=True):
                    self.assertEqual(result["audioTracks"], 1)
                    self.assertTrue(path.is_file() and path.stat().st_size > 1000)

                decoded = [decode(path) for path in paths]
                for own_index, samples in enumerate(decoded):
                    self.assertGreater(len(samples), 4000)
                    powers = [
                        _power(samples, frequency, 8000) for frequency in frequencies
                    ]
                    own_power = powers[own_index]
                    strongest_other = max(
                        power
                        for index, power in enumerate(powers)
                        if index != own_index
                    )
                    self.assertGreater(
                        own_power,
                        strongest_other * 50,
                        msg=(
                            f"room {own_index} ({frequencies[own_index]} Hz) did not "
                            f"isolate its source: own={own_power:.3g}, "
                            f"strongest_other={strongest_other:.3g}"
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
