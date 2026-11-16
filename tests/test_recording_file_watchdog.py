import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from whatnot_monitor.recording_file_watchdog import RecordingFileWatchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / 'a.webm'
        self.video = Path(self.tmp.name) / 'v.webm'
        self.audio.write_bytes(b'')
        self.video.write_bytes(b'video')
        self.events = queue.Queue()
        self.now = 0
        self.w = RecordingFileWatchdog(None, self.events, clock=lambda: self.now)
        self.w.register('s', 'room', {'音频': self.audio, '视频': self.video})

    def test_each_channel_checked_and_recovers_without_stopping(self):
        self.w.poll_once()
        self.now = 179
        self.video.write_bytes(b'videomore')
        self.w.poll_once()
        self.assertFalse(self.w.warning('s'))
        self.now = 180
        self.w.poll_once()
        self.assertTrue(self.w.warning('s'))
        self.assertIn('音频文件无增长', self.w._alerts.get_nowait()[3])
        self.audio.write_bytes(b'now we have data')
        self.now = 181
        self.w.poll_once()
        self.assertFalse(self.w.warning('s'))
        self.assertEqual(self.video.read_bytes(), b'videomore')

    def test_independent_thread_reports_without_browser_owner(self):
        w = RecordingFileWatchdog(None, self.events, timeout_seconds=0.05, interval_seconds=0.01)
        w._persist = lambda sid, message, observed: self.events.put(('warning', message))
        w.register('s', 'room', {'音频': self.audio})
        w.start()
        try:
            level, message = self.events.get(timeout=2)
            self.assertEqual(level, 'warning')
            self.assertIn('音频文件无增长', message)
            self.assertTrue(w.warning('s'))
        finally:
            w.stop()
        self.assertFalse(w._thread.is_alive())

    def test_unregister_and_same_id_reregistration_ignore_old_poll(self):
        entered, resume = threading.Event(), threading.Event()
        original = Path.stat
        def blocked(path, *args, **kwargs):
            if path == self.audio:
                entered.set()
                self.assertTrue(resume.wait(2))
            return original(path, *args, **kwargs)
        self.now = 200
        with patch.object(Path, 'stat', blocked):
            worker = threading.Thread(target=self.w.poll_once)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.w.unregister('s')
            self.w.register('s', 'new room', {'视频': self.video})
            resume.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertFalse(self.w.warning('s'))
        self.assertTrue(self.events.empty())

    def test_stop_start_are_idempotent(self):
        self.w.start()
        initial = self.w._thread
        self.w.start()
        self.assertIs(initial, self.w._thread)
        self.w.stop()
        self.w.stop()
        self.assertFalse(initial.is_alive())
        self.w.start()
        self.assertTrue(self.w._thread.is_alive())
        self.w.stop()

if __name__ == '__main__': unittest.main()
