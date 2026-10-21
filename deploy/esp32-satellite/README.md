# Waveshare ESP32-S3 Phase-1 firmware source

This directory contains the reproducible, speaker-only acceptance firmware source for the temporary `growraum-sprache-1-home-test` device identity.

## Privacy lock

The vendored core is derived from the exact upstream revision recorded in `vendor/provenance.json`. On every boot it calls `microphone.mute` before the sole `microphone.capture` action. Capture stays active only to generate the shared I2S clocks required by the speaker; ESPHome's mute primitive zero-fills every microphone buffer. No action can unmute the microphone or start micro-wake-word or voice-assistant processing. Wake and boot sounds are permanently disabled, OTA is absent, and native API encryption is required.

The I2S pin map, 16-bit microphone-master bus, secondary speaker bus, DAC/ADC, mixer, and media-player pipelines remain based on the pinned upstream core. External sound and wakeword assets use immutable commit URLs.

## Local validation

Use a throwaway virtual environment. Do not run, upload, or flash as part of source validation.

```sh
python3 -m venv /private/tmp/waveshare-phase1-venv
/private/tmp/waveshare-phase1-venv/bin/pip install -r requirements-macos.txt
cp secrets.example.yaml secrets.yaml
chmod 600 secrets.yaml
# Replace all three placeholders locally, then validate only:
/private/tmp/waveshare-phase1-venv/bin/esphome config waveshare-phase1.yaml
```

The local secret schema is exactly `wifi_ssid`, `wifi_password`, `wifi_bssid`, and `api_encryption_key`. Keep `secrets.yaml` beside the thin config and never commit it.

## Speaker acceptance

Phase 1 serves a fixed low-amplitude WAV fixture, so the `media_pipeline.format` is intentionally `WAV`. API command acceptance and an HTTP fetch are transport evidence only; the operator must separately confirm that the tone and spoken sentence are audible and intelligible.

The normal relay binds only to the bridge host's freshly discovered Tailscale address. If macOS accepts the Tailscale connection but then refuses its LAN connection with `EHOSTUNREACH`, use the narrower key-authenticated reverse tunnel instead:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:16053:<board-ip>:6053 <controller>
```

Verify the controller listener is bound only to `127.0.0.1`, run encrypted API discovery through that loopback endpoint, and tear the tunnel down after acceptance. Never omit the explicit remote loopback bind.

## Local-only outputs

Put backups, manifests, serial logs, build trees, and generated firmware under `local-output/`. Both that directory and `secrets.yaml` are ignored only at this deployment path. Keep private directories mode 0700 and secret or manifest files mode 0600.

`vendor/startup.mp3` is retained byte-for-byte for source reproducibility even though the Phase-1 boot-sound switch is forced off. Hashes and all three source commits are in `vendor/provenance.json` and are checked by `tests/test_esp32_satellite_config.py`.

## Pending flash: the wake word engine (2026-10-21)

Two firmware defects made the satellite deaf, and both are fixed in the
repository but **not yet on the board**:

1. `apply_wake_word_sensitivity` ignored the "Wake word threshold"
   slider in every position but "Custom" and replaced the model cutoff
   with the model's own default (0.97 -> 247). Dialling the threshold
   down to 0.6 changed the number and nothing else.
2. The wake word engine was started only once, during boot.
   `MicroWakeWord::start()` has no retry, so a start that lost the race
   against the component setup left the board deaf for the whole uptime.
   On the live board that happened after the restart of 2026-10-20
   08:29; 43 later reconnects, every one of them logged as "satellite
   connected", did not bring the wake word back.

The fixes: the threshold is the effective cutoff in every position, the
boot path runs at priority 50 (after `micro_wake_word.setup()`) and
repeats the start until the engine reports itself running, and a client
reconnect restarts a dead engine.

### What the board actually runs (measured 2026-10-21, after a restart)

| Fact | Value |
|---|---|
| Firmware on the board | compiled **2026-10-19 07:05:29** - before every fix in this section |
| ESPHome version | 2026.9.0 |
| Wake word threshold | 0.6 |
| Wake word sensitivity | **Custom** (so the slider applies even on the old firmware) |
| Microphone gain | 24.0 dB |
| Logger level | DEBUG |
| Wake word activity after a restart | **none** - no "Detected", no "Inference task" |

The board was restarted over the API (the "Restart" button entity) and the
wake word still did not start. The threshold is not the reason: the board
stands on "Custom", where the old firmware does apply the slider, and the
engine still stays silent. The firmware from 2026-10-19 is the reason, and
only a flash replaces it.

### The board is healthy

Measured over the encrypted API on 2026-10-21: board 192.168.1.192,
SSID Eifelgreen, signal -47 dBm, gateway 192.168.1.1, TCP 6053 open.
The guest network (10.217.222.205) is not reachable and has no route,
so the board is in the home network. Microphone gain 24 dB, the mute
switch is off, and the board's own serial log of 2026-10-19 shows the
engine starting and detecting "Hey Jarvis" at 0.99.

### The flash (on the Mac, with the board on USB)

The board's flash is only reachable over USB, so this runs on the Mac.
**As of 2026-10-21 17:28 the firmware is already built with every fix**
and waits on the Mac:

| | |
|---|---|
| Firmware | `~/waveshare-va-test/config/esp32-satellite/.esphome/build/growraum-sprache-1-home-test/build/firmware.factory.bin` |
| SHA-256 | `c62c377a92d12b37231af932a1cd9e4538eff83da1997d82cc22a9b019bce9db` |
| Previous build (without the reconnect fix) | `64173e3d5c908ce65a06d0948caf1b14647da1144f42522174153b38e0966c51` |
| `vendor/core.yaml` on the Mac | `52cddc1ea29a4225cd0778e97e723f146ce430855da7c37bf4e2050dd0bef23b` (same as the repository) |

The vendor files were copied from the repository before the build, so the
artifact contains the reconnect path that the previous build was missing.
Check the artifact before flashing - it must be `c62c377a…`:

```sh
shasum -a 256 ~/waveshare-va-test/config/esp32-satellite/.esphome/build/growraum-sprache-1-home-test/build/firmware.factory.bin
```

Both fixes are proven **in the compiled binary**, not only in the source
(`strings` on the factory image, 2026-10-21):

```
Wake word engine restarted after the client connected
Sensitivity '%s': %s cutoff %u%s
```

Then plug the board into the Mac and run:

```sh
sh ~/waveshare-va-test/flash-satellite.sh
```

The guarded flow (`scripts/esp32_flash_guard.py`) takes a full 16 MB
backup before every write, binds the write to a one-use confirmation
with a ten-minute lifetime, verifies the written range against the
registered artifact hash before the chip is reset, and refuses a
restore without a separate confirmation. The expected backup hash of
the board as it stands is
`ed881f10a6cd7afafa1e1520240056d00ea5731778f594b9e63b4f02382a5f56`.

### Verifying the flash

```sh
~/waveshare-va-test/venv/bin/python ~/waveshare-va-test/satellite_wakeword_monitor.py 60
```

The expected line is

```
Sensitivity 'Slightly sensitive': Hey Jarvis cutoff 153
```

153 is 0.6 - the operator's threshold. 247 is the old model default and
means the fix did not reach the board.

The monitor is read-only: it only opens a log and a state subscription.
It cannot see the boot lines at all (they are emitted before the Wi-Fi
is up), so "no wake-word-related log lines" is a false negative and not
evidence of a dead engine. What counts is the cutoff line and a
`Detected 'Hey Jarvis'` line while a person says the wake word in front
of the device.

### After the flash

A person has to say "Hey Jarvis" in front of the board for the final
proof. With the engine running, the speaker enrollment follows:
`systemctl --user start jarvis-speaker-capture@lukas` on
lacasa-server, then "Hey Jarvis" and about four seconds of speech.
