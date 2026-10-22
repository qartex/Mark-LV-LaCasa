"""Firmware contract for the satellite's wake-word engine.

Two defects are pinned here, both found on the live board (2026-10-21):

1. The wake-word threshold slider was SILENTLY IGNORED.
   ``apply_wake_word_sensitivity`` only read the slider when the "Wake word
   sensitivity" select was set to "Custom". In every other position the script
   replaced the model cutoff with the model's own default (0.97 -> 247). An
   operator who dialled the threshold down to 0.6 in Home Assistant therefore
   saw the number change and nothing else: the board kept running at 0.97.
   The operator's threshold must be the effective cutoff in every position.

2. The wake-word engine must come up at boot, ordered AFTER
   ``micro_wake_word.setup()``, and it must not be able to stay dead.

   ``MicroWakeWord::get_setup_priority()`` is ``setup_priority::AFTER_CONNECTION``
   (100 in ESPHome 2026.9.0), so the component's ``setup()`` runs during
   ``Application::setup()`` at priority 100. The ``esphome: on_boot``
   automations are ``StartupTrigger`` components whose priority is the number
   given in the YAML, and ``Application::setup()`` sorts ALL components by
   priority, DESCENDING (highest first - see the insertion sort in
   ``esphome/core/application.cpp``). A lower number therefore runs LATER.

   Consequence: an on_boot block with priority -100 runs AFTER the component
   was set up, so ``micro_wake_word.start()`` finds ``is_ready() == true`` and
   the inference task starts. The board's own serial log of 2026-10-19 shows
   exactly that on this firmware: "Starting wake word detection", "Inference
   task is running", "Detected 'Hey Jarvis' ... 0.99".

   The failure mode that must never come back is the opposite order: a start
   that ran BEFORE ``micro_wake_word.setup()`` would hit "Wake word detection
   can't start as the component hasn't been setup yet" and - because
   ``MicroWakeWord::start()`` has no retry - the engine would stay dead for
   the whole boot, with no recovery except a reboot. The firmware therefore
   re-issues the start until the engine reports itself running.

The YAML is an ESPHome document with custom tags (!include/!secret/!lambda),
so the tests assert on the parsed structure of the relevant blocks instead of
loading the whole file with a plain YAML parser.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "esp32-satellite"
CORE = DEPLOY / "vendor" / "core.yaml"

# ESPHome 2026.9.0 setup priorities (esphome/core/component.h).
SETUP_PRIORITY_AFTER_CONNECTION = 100
SETUP_PRIORITY_LATE = -100


def _on_boot_blocks(core: str) -> list[tuple[int, str]]:
    """Return (priority, body) for every ``esphome: on_boot`` block."""
    section = core.split("  on_boot:\n", 1)
    if len(section) == 1:
        return []
    body = section[1].split("\nesp32:\n", 1)[0]
    blocks: list[tuple[int, str]] = []
    current_priority: int | None = None
    current: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^    - priority:\s*(-?\d+)\s*$", line)
        if match:
            if current_priority is not None:
                blocks.append((current_priority, "\n".join(current)))
            current_priority = int(match.group(1))
            current = []
            continue
        if re.match(r"^    - ", line) and current_priority is not None:
            # A new list item that is not a priority -> end of this block.
            blocks.append((current_priority, "\n".join(current)))
            current_priority = None
            current = []
            continue
        if current_priority is not None:
            current.append(line)
    if current_priority is not None:
        blocks.append((current_priority, "\n".join(current)))
    return blocks


class ESP32WakeWordEngineTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.core = CORE.read_text(encoding="utf-8")

    # -- 3. a client reconnect must be able to revive the engine -----------

    def test_a_client_reconnect_restarts_a_dead_wake_word_engine(self):
        """The engine must come back when a client connects.

        Symptom: the board went deaf after a restart and stayed deaf
        through 43 reconnects, because only the boot path ever started
        the engine.
        """
        connect = self._voice_assistant_connect_block()
        self.assertIsNotNone(
            connect, "voice_assistant has no on_client_connected block")
        self.assertIn(
            "micro_wake_word.start",
            connect,
            "a client reconnect must be able to restart the wake word "
            "engine; otherwise a lost start race is permanent",
        )
        self.assertIn(
            "is_running()",
            connect,
            "the restart must be conditional on the engine actually "
            "being down, so a healthy engine is not restarted",
        )

    # -- 4. a client reconnect must reopen a stopped microphone -------------

    def test_a_client_reconnect_reopens_a_stopped_microphone(self):
        """The microphone must run again after a client switch.

        Symptom (2026-10-22, live in the Growroom Madrid): after the
        speaker enrollment the board stopped hearing "Hey Jarvis" for
        good, although the engine reported "cutoff 153 (active)".

        on_client_disconnected runs voice_assistant.stop, and the voice
        assistant's STOP_MICROPHONE state calls MicrophoneSource::stop().
        The source is not passive, so its data gate
        (``enabled_ || passive_``) closes and the RUNNING wake word engine
        receives no audio at all. Only the engine's inference task opens
        the source again (microphone_source_->start()), so the reconnect
        path has to restart the engine when the MICROPHONE is down - the
        engine's own state says nothing about the source's data gate.
        """
        connect = self._voice_assistant_connect_block()
        self.assertIsNotNone(
            connect, "voice_assistant has no on_client_connected block")
        self.assertIn(
            "microphone.is_capturing",
            connect,
            "a client reconnect must check whether the microphone is still "
            "running; the engine's own state says nothing about the "
            "microphone source's data gate",
        )
        self.assertIn(
            "micro_wake_word.stop",
            connect,
            "a stopped microphone is only reopened by a fresh inference "
            "task, so the engine has to be stopped and started again",
        )

    def test_the_ring_shows_that_the_wake_word_was_detected(self):
        """The ring is the only feedback that the satellite heard.

        Symptom: the ring stayed dark while the board listened. The phase
        switch in control_leds handled the wake word phase, but nothing
        called control_leds when the phase changed - only on boot and on
        client connect/disconnect.
        """
        block = self._voice_assistant_trigger_block("on_wake_word_detected")
        self.assertIsNotNone(
            block, "voice_assistant has no on_wake_word_detected block")
        self.assertIn(
            "control_leds",
            block,
            "the detected wake word must update the LED ring, otherwise "
            "there is no visible confirmation that the board heard",
        )
        self.assertIn(
            "voice_assistant_phase",
            block,
            "the ring follows the voice assistant phase, so the detection "
            "must set the phase before the ring is updated",
        )

    def test_the_boot_path_and_the_reconnect_path_both_start_the_engine(self):
        """Both paths must exist: one for the boot, one for the recovery."""
        self.assertGreaterEqual(
            self.core.count("micro_wake_word.start"),
            2,
            "the engine needs a boot path and a reconnect path",
        )

    # -- 1. the operator's threshold must be the effective cutoff -----------

    def test_threshold_slider_is_applied_in_every_sensitivity_position(self):
        """The slider must not be ignored outside the 'Custom' position.

        Symptom: the operator sets "Wake word threshold" to 0.6 in Home
        Assistant; the board keeps running at the model's default cutoff
        (0.97 -> 247) and never detects the wake word.
        """
        script = self._script_block("apply_wake_word_sensitivity")
        self.assertIsNotNone(script, "missing apply_wake_word_sensitivity script")

        # The slider is read unconditionally - there must be no branch that
        # skips it.
        self.assertIn(
            "id(wake_word_threshold).state",
            script,
            "the script must read the threshold slider",
        )
        self.assertEqual(
            1,
            script.count("id(wake_word_threshold).state"),
            "the slider must be read exactly once, unconditionally",
        )
        self.assertNotIn(
            'if (opt == "Custom")',
            script,
            "the slider must not be read only in the 'Custom' position",
        )
        self.assertNotIn(
            "get_default_probability_cutoff() - offset",
            script,
            "the presets must not replace the operator's threshold with the "
            "model's default cutoff",
        )

    def test_sensitivity_presets_scale_the_operator_threshold(self):
        """'Very sensitive' must lower the operator's threshold, not replace it.

        The preset offsets are a RELATIVE adjustment on top of the slider
        value, so the three positions keep their ordering and the slider
        stays authoritative.
        """
        script = self._script_block("apply_wake_word_sensitivity")
        self.assertIsNotNone(script)
        self.assertIn("id(wake_word_threshold).state", script)
        self.assertRegex(
            script,
            r"offset",
            "the sensitivity presets must keep their relative offsets",
        )
        self.assertRegex(
            script,
            r"base",
            "the presets must be applied to the slider's base value",
        )

    def test_threshold_slider_is_published_before_the_first_cutoff_application(self):
        """The slider must be readable when the script first runs.

        The script returns early when the slider state is NaN (the template
        number has not published its restored value yet). The boot path must
        therefore apply the cutoffs only after the number has a value.
        """
        script = self._script_block("apply_wake_word_sensitivity")
        self.assertIsNotNone(script)
        self.assertIn(
            "std::isnan(t)",
            script,
            "the script must guard against an unpublished slider state",
        )
        boot = self._on_boot_body()
        self.assertIn(
            "- script.execute: apply_wake_word_sensitivity",
            boot,
            "the boot path must apply the wake word cutoffs",
        )

    # -- 2. the engine must start after micro_wake_word.setup() ------------

    def test_wake_word_start_runs_after_the_component_setup(self):
        """The start must be ordered after micro_wake_word.setup().

        micro_wake_word sets up at AFTER_CONNECTION (100). Application::setup()
        sorts components by priority, DESCENDING, so a lower number runs LATER:
        the start block's priority must be BELOW 100, otherwise
        MicroWakeWord::start() finds is_ready() == false, logs "Wake word
        detection can't start as the component hasn't been setup yet" and -
        because start() has no retry - the engine stays dead for the whole
        boot.
        """
        starts = [
            (priority, body)
            for priority, body in _on_boot_blocks(self.core)
            if "micro_wake_word.start" in body
        ]
        self.assertTrue(
            starts,
            "no on_boot block starts micro_wake_word; the engine would never "
            "come up",
        )
        for priority, body in starts:
            self.assertLess(
                priority,
                SETUP_PRIORITY_AFTER_CONNECTION,
                "the micro_wake_word start must run AFTER the component setup: "
                "on_boot priorities sort DESCENDING, so the start block needs a "
                "priority below micro_wake_word's own setup priority "
                f"(AFTER_CONNECTION = {SETUP_PRIORITY_AFTER_CONNECTION})",
            )

    def test_wake_word_start_is_not_in_the_early_boot_block(self):
        """The start must not sit in a block that runs before the setup.

        The 375 block runs before everything else (Wi-Fi/API are still coming
        up); a start there would be lost.
        """
        early = [
            (priority, body)
            for priority, body in _on_boot_blocks(self.core)
            if priority > SETUP_PRIORITY_AFTER_CONNECTION
        ]
        for priority, body in early:
            self.assertNotIn(
                "micro_wake_word.start",
                body,
                f"the micro_wake_word start must not sit in the {priority} "
                "boot block: that block runs before the component setup",
            )

    def test_the_start_is_retried_so_a_lost_start_cannot_stay_lost(self):
        """A start that loses the race must be retried, not dropped.

        MicroWakeWord::start() returns without starting anything when the
        component is not set up yet, and it never retries on its own. The
        firmware must therefore re-issue the start until the engine is
        running, so the engine cannot stay dead for the whole boot.
        """
        blocks = "\n".join(body for _, body in _on_boot_blocks(self.core))
        self.assertRegex(
            blocks,
            r"repeat:",
            "the boot path must repeat the start until the engine runs",
        )
        self.assertRegex(
            blocks,
            r"micro_wake_word\.is_running|id\(mww\)\.is_running",
            "the retry must stop as soon as the engine is running",
        )

    def test_the_engine_start_survives_a_reboot_without_an_api_client(self):
        """The engine must not depend on an API client connecting.

        The board runs headless here. A start wired to an API/on_connect
        trigger would leave the board deaf until the first client connects.
        """
        blocks = "\n".join(body for _, body in _on_boot_blocks(self.core))
        self.assertNotIn(
            "on_connect",
            blocks,
            "the wake word start must not depend on an API client connecting",
        )
        self.assertNotIn(
            "on_client_connected",
            blocks,
            "the wake word start must not depend on an API client connecting",
        )

    # -- helpers ------------------------------------------------------------

    def _script_block(self, script_id: str) -> str:
        """Return the body of the ``script:`` entry with the given id."""
        match = re.search(
            rf"(?ms)^  - id: {re.escape(script_id)}\n(.*?)(?=^  - id: |^\n[a-z_]+:\n)",
            self.core,
        )
        if match is None:
            self.fail(f"missing script block {script_id}")
        return match.group(1)

    def _on_boot_body(self) -> str:
        return "\n".join(body for _, body in _on_boot_blocks(self.core))

    def _voice_assistant_trigger_block(self, trigger: str):
        """The body of a named trigger inside the voice_assistant block."""
        lines = self.core.splitlines()
        start = None
        for number, line in enumerate(lines):
            if line.startswith('voice_assistant:'):
                start = number
                break
        if start is None:
            return None
        body = []
        inside = False
        for line in lines[start + 1:]:
            if line and not line[0].isspace():
                break
            stripped = line.strip()
            if stripped.startswith(trigger + ':'):
                inside = True
                continue
            if inside:
                if stripped.startswith('on_') and stripped.endswith(':'):
                    break
                body.append(line)
        return chr(10).join(body) if body else None

    def _voice_assistant_connect_block(self):
        # The body of the voice assistant's on_client_connected block.
        # Several components have an on_client_connected; only the one
        # inside the voice_assistant block may restart the engine.
        lines = self.core.splitlines()
        start = None
        for number, line in enumerate(lines):
            if line.startswith('voice_assistant:'):
                start = number
                break
        if start is None:
            return None
        body = []
        inside = False
        for line in lines[start + 1:]:
            if line and not line[0].isspace():
                break
            stripped = line.strip()
            if stripped.startswith('on_client_connected:'):
                inside = True
                continue
            if inside:
                if stripped.startswith('on_client_disconnected:'):
                    break
                if stripped.startswith('on_error:'):
                    break
                body.append(line)
        return chr(10).join(body) if body else None
