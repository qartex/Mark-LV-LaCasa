"""Private Spotify OAuth store with six-month authorization model."""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REDIRECT_URI = "https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback"
TARGET_DEVICE_NAME = "raspotify (lacasa-server)"
REQUIRED_SCOPES = (
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
)


class SpotifyConfigError(ValueError):
    """Permanent store configuration failure; headless maps to exit 78."""


class SpotifyAuthorizationRequired(RuntimeError):
    """Authorization data missing or expired."""


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise SpotifyConfigError("timestamp must be a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SpotifyConfigError("invalid UTC timestamp") from None
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise SpotifyConfigError("timestamp must use UTC")
    return dt


def _six_calendar_months(start: datetime) -> datetime:
    months = start.month + 6
    year = start.year + (months - 1) // 12
    month = (months - 1) % 12 + 1
    import calendar
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class OAuthRecord:
    client_id: str
    authorized_at: datetime
    authorization_expires_at: datetime
    access_token: str | None = None
    access_expires_at: datetime | None = None
    refresh_token: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)


_RECORD_FIELDS = frozenset(
    {
        "client_id",
        "authorized_at",
        "authorization_expires_at",
        "access_token",
        "access_expires_at",
        "refresh_token",
        "scopes",
    }
)


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SpotifyConfigError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SpotifyConfigError(f"{field_name} must use UTC")
    return value


def _validate_record(record: object) -> OAuthRecord:
    if not isinstance(record, OAuthRecord):
        raise SpotifyConfigError("record must be an OAuthRecord")
    if not isinstance(record.client_id, str) or not record.client_id:
        raise SpotifyConfigError("client_id must be a non-empty string")

    authorized_at = _require_utc_datetime(record.authorized_at, "authorized_at")
    authorization_expires_at = _require_utc_datetime(
        record.authorization_expires_at, "authorization_expires_at"
    )
    if authorization_expires_at != _six_calendar_months(authorized_at):
        raise SpotifyConfigError("authorization_expires_at does not match six-month deadline")

    for field_name, value in (
        ("access_token", record.access_token),
        ("refresh_token", record.refresh_token),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise SpotifyConfigError(f"{field_name} must be a non-empty string or null")
    if record.access_expires_at is not None:
        _require_utc_datetime(record.access_expires_at, "access_expires_at")
    if not isinstance(record.scopes, tuple) or record.scopes != REQUIRED_SCOPES:
        raise SpotifyConfigError("scopes do not exactly match required scopes")
    return record


def _record_from_data(data: object) -> OAuthRecord:
    if not isinstance(data, dict):
        raise SpotifyConfigError("Spotify OAuth store is not an object")
    if set(data) != _RECORD_FIELDS:
        raise SpotifyConfigError("Spotify OAuth store fields do not match the required schema")

    client_id = data["client_id"]
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    raw_scopes = data["scopes"]
    if not isinstance(client_id, str) or not client_id:
        raise SpotifyConfigError("client_id must be a non-empty string")
    for field_name, value in (
        ("access_token", access_token),
        ("refresh_token", refresh_token),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise SpotifyConfigError(f"{field_name} must be a non-empty string or null")
    if not isinstance(raw_scopes, list) or not all(isinstance(scope, str) for scope in raw_scopes):
        raise SpotifyConfigError("scopes must be a list of strings")

    raw_access_expires_at = data["access_expires_at"]
    access_expires_at = None
    if raw_access_expires_at is not None:
        access_expires_at = _parse_utc(raw_access_expires_at)
    return _validate_record(
        OAuthRecord(
            client_id=client_id,
            authorized_at=_parse_utc(data["authorized_at"]),
            authorization_expires_at=_parse_utc(data["authorization_expires_at"]),
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            scopes=tuple(raw_scopes),
        )
    )


class OAuthStore:
    def __init__(self, path: Path = Path(__file__).resolve().parents[1] / "config" / "spotify_oauth.json"):
        self.path = Path(path)

    @staticmethod
    def _directory_flags() -> int:
        if not hasattr(os, "O_NOFOLLOW"):
            raise SpotifyConfigError("secure no-follow filesystem access is unavailable")
        return os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW

    @staticmethod
    def _validate_parent_stat(parent_stat: os.stat_result) -> None:
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise SpotifyConfigError("Spotify OAuth store parent is not a directory")
        if parent_stat.st_uid != os.getuid():
            raise SpotifyConfigError("Spotify OAuth store parent has a foreign owner")
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise SpotifyConfigError("Spotify OAuth store parent has unsafe permissions")

    @staticmethod
    def _validate_store_stat(store_stat: os.stat_result) -> None:
        if not stat.S_ISREG(store_stat.st_mode):
            raise SpotifyConfigError("Spotify OAuth store is not a regular file")
        if store_stat.st_uid != os.getuid():
            raise SpotifyConfigError("Spotify OAuth store has a foreign owner")
        if stat.S_IMODE(store_stat.st_mode) != 0o600:
            raise SpotifyConfigError("Spotify OAuth store must have mode 0600")

    def _open_parent(self, *, create: bool) -> tuple[int | None, str]:
        absolute = Path(os.path.abspath(os.fspath(self.path)))
        filename = absolute.name
        if not filename:
            raise SpotifyConfigError("Spotify OAuth store path has no filename")

        flags = self._directory_flags()
        try:
            current_fd = os.open(absolute.anchor, flags)
        except OSError:
            raise SpotifyConfigError("cannot open Spotify OAuth store root") from None

        try:
            for component in absolute.parent.parts[1:]:
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        os.close(current_fd)
                        return None, filename
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            self._validate_parent_stat(os.fstat(current_fd))
            return current_fd, filename
        except SpotifyConfigError:
            os.close(current_fd)
            raise
        except OSError:
            os.close(current_fd)
            raise SpotifyConfigError("unsafe Spotify OAuth store path") from None

    def load(self) -> OAuthRecord | None:
        parent_fd, filename = self._open_parent(create=False)
        if parent_fd is None:
            return None
        try:
            try:
                file_fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise SpotifyConfigError("cannot safely open Spotify OAuth store") from None
            try:
                self._validate_store_stat(os.fstat(file_fd))
                with os.fdopen(file_fd, "r", encoding="utf-8") as store_file:
                    file_fd = -1
                    data = json.load(store_file)
            except SpotifyConfigError:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                raise SpotifyConfigError("malformed Spotify OAuth store") from None
            finally:
                if file_fd != -1:
                    os.close(file_fd)
        finally:
            os.close(parent_fd)
        return _record_from_data(data)

    def save(self, record: OAuthRecord) -> None:
        record = _validate_record(record)
        data = {
            "client_id": record.client_id,
            "authorized_at": record.authorized_at.isoformat(),
            "authorization_expires_at": record.authorization_expires_at.isoformat(),
            "access_token": record.access_token,
            "access_expires_at": record.access_expires_at.isoformat() if record.access_expires_at else None,
            "refresh_token": record.refresh_token,
            "scopes": list(record.scopes),
        }
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
        parent_fd, filename = self._open_parent(create=True)
        assert parent_fd is not None
        temp_name = None
        try:
            try:
                existing = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise SpotifyConfigError("cannot inspect Spotify OAuth store") from None
            else:
                self._validate_store_stat(existing)

            for _ in range(128):
                candidate = f".spotify-oauth-{secrets.token_hex(12)}.tmp"
                try:
                    fd = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                temp_name = candidate
                break
            else:
                raise FileExistsError("could not allocate Spotify OAuth temporary file")

            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temp_name = None
            os.fsync(parent_fd)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    def invalidate_tokens(self) -> OAuthRecord | None:
        record = self.load()
        if record is None:
            return None
        cleared = OAuthRecord(
            client_id=record.client_id,
            authorized_at=record.authorized_at,
            authorization_expires_at=record.authorization_expires_at,
            access_token=None,
            access_expires_at=None,
            refresh_token=None,
            scopes=record.scopes,
        )
        self.save(cleared)
        return cleared
