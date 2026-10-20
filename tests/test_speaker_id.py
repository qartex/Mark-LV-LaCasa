"""Speaker identification contract tests (fake embedder, no model, no network)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.speaker_id import (
    SpeakerIdentifier,
    SpeakerProfiles,
    cosine,
)

SAMPLE_RATE = 16000
LONG_PCM = b"\x00\x01" * (SAMPLE_RATE * 2)  # 2 s of 16-bit mono


def _fake_embedder(table):
    def embed(pcm):
        return table[pcm[:4]]

    return embed


class CosineTests(unittest.TestCase):
    def test_identical_vectors_score_one(self):
        self.assertAlmostEqual(1.0, cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]))

    def test_orthogonal_vectors_score_zero(self):
        self.assertAlmostEqual(0.0, cosine([1.0, 0.0], [0.0, 1.0]))


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "speakers.json"

    def test_profiles_round_trip_owner_only(self):
        profiles = SpeakerProfiles(self.path)
        profiles.vectors["lukas"] = [1.0, 0.0]
        profiles.save(model="model.onnx")
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)
        again = SpeakerProfiles(self.path)
        self.assertEqual("model.onnx", again.model)
        key, score = again.match([1.0, 0.0])
        self.assertEqual("lukas", key)
        self.assertAlmostEqual(1.0, score)

    def test_world_readable_profiles_are_refused(self):
        self.path.write_text(json.dumps({"speakers": {"lukas": [1.0, 0.0]}}))
        os.chmod(self.path, 0o644)
        with self.assertRaises(PermissionError):
            SpeakerProfiles(self.path)

    def test_below_threshold_is_not_a_match(self):
        profiles = SpeakerProfiles(self.path)
        profiles.vectors["lukas"] = [1.0, 0.0]
        key, _score = profiles.match([0.2, 1.0])
        self.assertIsNone(key)


class IdentifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "speakers.json"
        self.profiles = SpeakerProfiles(self.path)

    def _identifier(self, table):
        return SpeakerIdentifier(
            profiles=self.profiles, embedder=_fake_embedder(table),
            sample_rate=SAMPLE_RATE,
        )

    def test_identifies_an_enrolled_voice(self):
        lukas = b"luka"
        bjoern = b"bjoe"
        self.profiles.vectors["lukas"] = [1.0, 0.0, 0.0]
        self.profiles.vectors["bjoern"] = [0.0, 1.0, 0.0]
        identifier = self._identifier({lukas: [0.98, 0.05, 0.0], bjoern: [0.1, 0.9, 0.0]})
        self.assertEqual("lukas", identifier.identify(lukas + LONG_PCM))
        self.assertEqual("bjoern", identifier.identify(bjoern + LONG_PCM))

    def test_unknown_voice_is_not_guessed(self):
        self.profiles.vectors["lukas"] = [1.0, 0.0, 0.0]
        identifier = self._identifier({b"xxxx": [0.0, 0.0, 1.0]})
        self.assertIsNone(identifier.identify(b"xxxx" + LONG_PCM))

    def test_short_audio_never_produces_an_identity(self):
        self.profiles.vectors["lukas"] = [1.0, 0.0]
        identifier = self._identifier({b"luka": [1.0, 0.0]})
        self.assertIsNone(identifier.identify(b"luka"))  # 4 bytes only

    def test_enrollment_stores_a_normalized_vector(self):
        identifier = self._identifier({b"luka": [3.0, 4.0]})
        self.assertEqual("lukas", identifier.enroll("Lukas", b"luka" + LONG_PCM))
        stored = json.loads(self.path.read_text())
        self.assertIn("lukas", stored["speakers"])
        self.assertAlmostEqual(0.6, stored["speakers"]["lukas"][0], places=6)

    def test_unavailable_model_degrades_to_no_identity(self):
        self.profiles.vectors["lukas"] = [1.0, 0.0]
        identifier = SpeakerIdentifier(profiles=self.profiles,
                                       model_path="/nonexistent/model.onnx")
        self.assertIsNone(identifier.identify(LONG_PCM))


if __name__ == "__main__":
    unittest.main()
