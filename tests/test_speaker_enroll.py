"""Enrollment CLI contract: WAV handling only (no model, no audio kept)."""

import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from scripts.jarvis_speaker_enroll import load_wav


def _wav(path: Path, samples, *, rate=16000, channels=1) -> None:
    data = array("h", samples)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())


class LoadWavTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_mono_pcm_is_returned_unchanged(self):
        path = self.dir / "mono.wav"
        _wav(path, [0, 100, -100, 200])
        self.assertEqual([0, 100, -100, 200], list(array("h", load_wav(path))))

    def test_stereo_is_downmixed_to_mono(self):
        path = self.dir / "stereo.wav"
        _wav(path, [100, 300, 200, 400], channels=2)
        self.assertEqual([200, 300], list(array("h", load_wav(path))))

    def test_foreign_rate_is_resampled_to_16k(self):
        path = self.dir / "48k.wav"
        _wav(path, list(range(0, 4800, 10)), rate=48000)
        pcm = load_wav(path)
        self.assertEqual(160, len(pcm) // 2)

    def test_non_16bit_is_refused(self):
        path = self.dir / "8bit.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(16000)
            handle.writeframes(b"\x80" * 100)
        with self.assertRaises(ValueError):
            load_wav(path)


if __name__ == "__main__":
    unittest.main()
