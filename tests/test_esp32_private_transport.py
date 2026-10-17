"""Private-transport contract tests for phase-1 ESP32 speaker (plan Task 3).

Covers scripts/esp32_private_relay.py, scripts/esp32_test_audio_server.py and
scripts/esp32_api_acceptance.py. Pure stdlib. Network behaviour is exercised on
real loopback sockets: the Mac Tailscale IPv4 cannot exist in this environment,
so the module-level Tailscale reader and route-source prober are monkeypatched
to loopback-derived addresses and every bind/forward/HTTP assertion uses real
TCP connections. No physical device, no USB, no SSH and no Home Assistant call
is ever made, and no real MAC, LAN host or secret appears anywhere.
"""

import contextlib
import http.client
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import wave
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import esp32_private_relay as relay  # noqa: E402
import esp32_test_audio_server as audio_server  # noqa: E402
import esp32_api_acceptance as acceptance  # noqa: E402

# Obviously fake identifiers (RFC 5737 ranges + invented MACs).
TAILSCALE_FAKE = "10.9.8.7"  # private, but NOT inside the Tailscale 100.64/10 range
CGNAT_LOOKALIKE = "100.117.85.9"  # tailscale-shaped range, yet refused as a board target
LAN_BOARD = "192.168.37.51"
LAN_OTHER = "192.168.37.99"
PUBLIC_DOC = "203.0.113.5"
LOOPBACK = "127.0.0.1"
MAC_EXPECTED = "AA:BB:CC:DD:EE:F0"
MAC_OTHER = "de:ad:be:ef:00:01"
KEY_MARKER = "TEST-API-KEY-LEAK-MARKER-3f9a"
PORT_BOUNCE = "8123"  # forbidden token: acceptance client must never touch HA

LOG_CAPTURED_NAMES = ("esp32_private_relay", "esp32_test_audio_server", "esp32_api_acceptance")


# --------------------------------------------------------------------------- helpers

class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(repr(record.msg))


@contextlib.contextmanager
def captured_logs(name):
    logger = logging.getLogger(name)
    handler = _CaptureHandler()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


def _listener_count(port):
    """Count OS LISTEN sockets on *port* via /proc (0 when /proc is absent)."""
    matches = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            rows = fh.read().splitlines()[1:]
        for row in rows:
            cols = row.split()
            if len(cols) < 4:
                continue
            if cols[3] == "0A" and int(cols[1].split(":")[1], 16) == port:
                matches += 1
    return matches


def _wait(predicate, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return predicate()


class _EchoServer:
    """Real two-way TCP upstream on a loopback port; echoes received bytes."""

    def __init__(self, host=LOOPBACK, port=0):
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.bind((host, port))
        self._listen.listen(8)
        self.port = self._listen.getsockname()[1]
        self._conns = []
        self._thread = None

    def start(self):
        def accept_loop():
            try:
                while True:
                    conn, _ = self._listen.accept()
                    self._conns.append(conn)

                    def pump(c=conn):
                        try:
                            c.settimeout(20)
                            while True:
                                data = c.recv(65536)
                                if not data:
                                    break
                                c.sendall(data)
                        except OSError:
                            pass
                        finally:
                            try:
                                c.close()
                            except OSError:
                                pass

                    t = threading.Thread(target=pump, daemon=True)
                    t.start()
            except OSError:
                pass

        self._thread = threading.Thread(target=accept_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._listen.close()
        except OSError:
            pass


def _connect(host, port, timeout=5.0):
    return socket.create_connection((host, port), timeout=timeout)


def _send_and_read(sock, payload: bytes, read_bytes: int, timeout=10.0):
    sock.settimeout(timeout)
    sock.sendall(payload)
    chunks = []
    remaining = read_bytes
    while remaining > 0:
        data = sock.recv(min(65536, remaining))
        if not data:
            break
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _drain(sock, timeout=3.0):
    """Read until EOF or timeout; returns all bytes."""
    sock.settimeout(timeout)
    chunks = []
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    except (socket.timeout, TimeoutError):
        pass
    return b"".join(chunks)


def _make_wav(directory: Path, name: str = "tone.wav") -> Path:
    path = directory / name
    return audio_server.generate_test_wav(path)


# --------------------------------------------------------------------------- Tailscale discovery

class TailscaleDiscoveryTests(unittest.TestCase):
    """current_tailscale_ipv4(): derived from local state, callers cannot inject trust."""

    def test_returns_ipv4_from_local_state(self):
        with unittest.mock.patch.object(
            relay, "_tailscale_cli_output", return_value="100.64.101.102\n"
        ) as cli:
            self.assertEqual(relay.current_tailscale_ipv4(), ipv4("100.64.101.102"))
        cli.assert_called_once()
        argv = cli.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(t, str) for t in argv))

    def test_takes_no_trust_list_parameter(self):
        sig = inspect.signature(relay.current_tailscale_ipv4)
        self.assertEqual(list(sig.parameters), [])

    def test_falls_back_to_macos_app_cli_when_standard_cli_missing(self):
        with unittest.mock.patch.object(
            relay,
            "_tailscale_cli_output",
            side_effect=[
                relay.TailscaleStateError("tailscale state unavailable"),
                "100.64.101.103\n",
            ],
        ) as cli:
            self.assertEqual(relay.current_tailscale_ipv4(), ipv4("100.64.101.103"))
        self.assertEqual(2, cli.call_count)
        self.assertIn("/Applications/Tailscale.app", cli.call_args.args[0][0])

    def test_fails_closed_when_every_candidate_is_unavailable(self):
        with unittest.mock.patch.object(
            relay,
            "_tailscale_cli_output",
            side_effect=relay.TailscaleStateError("tailscale state unavailable"),
        ) as cli:
            with self.assertRaises(relay.TailscaleStateError):
                relay.current_tailscale_ipv4()
        self.assertEqual(len(relay._TAILSCALE_CANDIDATES), cli.call_count)

    def test_default_preflight_runner_sends_dialer_on_stdin(self):
        runner_args = inspect.signature(relay._default_preflight_runner).parameters
        self.assertEqual(list(runner_args), ["tailscale_ip", "bind_port"])
        with unittest.mock.patch.object(
            relay.subprocess, "run", return_value=unittest.mock.Mock(returncode=0)
        ) as run:
            self.assertTrue(relay._default_preflight_runner("100.64.101.104", 16053))
        argv = run.call_args.args[0]
        self.assertIn("python3", argv)
        self.assertEqual("-", argv[argv.index("python3") + 1])
        self.assertNotIn("-c", argv)
        self.assertTrue(run.call_args.kwargs.get("input", b""))

    def test_refuses_wildcard_output(self):
        with unittest.mock.patch.object(relay, "_tailscale_cli_output", return_value="0.0.0.0\n"):
            with self.assertRaises(relay.TailscaleStateError):
                relay.current_tailscale_ipv4()

    def test_refuses_public_output(self):
        with unittest.mock.patch.object(relay, "_tailscale_cli_output", return_value=f"{PUBLIC_DOC}\n"):
            with self.assertRaises(relay.TailscaleStateError):
                relay.current_tailscale_ipv4()

    def test_refuses_non_tailscale_private_output(self):
        with unittest.mock.patch.object(
            relay, "_tailscale_cli_output", return_value=f"{TAILSCALE_FAKE}\n"
        ):
            with self.assertRaises(relay.TailscaleStateError):
                relay.current_tailscale_ipv4()

    def test_refuses_multiple_or_empty_output(self):
        for raw in ("100.64.0.1\n100.64.0.2\n", "", "\n"):
            with self.subTest(raw=raw):
                with unittest.mock.patch.object(relay, "_tailscale_cli_output", return_value=raw):
                    with self.assertRaises(relay.TailscaleStateError):
                        relay.current_tailscale_ipv4()

    def test_refuses_garbage_output(self):
        with unittest.mock.patch.object(relay, "_tailscale_cli_output", return_value="not-an-ip\n"):
            with self.assertRaises(relay.TailscaleStateError):
                relay.current_tailscale_ipv4()

    def test_refuses_when_local_state_unreadable_and_stays_bounded(self):
        def broken(argv):
            raise RuntimeError("state backend exploded with secret payload")

        with unittest.mock.patch.object(relay, "_tailscale_cli_output", side_effect=broken):
            with self.assertRaises(relay.TailscaleStateError) as ctx:
                relay.current_tailscale_ipv4()
        message = str(ctx.exception)
        self.assertLess(len(message), 200)
        self.assertNotIn("secret", message.lower())


def ipv4(text):
    import ipaddress

    return ipaddress.IPv4Address(text)


# --------------------------------------------------------------------------- board validation

class BoardTargetTests(unittest.TestCase):
    """validate_board_target(): private home-LAN + ARP/neighbor MAC match only."""

    @staticmethod
    def _neighbor(value):
        return lambda ip: value

    def test_accepts_private_lan_with_matching_neighbor_mac(self):
        seen = []
        reader = lambda ip: seen.append(ip) or MAC_EXPECTED.lower()
        result = relay.validate_board_target(LAN_BOARD, MAC_EXPECTED, neighbor_reader=reader)
        self.assertEqual(result, ipv4(LAN_BOARD))
        self.assertEqual(seen, [LAN_BOARD])

    def test_rejects_mac_mismatch(self):
        with self.assertRaises(relay.ValidateTargetError):
            relay.validate_board_target(LAN_BOARD, MAC_EXPECTED, neighbor_reader=self._neighbor(MAC_OTHER))

    def test_rejects_missing_neighbor_entry(self):
        for value in (None, ""):
            with self.subTest(value=value):
                with self.assertRaises(relay.ValidateTargetError):
                    relay.validate_board_target(LAN_BOARD, MAC_EXPECTED, neighbor_reader=self._neighbor(value))

    def test_rejects_public_loopback_and_special_targets(self):
        refused = [
            PUBLIC_DOC,
            LOOPBACK,
            "169.254.9.9",
            CGNAT_LOOKALIKE,
            "224.0.0.5",
            "0.0.0.0",
            "255.255.255.255",
        ]
        for address in refused:
            with self.subTest(address=address):
                with self.assertRaises(relay.ValidateTargetError):
                    relay.validate_board_target(
                        address, MAC_EXPECTED, neighbor_reader=self._neighbor(MAC_EXPECTED)
                    )

    def test_rejects_malformed_ip_or_mac(self):
        bad_ips = ["192.168.1", "192.168.1.256", ""]
        bad_macs = ["gg:hh:ii:jj:kk:ll", "aabbccddeeff", "11:22:33:44:55:66:77"]
        for address in bad_ips:
            with self.subTest(ip=address):
                with self.assertRaises(relay.ValidateTargetError):
                    relay.validate_board_target(
                        address, MAC_EXPECTED, neighbor_reader=self._neighbor(MAC_EXPECTED)
                    )
        for mac in bad_macs:
            with self.subTest(mac=mac):
                with self.assertRaises(relay.ValidateTargetError):
                    relay.validate_board_target(
                        LAN_BOARD, mac, neighbor_reader=self._neighbor(mac)
                    )

    def test_compares_mac_case_and_separator_insensitively(self):
        reader = self._neighbor("aa-bb-cc-dd-ee-f0")
        result = relay.validate_board_target(LAN_BOARD, "AA:BB:CC:DD:EE:F0", neighbor_reader=reader)
        self.assertEqual(result, ipv4(LAN_BOARD))

    def test_mac_required_positionally_without_bypass(self):
        sig = inspect.signature(relay.validate_board_target)
        for name in ("ip", "expected_mac"):
            self.assertIn(name, sig.parameters)
            self.assertEqual(sig.parameters[name].default, inspect.Parameter.empty)

    def test_default_neighbor_reader_is_module_level_and_used(self):
        with unittest.mock.patch.object(relay, "neighbor_mac_for", return_value=MAC_EXPECTED) as reader:
            result = relay.validate_board_target(LAN_OTHER, MAC_EXPECTED)
        self.assertEqual(result, ipv4(LAN_OTHER))
        reader.assert_called_once_with(LAN_OTHER)


# --------------------------------------------------------------------------- relay

class RelayTests(unittest.TestCase):
    """run_relay(): fresh Tailscale-only bind, bounded forwarding, no residue."""

    def test_signature_cannot_inject_bind_or_trust(self):
        sig = inspect.signature(relay.run_relay)
        self.assertEqual(list(sig.parameters), ["target_ip", "target_port", "bind_port"])
        self.assertEqual(sig.parameters["target_port"].default, 6053)
        self.assertEqual(sig.parameters["bind_port"].default, 16053)

    def test_binds_fresh_reader_address_and_forwards_bidirectionally(self):
        echo = _EchoServer().start()
        bind_port = _free_port()
        handle = None
        payload = b"PAYLOAD-MARKER-77aa9" + Random(20261017).randbytes(200_000)
        try:
            with unittest.mock.patch.object(
                relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
            ):
                handle = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=bind_port)
            self.assertEqual(handle.bind_address, ipv4(LOOPBACK))
            self.assertEqual(handle.bind_port, bind_port)
            with _connect(LOOPBACK, handle.bind_port) as sock:
                echoed = _send_and_read(sock, payload, len(payload))
            self.assertEqual(echoed, payload)
        finally:
            if handle:
                handle.close()
            echo.stop()

    def test_rebinds_freshly_discovered_address_on_each_call(self):
        echo = _EchoServer().start()
        port_a, port_b = _free_port(), _free_port()
        reader = unittest.mock.Mock(
            side_effect=[ipv4(LOOPBACK), ipv4("127.0.0.2")]
        )
        handles = []
        try:
            with unittest.mock.patch.object(relay, "current_tailscale_ipv4", reader):
                first = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=port_a)
                second = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=port_b)
            handles = [first, second]
            self.assertEqual(first.bind_address, ipv4(LOOPBACK))
            self.assertEqual(second.bind_address, ipv4("127.0.0.2"))
            with _connect("127.0.0.2", second.bind_port) as sock:
                self.assertEqual(_send_and_read(sock, b"ping-stale", 10), b"ping-stale")
        finally:
            for handle in handles:
                handle.close()
            echo.stop()

    def test_refuses_wildcard_bind_without_listener(self):
        bind_port = _free_port()
        with unittest.mock.patch.object(relay, "current_tailscale_ipv4", return_value=ipv4("0.0.0.0")):
            with self.assertRaises(relay.BindPolicyError):
                relay.run_relay(LOOPBACK, bind_port=bind_port)
        self.assertEqual(_listener_count(bind_port), 0)
        with self.assertRaises(OSError):
            _connect(LOOPBACK, bind_port, timeout=2)

    def test_close_leaves_zero_listeners_and_sockets(self):
        echo = _EchoServer().start()
        bind_port = _free_port()
        try:
            with unittest.mock.patch.object(
                relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
            ):
                handle = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=bind_port)
            self.assertEqual(_listener_count(bind_port), 1)
            handle.close()
            self.assertTrue(_wait(lambda: handle.socket_count == 0), "expected all sockets closed")
            self.assertTrue(_wait(lambda: _listener_count(bind_port) == 0), "listener still present")
            with self.assertRaises(OSError):
                _connect(LOOPBACK, bind_port, timeout=2)
        finally:
            echo.stop()

    def test_client_disconnect_cleans_up(self):
        echo = _EchoServer().start()
        bind_port = _free_port()
        handle = None
        try:
            with unittest.mock.patch.object(
                relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
            ):
                handle = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=bind_port)
            sock = _connect(LOOPBACK, handle.bind_port)
            sock.close()
            self.assertTrue(
                _wait(lambda: handle.socket_count == 1),
                "relay kept sockets after client disconnect",
            )
        finally:
            handle.close()
            echo.stop()

    def test_target_outage_returns_eof_and_listener_survives(self):
        dead_port = _free_port()
        bind_port = _free_port()
        handle = None
        echo = None
        try:
            with unittest.mock.patch.object(
                relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
            ):
                handle = relay.run_relay(LOOPBACK, target_port=dead_port, bind_port=bind_port)
            with _connect(LOOPBACK, handle.bind_port) as sock:
                self.assertEqual(_drain(sock), b"")
            echo = _EchoServer(port=dead_port).start()
            with _connect(LOOPBACK, handle.bind_port) as sock:
                self.assertEqual(_send_and_read(sock, b"second-try", 10), b"second-try")
        finally:
            if handle:
                handle.close()
            if echo:
                echo.stop()

    def test_no_payload_in_logs(self):
        echo = _EchoServer().start()
        bind_port = _free_port()
        probe = b"PAYLOAD-MARKER-77aa9" + Random(7).randbytes(120_000)
        haystack_a = probe[1_000:1_016]
        haystack_b = probe[100_001:100_017]
        handle = None
        try:
            with captured_logs("esp32_private_relay") as lines:
                with unittest.mock.patch.object(
                    relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
                ):
                    handle = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=bind_port)
                with _connect(LOOPBACK, handle.bind_port) as sock:
                    self.assertEqual(_send_and_read(sock, probe, len(probe)), probe)
            self.assertTrue(lines, "expected relay status logs")
            blob = "\n".join(lines)
            for needle in (b"PAYLOAD-MARKER-77aa9", haystack_a, haystack_b):
                self.assertNotIn(needle.hex(), blob)
                self.assertNotIn(needle.decode("latin-1"), blob)
            self.assertNotIn("PAYLOAD-MARKER", blob)
        finally:
            handle.close()
            echo.stop()

    def test_bounded_concurrent_connections(self):
        echo = _EchoServer().start()
        bind_port = _free_port()
        handle = None
        try:
            with unittest.mock.patch.object(
                relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
            ), unittest.mock.patch.object(relay, "MAX_CLIENTS", 1):
                handle = relay.run_relay(LOOPBACK, target_port=echo.port, bind_port=bind_port)
            first = _connect(LOOPBACK, handle.bind_port)
            self.assertTrue(
                _wait(lambda: handle.active_count == 1), "first client was never activated"
            )
            second = _connect(LOOPBACK, handle.bind_port)
            overflow = _drain(second, timeout=2.0)
            self.assertEqual(overflow, b"")
            second.close()
            self.assertEqual(_send_and_read(first, b"still-alive", 11), b"still-alive")
            first.close()
        finally:
            handle.close()
            echo.stop()


# --------------------------------------------------------------------------- route source

class RouteSourceTests(unittest.TestCase):
    def test_derives_loopback_source_via_udp_connect(self):
        self.assertEqual(relay.route_source_for_board(ipv4("127.0.0.5")), ipv4(LOOPBACK))

    def test_signature_has_no_interface_parameter(self):
        sig = inspect.signature(relay.route_source_for_board)
        self.assertEqual(list(sig.parameters), ["board_ip"])

    def test_refuses_unsafe_derived_sources(self):
        refused = ["0.0.0.0", "224.0.0.5", PUBLIC_DOC, "255.255.255.255", "169.254.9.9"]
        for address in refused:
            with self.subTest(address=address), unittest.mock.patch.object(
                relay, "_route_source_probe", return_value=ipv4(address)
            ):
                with self.assertRaises(relay.RouteSourceError):
                    relay.route_source_for_board(ipv4(LAN_BOARD))


# --------------------------------------------------------------------------- audio server

class AudioServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.wav_path = _make_wav(Path(self._tmp.name))
        self.wav_bytes = self.wav_path.read_bytes()

    def start_server(self):
        handle = audio_server.create_audio_server(ipv4("127.0.0.5"), self.wav_path)
        self.addCleanup(handle.close)
        return handle

    def request(self, handle, method_path_line):
        conn = http.client.HTTPConnection("127.0.0.1", handle.bound_port, timeout=5)
        try:
            conn.putrequest("GET", method_path_line, skip_host=False)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read(), dict(resp.getheaders())
        finally:
            conn.close()

    def test_serves_exact_fixed_path_200(self):
        handle = self.start_server()
        self.assertEqual(handle.bound_ip, ipv4(LOOPBACK))
        status, body, headers = self.request(handle, audio_server.FIXED_WAV_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.wav_bytes)
        self.assertIn("audio/wav", headers.get("Content-Type", ""))
        self.assertEqual(int(headers.get("Content-Length", "-1")), len(self.wav_bytes))

    def test_other_paths_and_traversal_are_404(self):
        handle = self.start_server()
        for path in (
            "/",
            "/jarvis-speaker-test.wavx",
            f"{audio_server.FIXED_WAV_PATH}/",
            "/%2e%2e%2fetc%2fpasswd",
            "/../../etc/passwd",
            "/secrets?token=EVILTOKEN987",
            f"{audio_server.FIXED_WAV_PATH}?x=1",
            "/health",
            f"..{audio_server.FIXED_WAV_PATH}",
        ):
            with self.subTest(path_class=path[:12]):
                status, body, _ = self.request(handle, path)
                self.assertEqual(status, 404)
                self.assertNotEqual(body, self.wav_bytes)

    def test_logs_status_and_class_only(self):
        handle = self.start_server()
        with captured_logs("esp32_test_audio_server") as lines:
            self.request(handle, audio_server.FIXED_WAV_PATH)
            self.request(handle, "/../../etc/passwd")
            self.request(handle, "/secrets?token=EVILTOKEN987")
        blob = "\n".join(lines)
        self.assertIn("status=200", blob)
        self.assertIn("status=404", blob)
        self.assertNotIn("passwd", blob)
        self.assertNotIn("EVILTOKEN987", blob)
        self.assertNotIn("secrets", blob)

    def test_refuses_unsafe_derived_bind(self):
        with unittest.mock.patch.object(
            relay, "_route_source_probe", return_value=ipv4("0.0.0.0")
        ):
            with self.assertRaises(relay.RouteSourceError):
                audio_server.create_audio_server(ipv4(LAN_BOARD), self.wav_path)

    def test_refuses_missing_or_oversized_wav(self):
        missing = Path(self._tmp.name) / "absent.wav"
        with self.assertRaises(ValueError):
            audio_server.create_audio_server(ipv4("127.0.0.5"), missing)
        big = Path(self._tmp.name) / "big.wav"
        big.write_bytes(b"\0" * (audio_server.WAV_MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            audio_server.create_audio_server(ipv4("127.0.0.5"), big)

    def test_close_leaves_no_listener(self):
        handle = self.start_server()
        port = handle.bound_port
        handle.close()
        self.assertTrue(_wait(lambda: _listener_count(port) == 0))
        with self.assertRaises(OSError):
            _connect(LOOPBACK, port, timeout=2)


# --------------------------------------------------------------------------- test wave

class TestWavTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_generation_is_deterministic_byte_identical(self):
        first = _make_wav(Path(self._tmp.name), "a.wav")
        second = _make_wav(Path(self._tmp.name), "b.wav")
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_low_volume_safe_amplitude_and_format(self):
        path = _make_wav(Path(self._tmp.name))
        with wave.open(str(path), "rb") as fh:
            channels, sampwidth, rate, frames = (
                fh.getnchannels(),
                fh.getsampwidth(),
                fh.getframerate(),
                fh.getnframes(),
            )
            raw = fh.readframes(frames)
        samples = [int.from_bytes(raw[i : i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
        peak = max(abs(v) for v in samples)
        self.assertEqual((channels, sampwidth, rate), (1, 2, 8000))
        self.assertEqual(frames, 16000)  # exactly 2.0 s
        self.assertGreaterEqual(
            peak, 512, "amplitude too low to be audible at minimum volume"
        )
        self.assertLessEqual(
            peak, 2048, "amplitude must stay far below full scale for a safe test wave"
        )

    def test_generator_enforces_safe_peak_bounds(self):
        path = Path(self._tmp.name) / "guard.wav"
        for bad_peak in (0, -5, audio_server.WAV_MAX_PEAK + 1, 1 << 20):
            with self.subTest(peak=bad_peak):
                with self.assertRaises(ValueError):
                    audio_server.generate_test_wav(path, peak=bad_peak)
        ok = audio_server.generate_test_wav(path, peak=audio_server.WAV_MAX_PEAK, duration_s=0.5)
        self.assertTrue(ok.exists())
        for bad_duration in (0, -1, audio_server.MAX_WAV_SECONDS + 0.1):
            with self.subTest(duration=bad_duration):
                with self.assertRaises(ValueError):
                    audio_server.generate_test_wav(path, duration_s=bad_duration)


# --------------------------------------------------------------------------- preflight

class PreflightTests(unittest.TestCase):
    @staticmethod
    def _fake_runner_replies(tailscale_ip, bind_port, reply_probe=True):
        """Dials like lacasa-server would, using the fixed harmless protocol."""
        sock = _connect(tailscale_ip, bind_port)
        try:
            if reply_probe:
                sock.sendall(relay.PROBE_TOKEN)
                reply = b""
                sock.settimeout(5)
                while not reply.endswith(b"\n"):
                    data = sock.recv(64)
                    if not data:
                        break
                    reply += data
                return reply, True
            return b"", True
        finally:
            sock.close()

    def test_success_roundtrip_returns_true_and_exact_health_reply(self):
        bind_port = _free_port()
        replies = []
        with unittest.mock.patch.object(
            relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
        ):
            def runner(tailscale_ip, port):
                reply, ok = self._fake_runner_replies(tailscale_ip, port)
                replies.append(reply)
                return ok

            result = relay.preflight_from_lacasa_server(bind_port, runner=runner)
        self.assertTrue(result)
        self.assertEqual(replies, [relay.HEALTH_RESPONSE])
        with self.assertRaises(OSError):
            _connect(LOOPBACK, bind_port, timeout=2)

    def test_runner_failure_returns_false_and_closes_listener(self):
        bind_port = _free_port()
        with unittest.mock.patch.object(
            relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
        ):
            def broken(tailscale_ip, port):
                raise RuntimeError("lacasa-server unreachable via tailscale")

            self.assertFalse(relay.preflight_from_lacasa_server(bind_port, runner=broken))
            self.assertFalse(relay.preflight_from_lacasa_server(bind_port, runner=lambda ip, p: False))
        with self.assertRaises(OSError):
            _connect(LOOPBACK, bind_port, timeout=2)

    def test_returns_false_when_no_exchange_before_timeout(self):
        bind_port = _free_port()
        with unittest.mock.patch.object(
            relay, "current_tailscale_ipv4", return_value=ipv4(LOOPBACK)
        ):
            silent = unittest.mock.Mock(return_value=True)  # claims success, never dials
            result = relay.preflight_from_lacasa_server(bind_port, runner=silent, timeout=0.6)
        self.assertFalse(result)
        silent.assert_called_once_with("127.0.0.1", bind_port)
        with self.assertRaises(OSError):
            _connect(LOOPBACK, bind_port, timeout=2)

    def test_refuses_wildcard_tailscale_state(self):
        bind_port = _free_port()
        with unittest.mock.patch.object(
            relay, "current_tailscale_ipv4", return_value=ipv4("0.0.0.0")
        ):
            with self.assertRaises(relay.BindPolicyError):
                relay.preflight_from_lacasa_server(bind_port, runner=lambda ip, p: True)
        self.assertEqual(_listener_count(bind_port), 0)


# --------------------------------------------------------------------------- API acceptance

class MediaPlayerInfo:
    """Fake aioesphomeapi-shaped media-player entity info."""

    def __init__(self, key):
        self.key = key
        self.object_id = "growraum_sprache_1_home_test"
        self.name = "Temporary Speaker"


class FakeSensor:
    def __init__(self, key):
        self.key = key
        self.object_id = f"sensor_{key}"


class FakeConnection:
    def __init__(self):
        self.ops = []

    def connect(self):
        self.ops.append(("connect", None))

    def list_entities(self):
        self.ops.append(("list_entities", None))
        return [FakeSensor(7), MediaPlayerInfo(41)]

    def play_media_url(self, key, url):
        self.ops.append(("play_media_url", (key, url)))

    def disconnect(self):
        self.ops.append(("disconnect", None))


class ApiAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.factory_calls = []

    def _key_file(self, mode=0o600, content=KEY_MARKER + "\n"):
        path = Path(self._tmp.name) / "api-key"
        path.write_bytes(content.encode())
        path.chmod(mode)
        return path

    def _factory(self, host, port):
        conn = FakeConnection()
        self.factory_calls.append((host, port, conn))
        return conn

    def _run(self, wav_url=f"http://{PUBLIC_DOC}:18080{acceptance.FIXED_WAV_PATH}", **kwargs):
        base = dict(
            relay_host=LOOPBACK,
            relay_port=16053,
            key_path=self._key_file(),
            wav_url=wav_url,
            connection_factory=self._factory,
        )
        base.update(kwargs)
        return acceptance.run_acceptance(**base)

    def test_load_key_requires_mode_0600(self):
        self.assertEqual(acceptance.load_api_key(self._key_file(0o600)), KEY_MARKER)
        bad_modes = (0o644, 0o640, 0o040, 0o700, 0o777)
        for mode in bad_modes:
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(acceptance.KeyFileError):
                    acceptance.load_api_key(self._key_file(mode))
        with self.subTest(case="missing"):
            with self.assertRaises(acceptance.KeyFileError):
                acceptance.load_api_key(Path(self._tmp.name) / "absent")
        with self.subTest(case="symlink"):
            link = Path(self._tmp.name) / "link"
            link.symlink_to(self._key_file(0o600))
            with self.assertRaises(acceptance.KeyFileError):
                acceptance.load_api_key(link)
        with self.subTest(case="oversize"):
            with self.assertRaises(acceptance.KeyFileError):
                acceptance.load_api_key(self._key_file(0o600, content="k" * 300))
        with self.subTest(case="empty"):
            with self.assertRaises(acceptance.KeyFileError):
                acceptance.load_api_key(self._key_file(0o600, content=""))
        with self.subTest(case="multiline"):
            with self.assertRaises(acceptance.KeyFileError):
                acceptance.load_api_key(self._key_file(0o600, content=KEY_MARKER + "\nextra\n"))

    def test_acceptance_runs_through_relay_with_fixed_url_and_never_logs_key(self):
        url = f"http://{PUBLIC_DOC}:18080{acceptance.FIXED_WAV_PATH}"
        with captured_logs("esp32_api_acceptance") as lines:
            with contextlib.redirect_stdout(io_string()) as out:
                report = self._run(wav_url=url)
        blob = "\n".join(lines) + "\n" + out.getvalue()
        self.assertNotIn(KEY_MARKER, blob)
        self.assertEqual(report.media_player_key, 41)
        self.assertEqual(report.plays, 1)
        self.assertEqual(self.factory_calls[0][:2], (LOOPBACK, 16053))
        self.assertEqual(
            self.factory_calls[0][2].ops,
            [
                ("connect", None),
                ("list_entities", None),
                ("play_media_url", (41, url)),
                ("disconnect", None),
            ],
        )

    def test_discover_only_does_not_play(self):
        report = self._run(discover_only=True)
        self.assertEqual(report.media_player_key, 41)
        self.assertEqual(report.plays, 0)
        ops = [name for name, _ in self.factory_calls[0][2].ops]
        self.assertEqual(ops, ["connect", "list_entities", "disconnect"])

    def test_requires_exactly_one_media_player_entity(self):
        original = FakeConnection.list_entities

        def doublesided(self):
            return [MediaPlayerInfo(41), MediaPlayerInfo(42)]

        with unittest.mock.patch.object(FakeConnection, "list_entities", doublesided):
            with self.assertRaises(acceptance.AcceptanceError):
                self._run()
        ops = [name for name, _ in self.factory_calls[-1][2].ops]
        self.assertNotIn("play_media_url", ops)
        self.assertIn("disconnect", ops)

    def test_refuses_non_fixed_wav_url_before_connecting(self):
        for url in (
            f"https://{PUBLIC_DOC}:18080{acceptance.FIXED_WAV_PATH}",
            "http://203.0.113.5:18080/evil.wav",
            f"http://{PUBLIC_DOC}:18080{acceptance.FIXED_WAV_PATH}/extra",
        ):
            with self.subTest(url=url):
                with self.assertRaises(acceptance.AcceptanceError):
                    self._run(wav_url=url)
        self.assertEqual(self.factory_calls, [])

    def test_no_ha_calls_static_source_scan(self):
        source = (SCRIPTS / "esp32_api_acceptance.py").read_text()
        for token in (
            PORT_BOUNCE,
            "homeassistant",
            "urllib",
            "requests",
            "aiohttp",
            "http.client",
        ):
            self.assertNotIn(token, source)

    def test_aioesphomeapi_import_is_lazy(self):
        source = (SCRIPTS / "esp32_api_acceptance.py").read_text()
        self.assertIsNone(
            __import__("re").search(r"(?m)^(import|from)\s+aioesphomeapi", source)
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, json; sys.path.insert(0, %r); import esp32_api_acceptance; "
                    "print(json.dumps({'eager': 'aioesphomeapi' in sys.modules}))" % str(SCRIPTS)
                ),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr[-400:])
        self.assertIs(json.loads(probe.stdout.strip())["eager"], False)

    def test_main_refuses_bad_key_mode_without_leaking_and_reports_never_to_ha(self):
        bad = self._key_file(0o644)
        url = f"http://{PUBLIC_DOC}:18080{acceptance.FIXED_WAV_PATH}"
        argv = [
            "--relay-host",
            LOOPBACK,
            "--relay-port",
            "16053",
            "--key-file",
            str(bad),
            "--wav-url",
            url,
        ]
        with contextlib.redirect_stdout(io_string()) as out, contextlib.redirect_stderr(
            io_string()
        ) as err:
            exit_code = acceptance.main(argv)
        blob = out.getvalue() + err.getvalue()
        self.assertNotEqual(exit_code, 0)
        self.assertNotIn(KEY_MARKER, blob)


def io_string():
    import io

    return io.StringIO()


if __name__ == "__main__":
    unittest.main()