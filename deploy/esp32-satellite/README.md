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
