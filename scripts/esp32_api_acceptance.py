#!/usr/bin/env python3
"""Direct ESPHome API acceptance client for the phase-1 speaker test (Task 3).

Plan: docs/superpowers/plans/2026-10-17-waveshare-esp32-satellite-phase1.md.

Runs on lacasa-server. Safety contract:

- The ESPHome API encryption key is read *only* from a single-line, ASCII,
  regular, mode-0600, non-symlinked local file (O_NOFOLLOW, fstat-verified);
  any other form is refused before any network activity.
- It reaches the board exclusively through the private TCP relay, discovers
  the temporary device's single media player entity and sends the fixed WAV
  URL pointing at the audio server's route-source address.
- It speaks the ESPHome native API only; no other service is contacted and no
  configuration or registry anywhere is modified.
- Key material never appears in logs, exceptions or output; messages are short
  and status-only.

``aioesphomeapi`` is imported lazily: tests inject a fake connection factory,
and a missing dependency can never break importing this module. The pinned
third-party client is required only for the real device run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FIXED_WAV_PATH",
    "KEY_FILE_MODE",
    "MAX_KEY_BYTES",
    "KeyFileError",
    "AcceptanceError",
    "AcceptanceReport",
    "load_api_key",
    "run_acceptance",
    "main",
    "EspHomeConnection",
    "build_arg_parser",
]

_LOG = logging.getLogger("esp32_api_acceptance")

FIXED_WAV_PATH = "/jarvis-speaker-test.wav"
KEY_FILE_MODE = 0o600
MAX_KEY_BYTES = 256
DEFAULT_RELAY_PORT = 16053
_MEDIA_PLAYER_SUFFIX = "MediaPlayerInfo"
_URL_PREFIX = "http://"
_WAV_HOST_HINT = "the audio server route-source host must include a port"


class KeyFileError(Exception):
    """The key file did not meet the strict mode-0600 single-line contract."""


class AcceptanceError(Exception):
    """The acceptance run refused to proceed (no key material is included)."""


@dataclass(frozen=True)
class AcceptanceReport:
    media_player_key: int | None
    wav_url: str
    plays: int


def load_api_key(path: Path | str) -> str:
    """Read the API key only from a mode-0600 single-line regular file."""
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise KeyFileError("key file unavailable") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise KeyFileError("key file is not a regular file")
            if stat.S_IMODE(info.st_mode) != KEY_FILE_MODE:
                raise KeyFileError("key file mode must be 0600")
            raw = handle.read(MAX_KEY_BYTES + 1)
    except OSError:
        raise KeyFileError("key file unreadable") from None
    if len(raw) > MAX_KEY_BYTES:
        raise KeyFileError("key file oversized")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise KeyFileError("key file is not ASCII") from None
    value = text.rstrip("\n")
    if not value or "\n" in value or not value.isprintable():
        raise KeyFileError("key file must hold exactly one printable line")
    return value


def _validated_wav_url(url) -> str:
    if not isinstance(url, str) or not url.startswith(_URL_PREFIX):
        raise AcceptanceError("wav URL must be plain http")
    rest = url[len(_URL_PREFIX) :]
    slash = rest.find("/")
    if slash == -1:
        raise AcceptanceError("wav URL must use the fixed path")
    host, path = rest[:slash], rest[slash:]
    if path != FIXED_WAV_PATH:
        raise AcceptanceError("wav URL must use the fixed path")
    if not host or ":" not in host or "@" in host or any(ch.isspace() for ch in host):
        raise AcceptanceError(_WAV_HOST_HINT)
    return url


def _media_player_entities(entities) -> list:
    return [e for e in entities if type(e).__name__.endswith(_MEDIA_PLAYER_SUFFIX)]


def run_acceptance(
    relay_host: str,
    relay_port: int,
    key_path: Path,
    wav_url: str | None,
    *,
    discover_only: bool = False,
    connection_factory=None,
) -> AcceptanceReport:
    """Run discover (+ optional play) strictly through the private relay."""
    key = load_api_key(key_path)  # refuses anything but the strict 0600 form
    wav = _validated_wav_url(wav_url)
    factory = connection_factory or (lambda host, port: EspHomeConnection(host, port, noise_psk=key))

    media_key: int | None = None
    plays = 0
    connection = factory(str(relay_host), int(relay_port))
    try:
        connection.connect()
        try:
            found = _media_player_entities(connection.list_entities())
            if len(found) != 1:
                raise AcceptanceError(
                    f"expected exactly one media-player entity, found {len(found)}"
                )
            media_key = int(found[0].key)
            if not discover_only:
                connection.play_media_url(media_key, wav)
                plays = 1
        finally:
            connection.disconnect()
    finally:
        _LOG.info(
            "acceptance finished relay_port=%d discover_only=%s plays=%d",
            int(relay_port),
            bool(discover_only),
            plays,
        )
    return AcceptanceReport(media_player_key=media_key, wav_url=wav, plays=plays)


class EspHomeConnection:
    """Thin synchronous facade over the lazily imported native API client."""

    def __init__(self, host: str, port: int, noise_psk: str):
        self._host = str(host)
        self._port = int(port)
        self._key = noise_psk
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def connect(self):
        import aioesphomeapi  # type: ignore[import-not-found]  # lazy, optional.

        async def _connect():
            client = aioesphomeapi.APIClient(
                address=self._host,
                port=self._port,
                password=None,
                noise_psk=self._key,
            )
            await client.connect()
            return client

        try:
            self._client = self._run(_connect())
        except BaseException:
            self._teardown_loop()
            raise

    def list_entities(self):
        if self._client is None:
            raise AcceptanceError("not connected")
        async def _list():
            try:
                result = await self._client.list_entities_services()
            except AttributeError:
                result = await self._client.list_entities()
            if isinstance(result, tuple) and result:
                if isinstance(result[0], (list, tuple)):
                    return list(result[0])
                if len(result) > 1 and isinstance(result[1], (list, tuple)):
                    return list(result[1])
            return list(result)

        return self._run(_list())

    def play_media_url(self, entity_key: int, url: str):
        if self._client is None:
            raise AcceptanceError("not connected")
        async def _play():
            import aioesphomeapi  # type: ignore[import-not-found]  # lazy, optional.

            media_url = url
            try:
                request = aioesphomeapi.MediaPlayerCommandRequest(
                    key=entity_key,
                    command=aioesphomeapi.MediaPlayerCommand.PLAY,
                    media_url=media_url,
                )
            except TypeError:
                request = aioesphomeapi.MediaPlayerCommandRequest(
                    key=entity_key,
                    command=aioesphomeapi.MediaPlayerCommand.PLAY,
                    commanded_media_url=media_url,
                )
            sender = getattr(self._client, "send", None)
            if sender is None:
                raise AcceptanceError("installed native API client cannot send commands")
            await sender(request)

        self._run(_play())

    def disconnect(self):
        if self._client is None:
            self._teardown_loop()
            return
        try:
            self._run(self._client.disconnect())
        finally:
            self._client = None
            self._teardown_loop()

    def _teardown_loop(self):
        if self._loop is not None:
            try:
                self._loop.close()
            except RuntimeError:
                pass
            self._loop = None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Direct ESPHome API acceptance client over the private relay"
    )
    parser.add_argument("--relay-host", required=True)
    parser.add_argument("--relay-port", type=int, default=DEFAULT_RELAY_PORT)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--wav-url", default=None)
    parser.add_argument("--discover-only", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report = run_acceptance(
            relay_host=args.relay_host,
            relay_port=args.relay_port,
            key_path=Path(args.key_file),
            wav_url=args.wav_url,
            discover_only=args.discover_only,
        )
    except (KeyFileError, AcceptanceError) as exc:
        print(f"acceptance refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print("acceptance failed: connection error", file=sys.stderr)
        _LOG.info("connection failure to relay")
        del exc
        return 1
    summary = f"media-player key={report.media_player_key} plays={report.plays}"
    print(f"acceptance ok: {summary}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())