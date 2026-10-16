"""Private Spotify OAuth store with six-month authorization model."""
from __future__ import annotations

import json
import logging
import os
import tempfile
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


def _parse_utc(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise SpotifyConfigError(f"invalid timestamp {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise SpotifyConfigError("timestamp must carry a UTC offset")
    return dt.astimezone(timezone.utc)


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


class OAuthStore:
    def __init__(self, path: Path = Path(__file__).resolve().parents[1] / "config" / "spotify_oauth.json"):
        self.path = path

    def _validate_layout(self) -> None:
        st = self.path.lstat()
        if st.st_mode & 0o777 != 0o600:
            raise SpotifyConfigError(f"unsafe permissions on {self.path}")
        if os.path.islink(self.path):
            raise SpotifyConfigError(f"symlinked store {self.path}")
        parent = self.path.parent
        pst = parent.lstat()
        if pst.st_mode & 0o022:
            raise SpotifyConfigError(f"unsafe parent permissions on {parent}")
        if os.path.islink(parent):
            raise SpotifyConfigError(f"symlinked parent {parent}")
        if pst.st_uid != os.getuid():
            raise SpotifyConfigError(f"foreign owner on {parent}")

    def load(self) -> OAuthRecord | None:
        if not self.path.exists():
            return None
        self._validate_layout()
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpotifyConfigError(f"malformed Spotify OAuth store: {exc}") from exc
        if not isinstance(data, dict):
            raise SpotifyConfigError("Spotify OAuth store is not an object")
        try:
            client_id = data["client_id"]
            authorized_at = _parse_utc(data["authorized_at"])
            expires_at = _parse_utc(data["authorization_expires_at"])
            access_token = data.get("access_token")
            access_expires_at = data.get("access_expires_at")
            if access_expires_at is not None:
                access_expires_at = _parse_utc(access_expires_at)
            refresh_token = data.get("refresh_token")
            scopes = tuple(data["scopes"])
        except KeyError as exc:
            raise SpotifyConfigError(f"missing field {exc}") from exc
        if not isinstance(client_id, str) or not client_id:
            raise SpotifyConfigError("client_id must be a non-empty string")
        if not isinstance(scopes, tuple) or set(scopes) != set(REQUIRED_SCOPES):
            raise SpotifyConfigError("scopes do not match required set")
        if expires_at != _six_calendar_months(authorized_at):
            raise SpotifyConfigError("authorization_expires_at does not match six-month deadline")
        return OAuthRecord(
            client_id=client_id,
            authorized_at=authorized_at,
            authorization_expires_at=expires_at,
            access_token=access_token if isinstance(access_token, str) else None,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            scopes=scopes,
        )

    def save(self, record: OAuthRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pst = self.path.parent.lstat()
        if pst.st_mode & 0o022:
            raise SpotifyConfigError(f"unsafe parent permissions on {self.path.parent}")
        if os.path.islink(self.path.parent):
            raise SpotifyConfigError(f"symlinked parent {self.path.parent}")
        if pst.st_uid != os.getuid():
            raise SpotifyConfigError(f"foreign owner on {self.path.parent}")
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
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".spotify-oauth-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dirfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        os.chmod(self.path, 0o600)

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
        try:
            self.save(cleared)
        except OSError:
            return None
        return cleared
