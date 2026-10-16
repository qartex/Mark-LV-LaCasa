"""RED/GREEN tests for the private Spotify OAuth store (Task 1)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core.spotify import (
    OAuthRecord,
    OAuthStore,
    REQUIRED_SCOPES,
    REDIRECT_URI,
    SpotifyConfigError,
    TARGET_DEVICE_NAME,
    _six_calendar_months,
)


def authorized_record(authorized_at="2026-08-31T12:00:00Z"):
    start = datetime.fromisoformat(authorized_at.replace("Z", "+00:00"))
    return OAuthRecord(
        client_id="SpotifyClientID",
        authorized_at=start,
        authorization_expires_at=_six_calendar_months(start),
        access_token="old-access",
        access_expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        refresh_token="old-refresh",
        scopes=(
            "user-read-playback-state",
            "user-read-currently-playing",
            "user-modify-playback-state",
        ),
    )


def write_store(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


class SpotifyOAuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_dir = Path(self.tmp.name) / "config"
        self.cfg_dir.mkdir()
        self.store_path = self.cfg_dir / "spotify_oauth.json"

    def test_constants_are_exact(self):
        self.assertEqual(
            REDIRECT_URI,
            "https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback",
        )
        self.assertEqual(TARGET_DEVICE_NAME, "raspotify (lacasa-server)")
        self.assertEqual(
            REQUIRED_SCOPES,
            (
                "user-read-playback-state",
                "user-read-currently-playing",
                "user-modify-playback-state",
            ),
        )

    def test_store_round_trip_is_private_and_six_calendar_months(self):
        store = OAuthStore(self.store_path)
        store.save(authorized_record())
        loaded = store.load()
        self.assertEqual(
            loaded.authorization_expires_at.isoformat(),
            "2027-02-28T12:00:00+00:00",
        )
        self.assertEqual(self.store_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("client_secret", self.store_path.read_text())
        self.assertEqual(loaded.client_id, "SpotifyClientID")

    def test_missing_file_is_not_authorized(self):
        self.assertIsNone(OAuthStore(self.store_path).load())

    def test_unsafe_mode_fails_closed(self):
        write_store(self.store_path, {})
        os.chmod(self.store_path, 0o644)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_symlinked_store_fails_closed(self):
        real = self.cfg_dir / "real.json"
        real.write_text("{}")
        os.symlink(real, self.store_path)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_symlinked_parent_fails_closed(self):
        real_dir = Path(self.tmp.name) / "realdir"
        real_dir.mkdir()
        link_dir = self.cfg_dir.parent / "linkdir"
        link_dir.symlink_to(real_dir)
        bad_path = link_dir / "config" / "spotify_oauth.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("{}")
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(bad_path).load()

    def test_corrupt_json_fails_closed(self):
        self.store_path.write_text("{not json")
        os.chmod(self.store_path, 0o600)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_invalid_timestamps_fails_closed(self):
        data = {
            "client_id": "SpotifyClientID",
            "authorized_at": "2026-13-99T12:00:00Z",
            "authorization_expires_at": "2027-02-28T12:00:00+00:00",
            "access_token": "old-access",
            "access_expires_at": "2026-09-01T12:00:00+00:00",
            "refresh_token": "old-refresh",
            "scopes": list(REQUIRED_SCOPES),
        }
        write_store(self.store_path, data)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_missing_required_scopes_fails_closed(self):
        data = {
            "client_id": "SpotifyClientID",
            "authorized_at": "2026-08-31T12:00:00Z",
            "authorization_expires_at": "2027-02-28T12:00:00+00:00",
            "access_token": "old-access",
            "access_expires_at": "2026-09-01T12:00:00+00:00",
            "refresh_token": "old-refresh",
            "scopes": ["user-read-playback-state"],
        }
        write_store(self.store_path, data)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_invalidation_is_atomic_and_secret_free(self):
        store = OAuthStore(self.store_path)
        store.save(authorized_record())
        with mock.patch("os.replace", side_effect=OSError("boom")):
            result = store.invalidate_tokens()
        self.assertIsNone(result)
        self.assertTrue(self.store_path.exists())
        # A failed atomic write must leave the previous valid record intact.
        loaded = store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.access_token, "old-access")
        self.assertEqual(loaded.refresh_token, "old-refresh")
        self.assertEqual(loaded.client_id, "SpotifyClientID")
        self.assertEqual(
            loaded.authorization_expires_at.isoformat(),
            "2027-02-28T12:00:00+00:00",
        )
        self.assertEqual(len(loaded.scopes), 3)
        # A successful invalidation clears only the three token fields.
        store.invalidate_tokens()
        cleared = store.load()
        self.assertIsNotNone(cleared)
        self.assertIsNone(cleared.access_token)
        self.assertIsNone(cleared.refresh_token)
        self.assertIsNone(cleared.access_expires_at)
        self.assertEqual(cleared.client_id, "SpotifyClientID")
        self.assertEqual(
            cleared.authorization_expires_at.isoformat(),
            "2027-02-28T12:00:00+00:00",
        )
        self.assertEqual(len(cleared.scopes), 3)


if __name__ == "__main__":
    unittest.main()
