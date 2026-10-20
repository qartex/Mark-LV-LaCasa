"""Enroll one person's voice for JARVIS (owner-only profiles, no secrets).

Usage:
    python -m scripts.jarvis_speaker_enroll --speaker lukas --wav lukas.wav

The WAV must be 16-bit mono PCM (16 kHz preferred, one to five seconds of
normal speech). The vector is stored in the owner-only profile file that the
bridge reads; nothing about the audio itself is kept.
"""
from __future__ import annotations

import argparse
import sys
import wave
from array import array
from pathlib import Path


def _resample(samples: array, source_rate: int, target_rate: int) -> array:
    """Linear resampling, standalone so the CLI never imports the bridge."""
    if source_rate == target_rate or len(samples) <= 1:
        return samples
    count = max(1, (len(samples) * target_rate) // source_rate)
    out = array("h")
    for index in range(count):
        position = index * source_rate
        left = position // target_rate
        fraction = position % target_rate
        if left >= len(samples) - 1:
            value = samples[-1]
        else:
            value = (samples[left] * (target_rate - fraction)
                     + samples[left + 1] * fraction) // target_rate
        out.append(max(-32768, min(32767, value)))
    return out


def load_wav(path: Path, target_rate: int = 16000) -> bytes:
    """Read a PCM WAV and return 16-bit mono PCM at ``target_rate``."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("WAV must be 16-bit")
        channels = handle.getnchannels()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        mono = array("h")
        for index in range(0, len(samples) - channels + 1, channels):
            mono.append(sum(samples[index:index + channels]) // channels)
        samples = mono
    samples = _resample(samples, rate, target_rate)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Enroll a JARVIS speaker voice")
    parser.add_argument("--speaker", required=True, help="lukas, bjoern, alex")
    parser.add_argument("--wav", required=True, help="16-bit mono PCM recording")
    parser.add_argument(
        "--profiles",
        default="/home/lacasa/waveshare-va-test/speaker-profiles.json",
    )
    parser.add_argument(
        "--model",
        default="/home/lacasa/waveshare-va-test/models/3dspeaker_eres2net.onnx",
    )
    args = parser.parse_args(argv)

    from core.speaker_id import SpeakerIdentifier, SpeakerProfiles

    profiles = SpeakerProfiles(args.profiles)
    identifier = SpeakerIdentifier(profiles=profiles, model_path=args.model)
    try:
        pcm = load_wav(Path(args.wav))
    except (OSError, ValueError, wave.Error) as exc:
        print(f"recording refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        key = identifier.enroll(args.speaker, pcm)
    except ValueError as exc:
        print(f"enrollment refused: {exc}", file=sys.stderr)
        return 1
    print(f"enrolled speaker={key} profiles={len(profiles.vectors)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
