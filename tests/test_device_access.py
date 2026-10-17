"""One-hour in-memory bearer sessions and durable dashboard login tests."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.device_credentials import DeviceCredentialConfigError, DeviceCredentialStore
from dashboard.access import AttemptLimiter, SessionRegistry
from dashboard.server import DashboardServer


class SessionRegistryTests(unittest.TestCase):
    def test_session_record_is_valid_before_and_invalid_at_absolute_expiry(self):
        now = [1_000.0]
        registry = SessionRegistry(ttl_seconds=3600, clock=lambda: now[0])
        record = registry.issue("device-a")

        now[0] = 4_599.999
        self.assertEqual(registry.authenticate(record.token), record)
        now[0] = 4_600.0
        self.assertIsNone(registry.authenticate(record.token))
        self.assertIsNone(registry.authenticate(record.token))

    def test_session_issue_generates_fresh_bearer_and_encryption_key(self):
        registry = SessionRegistry(clock=lambda: 1_000.0)
        first = registry.issue("device-a")
        second = registry.issue("device-a")

        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.session_key, second.session_key)
        self.assertEqual(first.device_id, "device-a")
        self.assertEqual(second.device_id, "device-a")
        self.assertEqual(first.expires_at - first.created_at, 3600)
        self.assertNotIn("device-a", vars(registry))

    def test_registry_revokes_only_matching_device_or_transient_sessions(self):
        registry = SessionRegistry(clock=lambda: 1_000.0)
        first = registry.issue("device-a")
        second = registry.issue("device-a")
        other = registry.issue("device-b")
        transient = registry.issue(None)

        self.assertEqual(registry.revoke_device("device-a"), {first.token, second.token})
        self.assertIsNone(registry.authenticate(first.token))
        self.assertIsNone(registry.authenticate(second.token))
        self.assertEqual(registry.authenticate(other.token), other)
        self.assertEqual(registry.authenticate(transient.token), transient)
        self.assertEqual(registry.revoke_transient(), {transient.token})
        self.assertEqual(registry.authenticate(other.token), other)
        self.assertIsNone(registry.authenticate(transient.token))

    def test_rate_limit_has_independent_ten_per_minute_buckets(self):
        now = [100.0]
        limiter = AttemptLimiter(clock=lambda: now[0])

        for _ in range(10):
            self.assertTrue(limiter.allow("transient-login"))
        self.assertFalse(limiter.allow("transient-login"))
        for _ in range(10):
            self.assertTrue(limiter.allow("durable-login"))
        self.assertFalse(limiter.allow("durable-login"))
        now[0] += 60
        self.assertTrue(limiter.allow("transient-login"))
        self.assertTrue(limiter.allow("durable-login"))


class DurableDeviceLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o700)
        self.path = root / "devices.json"
        self.now = [1_000.0]
        self.monotonic = [100.0]
        self.store = DeviceCredentialStore(self.path, clock=lambda: self.now[0])
        self.device = self.store.create("LaCasa iPhone")
        self.server = self._server(self.store)
        self.client = TestClient(self.server.app)

    def _server(self, store):
        return DashboardServer(
            public_url="https://example.test",
            host="127.0.0.1",
            device_store=store,
            bearer_ttl_seconds=3600,
            wall_clock=lambda: self.now[0],
            monotonic_clock=lambda: self.monotonic[0],
        )

    def _login(self, client=None, device=None):
        client = client or self.client
        device = device or self.device
        return client.post(
            "/api/device-login",
            json={"device_id": device.summary.device_id, "secret": device.secret},
        )

    def test_device_login_rejects_malformed_unknown_wrong_and_revoked_identically(self):
        malformed = self.client.post("/api/device-login", content=b"[")
        wrong_type = self.client.post("/api/device-login", json={"device_id": 1, "secret": 2})
        wrong_secret = ("A" if self.device.secret[0] != "A" else "B") + self.device.secret[1:]
        wrong = self.client.post("/api/device-login", json={
            "device_id": self.device.summary.device_id,
            "secret": wrong_secret,
        })
        unknown = self.client.post("/api/device-login", json={
            "device_id": "A" * len(self.device.summary.device_id),
            "secret": self.device.secret,
        })
        revoked_device = self.store.create("LaCasa iPad")
        self.store.revoke(revoked_device.summary.device_id)
        revoked = self._login(device=revoked_device)

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(wrong_type.status_code, 400)
        self.assertEqual({wrong.status_code, unknown.status_code, revoked.status_code}, {401})
        self.assertEqual(wrong.content, unknown.content)
        self.assertEqual(wrong.content, revoked.content)

    def test_device_login_rejects_malformed_credential_encodings_before_verification(self):
        class StoreWithoutMalformedVerifier:
            def __init__(self):
                self.calls = []

            def list_devices(self):
                return []

            def verify(self, device_id, secret):
                self.calls.append((device_id, secret))
                return None

        store = StoreWithoutMalformedVerifier()
        client = TestClient(self._server(store).app)
        valid_id = self.device.summary.device_id
        valid_secret = self.device.secret
        malformed = {
            "invalid base64url device_id": {"device_id": "!" * len(valid_id), "secret": valid_secret},
            "invalid base64url secret": {"device_id": valid_id, "secret": "!" * len(valid_secret)},
            "wrong-length device_id": {"device_id": valid_id[:-1], "secret": valid_secret},
            "wrong-length secret": {"device_id": valid_id, "secret": valid_secret[:-1]},
            "non-ASCII device_id": {"device_id": "é" * len(valid_id), "secret": valid_secret},
            "non-ASCII secret": {"device_id": valid_id, "secret": "é" * len(valid_secret)},
        }

        for description, body in malformed.items():
            with self.subTest(description=description):
                response = client.post("/api/device-login", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"ok": False})
                self.assertEqual(store.calls, [])

    def test_corrupt_credential_store_is_rejected_at_dashboard_construction(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        self.path.chmod(0o600)

        with self.assertRaises(DeviceCredentialConfigError):
            self._server(DeviceCredentialStore(self.path, clock=lambda: self.now[0]))

    def test_device_login_returns_fresh_session_material_without_secret_disclosure(self):
        first = self._login()
        second = self._login()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(set(first.json()), {"ok", "token", "key", "expires_in"})
        self.assertTrue(first.json()["ok"])
        self.assertEqual(first.json()["expires_in"], 3600)
        self.assertNotEqual(first.json()["token"], second.json()["token"])
        self.assertNotEqual(first.json()["key"], second.json()["key"])
        self.assertNotIn(self.device.secret, first.text)
        self.assertNotIn(self.device.secret, second.text)

    def test_durable_login_survives_new_dashboard_server_with_same_store(self):
        initial = self._login().json()
        replacement = self._server(DeviceCredentialStore(self.path, clock=lambda: self.now[0]))
        replacement_client = TestClient(replacement.app)
        resumed = self._login(replacement_client)

        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(replacement_client.get(
            "/api/status", headers={"Authorization": f"Bearer {initial['token']}"}
        ).status_code, 401)

    def test_http_bearer_expires_at_one_hour_without_sliding(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        headers = {"Authorization": f"Bearer {response.json()['token']}"}
        self.now[0] += 3_599.999
        self.assertEqual(self.client.get("/api/status", headers=headers).status_code, 200)
        self.now[0] += 0.001

        self.assertEqual(self.client.get("/api/status", headers=headers).status_code, 401)
        self.assertEqual(self.client.post("/api/command", headers=headers, json={"text": "x"}).status_code, 401)
        self.assertEqual(self.client.post("/api/interrupt", headers=headers).status_code, 401)

    def test_durable_login_rate_limit_does_not_consume_transient_pin_budget(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/login", json={"pin": "WRONG"}).status_code, 401)
        self.assertEqual(self.client.post("/login", json={"pin": "WRONG"}).status_code, 429)

        self.assertEqual(self._login().status_code, 200)

    def test_no_raw_server_secret_or_session_material_is_written_to_store(self):
        before = self.path.read_bytes()
        response = self._login()
        after = self.path.read_bytes()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(after, before)
        self.assertNotIn(self.device.secret.encode(), after)
        self.assertNotIn(response.json()["token"].encode(), after)
        self.assertNotIn(response.json()["key"].encode(), after)
        self.assertNotIn(b"session_key", after)
        self.assertEqual(json.loads(after)["version"], 1)

    def test_transient_pair_does_not_change_store_or_durable_bearers(self):
        durable = self._login().json()
        before = self.path.read_bytes()
        key = self.server.new_key()
        transient = self.client.post("/login", json={"pin": key}).json()

        self.server.new_key()

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.get(
            "/api/status", headers={"Authorization": f"Bearer {durable['token']}"}
        ).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/status", headers={"Authorization": f"Bearer {transient['token']}"}
        ).status_code, 401)

    def test_revocation_wins_a_concurrent_relogin_and_cannot_resurrect_device(self):
        active = self._login().json()

        summary = asyncio.run(self.server.revoke_device(self.device.summary.device_id))
        relogin = self._login()

        self.assertEqual(summary.device_id, self.device.summary.device_id)
        self.assertIsNone(self.server._sessions.authenticate(active["token"]))
        self.assertIsNone(self.store.verify(self.device.summary.device_id, self.device.secret))
        self.assertEqual(relogin.status_code, 401)


if __name__ == "__main__":
    unittest.main()
