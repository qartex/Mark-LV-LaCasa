"""Voice-assistant bridge contract tests (phase-1 ESP32 satellite, Task 4).

Covers scripts/esp32_jarvis_bridge.py. Pure stdlib: neither aioesphomeapi nor
google.genai is installed in this environment (both are imported lazily by the
module), so every network and model actor is an injected fake. No physical
device, no USB, no SSH and no real key material appears anywhere; key files use
the same mode-0600 single-line contract reused from esp32_api_acceptance.
"""

import asyncio
from array import array
import base64
import contextlib
import inspect
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import esp32_api_acceptance as acceptance  # noqa: E402
import esp32_jarvis_bridge as bridge  # noqa: E402

# Obviously fake identifiers; never valid keys.
API_KEY_MARKER = "TEST-NOISE-PSK-LEAK-MARKER-77c1"
GEMINI_KEY_MARKER = "TEST-GEMINI-KEY-LEAK-MARKER-42ab"
TRANSCRIPT_MARKER = "SPOKEN-TRANSCRIPT-LEAK-TOKEN-b9d3"
CONVERSATION_ID = "conv-satellite-01"
LOOPBACK = "127.0.0.1"
REPLY_PCM = bytes(range(256)) * 8  # 2048 bytes of fake 16-bit mono PCM
# ~0.25 peak-amplitude square wave: well above the silence threshold.
LOUD_PCM = (b"\x00\x40" * 64) * 4

LOG_NAME = "esp32_jarvis_bridge"


# --------------------------------------------------------------------------- helpers

class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(repr(record.msg))


@contextlib.contextmanager
def captured_logs(name=LOG_NAME):
    logger = logging.getLogger(name)
    handler = _CaptureHandler()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


class FakeVoiceClient:
    """ESPHome-shaped aioesphomeapi voice-assistant client (fake)."""

    def __init__(self, *, fail_connect=False):
        self.ops = []
        self.handlers = None
        self.fail_connect = fail_connect
        self._disconnect = asyncio.Event()

    async def connect_and_subscribe(self, handle_start, handle_stop, handle_audio):
        if self.fail_connect:
            raise ConnectionError("fake relay unreachable")
        self.handlers = (handle_start, handle_stop, handle_audio)
        self.ops.append(("subscribe", None))

    async def send_voice_assistant_event(self, event_type, data=None):
        self.ops.append(("event", event_type, dict(data or {})))

    async def send_voice_assistant_audio(self, data):
        self.ops.append(("audio", data))

    async def wait_until_disconnected(self):
        await self._disconnect.wait()

    async def close(self):
        self.ops.append(("close", None))

    def hang_up(self):
        self._disconnect.set()

    def saw_run_end(self):
        return any(
            name == "event" and getattr(event, "name", None) == "VOICE_ASSISTANT_RUN_END"
            for name, event, *_ in self.ops
        )


def _wav_bytes(frames: bytes, *, rate: int, channels: int) -> bytes:
    """Wrap raw 16-bit frames in a minimal RIFF/WAVE container."""
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


class FakeTurnEngine:
    """Deterministic stand-in for the Gemini turn engine."""

    def __init__(self, transcript=TRANSCRIPT_MARKER, reply=REPLY_PCM):
        self.calls = []
        self.turn_contexts = []
        self.result = (transcript, reply)
        self.resets = 0
        self.gate: asyncio.Future | None = None  # optional: keep the turn pending under test control

    async def turn(self, mic_pcm):
        self.calls.append(mic_pcm)
        if self.gate is not None:
            await self.gate
        return self.result

    def set_turn_context(self, turn):
        self.turn_contexts.append(turn)

    def reset_conversation(self):
        self.resets += 1


def make_bridge(**overrides):
    engine = overrides.pop("engine", None) or FakeTurnEngine()
    base = dict(
        relay_host=LOOPBACK,
        relay_port=16053,
        api_key=API_KEY_MARKER,
        gemini_key=GEMINI_KEY_MARKER,
        engine=engine,
        tts_source_sample_rate=bridge.DEFAULT_SAMPLE_RATE,
    )
    base.update(overrides)
    return bridge.JarvisBridge(**base)


async def _session_with_handlers(bridge_obj, client):
    """Run bridge.session() as a task, yield handlers once subscribed."""
    task = asyncio.get_running_loop().create_task(bridge_obj.session(client))
    try:
        for _ in range(1000):
            if client.handlers is not None:
                break
            await asyncio.sleep(0)
        if client.handlers is None:
            raise AssertionError("bridge never subscribed voice handlers")
        return task, client.handlers
    except BaseException:
        task.cancel()
        with contextlib.suppress(Exception):
            await task
        raise


def _key_file(directory, mode=0o600, content=API_KEY_MARKER + "\n", name="api-key"):
    path = Path(directory) / name
    if path.exists():
        path.unlink()
    path.write_bytes(content.encode())
    path.chmod(mode)
    return path


async def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- reuse + laziness

class ReuseAndLazinessTests(unittest.TestCase):
    def test_load_api_key_is_reused_from_acceptance_module(self):
        self.assertIs(bridge.load_api_key, acceptance.load_api_key)
        self.assertIs(bridge.KeyFileError, acceptance.KeyFileError)

    def test_heavy_imports_are_lazy(self):
        source = (SCRIPTS / "esp32_jarvis_bridge.py").read_text()
        for pattern in (r"(?m)^(import|from)\s+aioesphomeapi", r"(?m)^(import|from)\s+google"):
            self.assertIsNone(re.search(pattern, source), pattern)
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, json; sys.path.insert(0, %r); import esp32_jarvis_bridge; "
                    "print(json.dumps({'aio': 'aioesphomeapi' in sys.modules, "
                    "'genai': 'google.genai' in sys.modules}))" % str(SCRIPTS)
                ),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr[-400:])
        flags = json.loads(probe.stdout.strip())
        self.assertIs(flags["aio"], False)
        self.assertIs(flags["genai"], False)

    def test_bridge_binds_no_udp_or_tcp_listener(self):
        source = (SCRIPTS / "esp32_jarvis_bridge.py").read_text()
        for token in ("socket", "SOCK_DGRAM", "create_datagram_endpoint"):
            self.assertNotIn(token, source)

    def test_gemini_engine_client_is_lazy_and_fails_cleanly_without_genai(self):
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        # Force the "google-genai is not installed" condition: with the SDK
        # installed this test would otherwise issue a real API call.
        with unittest.mock.patch.dict(
            sys.modules, {"google": None, "google.genai": None}
        ):
            with self.assertRaises(ImportError):
                asyncio.run(engine.turn(b"\x00\x00"))


# --------------------------------------------------------------------------- start contract

class StartContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_start_returns_exactly_int_zero(self):
        b = make_bridge()
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        self.assertTrue(inspect.iscoroutinefunction(start))
        result = await start(CONVERSATION_ID, 0, None, None)
        self.assertIs(int, type(result))
        self.assertEqual(0, result)
        client.hang_up()
        await _task

    async def test_handle_start_with_wake_word_phrase_and_flags(self):
        b = make_bridge()
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        result = await start("conv-2", 1, object(), "hey jarvis")
        self.assertEqual((0, int), (result, type(result)))
        client.hang_up()
        await _task

    async def test_first_audio_marks_pipeline_running_and_listening(self):
        b = make_bridge()
        client = FakeVoiceClient()
        task, (start, _stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(b"\x01\x00" * 32, None)
        names = [op[1].name for op in client.ops if op[0] == "event"]
        self.assertEqual(
            ["VOICE_ASSISTANT_RUN_START", "VOICE_ASSISTANT_STT_START"], names
        )
        client.hang_up()
        await task

    async def test_no_socket_is_created_during_start_and_turn(self):
        b = make_bridge()
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        with unittest.mock.patch("socket.socket", side_effect=AssertionError("socket used")) as spy:
            await start(CONVERSATION_ID, 0, None, None)
            await audio(b"\x01\x00" * 64, None)
            await stop(False)
            self.assertTrue(await _wait_for(client.saw_run_end), "turn never completed")
            await stop(True)  # post-turn abort path as well
        self.assertEqual(0, spy.call_count, "bridge must not create sockets by hand")
        client.hang_up()
        await _task

    async def test_finalized_esp32_turn_has_stable_provenance_before_engine_call(self):
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual(1, len(engine.turn_contexts))
        turn = engine.turn_contexts[0]
        self.assertEqual("esp32_pcm", turn.channel)
        self.assertEqual("growroom-sprache-1", turn.stable_device_id)
        self.assertTrue(turn.auth_session_id)
        self.assertTrue(turn.capture_id)
        client.hang_up()
        await task


# --------------------------------------------------------------------------- mic accumulation

class MicAccumulationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_accumulate_into_exactly_one_gemini_request(self):
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 80, None)
        await audio(b"\x02\x00" * 40, None)
        await audio(b"\x03\x00" * 20, b"\x04\x00" * 10)
        await stop(False)
        self.assertTrue(
            await _wait_for(lambda: engine.calls and client.saw_run_end),
            "turn never completed",
        )
        self.assertEqual(1, len(engine.calls), "one turn must be one Gemini request")
        self.assertEqual(
            (b"\x01\x00" * 80) + (b"\x02\x00" * 40) + (b"\x03\x00" * 20) + (b"\x04\x00" * 10),
            engine.calls[0],
        )
        self.assertEqual([], b._mic_chunks, "mic state must be consumed")
        client.hang_up()
        await _task

    async def test_capture_window_finishes_without_device_stop_callback(self):
        """API audio streams until the controller endpoints the utterance."""
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine, capture_window=0.02)
        client = FakeVoiceClient()
        task, (start, _stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(LOUD_PCM, None)
        self.assertTrue(
            await _wait_for(lambda: engine.calls and client.saw_run_end),
            "bounded capture did not finish the turn",
        )
        self.assertEqual([LOUD_PCM], engine.calls)
        client.hang_up()
        await task

    async def test_stop_without_start_or_chunks_skips_gemini(self):
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        task, (start, stop, audio) = await _session_with_handlers(b, client)
        await stop(False)  # never started, no chunks
        self.assertTrue(await _wait_for(lambda: not (b._turn_task and not b._turn_task.done())))
        self.assertEqual([], engine.calls)
        # A silent turn with no open conversation ends it: no audio, no TTS,
        # but the device must be released from its listening state.
        self.assertEqual(
            [
                ("subscribe", None),
                ("event", "VOICE_ASSISTANT_INTENT_END"),
                ("event", "VOICE_ASSISTANT_RUN_END"),
            ],
            [
                (op[0], op[1] if op[0] == "subscribe" else op[1].name)
                for op in client.ops
                if op[0] != "close"
            ],
        )
        client.hang_up()
        await task


# --------------------------------------------------------------------------- reply streaming

class ReplyStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_ducks_spotify_only_while_satellite_speaks(self):
        class FakeDucking:
            def __init__(self):
                self.ops = []

            def begin(self):
                self.ops.append(("begin",))
                return True

            def heartbeat(self, hold_for=0.0):
                self.ops.append(("heartbeat", hold_for))
                return True

            def end(self):
                self.ops.append(("end",))
                return True

        ducking = FakeDucking()
        b = make_bridge(ducking=ducking)
        client = FakeVoiceClient()

        await b._stream_reply(client, REPLY_PCM)

        self.assertEqual("begin", ducking.ops[0][0])
        self.assertTrue(any(op[0] == "heartbeat" for op in ducking.ops))
        self.assertEqual("end", ducking.ops[-1][0])

    async def test_reply_restores_spotify_when_satellite_stream_fails(self):
        class FakeDucking:
            def __init__(self):
                self.ops = []

            def begin(self):
                self.ops.append("begin")
                return True

            def heartbeat(self, hold_for=0.0):
                self.ops.append("heartbeat")
                return True

            def end(self):
                self.ops.append("end")
                return True

        class BrokenVoice(FakeVoiceClient):
            async def send_voice_assistant_audio(self, data):
                raise ConnectionError("satellite gone")

        ducking = FakeDucking()
        b = make_bridge(ducking=ducking)

        with self.assertRaises(ConnectionError):
            await b._stream_reply(BrokenVoice(), REPLY_PCM)
        self.assertEqual(["begin", "heartbeat", "end"], ducking.ops)

    async def test_followup_replies_end_with_a_listening_cue(self):
        b = make_bridge()
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        cue = bridge._listen_cue_pcm()
        sent = b"".join(op[1] for op in client.ops if op[0] == "audio")
        self.assertTrue(len(sent) > len(cue), "cue must follow the reply audio")
        self.assertEqual(cue, sent[-len(cue):])
        client.hang_up()
        await _task

    async def test_reply_streamed_as_raw_16bit_mono_chunks(self):
        engine = FakeTurnEngine(reply=REPLY_PCM)
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(REPLY_PCM[:64], None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        audio_ops = [op for op in client.ops if op[0] == "audio"]
        self.assertTrue(audio_ops, "reply audio must be streamed to the device")
        payloads = [op[1] for op in audio_ops]
        self.assertTrue(all(c and len(c) % 2 == 0 for c in payloads),
                        "chunks must be non-empty and 16-bit aligned")
        body = b"".join(payloads)
        cue = bridge._listen_cue_pcm()
        self.assertEqual(REPLY_PCM, body[: len(REPLY_PCM)],
                         "chunks must be byte-exact PCM, no container header")
        self.assertEqual(cue, body[len(REPLY_PCM):],
                         "follow-up replies end with the listening cue")
        self.assertNotIn(b"RIFF", body)
        self.assertNotIn(b"WAVE", body)
        self.assertTrue(all(len(c) <= bridge.REPLY_CHUNK_BYTES for c in payloads))
        client.hang_up()
        await _task

    async def test_run_event_order_matches_api_audio_stream_contract(self):
        b = make_bridge()
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(REPLY_PCM[:64], None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        timeline = [op for op in client.ops if op[0] in ("audio", "event")]
        names = [op[1].name for op in timeline if op[0] == "event"]
        self.assertEqual(
            [
                "VOICE_ASSISTANT_RUN_START",
                "VOICE_ASSISTANT_STT_START",
                "VOICE_ASSISTANT_INTENT_END",
                "VOICE_ASSISTANT_TTS_START",
                "VOICE_ASSISTANT_TTS_END",
                "VOICE_ASSISTANT_TTS_STREAM_START",
                "VOICE_ASSISTANT_TTS_STREAM_END",
                "VOICE_ASSISTANT_RUN_END",
            ],
            names,
        )
        # The firmware only enters STREAMING_RESPONSE (the state that actually
        # writes API audio to the speaker) on TTS_END with a non-empty url.
        tts_end = next(op for op in timeline if op[0] == "event" and op[1].name == "VOICE_ASSISTANT_TTS_END")
        self.assertTrue(tts_end[2].get("url"), "TTS_END must carry a url for the device state machine")
        first_audio = next(i for i, op in enumerate(timeline) if op[0] == "audio")
        self.assertEqual(6, first_audio, "listening and TTS start events must precede audio")
        self.assertTrue(all(op[0] == "event" for op in timeline[-2:]))
        codes = [op[1].value for op in timeline if op[0] == "event"]
        self.assertEqual([1, 3, 6, 7, 8, 98, 99, 2], codes)
        client.hang_up()
        await _task

    async def test_reply_stream_is_paced_to_the_device_buffer(self):
        b = make_bridge()
        client = FakeVoiceClient()
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        two_seconds = b"\x00\x00" * (bridge.DEFAULT_SAMPLE_RATE * 2)
        with unittest.mock.patch.object(bridge.asyncio, "sleep", fake_sleep):
            await b._stream_reply(client, two_seconds)
        self.assertTrue(delays, "long TTS must not be burst into the fixed speaker buffer")
        self.assertTrue(all(delay > 0 for delay in delays))


class MultiTurnConversationTests(unittest.IsolatedAsyncioTestCase):
    """A question from JARVIS must keep the microphone open for the answer."""

    async def test_morning_greeting_speaks_the_host_briefing(self):
        """A greeting must not depend on the model calling the tool."""

        class BriefingEngine(FakeTurnEngine):
            def __init__(self, *args, **kwargs):
                super().__init__(transcript="Guten Morgen!", *args, **kwargs)
                self.spoken = []

            async def speak(self, text):
                self.spoken.append(text)
                return REPLY_PCM

        engine = BriefingEngine()
        b = make_bridge(
            engine=engine,
            briefing_runner=lambda key: "Guten Morgen, Head of Grow. Madrid: Blüte Tag 15.",
        )
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual(
            ["Guten Morgen, Head of Grow. Madrid: Blüte Tag 15."], engine.spoken
        )
        intent = next(
            op for op in client.ops
            if op[0] == "event" and op[1].name == "VOICE_ASSISTANT_INTENT_END"
        )
        self.assertIn("Head of Grow", intent[2]["speech"])
        client.hang_up()
        await _task

    async def test_ordinary_question_keeps_the_model_answer(self):
        class BriefingEngine(FakeTurnEngine):
            def __init__(self, *args, **kwargs):
                super().__init__(transcript="Madrid hat 20 Grad.", *args, **kwargs)
                self.spoken = []

            async def speak(self, text):
                self.spoken.append(text)
                return REPLY_PCM

        engine = BriefingEngine()
        b = make_bridge(
            engine=engine, briefing_runner=lambda key: "Guten Morgen, Head of Grow."
        )
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual([], engine.spoken)
        client.hang_up()
        await _task


    async def test_voice_identification_sets_the_speaker_for_the_turn(self):
        class IdentifyingEngine(FakeTurnEngine):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.speakers = []

            def set_speaker(self, key):
                self.speakers.append(key)

        class Identifier:
            def identify(self, pcm):
                return "bjoern"

        engine = IdentifyingEngine()
        b = make_bridge(engine=engine, speaker_identifier=Identifier(),
                        default_speaker="lukas")
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual("bjoern", engine.speakers[-1])
        client.hang_up()
        await _task


    async def test_question_answer_keeps_the_conversation_open(self):
        engine = FakeTurnEngine(transcript="Welcher Ort?")
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(b"\x01\x00" * 64, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        intent_end = [
            op for op in client.ops
            if op[0] == "event" and op[1].name == "VOICE_ASSISTANT_INTENT_END"
        ]
        self.assertTrue(intent_end, "the device needs INTENT_END to know it may listen on")
        self.assertEqual("1", intent_end[0][2]["continue_conversation"])
        self.assertEqual("Welcher Ort?", intent_end[0][2]["speech"])
        self.assertEqual(CONVERSATION_ID, intent_end[0][2]["conversation_id"])
        client.hang_up()
        await _task

    async def test_statement_answer_keeps_listening_too(self):
        """Continuous conversation: the mic stays open after every answer.

        The turn only ends when the user stays silent for the idle window, so a
        location or a follow-up word never needs the wake word again.
        """
        engine = FakeTurnEngine(transcript="Mir geht es gut, danke.")
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        intent_end = next(
            op for op in client.ops
            if op[0] == "event" and op[1].name == "VOICE_ASSISTANT_INTENT_END"
        )
        self.assertEqual("1", intent_end[2]["continue_conversation"])
        client.hang_up()
        await _task

    async def test_followup_start_gets_a_persistent_window(self):
        engine = FakeTurnEngine(transcript="Welcher Ort?")
        b = make_bridge(engine=engine, capture_window=0.05, followup_window=0.3)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        with captured_logs() as logs:
            await start(CONVERSATION_ID, 1, None, "hey jarvis")
            self.assertTrue(await _wait_for(lambda: any("idle_window=0.05" in l for l in logs)))
            await audio(LOUD_PCM, None)
            await stop(False)
            self.assertTrue(await _wait_for(client.saw_run_end))
            await start(CONVERSATION_ID, 1, None, "")
            self.assertTrue(
                await _wait_for(lambda: any("idle_window=persistent" in l for l in logs)),
                "an active conversation must wait until the user speaks",
            )
        client.hang_up()
        await _task

    async def test_long_speech_must_not_be_cut_off(self):
        b = make_bridge(capture_window=0.3, followup_window=0.3, silence_end=0.5)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        # Keep talking for more than twice the idle window: only silence ends a turn.
        spoke_until = time.monotonic() + 0.8
        while time.monotonic() < spoke_until:
            await audio(LOUD_PCM, None)
            await asyncio.sleep(0.1)
        self.assertFalse(client.saw_run_end(), "speech must keep the microphone open")
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        client.hang_up()
        await _task

    async def test_turn_ends_after_a_short_silence_tail(self):
        b = make_bridge(capture_window=5.0, silence_end=0.2)
        engine = FakeTurnEngine()
        b.engine = engine
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        with captured_logs() as logs:
            await start(CONVERSATION_ID, 1, None, "hey jarvis")
            await audio(LOUD_PCM, None)
            self.assertTrue(
                await _wait_for(lambda: any("silence" in line for line in logs), 3.0),
                "the turn must end once the user stopped speaking",
            )
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual(1, len(engine.calls))
        client.hang_up()
        await _task

    async def test_silent_followup_turn_keeps_the_conversation_open(self):
        engine = FakeTurnEngine(transcript="Welcher Ort?")
        b = make_bridge(engine=engine, capture_window=0.05, followup_window=0.05)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(b"\x01\x00" * 64, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        resets_before = engine.resets
        client.ops.clear()
        await start(CONVERSATION_ID, 1, None, "")
        await stop(False)  # user said nothing
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual(0, len(engine.calls[1:]), "silence must not call the model")
        self.assertEqual(resets_before, engine.resets)
        intents = [op for op in client.ops
                   if op[0] == "event" and op[1].name == "VOICE_ASSISTANT_INTENT_END"]
        self.assertEqual(["1"], [op[2]["continue_conversation"] for op in intents])
        client.hang_up()
        await _task

    async def test_automatic_followup_with_repeated_wake_phrase_waits_through_room_noise(self):
        engine = FakeTurnEngine(transcript="Was läuft gerade?")
        b = make_bridge(engine=engine, capture_window=0.03, followup_window=0.08)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual(1, len(engine.calls))

        client.ops.clear()
        with captured_logs() as logs:
            # ESPHome repeats the original wake-word phrase on its automatic
            # continue-conversation start, so pending state is authoritative.
            await start(CONVERSATION_ID, 1, None, "hey jarvis")
            await audio(b"\x01\x00" * 128, None)  # buffered room noise, no speech
            await asyncio.sleep(0.2)

        self.assertTrue(any("followup=True" in line for line in logs))
        self.assertTrue(b._active)
        self.assertFalse(client.saw_run_end())
        self.assertEqual(1, len(engine.calls), "room noise must not repeat the answer")
        client.hang_up()
        await _task

    async def test_followup_waits_past_idle_window_until_the_user_speaks(self):
        """Once woken, silence must not end the Mark-LV-style conversation."""
        engine = FakeTurnEngine(transcript="Welchen Raum?")
        b = make_bridge(engine=engine, capture_window=0.05, followup_window=0.05)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        self.assertEqual(1, len(engine.calls))

        client.ops.clear()
        await start(CONVERSATION_ID, 1, None, "")
        await asyncio.sleep(0.15)
        self.assertTrue(b._active, "an active conversation must wait indefinitely")
        self.assertFalse(client.saw_run_end())
        self.assertEqual(1, len(engine.calls), "silence must never call the model")
        client.hang_up()
        await _task

    async def test_silent_preroll_is_bounded_while_followup_waits(self):
        b = make_bridge(followup_window=0.02)
        client = FakeVoiceClient()
        _task, (start, _stop, audio) = await _session_with_handlers(b, client)
        b._followup_pending = True
        await start(CONVERSATION_ID, 1, None, "")
        quiet = b"\x01\x00" * 64
        for _ in range(300):
            await audio(quiet, None)
        self.assertLessEqual(
            sum(map(len, b._mic_chunks)),
            bridge.DEFAULT_SAMPLE_RATE * 2,
            "waiting forever must not grow the microphone buffer forever",
        )
        client.hang_up()
        await _task

    async def test_spoken_mute_request_returns_to_wake_word_only_mode(self):
        class MutingEngine(FakeTurnEngine):
            def consume_mute_request(self):
                return True

        engine = MutingEngine(transcript="Mikrofon ist stumm.")
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        await audio(LOUD_PCM, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        intent = next(
            op for op in client.ops
            if op[0] == "event" and op[1].name == "VOICE_ASSISTANT_INTENT_END"
        )
        self.assertEqual("0", intent[2]["continue_conversation"])
        self.assertFalse(b._followup_pending)
        client.hang_up()
        await _task

    async def test_wake_word_start_resets_the_conversation_memory(self):
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, _stop, _audio) = await _session_with_handlers(b, client)
        self.assertEqual(0, engine.resets)
        await start(CONVERSATION_ID, 1, None, "hey jarvis")
        self.assertEqual(1, engine.resets)
        client.hang_up()
        await _task


# --------------------------------------------------------------------------- engine failure / timeout

class TurnFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_failure_discards_state_and_streams_nothing(self):
        class BrokenEngine:
            async def turn(self, mic_pcm):
                raise RuntimeError("model call failed")

        b = make_bridge(engine=BrokenEngine())
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 16, None)
        await stop(False)
        self.assertTrue(
            await _wait_for(lambda: b._turn_task is None),
            "failed turn must be discarded",
        )
        self.assertEqual(
            [
                "VOICE_ASSISTANT_RUN_START",
                "VOICE_ASSISTANT_STT_START",
                "VOICE_ASSISTANT_RUN_END",
            ],
            [op[1].name for op in client.ops if op[0] == "event"],
        )
        self.assertEqual([], [op for op in client.ops if op[0] == "audio"])
        self.assertEqual([], b._mic_chunks)
        client.hang_up()
        await _task

    async def test_turn_timeout_discards_state(self):
        engine = FakeTurnEngine()
        bridge_obj = make_bridge(engine=engine)
        bridge_obj._turn_timeout = 0.02
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(bridge_obj, client)
        engine.gate = asyncio.get_running_loop().create_future()  # keep the turn pending
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 16, None)
        await stop(False)
        self.assertTrue(
            await _wait_for(lambda: bridge_obj._turn_task is None),
            "timed-out turn must be discarded",
        )
        self.assertEqual(1, len(engine.calls), "engine was still invoked once")
        self.assertEqual(
            [
                "VOICE_ASSISTANT_RUN_START",
                "VOICE_ASSISTANT_STT_START",
                "VOICE_ASSISTANT_RUN_END",
            ],
            [op[1].name for op in client.ops if op[0] == "event"],
        )
        self.assertEqual([], [op for op in client.ops if op[0] == "audio"])
        client.hang_up()
        await _task


# --------------------------------------------------------------------------- abort

class AbortTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_mid_turn_discards_pending_state(self):
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine)
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        engine.gate = asyncio.get_running_loop().create_future()
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 32, None)
        await stop(False)  # turn scheduled and pending on the engine gate
        self.assertTrue(await _wait_for(lambda: engine.calls), "engine never invoked")
        await stop(True)  # aborted mid-turn
        self.assertTrue(
            await _wait_for(lambda: b._turn_task is None),
            "turn task must be dropped on abort",
        )
        await asyncio.sleep(0)  # allow any (wrong) streaming to surface
        self.assertEqual(
            ["VOICE_ASSISTANT_RUN_START", "VOICE_ASSISTANT_STT_START"],
            [op[1].name for op in client.ops if op[0] == "event"],
        )
        self.assertEqual([], [op for op in client.ops if op[0] == "audio"])
        self.assertEqual([], b._mic_chunks)
        # A fresh start after an abort works again.
        engine.gate = None
        await start("conv-fresh", 0, None, None)
        await audio(b"\x02\x00" * 8, None)
        await stop(False)
        self.assertTrue(await _wait_for(client.saw_run_end))
        client.hang_up()
        await _task

    async def test_abort_without_active_turn_is_a_noop(self):
        b = make_bridge()
        client = FakeVoiceClient()
        _task, (start, stop, audio) = await _session_with_handlers(b, client)
        noop = b._turn_task is None
        await stop(True)
        self.assertTrue(noop)
        self.assertEqual(b"", b"".join(b._mic_chunks))
        client.hang_up()
        await _task



class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_reconnects_with_bounded_backoff_and_resets_after_success(self):
        delays = []
        clean = FakeVoiceClient()
        clean.hang_up()  # connected session ends immediately -> backoff reset
        attempts = iter(
            [
                FakeVoiceClient(fail_connect=True),
                FakeVoiceClient(fail_connect=True),
                clean,
                FakeVoiceClient(fail_connect=True),
            ]
        )

        async def recorder(delay):
            delays.append(delay)
            if len(delays) >= 3:
                bridge_obj.stop()
            return await asyncio.sleep(0)

        bridge_obj = make_bridge(voice_client_factory=lambda: next(attempts), sleep=recorder)
        await asyncio.wait_for(bridge_obj.run(), timeout=10)
        self.assertEqual(
            [bridge._BACKOFF_START_S, bridge._BACKOFF_START_S * 2, bridge._BACKOFF_START_S],
            delays,
            "backoff must be bounded and reset after a clean session",
        )
        self.assertTrue(all(d <= bridge._BACKOFF_MAX_S for d in delays))

    async def test_default_voice_client_is_a_lazy_facade(self):
        b = make_bridge()
        voice = b.voice_client_factory()
        self.assertIsInstance(voice, bridge.EspHomeVoiceClient)
        self.assertEqual(LOOPBACK, voice.host)
        self.assertEqual(16053, voice.port)
        for meth in ("connect_and_subscribe", "send_voice_assistant_event",
                     "send_voice_assistant_audio", "wait_until_disconnected", "close"):
            self.assertTrue(callable(getattr(voice, meth, None)), meth)
        self.assertIsNone(re.search(
            r"(?m)^(import|from)\s+aioesphomeapi",
            (SCRIPTS / "esp32_jarvis_bridge.py").read_text(),
        ))

    async def test_subscribe_call_uses_keyword_callbacks_per_46_3_0(self):
        """46.3.0 subscribe_voice_assistant is keyword-only; regression for the
        live TypeError seen on the real device."""
        captured = {}

        class SpyClient:
            def subscribe_voice_assistant(self, *args, **kwargs):
                captured["args"], captured["kwargs"] = args, kwargs

            def unsubscribe(self):
                pass

        voice = bridge.EspHomeVoiceClient("127.0.0.1", 16053, "psk")
        async def h1(*a): return 0
        async def h2(*a): return None
        async def h3(*a): return None
        await voice.connect_and_subscribe(h1, h2, h3, _client_override=SpyClient())
        self.assertEqual((), captured["args"])
        self.assertIs(h1, captured["kwargs"]["handle_start"])
        self.assertIs(h2, captured["kwargs"]["handle_stop"])
        self.assertIs(h3, captured["kwargs"]["handle_audio"])

    async def test_failed_turn_and_reconnect_never_log_secrets(self):
        seen_delays = []

        class SecretLeakEngine:
            async def turn(self, mic_pcm):
                raise RuntimeError("boom key=" + API_KEY_MARKER)

        async def recorder(delay):
            seen_delays.append(delay)
            bridge_obj.stop()
            return await asyncio.sleep(0)

        bridge_obj = make_bridge(engine=SecretLeakEngine(), sleep=recorder)
        client = FakeVoiceClient()
        task, (start, stop, audio) = await _session_with_handlers(bridge_obj, client)
        with captured_logs() as lines:
            await start(CONVERSATION_ID, 0, None, None)
            await audio(b"\x01\x00" * 8, None)
            await stop(False)
            await _wait_for(lambda: bridge_obj._turn_task is None)
        blob = "\n".join(lines)
        for secret in (API_KEY_MARKER, GEMINI_KEY_MARKER, TRANSCRIPT_MARKER):
            self.assertNotIn(secret, blob)
        self.assertTrue(lines, "bridge must produce status logs")
        client.hang_up()
        await task


class OptionalWorkflowTests(unittest.TestCase):
    def test_learning_queue_failure_keeps_cannanas_workflow(self):
        import core.esp32_voice_workflows as workflows

        diary_sentinel = object()
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                workflows, "CannanasRoomDiaryWorkflow", lambda: diary_sentinel
            ), unittest.mock.patch.object(
                workflows, "LearningQueue", side_effect=PermissionError("denied")
            ):
                diary, queue = bridge._optional_voice_workflows(tmp)
        self.assertIs(diary_sentinel, diary)
        self.assertIsNone(queue)

    def test_cannanas_failure_keeps_learning_queue(self):
        import core.esp32_voice_workflows as workflows

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                workflows,
                "CannanasRoomDiaryWorkflow",
                side_effect=PermissionError("denied"),
            ):
                diary, queue = bridge._optional_voice_workflows(tmp)
        self.assertIsNone(diary)
        self.assertIsInstance(queue, workflows.LearningQueue)


class SessionIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_gets_a_fresh_auth_session_and_capture_epoch(self):
        engine = FakeTurnEngine()
        b = make_bridge(engine=engine)
        for _round in range(2):
            client = FakeVoiceClient()
            task, (start, stop, audio) = await _session_with_handlers(b, client)
            await start(CONVERSATION_ID, 1, None, "hey jarvis")
            await audio(b"\x01\x00" * 32, None)
            await stop(False)
            await _wait_for(lambda: b._turn_task is None)
            client.hang_up()
            await task
        first, second = engine.turn_contexts[0], engine.turn_contexts[1]
        self.assertNotEqual(
            first.auth_session_id,
            second.auth_session_id,
            "a reconnect must not reuse the previous connection's session identity",
        )
        self.assertNotEqual(
            first.capture_id.split(":")[0],
            second.capture_id.split(":")[0],
            "capture identity must be fenced per connection epoch",
        )

    async def test_disconnect_cancels_the_devices_pending_confirmation(self):
        cancelled = []

        class PendingEngine(FakeTurnEngine):
            def cancel_pending_confirmation(self):
                cancelled.append(True)
                return True

        b = make_bridge(engine=PendingEngine())
        client = FakeVoiceClient()
        task, _handlers = await _session_with_handlers(b, client)
        client.hang_up()
        await task
        self.assertEqual([True], cancelled)


class CompletionAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def _completed_queue(self, tmpdir):
        import os as _os

        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(tmpdir)
        turn = SimpleNamespace(
            stable_device_id="esp32-growroom-1",
            capture_id="capture-1",
            user_turn_id=1,
        )
        queue.submit({"request": "Lerne CO2."}, turn)
        job = next(queue.pending.glob("*.json"))
        _os.replace(job, queue.dispatched / job.name)
        queue.write_completion(job.stem, capability="CO2-Wert", status="completed")
        return queue, job

    def test_engine_claims_the_announcement_before_speaking(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, job = self._completed_queue(tmp)
            engine = bridge.GeminiTurnEngine(
                GEMINI_KEY_MARKER, learning_queue=queue
            )
            answer = engine.attach_completion_announcement("Es sind 24 Grad.")
            self.assertIn("So, jetzt kannst du die Funktion CO2-Wert nutzen.", answer)
            self.assertEqual([], list(queue.completed.glob("*.json")))
            self.assertEqual(1, len(list(queue.claimed.glob("*.json"))))
            engine.confirm_announcement_spoken()
            self.assertTrue((queue.announced / job.name).exists())

    async def test_failed_audio_stream_releases_the_announcement_for_retry(self):
        confirmed, released = [], []

        class AnnouncingEngine(FakeTurnEngine):
            def confirm_announcement_spoken(self):
                confirmed.append(True)

            def release_announcement(self):
                released.append(True)

        class FailingAudioClient(FakeVoiceClient):
            async def send_voice_assistant_audio(self, data):
                raise RuntimeError("link down")

        b = make_bridge(engine=AnnouncingEngine())
        client = FailingAudioClient()
        task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 32, None)
        await stop(False)
        await _wait_for(lambda: b._turn_task is None)
        self.assertEqual([], confirmed)
        self.assertEqual([True], released, "an unspoken announcement must be retried")
        client.hang_up()
        await task

    async def test_turn_failure_after_claim_releases_the_announcement(self):
        released = []

        class FailingEngine(FakeTurnEngine):
            async def turn(self, mic_pcm):
                raise RuntimeError("TTS quota exhausted")

            def release_announcement(self):
                released.append(True)

        b = make_bridge(engine=FailingEngine())
        client = FakeVoiceClient()
        task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 32, None)
        await stop(False)
        await _wait_for(lambda: b._turn_task is None)
        self.assertEqual([True], released)
        client.hang_up()
        await task

    async def test_spoken_announcement_is_acknowledged_once(self):
        confirmed, released = [], []

        class AnnouncingEngine(FakeTurnEngine):
            def confirm_announcement_spoken(self):
                confirmed.append(True)

            def release_announcement(self):
                released.append(True)

        b = make_bridge(engine=AnnouncingEngine())
        client = FakeVoiceClient()
        task, (start, stop, audio) = await _session_with_handlers(b, client)
        await start(CONVERSATION_ID, 0, None, None)
        await audio(b"\x01\x00" * 32, None)
        await stop(False)
        await _wait_for(lambda: b._turn_task is None)
        self.assertEqual([True], confirmed)
        self.assertEqual([], released)
        client.hang_up()
        await task


# --------------------------------------------------------------------------- CLI + logging

class CliAndLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_argument_parser_contract(self):
        parser = bridge.build_arg_parser()
        argv = [
            "--relay-host", LOOPBACK,
            "--relay-port", "16053",
            "--key-file", "a",
            "--gemini-key-file", "b",
            "--model", "m",
            "--tts-model", "tts",
            "--tts-voice", "Kore",
            "--sample-rate", "24000",
            "--turn-timeout", "30",
        ]
        args = parser.parse_args(argv)
        self.assertEqual(LOOPBACK, args.relay_host)
        self.assertEqual(16053, args.relay_port)
        self.assertEqual("a", args.key_file)
        self.assertEqual("b", args.gemini_key_file)
        self.assertEqual(24000, args.sample_rate)
        self.assertEqual(30.0, args.turn_timeout)
        defaults = parser.parse_args(["--relay-host", "h", "--key-file", "k", "--gemini-key-file", "g"])
        self.assertEqual("gemini-3.1-flash-lite-preview", bridge.DEFAULT_MODEL)
        self.assertEqual(bridge.DEFAULT_MODEL, defaults.model)
        self.assertEqual("gemini-3.8-flash-lite-tts", bridge.DEFAULT_TTS_MODEL)
        self.assertEqual(bridge.DEFAULT_TTS_MODEL, defaults.tts_model)
        self.assertEqual(bridge.DEFAULT_TTS_VOICE, defaults.tts_voice)
        self.assertEqual(bridge.DEFAULT_SAMPLE_RATE, defaults.sample_rate)

    def test_api_key_file_alias_is_acceptable(self):
        args = bridge.build_arg_parser().parse_args(
            ["--relay-host", "h", "--api-key-file", "k2", "--gemini-key-file", "g"]
        )
        self.assertEqual("k2", args.api_key_file)
        self.assertIsNone(args.key_file)

    def test_system_instruction_is_a_short_german_jarvis_persona_without_tools(self):
        instruction = bridge.DEFAULT_SYSTEM_INSTRUCTION
        self.assertIn("JARVIS", instruction)
        lowered = instruction.lower()
        self.assertIn("deutsch", lowered)
        self.assertIn("kurz", lowered)
        self.assertNotIn("tool", lowered)
        self.assertIn("tagebuch", lowered)
        self.assertIn("zone", lowered)
        self.assertIn("lernen", lowered)

    def test_spotify_function_call_runs_the_narrow_controller(self):
        calls = []
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER,
            spotify_runner=lambda args: calls.append(args) or "Spotify gestartet.",
        )
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="spotify_control",
                    args={"action": "play", "query": "Queen", "kind": "artist"},
                ))
            ]))],
        )

        self.assertEqual("Spotify gestartet.", engine._answer_from_response(response))
        self.assertEqual(
            [{"action": "play", "query": "Queen", "kind": "artist"}], calls
        )

    def test_temperature_function_runs_the_room_runner(self):
        calls = []
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER,
            temperature_runner=lambda args: calls.append(args) or "Madrid 26,0 Grad.",
        )
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="growroom_temperature",
                    args={"room": "Madrid"},
                ))
            ]))],
        )
        self.assertEqual("Madrid 26,0 Grad.", engine._answer_from_response(response))
        self.assertEqual([{"room": "Madrid"}], calls)

    def test_microphone_control_function_requests_host_owned_mute(self):
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="microphone_control", args={"action": "mute"}
                ))
            ]))],
        )
        try:
            answer = engine._answer_from_response(response)
        except RuntimeError as exc:
            self.fail(f"microphone_control must be a supported host action: {exc}")
        self.assertEqual("Mikrofon ist stumm.", answer)
        self.assertTrue(engine.consume_mute_request())
        self.assertFalse(engine.consume_mute_request(), "the request is one-shot")

    def test_briefing_function_returns_the_host_composed_text(self):
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER,
            briefing_runner=lambda _key: "Guten Morgen, Head of Grow. Madrid: Blüte Tag 45.",
        )
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="growroom_briefing", args={}
                ))
            ]))],
        )
        self.assertEqual(
            "Guten Morgen, Head of Grow. Madrid: Blüte Tag 45.",
            engine._answer_from_response(response),
        )

    def test_briefing_is_offered_as_a_tool_only_when_configured(self):
        class FakeTypes:
            class FunctionDeclaration:
                def __init__(self, **kwargs):
                    self.name = kwargs["name"]

            class Tool:
                def __init__(self, function_declarations):
                    self.function_declarations = function_declarations

        without = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        names = [d.name for d in without._chat_tools(FakeTypes)[0].function_declarations]
        self.assertNotIn("growroom_briefing", names)

        with_runner = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER, briefing_runner=lambda _key: "x")
        names = [
            d.name
            for d in with_runner._chat_tools(FakeTypes)[0].function_declarations
        ]
        self.assertIn("growroom_briefing", names)

    def test_briefing_receives_the_identified_speaker(self):
        seen = []
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER,
            briefing_runner=lambda key: seen.append(key) or "Guten Morgen.",
        )
        engine.set_speaker("Bjoern")
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="growroom_briefing", args={}
                ))
            ]))],
        )
        engine._answer_from_response(response)
        self.assertEqual(["bjoern"], seen)

    def test_cannanas_tool_is_offered_with_the_closed_action_enum(self):
        class FakeTypes:
            class FunctionDeclaration:
                def __init__(self, **kwargs):
                    self.name = kwargs["name"]
                    self.parameters_json_schema = kwargs.get("parameters_json_schema")

            class Tool:
                def __init__(self, function_declarations):
                    self.function_declarations = function_declarations

        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER, cannanas_runner=lambda a: "ok")
        decls = {
            d.name: d
            for d in engine._chat_tools(FakeTypes)[0].function_declarations
        }
        self.assertIn("cannanas", decls)
        enum = decls["cannanas"].parameters_json_schema["properties"]["action"]["enum"]
        self.assertIn("prepare_zone_update", enum)
        self.assertIn("prepare_harvest_create", enum)
        self.assertNotIn("confirm_diary_create", enum)

    def test_cannanas_runner_returns_the_host_summary(self):
        runner = bridge._optional_cannanas_runner()
        self.assertTrue(callable(runner))
        # An unknown action is refused by the closed enum, never executed.
        answer = runner({"action": "confirm_diary_create"})
        self.assertIsInstance(answer, str)
        self.assertTrue(answer.strip())

    def test_cannanas_tool_is_absent_without_a_runner(self):
        class FakeTypes:
            class FunctionDeclaration:
                def __init__(self, **kwargs):
                    self.name = kwargs["name"]

            class Tool:
                def __init__(self, function_declarations):
                    self.function_declarations = function_declarations

        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        names = [d.name for d in engine._chat_tools(FakeTypes)[0].function_declarations]
        self.assertNotIn("cannanas", names)

    def test_cannanas_function_call_runs_the_host_runner(self):
        seen = []
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER,
            cannanas_runner=lambda params: seen.append(params) or "Vorschau gelesen.",
        )
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="cannanas", args={"action": "prepare_zone_update"}
                ))
            ]))],
        )
        self.assertEqual("Vorschau gelesen.", engine._answer_from_response(response))
        self.assertEqual([{"action": "prepare_zone_update"}], seen)

    def test_new_wake_word_conversation_clears_an_unspoken_mute_request(self):
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="microphone_control", args={"action": "mute"}
                ))
            ]))],
        )
        engine._answer_from_response(response)
        engine.reset_conversation()
        self.assertFalse(engine.consume_mute_request())

    def test_diary_prepare_function_runs_with_host_owned_turn_context(self):
        calls = []
        workflow = SimpleNamespace(
            prepare=lambda args, turn: calls.append((args, turn)) or "Vorschau. Sag ja.",
            take_completion_announcement=lambda: None,
        )
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER, diary_workflow=workflow,
        )
        turn = SimpleNamespace(stable_device_id="esp32-1", user_turn_id=4)
        engine.set_turn_context(turn)
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="cannanas_diary_prepare",
                    args={"room": "Helsinki", "text": "Blätter kontrolliert."},
                ))
            ]))],
        )
        self.assertEqual("Vorschau. Sag ja.", engine._answer_from_response(response))
        self.assertEqual(
            [({"room": "Helsinki", "text": "Blätter kontrolliert."}, turn)], calls
        )

    def test_learning_function_queues_requirement_with_turn_context(self):
        calls = []
        queue = SimpleNamespace(
            submit=lambda args, turn: calls.append((args, turn)) or "Muss ich lernen.",
            take_completion_announcement=lambda: None,
        )
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER, learning_queue=queue)
        turn = SimpleNamespace(stable_device_id="esp32-1", user_turn_id=5)
        engine.set_turn_context(turn)
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="request_capability_learning",
                    args={"request": "Lerne den CO2-Wert anzusagen."},
                ))
            ]))],
        )
        self.assertEqual("Muss ich lernen.", engine._answer_from_response(response))
        self.assertEqual(
            [({"request": "Lerne den CO2-Wert anzusagen."}, turn)], calls
        )

    def test_pending_diary_confirmation_is_decided_by_workflow(self):
        turn = SimpleNamespace(stable_device_id="esp32-1", user_turn_id=6)
        workflow = SimpleNamespace(
            has_pending=lambda candidate: candidate is turn,
            evaluate_confirmation=lambda candidate, text: (
                "Erledigt — Tagebucheintrag gespeichert."
                if candidate is turn and text == "ja" else None
            ),
        )
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER, diary_workflow=workflow,
        )
        engine.set_turn_context(turn)
        self.assertTrue(engine.has_pending_confirmation())
        self.assertEqual(
            "Erledigt — Tagebucheintrag gespeichert.",
            engine.evaluate_pending_confirmation("ja"),
        )

    def test_completed_learning_job_is_prefixed_once(self):
        announcements = iter(
            [("So, jetzt kannst du die Funktion CO2 nutzen.", "job-1"), None]
        )
        queue = SimpleNamespace(
            claim_completion_announcement=lambda: next(announcements),
            ack_completion_announcement=lambda job_id: True,
        )
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER, learning_queue=queue)
        self.assertEqual(
            "So, jetzt kannst du die Funktion CO2 nutzen. Aktuelle Antwort.",
            engine.attach_completion_announcement("Aktuelle Antwort."),
        )
        self.assertEqual(
            "Nächste Antwort.", engine.attach_completion_announcement("Nächste Antwort.")
        )
        self.assertTrue(engine.confirm_announcement_spoken())

    def test_default_temperature_runner_reports_measured_zone(self):
        fake = [
            {"entity_id": "sensor.zonen_madrid_temperatur_celsius", "state": "26.0"},
            {"entity_id": "sensor.zonen_helsinki_temperatur_celsius", "state": "24.2"},
            {"entity_id": "sensor.zonen_halle_temperatur_celsius", "state": "13.6"},
        ]
        with unittest.mock.patch.object(bridge, "_ha_states", return_value=fake):
            self.assertEqual(
                "Raum Madrid hat aktuell 26.0 Grad.",
                bridge._default_temperature_runner({"room": "Madrid"}),
            )
            self.assertEqual(
                "Raum Helsinki hat aktuell 24.2 Grad.",
                bridge._default_temperature_runner({"room": "Helsinki"}),
            )
            answer = bridge._default_temperature_runner({"room": "Tokio"})
            self.assertIn("keinen Temperatursensor", answer)
            self.assertIn("helsinki", answer)
            self.assertIn("madrid", answer)

    def test_default_temperature_runner_handles_unavailable_state(self):
        fake = [
            {"entity_id": "sensor.zonen_tokio_temperatur_celsius", "state": "unavailable"},
        ]
        with unittest.mock.patch.object(bridge, "_ha_states", return_value=fake):
            self.assertIn("keinen Temperaturwert",
                bridge._default_temperature_runner({"room": "Tokio"}))

    def test_unknown_function_call_is_never_executed(self):
        calls = []
        engine = bridge.GeminiTurnEngine(
            GEMINI_KEY_MARKER, spotify_runner=lambda args: calls.append(args) or "no"
        )
        response = SimpleNamespace(
            text=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(
                    name="shell", args={"command": "id"}
                ))
            ]))],
        )

        with self.assertRaisesRegex(RuntimeError, "unsupported model function"):
            engine._answer_from_response(response)
        self.assertEqual([], calls)

    def test_pcm_mime_uses_declared_device_rate(self):
        self.assertEqual("audio/pcm;rate=24000", bridge._pcm_mime(24000))
        self.assertEqual("audio/pcm;rate=16000", bridge._pcm_mime(16000))
        self.assertEqual(16000, bridge.DEFAULT_SAMPLE_RATE)

    def test_default_tts_is_a_calm_male_jarvis_voice(self):
        self.assertEqual("Charon", bridge.DEFAULT_TTS_VOICE)

    def test_tts_speaks_only_the_answer_with_german_style_instruction(self):
        answer = "Auf Spotify läuft gerade ein Titel."
        style, contents = bridge._tts_texts(answer)
        self.assertEqual("Auf Spotify läuft gerade ein Titel.", contents)
        self.assertIn("Deutsch", style)
        self.assertIn("Männerstimme", style)
        self.assertNotIn(answer, style)

    def test_tts_pcm_resamples_from_24khz_to_device_16khz(self):
        source = array("h", [i * 1000 for i in range(24)]).tobytes()
        converted = bridge._resample_pcm16_mono(source, 24000, 16000)
        samples = array("h")
        samples.frombytes(converted)
        self.assertEqual(16, len(samples))
        self.assertEqual([0, 1500, 3000, 4500], list(samples[:4]))

    def test_inline_audio_accepts_sdk_raw_bytes_and_base64_text(self):
        raw = b"\x01\x02\x03\x04"
        self.assertEqual(raw, bridge._inline_audio_bytes(raw))
        encoded = base64.b64encode(raw).decode("ascii")
        self.assertEqual(raw, bridge._inline_audio_bytes(encoded))

    def test_tts_wav_container_is_decoded_to_raw_pcm(self):
        """gemini-3.x TTS answers with a WAV container, not bare L16 PCM."""
        frames = array("h", [i for i in range(32)]).tobytes()
        wav = _wav_bytes(frames, rate=24000, channels=1)
        self.assertEqual((frames, 24000), bridge._decode_tts_audio(wav))

    def test_tts_raw_pcm_is_returned_with_the_default_rate(self):
        raw = b"\x01\x00\x02\x00"
        self.assertEqual((raw, 24000), bridge._decode_tts_audio(raw))

    def test_tts_wav_at_a_foreign_rate_is_resampled_to_the_source_rate(self):
        frames = array("h", [i * 100 for i in range(48)]).tobytes()
        wav = _wav_bytes(frames, rate=48000, channels=1)
        pcm = bridge._tts_pcm_from_response(wav)
        self.assertEqual(24, len(pcm) // 2)

    def test_speech_falls_back_to_the_local_engine_when_every_key_is_dead(self):
        """A silent answer is the worst outcome - the local voice takes over.

        Observed live on 2026-10-22: every key and every TTS model hit
        the free-tier quota and the turn produced no audio at all. Piper
        on the same server measures faster than real time with no network
        and no quota, so it becomes the emergency voice when the last
        key is exhausted.
        """
        class Quota(Exception):
            code = 429

        calls = []

        class Models:
            def generate_content(self, model, contents, config):
                calls.append(model)
                raise Quota("429 RESOURCE_EXHAUSTED")

        fake_genai = SimpleNamespace(
            Client=lambda **kw: SimpleNamespace(models=Models()),
            types=unittest.mock.MagicMock())  # speech/Voice/Config-Dummy
        fake_genai.genai = fake_genai  # from google import genai
        engine = bridge.GeminiTurnEngine(["key-one", "key-two"])
        self.assertEqual("key-one", engine.current_key())

        rendered = []

        def fake_local(text, sample_rate=bridge.DEFAULT_SAMPLE_RATE, **kwargs):
            rendered.append(text)
            return b"\x01\x00" * 4

        with unittest.mock.patch.object(bridge, "local_tts_pcm", fake_local), \
                unittest.mock.patch.dict(
                    sys.modules,
                    {"google": fake_genai, "google.genai": fake_genai}):
            pcm = asyncio.run(engine.speak("Guten Morgen"))

        self.assertEqual(b"\x01\x00" * 4, pcm, "the local voice must render")
        self.assertEqual(["Guten Morgen"], rendered)
        self.assertGreaterEqual(
            len(calls), 2, "every key must have been tried first")
        # Und der naechste Turn versucht es wieder mit Gemini: der
        # Fallback ist ein Notdienst, kein Dauerzustand.
        self.assertEqual(
            "key-two", engine.current_key(),
            "the rotation must have moved past the dead key")

    def test_tts_default_model_is_a_quota_available_tts_model(self):
        self.assertEqual("gemini-3.8-flash-lite-tts", bridge.DEFAULT_TTS_MODEL)

    def test_provider_rate_limits_are_retryable_but_not_model_errors(self):
        class Err(Exception):
            def __init__(self, code):
                self.code = code

        self.assertTrue(bridge._is_retryable(Err(429)))
        self.assertTrue(bridge._is_retryable(Err(503)))
        self.assertFalse(bridge._is_retryable(Err(404)))
        self.assertFalse(bridge._is_retryable(ValueError("boom")))
        self.assertTrue(bridge._is_model_incompatible(Err(400)))
        self.assertTrue(bridge._is_model_incompatible(Err(404)))
        self.assertFalse(bridge._is_model_incompatible(Err(429)))

    def test_quota_error_falls_back_without_retry_delay(self):
        class Err(Exception):
            code = 429

        class Models:
            def __init__(self):
                self.calls = 0

            def generate_content(self, **kwargs):
                self.calls += 1
                raise Err("RESOURCE_EXHAUSTED")

        client = SimpleNamespace(models=Models())
        with unittest.mock.patch.object(bridge.time, "sleep") as sleep:
            with self.assertRaises(Err):
                bridge.GeminiTurnEngine._call_with_retries(client, "model", [], None)
        self.assertEqual(1, client.models.calls)
        sleep.assert_not_called()

    def test_tts_model_incompatible_with_style_is_retried_without_it(self):
        class Err(Exception):
            code = 400

        calls = []

        class Models:
            def generate_content(self, model, contents, config):
                calls.append((model, contents, config))
                if getattr(config, "system_instruction", None):
                    raise Err("system_instruction rejected")
                return "spoken"

        client = SimpleNamespace(models=Models())
        style = SimpleNamespace(system_instruction="deutsch")
        plain = SimpleNamespace(system_instruction=None)
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        result = engine._generate(client, ("m",), "text", style, retry_config=plain)
        self.assertEqual(("m", "spoken"), result)
        self.assertEqual(2, len(calls), "same model must be retried without style")
        self.assertIsNone(calls[1][2].system_instruction)

    def test_a_model_that_rejected_the_style_is_not_styled_again(self):
        """The second turn must not waste a request on the styled call.

        A speech model rejects the system_instruction, so every turn paid
        for one doomed request before the working one. After the first
        turn the model is known - it must be asked without the style.
        """
        class Err(Exception):
            code = 400

        calls = []

        class Models:
            def generate_content(self, model, contents, config):
                calls.append((model, getattr(config, "system_instruction", None)))
                if getattr(config, "system_instruction", None):
                    raise Err("system_instruction rejected")
                return SimpleNamespace(
                    candidates=[SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(
                                inline_data=SimpleNamespace(data=b"\x00\x00"))]))])

        client = SimpleNamespace(models=Models())
        style = SimpleNamespace(system_instruction="deutsch")
        plain = SimpleNamespace(system_instruction=None)
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)

        engine._generate(client, ("speech-model",), "text", style,
                         retry_config=plain)
        self.assertEqual(2, len(calls), "the first turn needs the retry")

        # The next turn must go straight to the working configuration.
        calls.clear()
        engine._generate(client, ("speech-model",), "text", style,
                         retry_config=plain)
        self.assertEqual(1, len(calls),
                         "a model that already rejected the style must not "
                         "be asked for it again: the doomed request wastes "
                         "quota and a round trip")
        self.assertIsNone(calls[0][1])

    def test_every_model_has_a_fallback_chain(self):
        self.assertIn(bridge.DEFAULT_MODEL, bridge.FALLBACK_CHAT_MODELS)
        self.assertIn(bridge.DEFAULT_TTS_MODEL, bridge.FALLBACK_TTS_MODELS)
        self.assertGreater(len(bridge.FALLBACK_CHAT_MODELS), 1)
        self.assertGreater(len(bridge.FALLBACK_TTS_MODELS), 1)

    def test_second_gemini_key_rotates_on_quota_errors(self):
        class Err(Exception):
            code = 429

        keys = ("first", "second", "third")
        engine = bridge.GeminiTurnEngine(keys)
        self.assertEqual("first", engine.current_key())
        self.assertTrue(engine._rotate_key())
        self.assertEqual("second", engine.current_key())
        self.assertTrue(engine._rotate_key())
        self.assertEqual("third", engine.current_key())
        self.assertFalse(engine._rotate_key(), "no rotation past the last key")
        self.assertEqual("third", engine.current_key())

    def test_rotation_helper_retries_turn_on_quota_only(self):
        class Quota(Exception):
            code = 429

        class Broken(Exception):
            pass

        engine = bridge.GeminiTurnEngine(("a", "b", "c"))
        seen = []

        def flaky(key):
            seen.append(key)
            if len(seen) < 3:
                raise Quota("quota")
            return "ok"

        self.assertEqual("ok", engine._turn_with_key_rotation(flaky))
        self.assertEqual(["a", "b", "c"], seen)

        engine2 = bridge.GeminiTurnEngine(("a",))
        seen2 = []

        def always_quota(key):
            seen2.append(key)
            raise Quota("quota")

        with self.assertRaises(Quota):
            bridge.GeminiTurnEngine._turn_with_key_rotation(engine2, always_quota)
        self.assertEqual(["a"], seen2)

        engine3 = bridge.GeminiTurnEngine(("a", "b"))
        with self.assertRaises(Broken):
            bridge.GeminiTurnEngine._turn_with_key_rotation(
                engine3, lambda key: (_ for _ in ()).throw(Broken())
            )

    def test_rotation_also_skips_unauthenticated_keys(self):
        class Unauthenticated(Exception):
            code = 401

        engine = bridge.GeminiTurnEngine(("dead", "alive"))
        self.assertEqual(
            "ok",
            bridge.GeminiTurnEngine._turn_with_key_rotation(
                engine, lambda key: "ok" if key == "alive" else (_ for _ in ()).throw(Unauthenticated())
            ),
        )

    def test_rotation_ignores_plain_model_errors(self):
        class Incompatible(Exception):
            def __str__(self):
                return "INVALID_ARGUMENT: model cannot do that"

        engine = bridge.GeminiTurnEngine(("a", "b"))
        with self.assertRaises(Incompatible):
            bridge.GeminiTurnEngine._turn_with_key_rotation(
                engine, lambda key: (_ for _ in ()).throw(Incompatible())
            )

    def test_tts_key_rotation_never_repeats_the_chat_side_effect(self):
        """A quota failure while synthesising must not re-run the answer turn."""
        calls = {"answer": 0, "speech": 0}

        class Quota(Exception):
            code = 429

        class Engine(bridge.GeminiTurnEngine):
            def _answer_on_key(self, genai, mic_pcm):
                calls["answer"] += 1
                return "Antwort mit Seiteneffekt"

            def _speech_on_key(self, genai, answer):
                calls["speech"] += 1
                if calls["speech"] == 1:
                    raise Quota("429 quota")
                return REPLY_PCM

        engine = Engine(("first", "second"))
        stub = unittest.mock.MagicMock()
        with unittest.mock.patch.dict(
            sys.modules,
            {"google": stub, "google.genai": stub},
        ):
            answer, pcm = asyncio.run(engine.turn(b"\x01\x00" * 8))

        self.assertEqual("Antwort mit Seiteneffekt", answer)
        self.assertEqual(REPLY_PCM, pcm)
        self.assertEqual(1, calls["answer"], "chat/side effects must run exactly once")
        self.assertEqual(2, calls["speech"], "speech retries on the next key")

    def test_unauthenticated_key_rotates_before_any_side_effect(self):
        calls = []

        class Unauthenticated(Exception):
            code = 401

        class Engine(bridge.GeminiTurnEngine):
            def _answer_on_key(self, genai, mic_pcm):
                calls.append(self.current_key())
                if self.current_key() == "dead":
                    raise Unauthenticated("API key not valid")
                return "ok"

            def _speech_on_key(self, genai, answer):
                return b"\x00\x00"

        engine = Engine(("dead", "alive"))
        stub = unittest.mock.MagicMock()
        with unittest.mock.patch.dict(
            sys.modules, {"google": stub, "google.genai": stub}
        ):
            answer, _pcm = asyncio.run(engine.turn(b"\x01\x00" * 8))

        self.assertEqual("ok", answer)
        self.assertEqual(["dead", "alive"], calls, "an unauthenticated key is skipped")

    def test_extra_gemini_key_files_extend_the_key_ring(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            primary = tmp_path / "gemini-key.txt"
            extra = tmp_path / "gemini-key-2.txt"
            missing = tmp_path / "gemini-key-3.txt"
            for path, marker in ((primary, "KEY-A"), (extra, "KEY-B")):
                path.write_text(marker + "\n")
                os.chmod(path, 0o600)
            keys = bridge.load_gemini_keys(primary, [extra, missing])
            self.assertEqual(("KEY-A", "KEY-B"), keys)
            self.assertEqual(("KEY-A",), bridge.load_gemini_keys(primary, []))

    def test_extra_gemini_key_file_flag_is_repeatable(self):
        args = bridge.build_arg_parser().parse_args(
            [
                "--relay-host", "h", "--key-file", "k", "--gemini-key-file", "g",
                "--extra-gemini-key-file", "g2", "--extra-gemini-key-file", "g3",
            ]
        )
        self.assertEqual(["g2", "g3"], args.extra_gemini_key_file)

    def test_engine_reuses_the_model_that_last_worked(self):
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        self.assertEqual(
            ("a", "b", "c"), engine._model_order(None, ("a", "b", "c"))
        )
        self.assertEqual(
            ("c", "a", "b"), engine._model_order("c", ("a", "b", "c"))
        )

    def test_engine_replays_the_running_dialogue_to_the_model(self):
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        pcm = b"\x01\x00" * 8
        self.assertEqual(1, len(engine._contents(pcm)))
        engine._remember(pcm, "Welcher Ort?")
        contents = engine._contents(pcm)
        self.assertEqual(3, len(contents))
        self.assertEqual("user", contents[0]["role"])
        self.assertEqual("model", contents[1]["role"])
        self.assertEqual("Welcher Ort?", contents[1]["parts"][0]["text"])
        self.assertEqual("user", contents[2]["role"])
        engine.reset_conversation()
        self.assertEqual(1, len(engine._contents(pcm)))

    def test_engine_conversation_history_is_bounded(self):
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER)
        pcm = b"\x01\x00" * 8
        for _ in range(12):
            engine._remember(pcm, "Antwort")
        self.assertLessEqual(
            len(engine._contents(pcm)), 1 + 2 * bridge.MAX_HISTORY_TURNS
        )

    def test_main_refuses_bad_key_mode_without_leaking(self):
        bad = _key_file(self._tmp.name, mode=0o644, name="bad-api-key")
        argv = [
            "--relay-host", LOOPBACK,
            "--relay-port", "16053",
            "--key-file", str(bad),
            "--gemini-key-file", str(_key_file(self._tmp.name, name="gemini-key")),
        ]
        out, err = _io_buffers()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exit_code = bridge.main(argv)
        self.assertNotEqual(0, exit_code)
        blob = out.getvalue() + err.getvalue()
        self.assertNotIn(API_KEY_MARKER, blob)
        self.assertNotIn(GEMINI_KEY_MARKER, blob)

    def test_main_requires_exactly_one_noise_key_flag(self):
        noise = _key_file(self._tmp.name, name="noise-key")
        gemini = _key_file(self._tmp.name, name="gemini", content=GEMINI_KEY_MARKER + "\n")
        base = ["--relay-host", "h", "--gemini-key-file", str(gemini)]
        cases = (base, base + ["--key-file", str(noise), "--api-key-file", str(noise)])
        for argv in cases:
            out, err = _io_buffers()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = bridge.main(argv)
            self.assertNotEqual(0, exit_code)
            blob = out.getvalue() + err.getvalue()
            self.assertNotIn(API_KEY_MARKER, blob)
            self.assertNotIn(GEMINI_KEY_MARKER, blob)


def _io_buffers():
    import io

    return io.StringIO(), io.StringIO()


if __name__ == "__main__":
    unittest.main()