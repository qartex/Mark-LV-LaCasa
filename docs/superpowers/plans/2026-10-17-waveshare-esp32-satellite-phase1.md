# Waveshare ESP32-S3 Satellite Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely back up the existing Waveshare ESP32-S3 firmware, install a temporary privacy-locked home-network image, privately bridge it to LaCasa, and verify repeatable audible speaker playback.

**Architecture:** A reviewed vendor snapshot supplies the full audio core because the microphone I²S controller must keep generating the speaker clock. The Phase-1 patch mutes before clock-only capture, permanently zero-fills microphone buffers, never starts wakeword/voice-assistant consumers, encrypts the ESPHome API, and disables OTA. Tested helpers bind backups, exact factory-image write maps and confirmations to one USB device through a persistent owner-only manifest. A narrow direct ESPHome API client on `lacasa-server` reaches the board only through a Mac relay bound to its current Tailscale IPv4; Home Assistant is not changed because its ESPHome config identity is the board MAC.

**Tech Stack:** Python 3, `unittest`, ESPHome `2026.9.0`, esptool `5.3.1`, ESPHome native API, macOS, Tailscale.

**Spec:** `docs/superpowers/specs/2026-10-17-waveshare-esp32-satellite-phase1-design.md`

## Global Constraints

- Phase 1 tests speaker output only; no JARVIS audio-routing, microphone-input, wake-word, or ducking feature work.
- Vendor provenance: `MichalZaniewicz/esphome-waveshare-esp32-s3-audio-va` commit `351067f95dffd072b53ce7f00b0a99ce3077ed06`; pin voice assets to `esphome/home-assistant-voice-pe` commit `d4e6fa43d6a1a7d342b7d4d403567e06516719de` and wakeword model assets to `esphome/micro-wake-word-models` commit `40ff33f57f8fc6ad71a75ef085abbf742495b225`.
- Pin Mac tools to ESPHome `2026.9.0` and esptool `5.3.1`.
- Temporary device name is exactly `growraum-sprache-1-home-test` / `Growraum Sprache 1 Home Test`, but it is never added to HA; snapshot the relevant production HA registry/config-entry files before and after acceptance and require byte-for-byte equality.
- The mic I²S capture remains clock-active but returns only zero-filled buffers; `micro_wake_word` and `voice_assistant` never start. OTA is absent. ESPHome API encryption is mandatory.
- Wi-Fi secrets, full-flash backup, safety manifest, builds, serial logs and generated firmware remain Mac-local, owner-only and outside Git/output. The API key may additionally exist only in a mode-0600 ephemeral acceptance file on `lacasa-server`, deleted after the test.
- No public listeners, Funnel, router forwarding, wildcard binds, or plaintext secrets in argv/stdout. The only LAN listener is the fixed-file audio server on the Mac address derived from the OS route to the validated board.
- No flash write until a complete backup and compiled image have verified hashes and the user gives a second, time-bounded explicit confirmation.
- Actual restore occurs only on failure or a later explicit request; normal success proves every restore guard without rewriting flash.

## Review Focus

1. USB port/device substitution between probe, backup and flash must abort before writing.
2. Short/partial backups, changed firmware hashes, stale confirmations, missing manifests and unsafe/symlinked paths must fail closed.
3. Relay bind drift to wildcard, LAN, public or stale Tailscale addresses must fail; audio-server binding must equal the OS route-source address to the validated board and reject other private interfaces.
4. Boot must mute before capture; no path may unmute or transmit microphone audio or start wakeword/voice assistant after boot, API connect, button, or media state changes.
5. Secrets, flash bytes and relay payloads must never appear in exceptions, logs, subprocess output or Git.

---

### Task 1: Reproducible privacy-locked Phase-1 firmware source

**Files:**
- Create: `deploy/esp32-satellite/waveshare-phase1.yaml`
- Create: `deploy/esp32-satellite/vendor/core.yaml`
- Create: `deploy/esp32-satellite/vendor/startup.mp3`
- Create: `deploy/esp32-satellite/vendor/provenance.json`
- Create: `deploy/esp32-satellite/secrets.example.yaml`
- Create: `deploy/esp32-satellite/requirements-macos.txt`
- Create: `deploy/esp32-satellite/README.md`
- Create: `tests/test_esp32_satellite_config.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces a thin config named `growraum-sprache-1-home-test` that includes only the reviewed local vendor core.
- `provenance.json` contains the exact three source commits and SHA-256 values for the unmodified upstream core and vendored Phase-1 core.
- Local secrets schema is exactly `wifi_ssid`, `wifi_password`, `api_encryption_key`; real values never exist in Git.

- [x] **Step 1: Write failing static contract tests**
  - Assert exact identity/version/tool pins and provenance hashes; no `raw/main`, `raw/dev`, branch ref, OTA, plaintext Wi-Fi value or production identity.
  - Assert the API encryption key is required.
  - Assert every `voice_assistant.start`, `micro_wake_word.start` and `microphone.unmute` path is absent/disabled and that every boot path executes `microphone.mute` before `microphone.capture`; mere presence of both actions is insufficient.
  - Assert secrets/backups/builds/manifests are ignored only under the feature's local-output paths.

- [x] **Step 2: Run tests RED**
  - Run: `python -m unittest tests.test_esp32_satellite_config -v`
  - Expected: missing deployment files.

- [x] **Step 3: Vendor and patch the pinned core**
  - Keep I²S hardware/pins/media player unchanged; set the muted state and execute `microphone.mute` before starting clock-only `microphone.capture`, remove all start/unmute actions, disable boot/wake sounds, require API encryption, remove OTA, and pin every external asset URL.

- [x] **Step 4: Run `esphome config` with synthetic secrets**
  - Use Mac-tool pins in an isolated throwaway venv; output must show temporary identity, encrypted API, no OTA and no configuration error.

- [x] **Step 5: Run tests GREEN and commit**
  - Commit: `feat(esp32): add reproducible privacy-locked speaker firmware`

- Verified 2026-10-17: RED run showed 11 failures without the deployment files; GREEN run 11/11 OK; `esphome 2026.9.0 config` on a throwaway scratch copy with synthetic secrets returned `Configuration is valid!` with the exact temporary identity, encrypted API and zero `ota:` entries; full repository suite `409 tests OK (skipped=1)`.

---

### Task 2: Persistent safety manifest and guarded backup/build/flash/restore CLI

**Files:**
- Create: `scripts/esp32_flash_guard.py`
- Create: `tests/test_esp32_flash_guard.py`

**Interfaces:**
- CLI commands: `probe`, `backup`, `register-build`, `issue-flash-confirmation`, `flash`, `restore-check`, `issue-restore-confirmation`, `restore`.
- `probe(port: Path, runner: Runner) -> ProbeResult` records ESP32-S3 chip, stable USB/MAC fingerprint, flash size and serial path.
- Owner-only `SafetyManifest` persists device fingerprint, flash size, backup path/SHA, exact artifact type/path/SHA/length, source provenance, write offset, erase policy, flash mode/frequency/size, verification range, creation time and confirmation expiry; atomic mode 0600 write in a mode-0700 real directory.
- `backup(...)` uses no-reset after read, creates a new non-overwriting 16,777,216-byte image, fsyncs it and records SHA-256.
- `register-build(...)` accepts only ESPHome's exact `firmware.factory.bin`, verifies `esptool image-info` identifies ESP32-S3, and binds its SHA/length to `offset=0x0`, `erase_all=true`, `flash_size=16MB`, `flash_mode=dio`, `flash_freq=80m`, and verification range `[0, artifact_length)`; it never flashes.
- `issue-flash-confirmation(...)` returns a short nonsecret one-use ID bound to device + backup SHA + exact factory artifact/write map with ≤10-minute lifetime.
- `flash(...)` re-probes identity, re-hashes both images and consumes only a flash confirmation. Immediately before invoking erase/write it atomically persists `flash_write_started=true` and `flash_may_have_mutated=true`; after that durable transition it performs the exact bound write map with `--after no-reset`, reads/verifies the bound range before first firmware boot, marks success, then resets.
- `issue-restore-confirmation(...)` is unavailable unless the durable `flash_may_have_mutated` transition exists, including interrupted/partial writes that never reached success. It returns a separate one-use ID bound to device + backup SHA + `offset=0x0`, `length=16,777,216`, `erase_all=true`, and full-range verification with ≤10-minute lifetime.
- `restore(...)` refuses a flash confirmation, consumes only the restore-specific confirmation, writes only the manifest-bound 16 MB raw image at `0x0`, verifies all 16 MB before reset, and records the restore result.

- [ ] **Step 1: Write failing tests**
  - Cover safe probe/backup; short read; existing output; symlink/wrong-mode ancestor; atomic manifest; wrong chip/flash size; USB substitution; rejection of non-factory artifacts; changed offset/length/erase/mode/frequency/size; firmware hash drift; stale/replayed/wrong-kind confirmations; pre-transition restore-confirmation rejection; durable write-start transition ordering; interrupted/partial write restore eligibility; missing manifest; secret-free failures; and no-reset full-range verify ordering.

- [ ] **Step 2: Run tests RED**
  - Run: `python -m unittest tests.test_esp32_flash_guard -v`

- [ ] **Step 3: Implement with argv-list subprocess calls and bounded sanitized diagnostics**
  - Never shell-expand dynamic values or emit raw esptool output.

- [ ] **Step 4: Run tests GREEN and commit**
  - Commit: `feat(esp32): guard backup build flash and rollback with manifest`

---

### Task 3: Self-validating private relay and fixed-file test server

**Files:**
- Create: `scripts/esp32_private_relay.py`
- Create: `scripts/esp32_test_audio_server.py`
- Create: `scripts/esp32_api_acceptance.py`
- Create: `tests/test_esp32_private_transport.py`

**Interfaces:**
- `current_tailscale_ipv4() -> IPv4Address` queries local Tailscale state directly; callers cannot supply the trusted-address list.
- `validate_board_target(ip: str, expected_mac: str) -> IPv4Address` accepts only a private home-LAN IPv4 whose ARP/neighbor MAC matches the manifest fingerprint.
- `run_relay(target_ip: str, target_port: int = 6053, bind_port: int = 16053)` binds only the freshly discovered Mac Tailscale IPv4, uses bounded buffers/timeouts, logs no payload and leaves no listener after shutdown.
- `route_source_for_board(board_ip: IPv4Address) -> IPv4Address` derives the Mac LAN source address from the OS route to the manifest-validated board; callers cannot select another interface.
- `create_audio_server(board_ip: IPv4Address, wav_path: Path)` binds only the derived route-source address and serves only `/jarvis-speaker-test.wav`; every other path is 404.
- `preflight_from_lacasa_server(bind_port: int) -> bool` verifies `lacasa-server`→Mac Tailscale reachability against a harmless nonce-free health response before device flash.
- `esp32_api_acceptance.py` reads the encryption key only from a mode-0600 file, connects through the relay, discovers the temporary media-player key, sends the fixed WAV URL, and never logs key material or changes HA.

- [ ] **Step 1: Write failing transport tests**
  - Cover wildcard/public/stale IP rejection, caller-supplied trust-list bypass absence, MAC mismatch, `lacasa-server` preflight, bidirectional relay, disconnect cleanup, payload absence from logs, route-source selection, wrong-private-interface rejection, exact WAV path, traversal/other paths 404, API-key-file mode enforcement, no HA calls, and minimum safe test-wave amplitude.

- [ ] **Step 2: Run tests RED**
  - Run: `python -m unittest tests.test_esp32_private_transport -v`

- [ ] **Step 3: Implement relay/server, direct API client, and deterministic short low-volume WAV generation**

- [ ] **Step 4: Run tests GREEN and commit**
  - Commit: `feat(esp32): add self-validating private speaker transport`

---

### Task 4: Verified Mac deployment and acoustic acceptance

**Files:**
- Modify only if evidence requires a correction: `deploy/esp32-satellite/README.md`
- Create outside Git on Mac: `~/waveshare-va-test/` private workspace.

**Interfaces:**
- Mac access is key-only SSH as `qartex`, `BatchMode=yes`; first read-back must prove `whoami=qartex`, the expected Mac Tailscale node/IP and the expected USB fingerprint. Production HA storage is only hashed before/after, never modified.
- Repository snapshot reaches the Mac as a hash-verified archive without any operational secrets.
- New board LAN address is identified by matching its expected MAC, not by hostname guess.

- [ ] **Step 1: Run full local verification and secret scan**
  - Full `unittest`, `compileall`, `git diff --check`; prove no `secrets.yaml`, `.bin`, manifest, build directory, Wi-Fi/API value or generated firmware is tracked.

- [ ] **Step 2: Verify key-only Mac path and `lacasa-server`→Mac Tailscale preflight**
  - Prove `qartex`, expected Mac node, ESP USB fingerprint, a successful harmless listener probe from `lacasa-server`, and record pre-test hashes of the relevant HA config-entry/entity-registry files before touching flash.

- [ ] **Step 3: Install exact tools in Mac-local venv and probe**
  - Verify ESPHome `2026.9.0`, esptool `5.3.1`, ESP32-S3 identity and 16 MB flash.

- [ ] **Step 4: Create and verify full-flash backup + manifest**
  - Exact bytes, 0600 modes, SHA-256, no overwrite, no-reset read, device binding.

- [ ] **Step 5: User enters home Wi-Fi and generated API key locally**
  - Use hidden interactive Mac prompts writing mode-0600 `secrets.yaml`; never traverse SSH stdout/argv.

- [ ] **Step 6: Run `esphome config`, compile only, image-info and register-build**
  - Select only `firmware.factory.bin`; verify source provenance and bind its SHA/length to offset `0x0`, erase-all, `16MB/dio/80m`, and the exact verification range before requesting permission.

- [ ] **Step 7: Stop for explicit flash confirmation**
  - Show only safe device fingerprint suffix, backup SHA, firmware SHA and confirmation expiry.

- [ ] **Step 8: Flash once through the consumed guard and immediately verify the bound range before boot**
  - Abort on any changed fingerprint, artifact, write-map value, hash or confirmation.

- [ ] **Step 9: Discover board by MAC, start relay, and use the direct encrypted acceptance client**
  - Copy the API key to a mode-0600 ephemeral file on `lacasa-server`, verify API without key fails, discover temporary media-player metadata through the relay, and do not invoke HA config flow or APIs.

- [ ] **Step 10: Verify privacy lock**
  - Cold boot; serial/runtime evidence: mic capture clock active, microphone muted, no wakeword/VA start, no OTA. Repeat after direct API reconnect and button presses.

- [ ] **Step 11: Play the fixed test WAV twice through the direct client with a cold boot between runs**
  - Start at minimum configured volume. Record automated availability separately from the user's audible confirmation; delete the ephemeral API-key file after the test.

- [ ] **Step 12: Run `restore-check`, evaluate the explicit post-write failure branch, and re-hash HA state**
  - Restore-eligible failures begin only after the durable `flash_may_have_mutated` transition and include interrupted/partial erase or write, bound-range verification failure, bounded boot/API timeout with the correct key, any microphone/wakeword/VA privacy violation, or no audible output after both controlled attempts. On one of these, stop and request a new restore-specific confirmation bound to device + backup SHA + full 16 MB map; after approval restore at `0x0`, verify all 16 MB before reset, and record the result. Failures before the transition never authorize restore. On success or declined restore, perform no write. Require the production HA hashes to match their pre-test values.

- [ ] **Step 13: Commit any evidence-driven doc correction, run whole-branch review, and update private backup repo**
