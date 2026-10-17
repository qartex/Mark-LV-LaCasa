#!/usr/bin/env python3
"""Fixed-file low-volume WAV test server for the phase-1 ESP32 speaker (Task 3).

Plan: docs/superpowers/plans/2026-10-17-waveshare-esp32-satellite-phase1.md.

- ``generate_test_wav()`` writes a deterministic, byte-for-byte reproducible,
  short and quiet mono 16-bit test tone. The amplitude is guard-railed: loud
  enough to be audible at minimum volume, far below full scale.
- ``create_audio_server()`` binds *only* to the OS route-source address
  derived by ``esp32_private_relay.route_source_for_board()`` for the
  manifest-validated board; callers cannot choose an interface. It serves
  exactly ``/jarvis-speaker-test.wav`` (200); every other path — including
  traversal forms and query strings — gets 404. Logging records status and a
  fixed path class only, never the raw path, query tokens or payload bytes.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from esp32_private_relay import RouteSourceError, route_source_for_board

__all__ = [
    "FIXED_WAV_PATH",
    "WAV_PEAK_DEFAULT",
    "WAV_MIN_PEAK",
    "WAV_MAX_PEAK",
    "WAV_MAX_BYTES",
    "MAX_WAV_SECONDS",
    "generate_test_wav",
    "create_audio_server",
    "AudioServerHandle",
]

_LOG = logging.getLogger("esp32_test_audio_server")

FIXED_WAV_PATH = "/jarvis-speaker-test.wav"
TONE_HZ = 440.0
WAV_RATE_DEFAULT = 8000
WAV_RATE_MIN = 4000
WAV_RATE_MAX = 48000
WAV_SECONDS_DEFAULT = 2.0
MAX_WAV_SECONDS = 3.0
WAV_PEAK_DEFAULT = 2047
WAV_MIN_PEAK = 1
WAV_MAX_PEAK = 8192
WAV_MAX_BYTES = 4 * 1024 * 1024

_OK_CLASS = "served"
_REJECTED_CLASS = "rejected"


def generate_test_wav(
    path: Path,
    *,
    duration_s: float = WAV_SECONDS_DEFAULT,
    sample_rate: int = WAV_RATE_DEFAULT,
    peak: int = WAV_PEAK_DEFAULT,
) -> Path:
    """Write a deterministic, short, low-volume mono 16-bit tone to *path*."""
    if not isinstance(peak, int) or not WAV_MIN_PEAK <= peak <= WAV_MAX_PEAK:
        raise ValueError(f"peak must be an integer in [{WAV_MIN_PEAK}, {WAV_MAX_PEAK}]")
    if not isinstance(sample_rate, int) or not WAV_RATE_MIN <= sample_rate <= WAV_RATE_MAX:
        raise ValueError(f"sample_rate must be an integer in [{WAV_RATE_MIN}, {WAV_RATE_MAX}]")
    if not isinstance(duration_s, (int, float)) or not 0 < float(duration_s) <= MAX_WAV_SECONDS:
        raise ValueError(f"duration_s must be within (0, {MAX_WAV_SECONDS}]")
    out = Path(path)
    if not out.parent.is_dir():
        raise ValueError("output directory does not exist")
    frames = int(round(float(duration_s) * sample_rate))
    if frames <= 0:
        raise ValueError("wav would contain no frames")
    omega = 2.0 * math.pi * TONE_HZ / sample_rate
    values = [int(peak * math.sin(omega * i)) for i in range(frames)]
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(struct.pack(f"<{frames}h", *values))
    return out


class _FixedWavHandler(BaseHTTPRequestHandler):
    """Serve the exact fixed WAV path; classify everything else as rejected."""

    protocol_version = "HTTP/1.0"

    @property
    def _wav_bytes(self) -> bytes:
        return self.server.wav_bytes  # type: ignore[attr-defined]

    def _audit(self, status: int, path_class: str) -> None:
        _LOG.info("wav-request status=%d class=%s", status, path_class)

    @staticmethod
    def _reply(head, body: bytes, status: int, path_class: str, content_type: str) -> None:
        head.send_response(status)
        head.send_header("Content-Type", content_type)
        head.send_header("Content-Length", str(len(body)))
        head.end_headers()
        if body:
            head.wfile.write(body)
        head._audit(status, path_class)

    def do_GET(self) -> None:
        if self.path == FIXED_WAV_PATH:
            self._reply(self, self._wav_bytes, 200, _OK_CLASS, "audio/wav")
        else:
            self._reply(self, b"not found\n", 404, _REJECTED_CLASS, "text/plain")

    def do_HEAD(self) -> None:
        status = 200 if self.path == FIXED_WAV_PATH else 404
        self._reply(
            self,
            b"",
            status,
            _OK_CLASS if status == 200 else _REJECTED_CLASS,
            "audio/wav" if status == 200 else "text/plain",
        )

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Suppress base http.server logging entirely."""
        return

    def log_error(self, format, *args):  # noqa: A002 - stdlib signature
        return


class AudioServerHandle:
    """Running fixed-file audio server with an audited, full close()."""

    def __init__(self, httpd: ThreadingHTTPServer):
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="audio-serve", daemon=True
        )
        self._thread.start()

    @property
    def bound_ip(self) -> ipaddress.IPv4Address:
        return ipaddress.IPv4Address(self._httpd.server_address[0])

    @property
    def bound_port(self) -> int:
        return int(self._httpd.server_address[1])

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread.is_alive():
            self._thread.join(2.0)


def create_audio_server(
    board_ip: ipaddress.IPv4Address, wav_path: Path
) -> AudioServerHandle:
    """Bind the fixed-file server to the route source derived for *board_ip*.

    Bind-address policy runs before any socket is opened: unsafe derived
    addresses raise ``RouteSourceError``. The WAV is validated and fully read
    into the bound server before binding, so a missing or oversized file can
    never leave a listener behind.
    """
    try:
        board = (
            board_ip
            if isinstance(board_ip, ipaddress.IPv4Address)
            else ipaddress.IPv4Address(str(board_ip))
        )
    except ValueError:
        raise ValueError("board_ip must be an IPv4 address") from None
    source = route_source_for_board(board)
    source_address = (
        source if isinstance(source, ipaddress.IPv4Address) else ipaddress.IPv4Address(str(source))
    )
    path = Path(wav_path)
    if not path.is_file():
        raise ValueError("test WAV file is missing")
    size = path.stat().st_size
    if size == 0 or size > WAV_MAX_BYTES:
        raise ValueError("test WAV size is out of bounds")
    payload = path.read_bytes()

    httpd = ThreadingHTTPServer((str(source_address), 0), _FixedWavHandler)
    httpd.daemon_threads = True
    httpd.wav_bytes = payload  # type: ignore[attr-defined]
    _LOG.info(
        "audio server listening port=%d file_bytes=%d", httpd.server_address[1], len(payload)
    )
    return AudioServerHandle(httpd)


if __name__ == "__main__":  # pragma: no cover - operational helper only
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Serve the fixed phase-1 test WAV")
    parser.add_argument("board_ip", help="manifest-validated board LAN address")
    parser.add_argument("wav_path", type=Path)
    args = parser.parse_args(sys.argv[1:])
    handle = create_audio_server(ipaddress.IPv4Address(args.board_ip), args.wav_path)
    print(f"serving {FIXED_WAV_PATH} on port {handle.bound_port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        handle.close()