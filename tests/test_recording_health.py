import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from whatnot_monitor.recording_health import RecordingHealth, empty_recording_channels


class RecordingHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / 'audio.webm'
        self.video = Path(self.tmp.name) / 'video.webm'
        self.audio.write_bytes(b'')
        self.video.write_bytes(b'')
        self.recorder = SimpleNamespace(audio_path=self.audio, video_path=self.video)
        self.now = 0
        self.health = RecordingHealth(timeout_seconds=180, clock=lambda: self.now)

    def test_startup_grace_and_zero_byte_failure(self):
        self.assertIsNone(self.health.check('one', self.recorder))
        self.now = 179
        self.assertIsNone(self.health.check('one', self.recorder))
        self.now = 180
        self.assertIn('音频', self.health.check('one', self.recorder))
        self.assertEqual(self.audio.stat().st_size, 0)
        self.assertEqual(self.video.stat().st_size, 0)

    def test_one_channel_growth_does_not_mask_other_channel_stall(self):
        self.health.check('one', self.recorder)
        self.now = 100
        self.video.write_bytes(b'video')
        self.health.check('one', self.recorder)
        self.now = 180
        error = self.health.check('one', self.recorder)
        self.assertIn('音频', error)
        self.assertNotIn('视频（', error)
        self.assertEqual(self.video.read_bytes(), b'video')

    def test_growing_channels_remain_healthy_and_forget_resets(self):
        self.health.check('one', self.recorder)
        self.now = 100
        self.audio.write_bytes(b'a')
        self.video.write_bytes(b'v')
        self.assertIsNone(self.health.check('one', self.recorder))
        self.now = 250
        self.assertIsNone(self.health.check('one', self.recorder))
        self.health.forget('one')
        self.now = 999
        self.assertIsNone(self.health.check('one', self.recorder))

    def test_final_zero_output_is_not_success(self):
        self.video.write_bytes(b'v')
        self.assertEqual(empty_recording_channels(self.recorder), ['音频'])
        self.audio.write_bytes(b'a')
        self.assertEqual(empty_recording_channels(self.recorder), [])

    def test_first_observation_does_not_retroactively_timeout(self):
        self.now = 999999
        self.assertIsNone(self.health.check('one', self.recorder))

    def test_sessions_are_independent(self):
        self.health.check('one', self.recorder)
        self.now = 170
        self.health.check('two', self.recorder)
        self.now = 180
        self.assertIsNotNone(self.health.check('one', self.recorder))
        self.assertIsNone(self.health.check('two', self.recorder))

if __name__ == '__main__':
    unittest.main()
