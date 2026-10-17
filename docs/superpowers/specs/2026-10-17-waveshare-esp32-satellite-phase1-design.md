# Waveshare ESP32-S3 Satellite Phase 1 Design

**Status:** approved direction: staged integration; speaker output first, microphones and wake word later.

## Intent

Use the Waveshare ESP32-S3-AUDIO-Board currently connected by USB-C to the user's MacBook as a remote test speaker for LaCasa JARVIS without exposing Home Assistant, JARVIS, or ESPHome publicly. Preserve the board's current LaCasa installation so it can be restored byte-for-byte.

## Confirmed starting state

- USB device: Espressif USB JTAG/serial debug unit, serial port `/dev/cu.usbmodem1201`, 16 MB flash class.
- Existing firmware: ESPHome `2026.9.0`, device `Growraum Sprache 1`, board `esp32-s3-devkitc-1`.
- Existing HA entities include `media_player.growraum_sprache_1` and `assist_satellite.growraum_sprache_1_assist_satellit`.
- Firmware currently knows only the old LaCasa Wi-Fi and exposes no fallback access point.
- The matching public configuration family is `MichalZaniewicz/esphome-waveshare-esp32-s3-audio-va`; pin source commit `351067f95dffd072b53ce7f00b0a99ce3077ed06` rather than tracking `main`.

## Phase-1 architecture

1. On the Mac, read and preserve the complete 16 MB flash before any write. Store the binary and SHA-256 in an owner-only local directory; never transfer or commit it because it contains Wi-Fi material.
2. Build a temporary ESPHome `2026.9.0` image from a reviewed vendor snapshot derived from source commit `351067f95dffd072b53ce7f00b0a99ce3077ed06`. All upstream `main`/`dev` asset URLs are replaced with commit-pinned URLs. Home Wi-Fi values and a generated ESPHome API encryption key live only in a local mode-0600 `secrets.yaml` entered by the user on the Mac.
3. Use the full audio core because the microphone I²S controller must keep producing the speaker clock, but force the microphone to return zero-filled buffers, never start `micro_wake_word`, and never start or transmit through `voice_assistant`. Disable OTA for Phase 1.
4. Use a temporary device name (`growraum-sprache-1-home-test`) but do not add it to Home Assistant: ESPHome config entries are keyed by the board MAC, so a second flow could mutate the existing production entry despite a different name. A narrow direct ESPHome API acceptance client on `lacasa-server` controls the temporary media player instead.
5. Keep the direct API/JARVIS path private. A small TCP relay binds only to the Mac's current Tailscale IPv4 address, discovered and validated by the relay itself, and forwards the encrypted ESPHome native API to the board's verified home-LAN address. No `0.0.0.0`, Funnel, public DNS, router port forwarding, or production HA registry/config-entry change.
6. Serve a short, low-volume, nonsecret test WAV from the Mac's home-LAN address. Trigger playback through the temporary ESPHome media-player path, cold-boot once, repeat playback, and obtain the user's audible confirmation.
7. If a defined post-write acceptance stage fails, stop and offer a separately confirmed restore of the verified full-flash backup over USB; pre-write failures never trigger a restore write. The normal success path proves all restore guards but does not overwrite the accepted temporary image merely to exercise rollback. Do not proceed to microphone or wake-word integration until playback is audible and stable.

## Security and safety constraints

- Never request or print Wi-Fi passwords, API keys, flash contents, or HA tokens in chat or logs.
- The flash backup, safety manifest, `secrets.yaml`, build output, serial logs, and generated firmware stay outside Git with modes `0700`/`0600` as appropriate. The manifest binds device fingerprint, flash size, backup SHA-256, exact factory-artifact path/type/hash/length, write offset `0x0`, erase policy, flash mode/frequency/size, verification range, source provenance and confirmation expiry across separate invocations.
- Verify the detected chip, flash size, source commit, ESPHome version, firmware hash, and serial port before flashing.
- A flash operation requires a separate explicit user confirmation after backup and compiled factory-image hashes have been verified. The write helper re-probes the USB identity and refuses a changed/stale confirmation or changed artifact/write map.
- Immediately before invoking erase/write, the guard atomically persists `flash_write_started=true` / `flash_may_have_mutated=true`; this transition survives interruption. A restore requires that state plus a new, separately issued, time-bounded confirmation bound to device, backup hash and the full 16 MB restore map at offset `0x0`; the flash confirmation can never authorize restoration. Failures before that transition cannot restore, while interruption, partial write, verification failure and later post-write failures can enter the separately confirmed path. Restored bytes are verified before reset.
- The relay binds only to the Mac's current Tailscale IP and refuses wildcard or non-Tailscale bind addresses. The audio server is the sole LAN listener and derives its bind address from the OS route to the manifest-validated board; it rejects caller-selected or unrelated private interfaces.
- Boot ordering is a privacy invariant: mute first, then start clock-only capture. Every unmute, wakeword-start and voice-assistant-start path remains absent.
- Phase 1 changes no JARVIS audio-routing code and does not claim microphone, wake-word, ducking, or conversational latency acceptance.

## Acceptance

- Full 16 MB pre-change backup exists without overwrite, is mode 0600, and has a recorded SHA-256 in a mode-0600 manifest.
- Temporary firmware connects to the home Wi-Fi without revealing credentials.
- The direct acceptance client on `lacasa-server` can reach the ESPHome API only through the private Mac/Tailscale relay; Home Assistant is not changed.
- API access without the generated encryption key fails; OTA is absent; serial/runtime evidence shows wakeword stopped and microphone permanently muted while its I²S clock remains active.
- Temporary media-player metadata becomes available to the narrow direct client, while the production HA config entry and entity registry remain byte-for-byte unchanged.
- A test tone is audibly reproduced by the Waveshare speaker and confirmed by the user.
- Restore guards validate the original image, full 16 MB map and device binding; an actual restore is performed only after a post-write failure or later explicit request and a fresh restore-specific confirmation.
