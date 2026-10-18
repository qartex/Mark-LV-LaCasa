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
from array import array
import base64
import contextlib
import enum
import functools
import inspect
import io
import logging
import sys
import time
import wave
from pathlib import Path

from esp32_api_acceptance import KeyFileError, load_api_key  # reused contract

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
DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_TTS_MODEL = "gemini-3.8-flash-tts"
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
DEFAULT_TTS_VOICE = "Kore"
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
REPLY_CHUNK_BYTES = 2048  # 1024 samples of 16-bit mono PCM per message

# TTS_END must carry a non-empty url: the firmware ignores an empty one and
# would then never enter STREAMING_RESPONSE, the only state that writes the
# streamed PCM to the speaker. With `speaker:` bound (no media player) the
# device never fetches it, so this placeholder is inert.
REPLY_STREAM_URL = "http://127.0.0.1/tts/bridge.pcm"

_BACKOFF_START_S = 1.0
_BACKOFF_FACTOR = 2
_BACKOFF_MAX_S = 30.0

_ASSISTANT_NAME = "JARVIS"
DEFAULT_SYSTEM_INSTRUCTION = (
    "Du bist %s, ein knapper Assistent an einem Lautsprecher im Growraum. "
    "Antworte immer auf Deutsch: kurz, freundlich, in hoestens ein bis zwei "
    "Saetzen ohne Listen. Wenn eine Angabe fehlt (zum Beispiel der Ort beim "
    "Wetter), rate nicht, sondern frage in einem kurzen Satz danach. Wenn dir "
    "Audio fehlt oder du etwas nicht verstehst, bitte einmal kurz wiederholen."
    % _ASSISTANT_NAME
)


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
        gemini_key: str,
        *,
        model: str = DEFAULT_MODEL,
        tts_model: str = DEFAULT_TTS_MODEL,
        tts_voice: str = DEFAULT_TTS_VOICE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        system_instruction: str | None = None,
    ):
        self._gemini_key = gemini_key
        self._model = model
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._sample_rate = int(sample_rate)
        self._system_instruction = system_instruction or DEFAULT_SYSTEM_INSTRUCTION
        self._history: list[dict] = []
        self._chat_model_pref: str | None = None
        self._tts_model_pref: str | None = None

    def reset_conversation(self) -> None:
        """Drop the remembered dialogue (a new wake-word turn starts fresh)."""
        self._history = []

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
                if not _is_retryable(exc):
                    raise
                last = exc
        assert last is not None
        raise last

    def _model_order(self, preferred: str | None, chain) -> tuple[str, ...]:
        """Try the model that last worked first, then the rest of the chain."""
        head = (preferred,) if preferred else ()
        return tuple(dict.fromkeys(head + tuple(chain)))

    def _generate(self, client, models, contents, config):
        """Walk a model chain so one exhausted model does not drop the turn."""
        last: BaseException | None = None
        for model in models:
            try:
                result = self._call_with_retries(client, model, contents, config)
            except Exception as exc:
                if _is_retryable(exc) or _is_model_incompatible(exc):
                    _LOG.info("model unavailable status=fallback model=%s", model)
                    last = exc
                    continue
                raise
            return model, result
        assert last is not None
        raise last

    def _turn_sync(self, mic_pcm: bytes) -> tuple[str, bytes]:
        from google import genai  # lazy, optional dependency

        client = genai.Client(api_key=self._gemini_key, http_options={"api_version": "v1beta"})
        types = genai.types
        chat_models = self._model_order(
            self._chat_model_pref, (self._model,) + FALLBACK_CHAT_MODELS
        )
        used_chat, answer_call = self._generate(
            client,
            chat_models,
            self._contents(mic_pcm),
            types.GenerateContentConfig(system_instruction=self._system_instruction),
        )
        self._chat_model_pref = used_chat
        answer = (answer_call.text or "").strip()
        if not answer:
            raise RuntimeError("model returned no text")
        self._remember(mic_pcm, answer)
        tts_models = self._model_order(
            self._tts_model_pref, (self._tts_model,) + FALLBACK_TTS_MODELS
        )
        used_tts, speech_call = self._generate(
            client,
            tts_models,
            answer,
            types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=self._tts_voice
                        )
                    )
                ),
            ),
        )
        self._tts_model_pref = used_tts
        part = speech_call.candidates[0].content.parts[0]
        return answer, _tts_pcm_from_response(_inline_audio_bytes(part.inline_data.data))


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
        gemini_key: str,
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
        voice_client_factory=None,
        sleep=None,
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
        )
        self.voice_client_factory = voice_client_factory or (
            lambda: EspHomeVoiceClient(self.relay_host, self.relay_port, api_key)
        )
        self._voice = None
        self._mic_chunks: list[bytes] = []
        self._capture_task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._active = False
        self._listening_announced = False
        self._stopped = False
        # Multi-turn: the device re-enters the pipeline without a wake word
        # after an answer of ours that asked something.
        self._followup_pending = False
        self._conversation_id = ""
        self._heard_speech = False
        self._last_voice_at = 0.0
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
        _LOG.info("satellite connected relay_port=%d", self.relay_port)
        try:
            await _maybe_await(voice.wait_until_disconnected())
            _LOG.info("satellite disconnected status=closed")
        finally:
            self._voice = None
            self._abort_turn()
            await _maybe_await(voice.close())

    # -- device callbacks (awaited by aioesphomeapi 46.3.0)

    async def handle_start(
        self, conversation_id, flags, audio_settings, wake_word_phrase
    ) -> int:
        """Resolve the start request to encrypted API-audio mode (port 0)."""
        self._abort_turn()  # discard any stale turn from a previous round
        self._mic_chunks = []
        self._active = True
        self._listening_announced = False
        followup = bool(self._followup_pending) and not wake_word_phrase
        self._followup_pending = False
        if not followup:
            self._reset_conversation()
        if conversation_id:
            self._conversation_id = str(conversation_id)
        window = self._followup_window if followup else self._capture_window
        self._heard_speech = False
        self._last_voice_at = 0.0
        self._capture_task = asyncio.get_running_loop().create_task(
            self._listen_until_done(window)
        )
        _LOG.info(
            "assistant start flags=%s wake_word=%s followup=%s idle_window=%s",
            int(bool(flags)),
            bool(wake_word_phrase),
            followup,
            window,
        )
        return 0

    def _reset_conversation(self) -> None:
        """Forget the running dialogue in the engine (new wake-word turn)."""
        reset = getattr(self.engine, "reset_conversation", None)
        if callable(reset):
            reset()

    async def _listen_until_done(self, idle_window: float) -> None:
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
                elif now - started >= idle_window:
                    _LOG.info("listen ended status=idle-timeout")
                    self._finish_turn_request()
                    return
                if now - started >= self._max_utterance:
                    _LOG.info("listen ended status=max-utterance")
                    self._finish_turn_request()
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
        if _pcm_level(data) >= self._voice_level:
            self._heard_speech = True
            self._last_voice_at = asyncio.get_running_loop().time()
        self._mic_chunks.append(bytes(data))
        if data2:
            self._mic_chunks.append(bytes(data2))

    async def handle_stop(self, aborted: bool) -> None:
        if aborted:
            _LOG.info("assistant aborted status=discarded")
            self._abort_turn()
        else:
            self._finish_turn_request()

    # -- turn execution

    def _finish_turn_request(self) -> None:
        capture_task = self._capture_task
        self._capture_task = None
        if capture_task is not None and capture_task is not asyncio.current_task():
            capture_task.cancel()
        self._active = False
        if self._turn_task is not None:
            return  # already running
        if not self._mic_chunks:
            voice = self._voice
            self._reset_turn()
            self._reset_conversation()
            _LOG.info("turn empty status=skipped")
            if voice is not None:
                # Leave the device listening state and end the conversation.
                self._turn_task = asyncio.get_running_loop().create_task(
                    self._send_run_end(voice)
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
                result = await asyncio.wait_for(
                    _maybe_await(self.engine.turn(mic_pcm)),
                    timeout=self._turn_timeout,
                )
            except asyncio.TimeoutError:
                _LOG.info("turn timed out status=discarded")
                await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
                return
            except Exception as exc:  # status-only: never log engine internals
                _LOG.info("turn failed status=discarded error_type=%s", type(exc).__name__)
                await voice.send_voice_assistant_event(_voice_event("RUN_END"), {})
                return
            transcript, reply_pcm = result
            if not self._stopped:
                await self._stream_reply(
                    voice,
                    reply_pcm,
                    answer=transcript,
                    continue_conversation=_wants_followup(transcript),
                )
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

    async def _stream_reply(
        self, voice, reply_pcm, *, answer: str = "", continue_conversation: bool = False
    ) -> None:
        pcm = _resample_pcm16_mono(
            bytes(reply_pcm), self._tts_source_sample_rate, self._device_sample_rate
        )
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
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if bool(args.key_file) == bool(args.api_key_file):
        print("exactly one of --key-file or --api-key-file must be provided", file=sys.stderr)
        return 1
    try:
        noise_key = load_api_key(Path(args.key_file or args.api_key_file))
        gemini_key = load_api_key(Path(args.gemini_key_file))
    except KeyFileError as exc:
        print(f"key file refused: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    satellite_bridge = JarvisBridge(
        relay_host=args.relay_host,
        relay_port=args.relay_port,
        api_key=noise_key,
        gemini_key=gemini_key,
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