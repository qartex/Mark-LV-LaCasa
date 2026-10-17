#!/usr/bin/env python3
"""Self-validating private TCP relay for the phase-1 ESP32 speaker (Task 3).

Plan: docs/superpowers/plans/2026-10-17-waveshare-esp32-satellite-phase1.md.
This module is the single point of trust for the Mac's private transport:

- ``current_tailscale_ipv4()`` derives the Mac Tailscale IPv4 from local
  Tailscale state itself. It takes no parameters, so callers cannot inject a
  trusted address list; tests monkeypatch the module-level reader instead.
- ``validate_board_target()`` accepts only a private home-LAN IPv4 whose
  ARP/neighbor hardware address matches the manifest fingerprint. The
  neighbour reader is injectable for tests; the expected MAC is a required
  argument and can never be bypassed.
- ``run_relay()`` binds *only* the freshly discovered Tailscale IPv4 (wildcard,
  multicast and broadcast binds are refused), forwards the encrypted ESPHome
  native API with bounded buffers/timeouts, logs no payload and leaves zero
  sockets behind after ``close()``.
- ``route_source_for_board()`` derives the Mac LAN source address from the OS
  route to the validated board via the UDP-connect trick; callers cannot
  select a different interface.
- ``preflight_from_lacasa_server()`` verifies reachability from
  ``lacasa-server`` over Tailscale against a fixed, nonce-free health response
  before any device flash. The dialer is injectable for tests.

Safety rules: no payload or key material is ever logged (byte counts and port
numbers only), all subprocess calls use argv lists with bounded timeouts, and
every socket the module creates is tracked so cleanup is countable.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import subprocess
import threading
import time

__all__ = [
    "RelayError",
    "TailscaleStateError",
    "BindPolicyError",
    "ValidateTargetError",
    "RouteSourceError",
    "RelayHandle",
    "current_tailscale_ipv4",
    "validate_board_target",
    "route_source_for_board",
    "run_relay",
    "preflight_from_lacasa_server",
    "neighbor_mac_for",
    "PROBE_TOKEN",
    "HEALTH_RESPONSE",
]

_LOG = logging.getLogger("esp32_private_relay")

MAX_CHUNK_BYTES = 65536
MAX_BYTES_PER_DIRECTION = 16 * 1024 * 1024
MAX_CLIENTS = 8
LISTEN_BACKLOG = 8
CONNECT_TIMEOUT_S = 10.0
SOCKET_TIMEOUT_S = 20.0
CLI_TIMEOUT_S = 8.0
NEIGHBOR_TIMEOUT_S = 3.0
JOIN_TIMEOUT_S = 5.0
ACCEPT_POLL_S = 0.5  # poll-based accept loop; close() is honoured within this
DEFAULT_TARGET_PORT = 6053
DEFAULT_BIND_PORT = 16053
ROUTE_PROBE_PORT = 9

PROBE_TOKEN = b"PREFLIGHT-PROBE\n"
HEALTH_RESPONSE = b"preflight-ok\n"
DEFAULT_PREFLIGHT_HOST = "lacasa-server"
PREFLIGHT_LISTEN_TIMEOUT_S = 0.25
PREFLIGHT_EXCHANGE_TIMEOUT_S = 3.0
PREFLIGHT_RUNNER_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------- errors

class RelayError(Exception):
    """Base class for private-transport refusals."""


class TailscaleStateError(RelayError):
    """Local Tailscale state is missing, ambiguous or unsafe to trust."""


class BindPolicyError(RelayError):
    """A requested bind address violates the private-transport bind policy."""


class ValidateTargetError(RelayError):
    """A board address or fingerprint did not validate."""


class RouteSourceError(RelayError):
    """The OS route source could not be derived or is unsafe to bind."""


# --------------------------------------------------------------------------- tailscale discovery

_HOME_LAN_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_MAC_RE = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")


def _tailscale_cli_output(argv: list[str] | None = None) -> str:
    """Read local Tailscale state via a bounded argv-list subprocess call."""
    args = argv if argv is not None else ["tailscale", "ip", "-4"]
    try:
        proc = subprocess.run(
            args, timeout=CLI_TIMEOUT_S, capture_output=True, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TailscaleStateError("tailscale state unavailable") from None
    if proc.returncode != 0:
        raise TailscaleStateError("tailscale state unavailable")
    return proc.stdout.decode("utf-8", "replace")


def current_tailscale_ipv4() -> ipaddress.IPv4Address:
    """Derive the Mac's current Tailscale IPv4 from local state.

    Reads the Tailscale CLI with the explicit argv list. Deliberately
    parameter-free for callers: the trusted address can never be supplied by a
    caller; tests monkeypatch this module-level reader or
    ``_tailscale_cli_output``.
    """
    try:
        raw = _tailscale_cli_output(["tailscale", "ip", "-4"])
    except TailscaleStateError:
        raise
    except Exception:
        # Fail closed with a bounded message; underlying reader errors (and any
        # secret-bearing exception text) never escape here.
        raise TailscaleStateError("tailscale state unavailable") from None
    addresses: list[ipaddress.IPv4Address] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            addresses.append(ipaddress.IPv4Address(text))
        except ValueError:
            raise TailscaleStateError("tailscale reported a non-IPv4 address") from None
    if len(addresses) != 1:
        raise TailscaleStateError("tailscale state is ambiguous")
    address = addresses[0]
    if address not in _TAILSCALE_NETWORK:
        raise TailscaleStateError("tailscale state is not a tailscale address")
    return address


# --------------------------------------------------------------------------- board validation


def _is_home_lan(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in _HOME_LAN_NETWORKS)


def normalized_mac(value) -> str | None:
    """Normalize a MAC string (colon/hyphen) or return None when malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace("-", ":")
    if not _MAC_RE.fullmatch(text):
        return None
    return text


def neighbor_mac_for(ip: str) -> str | None:
    """Read the OS neighbour/ARP entry for *ip* (Linux then macOS fallback)."""
    for argv in (["ip", "neigh", "show", str(ip)], ["arp", "-n", str(ip)]):
        try:
            proc = subprocess.run(
                argv, timeout=NEIGHBOR_TIMEOUT_S, capture_output=True, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        for token in proc.stdout.decode("utf-8", "replace").split():
            if token.lower() in ("at", "lladdr", "dev"):
                continue
            mac = normalized_mac(token)
            if mac:
                return mac
    return None


def validate_board_target(
    ip: str,
    expected_mac: str,
    *,
    neighbor_reader=None,
) -> ipaddress.IPv4Address:
    """Accept only a private home-LAN IPv4 whose neighbour MAC matches *expected_mac*.

    The expected MAC is required and may never be bypassed; tests may inject
    *neighbor_reader*, production defaults to the module-level OS reader.
    """
    try:
        address = ipaddress.IPv4Address(str(ip).strip())
    except ValueError:
        raise ValidateTargetError("board target is not a valid IPv4 address") from None
    if not _is_home_lan(address):
        raise ValidateTargetError("board target is not a private home-LAN address")
    mac = normalized_mac(expected_mac)
    if mac is None:
        raise ValidateTargetError("expected MAC is not a valid hardware address")
    reader = neighbor_reader if neighbor_reader is not None else neighbor_mac_for
    try:
        observed = reader(str(address))
    except Exception:
        raise ValidateTargetError("neighbour lookup failed") from None
    observed_mac = normalized_mac(observed)
    if observed_mac is None:
        raise ValidateTargetError("no neighbour entry found for the board")
    if observed_mac != mac:
        raise ValidateTargetError("neighbour MAC does not match the manifest fingerprint")
    return address


# --------------------------------------------------------------------------- route source


def _route_source_probe(board_ip: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
    """Derive the local source address the OS route to *board_ip* would use.

    UDP connect sets the peer without emitting a packet; ``getsockname`` then
    reports the source address the kernel picked. Module-level so tests can
    monkeypatch it; callers cannot select an interface.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((str(board_ip), ROUTE_PROBE_PORT))
        except OSError:
            raise RouteSourceError("no OS route to the board address") from None
        source = sock.getsockname()[0]
    try:
        return ipaddress.IPv4Address(source)
    except ValueError:
        raise RouteSourceError("route source is not IPv4") from None


def _is_unsafe_bind(address: ipaddress.IPv4Address) -> bool:
    return (
        address.is_multicast
        or address == ipaddress.IPv4Address("0.0.0.0")
        or address == ipaddress.IPv4Address("255.255.255.255")
    )


def _is_unsafe_interface(address: ipaddress.IPv4Address) -> bool:
    return _is_unsafe_bind(address) or address.is_link_local or not (
        address.is_loopback or _is_home_lan(address)
    )


def route_source_for_board(board_ip: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
    """Derive the Mac LAN source for the *validated* board from the OS route."""
    try:
        board = board_ip if isinstance(board_ip, ipaddress.IPv4Address) else ipaddress.IPv4Address(
            str(board_ip)
        )
    except ValueError:
        raise RouteSourceError("board address is not IPv4") from None
    source = _route_source_probe(board)
    if _is_unsafe_interface(source):
        raise RouteSourceError("derived route source is not a usable private interface")
    return source


# --------------------------------------------------------------------------- private relay


class RelayHandle:
    """Trackable handle over the relay: every socket it creates is counted."""

    def __init__(
        self,
        bind_address: ipaddress.IPv4Address,
        bind_port: int,
        target_ip: str,
        target_port: int,
        listener: socket.socket,
    ):
        self.bind_address = bind_address
        self.bind_port = bind_port
        self._target_ip = target_ip
        self._target_port = target_port
        self._listener = listener
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = {listener}
        self._active: dict[socket.socket, threading.Thread | None] = {}
        self._closed = False
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="relay-accept", daemon=True
        )
        self._accept_thread.start()
        _LOG.info(
            "relay listening bind_port=%d target_port=%d", bind_port, target_port
        )

    @property
    def socket_count(self) -> int:
        with self._lock:
            return len(self._sockets)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        try:
            # Poll-based accept: a blocked-in-accept thread would otherwise keep
            # the (already closed) listening socket referenced in the kernel and
            # visible in /proc after shutdown.
            self._listener.settimeout(ACCEPT_POLL_S)
        except OSError:
            return
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # listener closed by shutdown
            drop = False
            with self._lock:
                if self._closed:
                    drop = True
                elif len(self._active) >= MAX_CLIENTS:
                    drop = True
                else:
                    self._active[conn] = None  # reserved until worker starts
            if drop:
                _LOG.info("relay client refused: concurrency cap reached")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            self._track(conn)
            worker = threading.Thread(
                target=self._serve_client, args=(conn,), daemon=True
            )
            with self._lock:
                if self._closed:
                    drop = True
                    self._active.pop(conn, None)
                else:
                    self._active[conn] = worker
            if drop:
                self._untrack(conn)
                continue
            worker.start()

    def _serve_client(self, conn: socket.socket) -> None:
        conn.settimeout(SOCKET_TIMEOUT_S)
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._track(target)
        try:
            target.settimeout(CONNECT_TIMEOUT_S)
            target.connect((self._target_ip, self._target_port))
            target.settimeout(SOCKET_TIMEOUT_S)
        except OSError:
            _LOG.info("relay client closed: target unavailable")
            self._untrack(conn)
            self._untrack(target)
            with self._lock:
                self._active.pop(conn, None)
            return
        finished = threading.Event()
        up_bytes = [0]
        down_bytes = [0]
        pumps = [
            threading.Thread(
                target=self._pump, args=(conn, target, up_bytes, finished), daemon=True
            ),
            threading.Thread(
                target=self._pump, args=(target, conn, down_bytes, finished), daemon=True
            ),
        ]
        for pump in pumps:
            pump.start()
        finished.wait(SOCKET_TIMEOUT_S + JOIN_TIMEOUT_S)
        # One direction ended: close both sockets so the opposite pump unblocks.
        self._untrack(conn)
        self._untrack(target)
        for pump in pumps:
            pump.join(JOIN_TIMEOUT_S)
        _LOG.info(
            "relay client finished directions=%d bytes=%d+%d",
            len(pumps), up_bytes[0], down_bytes[0],
        )
        with self._lock:
            self._active.pop(conn, None)

    def _pump(
        self,
        src: socket.socket,
        dst: socket.socket,
        counter: list[int],
        finished: threading.Event,
    ) -> None:
        try:
            src.settimeout(SOCKET_TIMEOUT_S)
            while True:
                data = src.recv(MAX_CHUNK_BYTES)
                if not data:
                    break
                counter[0] += len(data)
                if counter[0] > MAX_BYTES_PER_DIRECTION:
                    _LOG.info("relay client closed: byte budget exhausted")
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            finished.set()

    def close(self) -> None:
        """Close every tracked socket; afterwards ``socket_count`` is zero."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sockets = list(self._sockets)
            self._active.clear()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            self._sockets.clear()
        if self._accept_thread.is_alive():
            self._accept_thread.join(JOIN_TIMEOUT_S)
        _LOG.info("relay closed bind_port=%d", self.bind_port)


def _validated_unicast_target(target_ip: str) -> str:
    try:
        address = ipaddress.IPv4Address(str(target_ip).strip())
    except ValueError:
        raise ValidateTargetError("relay target is not a valid IPv4 address") from None
    if _is_unsafe_bind(address):
        raise ValidateTargetError("relay target is not a unicast address")
    return str(address)


def run_relay(
    target_ip: str,
    target_port: int = DEFAULT_TARGET_PORT,
    bind_port: int = DEFAULT_BIND_PORT,
) -> RelayHandle:
    """Relay the ESPHome API from the fresh Tailscale IPv4 to the board.

    The bind address comes from ``current_tailscale_ipv4()`` at call time, so
    it is always freshly discovered and can never be chosen or injected by a
    caller. Wildcard/multicast/broadcast binds are refused.
    """
    if not isinstance(target_port, int) or not 1 <= target_port <= 65535:
        raise ValueError("target_port must be an integer in [1, 65535]")
    if not isinstance(bind_port, int) or not 1 <= bind_port <= 65535:
        raise ValueError("bind_port must be an integer in [1, 65535]")
    bind_address = current_tailscale_ipv4()
    if _is_unsafe_bind(bind_address):
        raise BindPolicyError("refusing wildcard or non-unicast relay bind")
    target = _validated_unicast_target(target_ip)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((str(bind_address), bind_port))
        listener.listen(LISTEN_BACKLOG)
    except OSError:
        listener.close()
        raise
    return RelayHandle(
        bind_address=bind_address,
        bind_port=bind_port,
        target_ip=target,
        target_port=target_port,
        listener=listener,
    )


# --------------------------------------------------------------------------- preflight

_PREFLIGHT_DIALER = (
    "import sys\n"
    "from socket import create_connection\n"
    "conn = create_connection((sys.argv[1], int(sys.argv[2])), timeout=8)\n"
    "conn.settimeout(8)\n"
    "conn.sendall(b'PREFLIGHT-PROBE\\n')\n"
    "reply = b''\n"
    "while not reply.endswith(b'\\n'):\n"
    "    chunk = conn.recv(64)\n"
    "    if not chunk:\n        break\n"
    "    reply += chunk\n"
    "conn.close()\n"
    "sys.exit(0 if reply == b'preflight-ok\\n' else 1)\n"
)


def _default_preflight_runner(tailscale_ip: str, bind_port: int) -> bool:
    """Dial from lacasa-server via key-only SSH; reply stays in the pipe."""
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        DEFAULT_PREFLIGHT_HOST,
        "python3",
        "-c",
        _PREFLIGHT_DIALER,
        str(tailscale_ip),
        str(int(bind_port)),
    ]
    try:
        proc = subprocess.run(
            argv,
            timeout=PREFLIGHT_RUNNER_TIMEOUT_S,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _LOG.info("preflight runner failed to launch")
        return False
    return proc.returncode == 0  # output captured and discarded, never logged


def preflight_from_lacasa_server(
    bind_port: int,
    runner=None,
    timeout: float = 10.0,
) -> bool:
    """Verify lacasa-server can reach the Mac over Tailscale.

    Binds a harmless health listener on the freshly discovered Tailscale IPv4
    only, exchanges the fixed nonce-free probe/response with exactly one
    dialer, and always leaves zero listeners behind. Returns False on any
    failure; Tailscale-state or bind-policy problems raise.
    """
    if not isinstance(bind_port, int) or not 1 <= bind_port <= 65535:
        raise ValueError("bind_port must be an integer in [1, 65535]")
    bind_address = current_tailscale_ipv4()
    if _is_unsafe_bind(bind_address):
        raise BindPolicyError("refusing wildcard or non-unicast preflight bind")
    dialer = runner if runner is not None else _default_preflight_runner

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((str(bind_address), bind_port))
    listener.listen(4)

    done = threading.Event()

    def serve() -> None:
        listener.settimeout(PREFLIGHT_LISTEN_TIMEOUT_S)
        deadline = time.monotonic() + max(timeout, 0.0)
        while not done.is_set() and time.monotonic() < deadline:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(PREFLIGHT_EXCHANGE_TIMEOUT_S)
                    probe = b""
                    while not probe.endswith(b"\n") and len(probe) < 64:
                        chunk = conn.recv(64)
                        if not chunk:
                            break
                        probe += chunk
                    if probe == PROBE_TOKEN:
                        conn.sendall(HEALTH_RESPONSE)
                        done.set()
                except OSError:
                    pass

    thread = threading.Thread(target=serve, name="preflight-health", daemon=True)
    thread.start()
    try:
        try:
            dialed = bool(dialer(str(bind_address), int(bind_port)))
        except Exception:
            _LOG.info("preflight runner errored")
            dialed = False
        if not dialed:
            return False
        return done.wait(timeout=max(timeout, 0.0))
    finally:
        done.set()
        listener.close()
        thread.join(JOIN_TIMEOUT_S)
        _LOG.info("preflight listener closed bind_port=%d", bind_port)