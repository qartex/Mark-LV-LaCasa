#!/usr/bin/env python3
"""Guarded ESP32-S3 backup / build-registration / flash / restore CLI.

Plan Task 2 (docs/superpowers/plans/2026-10-17-waveshare-esp32-satellite-phase1.md):
a persistent owner-only SafetyManifest binds a device fingerprint, a full 16 MB
backup, an exact ESPHome ``firmware.factory.bin`` artifact and the write map
(offset 0x0, erase-all, 16MB/dio/80m, verification range) across separate CLI
invocations. Every destructive step requires a fresh one-use, nonsecret
confirmation with a <=10-minute lifetime, and the flash/restore write may only
start after the guard durably recorded ``flash_write_started`` /
``flash_may_have_mutated``.

Safety rules enforced here:
- Subprocesses are invoked with argv lists only; shell interpolation never happens.
- esptool output is never echoed; diagnostics are short, bounded status lines.
- Manifests and backups are mode-0600 files in mode-0700 real directories
  (symlinked or permissive ancestors are refused, O_NOFOLLOW).
- A flash confirmation can never authorize a restore; restore additionally
  requires the durable ``flash_may_have_mutated`` transition, which covers
  interrupted and partial writes.
- Read-verify always runs (with ``--after no-reset``) before the first firmware
  boot; the chip is reset only after the verification result is durable.

This module uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat as stat_module
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Protocol

DEFAULT_ESPTOOL = "esptool"
DEFAULT_BAUD = 921600
CHIP_ARG = "esp32s3"
REQUIRED_CHIP = "ESP32-S3"
FLASH_SIZE_BYTES = 16 * 1024 * 1024  # 16777216
WRITE_OFFSET = 0x0
ERASE_ALL = True
FLASH_MODE = "dio"
FLASH_FREQ = "80m"
FLASH_SIZE_LABEL = "16MB"
FACTORY_ARTIFACT_NAME = "firmware.factory.bin"
CONFIRMATION_TTL_S = 600  # <= 10 minutes
ESPTOOL_TIMEOUT_S = 900.0
MAX_MESSAGE_CHARS = 400
MANIFEST_NAME = "safety-manifest.json"
MAX_MANIFEST_BYTES = 262144
VERIFY_FILE_PREFIX = "verify-"
FLASH_CONFIRMATION_PREFIX = "f-"
RESTORE_CONFIRMATION_PREFIX = "r-"
DEFAULT_PROVENANCE = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "esp32-satellite" / "vendor" / "provenance.json"
)


class GuardError(Exception):
    """Bounded, secret-free refusal. Runner output is never included."""

    def __init__(self, message: str):
        super().__init__(_bounded(message))


class RunResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, argv: list[str]) -> RunResult: ...


class SubprocessRunner:
    """Executes argv lists strictly; no shell, no string commands."""

    def __init__(self, *, timeout_s: float = ESPTOOL_TIMEOUT_S):
        self.timeout_s = float(timeout_s)

    def run(self, argv: list[str]) -> RunResult:
        if not isinstance(argv, list) or not argv or not all(isinstance(t, str) for t in argv):
            raise GuardError(
                "runner requires argv as list[str]; shell strings and interpolation are not supported"
            )
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise GuardError(
                f"esptool timed out after {self.timeout_s:g}s (output suppressed)"
            ) from None
        except OSError as exc:
            raise GuardError(
                f"esptool could not be executed: {type(exc).__name__} (output suppressed)"
            ) from None
        return RunResult(
            int(completed.returncode if completed.returncode is not None else 0),
            completed.stdout or "",
            completed.stderr or "",
        )


class ProbeResult(NamedTuple):
    port: Path
    chip: str
    mac: str
    flash_size: int
    fingerprint: str


class BackupResult(NamedTuple):
    path: Path
    sha256: str
    size: int
    fingerprint: str


class RegisterBuildResult(NamedTuple):
    artifact_path: Path
    sha256: str
    length: int
    write_offset: int
    erase_all: bool
    flash_mode: str
    flash_freq: str
    flash_size: str
    verify_start: int
    verify_end: int


class Confirmation(NamedTuple):
    id: str
    kind: str
    issued_at: str
    expires_at: str


class FlashResult(NamedTuple):
    fingerprint: str
    artifact_sha256: str
    verified_bytes: int
    reset_performed: bool
    confirmation_id: str


class RestoreCheckResult(NamedTuple):
    eligible: bool
    reasons: list
    details: dict


class RestoreResult(NamedTuple):
    fingerprint: str
    backup_sha256: str
    verified_bytes: int
    reset_performed: bool
    confirmation_id: str


# --------------------------------------------------------------------------
# bounded, secret-free primitives
# --------------------------------------------------------------------------

_PRINTABLE_SUBSET = re.compile(r"[^\x20-\x7e]")


def _bounded(message: str) -> str:
    """Make any message short, single-line and free of control characters."""
    text = _PRINTABLE_SUBSET.sub(" ", str(message))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS - 3] + "..."
    return text


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# manifest persistence (atomic, owner-only, symlink-refusing)
# --------------------------------------------------------------------------

class ManifestStore:
    """Atomic mode-0600 JSON manifest inside a mode-0700 real directory."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @property
    def path(self) -> Path:
        return self.directory / MANIFEST_NAME

    def ensure(self) -> None:
        if self.directory.is_symlink():
            raise GuardError(f"manifest directory must not be a symlink: {self.directory}")
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700, parents=True)
            os.chmod(self.directory, 0o700)
        fd = self._open_dir_checked()
        os.close(fd)

    def has_manifest(self) -> bool:
        fd = self._open_dir_checked()
        try:
            os.stat(MANIFEST_NAME, dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(fd)

    def load(self) -> dict:
        fd = self._open_dir_checked()
        try:
            try:
                fd_file = os.open(MANIFEST_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise GuardError(
                    "no safety manifest found; run `probe` first"
                ) from None
            except OSError:
                raise GuardError(
                    "manifest must be a regular file inside a real directory; symlink refused"
                ) from None
            try:
                info = os.fstat(fd_file)
                if info.st_size > MAX_MANIFEST_BYTES:
                    raise GuardError("manifest file is implausibly large; refusing")
                if stat_module.S_IMODE(info.st_mode) & 0o077:
                    raise GuardError("manifest file must be owner-only (0600)")
                data = os.read(fd_file, MAX_MANIFEST_BYTES + 1)
            finally:
                os.close(fd_file)
        finally:
            os.close(fd)
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GuardError("manifest is not valid JSON (content withheld)") from None
        _validate_manifest(payload)
        return payload

    def save(self, manifest: dict) -> None:
        _validate_manifest(manifest)
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd = self._open_dir_checked()
        tmp_name = f".{MANIFEST_NAME}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            fd_tmp = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
            try:
                with os.fdopen(fd_tmp, "wb", closefd=True) as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, MANIFEST_NAME, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            except BaseException:
                try:
                    os.unlink(tmp_name, dir_fd=fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)

    def _open_dir_checked(self) -> int:
        try:
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise GuardError(
                f"manifest directory does not exist: {self.directory}"
            ) from None
        except OSError:
            raise GuardError(
                "manifest directory must be a real directory; symlink permissive "
                f"ancestor refused: {self.directory}"
            ) from None
        try:
            info = os.fstat(fd)
            if stat_module.S_IMODE(info.st_mode) & 0o077:
                raise GuardError(
                    f"manifest directory mode must be owner-only (0700 or stricter): {self.directory}"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd


def _validate_manifest(payload) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise GuardError("manifest schema mismatch; expected schema_version 1")
    for key in ("created_at", "updated_at", "device", "state", "confirmations"):
        if key not in payload:
            raise GuardError(f"manifest missing required field: {key}")
    device = payload["device"]
    if not isinstance(device, dict):
        raise GuardError("manifest device record missing")
    for key in ("chip", "mac", "fingerprint", "usb_port"):
        if not isinstance(device.get(key), str) or not device[key]:
            raise GuardError(f"manifest device field invalid: {key}")
    if not isinstance(device.get("flash_size"), int):
        raise GuardError("manifest device flash_size invalid")
    state = payload["state"]
    if not isinstance(state, dict):
        raise GuardError("manifest state record missing")
    confirmations = payload["confirmations"]
    if (not isinstance(confirmations, dict)
            or not isinstance(confirmations.get("pending"), dict)
            or not isinstance(confirmations.get("consumed"), list)):
        raise GuardError("manifest confirmations record invalid")
    for optional in ("backup", "build"):
        value = payload.get(optional)
        if value is not None and not isinstance(value, dict):
            raise GuardError(f"manifest {optional} record invalid")


def _new_manifest() -> dict:
    now = _now_iso()
    return {
        "schema_version": 1,
        "created_at": now,
        "updated_at": now,
        "device": None,
        "backup": None,
        "build": None,
        "state": {
            "flash_started_at": None,
            "flash_write_started_at": None,
            "flash_may_have_mutated_at": None,
            "flash_success": None,
            "flash_finished_at": None,
            "reset_performed": False,
            "reset_failed_at": None,
            "restore_write_started_at": None,
            "restore_result": None,
            "restore_finished_at": None,
            "restore_verified_bytes": None,
        },
        "confirmations": {"pending": {}, "consumed": []},
    }


def _touch(manifest: dict) -> None:
    manifest["updated_at"] = _now_iso()


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

_CHIP_RE = re.compile(r"(?im)^\s*chip\s+is\s+([A-Za-z0-9_-]+)")
_MAC_RE = re.compile(r"(?im)^\s*mac:\s*([0-9a-f:]{11,17})\s*$")
_SIZE_MB_RE = re.compile(r"(?im)^\s*detected\s+flash\s+size:\s*(\d+)\s*MB\s*$")
_SIZE_B_RE = re.compile(r"(?im)^\s*flash\s+size:\s*(\d+)\s*(?:bytes)?\s*$")


def _normalize_mac(text: str) -> str:
    cleaned = text.strip().lower()
    if ":" not in cleaned and len(cleaned) == 12:
        cleaned = ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))
    parts = cleaned.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise GuardError("probe: device MAC not found in flash-id output (output suppressed)")
    return ":".join(parts)


def parse_flash_id_output(stdout: str, port: Path) -> ProbeResult:
    chip = None
    mac = None
    size = None
    match = _CHIP_RE.search(stdout or "")
    if match:
        chip = match.group(1)
    match = _MAC_RE.search(stdout or "")
    if match:
        mac = _normalize_mac(match.group(1))
    if mac is None:
        raise GuardError("probe: device MAC not found in flash-id output (output suppressed)")
    match = _SIZE_MB_RE.search(stdout or "")
    if match:
        size = int(match.group(1)) * 1024 * 1024
    else:
        match = _SIZE_B_RE.search(stdout or "")
        if match:
            size = int(match.group(1))
    if chip != REQUIRED_CHIP:
        raise GuardError(
            f"probe: detected chip {chip or 'unknown'} is not {REQUIRED_CHIP}; aborting"
        )
    if size is None:
        raise GuardError(f"probe: flash size not detected; required {FLASH_SIZE_BYTES} bytes")
    if size != FLASH_SIZE_BYTES:
        raise GuardError(
            f"probe: detected flash size {size} bytes does not match required "
            f"{FLASH_SIZE_BYTES} bytes; aborting"
        )
    fingerprint = f"{REQUIRED_CHIP}:{mac}"
    return ProbeResult(port=Path(port), chip=chip, mac=mac, flash_size=size,
                       fingerprint=fingerprint)


def _esptool_argv(esptool: str, port, baud: int, after: str, verb: str, tail: list) -> list[str]:
    return [
        str(esptool), "--chip", CHIP_ARG, "--port", str(port), "--baud", str(baud),
        "--after", str(after), verb,
    ] + [str(item) for item in tail]


def _run(runner: Runner, argv: list[str], verb: str) -> RunResult:
    result = runner.run(argv)
    if result.returncode != 0:
        raise GuardError(
            f"esptool {verb} failed with exit code {result.returncode} (output suppressed)"
        )
    return result


def _probe_device(port, runner: Runner, esptool: str, baud: int) -> ProbeResult:
    # esptool 5.x dropped the esptool-4 `default-reset` choice; `hard-reset`
    # reproduces the classic post-probe reset behavior.
    argv = _esptool_argv(esptool, port, baud, "hard-reset", "flash-id", [])
    result = _run(runner, argv, "flash-id")
    return parse_flash_id_output(result.stdout, port)


def probe(port, runner: Runner, *, esptool: str = DEFAULT_ESPTOOL,
          baud: int = DEFAULT_BAUD, manifest_dir=None) -> ProbeResult:
    """Record ESP32-S3 identity, stable fingerprint, flash size and serial path."""
    result = _probe_device(port, runner, esptool, baud)
    if manifest_dir is not None:
        store = ManifestStore(Path(manifest_dir))
        store.ensure()
        manifest = store.load() if store.has_manifest() else _new_manifest()
        manifest["device"] = {
            "chip": result.chip,
            "usb_port": str(result.port),
            "mac": result.mac,
            "fingerprint": result.fingerprint,
            "flash_size": result.flash_size,
        }
        _touch(manifest)
        store.save(manifest)
    return result


def _require_device_match(manifest: dict, probed: ProbeResult) -> None:
    device = manifest.get("device")
    if not isinstance(device, dict):
        raise GuardError("manifest has no device record; run `probe` first")
    if device.get("fingerprint") != probed.fingerprint:
        raise GuardError(
            "device fingerprint mismatch: manifest has "
            f"{device.get('fingerprint')} but probe detected {probed.fingerprint}; "
            "refusing to continue"
        )
    if device.get("flash_size") != probed.flash_size:
        raise GuardError(
            f"device flash size mismatch: manifest has {device.get('flash_size')} "
            f"but probe detected {probed.flash_size}; refusing to continue"
        )


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------

def _require_private_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise GuardError(f"directory does not exist: {path}") from None
    except OSError:
        raise GuardError(
            f"directory must be a real, non-symlinked directory: {path}"
        ) from None
    try:
        info = os.fstat(fd)
        if stat_module.S_IMODE(info.st_mode) & 0o077:
            raise GuardError(
                f"directory must be owner-only (0700 or stricter): {path}"
            )
    finally:
        os.close(fd)


def backup(port, output, manifest_dir, runner: Runner, *,
           esptool: str = DEFAULT_ESPTOOL, baud: int = DEFAULT_BAUD) -> BackupResult:
    """Full-flash backup: no-reset read, non-overwriting image, fsync + SHA-256."""
    out = Path(output)
    if out.is_symlink():
        raise GuardError(f"backup output must not be a symlink: {out}")
    if out.exists():
        raise GuardError(f"refusing to overwrite existing backup output: {out}")
    _require_private_dir(out.parent)
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` first")
    manifest = store.load()
    probed = _probe_device(port, runner, esptool, baud)
    _require_device_match(manifest, probed)
    argv = _esptool_argv(esptool, port, baud, "no-reset", "read-flash",
                         [WRITE_OFFSET, FLASH_SIZE_BYTES, out])
    try:
        _run(runner, argv, "read-flash")
    except GuardError:
        _remove_unlink(out)
        raise
    if not out.exists():
        raise GuardError("esptool did not produce the backup image (output suppressed)")
    size = out.stat().st_size
    if size != FLASH_SIZE_BYTES:
        actual = size
        _remove_unlink(out)
        raise GuardError(
            f"backup short read: expected {FLASH_SIZE_BYTES} bytes, found {actual}; "
            "partial image removed"
        )
    try:
        fd = os.open(out, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(out, 0o600)
    except OSError:
        raise GuardError("could not finalize the backup image on disk (output suppressed)") from None
    digest = _sha256_file(out)
    manifest["backup"] = {
        "path": str(os.path.abspath(out)),
        "sha256": digest,
        "size": size,
        "created_at": _now_iso(),
    }
    _touch(manifest)
    store.save(manifest)
    return BackupResult(out, digest, size, probed.fingerprint)


def _remove_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# register-build
# --------------------------------------------------------------------------

def _image_info_is_s3(stdout: str) -> bool:
    return re.search(r"(?im)^\s*chip:\s*esp32-s3\b", stdout or "") is not None


def register_build(artifact, manifest_dir, runner: Runner, *,
                   esptool: str = DEFAULT_ESPTOOL, provenance=None) -> RegisterBuildResult:
    """Bind the exact factory artifact and write map; never flashes."""
    art = Path(artifact)
    if art.name != FACTORY_ARTIFACT_NAME:
        raise GuardError(
            f"register-build accepts only {FACTORY_ARTIFACT_NAME}; got {art.name}"
        )
    if art.is_symlink() or not art.is_file():
        raise GuardError(f"artifact must be a regular file: {art}")
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` first")
    manifest = store.load()
    prov_path = Path(provenance) if provenance is not None else DEFAULT_PROVENANCE
    if not prov_path.is_file():
        raise GuardError(f"provenance file not found: {prov_path}; pass --provenance")
    try:
        source_provenance = json.loads(prov_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GuardError("provenance file is not readable JSON (content withheld)") from None
    if not isinstance(source_provenance, dict):
        raise GuardError("provenance file must contain a JSON object")
    result = _run(runner, [str(esptool), "image-info", str(art)], "image-info")
    if not _image_info_is_s3(result.stdout):
        raise GuardError(
            f"esptool image-info did not identify {REQUIRED_CHIP} for "
            f"{art.name} (output suppressed)"
        )
    length = art.stat().st_size
    if length <= 0:
        raise GuardError("factory artifact is empty")
    if length > FLASH_SIZE_BYTES:
        raise GuardError(f"factory artifact is larger than flash ({length} bytes)")
    digest = _sha256_file(art)
    build = {
        "artifact_type": FACTORY_ARTIFACT_NAME,
        "artifact_path": str(os.path.abspath(art)),
        "artifact_sha256": digest,
        "artifact_length": length,
        "source_provenance": source_provenance,
        "write_offset": WRITE_OFFSET,
        "erase_all": ERASE_ALL,
        "flash_mode": FLASH_MODE,
        "flash_freq": FLASH_FREQ,
        "flash_size": FLASH_SIZE_LABEL,
        "verify_start": 0,
        "verify_end": length,
        "registered_at": _now_iso(),
    }
    manifest["build"] = build
    _touch(manifest)
    store.save(manifest)
    return RegisterBuildResult(
        art, digest, length, build["write_offset"], build["erase_all"],
        build["flash_mode"], build["flash_freq"], build["flash_size"],
        build["verify_start"], build["verify_end"],
    )


# --------------------------------------------------------------------------
# confirmation issue/consume
# --------------------------------------------------------------------------

def _canonical_binding(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def flash_binding(manifest: dict) -> str:
    return _canonical_binding({
        "kind": "flash",
        "device_fingerprint": manifest["device"]["fingerprint"],
        "flash_size": manifest["device"]["flash_size"],
        "backup_sha256": manifest["backup"]["sha256"],
        "backup_size": manifest["backup"]["size"],
        "artifact_type": manifest["build"]["artifact_type"],
        "artifact_sha256": manifest["build"]["artifact_sha256"],
        "artifact_length": manifest["build"]["artifact_length"],
        "write_offset": manifest["build"]["write_offset"],
        "erase_all": manifest["build"]["erase_all"],
        "flash_mode": manifest["build"]["flash_mode"],
        "flash_freq": manifest["build"]["flash_freq"],
        "flash_size": manifest["build"]["flash_size"],
        "verify_start": manifest["build"]["verify_start"],
        "verify_end": manifest["build"]["verify_end"],
    })


def restore_binding(manifest: dict) -> str:
    return _canonical_binding({
        "kind": "restore",
        "device_fingerprint": manifest["device"]["fingerprint"],
        "flash_size": manifest["device"]["flash_size"],
        "backup_sha256": manifest["backup"]["sha256"],
        "write_offset": WRITE_OFFSET,
        "erase_all": ERASE_ALL,
        "restore_length": FLASH_SIZE_BYTES,
        "verify_start": 0,
        "verify_end": FLASH_SIZE_BYTES,
    })


def _issue_confirmation(manifest: dict, kind: str) -> Confirmation:
    prefix = FLASH_CONFIRMATION_PREFIX if kind == "flash" else RESTORE_CONFIRMATION_PREFIX
    confirmation_id = prefix + secrets.token_hex(4)
    issued = _utcnow()
    expires = issued + timedelta(seconds=CONFIRMATION_TTL_S)
    manifest["confirmations"]["pending"][kind] = {
        "id": confirmation_id,
        "kind": kind,
        "binding": flash_binding(manifest) if kind == "flash" else restore_binding(manifest),
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }
    return Confirmation(confirmation_id, kind, issued.isoformat(), expires.isoformat())


def issue_flash_confirmation(manifest_dir) -> Confirmation:
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` and `backup` first")
    manifest = store.load()
    if not manifest.get("backup"):
        raise GuardError(
            "issue-flash-confirmation requires a verified full-flash backup; run backup first"
        )
    if not manifest.get("build"):
        raise GuardError(
            "issue-flash-confirmation requires a registered factory build; run register-build first"
        )
    if manifest["state"].get("flash_success") is True:
        raise GuardError(
            "flash already recorded as successful; refusing to issue another flash confirmation"
        )
    confirmation = _issue_confirmation(manifest, "flash")
    _touch(manifest)
    store.save(manifest)
    return confirmation


def _consume_confirmation(manifest: dict, store: ManifestStore, kind: str,
                          confirmation_id) -> None:
    if not isinstance(confirmation_id, str) or not confirmation_id:
        raise GuardError(f"a {kind} confirmation id is required")
    confirmations = manifest["confirmations"]
    pending = confirmations["pending"]
    consumed = confirmations["consumed"]
    for other_kind, entry in pending.items():
        if other_kind != kind and entry.get("id") == confirmation_id:
            raise GuardError(
                f"the provided id is a {other_kind} confirmation; {kind} requires its own "
                "separately issued confirmation"
            )
    for record in consumed:
        if record.get("id") == confirmation_id:
            if record.get("kind") != kind:
                raise GuardError(
                    f"the provided id is a {record.get('kind')} confirmation; {kind} "
                    "requires its own confirmation"
                )
            raise GuardError(f"{kind} confirmation was already consumed; request a new one")
    entry = pending.get(kind)
    if not isinstance(entry, dict) or entry.get("id") != confirmation_id:
        raise GuardError(
            f"no pending {kind} confirmation matches that id; request one with "
            f"issue-{kind}-confirmation"
        )
    if entry.get("kind") != kind:
        raise GuardError(
            f"the pending confirmation is not a {kind} confirmation; refusing"
        )
    try:
        expires = _parse_iso(entry["expires_at"])
    except (KeyError, ValueError):
        raise GuardError(f"{kind} confirmation has an unreadable expiry; refusing") from None
    if _utcnow() >= expires:
        pending.pop(kind, None)
        _touch(manifest)
        store.save(manifest)
        raise GuardError(f"{kind} confirmation expired at {entry['expires_at']}")
    expected = flash_binding(manifest) if kind == "flash" else restore_binding(manifest)
    if entry.get("binding") != expected:
        raise GuardError(
            f"{kind} confirmation binding no longer matches the recorded device, backup or "
            "write map; refusing to continue"
        )
    pending.pop(kind, None)
    consumed.append({
        "id": confirmation_id,
        "kind": kind,
        "consumed_at": _now_iso(),
    })
    _touch(manifest)
    store.save(manifest)


def issue_restore_confirmation(manifest_dir) -> Confirmation:
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` and `backup` first")
    manifest = store.load()
    state = manifest["state"]
    if not state.get("flash_may_have_mutated_at"):
        raise GuardError(
            "issue-restore-confirmation requires the durable flash_may_have_mutated "
            "transition; failures before that transition never authorize a restore"
        )
    if not manifest.get("backup"):
        raise GuardError("manifest has no bound backup to restore")
    confirmation = _issue_confirmation(manifest, "restore")
    _touch(manifest)
    store.save(manifest)
    return confirmation


# --------------------------------------------------------------------------
# flash
# --------------------------------------------------------------------------

def _recheck_image(record: dict, label: str, *, path_key: str = "path",
                   sha_key: str = "sha256", size_key: str = "size") -> tuple[Path, str]:
    path = Path(record[path_key])
    if path.is_symlink() or not path.is_file():
        raise GuardError(f"bound {label} image is missing: {path}")
    try:
        info = path.stat()
    except OSError:
        raise GuardError(f"bound {label} image is missing: {path}") from None
    if info.st_size != record[size_key]:
        raise GuardError(
            f"bound {label} image size drift ({info.st_size} != {record[size_key]}); refusing"
        )
    digest = _sha256_file(path)
    if digest != record[sha_key]:
        raise GuardError(f"bound {label} image sha256 drift; refusing (re-record it)")
    return path, digest


def _verify_tmp_name(store: ManifestStore, token: str) -> Path:
    return store.directory / f"{VERIFY_FILE_PREFIX}{token}.bin"


def flash(port, confirmation_id, manifest_dir, runner: Runner, *,
          esptool: str = DEFAULT_ESPTOOL, baud: int = DEFAULT_BAUD) -> FlashResult:
    """One guarded write: re-probe, re-hash, consume confirmation, durable
    transition, bound write, verify before boot, mark success, reset."""
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` first")
    manifest = store.load()
    state = manifest["state"]
    if state.get("flash_success") is True:
        raise GuardError(
            "flash already recorded as successful; refusing to write again"
        )
    probed = _probe_device(port, runner, esptool, baud)
    _require_device_match(manifest, probed)
    backup_record = manifest.get("backup")
    if not backup_record:
        raise GuardError("manifest has no bound backup; run backup first")
    _recheck_image(backup_record, "backup")
    build = manifest.get("build")
    if not build:
        raise GuardError("manifest has no registered factory build; run register-build first")
    _recheck_image(build, "factory", path_key="artifact_path",
                   sha_key="artifact_sha256", size_key="artifact_length")
    _consume_confirmation(manifest, store, "flash", confirmation_id)
    # Durable transition: both flags land in one atomic manifest write BEFORE
    # any erase/write subprocess is invoked; interruption after this point
    # keeps the device restore-eligible.
    now = _now_iso()
    state["flash_started_at"] = now
    state["flash_write_started_at"] = now
    state["flash_may_have_mutated_at"] = now
    _touch(manifest)
    store.save(manifest)
    artifact_path = build["artifact_path"]
    verify_file = _verify_tmp_name(store, secrets.token_hex(4))
    try:
        _run(runner, _esptool_argv(esptool, port, baud, "no-reset", "erase-flash", []),
             "erase-flash")
        _run(runner, _esptool_argv(
            esptool, port, baud, "no-reset", "write-flash",
            ["--flash-mode", build["flash_mode"], "--flash-freq", build["flash_freq"],
             "--flash-size", build["flash_size"], _hex_offset(build["write_offset"]),
             artifact_path]), "write-flash")
        _run(runner, _esptool_argv(esptool, port, baud, "no-reset", "read-flash",
                                   [_hex_offset(build["verify_start"]),
                                    build["verify_end"], verify_file]), "read-flash")
        if not verify_file.exists():
            raise GuardError(
                "post-flash verification read produced no image (output suppressed)")
        verified_digest = _sha256_file(verify_file)
        verified_size = verify_file.stat().st_size
        if (verified_digest != build["artifact_sha256"]
                or verified_size != build["artifact_length"]):
            raise GuardError(
                f"post-flash verification of bound range "
                f"[{_hex_offset(build['verify_start'])},{build['verify_end']}) failed "
                "(output suppressed)"
            )
        state["flash_success"] = True
        state["flash_finished_at"] = _now_iso()
        _touch(manifest)
        store.save(manifest)
    except GuardError:
        state["flash_success"] = False
        state["flash_finished_at"] = _now_iso()
        _touch(manifest)
        store.save(manifest)
        raise
    finally:
        _remove_unlink(verify_file)
    reset_performed = True
    try:
        _run(runner, _esptool_argv(esptool, port, baud, "hard-reset", "flash-id", []),
             "flash-id")
    except GuardError:
        reset_performed = False
        state["reset_performed"] = False
        state["reset_failed_at"] = _now_iso()
        _touch(manifest)
        store.save(manifest)
        raise
    state["reset_performed"] = True
    _touch(manifest)
    store.save(manifest)
    return FlashResult(probed.fingerprint, build["artifact_sha256"],
                       build["verify_end"] - build["verify_start"],
                       reset_performed, str(confirmation_id))


def _hex_offset(value: int) -> str:
    return f"0x{int(value):x}"


# --------------------------------------------------------------------------
# restore-check / restore
# --------------------------------------------------------------------------

def restore_check(manifest_dir) -> RestoreCheckResult:
    """Report restore eligibility without touching the device."""
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` first")
    manifest = store.load()
    reasons = []
    state = manifest["state"]
    backup_record = manifest.get("backup")
    if not state.get("flash_may_have_mutated_at"):
        reasons.append(
            "durable flash_may_have_mutated transition not present; pre-write failures "
            "never authorize a restore"
        )
    backup_sha = None
    backup_size = None
    if not backup_record:
        reasons.append("manifest has no bound backup record")
    else:
        backup_sha = backup_record["sha256"]
        backup_size = backup_record["size"]
        path = Path(backup_record["path"])
        if path.is_symlink() or not path.is_file():
            reasons.append(f"bound backup image is missing: {path}")
        else:
            if path.stat().st_size != backup_record["size"]:
                reasons.append(
                    f"backup image size drifts from manifest "
                    f"({path.stat().st_size} != {backup_record['size']})"
                )
            if _sha256_file(path) != backup_record["sha256"]:
                reasons.append("backup image sha256 drifts from manifest")
    details = {
        "write_offset": WRITE_OFFSET,
        "restore_length": FLASH_SIZE_BYTES,
        "erase_all": ERASE_ALL,
        "verify_start": 0,
        "verify_end": FLASH_SIZE_BYTES,
        "backup_sha256": backup_sha,
        "backup_size": backup_size,
        "backup_path": backup_record["path"] if backup_record else None,
    }
    return RestoreCheckResult(not reasons, reasons, details)


def restore(port, confirmation_id, manifest_dir, runner: Runner, *,
            esptool: str = DEFAULT_ESPTOOL, baud: int = DEFAULT_BAUD) -> RestoreResult:
    """Confirmed full-16MB rollback of the manifest-bound backup image."""
    store = ManifestStore(Path(manifest_dir))
    if not store.has_manifest():
        raise GuardError("no safety manifest found; run `probe` first")
    manifest = store.load()
    state = manifest["state"]
    if not state.get("flash_may_have_mutated_at"):
        raise GuardError(
            "restore requires the durable flash_may_have_mutated transition; failures "
            "before that transition never authorize a restore"
        )
    backup_record = manifest.get("backup")
    if not backup_record:
        raise GuardError("manifest has no bound backup to restore")
    probed = _probe_device(port, runner, esptool, baud)
    _require_device_match(manifest, probed)
    backup_path, backup_digest = _recheck_image(backup_record, "backup")
    _consume_confirmation(manifest, store, "restore", confirmation_id)
    state["restore_write_started_at"] = _now_iso()
    _touch(manifest)
    store.save(manifest)
    verify_file = _verify_tmp_name(store, secrets.token_hex(4))
    try:
        _run(runner, _esptool_argv(esptool, port, baud, "no-reset", "erase-flash", []),
             "erase-flash")
        _run(runner, _esptool_argv(
            esptool, port, baud, "no-reset", "write-flash",
            ["--flash-mode", FLASH_MODE, "--flash-freq", FLASH_FREQ,
             "--flash-size", FLASH_SIZE_LABEL, _hex_offset(WRITE_OFFSET),
             str(backup_path)]), "write-flash")
        _run(runner, _esptool_argv(esptool, port, baud, "no-reset", "read-flash",
                                   [_hex_offset(WRITE_OFFSET), FLASH_SIZE_BYTES,
                                    verify_file]), "read-flash")
        if not verify_file.exists():
            raise GuardError(
                "post-restore verification read produced no image (output suppressed)")
        verified_digest = _sha256_file(verify_file)
        verified_size = verify_file.stat().st_size
        if verified_digest != backup_digest or verified_size != FLASH_SIZE_BYTES:
            raise GuardError(
                f"post-restore verification of full range "
                f"[{_hex_offset(WRITE_OFFSET)},{FLASH_SIZE_BYTES}) failed "
                "(output suppressed)"
            )
        state["restore_result"] = "success"
        state["restore_finished_at"] = _now_iso()
        state["restore_verified_bytes"] = verified_size
        _touch(manifest)
        store.save(manifest)
    except GuardError:
        state["restore_result"] = "failed"
        state["restore_finished_at"] = _now_iso()
        _touch(manifest)
        store.save(manifest)
        raise
    finally:
        _remove_unlink(verify_file)
    reset_performed = True
    try:
        _run(runner, _esptool_argv(esptool, port, baud, "hard-reset", "flash-id", []),
             "flash-id")
    except GuardError:
        reset_performed = False
        state["reset_performed"] = False
        state["reset_failed_at"] = _now_iso()
        _touch(manifest)
        store.save(manifest)
        raise
    state["reset_performed"] = True
    _touch(manifest)
    store.save(manifest)
    return RestoreResult(probed.fingerprint, backup_digest, FLASH_SIZE_BYTES,
                         reset_performed, str(confirmation_id))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="esp32_flash_guard",
        description="Persistent-manifest guarded backup/flash/restore for the Waveshare ESP32-S3",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--manifest-dir", required=True, help="owner-only manifest directory")
    shared.add_argument("--esptool", default=DEFAULT_ESPTOOL, help="esptool executable")
    shared.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    device = argparse.ArgumentParser(add_help=False)
    device.add_argument("--port", required=True, help="USB serial port")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_probe = subparsers.add_parser("probe", parents=[shared, device])
    p_probe.set_defaults(func=_cmd_probe)

    p_backup = subparsers.add_parser("backup", parents=[shared, device])
    p_backup.add_argument("--output", required=True, help="backup image path (must not exist)")
    p_backup.set_defaults(func=_cmd_backup)

    p_register = subparsers.add_parser("register-build", parents=[shared])
    p_register.add_argument("--artifact", required=True)
    p_register.add_argument("--provenance", default=None)
    p_register.set_defaults(func=_cmd_register_build)

    p_issue_flash = subparsers.add_parser("issue-flash-confirmation", parents=[shared])
    p_issue_flash.set_defaults(func=_cmd_issue_flash_confirmation)

    p_flash = subparsers.add_parser("flash", parents=[shared, device])
    p_flash.add_argument("--confirmation", required=True)
    p_flash.set_defaults(func=_cmd_flash)

    p_restore_check = subparsers.add_parser("restore-check", parents=[shared])
    p_restore_check.set_defaults(func=_cmd_restore_check)

    p_issue_restore = subparsers.add_parser("issue-restore-confirmation", parents=[shared])
    p_issue_restore.set_defaults(func=_cmd_issue_restore_confirmation)

    p_restore = subparsers.add_parser("restore", parents=[shared, device])
    p_restore.add_argument("--confirmation", required=True)
    p_restore.set_defaults(func=_cmd_restore)
    return parser


def _cmd_probe(args, runner: Runner) -> None:
    result = probe(Path(args.port), runner, esptool=args.esptool, baud=args.baud,
                   manifest_dir=Path(args.manifest_dir))
    print(f"probe ok: chip={result.chip} mac={result.mac} flash_size={result.flash_size} "
          f"port={result.port}")
    print(f"probe ok: fingerprint={result.fingerprint} bound to manifest")


def _cmd_backup(args, runner: Runner) -> None:
    result = backup(Path(args.port), args.output, args.manifest_dir, runner,
                    esptool=args.esptool, baud=args.baud)
    print(f"backup ok: image={result.path} bytes={result.size}")
    print(f"backup ok: sha256={result.sha256} fingerprint={result.fingerprint}")


def _cmd_register_build(args, runner: Runner) -> None:
    result = register_build(args.artifact, args.manifest_dir, runner,
                            esptool=args.esptool, provenance=args.provenance)
    print(f"register-build ok: artifact={result.artifact_path} type={FACTORY_ARTIFACT_NAME} "
          f"bytes={result.length}")
    print(f"register-build ok: sha256={result.sha256} offset={_hex_offset(result.write_offset)} "
          f"erase_all={str(result.erase_all).lower()} "
          f"flash_mode={result.flash_mode} flash_freq={result.flash_freq} "
          f"flash_size={result.flash_size} "
          f"verify=[{_hex_offset(result.verify_start)},{result.verify_end})")


def _cmd_issue_flash_confirmation(args, runner: Runner) -> None:
    confirmation = issue_flash_confirmation(args.manifest_dir)
    print(f"issue ok: kind={confirmation.kind} id={confirmation.id} "
          f"expires={confirmation.expires_at}")


def _cmd_flash(args, runner: Runner) -> None:
    result = flash(Path(args.port), args.confirmation, args.manifest_dir, runner,
                   esptool=args.esptool, baud=args.baud)
    print(f"flash ok: verified bytes={result.verified_bytes} sha256={result.artifact_sha256}")
    print(f"flash ok: reset {'issued' if result.reset_performed else 'failed'} "
          f"confirmation={result.confirmation_id}")


def _cmd_restore_check(args, runner: Runner) -> None:
    result = restore_check(args.manifest_dir)
    details = " ".join(f"{key}={value}" for key, value in result.details.items())
    print(f"restore-check: eligible={str(result.eligible).lower()}")
    print(f"restore-check: {details}")
    for reason in result.reasons:
        print(f"restore-check: reason={reason}")
    if not result.eligible:
        raise GuardError("restore-check: not eligible; " + "; ".join(result.reasons[:2]))


def _cmd_issue_restore_confirmation(args, runner: Runner) -> None:
    confirmation = issue_restore_confirmation(args.manifest_dir)
    print(f"issue ok: kind={confirmation.kind} id={confirmation.id} "
          f"expires={confirmation.expires_at}")


def _cmd_restore(args, runner: Runner) -> None:
    result = restore(Path(args.port), args.confirmation, args.manifest_dir, runner,
                     esptool=args.esptool, baud=args.baud)
    print(f"restore ok: verified bytes={result.verified_bytes} sha256={result.backup_sha256}")
    print(f"restore ok: reset {'issued' if result.reset_performed else 'failed'} "
          f"confirmation={result.confirmation_id}")


def main(argv=None, *, runner: "Runner | None" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    runner = runner if runner is not None else SubprocessRunner()
    try:
        args.func(args, runner)
    except GuardError as exc:
        print(f"esp32-flash-guard: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())