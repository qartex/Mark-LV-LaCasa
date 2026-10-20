"""Voice-sample capture contract: bounded collection and WAV output."""

import tempfile
import unittest
import wave
from pathlib import Path

from scripts.jarvis_speaker_capture import SampleCollector, level, write_wav

LOUD = b"\x00\x40" * 64          # well above the voice threshold
QUIET = b"\x01\x00" * 64         # room noise


class LevelTests(unittest.TestCase):
    def test_quiet_room_noise_stays_below_the_threshold(self):
        self.assertLess(level(QUIET), 500)

    def test_loud_speech_is_detected(self):
        self.assertGreater(level(LOUD), 500)


class CollectorTests(unittest.TestCase):
    def test_leading_noise_is_dropped_until_speech_starts(self):
        collector = SampleCollector(seconds=0.1, sample_rate=16000)  # 3200 bytes
        for _ in range(5):
            collector.feed(QUIET)
        self.assertEqual(0, collector.collected_bytes)
        collector.feed(LOUD)
        self.assertGreater(collector.collected_bytes, 0)

    def test_collection_stops_at_the_requested_duration(self):
        collector = SampleCollector(seconds=0.1, sample_rate=16000)
        for _ in range(50):
            collector.feed(LOUD)
        self.assertLessEqual(collector.collected_bytes, 3200)
        self.assertTrue(collector.finished.is_set())

    def test_empty_chunks_are_ignored(self):
        collector = SampleCollector(seconds=0.1)
        collector.feed(b"")
        self.assertEqual(0, collector.collected_bytes)


class WavTests(unittest.TestCase):
    def test_wav_is_mono_16k_and_owner_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sample.wav"
        write_wav(path, b"\x00\x01" * 16000)
        with wave.open(str(path), "rb") as handle:
            self.assertEqual(1, handle.getnchannels())
            self.assertEqual(2, handle.getsampwidth())
            self.assertEqual(16000, handle.getframerate())
            self.assertEqual(16000, handle.getnframes())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
