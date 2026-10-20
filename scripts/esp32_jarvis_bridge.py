#!/usr/bin/env python3
"""ESP32 satellite voice-assistant bridge to Gemini (phase 1, v1).

Runs on lacasa-server next to the private TCP relay: the ESPHome satellite
device opens its encrypted native API connection through the relay and this
bridge answers its voice-assistant requests with the Gemini API.

Contract (aioesphomeapi voice assistant, API-audio mode):

- ``subscribe_voice_assistant(handle_start, handle_stop, handle_audio)``.
- ``handle_start`` is awaited by ``aioesphomeapi`` and MUST resolve to an int;
  resolving to ``0`` answers ``VoiceAssistantResponse(port=0)`` so the device streams the
  microphone over the encrypted native API connection (no external unicast
  endpoint of any kind is opened by this process).
- ``handle_audio(data, data2)`` delivers 16-bit little-endian mono PCM mic
  chunks; the device (via aioesphomeapi) ends the recording with the empty
  chunk / ``handle_stop(False)``; ``handle_stop(True)`` means the turn was
  aborted and all buffered state is discarded.
- The reply PCM is streamed back with ``send_voice_assistant_audio`` in
  ``REPLY_CHUNK_BYTES``-sized chunks, terminated by one empty chunk (the
  device treats the empty payload as end-of-stream), wrapped in minimal
  run events: ``TTS_STREAM_START`` before the first chunk, ``TTS_END`` and
  ``RUN_END`` after it. STT/VAD/RUN-start phases need no bridge events; the
  firmware drives its own LED phases from device-side triggers.

The Gemini path is deterministic v1 without helpers: one ``generate_content``
call transcribes the mic PCM and produces a short German answer, then the
TTS model turns the answer into 16-bit mono PCM. ``google.genai`` is imported
lazily so importing this module never requires the dependency.

Safety contract mirrors esp32_api_acceptance: noise PSK and Gemini API key are
read only from mode-0600 single-line files via ``esp32_api_acceptance.load_api_key``;
no key material, transcript or audio payload ever appears in logs — status-only
lines. The main loop reconnects with bounded exponential backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from array import array
import base64
import contextlib
import enum
import functools
import inspect
import io
import logging
import secrets
import sys
import time
import wave
from pathlib import Path

from esp32_api_acceptance import KeyFileError, load_api_key  # reused contract
from core.turn_provenance import (
    CHANNEL_ESP32_PCM,
    IngressEnvelope,
    TurnIdentity,
    auth_session_id_of,
)
from core.user_turn import finalize_turn

__all__ = [
    "DEFAULT_CAPTURE_WINDOW_S",
    "DEFAULT_MODEL",
    "DEFAULT_RELAY_PORT",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_TTS_SOURCE_SAMPLE_RATE",
    "DEFAULT_SYSTEM_INSTRUCTION",
    "DEFAULT_TTS_MODEL",
    "DEFAULT_TTS_VOICE",
    "DEFAULT_TURN_TIMEOUT_S",
    "REPLY_CHUNK_BYTES",
    "REPLY_STREAM_URL",
    "EspHomeVoiceClient",
    "GeminiTurnEngine",
    "JarvisBridge",
    "KeyFileError",
    "VoiceAssistantEventType",
    "load_api_key",
    "build_arg_parser",
    "main",
]

_LOG = logging.getLogger("esp32_jarvis_bridge")

DEFAULT_RELAY_PORT = 16053
DEFAULT_SAMPLE_RATE = 16000  # ESPHome voice-assistant microphone and speaker
DEFAULT_TTS_SOURCE_SAMPLE_RATE = 24000  # Gemini TTS raw PCM output
DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_TTS_MODEL = "gemini-3.8-flash-lite-tts"
# Free-tier keys hit per-minute limits per model; the turn must survive a 429
# instead of dropping the user's question.
FALLBACK_CHAT_MODELS = (
    "gemini-flash-latest",
    # The preview chat models reject inline audio (400), so the audio-capable
    # lite models are the real fallbacks behind a 429.
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite-preview",
)
FALLBACK_TTS_MODELS = (
    "gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-3.8-flash-lite-tts",
)
RETRY_DELAYS_S = (2.0, 5.0)
DEFAULT_TTS_VOICE = "Charon"
DEFAULT_TURN_TIMEOUT_S = 60.0
DEFAULT_CAPTURE_WINDOW_S = 5.0
# A follow-up turn follows JARVIS's own question, so the user answers without a
# wake word and usually starts after a short pause.
DEFAULT_FOLLOWUP_WINDOW_S = 8.0
# Dialogue turns replayed to the model so a follow-up keeps its context.
MAX_HISTORY_TURNS = 6
# Listening is driven by speech, not by a fixed cut: the user may talk as long
# as he likes, and the turn only ends after this much silence.
DEFAULT_SILENCE_END_S = 1.2
DEFAULT_MAX_UTTERANCE_S = 25.0
DEFAULT_VOICE_LEVEL = 500  # RMS on the 16-bit scale; room noise stays below
_LISTEN_POLL_S = 0.1
PRE_SPEECH_BUFFER_S = 0.5
REPLY_CHUNK_BYTES = 2048  # 1024 samples of 16-bit mono PCM per message

# TTS_END must carry a non-empty url: the firmware ignores an empty one and
# would then never enter STREAMING_RESPONSE, the only state that writes the
# streamed PCM to the speaker. With `speaker:` bound (no media player) the
# device never fetches it, so this placeholder is inert.
REPLY_STREAM_URL = "http://127.0.0.1/tts/bridge.pcm"

_BACKOFF_START_S = 1.0
_BACKOFF_FACTOR = 2
_BACKOFF_MAX_S = 30.0


def load_gemini_keys(primary: Path, extra_paths) -> tuple[str, ...]:
    """Load the primary Gemini key plus every extra key that exists on disk."""
    keys = [load_api_key(primary)]
    for path in extra_paths:
        try:
            keys.append(load_api_key(path))
        except KeyFileError:
            # An optional second account simply is not configured yet.
            _LOG.info("gemini extra key skipped status=missing file_base=%s", path.name)
    return tuple(keys)

_ASSISTANT_NAME = "JARVIS"
DEFAULT_SYSTEM_INSTRUCTION = (
    "Du bist %s, ein knapper Assistent an einem Lautsprecher im Growraum. "
    "Antworte immer auf Deutsch: kurz, freundlich, in hoestens ein bis zwei "
    "Saetzen ohne Listen. Wenn eine Angabe fehlt (zum Beispiel der Ort beim "
    "Wetter), rate nicht, sondern frage in einem kurzen Satz danach. Wenn dir "
    "Audio fehlt oder du etwas nicht verstehst, bitte einmal kurz wiederholen. "
    "Nutze fuer Befehle wie spielen, pausieren, fortsetzen, naechster Titel, "
    "vorheriger Titel, Lautstaerke oder was gerade laeuft die Spotify-Funktion. "
    "Wenn jemand nach der Temperatur eines Raums fragt, nutze die "
    "Temperature-Funktion mit dem Raumnamen und nenne den gemessenen Wert. "
    "Wenn jemand einen Tagebucheintrag fuer einen Raum machen will, nutze die "
    "Cannanas-Tagebuchfunktion: der Raum ist die Cannanas-Zone und braucht keine "
    "Pflanzenangabe; fehlt der Text, frage kurz danach. "
    "Wenn der Benutzer das Gespraech mit Formulierungen wie 'Mic muten', "
    "'Mikrofon stumm', 'sei stumm' oder 'hoer auf zuzuhoeren' beendet, rufe "
    "ausschliesslich die Mikrofon-Funktion mit action mute auf. "
    "Wenn ein ausdruecklicher Wunsch mit den vorhandenen Funktionen nicht "
    "erfuellbar ist, nutze die Lernfunktion genau einmal. Sage nicht, dass du "
    "es kannst: JARVIS muss es erst lernen."
    % _ASSISTANT_NAME
)

JARVIS_CONFIG_PATH = "/home/lacasa/Mark-LV-LaCasa/config/api_keys.json"
TEMPERATURE_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "room": {
            "type": "string",
            "description": "Raumname wie Madrid, Helsinki oder Tokio.",
        },
    },
    "required": ["room"],
}

DIARY_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "room": {"type": "string", "description": "Cannanas-Raumname."},
        "text": {"type": "string", "description": "Der diktierte Tagebuchtext."},
        "target": {
            "type": "string",
            "description": "Optional: zuvor geklaerte Pflanze oder Charge im Raum.",
        },
    },
    "required": ["room"],
    "additionalProperties": False,
}

LEARNING_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "request": {
            "type": "string",
            "description": "Der unerfuellbare Wunsch als knappe Funktionsanforderung.",
            "maxLength": 1000,
        },
    },
    "required": ["request"],
    "additionalProperties": False,
}

MICROPHONE_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["mute"]}},
    "required": ["action"],
    "additionalProperties": False,
}

BRIEFING_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _room_key(name: str) -> str:
    """Umlaut- and case-insensitive room identifier for matching."""
    return (
        str(name).strip().lower()
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("ß", "ss").replace(" ", "")
    )


def _ha_states(config_path: str) -> list[dict]:
    """Read the HA state list with the token JARVIS already ships with."""
    import json as _json
    import urllib.request as _urllib

    cfg = _json.load(open(config_path))
    url = None
    token = None

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k, v
                yield from _walk(v)
        elif isinstance(node, list):
            for v in node:
                yield None, v
                yield from _walk(v)
        elif isinstance(node, str):
            for part in node.replace('"', " ").split():
                yield None, part

    for k, v in _walk(cfg):
        ks = str(k or "").lower()
        if isinstance(v, str) and v.startswith("ey") and ("token" in ks or "ha" in ks):
            token = v
        if isinstance(v, str) and v.startswith("http") and "url" in ks:
            url = v
    if token is None:
        raise RuntimeError("no home assistant token in jarvis config")
    req = _urllib.Request(
        (url or "http://192.168.1.88:8123") + "/api/states",
        headers={"Authorization": f"Bearer {token}"},
    )
    with _urllib.urlopen(req, timeout=15) as resp:
        return list(_json.load(resp))


def _default_temperature_runner(args: dict) -> str:
    """Speak the measured growroom temperature of the named zone."""
    if not isinstance(args, dict) or not args.get("room"):
        return "Welchen Raum möchtest du wissen?"
    want = _room_key(args["room"])
    zones: dict[str, tuple[str, str]] = {}
    for state in _ha_states(JARVIS_CONFIG_PATH):
        entity_id = str(state.get("entity_id", ""))
        if entity_id.startswith("sensor.zonen_") and entity_id.endswith("_temperatur_celsius"):
            room = entity_id[len("sensor.zonen_"):-len("_temperatur_celsius")]
            zones[_room_key(room)] = (room, state.get("state", ""))
    if want in zones:
        room, raw = zones[want]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return f"Raum {args['room']} liefert gerade keinen Temperaturwert."
        return f"Raum {args['room']} hat aktuell {value:.1f} Grad."
    rooms = ", ".join(zones[k][0] for k in sorted(zones)) or "keine"
    return f"Für Raum {args['room']} habe ich keinen Temperatursensor. Verfügbare Räume: {rooms}."

SPOTIFY_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["play", "pause", "resume", "next", "previous", "volume", "current"],
        },
        "query": {"type": "string"},
        "kind": {"type": "string", "enum": ["track", "album", "artist"]},
        "volume_percent": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _default_spotify_runner(parameters: dict) -> str:
    """Load the deployed narrow controller only when a Spotify command exists."""
    from plugins import spotify_control

    return spotify_control.run(parameters)


class VoiceAssistantEventType(enum.IntEnum):
    """Mirror of esphome's api.proto VoiceAssistantEvent enum.

    Used only when the installed aioesphomeapi does not expose the real enum;
    codes follow esphome's api.proto (TTS stream events live in the 98+ block).
    """

    VOICE_ASSISTANT_ERROR = 0
    VOICE_ASSISTANT_RUN_START = 1
    VOICE_ASSISTANT_RUN_END = 2
    VOICE_ASSISTANT_STT_START = 3
    VOICE_ASSISTANT_STT_END = 4
    VOICE_ASSISTANT_INTENT_START = 5
    VOICE_ASSISTANT_INTENT_END = 6
    VOICE_ASSISTANT_TTS_START = 7
    VOICE_ASSISTANT_TTS_END = 8
    VOICE_ASSISTANT_WAKE_WORD_START = 9
    VOICE_ASSISTANT_WAKE_WORD_END = 10
    VOICE_ASSISTANT_STT_VAD_START = 11
    VOICE_ASSISTANT_STT_VAD_END = 12
    VOICE_ASSISTANT_TTS_STREAM_START = 98
    VOICE_ASSISTANT_TTS_STREAM_END = 99
    VOICE_ASSISTANT_INTENT_PROGRESS = 100


def _pcm_mime(sample_rate: int) -> str:
    """mime-type for raw 16-bit little-endian mono PCM at the declared rate."""
    return f"audio/pcm;rate={int(sample_rate)}"


def _resample_pcm16_mono(data: bytes, source_rate: int, target_rate: int) -> bytes:
    """Linearly resample signed 16-bit mono PCM without optional dependencies."""
    source_rate = int(source_rate)
    target_rate = int(target_rate)
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    usable = len(data) - (len(data) % 2)
    if usable == 0:
        return b""
    samples = array("h")
    samples.frombytes(data[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    if source_rate == target_rate or len(samples) == 1:
        return data[:usable]
    output_count = max(1, (len(samples) * target_rate) // source_rate)
    converted = array("h")
    for out_index in range(output_count):
        numerator = out_index * source_rate
        left = numerator // target_rate
        fraction = numerator % target_rate
        if left >= len(samples) - 1:
            value = samples[-1]
        else:
            value = (
                samples[left] * (target_rate - fraction)
                + samples[left + 1] * fraction
            ) // target_rate
        converted.append(max(-32768, min(32767, value)))
    if sys.byteorder != "little":
        converted.byteswap()
    return converted.tobytes()


@functools.lru_cache(maxsize=None)
def _voice_event(suffix: str):
    """Resolve a VoiceAssistantEventType member (real enum when installed)."""
    full_name = f"VOICE_ASSISTANT_{suffix}"
    try:
        from aioesphomeapi import VoiceAssistantEventType as real_enum
    except ImportError:
        real_enum = VoiceAssistantEventType  # our static mirror
    member = getattr(real_enum, full_name, None)
    if member is None:
        member = getattr(VoiceAssistantEventType, full_name)
    return member


async def _maybe_await(value):
    """Support fakes whose methods are sync or async alike."""
    if inspect.isawaitable(value):
        return await value
    return value


def _inline_audio_bytes(value) -> bytes:
    """Normalize google-genai inline audio across SDK byte/string versions."""
    if isinstance(value, str):
        return base64.b64decode(value, validate=True)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise TypeError("inline audio payload is not bytes or base64 text")


TTS_STYLE_INSTRUCTION = (
    "Sprich ausschließlich auf Deutsch mit natürlicher deutscher Aussprache, "
    "in einer ruhigen, souveränen Männerstimme."
)


def _tts_texts(answer: str) -> tuple[str, str]:
    """Split a spoken turn: style goes to system_instruction, never to content.

    The TTS models read their whole content aloud, so any instruction placed
    in ``contents`` leaks into the speaker as spoken text. Style shaping has
    to live in the config, contents must be exactly the answer.
    """
    return TTS_STYLE_INSTRUCTION, str(answer)


def _is_retryable(exc: BaseException) -> bool:
    """Provider-side transient failures (quota, overload), not model errors."""
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            return int(code) in (429, 500, 502, 503, 504)
        except (TypeError, ValueError):
            pass
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "503" in text


def _is_quota_error(exc: BaseException) -> bool:
    """Quota failures usually persist; switch models instead of sleeping."""
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            if int(code) == 429:
                return True
        except (TypeError, ValueError):
            pass
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def _is_bad_gemini_key(exc: BaseException) -> bool:
    """A dead/revoked second-account key must not kill the turn."""
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            if int(code) in (401, 403):
                return True
        except (TypeError, ValueError):
            pass
    lowered = str(exc).lower()
    return (
        "unauthenticated" in lowered
        or "api key not valid" in lowered
        or "api_key_invalid" in lowered
    )


def _is_model_incompatible(exc: BaseException) -> bool:
    """The model cannot serve THIS request (no audio input, unknown id)."""
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            return int(code) in (400, 404)
        except (TypeError, ValueError):
            pass
    text = str(exc)
    return "INVALID_ARGUMENT" in text or "NOT_FOUND" in text


def _pcm_level(pcm: bytes) -> int:
    """Rough RMS of 16-bit mono PCM, used only to detect that speech started."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable == 0:
        return 0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    total = 0
    for value in samples:
        total += value * value
    return int((total / len(samples)) ** 0.5)


def _listen_cue_pcm() -> bytes:
    """Soft double-beep marking "the microphone is open now".
    Device-rate 16 kHz mono: 250 ms of silence, two 120 ms beeps at 880 Hz.
    The board reopens its microphone right after the reply, so the cue tells
    the user exactly when his answer will be heard.
    """
    rate = DEFAULT_SAMPLE_RATE
    cue = bytearray()
    cue += b"\x00\x00" * int(rate * 0.25)
    for _beat in range(2):
        for i in range(int(rate * 0.12)):
            value = int(2500 * math.sin(2.0 * math.pi * 880.0 * i / rate))
            cue += int(value).to_bytes(2, "little", signed=True)
        cue += b"\x00\x00" * int(rate * 0.08)
    return bytes(cue)


def _wants_followup(answer: str) -> bool:
    """Always keep the microphone open after a reply.

    The user talks continuously: the turn ends on silence (the idle window in
    ``_listen_until_done``), never because our answer did not look like a
    question. The argument is still accepted so call sites stay explicit.
    """
    return True


def _decode_tts_audio(payload: bytes) -> tuple[bytes, int]:
    """Return raw 16-bit mono PCM and its sample rate.

    Newer Gemini TTS models answer with a WAV container (``audio/wav``) while
    older ones return bare ``audio/L16`` PCM; both must reach the device as raw
    PCM, because the ESP32 firmware writes the API audio frame untouched.
    """
    data = bytes(payload)
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        with wave.open(io.BytesIO(data), "rb") as handle:
            if handle.getsampwidth() != 2:
                raise ValueError("TTS WAV must be 16-bit")
            rate = handle.getframerate()
            channels = handle.getnchannels()
            frames = handle.readframes(handle.getnframes())
        if channels == 1:
            return frames, rate
        if channels == 2:
            samples = array("h")
            samples.frombytes(frames)
            mono = array(
                "h",
                ((samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)),
            )
            return mono.tobytes(), rate
        raise ValueError("unsupported TTS channel count")
    return data, DEFAULT_TTS_SOURCE_SAMPLE_RATE


def _tts_pcm_from_response(payload: bytes) -> bytes:
    """Decode a TTS reply to raw PCM at ``DEFAULT_TTS_SOURCE_SAMPLE_RATE``."""
    pcm, rate = _decode_tts_audio(payload)
    if rate == DEFAULT_TTS_SOURCE_SAMPLE_RATE:
        return pcm
    return _resample_pcm16_mono(pcm, rate, DEFAULT_TTS_SOURCE_SAMPLE_RATE)


# --------------------------------------------------------------------------- Gemini

class GeminiTurnEngine:
    """Simple deterministic v1 turn: transcribe+answer, then TTS.

    One ``generate_content`` call takes the whole mic PCM inline and returns
    the short German answer (no helpers, no function calls). A second call
    with the TTS model turns the answer into inline PCM audio.
    """

    def __init__(
        self,
        gemini_key: str | tuple[str, ...],
        *,
        model: str = DEFAULT_MODEL,
        tts_model: str = DEFAULT_TTS_MODEL,
        tts_voice: str = DEFAULT_TTS_VOICE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        system_instruction: str | None = None,
        spotify_runner=None,
        temperature_runner=None,
        diary_workflow=None,
        learning_queue=None,
        briefing_runner=None,
    ):
        self._gemini_keys = (
            (gemini_key,) if isinstance(gemini_key, str) else tuple(gemini_key)
        )
        self._key_index = 0
        self._model = model
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._sample_rate = int(sample_rate)
        self._system_instruction = system_instruction or DEFAULT_SYSTEM_INSTRUCTION
        self._spotify_runner = spotify_runner or _default_spotify_runner
        self._temperature_runner = temperature_runner or _default_temperature_runner
        self._diary_workflow = diary_workflow
        self._learning_queue = learning_queue
        self._briefing_runner = briefing_runner
        self._turn_context = None
        self._history: list[dict] = []
        self._chat_model_pref: str | None = None
        self._tts_model_pref: str | None = None
        self._announcement_job_id = ""
        self._mute_requested = False

    def current_key(self) -> str:
        return self._gemini_keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Advance to the next key; False when every key was tried."""
        if self._key_index + 1 >= len(self._gemini_keys):
            return False
        self._key_index += 1
        return True

    def _turn_with_key_rotation(self, turn_fn) -> object:
        """Run a turn on one key; rotate to the next key on quota exhaustion."""
        first: BaseException | None = None
        while True:
            try:
                return turn_fn(self.current_key())
            except Exception as exc:
                if not (_is_quota_error(exc) or _is_bad_gemini_key(exc)):
                    raise
                if first is None:
                    first = exc
                if not self._rotate_key():
                    raise first

    def reset_conversation(self) -> None:
        """Drop the remembered dialogue (a new wake-word turn starts fresh)."""
        self._history = []
        self._mute_requested = False

    def consume_mute_request(self) -> bool:
        """Consume one request to return the satellite to wake-word-only mode."""
        requested, self._mute_requested = self._mute_requested, False
        return requested

    def set_turn_context(self, turn) -> None:
        """Pin immutable provenance for every model-facing side effect."""
        self._turn_context = turn

    def has_pending_confirmation(self) -> bool:
        workflow = self._diary_workflow
        return bool(
            workflow is not None
            and self._turn_context is not None
            and workflow.has_pending(self._turn_context)
        )

    def evaluate_pending_confirmation(self, transcript: str) -> str | None:
        workflow = self._diary_workflow
        if workflow is None or self._turn_context is None:
            return None
        return workflow.evaluate_confirmation(self._turn_context, transcript)

    def cancel_pending_confirmation(self) -> bool:
        """Fence a pending preview when the satellite connection goes away."""
        workflow = self._diary_workflow
        turn = self._turn_context
        if workflow is None or turn is None:
            return False
        cancel = getattr(workflow, "cancel_pending", None)
        if not callable(cancel):
            return False
        return bool(cancel(getattr(turn, "stable_device_id", "") or ""))

    def attach_completion_announcement(self, answer: str) -> str:
        queue = self._learning_queue
        if queue is None:
            return answer
        claim = getattr(queue, "claim_completion_announcement", None)
        if not callable(claim):
            return answer
        claimed = claim()
        if not claimed:
            return answer
        announcement, job_id = claimed
        # Held until the bridge confirms the audio was streamed (or releases it).
        self._announcement_job_id = str(job_id)
        return f"{announcement} {answer}".strip()

    def confirm_announcement_spoken(self) -> bool:
        """The reply including the announcement reached the speaker."""
        job_id, self._announcement_job_id = self._announcement_job_id, ""
        if not job_id or self._learning_queue is None:
            return False
        ack = getattr(self._learning_queue, "ack_completion_announcement", None)
        return bool(callable(ack) and ack(job_id))

    def release_announcement(self) -> bool:
        """Speaking failed: hand the announcement back for a later turn."""
        job_id, self._announcement_job_id = self._announcement_job_id, ""
        if not job_id or self._learning_queue is None:
            return False
        release = getattr(self._learning_queue, "release_completion_announcement", None)
        return bool(callable(release) and release(job_id))

    def _contents(self, mic_pcm: bytes) -> list[dict]:
        """Dialogue so far plus this turn's audio."""
        contents = list(self._history)
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": _pcm_mime(self._sample_rate),
                            "data": bytes(mic_pcm),
                        }
                    }
                ],
            }
        )
        return contents

    def _remember(self, mic_pcm: bytes, answer: str) -> None:
        """Keep both sides of the turn so a follow-up keeps its context."""
        self._history.append(
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": _pcm_mime(self._sample_rate),
                            "data": bytes(mic_pcm),
                        }
                    }
                ],
            }
        )
        self._history.append({"role": "model", "parts": [{"text": answer}]})
        del self._history[: -2 * MAX_HISTORY_TURNS]

    async def turn(self, mic_pcm: bytes) -> tuple[str, bytes]:
        return await asyncio.to_thread(self._turn_sync, mic_pcm)

    @staticmethod
    def _call_with_retries(client, model: str, contents, config):
        """Retry provider quota/overload failures before giving up on a model."""
        last: BaseException | None = None
        for delay in (0.0,) + RETRY_DELAYS_S:
            if delay:
                time.sleep(delay)
            try:
                return client.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:
                if _is_quota_error(exc):
                    raise
                if not _is_retryable(exc):
                    raise
                last = exc
        assert last is not None
        raise last

    def _model_order(self, preferred: str | None, chain) -> tuple[str, ...]:
        """Try the model that last worked first, then the rest of the chain."""
        head = (preferred,) if preferred else ()
        return tuple(dict.fromkeys(head + tuple(chain)))

    def _generate(self, client, models, contents, config, *, retry_config=None):
        """Walk a model chain so one exhausted model does not drop the turn."""
        last: BaseException | None = None
        for model in models:
            try:
                result = self._call_with_retries(client, model, contents, config)
                return model, result
            except Exception as exc:
                if _is_retryable(exc) or _is_model_incompatible(exc):
                    _LOG.info("model unavailable status=fallback model=%s", model)
                    if (
                        _is_model_incompatible(exc)
                        and retry_config is not None
                        and retry_config is not config
                    ):
                        # Same model once more without optional shaping (the
                        # lite TTS model rejects system_instruction outright).
                        _LOG.info("model retry status=plain-config model=%s", model)
                        try:
                            return model, self._call_with_retries(
                                client, model, contents, retry_config
                            )
                        except Exception as retry_exc:
                            if _is_retryable(retry_exc) or _is_model_incompatible(retry_exc):
                                last = retry_exc
                                continue
                            raise
                    last = exc
                    continue
                raise
        assert last is not None
        raise last

    def _answer_from_response(self, response) -> str:
        """Run only the two narrow whitelisted voice functions."""
        for candidate in getattr(response, "candidates", ()) or ():
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", ()) or ():
                call = getattr(part, "function_call", None)
                if call is None:
                    continue
                name = getattr(call, "name", "")
                parameters = dict(getattr(call, "args", None) or {})
                if name == "spotify_control":
                    runner = self._spotify_runner
                    answer = runner(parameters)
                elif name == "growroom_temperature":
                    runner = self._temperature_runner
                    answer = runner(parameters)
                elif name == "cannanas_diary_prepare":
                    if self._diary_workflow is None or self._turn_context is None:
                        raise RuntimeError("diary workflow unavailable")
                    answer = self._diary_workflow.prepare(parameters, self._turn_context)
                elif name == "request_capability_learning":
                    if self._learning_queue is None or self._turn_context is None:
                        raise RuntimeError("learning queue unavailable")
                    answer = self._learning_queue.submit(parameters, self._turn_context)
                elif name == "microphone_control":
                    if parameters != {"action": "mute"}:
                        raise RuntimeError("unsupported microphone action")
                    self._mute_requested = True
                    answer = "Mikrofon ist stumm."
                elif name == "growroom_briefing":
                    if self._briefing_runner is None:
                        raise RuntimeError("briefing unavailable")
                    answer = self._briefing_runner()
                else:
                    raise RuntimeError("unsupported model function")
                if not isinstance(answer, str) or not answer.strip():
                    raise RuntimeError("voice function runner returned no text")
                return answer.strip()
        return str(getattr(response, "text", "") or "").strip()

    def _turn_sync(self, mic_pcm: bytes) -> tuple[str, bytes]:
        from google import genai  # lazy, optional dependency

        # The answer phase owns every host side effect (Spotify, Cannanas,
        # learning job, announcement claim). It runs exactly once; a quota
        # failure during synthesis must never replay it.
        answer = self._turn_with_key_rotation(
            lambda _key: self._answer_on_key(genai, mic_pcm)
        )
        pcm = self._turn_with_key_rotation(
            lambda _key: self._speech_on_key(genai, answer)
        )
        self._remember(mic_pcm, answer)  # only once the turn is actually spoken
        return answer, pcm

    def _chat_tools(self, types):
        declarations = [
            types.FunctionDeclaration(
                name="spotify_control",
                description=(
                    "Steuert Spotify auf dem festen LaCasa-Empfaenger. "
                    "Bei play Suchbegriff und Typ, bei volume Prozent angeben."
                ),
                parameters_json_schema=SPOTIFY_FUNCTION_SCHEMA,
            ),
            types.FunctionDeclaration(
                name="growroom_temperature",
                description=(
                    "Gibt die aktuelle Raumtemperatur eines Growroom-Raums "
                    "wieder (zum Beispiel Madrid oder Helsinki)."
                ),
                parameters_json_schema=TEMPERATURE_FUNCTION_SCHEMA,
            ),
            types.FunctionDeclaration(
                name="microphone_control",
                description=(
                    "Beendet den aktiven Dauerdialog und schaltet auf lokalen "
                    "Wakeword-Betrieb zurueck. Bei Mic muten, Mikrofon stumm, "
                    "sei stumm oder hoer auf zuzuhoeren immer allein aufrufen."
                ),
                parameters_json_schema=MICROPHONE_FUNCTION_SCHEMA,
            ),
        ]
        if self._diary_workflow is not None:
            declarations.append(types.FunctionDeclaration(
                name="cannanas_diary_prepare",
                description=(
                    "Bereitet einen Tagebucheintrag direkt fuer die genannte "
                    "Cannanas-Zone vor. target nur setzen, wenn der Benutzer "
                    "ausdruecklich eine Pflanze oder Charge nennt. Speichern "
                    "erfolgt erst nach einer spaeteren gesprochenen Bestaetigung."
                ),
                parameters_json_schema=DIARY_FUNCTION_SCHEMA,
            ))
        if self._learning_queue is not None:
            declarations.append(types.FunctionDeclaration(
                name="request_capability_learning",
                description=(
                    "Gibt einen ausdruecklichen Wunsch, den JARVIS noch nicht "
                    "erfuellen kann, einmalig als Lernauftrag an Hermes weiter."
                ),
                parameters_json_schema=LEARNING_FUNCTION_SCHEMA,
            ))
        if self._briefing_runner is not None:
            declarations.append(types.FunctionDeclaration(
                name="growroom_briefing",
                description=(
                    "Gibt das fertige Morgen-Briefing aus: Begruessung, eigene "
                    "Aufgaben und pro Raum Bluemestadium, Temperatur und letzter "
                    "Tagebucheintrag. Bei 'Briefing', 'Guten Morgen' oder 'was "
                    "steht an' genau einmal aufrufen und den Text unveraendert "
                    "vorlesen."
                ),
                parameters_json_schema=BRIEFING_FUNCTION_SCHEMA,
            ))
        return [types.Tool(function_declarations=declarations)]

    def _transcribe_pending_confirmation(self, client, types, chat_models, mic_pcm):
        """Transcribe only; the host grammar, never Gemini, decides confirmation."""
        used_model, response = self._generate(
            client,
            chat_models,
            [{
                "role": "user",
                "parts": [{"inline_data": {
                    "mime_type": _pcm_mime(self._sample_rate),
                    "data": bytes(mic_pcm),
                }}],
            }],
            types.GenerateContentConfig(
                system_instruction=(
                    "Transkribiere die deutsche Aufnahme wortgetreu. Gib nur "
                    "die gesprochenen Woerter aus, ohne Kommentar."
                ),
            ),
        )
        transcript = str(getattr(response, "text", "") or "").strip()
        return used_model, transcript

    def _answer_on_key(self, genai, mic_pcm: bytes) -> str:
        """Answer the turn once: transcription, tools, announcement claim."""
        client = genai.Client(
            api_key=self.current_key(), http_options={"api_version": "v1beta"}
        )
        types = genai.types
        chat_models = self._model_order(
            self._chat_model_pref, (self._model,) + FALLBACK_CHAT_MODELS
        )
        answer = None
        if self.has_pending_confirmation():
            used_chat, transcript = self._transcribe_pending_confirmation(
                client, types, chat_models, mic_pcm
            )
            self._chat_model_pref = used_chat
            answer = self.evaluate_pending_confirmation(transcript)
        if answer is None:
            used_chat, answer_call = self._generate(
                client,
                chat_models,
                self._contents(mic_pcm),
                types.GenerateContentConfig(
                    system_instruction=self._system_instruction,
                    tools=self._chat_tools(types),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            self._chat_model_pref = used_chat
            answer = self._answer_from_response(answer_call)
        if not answer:
            raise RuntimeError("model returned no text")
        return self.attach_completion_announcement(answer)

    def _speech_on_key(self, genai, answer: str) -> bytes:
        """Render the fixed answer; retried on another key without side effects."""
        client = genai.Client(
            api_key=self.current_key(), http_options={"api_version": "v1beta"}
        )
        types = genai.types
        tts_models = self._model_order(
            self._tts_model_pref, (self._tts_model,) + FALLBACK_TTS_MODELS
        )
        style, spoken = _tts_texts(answer)
        plain_without_voice = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._tts_voice
                    )
                )
            ),
        )
        used_tts, speech_call = self._generate(
            client,
            tts_models,
            spoken,
            types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                system_instruction=style,
                speech_config=plain_without_voice.speech_config,
            ),
            retry_config=plain_without_voice,
        )
        self._tts_model_pref = used_tts
        part = speech_call.candidates[0].content.parts[0]
        return _tts_pcm_from_response(_inline_audio_bytes(part.inline_data.data))


# --------------------------------------------------------------------------- ESPHome client

class EspHomeVoiceClient:
    """Lazy aioesphomeapi client facade for a single satellite session."""

    _DISCONNECT_POLL_S = 0.5

    def __init__(self, host: str, port: int, noise_psk: str):
        self.host = str(host)
        self.port = int(port)
        self._noise_psk = noise_psk
        self._client = None

    async def connect_and_subscribe(self, handle_start, handle_stop, handle_audio, _client_override=None) -> None:
        # _client_override is a test hook standing in for the real client.
        if _client_override is not None:
            self._client = _client_override
            client = self._client
        else:
            import aioesphomeapi  # lazy, optional

            client = aioesphomeapi.APIClient(
                address=self.host,
                port=self.port,
                password=None,
                noise_psk=self._noise_psk,
            )
            await client.connect()
        try:
            # 46.3.0 requires keyword-only callbacks.
            client.subscribe_voice_assistant(
                handle_start=handle_start,
                handle_stop=handle_stop,
                handle_audio=handle_audio,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        self._client = client

    async def send_voice_assistant_event(self, event_type, data=None) -> None:
        self._client.send_voice_assistant_event(event_type, data)

    async def send_voice_assistant_audio(self, data: bytes) -> None:
        self._client.send_voice_assistant_audio(data)

    async def wait_until_disconnected(self) -> None:
        """Best-effort end detection: poll the underlying connection state."""
        while True:
            await asyncio.sleep(self._DISCONNECT_POLL_S)
            internal = getattr(self._client, "_connection", None)
            if internal is None or getattr(internal, "is_connected", True) is False:
                return

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()


# --------------------------------------------------------------------------- bridge

class JarvisBridge:
    """Handle aioesphomeapi voice turns and answer them with Gemini (v1)."""

    def __init__(
        self,
        *,
        relay_host: str,
        relay_port: int = DEFAULT_RELAY_PORT,
        api_key: str,
        gemini_key: str | tuple[str, ...],
        engine=None,
        model: str = DEFAULT_MODEL,
        tts_model: str = DEFAULT_TTS_MODEL,
        tts_voice: str = DEFAULT_TTS_VOICE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        tts_source_sample_rate: int = DEFAULT_TTS_SOURCE_SAMPLE_RATE,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT_S,
        capture_window: float = DEFAULT_CAPTURE_WINDOW_S,
        followup_window: float = DEFAULT_FOLLOWUP_WINDOW_S,
        silence_end: float = DEFAULT_SILENCE_END_S,
        max_utterance: float = DEFAULT_MAX_UTTERANCE_S,
        voice_level: int = DEFAULT_VOICE_LEVEL,
        system_instruction: str | None = None,
        device_id: str = "growroom-sprache-1",
        diary_workflow=None,
        learning_queue=None,
        briefing_runner=None,
        voice_client_factory=None,
        sleep=None,
        ducking=None,
        session_nonce_factory=None,
    ):
        self.relay_host = str(relay_host)
        self.relay_port = int(relay_port)
        self._turn_timeout = float(turn_timeout)
        self._device_sample_rate = int(sample_rate)
        self._tts_source_sample_rate = int(tts_source_sample_rate)
        if self._device_sample_rate <= 0 or self._tts_source_sample_rate <= 0:
            raise ValueError("sample rates must be positive")
        self._capture_window = float(capture_window)
        if self._capture_window <= 0:
            raise ValueError("capture_window must be positive")
        self._followup_window = float(followup_window)
        if self._followup_window <= 0:
            raise ValueError("followup_window must be positive")
        self._silence_end = float(silence_end)
        if self._silence_end <= 0:
            raise ValueError("silence_end must be positive")
        self._max_utterance = float(max_utterance)
        if self._max_utterance <= 0:
            raise ValueError("max_utterance must be positive")
        self._voice_level = int(voice_level)
        self.engine: GeminiTurnEngine = engine or GeminiTurnEngine(
            gemini_key,
            model=model,
            tts_model=tts_model,
            tts_voice=tts_voice,
            sample_rate=sample_rate,
            system_instruction=system_instruction,
            diary_workflow=diary_workflow,
            learning_queue=learning_queue,
            briefing_runner=briefing_runner,
        )
        self._device_id = str(device_id).strip() or "growroom-sprache-1"
        self._key_digest = auth_session_id_of(api_key)
        self._session_nonce_factory = session_nonce_factory or (
            lambda: secrets.token_hex(16)
        )
        self._session_nonce = ""
        self._auth_session_id = self._key_digest
        self._turn_identity = TurnIdentity()
        self.voice_client_factory = voice_client_factory or (
            lambda: EspHomeVoiceClient(self.relay_host, self.relay_port, api_key)
        )
        self._voice = None
        self._ducking = ducking
        self._mic_chunks: list[bytes] = []
        self._capture_task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._active = False
        self._listening_announced = False
        self._stopped = False
        # Multi-turn: the device re-enters the pipeline without a wake word
        # after an answer of ours that asked something.
        self._followup_pending = False
        self._is_followup_turn = False
        self._conversation_id = ""
        self._heard_speech = False
        self._last_voice_at = 0.0
        self._first_voice_at = 0.0
        self._window_peak_rms = 0
        if sleep is not None:
            self._sleep = sleep
        else:
            self._sleep = asyncio.sleep

    # -- lifecycle

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Connect, serve and reconnect with bounded backoff until stopped."""
        backoff = _BACKOFF_START_S
        while not self._stopped:
            try:
                await self.session(self.voice_client_factory())
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.info("satellite connection lost; status=retrying")
            else:
                backoff = _BACKOFF_START_S
                continue
            if self._stopped:
                break
            await self._sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)
        _LOG.info("bridge stopped status=idle")

    async def session(self, voice) -> None:
        """Serve one built voice client until the connection drops."""
        await voice.connect_and_subscribe(self.handle_start, self.handle_stop, self.handle_audio)
        self._voice = voice
        self._begin_session()
        _LOG.info("satellite connected relay_port=%d", self.relay_port)
        try:
            await _maybe_await(voice.wait_until_disconnected())
            _LOG.info("satellite disconnected status=closed")
        finally:
            self._voice = None
            self._abort_turn()
            self._cancel_pending_confirmation()
            await _maybe_await(voice.close())

    def _begin_session(self) -> None:
        """Fence every connection with its own opaque session identity.

        The ESPHome PSK digest alone is constant for the life of the process, so
        a reconnect would otherwise inherit the previous connection's identity
        and could confirm a preview prepared before the disconnect.
        """
        self._session_nonce = str(self._session_nonce_factory())
        self._auth_session_id = f"{self._key_digest}:{self._session_nonce}"

    def _cancel_pending_confirmation(self) -> None:
        cancel = getattr(self.engine, "cancel_pending_confirmation", None)
        if not callable(cancel):
            return
        try:
            if cancel():
                _LOG.info("pending confirmation cancelled status=disconnected")
        except Exception as exc:  # never let fencing break a reconnect
            _LOG.info(
                "pending confirmation cancel failed error_type=%s",
                type(exc).__name__,
            )

    def _confirm_announcement_spoken(self) -> None:
        confirm = getattr(self.engine, "confirm_announcement_spoken", None)
        if not callable(confirm):
            return
        try:
            if confirm():
                _LOG.info("learning announcement spoken status=acknowledged")
        except Exception as exc:
            _LOG.info(
                "learning announcement ack failed error_type=%s", type(exc).__name__
            )

    def _release_announcement(self) -> None:
        release = getattr(self.engine, "release_announcement", None)
        if not callable(release):
            return
        try:
            if release():
                _LOG.info("learning announcement status=retry")
        except Exception as exc:
            _LOG.info(
                "learning announcement release failed error_type=%s",
                type(exc).__name__,
            )

    # -- device callbacks (awaited by aioesphomeapi 46.3.0)

    async def handle_start(
        self, conversation_id, flags, audio_settings, wake_word_phrase
    ) -> int:
        """Resolve the start request to encrypted API-audio mode (port 0)."""
        self._abort_turn()  # discard any stale turn from a previous round
        self._mic_chunks = []
        self._active = True
        self._listening_announced = False
        # ESPHome may repeat the original wake-word phrase on the automatic
        # continue-conversation callback. The bridge's pending state is the
        # authoritative signal that this is the follow-up turn.
        followup = bool(self._followup_pending)
        self._is_followup_turn = followup
        self._followup_pending = False
        if not followup:
            self._reset_conversation()
        if conversation_id:
            self._conversation_id = str(conversation_id)
        window = self._followup_window if followup else self._capture_window
        self._heard_speech = False
        self._last_voice_at = 0.0
        self._first_voice_at = 0.0
        self._window_peak_rms = 0
        self._capture_task = asyncio.get_running_loop().create_task(
            self._listen_until_done(None if followup else window)
        )
        _LOG.info(
            "assistant start flags=%s wake_word=%s followup=%s idle_window=%s",
            int(bool(flags)),
            bool(wake_word_phrase),
            followup,
            "persistent" if followup else window,
        )
        return 0

    def _reset_conversation(self) -> None:
        """Forget the running dialogue in the engine (new wake-word turn)."""
        reset = getattr(self.engine, "reset_conversation", None)
        if callable(reset):
            reset()

    async def _listen_until_done(self, idle_window: float | None) -> None:
        """Keep listening while the user speaks; end after real silence.

        ``idle_window`` only bounds the wait for the FIRST speech; once speech
        was heard the turn ends ``silence_end`` seconds after the last voiced
        chunk, so a long sentence is never cut in the middle.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            while self._active:
                await asyncio.sleep(_LISTEN_POLL_S)
                if not self._active:
                    return
                now = loop.time()
                if self._heard_speech:
                    if now - self._last_voice_at >= self._silence_end:
                        _LOG.info("listen ended status=silence")
                        self._finish_turn_request()
                        return
                    if now - self._first_voice_at >= self._max_utterance:
                        _LOG.info("listen ended status=max-utterance")
                        self._finish_turn_request()
                        return
                elif idle_window is not None and now - started >= idle_window:
                    _LOG.info("listen ended status=idle-timeout")
                    self._finish_turn_request(discard_if_silent=True)
                    return
        finally:
            if self._capture_task is asyncio.current_task():
                self._capture_task = None

    async def handle_audio(self, data: bytes, data2: bytes | None) -> None:
        if not self._active or self._voice is None:
            return
        if not self._listening_announced:
            await self._voice.send_voice_assistant_event(_voice_event("RUN_START"), {})
            await self._voice.send_voice_assistant_event(_voice_event("STT_START"), {})
            self._listening_announced = True
            _LOG.info("microphone stream started status=listening")
        if not data:  # empty chunk marks the end of the recording
            self._finish_turn_request()
            return
        peak = _pcm_level(data)
        if peak >= self._voice_level:
            if not self._heard_speech:
                self._first_voice_at = asyncio.get_running_loop().time()
            self._heard_speech = True
            self._last_voice_at = asyncio.get_running_loop().time()
        if peak > self._window_peak_rms:
            self._window_peak_rms = peak
        self._mic_chunks.append(bytes(data))
        if data2:
            self._mic_chunks.append(bytes(data2))
        if not self._heard_speech:
            limit = max(2, int(self._device_sample_rate * 2 * PRE_SPEECH_BUFFER_S))
            retained = b"".join(self._mic_chunks)[-limit:]
            self._mic_chunks = [retained] if retained else []

    async def handle_stop(self, aborted: bool) -> None:
        if aborted:
            _LOG.info("assistant aborted status=discarded")
            self._abort_turn()
        else:
            self._finish_turn_request()

    # -- turn execution

    def _finish_turn_request(self, *, discard_if_silent: bool = False) -> None:
        capture_task = self._capture_task
        self._capture_task = None
        _LOG.info(
            "listen window closed status=review peak_rms=%s voice_level=%s",
            self._window_peak_rms,
            self._voice_level,
        )
        if capture_task is not None and capture_task is not asyncio.current_task():
            capture_task.cancel()
        self._active = False
        if self._turn_task is not None:
            return  # already running
        if discard_if_silent and not self._heard_speech:
            # The device continuously sends room-noise frames during an
            # automatic follow-up. Do not send those frames back to the model,
            # which can otherwise repeat its previous answer indefinitely.
            self._mic_chunks = []
        if not self._mic_chunks:
            voice = self._voice
            self._reset_turn()
            _LOG.info("turn empty status=skipped")
            if voice is not None:
                if self._is_followup_turn:
                    # ESPHome may end a silent stream on its side. Re-open it
                    # without calling the model and keep the dialogue alive.
                    _LOG.info("followup extended status=listening")
                    self._turn_task = asyncio.get_running_loop().create_task(
                        self._stream_reply_pcm(
                            voice,
                            b"\x00\x00",
                            answer="",
                            continue_conversation=True,
                        )
                    )
                else:
                    _LOG.info("conversation closed status=silent-timeout")
                    self._turn_task = asyncio.get_running_loop().create_task(
                        self._close_silent(voice)
                    )
            return
        voice = self._voice
        if voice is None:
            return
        loop = asyncio.get_running_loop()
        self._turn_task = loop.create_task(self._run_turn(voice))

    def _abort_turn(self) -> None:
        capture_task = self._capture_task
        self._capture_task = None
        if capture_task is not None and not capture_task.done():
            capture_task.cancel()
        task = self._turn_task
        self._turn_task = None
        self._reset_turn()
        if task is not None and not task.done():
            task.cancel()

    def _reset_turn(self) -> None:
        self._mic_chunks = []
        self._active = False
        self._listening_announced = False

    async def _run_turn(self, voice) -> None:
        mic_pcm = b"".join(self._mic_chunks)
        self._mic_chunks = []  # take over the buffered audio for this one turn
        try:
            try:
                turn_id = self._turn_identity.issue()
                envelope = IngressEnvelope(
                    stable_device_id=self._device_id,
                    auth_session_id=self._auth_session_id,
                    capture_id=(
                        f"{self._session_nonce or 'session'}:"
                        f"{self._conversation_id or 'conversation'}:{turn_id}"
                    ),
                    user_turn_id=turn_id,
                    channel=CHANNEL_ESP32_PCM,
                    final=True,
                )
                turn = finalize_turn(envelope)
                set_context = getattr(self.engine, "set_turn_context", None)
                if callable(set_context):
                    set_context(turn)
                result = await asyncio.wait_for(
                    _maybe_await(self.engine.turn(mic_pcm)),
                    timeout=self._turn_timeout,
                )
            except asyncio.TimeoutError:
                _LOG.info("turn timed out status=discarded")
                self._release_announcement()
                await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
                return
            except Exception as exc:  # status-only: never log engine internals
                _LOG.info("turn failed status=discarded error_type=%s", type(exc).__name__)
                self._release_announcement()
                await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
                return
            transcript, reply_pcm = result
            if not self._stopped:
                try:
                    consume_mute = getattr(self.engine, "consume_mute_request", None)
                    muted = bool(callable(consume_mute) and consume_mute())
                    if muted:
                        self._cancel_pending_confirmation()
                        _LOG.info("conversation closed status=spoken-mute")
                    await self._stream_reply(
                        voice,
                        reply_pcm,
                        answer=transcript,
                        continue_conversation=(not muted and _wants_followup(transcript)),
                    )
                except Exception as exc:
                    # The announcement was claimed but never heard: release it
                    # so a later turn retries instead of losing it forever.
                    self._release_announcement()
                    _LOG.info(
                        "reply stream failed status=discarded error_type=%s",
                        type(exc).__name__,
                    )
                    with contextlib.suppress(Exception):
                        await voice.send_voice_assistant_event(
                            _voice_event("RUN_END"), {}
                        )
                    return
                self._confirm_announcement_spoken()
            _LOG.info("turn finished chars=%d audio_bytes=%d", len(transcript), len(reply_pcm))
        finally:
            if self._turn_task is asyncio.current_task():
                self._turn_task = None

    async def _send_run_end(self, voice) -> None:
        try:
            await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
        finally:
            if self._turn_task is asyncio.current_task():
                self._turn_task = None

    async def _close_silent(self, voice) -> None:
        """End a conversation whose follow-up windows elapsed without speech."""
        try:
            await voice.send_voice_assistant_event(
                _voice_event("INTENT_END"),
                {
                    "conversation_id": self._conversation_id,
                    "continue_conversation": "0",
                    "speech": "",
                },
            )
            self._followup_pending = False
            self._reset_conversation()
        finally:
            await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
            if self._turn_task is asyncio.current_task():
                self._turn_task = None

    async def _stream_reply(
        self, voice, reply_pcm, *, answer: str = "", continue_conversation: bool = False
    ) -> None:
        pcm = _resample_pcm16_mono(
            bytes(reply_pcm), self._tts_source_sample_rate, self._device_sample_rate
        )
        if continue_conversation:
            pcm += _listen_cue_pcm()
        ducking = self._ducking
        ducked = False
        if ducking is not None:
            ducked = bool(ducking.begin())
            if ducked:
                # Extend the recovery lease across the already-rendered audio.
                ducking.heartbeat(len(pcm) / (self._device_sample_rate * 2) + 1.0)
        try:
            await self._stream_reply_pcm(
                voice,
                pcm,
                answer=answer,
                continue_conversation=continue_conversation,
            )
        finally:
            if ducked and ducking is not None:
                ducking.end()

    async def _stream_reply_pcm(
        self, voice, pcm: bytes, *, answer: str, continue_conversation: bool
    ) -> None:
        # INTENT_END carries continue_conversation; the firmware restarts the
        # microphone after the reply only when that flag is set, which is what
        # lets the user answer a follow-up question without the wake word.
        await voice.send_voice_assistant_event(
            _voice_event("INTENT_END"),
            {
                "conversation_id": self._conversation_id,
                "continue_conversation": "1" if continue_conversation else "0",
                "speech": answer,
            },
        )
        self._followup_pending = continue_conversation
        # TTS_START starts the raw API speaker; TTS_END is what moves the device
        # into STREAMING_RESPONSE (only that state writes audio to the speaker);
        # STREAM_START/END bracket the PCM.
        await voice.send_voice_assistant_event(
            _voice_event("TTS_START"), {"text": answer or "JARVIS"}
        )
        await voice.send_voice_assistant_event(
            _voice_event("TTS_END"), {"url": REPLY_STREAM_URL}
        )
        await voice.send_voice_assistant_event(_voice_event("TTS_STREAM_START"), {})
        if pcm:
            loop = asyncio.get_running_loop()
            started = loop.time()
            audio_seconds_sent = 0.0
            bytes_per_second = self._device_sample_rate * 2
            for offset in range(0, len(pcm), REPLY_CHUNK_BYTES):
                chunk = pcm[offset : offset + REPLY_CHUNK_BYTES]
                await voice.send_voice_assistant_audio(chunk)
                audio_seconds_sent += len(chunk) / bytes_per_second
                elapsed = loop.time() - started
                wait_time = (audio_seconds_sent - 0.384) - elapsed
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
        await voice.send_voice_assistant_event(_voice_event("TTS_STREAM_END"), {})
        await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
        if not continue_conversation:
            self._reset_conversation()


# --------------------------------------------------------------------------- CLI

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ESP32 satellite voice bridge: aioesphomeapi x Gemini (v1)"
    )
    parser.add_argument("--relay-host", required=True)
    parser.add_argument("--relay-port", type=int, default=DEFAULT_RELAY_PORT)
    parser.add_argument(
        "--key-file",
        default=None,
        help="ESPHome API (noise) key file, mode 0600 single line",
    )
    parser.add_argument(
        "--api-key-file",
        default=None,
        help="alias for --key-file; exactly one of the two is required",
    )
    parser.add_argument("--gemini-key-file", required=True)
    parser.add_argument(
        "--extra-gemini-key-file",
        action="append",
        default=[],
        help="further Gemini key files for quota rotation; missing files are skipped",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    parser.add_argument("--tts-voice", default=DEFAULT_TTS_VOICE)
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT_S)
    parser.add_argument("--capture-window", type=float, default=DEFAULT_CAPTURE_WINDOW_S)
    parser.add_argument(
        "--followup-window", type=float, default=DEFAULT_FOLLOWUP_WINDOW_S
    )
    parser.add_argument(
        "--silence-end",
        type=float,
        default=DEFAULT_SILENCE_END_S,
        help="seconds of silence that end the user's turn",
    )
    parser.add_argument(
        "--max-utterance", type=float, default=DEFAULT_MAX_UTTERANCE_S
    )
    parser.add_argument("--voice-level", type=int, default=DEFAULT_VOICE_LEVEL)
    parser.add_argument(
        "--device-id", default="growroom-sprache-1",
        help="stable non-secret identity of this ESP32 satellite",
    )
    parser.add_argument(
        "--learning-queue-dir",
        default="/home/lacasa/waveshare-va-test/learning-queue",
        help="owner-only durable JARVIS-to-Hermes queue",
    )
    parser.add_argument(
        "--briefing-speakers",
        default="/home/lacasa/Mark-LV-LaCasa/config/briefing-speakers.json",
        help="owner-only speaker file (0600): titles, todos, default speaker",
    )
    parser.add_argument(
        "--speaker",
        default="",
        help="fixed speaker key when voice identification is not active",
    )
    return parser


def _optional_voice_workflows(queue_dir):
    """Build Cannanas and learning independently so one failure degrades only it."""
    import core.esp32_voice_workflows as workflows

    diary = None
    queue = None
    try:
        diary = workflows.CannanasRoomDiaryWorkflow()
    except Exception as exc:
        _LOG.info(
            "voice workflow unavailable status=disabled workflow=cannanas error_type=%s",
            type(exc).__name__,
        )
    try:
        queue = workflows.LearningQueue(queue_dir)
    except Exception as exc:
        _LOG.info(
            "voice workflow unavailable status=disabled workflow=learning_queue error_type=%s",
            type(exc).__name__,
        )
    return diary, queue


def _optional_briefing(speakers_path: str, speaker_key: str):
    """Build the host-owned briefing lazily; a failure only disables it."""
    try:
        from core.cannanas_host import get_host
        from core.growroom_briefing import GrowroomBriefing, SpeakerBook

        book = SpeakerBook(speakers_path)
    except Exception as exc:
        _LOG.info(
            "briefing unavailable status=disabled error_type=%s", type(exc).__name__
        )
        return None
    try:
        host = get_host()
    except Exception as exc:
        _LOG.info(
            "briefing unavailable status=disabled workflow=cannanas error_type=%s",
            type(exc).__name__,
        )
        host = None
    if host is None:
        return None
    temperatures = _briefing_temperatures
    briefing = GrowroomBriefing(
        host=host, speakers=book, temperatures=temperatures
    )
    return lambda: briefing.for_speaker(speaker_key)


def _briefing_temperatures() -> dict:
    """Live room temperatures keyed by the spoken room name (Celsius)."""
    zones: dict[str, float] = {}
    for state in _ha_states(JARVIS_CONFIG_PATH):
        entity_id = str(state.get("entity_id", ""))
        if not (entity_id.startswith("sensor.zonen_")
                and entity_id.endswith("_temperatur_celsius")):
            continue
        room = entity_id[len("sensor.zonen_"):-len("_temperatur_celsius")]
        try:
            zones[room.strip().title()] = float(state.get("state"))
        except (TypeError, ValueError):
            continue
    return zones


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if bool(args.key_file) == bool(args.api_key_file):
        print("exactly one of --key-file or --api-key-file must be provided", file=sys.stderr)
        return 1
    try:
        noise_key = load_api_key(Path(args.key_file or args.api_key_file))
        gemini_keys = load_gemini_keys(
            Path(args.gemini_key_file), [Path(p) for p in args.extra_gemini_key_file]
        )
    except KeyFileError as exc:
        print(f"key file refused: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        from core.ducking import DuckingController

        ducking = DuckingController()
    except Exception as exc:
        print(f"ducking unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        diary_workflow, learning_queue = _optional_voice_workflows(
            args.learning_queue_dir
        )
    except Exception as exc:
        print(f"voice workflows unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    briefing_runner = _optional_briefing(args.briefing_speakers, args.speaker)
    satellite_bridge = JarvisBridge(
        relay_host=args.relay_host,
        relay_port=args.relay_port,
        api_key=noise_key,
        gemini_key=gemini_keys,
        model=args.model,
        tts_model=args.tts_model,
        tts_voice=args.tts_voice,
        sample_rate=args.sample_rate,
        turn_timeout=args.turn_timeout,
        capture_window=args.capture_window,
        followup_window=args.followup_window,
        silence_end=args.silence_end,
        max_utterance=args.max_utterance,
        voice_level=args.voice_level,
        device_id=args.device_id,
        diary_workflow=diary_workflow,
        learning_queue=learning_queue,
        briefing_runner=briefing_runner,
        ducking=ducking,
    )
    try:
        asyncio.run(satellite_bridge.run())
    except KeyboardInterrupt:
        print("bridge stopped")
    except Exception:  # status-only: connection-level failures stay quiet
        print("bridge failed: connection error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())