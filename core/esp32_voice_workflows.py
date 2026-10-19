"""ESP32-only JARVIS workflows for Cannanas diaries and Hermes learning jobs.

The voice bridge keeps model-facing inputs narrow. Cannanas writes still pass
through the existing host-owned prepare/confirm state machine; learning jobs are
bounded untrusted requirements persisted to an owner-only spool for a separate
Hermes dispatcher.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_LEARNING_REQUEST_CHARS = 1000
MAX_CAPABILITY_CHARS = 200
DEFAULT_LEARNING_QUEUE_DIR = Path(
    "/home/lacasa/waveshare-va-test/learning-queue"
)
_LEARNING_ACK = (
    "Das kann ich noch nicht. Ich muss es erst lernen. "
    "Ich habe Hermes den Auftrag gegeben."
)
_CANNANAS_UNAVAILABLE = (
    "Cannanas ist derzeit nicht verfügbar. Bitte versuche es später erneut."
)
CLAIM_STALE_SECONDS = 300.0


def _key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _record_name(record: dict) -> str:
    for field in ("name", "plant_name", "display_name"):
        value = str(record.get(field) or "").strip()
        if value:
            return value
    return ""


def _record_plant(record: dict) -> tuple[str, str] | None:
    identity = str(record.get("id") or record.get("plant_id") or "").strip()
    label = _record_name(record)
    if not identity or not label:
        return None
    return identity, label


def _record_batch(record: dict) -> tuple[str, str] | None:
    nested = record.get("batch")
    identity = str(record.get("batch_id") or "").strip()
    label = str(record.get("batch_name") or "").strip()
    if isinstance(nested, dict):
        identity = identity or str(nested.get("id") or "").strip()
        label = label or str(nested.get("name") or "").strip()
    if not identity or not label:
        return None
    return identity, label


class CannanasRoomDiaryWorkflow:
    """Resolve a spoken room to one plant/batch, then prepare one diary write.

    Cannanas diaries bind to a plant or batch, never directly to a room. A room
    containing one shared batch resolves automatically. Ambiguous rooms are
    read-only until the speaker names one of the listed plants/batches.
    """

    def __init__(self, *, host=None) -> None:
        if host is None:
            from core.cannanas_host import get_host

            host = get_host()
        self.host = host

    def prepare(self, parameters: dict, turn) -> str:
        parameters = dict(parameters or {})
        room = str(parameters.get("room") or "").strip()
        text = str(parameters.get("text") or "").strip()
        target = str(parameters.get("target") or "").strip()
        if not room:
            return "Für welchen Raum soll ich einen Tagebucheintrag vorbereiten?"
        if not text:
            return f"Was soll ich in das Tagebuch für Raum {room} eintragen?"
        if turn is None or not getattr(turn, "stable_device_id", None):
            return "Der Tagebucheintrag ist nur über ein zugeordnetes Gerät möglich."
        try:
            return self._prepare_write(room=room, text=text, target=target, turn=turn)
        except Exception:
            # Availability and transport problems degrade to one bounded spoken
            # notice; upstream details never reach the model or the speaker.
            return _CANNANAS_UNAVAILABLE

    def _prepare_write(self, *, room: str, text: str, target: str, turn) -> str:
        listing = self.host.call_read(
            "list_plants", {"room": room, "count": 25}
        )
        if not listing.get("exact", True) or listing.get("truncated", False):
            return (
                f"Die Pflanzenliste für Raum {room} ist nicht eindeutig. "
                "Bitte nenne die Pflanze oder Charge."
            )
        records = [r for r in listing.get("records", []) if isinstance(r, dict)]
        if not records:
            return f"In Raum {room} habe ich keine Pflanze oder Charge gefunden."

        batch_links = [_record_batch(record) for record in records]
        plant_links = [_record_plant(record) for record in records]
        candidates: list[tuple[str, str, str]] = []
        seen_refs: set[tuple[str, str]] = set()
        for kind, links in (("Charge", batch_links), ("Pflanze", plant_links)):
            for link in links:
                if link is None or (kind, link[0]) in seen_refs:
                    continue
                seen_refs.add((kind, link[0]))
                candidates.append((kind, link[0], link[1]))
        candidates.sort(key=lambda item: (item[2].casefold(), item[0], item[1]))

        selected = ""
        if target:
            wanted = _key(target)
            matches = [
                candidate
                for candidate in candidates
                if wanted in {_key(candidate[1]), _key(candidate[2])}
            ]
            if len(matches) == 1:
                selected = matches[0][1]
            elif not matches:
                return f"{target} gehört nicht zu Raum {room}. Bitte nenne eines der Ziele."
        else:
            linked_batches = [link for link in batch_links if link is not None]
            batch_ids = {link[0] for link in linked_batches}
            if len(linked_batches) == len(records) and len(batch_ids) == 1:
                selected = linked_batches[0][0]
            else:
                plants = [link for link in plant_links if link is not None]
                if len(records) == 1 and len(plants) == 1:
                    selected = plants[0][0]

        if not selected:
            labels = []
            label_counts: dict[str, int] = {}
            for _kind, _identity, label in candidates:
                label_counts[_key(label)] = label_counts.get(_key(label), 0) + 1
            for kind, identity, label in candidates[:5]:
                if label_counts.get(_key(label), 0) > 1:
                    labels.append(f"{label} ({kind} {identity})")
                else:
                    labels.append(label)
            names = ", ".join(labels) or "keine eindeutigen Ziele"
            return (
                f"In Raum {room} habe ich mehrere Ziele: {names}. "
                "Für welche Pflanze oder Charge soll der Eintrag gelten?"
            )

        result = self.host.prepare(
            "prepare_diary_create",
            {"room": room, "plant_name": selected, "text": text},
            turn,
        )
        summary = str(result.get("summary") or "").strip()
        if not summary:
            return "Die Tagebuch-Vorschau konnte nicht erstellt werden."
        return summary

    def cancel_pending(self, device_id: str) -> bool:
        """Fence this device's pending preview (connection lost, STOP, restart)."""
        session_id = str(device_id or "").strip()
        if not session_id:
            return False
        coordinator = getattr(self.host, "coordinator", None)
        if coordinator is None:
            return False
        return bool(coordinator.cancel(session_id))

    def has_pending(self, turn) -> bool:
        if turn is None:
            return False
        checker = getattr(self.host, "has_pending_for", None)
        if callable(checker):
            return bool(checker(turn))
        coordinator = getattr(self.host, "coordinator", None)
        if coordinator is None:
            return False
        pending = coordinator.current(getattr(turn, "stable_device_id", "") or "")
        return bool(pending is not None and getattr(pending, "state", "") == "pending")

    def evaluate_confirmation(self, turn, transcript: str) -> str | None:
        try:
            outcome = self.host.evaluate(turn, transcript)
        except Exception:
            return _CANNANAS_UNAVAILABLE
        if not outcome.get("handled"):
            return None
        spoken = str(outcome.get("spoken") or "").strip()
        return spoken or "Der Cannanas-Vorgang wurde bearbeitet."


class LearningQueue:
    """Owner-only durable spool between JARVIS and the Hermes dispatcher."""

    def __init__(self, root: str | os.PathLike = DEFAULT_LEARNING_QUEUE_DIR) -> None:
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.dispatched = self.root / "dispatched"
        self.completed = self.root / "completed"
        self.claimed = self.root / "claimed"
        self.announced = self.root / "announced"
        self.quarantine = self.root / "quarantine"
        for directory in (
            self.root,
            self.pending,
            self.dispatched,
            self.completed,
            self.claimed,
            self.announced,
            self.quarantine,
        ):
            self._ensure_private_dir(directory)
        self._recover_stale_claims()

    @staticmethod
    def _ensure_private_dir(path: Path) -> None:
        if path.is_symlink():
            raise RuntimeError("learning queue path must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = path.stat()
        if stat.st_uid != os.getuid():
            raise PermissionError("learning queue path has wrong owner")
        os.chmod(path, 0o700)

    @staticmethod
    def _bounded_text(value: object, *, limit: int, label: str) -> str:
        text = " ".join(str(value or "").strip().split())
        if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
            raise ValueError(f"invalid {label}")
        return text

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Make a create/rename durable, not just the file contents."""
        try:
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write(
        self, directory: Path, file_name: str, payload: dict, *, overwrite: bool = True
    ) -> Path:
        self._ensure_private_dir(directory)
        target = directory / file_name
        if target.parent != directory or target.name != file_name:
            raise ValueError("invalid queue file name")
        temp = directory / f".{file_name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o600)
        try:
            encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, 0o600)
        if overwrite:
            os.replace(temp, target)
            self._fsync_dir(directory)
            return target
        # Immutable create: link() fails with FileExistsError when the target
        # already exists, so a completion can never be rewritten.
        try:
            os.link(temp, target)
        except FileExistsError:
            os.unlink(temp)
            raise ValueError("queue entry already exists") from None
        os.unlink(temp)
        self._fsync_dir(directory)
        return target

    def _recover_stale_claims(self, *, stale_after: float = CLAIM_STALE_SECONDS) -> int:
        """Return abandoned announcements to the queue after a crash."""
        recovered = 0
        now = time.time()
        for path in sorted(self.claimed.glob("*.json")):
            try:
                if now - path.stat().st_mtime < stale_after:
                    continue
                os.replace(path, self.completed / path.name)
                self._fsync_dir(self.completed)
            except OSError:
                continue
            recovered += 1
        return recovered

    @staticmethod
    def _read_payload(path: Path) -> dict | None:
        try:
            if path.is_symlink() or path.stat().st_size > 16_384:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def validate_job(cls, path: Path, payload: dict | None) -> dict:
        """Strict schema for one queue record; raises ValueError when invalid.

        Bounds are re-checked here, not only in ``submit``: anything that can
        write into ``pending/`` must pass the same gate before it is signed.
        """
        if not isinstance(payload, dict):
            raise ValueError("invalid job payload")
        expected = {"schema_version", "id", "status", "created_at", "request", "source"}
        if set(payload) != expected:
            raise ValueError("invalid job keys")
        if payload.get("schema_version") != 1:
            raise ValueError("invalid schema version")
        job_id = cls._bounded_text(payload.get("id"), limit=32, label="job id")
        try:
            canonical = uuid.UUID(job_id).hex
        except (ValueError, AttributeError):
            raise ValueError("invalid job id") from None
        if job_id != canonical or path.stem != job_id:
            raise ValueError("job id mismatch")
        if payload.get("status") != "pending":
            raise ValueError("invalid job status")
        created = payload.get("created_at")
        if not isinstance(created, int) or isinstance(created, bool) or created <= 0:
            raise ValueError("invalid created_at")
        request = cls._bounded_text(
            payload.get("request"),
            limit=MAX_LEARNING_REQUEST_CHARS,
            label="learning request",
        )
        source = payload.get("source")
        if not isinstance(source, dict) or set(source) != {
            "stable_device_id", "capture_id", "user_turn_id"
        }:
            raise ValueError("invalid job source")
        device = cls._bounded_text(
            source.get("stable_device_id"), limit=200, label="device id"
        )
        capture = str(source.get("capture_id") or "").strip()
        if len(capture) > 400 or any(ord(ch) < 32 for ch in capture):
            raise ValueError("invalid capture id")
        turn_id = source.get("user_turn_id")
        if not isinstance(turn_id, int) or isinstance(turn_id, bool) or turn_id < 0:
            raise ValueError("invalid user turn id")
        return {
            "schema_version": 1,
            "id": job_id,
            "status": "pending",
            "created_at": created,
            "request": request,
            "source": {
                "stable_device_id": device,
                "capture_id": capture,
                "user_turn_id": turn_id,
            },
        }

    def submit(self, parameters: dict, turn) -> str:
        request = self._bounded_text(
            dict(parameters or {}).get("request"),
            limit=MAX_LEARNING_REQUEST_CHARS,
            label="learning request",
        )
        device_id = self._bounded_text(
            getattr(turn, "stable_device_id", "unknown-device") or "unknown-device",
            limit=200,
            label="device id",
        )
        # Idempotent while a same-device requirement is queued or running.
        for directory in (self.pending, self.dispatched):
            for path in directory.glob("*.json"):
                payload = self._read_payload(path)
                if not payload:
                    continue
                source = payload.get("source") or {}
                if (
                    payload.get("request") == request
                    and source.get("stable_device_id") == device_id
                ):
                    return _LEARNING_ACK

        job_id = uuid.uuid4().hex
        payload = {
            "schema_version": 1,
            "id": job_id,
            "status": "pending",
            "created_at": int(time.time()),
            "request": request,
            "source": {
                "stable_device_id": device_id,
                "capture_id": str(getattr(turn, "capture_id", "") or ""),
                "user_turn_id": int(getattr(turn, "user_turn_id", 0) or 0),
            },
        }
        self._atomic_write(self.pending, f"{job_id}.json", payload)
        return _LEARNING_ACK

    def write_completion(self, job_id: str, *, capability: str, status: str) -> Path:
        safe_id = self._bounded_text(job_id, limit=32, label="job id")
        try:
            canonical_id = uuid.UUID(safe_id).hex
        except (ValueError, AttributeError):
            raise ValueError("invalid job id") from None
        if safe_id != canonical_id:
            raise ValueError("invalid job id")
        source = self._read_payload(self.dispatched / f"{safe_id}.json")
        if not source or source.get("id") != safe_id or source.get("status") != "pending":
            raise ValueError("unknown dispatched job")
        safe_status = str(status or "").strip().lower()
        if safe_status not in {"completed", "failed"}:
            raise ValueError("invalid completion status")
        safe_capability = self._bounded_text(
            capability, limit=MAX_CAPABILITY_CHARS, label="capability"
        )
        file_name = f"{safe_id}.json"
        for settled in (self.completed, self.claimed, self.announced):
            if (settled / file_name).exists():
                raise ValueError("completion already recorded")
        return self._atomic_write(
            self.completed,
            file_name,
            {
                "schema_version": 1,
                "id": safe_id,
                "status": safe_status,
                "capability": safe_capability,
                "completed_at": int(time.time()),
            },
            overwrite=False,
        )

    @staticmethod
    def _announcement_text(payload: dict | None) -> str | None:
        if not payload or payload.get("status") != "completed":
            return None
        try:
            capability = LearningQueue._bounded_text(
                payload.get("capability"),
                limit=MAX_CAPABILITY_CHARS,
                label="capability",
            )
        except ValueError:
            return None
        return f"So, jetzt kannst du die Funktion {capability} nutzen."

    def claim_completion_announcement(self) -> tuple[str, str] | None:
        """Claim one completed job for speaking; exactly one speaker wins.

        The record moves to ``claimed/`` before any audio is rendered, so a
        second turn cannot speak the same announcement. ``ack`` after the audio
        drained, or ``release`` when speaking failed.
        """
        for path in sorted(self.completed.glob("*.json")):
            payload = self._read_payload(path)
            text = self._announcement_text(payload)
            if not text:
                # Failed or unreadable records never reach the speaker; settle
                # them so they are not rescanned on every turn.
                with contextlib.suppress(OSError):
                    os.replace(path, self.announced / path.name)
                    self._fsync_dir(self.announced)
                continue
            target = self.claimed / path.name
            try:
                os.replace(path, target)
                os.chmod(target, 0o600)
                self._fsync_dir(self.claimed)
            except OSError:
                continue
            return text, path.stem
        return None

    def ack_completion_announcement(self, job_id: str) -> bool:
        """The announcement was streamed to the device: never speak it again."""
        file_name = f"{self._safe_job_id(job_id)}.json"
        source = self.claimed / file_name
        try:
            os.replace(source, self.announced / file_name)
            os.chmod(self.announced / file_name, 0o600)
            self._fsync_dir(self.announced)
        except OSError:
            return False
        return True

    def release_completion_announcement(self, job_id: str) -> bool:
        """Speaking failed: return the record so a later turn retries it."""
        file_name = f"{self._safe_job_id(job_id)}.json"
        try:
            os.replace(self.claimed / file_name, self.completed / file_name)
            os.chmod(self.completed / file_name, 0o600)
            self._fsync_dir(self.completed)
        except OSError:
            return False
        return True

    def take_completion_announcement(self) -> str | None:
        """Claim and acknowledge in one step (single-shot speakers)."""
        claim = self.claim_completion_announcement()
        if claim is None:
            return None
        text, job_id = claim
        if not self.ack_completion_announcement(job_id):
            return None
        return text

    def _safe_job_id(self, job_id: str) -> str:
        safe_id = self._bounded_text(job_id, limit=32, label="job id")
        try:
            canonical_id = uuid.UUID(safe_id).hex
        except (ValueError, AttributeError):
            raise ValueError("invalid job id") from None
        if safe_id != canonical_id:
            raise ValueError("invalid job id")
        return safe_id


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the signed body must only reach the configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LearningDispatcher:
    """HMAC-dispatch pending requirements to one private Hermes webhook."""

    @staticmethod
    def _no_redirect_handler() -> urllib.request.HTTPRedirectHandler:
        return _NoRedirect()

    @staticmethod
    def _is_private_host(hostname: str) -> bool:
        host = (hostname or "").strip().strip("[]").lower()
        if host in {"localhost", "::1"} or host.endswith(".localhost"):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return host.endswith(".local") or host.endswith(".internal")
        return (
            address.is_loopback
            or address.is_private
            or address.is_link_local
        )

    def __init__(
        self,
        queue: LearningQueue,
        *,
        webhook_url: str,
        secret: str,
        opener=None,
        timeout: float = 10.0,
    ) -> None:
        parsed = urllib.parse.urlparse(str(webhook_url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid webhook URL")
        if parsed.username or parsed.password:
            raise ValueError("webhook URL must not contain credentials")
        if parsed.scheme == "http" and not self._is_private_host(parsed.hostname):
            raise ValueError("cleartext webhook URL must stay on a private host")
        self.queue = queue
        self.webhook_url = str(webhook_url)
        self.secret = LearningQueue._bounded_text(
            secret, limit=512, label="webhook secret"
        )
        if opener is not None:
            self.opener = opener
        else:
            self._url_opener = urllib.request.build_opener(self._no_redirect_handler())
            self.opener = self._url_opener.open
        self.timeout = float(timeout)

    def _quarantine(self, path: Path) -> None:
        """Park an invalid record out of the dispatch loop, keeping it auditable."""
        try:
            target = self.queue.quarantine / path.name
            os.replace(path, target)
            os.chmod(target, 0o600)
            self.queue._fsync_dir(self.queue.quarantine)
        except OSError:
            pass

    def dispatch_once(self) -> int:
        accepted = 0
        for path in sorted(self.queue.pending.glob("*.json")):
            raw = self.queue._read_payload(path)
            try:
                job = self.queue.validate_job(path, raw)
            except ValueError:
                self._quarantine(path)
                continue
            body = json.dumps(
                {"event": "jarvis.learning.request", "job": job},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            signature = "sha256=" + hmac.new(
                self.secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            request = urllib.request.Request(
                self.webhook_url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "jarvis.learning.request",
                    "X-GitHub-Delivery": str(job.get("id") or path.stem),
                    "X-Hub-Signature-256": signature,
                },
            )
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", 0) or 0)
            except (OSError, ValueError):
                continue
            if not 200 <= status < 300:
                continue
            target = self.queue.dispatched / path.name
            try:
                os.replace(path, target)
                os.chmod(target, 0o600)
                self.queue._fsync_dir(self.queue.dispatched)
            except OSError:
                continue
            accepted += 1
        return accepted
