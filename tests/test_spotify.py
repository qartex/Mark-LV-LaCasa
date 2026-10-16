"""Tests for SpotifyAPIError, SpotifyHTTP, and token refresh (Task 3)."""
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.spotify import (
    OAuthRecord,
    OAuthStore,
    SpotifyAPIError,
    SpotifyHTTP,
    SpotifyOAuth,
    SpotifyAuthorizationRequired,
    REQUIRED_SCOPES,
    utc_now,
)


def _make_record(**overrides):
    # Use a future date so the six-month window is still open at test runtime
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    defaults = {
        "client_id": "test-client",
        "authorized_at": now,
        "authorization_expires_at": _six_months_from(now),
        "access_token": "old-token",
        "access_expires_at": now + timedelta(hours=2),
        "refresh_token": "old-refresh",
        "scopes": REQUIRED_SCOPES,
    }
    defaults.update(overrides)
    return OAuthRecord(**defaults)


def _six_months_from(dt):
    import calendar
    months = dt.month + 6
    year = dt.year + (months - 1) // 12
    month = (months - 1) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


class SpotifyAPIErrorTests(unittest.TestCase):
    def test_category_attribute(self):
        err = SpotifyAPIError("premium", "needs premium")
        self.assertEqual(err.category, "premium")
        self.assertIn("premium", str(err))

    def test_default_message(self):
        err = SpotifyAPIError("network")
        self.assertEqual(err.category, "network")
        self.assertEqual(str(err), "network")


class TokenRefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock(spec=OAuthStore)

    def test_access_token_returns_cached_when_not_expiring(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_expires_at=now + timedelta(hours=2),
        )
        oauth = SpotifyOAuth(self.store, clock=lambda: now)
        with patch.object(oauth._store, 'load', return_value=record):
            token = oauth.access_token()
        self.assertEqual(token, "old-token")

    def test_access_token_forces_refresh_when_flag_set(self):
        record = _make_record()
        new_record = _make_record(access_token="new-token")
        oauth = SpotifyOAuth(self.store)

        def mock_load():
            return record
        def mock_save(rec):
            pass

        with patch.object(oauth._store, 'load', side_effect=mock_load):
            with patch.object(oauth._store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', return_value=new_record):
                    token = oauth.access_token(force_refresh=True)
        self.assertEqual(token, "new-token")

    def test_access_token_raises_when_no_record(self):
        oauth = SpotifyOAuth(self.store)
        with patch.object(oauth._store, 'load', return_value=None):
            with self.assertRaises(SpotifyAuthorizationRequired):
                oauth.access_token()

    def test_access_token_raises_when_auth_expired(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=now - timedelta(days=1),
        )
        oauth = SpotifyOAuth(self.store)
        with patch.object(oauth._store, 'load', return_value=record):
            with patch.object(oauth._store, 'invalidate_tokens') as mock_inv:
                with self.assertRaises(SpotifyAuthorizationRequired):
                    oauth.access_token()
                mock_inv.assert_called_once()

    def test_refresh_rotates_token(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = _make_record(
            access_expires_at=now - timedelta(seconds=10),
        )
        new_record = _make_record(
            access_token="rotated-token",
            refresh_token="new-refresh",
            access_expires_at=now + timedelta(hours=1),
        )
        oauth = SpotifyOAuth(self.store)

        load_count = [0]
        def mock_load():
            load_count[0] += 1
            return record if load_count[0] == 1 else new_record

        saved = []
        def mock_save(rec):
            saved.append(rec)

        with patch.object(oauth._store, 'load', side_effect=mock_load):
            with patch.object(oauth._store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', return_value=new_record):
                    token = oauth.access_token()
        self.assertEqual(token, "rotated-token")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].access_token, "rotated-token")

    def test_refresh_preserves_old_refresh_when_not_rotated(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = _make_record(
            access_expires_at=now - timedelta(seconds=10),
        )
        new_record = _make_record(
            access_token="new-token",
            refresh_token=None,
            access_expires_at=now + timedelta(hours=1),
        )
        oauth = SpotifyOAuth(self.store)

        def mock_load():
            return record
        def mock_save(rec):
            pass

        with patch.object(oauth._store, 'load', side_effect=mock_load):
            with patch.object(oauth._store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', return_value=new_record):
                    token = oauth.access_token()
        self.assertEqual(token, "new-token")


class SpotifyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.oauth = MagicMock(spec=SpotifyOAuth)
        self.oauth.access_token.return_value = "test-token"

    def _make_client(self, **kwargs):
        defaults = {
            'oauth': self.oauth,
            'request': lambda method, url, **opts: self._mock_response(200),
            'sleep': lambda s: None,
        }
        defaults.update(kwargs)
        return SpotifyHTTP(**defaults)

    def _mock_response(self, status, text="", headers=None):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.headers = headers or {}
        try:
            resp.json.return_value = json.loads(text) if text else {}
        except json.JSONDecodeError:
            resp.json.return_value = {}
        return resp

    def test_success_returns_response(self):
        client = self._make_client()
        resp = client.request("GET", "/me")
        self.assertEqual(resp.status_code, 200)

    def test_401_raises_unauthorized(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(401)
        )
        self.oauth.invalidate_tokens = MagicMock()
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "unauthorized")

    def test_403_premium(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(
                403, '{"error": {"message": "Premium required"}}'
            )
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("PUT", "/me/player/pause")
        self.assertEqual(ctx.exception.category, "premium")

    def test_403_no_device(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(
                403, '{"error": {"message": "No active device found"}}'
            )
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("PUT", "/me/player/pause")
        self.assertEqual(ctx.exception.category, "no_device")

    def test_429_retries_once(self):
        responses = [
            self._mock_response(429, headers={"Retry-After": "1"}),
            self._mock_response(200),
        ]
        call_count = [0]
        def mock_request(*a, **k):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx] if idx < len(responses) else self._mock_response(500)

        sleeps = []
        client = self._make_client(request=mock_request, sleep=lambda s: sleeps.append(s))
        resp = client.request("GET", "/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0], 1.0)

    def test_429_invalid_retry_after_no_retry(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(429, headers={"Retry-After": "invalid"})
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "rate_limited")

    def test_429_clamps_retry_after_to_5(self):
        responses = [
            self._mock_response(429, headers={"Retry-After": "60"}),
            self._mock_response(200),
        ]
        call_count = [0]
        def mock_request(*a, **k):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx]

        sleeps = []
        client = self._make_client(request=mock_request, sleep=lambda s: sleeps.append(s))
        resp = client.request("GET", "/me")
        self.assertEqual(sleeps[0], 5.0)

    def test_network_error_raises(self):
        import requests
        client = self._make_client(
            request=lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("fail"))
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "network")

    def test_concurrent_access_token_uses_lock(self):
        """Two threads calling access_token should get same token after one refresh."""
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_expires_at=now - timedelta(seconds=10),
        )
        new_record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_token="new-token",
        )
        fake_store = MagicMock(spec=OAuthStore)
        oauth = SpotifyOAuth(fake_store, clock=lambda: now)

        refresh_count = [0]
        def mock_do_refresh(*args, **kwargs):
            refresh_count[0] += 1
            return new_record

        loaded = [False]
        def mock_load():
            if not loaded[0]:
                loaded[0] = True
                return record
            return new_record

        saved = []
        def mock_save(rec):
            saved.append(rec)

        with patch.object(fake_store, 'load', side_effect=mock_load):
            with patch.object(fake_store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', side_effect=mock_do_refresh):
                    results = []
                    errors = []
                    def worker():
                        try:
                            results.append(oauth.access_token())
                        except Exception as e:
                            errors.append(e)

                    t1 = threading.Thread(target=worker)
                    t2 = threading.Thread(target=worker)
                    t1.start()
                    t2.start()
                    t1.join()
                    t2.join()

        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], "new-token")
        self.assertEqual(results[1], "new-token")
        self.assertEqual(refresh_count[0], 1)



if __name__ == "__main__":
    unittest.main()


class ReceiverConfigConsistencyTests(unittest.TestCase):
    """Read only the public deployment template; assert the exact Web API target."""

    def test_raspotify_display_name_and_routing(self):
        from pathlib import Path
        conf = Path(__file__).resolve().parent.parent / "deploy" / "spotify" / "raspotify.conf"
        text = conf.read_text(encoding="utf-8")
        self.assertIn('LIBRESPOT_NAME="raspotify (lacasa-server)"', text)
        self.assertIn('LIBRESPOT_DEVICE="lacasa_spotify"', text)
        self.assertIn('LIBRESPOT_MIXER=softvol', text)
