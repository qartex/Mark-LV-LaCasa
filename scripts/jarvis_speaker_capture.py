"""Capture one voice sample from the ESP32 satellite and enroll it.

The board only streams microphone audio while a voice-assistant turn is
running, so the sample is recorded from a real wake word: say "Hey Jarvis",
speak normally for a few seconds, and the script stores exactly that audio.

Usage (stop the bridge first, it shares the single device session):
    systemctl --user stop esp32-jarvis-bridge
    venv/bin/python -m scripts.jarvis_speaker_capture --speaker lukas
    systemctl --user start esp32-jarvis-bridge

Add --enroll to write the voice vector into the owner-only profile file.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
import wave
from array import array
from pathlib import Path

DEFAULT_SECONDS = 4.0
DEFAULT_VOICE_LEVEL = 500


def write_wav(path: Path, pcm: bytes, sample_rate: int = 16000) -> None:
    """Write 16-bit mono PCM as a WAV file (owner-only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    path.chmod(0o600)


def level(pcm: bytes) -> int:
    usable = len(pcm) - (len(pcm) % 2)
    if not usable:
        return 0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    total = sum(value * value for value in samples)
    return int(math.sqrt(total / len(samples)))


class SampleCollector:
    """Collect audio from the first voiced chunk up to a fixed duration."""

    def __init__(self, *, seconds: float, sample_rate: int = 16000,
                 voice_level: int = DEFAULT_VOICE_LEVEL) -> None:
        self._limit = int(seconds * sample_rate * 2)
        self._voice_level = int(voice_level)
        self._started = False
        self._chunks: list[bytes] = []
        self.finished = asyncio.Event()

    @property
    def collected_bytes(self) -> int:
        return sum(len(chunk) for chunk in self._chunks)

    def feed(self, data: bytes) -> None:
        if self.finished.is_set() or not data:
            return
        if not self._started:
            if level(data) < self._voice_level:
                return
            self._started = True
        self._chunks.append(bytes(data))
        if self.collected_bytes >= self._limit:
            self.finished.set()

    def pcm(self) -> bytes:
        joined = b"".join(self._chunks)
        return joined[: self._limit]


async def capture(client, collector: SampleCollector, timeout: float) -> bool:
    """Serve one ESPHome session until the sample is complete."""

    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        return 0

    async def handle_stop(aborted):
        return None

    async def handle_audio(data, data2):
        collector.feed(data)
        if data2:
            collector.feed(data2)

    await client.connect_and_subscribe(handle_start, handle_stop, handle_audio)
    try:
        await asyncio.wait_for(collector.finished.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    finally:
        await client.close()
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture a JARVIS voice sample")
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--relay-host", default="growraum-sprache-1-home-test.local")
    parser.add_argument("--relay-port", type=int, default=6053)
    parser.add_argument("--key-file", default="/home/lacasa/waveshare-va-test/api-key.txt")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--voice-level", type=int, default=DEFAULT_VOICE_LEVEL)
    parser.add_argument("--out", default="")
    parser.add_argument("--enroll", action="store_true")
    parser.add_argument(
        "--profiles", default="/home/lacasa/waveshare-va-test/speaker-profiles.json"
    )
    parser.add_argument(
        "--model",
        default="/home/lacasa/waveshare-va-test/models/3dspeaker_eres2net.onnx",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from esp32_jarvis_bridge import EspHomeVoiceClient, load_api_key

    api_key = load_api_key(Path(args.key_file))
    out = Path(args.out or f"/home/lacasa/waveshare-va-test/voices/{args.speaker}.wav")
    collector = SampleCollector(seconds=args.seconds, voice_level=args.voice_level)
    client = EspHomeVoiceClient(args.relay_host, args.relay_port, api_key)
    print(f"Say 'Hey Jarvis', then speak for about {args.seconds:.0f} seconds.")
    completed = asyncio.run(capture(client, collector, args.timeout))
    if not completed or not collector.collected_bytes:
        print("capture failed: no speech arrived", file=sys.stderr)
        return 1
    write_wav(out, collector.pcm())
    print(f"sample written bytes={collector.collected_bytes} path={out}")
    if not args.enroll:
        return 0

    from core.speaker_id import SpeakerIdentifier, SpeakerProfiles

    profiles = SpeakerProfiles(args.profiles)
    identifier = SpeakerIdentifier(profiles=profiles, model_path=args.model)
    try:
        key = identifier.enroll(args.speaker, collector.pcm())
    except ValueError as exc:
        print(f"enrollment refused: {exc}", file=sys.stderr)
        return 1
    print(f"enrolled speaker={key} profiles={len(profiles.vectors)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
