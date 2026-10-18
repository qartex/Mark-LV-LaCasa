import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "esp32-satellite"
THIN_CONFIG = DEPLOY / "waveshare-phase1.yaml"
CORE = DEPLOY / "vendor" / "core.yaml"
STARTUP_SOUND = DEPLOY / "vendor" / "startup.mp3"
PROVENANCE = DEPLOY / "vendor" / "provenance.json"
SECRETS_EXAMPLE = DEPLOY / "secrets.example.yaml"
REQUIREMENTS = DEPLOY / "requirements-macos.txt"
README = DEPLOY / "README.md"

UPSTREAM_COMMIT = "351067f95dffd072b53ce7f00b0a99ce3077ed06"
VOICE_ASSET_COMMIT = "d4e6fa43d6a1a7d342b7d4d403567e06516719de"
WAKEWORD_MODEL_COMMIT = "40ff33f57f8fc6ad71a75ef085abbf742495b225"
UPSTREAM_CORE_SHA256 = "f76a3d4c6ed08dd41107dc5376396bdd01a54762e8d1cd794cff33d49f39e565"
UPSTREAM_STARTUP_SHA256 = "030e034ffed015d78985bea99a9f827e1dc9d0c7d6fbb1a3e90cb4012184552d"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Esp32SatelliteConfigTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        required = (
            THIN_CONFIG,
            CORE,
            STARTUP_SOUND,
            PROVENANCE,
            SECRETS_EXAMPLE,
            REQUIREMENTS,
            README,
        )
        missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
        self.assertEqual([], missing, f"missing deployment files: {', '.join(missing)}")
        self.thin = THIN_CONFIG.read_text(encoding="utf-8")
        self.core = CORE.read_text(encoding="utf-8")

    def test_exact_temporary_identity_and_local_vendor_include(self):
        self.assertRegex(self.thin, r"(?m)^  name: growraum-sprache-1-home-test$")
        self.assertRegex(self.thin, r'(?m)^  friendly_name: "Growraum Sprache 1 Home Test"$')
        self.assertRegex(
            self.thin,
            r"(?ms)^packages:\n  core: !include vendor/core\.yaml\s*$",
        )
        self.assertNotRegex(self.thin, r"(?m)^\s*(?:url|ref|refresh):")
        self.assertNotRegex(self.thin, r"(?m)^\s*name: growraum-sprache-1\s*$")
        self.assertNotRegex(self.thin, r'(?m)^\s*friendly_name: ["\']?Growraum Sprache 1["\']?\s*$')

    def test_exact_tool_and_esphome_version_pins(self):
        requirements = [
            line.strip()
            for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(["esphome==2026.9.0", "esptool==5.3.1"], requirements)
        self.assertRegex(self.core, r"(?m)^  min_version: 2026\.9\.0(?:\s|$)")

    def test_provenance_commits_and_hashes_are_exact_and_self_verifying(self):
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "upstream_core": {
                    "repository": "https://github.com/MichalZaniewicz/esphome-waveshare-esp32-s3-audio-va",
                    "commit": UPSTREAM_COMMIT,
                    "path": "base/core.yaml",
                    "sha256": UPSTREAM_CORE_SHA256,
                },
                "voice_assets": {
                    "repository": "https://github.com/esphome/home-assistant-voice-pe",
                    "commit": VOICE_ASSET_COMMIT,
                },
                "wakeword_models": {
                    "repository": "https://github.com/esphome/micro-wake-word-models",
                    "commit": WAKEWORD_MODEL_COMMIT,
                },
                "vendored_phase1_core": {
                    "path": "core.yaml",
                    "sha256": sha256(CORE),
                },
                "vendored_startup_sound": {
                    "path": "startup.mp3",
                    "sha256": UPSTREAM_STARTUP_SHA256,
                },
            },
            provenance,
        )
        self.assertEqual(UPSTREAM_STARTUP_SHA256, sha256(STARTUP_SOUND))

    def test_api_encryption_and_secret_schema_are_mandatory(self):
        self.assertIn(
            "api:\n  id: api_id\n  encryption:\n    key: !secret api_encryption_key\n",
            self.core,
        )
        entries = {}
        for line in SECRETS_EXAMPLE.read_text(encoding="utf-8").splitlines():
            match = re.match(r'^([a-z_]+):\s*"([^"]+)"$', line)
            if match:
                entries[match.group(1)] = match.group(2)
        self.assertEqual(
            {
                "wifi_ssid": "REPLACE_WITH_WIFI_SSID",
                "wifi_password": "REPLACE_WITH_WIFI_PASSWORD",
                "wifi_bssid": "REPLACE_WITH_HOME_AP_BSSID",
                "api_encryption_key": "REPLACE_WITH_32_BYTE_BASE64_KEY",
            },
            entries,
        )
        self.assertRegex(self.core, r"(?m)^    - ssid: !secret wifi_ssid$")
        self.assertRegex(self.core, r"(?m)^      password: !secret wifi_password$")
        self.assertRegex(self.core, r"(?m)^      bssid: !secret wifi_bssid$")

    def test_phase2_mic_is_unmuted_and_wake_word_runs_locally(self):
        """Phase-2 decision (user-approved): mic listens; detection stays local."""
        forbidden_actions = ("voice_assistant.start", "microphone.stop_capture")
        for action in forbidden_actions:
            self.assertNotIn(action, self.core, f"forbidden action remains: {action}")

        self.assertNotRegex(self.core, r"(?m)^\s*- action: start_va\s*$")
        boot_section = self.core.split("  on_boot:\n", 1)[1].split("\nesp32:\n", 1)[0]
        captures = list(re.finditer(
            r"(?m)^\s*- microphone\.capture: i2s_mics\s*$", boot_section
        ))
        self.assertEqual(1, len(captures), "clock/master capture must have one boot path")
        self.assertNotIn(
            "- microphone.mute: i2s_mics",
            boot_section,
            "phase 2 boots with the microphone listening",
        )
        self.assertIn(
            "  - id: mic_is_muted\n"
            "    type: bool\n"
            "    restore_value: no\n"
            "    initial_value: 'false'   # Phase 2: the microphone listens by default\n",
            self.core,
        )
        self.assertIn("- micro_wake_word.start:", boot_section)

    def test_phase2_mute_switch_can_unmute_again(self):
        switch_block = re.search(
            r"(?ms)^  - platform: template\n    id: mic_mute_switch\n(.*?)(?=^  - platform:)",
            self.core,
        )
        if switch_block is None:
            self.fail("missing mic_mute_switch")
        block = switch_block.group(1)
        self.assertIn("turn_on_action:", block)
        self.assertIn("- microphone.mute: i2s_mics", block)
        self.assertIn("turn_off_action:", block)
        self.assertIn("- microphone.unmute: i2s_mics", block)
        self.assertIn("id(mic_is_muted) = false", block)

    def test_phase2_wake_word_model_and_local_feedback_are_pinned(self):
        hey_jarvis = (
            "https://github.com/esphome/micro-wake-word-models/raw/"
            "40ff33f57f8fc6ad71a75ef085abbf742495b225/models/v2/hey_jarvis.json"
        )
        self.assertIn(f"- model: {hey_jarvis}", self.core)
        # Standalone mww activates only the first model; hey_jarvis must be the
        # only model so it is the one that actually listens.
        self.assertNotIn("models/v2/alexa.json", self.core)
        self.assertNotIn("models/v2/okay_nabu.json", self.core)
        self.assertNotIn("models/v2/hey_mycroft.json", self.core)
        self.assertNotIn("id(alexa)", self.core)
        detection = self.core.split("  on_wake_word_detected:\n", 1)[1].split(
            "\nvoice_assistant:\n", 1
        )[0]
        self.assertIn("wake_word_triggered_sound", detection)
        self.assertIn("switch.is_off: night_mode", detection)
        self.assertNotIn("switch.is_on: wake_sound", detection)

    def test_voice_assistant_pipeline_stays_manual(self):
        self.assertRegex(self.core, r"(?m)^  use_wake_word: false$")

    def test_boot_and_wake_sounds_are_disabled(self):
        for switch_id in ("wake_sound", "boot_sound"):
            block = re.search(
                rf"(?ms)^  - platform: template\n    id: {switch_id}\n(.*?)(?=^  - platform:|^[a-z_]+:|\Z)",
                self.core,
            )
            if block is None:
                self.fail(f"missing {switch_id} switch")
            self.assertIn("internal: true", block.group(1))
            self.assertIn("restore_mode: ALWAYS_OFF", block.group(1))

    def test_ota_is_absent_and_external_assets_are_commit_pinned(self):
        combined = self.thin + "\n" + self.core
        self.assertNotRegex(combined, r"(?m)^ota:")
        self.assertNotRegex(combined, r"/(?:raw/)?(?:main|dev)/")
        self.assertNotRegex(combined, r"(?m)^\s*ref:\s*(?:main|dev)\s*$")

        asset_urls = []
        for line in self.core.splitlines():
            stripped = line.strip()
            if stripped.startswith(("file: https://", "model: https://", "- model: https://")):
                asset_urls.append(stripped.split(": ", 1)[1])
        self.assertGreaterEqual(len(asset_urls), 5)
        for url in asset_urls:
            if "home-assistant-voice-pe" in url:
                self.assertIn(VOICE_ASSET_COMMIT, url)
            elif "micro-wake-word-models" in url:
                self.assertIn(WAKEWORD_MODEL_COMMIT, url)
            else:
                self.fail(f"unapproved external asset URL: {url}")
        self.assertRegex(self.core, r"(?m)^  boot_sound_file: vendor/startup\.mp3$")

    def test_shared_i2s_and_media_player_contract_is_preserved(self):
        for line in (
            "  i2s_mclk: GPIO12",
            "  i2s_bclk: GPIO13",
            "  i2s_lrclk: GPIO14",
            "  i2s_din: GPIO15",
            "  i2s_dout: GPIO16",
            "    bits_per_sample: 16bit",
            "    i2s_mode: secondary",
            "    id: external_media_player",
            "      sample_rate: 48000",
        ):
            self.assertIn(line, self.core)

    def test_phase1_media_pipeline_matches_fixed_wav_fixture(self):
        media_pipeline = self.core.split("    media_pipeline:\n", 1)[1].split(
            "    on_announcement:\n", 1
        )[0]
        self.assertIn("      format: WAV\n", media_pipeline)
        self.assertNotIn("      format: FLAC\n", media_pipeline)

    def test_plaintext_wifi_and_production_identity_are_absent(self):
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (THIN_CONFIG, CORE, SECRETS_EXAMPLE, README)
        )
        self.assertNotIn("Growraum Sprache 1\n", combined)
        network_values = []
        for line in combined.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("ssid:", "password:", "- ssid:")):
                continue
            network_values.append(re.sub(r"^-\s+", "", stripped))
        self.assertEqual(
            ["ssid: !secret wifi_ssid", "password: !secret wifi_password"],
            network_values,
        )

    def test_local_secret_and_outputs_are_narrowly_ignored(self):
        gitignore_lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/deploy/esp32-satellite/secrets.yaml", gitignore_lines)
        self.assertIn("/deploy/esp32-satellite/local-output/", gitignore_lines)

        ignored = (
            "deploy/esp32-satellite/secrets.yaml",
            "deploy/esp32-satellite/local-output/backup/full-flash.bin",
            "deploy/esp32-satellite/local-output/build/firmware.factory.bin",
            "deploy/esp32-satellite/local-output/safety-manifest.json",
        )
        visible = (
            "deploy/esp32-satellite/backup.bin",
            "deploy/esp32-satellite/safety-manifest.json",
            "deploy/other/local-output/safety-manifest.json",
        )
        for relative in ignored:
            result = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", relative],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(0, result.returncode, f"not ignored: {relative}")
        for relative in visible:
            result = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", relative],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(1, result.returncode, f"over-broad ignore matched: {relative}")


if __name__ == "__main__":
    unittest.main()
