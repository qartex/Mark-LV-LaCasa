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
DEFAULT_TTS_VOICE = "Kore"
DEFAULT_TURN_TIMEOUT_S = 60.0
DEFAULT_CAPTURE_WINDOW_S = 5.0
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
    "Saetzen ohne Listen. Wenn dir Audio fehlt oder du etwas nicht verstehst, "
    "bitte einmal kurz wiederholen." % _ASSISTANT_NAME
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

    async def turn(self, mic_pcm: bytes) -> tuple[str, bytes]:
        return await asyncio.to_thread(self._turn_sync, mic_pcm)

    def _turn_sync(self, mic_pcm: bytes) -> tuple[str, bytes]:
        from google import genai  # lazy, optional dependency

        client = genai.Client(api_key=self._gemini_key, http_options={"api_version": "v1beta"})
        types = genai.types
        answer_call = client.models.generate_content(
            model=self._model,
            contents=[
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
            ],
            config=types.GenerateContentConfig(system_instruction=self._system_instruction),
        )
        answer = (answer_call.text or "").strip()
        if not answer:
            raise RuntimeError("model returned no text")
        speech_call = client.models.generate_content(
            model=self._tts_model,
            contents=answer,
            config=types.GenerateContentConfig(
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
        self._capture_task = asyncio.get_running_loop().create_task(
            self._finish_after_capture_window()
        )
        _LOG.info(
            "assistant start flags=%s wake_word=%s",
            int(bool(flags)),
            bool(wake_word_phrase),
        )
        return 0

    async def _finish_after_capture_window(self) -> None:
        try:
            await asyncio.sleep(self._capture_window)
            if self._active:
                _LOG.info("capture window ended status=processing")
                self._finish_turn_request()
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
            self._reset_turn()
            _LOG.info("turn empty status=skipped")
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
                await self._stream_reply(voice, reply_pcm)
            _LOG.info("turn finished chars=%d audio_bytes=%d", len(transcript), len(reply_pcm))
        finally:
            if self._turn_task is asyncio.current_task():
                self._turn_task = None

    async def _stream_reply(self, voice, reply_pcm) -> None:
        pcm = _resample_pcm16_mono(
            bytes(reply_pcm), self._tts_source_sample_rate, self._device_sample_rate
        )
        # TTS_START starts the raw API speaker; TTS_END is what moves the device
        # into STREAMING_RESPONSE (only that state writes audio to the speaker);
        # STREAM_START/END bracket the PCM.
        await voice.send_voice_assistant_event(
            _voice_event("TTS_START"), {"text": "JARVIS"}
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