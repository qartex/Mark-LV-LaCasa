"""Contract tests for scripts/esp32_flash_guard.py (plan Task 2).

Pure stdlib. Every test drives the guard through a fake esptool runner, so no
physical device, USB port or subprocess is ever touched. The fake emits
esptool-shaped output so parsing, binding and ordering rules are exercised
end to end.
"""

import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import esp32_flash_guard as guard  # noqa: E402

MAC1 = "a8:48:fa:11:22:33"
MAC2 = "a8:48:fa:aa:bb:cc"
FLASH_SIZE = 16 * 1024 * 1024  # 16777216
BACKUP_PATTERN = bytes(range(256)) * 65536  # exactly 16777216 bytes
BACKUP_SHA256 = hashlib.sha256(BACKUP_PATTERN).hexdigest()
ARTIFACT_BYTES = bytes(range(256)) * 1024  # exactly 262144 bytes
ARTIFACT_LEN = len(ARTIFACT_BYTES)
ARTIFACT_SHA256 = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
UPSTREAM_COMMIT = "351067f95dffd072b53ce7f00b0a99ce3077ed06"
FAKE_ESPTOOL_SECRET = "SECRET-PASSWORD-123 leak-marker"

VERBS = ("flash-id", "read-flash", "write-flash", "erase-flash", "image-info")


def _verb_of(argv):
    found = None
    for token in argv:
        if token in VERBS:
            found = token
    return found


class FakeEsptool:
    """In-process esptool stand-in honouring the guard's argv-list contract."""

    def __init__(self, *, chip="ESP32-S3", mac=MAC1, flash_mb=16,
                 image_chip=None, fail_verb=None, fail_rc=2, short_read=False,
                 read_override=None, raw_flash_id=None, fail_verb_once=None):
        self.chip = chip
        self.mac = mac
        self.flash_mb = flash_mb
        self.image_chip = image_chip if image_chip is not None else chip
        self.fail_verb = fail_verb
        self.fail_verb_once = fail_verb_once
        self.fail_rc = fail_rc
        self._verb_once_failed = set()
        self.short_read = short_read
        self.read_override = read_override
        self.raw_flash_id = raw_flash_id
        self.flash_contents = BACKUP_PATTERN
        self.invocations = []
        self.read_calls = []
        self.write_calls = []

    def run(self, argv):
        if not isinstance(argv, list) or not argv or not all(isinstance(t, str) for t in argv):
            raise AssertionError(f"runner requires argv as list[str], got {type(argv).__name__}")
        self.invocations.append(list(argv))
        verb = _verb_of(argv)
        if verb is None:
            raise AssertionError(f"no esptool verb found in argv {argv[:4]}...")
        if verb == self.fail_verb:
            return guard.RunResult(self.fail_rc, "partial stdout", FAKE_ESPTOOL_SECRET)
        if verb == self.fail_verb_once and verb not in self._verb_once_failed:
            self._verb_once_failed.add(verb)
            return guard.RunResult(self.fail_rc, "partial stdout", FAKE_ESPTOOL_SECRET)
        self._check_verb_args(argv, verb)
        return getattr(self, "_do_" + verb.replace("-", "_"))(argv)

    @staticmethod
    def _check_verb_args(argv, verb):
        if verb != "image-info" and "--chip" not in argv:
            raise AssertionError(f"{verb} argv lacks --chip")

    @staticmethod
    def _split_tail(argv, verb):
        tail = argv[argv.index(verb) + 1:]
        flags, positional = {}, []
        i = 0
        while i < len(tail):
            token = tail[i]
            if token.startswith("--"):
                flags[token] = tail[i + 1]
                i += 2
            else:
                positional.append(token)
                i += 1
        return flags, positional

    def _do_flash_id(self, argv):
        if self.raw_flash_id is not None:
            return guard.RunResult(0, self.raw_flash_id, "")
        text = (
            f"Chip is {self.chip} (revision v0.2)\n"
            "Features: WiFi, BLE\n"
            "Crystal is 40MHz\n"
            f"MAC: {self.mac}\n"
            "Uploading stub...\n"
            "Running stub...\n"
            "Stub flasher running.\n"
            "Manufacturer: 20\n"
            "Device: 4019\n"
            f"Detected flash size: {self.flash_mb}MB\n"
        )
        return guard.RunResult(0, text, "")

    def _do_image_info(self, argv):
        _, positional = self._split_tail(argv, "image-info")
        path = positional[-1]
        text = (
            f"Image: {path}\n"
            f"File size: {os.path.getsize(path)} bytes\n"
            f"Chip: {self.image_chip} (revision v0.2)\n"
            "Compile time: ...\n"
            "Entry point: 0x400000\n"
        )
        return guard.RunResult(0, text, "")

    def _do_read_flash(self, argv):
        _, positional = self._split_tail(argv, "read-flash")
        address, size, out = int(positional[0], 0), int(positional[1], 0), Path(positional[2])
        self.read_calls.append((address, size, str(out)))
        if self.read_override is not None:
            data = self.read_override
        else:
            data = self.flash_contents
        end = address + (size // 2 if self.short_read else size)
        out.write_bytes(data[address:end])
        return guard.RunResult(0, f"Read {size} bytes ok\n", "")

    def _do_write_flash(self, argv):
        flags, positional = self._split_tail(argv, "write-flash")
        offset, image_path = positional[0], Path(positional[1])
        self.write_calls.append({
            "offset": offset,
            "path": str(image_path),
            "flash_mode": flags.get("--flash-mode"),
            "flash_freq": flags.get("--flash-freq"),
            "flash_size": flags.get("--flash-size"),
        })
        self.flash_contents = image_path.read_bytes()
        return guard.RunResult(0, "Write ok\n", "")

    def _do_erase_flash(self, argv):
        return guard.RunResult(0, "Erase ok\n", "")


class GuardFixture(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp32-flash-guard-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp)
        self.priv = self.root / "private"
        self.store = guard.ManifestStore(self.priv)
        self.store.ensure()
        self.port = self.root / "cu.fake-modem"
        self.artifact_dir = self.root / "builds"
        self.artifact_dir.mkdir(mode=0o700)
        self.artifact = self.artifact_dir / "firmware.factory.bin"
        self.artifact.write_bytes(ARTIFACT_BYTES)
        self.backup_path = self.priv / "flash-backup-16MB.bin"
        self.prov = self.root / "provenance.json"
        self.prov.write_text(json.dumps({
            "upstream_core": {
                "repository": "https://github.com/MichalZaniewicz/esphome-waveshare-esp32-s3-audio-va",
                "commit": UPSTREAM_COMMIT,
                "sha256": "0" * 64,
            },
        }), encoding="utf-8")
        self.fake = FakeEsptool()

    # -- helpers -----------------------------------------------------------
    def bind_probe(self, fake=None):
        return guard.probe(self.port, self.fake if fake is None else fake,
                           manifest_dir=self.store.directory)

    def do_backup(self, fake=None, output=None):
        return guard.backup(self.port, output or self.backup_path,
                            self.store.directory, self.fake if fake is None else fake)

    def register(self, fake=None, provenance="unset"):
        kwargs = {}
        if provenance != "unset":
            kwargs["provenance"] = provenance
        return guard.register_build(self.artifact, self.store.directory,
                                    self.fake if fake is None else fake, **kwargs)

    def prepare_full(self):
        """probe -> backup -> register-build, ending at registration state."""
        self.bind_probe()
        self.do_backup()
        self.register()

    def mark_transition(self):
        m = self.store.load()
        m["state"]["flash_write_started_at"] = guard._now_iso()
        m["state"]["flash_may_have_mutated_at"] = guard._now_iso()
        self.store.save(m)

    def m(self, store=None):
        return (store or self.store).load()

    def save_pristine(self):
        self._pristine = copy.deepcopy(self.store.load())

    def save_manifest(self, m, store=None):
        (store or self.store).save(m)

    def verbs(self, fake):
        return [_verb_of(argv) for argv in fake.invocations]

    def assert_no_write(self, fake):
        for argv in fake.invocations:
            self.assertNotIn("write-flash", argv)
            self.assertNotIn("erase-flash", argv)

    def do_flash(self, fake=None, confirmation=None):
        conf = guard.issue_flash_confirmation(self.store.directory)
        return guard.flash(self.port, confirmation or conf.id, self.store.directory,
                           self.fake if fake is None else fake), conf

    def do_restore(self, fake=None, confirmation=None):
        conf = guard.issue_restore_confirmation(self.store.directory)
        return guard.restore(self.port, confirmation or conf.id, self.store.directory,
                             self.fake if fake is None else fake), conf


class TestProbe(GuardFixture):

    def test_probe_parses_identity_and_binds_stable_fingerprint(self):
        p = self.bind_probe()
        self.assertEqual("ESP32-S3", p.chip)
        self.assertEqual(MAC1, p.mac)
        self.assertEqual(FLASH_SIZE, p.flash_size)
        self.assertEqual(f"ESP32-S3:{MAC1}", p.fingerprint)
        self.assertEqual(self.port, p.port)
        m = self.m()
        self.assertEqual("ESP32-S3", m["device"]["chip"])
        self.assertEqual(MAC1, m["device"]["mac"])
        self.assertEqual(FLASH_SIZE, m["device"]["flash_size"])
        self.assertEqual(str(self.port), m["device"]["usb_port"])
        self.assertTrue(m["device"]["fingerprint"])
        self.assertTrue(m["created_at"])
        self.assertEqual(["flash-id"], self.verbs(self.fake))
        self.assertIsInstance(self.fake.invocations[0], list)

    def test_probe_is_idempotent_and_keeps_state(self):
        self.bind_probe()
        first = self.m()
        self.bind_probe()
        second = self.m()
        self.assertEqual(first["device"], second["device"])
        self.assertEqual(first["created_at"], second["created_at"])

    def test_probe_rejects_wrong_chip(self):
        with self.assertRaisesRegex(guard.GuardError, "chip"):
            self.bind_probe(FakeEsptool(chip="ESP32-S2"))
        self.assertFalse(self.store.has_manifest())

    def test_probe_rejects_unexpected_flash_size(self):
        with self.assertRaisesRegex(guard.GuardError, "flash size"):
            self.bind_probe(FakeEsptool(flash_mb=8))

    def test_probe_rejects_malformed_output(self):
        with self.assertRaisesRegex(guard.GuardError, "MAC"):
            fake = FakeEsptool()
            fake.raw_flash_id = "hello world\nno useful data\n"
            self.bind_probe(fake)

    def test_probe_argv_uses_default_esptool_and_list_form(self):
        self.bind_probe()
        argv = self.fake.invocations[0]
        self.assertEqual(guard.DEFAULT_ESPTOOL, argv[0])
        self.assertEqual("flash-id", argv[-1])


class TestBackup(GuardFixture):

    def test_backup_writes_exact_image_records_sha_mode0600_and_no_reset(self):
        self.bind_probe()
        fsyncs = []
        real_fsync = os.fsync

        def fsync_spy(fd):
            fsyncs.append(fd)
            return real_fsync(fd)

        with unittest.mock.patch("os.fsync", fsync_spy):
            r = self.do_backup()
        self.assertEqual(FLASH_SIZE, r.size)
        self.assertEqual(BACKUP_SHA256, r.sha256)
        self.assertEqual(self.backup_path, r.path)
        self.assertEqual(BACKUP_PATTERN, self.backup_path.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.backup_path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(self.priv).st_mode))
        self.assertGreaterEqual(len(fsyncs), 1)
        m = self.m()
        self.assertEqual(FLASH_SIZE, m["backup"]["size"])
        self.assertEqual(BACKUP_SHA256, m["backup"]["sha256"])
        self.assertEqual(str(self.backup_path), m["backup"]["path"])
        self.assertTrue(m["backup"]["created_at"])
        # The flash is read in chunks: the native USB-Serial-JTAG of the
        # ESP32-S3 drops the link during a long transfer (a full 16 MB read
        # dies at ~34 % with "No more data to read from the serial port",
        # and a lower baud does not help). A 4 MB block goes through
        # reliably, so the guard reads in pieces and joins them.
        reads = self.fake.read_calls
        self.assertGreater(len(reads), 1, "the backup must read in chunks")
        self.assertEqual(
            ["flash-id", "flash-id"] + ["read-flash"] * len(reads),
            self.verbs(self.fake),
        )
        for argv in self.fake.invocations[2:]:
            self.assertIn("--after", argv)
            self.assertEqual("no-reset", argv[argv.index("--after") + 1])
        # The chunks must cover the flash without a gap or an overlap.
        offset = 0
        for address, size, _path in reads:
            self.assertEqual(offset, address)
            self.assertLessEqual(size, guard.BACKUP_CHUNK_BYTES)
            offset += size
        self.assertEqual(FLASH_SIZE, offset)
        # And the joined image must be the whole flash.
        self.assertEqual(BACKUP_PATTERN, self.backup_path.read_bytes())

    def test_backup_refuses_existing_output_without_running_esptool(self):
        self.backup_path.write_bytes(b"pretend-existing")
        argv_before = list(self.fake.invocations)
        with self.assertRaisesRegex(guard.GuardError, "overwrite|existing"):
            self.do_backup()
        self.assertEqual(argv_before, self.fake.invocations)
        self.assertEqual(b"pretend-existing", self.backup_path.read_bytes())

    def test_backup_fails_on_short_read_and_removes_partial_image(self):
        self.bind_probe()
        with self.assertRaisesRegex(guard.GuardError, "short read"):
            self.do_backup(FakeEsptool(short_read=True))
        self.assertFalse(os.path.lexists(self.backup_path))
        self.assertIsNone(self.m()["backup"])

    def test_backup_refuses_symlinked_or_wrong_mode_output_directory(self):
        link = self.root / "link"
        real = self.root / "real-dir"
        real.mkdir(mode=0o700)
        link.symlink_to(real)
        loose = self.root / "loose-dir"
        loose.mkdir(mode=0o755)
        for bad_dir in (link, loose):
            with self.subTest(bad_dir=str(bad_dir)):
                out = bad_dir / "flash-backup-16MB.bin"
                with self.assertRaises(guard.GuardError):
                    self.do_backup(output=out)
                self.assertFalse(os.path.lexists(out))
                self.assert_no_write(self.fake)

    def test_backup_refuses_device_substitution(self):
        self.bind_probe()
        substitute = FakeEsptool(mac=MAC2)
        with self.assertRaisesRegex(guard.GuardError, "fingerprint"):
            self.do_backup(substitute)
        self.assertEqual(["flash-id"], self.verbs(substitute))
        self.assertFalse(os.path.lexists(self.backup_path))
        self.assertIsNone(self.m()["backup"])

    def test_backup_failure_cleans_partial_image_and_stays_secret_free(self):
        self.bind_probe()
        bad = FakeEsptool(fail_verb="read-flash")
        with self.assertRaises(guard.GuardError) as ctx:
            self.do_backup(bad)
        self.assertNotIn(FAKE_ESPTOOL_SECRET, str(ctx.exception))
        self.assertFalse(os.path.lexists(self.backup_path))
        self.assertIsNone(self.m()["backup"])


class TestManifestStore(GuardFixture):

    def test_manifest_write_is_atomic_private_and_crash_safe(self):
        self.bind_probe()
        path = self.store.path
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        before = path.read_bytes()
        self.assertFalse(list(self.priv.glob("*.tmp*")))
        with unittest.mock.patch("os.replace", side_effect=OSError(9, "injected")):
            with self.assertRaises(OSError):
                self.store.save(self.store.load())
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertFalse(list(self.priv.glob("*.tmp*")))

    def test_manifest_store_rejects_wrong_mode_and_symlink_directory(self):
        loose = self.root / "loose"
        loose.mkdir(mode=0o755)
        loose_store = guard.ManifestStore(loose)
        self.bind_probe()
        m = self.m()
        with self.assertRaisesRegex(guard.GuardError, "0700|owner"):
            loose_store.save(m)
        real = self.root / "sym-real"
        real.mkdir(mode=0o700)
        link = self.root / "sym-link"
        link.symlink_to(real)
        link_store = guard.ManifestStore(link)
        with self.assertRaisesRegex(guard.GuardError, "symlink|real directory"):
            guard.probe(self.port, self.fake, manifest_dir=link)
        loose_file = self.priv / guard.MANIFEST_NAME
        os.chmod(loose_file, 0o644)
        try:
            with self.assertRaisesRegex(guard.GuardError, "0600|owner"):
                self.store.load()
        finally:
            os.chmod(loose_file, 0o600)

    def test_commands_fail_closed_when_manifest_is_missing(self):
        empty = self.root / "empty"
        empty_store = guard.ManifestStore(empty)
        empty_store.ensure()
        fresh_fake = FakeEsptool()
        cases = {
            "backup": lambda: self.do_backup(output=empty / "x.bin"),
            "register-build": lambda: guard.register_build(
                self.artifact, empty, fresh_fake, provenance=self.prov),
            "issue-flash-confirmation": lambda: guard.issue_flash_confirmation(empty),
            "flash": lambda: guard.flash(self.port, "f-00000000", empty, fresh_fake),
            "issue-restore-confirmation": lambda: guard.issue_restore_confirmation(empty),
            "restore-check": lambda: guard.restore_check(empty),
            "restore": lambda: guard.restore(self.port, "r-00000000", empty, fresh_fake),
        }
        for name, fn in cases.items():
            with self.subTest(command=name):
                with self.assertRaisesRegex(guard.GuardError, "manifest"):
                    fn()
        self.assertEqual([], fresh_fake.invocations)


class TestRegisterBuild(GuardFixture):

    def test_register_build_records_exact_factory_binding(self):
        self.bind_probe()
        self.do_backup()
        fresh = FakeEsptool()
        r = self.register(fake=fresh)
        self.assertEqual(["image-info"], self.verbs(fresh))
        self.assertEqual(ARTIFACT_SHA256, r.sha256)
        self.assertEqual(ARTIFACT_LEN, r.length)
        self.assertEqual(0, r.write_offset)
        self.assertTrue(r.erase_all)
        self.assertEqual(("dio", "80m", "16MB"), (r.flash_mode, r.flash_freq, r.flash_size))
        self.assertEqual((0, ARTIFACT_LEN), (r.verify_start, r.verify_end))
        m = self.m()
        b = m["build"]
        self.assertEqual("firmware.factory.bin", b["artifact_type"])
        self.assertEqual(str(self.artifact), b["artifact_path"])
        self.assertEqual(ARTIFACT_SHA256, b["artifact_sha256"])
        self.assertEqual(ARTIFACT_LEN, b["artifact_length"])
        self.assertEqual(0, b["write_offset"])
        self.assertTrue(b["erase_all"])
        self.assertEqual({"flash_mode": "dio", "flash_freq": "80m", "flash_size": "16MB"},
                         {"flash_mode": b["flash_mode"], "flash_freq": b["flash_freq"],
                          "flash_size": b["flash_size"]})
        self.assertEqual((0, ARTIFACT_LEN), (b["verify_start"], b["verify_end"]))
        self.assertEqual(UPSTREAM_COMMIT, b["source_provenance"]["upstream_core"]["commit"])
        self.assertTrue(b["registered_at"])

    def test_register_build_uses_repo_provenance_by_default(self):
        self.prepare_full()
        fresh = FakeEsptool()
        self.register(fake=fresh, provenance=None)
        m = self.m()
        commit = m["build"]["source_provenance"].get("upstream_core", {}).get("commit")
        self.assertEqual(UPSTREAM_COMMIT, commit)

    def test_register_build_rejects_non_factory_artifacts(self):
        self.bind_probe()
        self.do_backup()
        other = self.artifact_dir / "firmware.bin"
        shutil.copyfile(self.artifact, other)
        fresh = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "firmware.factory.bin"):
            guard.register_build(other, self.store.directory, fresh, provenance=self.prov)
        self.assertEqual([], fresh.invocations)

    def test_register_build_rejects_wrong_chip_image(self):
        self.bind_probe()
        self.do_backup()
        with self.assertRaisesRegex(guard.GuardError, "ESP32-S3"):
            self.register(fake=FakeEsptool(image_chip="ESP32-S2"))

    def test_register_build_never_flashes(self):
        self.prepare_full()
        for argv in self.fake.invocations:
            self.assertNotIn("write-flash", argv)
            self.assertNotIn("erase-flash", argv)


class TestFlashConfirmations(GuardFixture):

    def test_issue_flash_confirmation_requires_backup_and_build(self):
        conf_id_re = re.compile(r"^f-[0-9a-f]{8}$")
        self.bind_probe()
        with self.assertRaisesRegex(guard.GuardError, "backup"):
            guard.issue_flash_confirmation(self.store.directory)
        self.do_backup()
        with self.assertRaisesRegex(guard.GuardError, "register-build|build"):
            guard.issue_flash_confirmation(self.store.directory)
        self.register()
        conf = guard.issue_flash_confirmation(self.store.directory)
        self.assertEqual("flash", conf.kind)
        self.assertLessEqual(len(conf.id), 24)
        self.assertTrue(conf_id_re.match(conf.id))
        issued = guard._parse_iso(conf.issued_at)
        expires = guard._parse_iso(conf.expires_at)
        delta = (expires - issued).total_seconds()
        self.assertLessEqual(delta, guard.CONFIRMATION_TTL_S)
        self.assertGreaterEqual(delta, guard.CONFIRMATION_TTL_S - 60)
        m = self.m()
        self.assertEqual(conf.id, m["confirmations"]["pending"]["flash"]["id"])
        m2 = self.m()
        self.assertEqual(guard.flash_binding(m2),
                         m2["confirmations"]["pending"]["flash"]["binding"])

    def test_issue_flash_confirmation_refused_after_successful_flash(self):
        self.prepare_full()
        self.do_flash()
        with self.assertRaisesRegex(guard.GuardError, "already"):
            guard.issue_flash_confirmation(self.store.directory)


class TestFlash(GuardFixture):

    def test_flash_verify_retries_a_transient_read_failure(self):
        self.prepare_full()
        fake = FakeEsptool(fail_verb_once="read-flash")
        saved = (guard.VERIFY_RETRY_DELAY_S, guard.VERIFY_RETRY_ATTEMPTS)
        guard.VERIFY_RETRY_DELAY_S = 0.0005
        try:
            result, conf = self.do_flash(fake)
        finally:
            guard.VERIFY_RETRY_DELAY_S, guard.VERIFY_RETRY_ATTEMPTS = saved
        self.assertEqual(2, sum(1 for v in self.verbs(fake) if v == "read-flash"))
        m = self.m()
        self.assertTrue(m["state"]["flash_success"])

    def test_flash_requires_valid_one_use_confirmation(self):
        self.prepare_full()
        with self.assertRaisesRegex(guard.GuardError, "confirmation"):
            guard.flash(self.port, "f-doesnotexist", self.store.directory, self.fake)
        result, conf = self.do_flash(fake=FakeEsptool())
        self.assertTrue(result.reset_performed)
        m = self.m()
        self.assertTrue(m["state"]["flash_success"])
        self.assertNotIn("flash", m["confirmations"]["pending"])
        consumed = m["confirmations"]["consumed"]
        self.assertEqual(1, len(consumed))
        self.assertEqual((conf.id, "flash"), (consumed[0]["id"], consumed[0]["kind"]))
        # replay of the consumed id is refused
        with self.assertRaisesRegex(guard.GuardError, "already"):
            guard.flash(self.port, conf.id, self.store.directory, FakeEsptool())

    def test_flash_refuses_wrong_kind_confirmation(self):
        self.prepare_full()
        self.mark_transition()
        restore_conf = guard.issue_restore_confirmation(self.store.directory)
        substitute = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "restore confirmation"):
            guard.flash(self.port, restore_conf.id, self.store.directory, substitute)
        self.assertEqual(["flash-id"], self.verbs(substitute))
        self.assert_no_write(substitute)

    def test_flash_aborts_on_device_substitution_before_transition(self):
        self.prepare_full()
        conf = guard.issue_flash_confirmation(self.store.directory)
        substitute = FakeEsptool(mac=MAC2)
        with self.assertRaisesRegex(guard.GuardError, "fingerprint"):
            guard.flash(self.port, conf.id, self.store.directory, substitute)
        state = self.m()["state"]
        self.assertIsNone(state["flash_write_started_at"])
        self.assertIsNone(state["flash_may_have_mutated_at"])
        self.assertEqual(["flash-id"], self.verbs(substitute))
        self.assert_no_write(substitute)
        self.assertIn("flash", self.m()["confirmations"]["pending"])

    def test_flash_aborts_on_backup_hash_drift(self):
        self.prepare_full()
        conf = guard.issue_flash_confirmation(self.store.directory)
        with self.backup_path.open("ab") as fh:
            fh.write(b"\x00")
        bad = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "backup"):
            guard.flash(self.port, conf.id, self.store.directory, bad)
        self.assertIsNone(self.m()["state"]["flash_write_started_at"])
        self.assertEqual(["flash-id"], self.verbs(bad))

    def test_flash_aborts_on_factory_image_hash_drift(self):
        self.prepare_full()
        conf = guard.issue_flash_confirmation(self.store.directory)
        with self.artifact.open("ab") as fh:
            fh.write(b"\x01")
        bad = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "factory|artifact"):
            guard.flash(self.port, conf.id, self.store.directory, bad)
        self.assertIsNone(self.m()["state"]["flash_write_started_at"])
        self.assertEqual(["flash-id"], self.verbs(bad))

    def test_flash_refuses_tampered_write_map_after_binding(self):
        self.prepare_full()
        self.save_pristine()
        conf = guard.issue_flash_confirmation(self.store.directory)
        mutations = [
            (("device", "fingerprint"), "ESP32-S3:zz:zz:zz:zz:zz:zz"),
            (("build", "write_offset"), 16),
            (("build", "erase_all"), False),
            (("build", "flash_mode"), "qio"),
            (("build", "flash_freq"), "40m"),
            (("build", "flash_size"), "8MB"),
            (("build", "verify_start"), 4),
            (("build", "verify_end"), ARTIFACT_LEN - 4),
            (("backup", "sha256"), "e" * 64),
        ]
        for path, value in mutations:
            with self.subTest(field=".".join(path)):
                m = copy.deepcopy(self._pristine)
                self.save_manifest(m)  # restore pristine state, pending cleared
                conf = guard.issue_flash_confirmation(self.store.directory)
                m = self.m()
                node = m
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                self.save_manifest(m)
                bad = FakeEsptool()
                with self.assertRaises(guard.GuardError):
                    guard.flash(self.port, conf.id, self.store.directory, bad)
                self.assert_no_write(bad)

    def test_flash_refuses_stale_confirmation(self):
        self.prepare_full()
        conf = guard.issue_flash_confirmation(self.store.directory)
        m = self.m()
        backdated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)).isoformat()
        m["confirmations"]["pending"]["flash"]["expires_at"] = backdated
        self.save_manifest(m)
        with self.assertRaisesRegex(guard.GuardError, "expired"):
            guard.flash(self.port, conf.id, self.store.directory, FakeEsptool())

    def test_flash_persists_durable_transition_before_any_write(self):
        self.prepare_full()
        spy = self.TransitionSpy(self.fake, self.store.path)
        self.do_flash(fake=spy)
        self.assertEqual(["erase-flash", "write-flash"], spy.seen)
        state = self.m()["state"]
        self.assertTrue(state["flash_write_started_at"])
        self.assertTrue(state["flash_may_have_mutated_at"])
        self.assertTrue(state["flash_success"])

    class TransitionSpy:
        def __init__(self, fake, manifest_path):
            self.fake = fake
            self.manifest_path = Path(manifest_path)
            self.seen = []

        def run(self, argv):
            verb = _verb_of(argv)
            if verb in ("erase-flash", "write-flash"):
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                st = data["state"]
                assert st.get("flash_write_started_at"), "transition missing at write time"
                assert st.get("flash_may_have_mutated_at"), "mutation flag missing at write time"
                self.seen.append(verb)
            return self.fake.run(argv)

    def test_flash_write_map_and_verify_precede_reset(self):
        self.prepare_full()
        flash_fake = FakeEsptool()
        result, conf = self.do_flash(fake=flash_fake)
        seq = self.verbs(flash_fake)
        self.assertEqual(["flash-id", "erase-flash", "write-flash", "read-flash", "flash-id"], seq)
        self.assertEqual({
            "offset": "0x0",
            "path": str(self.artifact),
            "flash_mode": "dio",
            "flash_freq": "80m",
            "flash_size": "16MB",
        }, flash_fake.write_calls[0])
        address, size, verify_out = flash_fake.read_calls[0]
        self.assertEqual((0, ARTIFACT_LEN), (address, size))
        verify_out_path = Path(verify_out)
        self.assertEqual(self.priv, verify_out_path.parent)
        self.assertNotEqual(verify_out, str(self.artifact))
        for argv in flash_fake.invocations[1:4]:
            self.assertIn("--after", argv)
            self.assertEqual("no-reset", argv[argv.index("--after") + 1])
        reset_argv = flash_fake.invocations[-1]
        self.assertIn("--after", reset_argv)
        self.assertEqual("hard-reset", reset_argv[reset_argv.index("--after") + 1])
        self.assertLess(seq.index("read-flash"), len(seq) - 1)
        self.assertEqual(ARTIFACT_LEN, result.verified_bytes)
        self.assertEqual(ARTIFACT_SHA256, result.artifact_sha256)
        self.assertTrue(self.m()["state"]["flash_success"])
        self.assertFalse(list(self.priv.glob("*verify*")))

    def test_flash_verification_failure_marks_failure_and_keeps_restore_eligible(self):
        self.prepare_full()
        bad = FakeEsptool(read_override=b"\x00" * ARTIFACT_LEN)
        with self.assertRaisesRegex(guard.GuardError, "verification"):
            self.do_flash(fake=bad)
        state = self.m()["state"]
        self.assertIs(False, state["flash_success"])
        self.assertTrue(state["flash_write_started_at"])
        conf = guard.issue_restore_confirmation(self.store.directory)
        self.assertTrue(conf.id.startswith("r-"))


class TestRestoreConfirmation(GuardFixture):

    def test_issue_restore_confirmation_requires_durable_transition(self):
        self.prepare_full()
        with self.assertRaisesRegex(guard.GuardError, "flash_may_have_mutated"):
            guard.issue_restore_confirmation(self.store.directory)
        self.mark_transition()
        conf = guard.issue_restore_confirmation(self.store.directory)
        self.assertEqual("restore", conf.kind)
        self.assertTrue(re.match(r"^r-[0-9a-f]{8}$", conf.id))
        m = self.m()
        self.assertEqual(guard.restore_binding(m),
                         m["confirmations"]["pending"]["restore"]["binding"])
        self.assertNotEqual(guard.flash_binding(m), guard.restore_binding(m))
        expires = guard._parse_iso(conf.expires_at)
        issued = guard._parse_iso(conf.issued_at)
        self.assertLessEqual((expires - issued).total_seconds(), guard.CONFIRMATION_TTL_S)

    def test_restore_check_reports_eligibility_and_drift(self):
        empty = self.root / "empty2"
        guard.ManifestStore(empty).ensure()
        with self.assertRaisesRegex(guard.GuardError, "manifest"):
            guard.restore_check(empty)
        self.prepare_full()
        res = guard.restore_check(self.store.directory)
        self.assertFalse(res.eligible)
        self.assertTrue(any("flash_may_have_mutated" in r for r in res.reasons))
        self.mark_transition()
        res = guard.restore_check(self.store.directory)
        self.assertTrue(res.eligible, res.reasons)
        self.assertEqual(0, res.details["write_offset"])
        self.assertEqual(FLASH_SIZE, res.details["restore_length"])
        self.assertTrue(res.details["erase_all"])
        self.assertEqual((0, FLASH_SIZE), (res.details["verify_start"], res.details["verify_end"]))
        self.assertEqual(BACKUP_SHA256, res.details["backup_sha256"])
        with self.backup_path.open("ab") as fh:
            fh.write(b"\x09")
        res = guard.restore_check(self.store.directory)
        self.assertFalse(res.eligible)
        self.assertTrue(any("sha256" in r or "size" in r for r in res.reasons))
        os.remove(self.backup_path)
        res = guard.restore_check(self.store.directory)
        self.assertFalse(res.eligible)
        self.assertTrue(any("missing" in r for r in res.reasons))


class TestRestore(GuardFixture):

    def test_restore_refuses_pre_transition_state(self):
        self.prepare_full()
        fresh = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "flash_may_have_mutated"):
            guard.restore(self.port, "r-00000000", self.store.directory, fresh)
        self.assertEqual([], fresh.invocations)
        self.assert_no_write(fresh)

    def test_restore_refuses_flash_confirmation_kind(self):
        self.prepare_full()
        self.mark_transition()
        self.do_flash(fake=FakeEsptool())
        # capture the consumed flash id from the manifest
        m = self.m()
        flash_id = m["confirmations"]["consumed"][0]["id"]
        fresh = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "flash confirmation"):
            guard.restore(self.port, flash_id, self.store.directory, fresh)
        self.assertEqual(["flash-id"], self.verbs(fresh))
        self.assert_no_write(fresh)

    def test_restore_writes_backup_verifies_full_range_then_resets(self):
        self.prepare_full()
        self.mark_transition()
        restore_fake = FakeEsptool()
        result, conf = self.do_restore(fake=restore_fake)
        seq = self.verbs(restore_fake)
        self.assertEqual(["flash-id", "erase-flash", "write-flash", "read-flash", "flash-id"], seq)
        self.assertEqual({
            "offset": "0x0",
            "path": str(self.backup_path),
            "flash_mode": "dio",
            "flash_freq": "80m",
            "flash_size": "16MB",
        }, restore_fake.write_calls[0])
        address, size, verify_out = restore_fake.read_calls[0]
        self.assertEqual((0, FLASH_SIZE), (address, size))
        self.assertEqual(self.priv, Path(verify_out).parent)
        for argv in restore_fake.invocations[1:4]:
            self.assertIn("--after", argv)
            self.assertEqual("no-reset", argv[argv.index("--after") + 1])
        reset_argv = restore_fake.invocations[-1]
        self.assertEqual("hard-reset", reset_argv[reset_argv.index("--after") + 1])
        self.assertEqual(FLASH_SIZE, result.verified_bytes)
        m = self.m()
        self.assertEqual("success", m["state"]["restore_result"])
        self.assertTrue(m["state"]["restore_write_started_at"])
        self.assertEqual(FLASH_SIZE, m["state"]["restore_verified_bytes"])
        consumed = m["confirmations"]["consumed"]
        self.assertEqual((conf.id, "restore"), (consumed[0]["id"], consumed[0]["kind"]))
        self.assertNotIn("restore", m["confirmations"]["pending"])
        self.assertFalse(list(self.priv.glob("*verify*")))

    def test_restore_records_failure_on_verify_mismatch_without_reset(self):
        self.prepare_full()
        self.mark_transition()
        bad = FakeEsptool(read_override=b"\x0f" * FLASH_SIZE)
        with self.assertRaisesRegex(guard.GuardError, "verification"):
            self.do_restore(fake=bad)
        seq = self.verbs(bad)
        self.assertEqual("read-flash", seq[-1])
        state = self.m()["state"]
        self.assertEqual("failed", state["restore_result"])
        for argv in bad.invocations:
            self.assertNotIn("hard-reset", argv)

    def test_restore_aborts_on_backup_drift_before_consumption(self):
        self.prepare_full()
        self.mark_transition()
        conf = guard.issue_restore_confirmation(self.store.directory)
        with self.backup_path.open("ab") as fh:
            fh.write(b"\x07")
        bad = FakeEsptool()
        with self.assertRaisesRegex(guard.GuardError, "backup"):
            guard.restore(self.port, conf.id, self.store.directory, bad)
        self.assertIn("restore", self.m()["confirmations"]["pending"])
        self.assertEqual(["flash-id"], self.verbs(bad))


class TestDiagnosticsAndCli(GuardFixture):

    def test_failures_are_bounded_and_secret_free(self):
        self.assertLessEqual(len(guard._bounded("x" * 100000)), guard.MAX_MESSAGE_CHARS)
        self.bind_probe()
        bad = FakeEsptool(fail_verb="read-flash")
        with self.assertRaises(guard.GuardError) as ctx:
            self.do_backup(bad)
        message = str(ctx.exception)
        self.assertNotIn(FAKE_ESPTOOL_SECRET, message)
        self.assertNotIn("partial stdout", message)
        self.assertLessEqual(len(message), guard.MAX_MESSAGE_CHARS)
        # CLI surfaces the same bounded, secret-free diagnostics
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = guard.main(["backup", "--port", str(self.port), "--output",
                             str(self.backup_path), "--manifest-dir", str(self.store.directory)],
                            runner=bad)
        self.assertEqual(1, rc)
        combined = err.getvalue()
        self.assertNotIn(FAKE_ESPTOOL_SECRET, combined)
        self.assertIn("error", combined.lower())

    def test_cli_end_to_end_success_and_confirmation_consumption(self):
        cli_fake = FakeEsptool()
        base = ["--manifest-dir", str(self.store.directory)]

        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = guard.main(list(args) + base, runner=cli_fake)
            return rc, out.getvalue(), err.getvalue()

        rc, _, _ = run("probe", "--port", str(self.port))
        self.assertEqual(0, rc)
        rc, _, _ = run("backup", "--port", str(self.port), "--output", str(self.backup_path))
        self.assertEqual(0, rc)
        rc, _, _ = run("register-build", "--artifact", str(self.artifact),
                       "--provenance", str(self.prov))
        self.assertEqual(0, rc)
        rc, out, _ = run("issue-flash-confirmation")
        self.assertEqual(0, rc)
        match = re.search(r"id=(f-[0-9a-f]{8})", out)
        self.assertTrue(match, out)
        rc, _, _ = run("flash", "--port", str(self.port), "--confirmation", match.group(1))
        self.assertEqual(0, rc)
        rc, out, _ = run("restore-check")
        self.assertEqual(0, rc)
        rc, out, _ = run("issue-restore-confirmation")
        self.assertEqual(0, rc)
        restore_match = re.search(r"id=(r-[0-9a-f]{8})", out)
        self.assertTrue(restore_match, out)
        rc, _, _ = run("restore", "--port", str(self.port), "--confirmation",
                       restore_match.group(1))
        self.assertEqual(0, rc)
        m = self.m()
        self.assertTrue(m["state"]["flash_success"])
        self.assertEqual("success", m["state"]["restore_result"])
        # replaying any old confirmation must fail with a bounded message
        rc, _, err = run("flash", "--port", str(self.port), "--confirmation", match.group(1))
        self.assertEqual(1, rc)
        self.assertIn("error", err.lower())


class TestFlashResetState(GuardFixture):

    def test_reset_requires_explicit_flag(self):
        self.prepare_full()
        with self.assertRaisesRegex(guard.GuardError, "--confirm-reset"):
            guard.flash_reset_state(self.store.directory)

    def test_reset_refuses_unresolved_mutation(self):
        self.prepare_full()
        self.mark_transition()
        m = self.m()
        m["state"]["flash_success"] = None
        self.save_manifest(m)
        with self.assertRaisesRegex(guard.GuardError, "mutated"):
            guard.flash_reset_state(self.store.directory, confirm_reset=True)

    def test_reset_keeps_device_and_backup_clears_state_and_build(self):
        self.prepare_full()
        result, conf = self.do_flash()
        self.assertTrue(result.reset_performed)
        before = self.m()
        self.assertTrue(before["state"]["flash_success"])
        self.assertIn("build", before)
        manifest = guard.flash_reset_state(
            self.store.directory, confirm_reset=True)
        self.assertEqual(before["device"], manifest["device"])
        self.assertEqual(before["backup"], manifest["backup"])
        self.assertIsNone(manifest["build"])
        for value in manifest["state"].values():
            self.assertNotEqual(value, True)
            self.assertNotEqual(value, "success")
        self.assertEqual([], manifest["confirmations"]["consumed"])
        # a fresh cycle works after the reset
        self.register()
        conf = guard.issue_flash_confirmation(self.store.directory)
        self.assertEqual("flash", conf.kind)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()