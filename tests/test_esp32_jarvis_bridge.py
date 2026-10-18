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
        self.result = (transcript, reply)
        self.resets = 0
        self.gate: asyncio.Future | None = None  # optional: keep the turn pending under test control

    async def turn(self, mic_pcm):
        self.calls.append(mic_pcm)
        if self.gate is not None:
            await self.gate
        return self.result

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
        await audio(b"\x10\x00" * 320, None)
        self.assertTrue(
            await _wait_for(lambda: engine.calls and client.saw_run_end),
            "bounded capture did not finish the turn",
        )
        self.assertEqual([b"\x10\x00" * 320], engine.calls)
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
        # A silent turn ends the conversation: no audio, no TTS, but the device
        # must be released from its listening state with RUN_END.
        self.assertEqual(
            [("subscribe", None), ("event", "VOICE_ASSISTANT_RUN_END")],
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
        self.assertEqual(REPLY_PCM, body, "chunks must be byte-exact PCM, no container header")
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

    async def test_followup_start_gets_the_longer_window(self):
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
                await _wait_for(lambda: any("idle_window=0.3" in l for l in logs)),
                "a follow-up turn must wait longer for the user's answer",
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

    async def test_silent_followup_turn_ends_the_conversation(self):
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
        self.assertGreater(engine.resets, resets_before)
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
        self.assertEqual("gemini-flash-latest", bridge.DEFAULT_MODEL)
        self.assertEqual(bridge.DEFAULT_MODEL, defaults.model)
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

    def test_pcm_mime_uses_declared_device_rate(self):
        self.assertEqual("audio/pcm;rate=24000", bridge._pcm_mime(24000))
        self.assertEqual("audio/pcm;rate=16000", bridge._pcm_mime(16000))
        self.assertEqual(16000, bridge.DEFAULT_SAMPLE_RATE)

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

    def test_tts_default_model_is_a_quota_available_tts_model(self):
        self.assertEqual("gemini-3.8-flash-tts", bridge.DEFAULT_TTS_MODEL)

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

    def test_every_model_has_a_fallback_chain(self):
        self.assertIn(bridge.DEFAULT_MODEL, bridge.FALLBACK_CHAT_MODELS)
        self.assertIn(bridge.DEFAULT_TTS_MODEL, bridge.FALLBACK_TTS_MODELS)
        self.assertGreater(len(bridge.FALLBACK_CHAT_MODELS), 1)
        self.assertGreater(len(bridge.FALLBACK_TTS_MODELS), 1)

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