"""RED/GREEN tests for the private Spotify OAuth store (Task 1)."""
import json
import logging
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from core.spotify import (
    OAuthRecord,
    OAuthStore,
    REQUIRED_SCOPES,
    REDIRECT_URI,
    SpotifyConfigError,
    TARGET_DEVICE_NAME,
)


ACCESS_SECRET = "access-secret-7d21c5"
REFRESH_SECRET = "refresh-secret-2e199a"


def valid_store_data(**overrides):
    data = {
        "client_id": "SpotifyClientID",
        "authorized_at": "2026-08-31T12:00:00+00:00",
        "authorization_expires_at": "2027-02-28T12:00:00+00:00",
        "access_token": ACCESS_SECRET,
        "access_expires_at": "2026-09-01T12:00:00+00:00",
        "refresh_token": REFRESH_SECRET,
        "scopes": list(REQUIRED_SCOPES),
    }
    data.update(overrides)
    return data


def authorized_record(authorized_at="2026-08-31T12:00:00Z"):
    start = datetime.fromisoformat(authorized_at.replace("Z", "+00:00"))
    return OAuthRecord(
        client_id="SpotifyClientID",
        authorized_at=start,
        authorization_expires_at=datetime(2027, 2, 28, 12, 0, tzinfo=timezone.utc),
        access_token=ACCESS_SECRET,
        access_expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        refresh_token=REFRESH_SECRET,
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

    def test_broken_store_symlink_fails_closed(self):
        self.store_path.symlink_to(self.cfg_dir / "missing.json")
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_symlinked_ancestor_with_private_store_fails_closed(self):
        real_dir = Path(self.tmp.name) / "realdir"
        real_dir.mkdir()
        link_dir = self.cfg_dir.parent / "linkdir"
        link_dir.symlink_to(real_dir)
        bad_path = link_dir / "config" / "spotify_oauth.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        write_store(bad_path, valid_store_data())
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(bad_path).load()

    def test_directory_at_store_path_maps_to_config_error(self):
        self.store_path.mkdir()
        os.chmod(self.store_path, 0o600)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_foreign_file_owner_fails_closed(self):
        write_store(self.store_path, valid_store_data())
        real_fstat = os.fstat

        def foreign_regular_file(fd):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                values = list(result)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return result

        with mock.patch("core.spotify.os.fstat", side_effect=foreign_regular_file):
            with self.assertRaises(SpotifyConfigError):
                OAuthStore(self.store_path).load()

    def test_load_uses_opened_inode_when_path_is_swapped_after_validation(self):
        write_store(self.store_path, valid_store_data())
        malicious = self.cfg_dir / "malicious.json"
        write_store(malicious, valid_store_data(client_id="AttackerClientID"))
        displaced = self.cfg_dir / "displaced.json"
        real_islink = os.path.islink
        real_fstat = os.fstat
        swapped = False

        def swap_path():
            nonlocal swapped
            if not swapped:
                self.store_path.rename(displaced)
                self.store_path.symlink_to(malicious)
                swapped = True

        def racing_islink(path):
            result = real_islink(path)
            if Path(path) == self.store_path:
                swap_path()
            return result

        def racing_fstat(fd):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                swap_path()
            return result

        with mock.patch("core.spotify.os.path.islink", side_effect=racing_islink), mock.patch(
            "core.spotify.os.fstat", side_effect=racing_fstat
        ):
            loaded = OAuthStore(self.store_path).load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.client_id, "SpotifyClientID")

    def test_save_rejects_existing_destination_symlink(self):
        real = self.cfg_dir / "real.json"
        real.write_text("sentinel")
        self.store_path.symlink_to(real)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).save(authorized_record())
        self.assertTrue(self.store_path.is_symlink())
        self.assertEqual(real.read_text(), "sentinel")

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

    def test_load_requires_every_exact_field(self):
        for field_name in valid_store_data():
            with self.subTest(field_name=field_name):
                data = valid_store_data()
                del data[field_name]
                write_store(self.store_path, data)
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).load()

    def test_load_rejects_unknown_fields(self):
        write_store(self.store_path, valid_store_data(client_secret="must-not-be-here"))
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_load_rejects_wrong_field_types_as_config_errors(self):
        wrong_values = {
            "client_id": 123,
            "authorized_at": 123,
            "authorization_expires_at": [],
            "access_token": 123,
            "access_expires_at": {},
            "refresh_token": ["token"],
            "scopes": [list(REQUIRED_SCOPES)],
        }
        for field_name, wrong_value in wrong_values.items():
            with self.subTest(field_name=field_name):
                write_store(self.store_path, valid_store_data(**{field_name: wrong_value}))
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).load()

    def test_load_rejects_non_utc_offsets(self):
        cases = (
            {
                "authorized_at": "2026-08-31T13:00:00+01:00",
                "authorization_expires_at": "2027-02-28T13:00:00+01:00",
            },
            {"access_expires_at": "2026-09-01T13:00:00+01:00"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                write_store(self.store_path, valid_store_data(**overrides))
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).load()

    def test_load_rejects_duplicate_scopes(self):
        duplicate_scopes = list(REQUIRED_SCOPES) + [REQUIRED_SCOPES[0]]
        write_store(self.store_path, valid_store_data(scopes=duplicate_scopes))
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_save_validates_record_types_utc_and_six_month_deadline(self):
        plus_one = timezone(timedelta(hours=1))
        invalid_records = (
            "not-an-oauth-record",
            replace(authorized_record(), access_token=123),
            replace(
                authorized_record(),
                authorized_at=datetime(2026, 8, 31, 13, 0, tzinfo=plus_one),
            ),
            replace(
                authorized_record(),
                authorization_expires_at=datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc),
            ),
            replace(authorized_record(), scopes=list(REQUIRED_SCOPES)),
        )
        for record in invalid_records:
            with self.subTest(record=record):
                self.store_path.unlink(missing_ok=True)
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).save(record)  # type: ignore[arg-type]
                self.assertFalse(self.store_path.exists())

    def test_config_errors_and_logs_never_include_token_values(self):
        write_store(self.store_path, valid_store_data(authorized_at=ACCESS_SECRET))
        log_output = StringIO()
        handler = logging.StreamHandler(log_output)
        spotify_logger = logging.getLogger("core.spotify")
        spotify_logger.addHandler(handler)
        self.addCleanup(spotify_logger.removeHandler, handler)

        with self.assertRaises(SpotifyConfigError) as raised:
            OAuthStore(self.store_path).load()

        combined = f"{raised.exception}\n{log_output.getvalue()}"
        self.assertNotIn(ACCESS_SECRET, combined)
        self.assertNotIn(REFRESH_SECRET, combined)

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

    def test_invalidation_write_error_propagates_and_preserves_old_file(self):
        store = OAuthStore(self.store_path)
        store.save(authorized_record())

        with mock.patch("os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError) as raised:
                store.invalidate_tokens()

        self.assertNotIn(ACCESS_SECRET, str(raised.exception))
        self.assertNotIn(REFRESH_SECRET, str(raised.exception))
        loaded = store.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.access_token, ACCESS_SECRET)
        self.assertEqual(loaded.refresh_token, REFRESH_SECRET)
        self.assertEqual(loaded.client_id, "SpotifyClientID")
        self.assertEqual(
            loaded.authorization_expires_at.isoformat(),
            "2027-02-28T12:00:00+00:00",
        )
        self.assertEqual(loaded.scopes, REQUIRED_SCOPES)

    def test_invalidation_clears_only_token_fields(self):
        store = OAuthStore(self.store_path)
        original = authorized_record()
        store.save(original)

        result = store.invalidate_tokens()

        self.assertIsNotNone(result)
        cleared = store.load()
        self.assertIsNotNone(cleared)
        assert cleared is not None
        self.assertIsNone(cleared.access_token)
        self.assertIsNone(cleared.refresh_token)
        self.assertIsNone(cleared.access_expires_at)
        self.assertEqual(cleared.client_id, original.client_id)
        self.assertEqual(cleared.authorized_at, original.authorized_at)
        self.assertEqual(
            cleared.authorization_expires_at,
            original.authorization_expires_at,
        )
        self.assertEqual(cleared.scopes, original.scopes)

    def test_invalidation_missing_store_returns_none(self):
        self.assertIsNone(OAuthStore(self.store_path).invalidate_tokens())


if __name__ == "__main__":
    unittest.main()
