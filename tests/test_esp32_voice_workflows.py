import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class StubCannanasHost:
    def __init__(self, records, *, exact=True, truncated=False):
        self.records = records
        self.exact = exact
        self.truncated = truncated
        self.reads = []
        self.prepares = []
        self.evaluations = []
        self.pending = False

    def call_read(self, action, params):
        self.reads.append((action, dict(params)))
        return {
            "ok": True,
            "records": list(self.records),
            "exact": self.exact,
            "truncated": self.truncated,
        }

    def prepare(self, action, params, turn):
        self.prepares.append((action, dict(params), turn))
        self.pending = True
        return {
            "ok": True,
            "summary": "Vorschau: Helsinki, Blattkontrolle. Bitte mit ja bestätigen.",
            "requires_confirmation": True,
        }

    def has_pending_for(self, turn):
        return self.pending

    def evaluate(self, turn, text):
        self.evaluations.append((turn, text))
        if text.strip().lower() == "ja":
            self.pending = False
            return {"handled": True, "spoken": "Erledigt — Tagebucheintrag gespeichert."}
        return {"handled": False, "spoken": ""}


class DiaryWorkflowTests(unittest.TestCase):
    def _turn(self):
        return SimpleNamespace(
            stable_device_id="esp32-growroom-1",
            auth_session_id="auth-digest",
            capture_id="capture-1",
            user_turn_id=7,
        )

    def test_room_prepares_one_zone_diary_without_plant_lookup(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([
            {
                "id": "plant-1",
                "name": "Helsinki #1",
                "batch_id": "batch-4",
                "batch_name": "Helsinki Woche 4",
            },
            {
                "id": "plant-2",
                "name": "Helsinki #2",
                "batch_id": "batch-4",
                "batch_name": "Helsinki Woche 4",
            },
        ])
        workflow = CannanasRoomDiaryWorkflow(host=host)
        turn = self._turn()

        answer = workflow.prepare(
            {"room": "Helsinki", "text": "Blätter heute kontrolliert."}, turn
        )

        self.assertIn("Vorschau", answer)
        self.assertEqual([], host.reads)
        self.assertEqual(1, len(host.prepares))
        action, params, prepared_turn = host.prepares[0]
        self.assertEqual("prepare_diary_create", action)
        self.assertEqual("Helsinki", params["room"])
        self.assertNotIn("plant_name", params)
        self.assertEqual("Blätter heute kontrolliert.", params["text"])
        self.assertIs(turn, prepared_turn)

    def test_room_without_text_keeps_microphone_conversation_open(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        answer = CannanasRoomDiaryWorkflow(host=host).prepare(
            {"room": "Helsinki"}, self._turn()
        )
        self.assertEqual(
            "Was soll ich in das Tagebuch für Raum Helsinki eintragen?", answer
        )
        self.assertEqual([], host.reads)
        self.assertEqual([], host.prepares)

    def test_room_with_many_plants_still_targets_the_zone(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([
            {"id": "plant-brain-1", "name": "Brain Damage #1"},
            {"id": "plant-ztilton-2", "name": "Ztilton #2"},
        ])
        answer = CannanasRoomDiaryWorkflow(host=host).prepare(
            {"room": "Helsinki", "text": "Blätter kontrolliert."}, self._turn()
        )
        self.assertIn("Vorschau", answer)
        self.assertEqual([], host.reads)
        self.assertEqual("Helsinki", host.prepares[0][1]["room"])

    def test_explicit_target_must_belong_to_room(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([
            {"id": "plant-brain-1", "name": "Brain Damage #1"},
            {"id": "plant-ztilton-2", "name": "Ztilton #2"},
        ])
        workflow = CannanasRoomDiaryWorkflow(host=host)
        turn = self._turn()
        answer = workflow.prepare(
            {
                "room": "Helsinki",
                "target": "Ztilton #2",
                "text": "Blätter kontrolliert.",
            },
            turn,
        )
        self.assertIn("Vorschau", answer)
        self.assertEqual("plant-ztilton-2", host.prepares[0][1]["plant_name"])

        host.prepares.clear()
        refused = workflow.prepare(
            {
                "room": "Helsinki",
                "target": "Madrid Batch",
                "text": "Blätter kontrolliert.",
            },
            turn,
        )
        self.assertIn("gehört nicht zu Raum Helsinki", refused)
        self.assertEqual([], host.prepares)

    def test_duplicate_batch_label_with_different_ids_is_ambiguous(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([
            {
                "id": "plant-1",
                "name": "Helsinki #1",
                "batch_id": "batch-a",
                "batch_name": "Woche 4",
            },
            {
                "id": "plant-2",
                "name": "Helsinki #2",
                "batch_id": "batch-b",
                "batch_name": "Woche 4",
            },
        ])
        workflow = CannanasRoomDiaryWorkflow(host=host)
        answer = workflow.prepare(
            {
                "room": "Helsinki",
                "target": "Woche 4",
                "text": "Blätter kontrolliert.",
            },
            self._turn(),
        )
        self.assertIn("mehrere Ziele", answer)
        self.assertEqual([], host.prepares)

    def test_zone_diary_does_not_depend_on_a_complete_plant_listing(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost(
            [{"name": "Brain Damage #1"}], exact=False, truncated=True
        )
        answer = CannanasRoomDiaryWorkflow(host=host).prepare(
            {"room": "Helsinki", "text": "Blätter kontrolliert."}, self._turn()
        )
        self.assertIn("Vorschau", answer)
        self.assertEqual([], host.reads)
        self.assertEqual("Helsinki", host.prepares[0][1]["room"])

    def test_cannanas_transport_failure_returns_bounded_spoken_notice(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        host.prepare = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret upstream details")
        )
        answer = CannanasRoomDiaryWorkflow(host=host).prepare(
            {"room": "Helsinki", "text": "Blätter kontrolliert."}, self._turn()
        )
        self.assertEqual("Cannanas ist derzeit nicht verfügbar. Bitte versuche es später erneut.", answer)
        self.assertNotIn("secret", answer)

    def test_cannanas_prepare_failure_returns_bounded_spoken_notice(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([{"id": "plant-1", "name": "Helsinki #1"}])
        host.prepare = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret upstream details")
        )
        answer = CannanasRoomDiaryWorkflow(host=host).prepare(
            {
                "room": "Helsinki",
                "target": "Helsinki #1",
                "text": "Blätter kontrolliert.",
            },
            self._turn(),
        )
        self.assertEqual("Cannanas ist derzeit nicht verfügbar. Bitte versuche es später erneut.", answer)
        self.assertNotIn("secret", answer)

    def test_confirmation_is_host_decided_not_model_decided(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        host.pending = True
        workflow = CannanasRoomDiaryWorkflow(host=host)
        turn = self._turn()
        self.assertTrue(workflow.has_pending(turn))
        result = workflow.evaluate_confirmation(turn, "ja")
        self.assertEqual("Erledigt — Tagebucheintrag gespeichert.", result)
        self.assertEqual([(turn, "ja")], host.evaluations)


class DiaryMeasurementTests(unittest.TestCase):
    """A room diary carries the measured values of that room."""

    SENSORS = {
        "zonen_madrid_temperatur_celsius": 24.2,
        "zonen_madrid_luftfeuchte": 61.0,
        "zonen_helsinki_temperatur_celsius": 19.8,
    }

    def _turn(self):
        return SimpleNamespace(
            stable_device_id="esp32-growroom-1",
            auth_session_id="auth-digest",
            capture_id="capture-1",
            user_turn_id=7,
        )

    def test_a_room_diary_takes_the_measured_values(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        workflow = CannanasRoomDiaryWorkflow(
            host=host, sensors=lambda: dict(self.SENSORS)
        )
        answer = workflow.prepare(
            {"room": "Madrid", "text": "Blätter kontrolliert."}, self._turn()
        )
        self.assertIn("Vorschau", answer)
        params = host.prepares[0][1]
        self.assertEqual(24.2, params["measurements"]["air_temperature"])
        self.assertEqual(61.0, params["measurements"]["air_humidity"])

    def test_a_diary_without_a_room_keeps_no_measurements(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        workflow = CannanasRoomDiaryWorkflow(
            host=host, sensors=lambda: dict(self.SENSORS)
        )
        workflow.prepare(
            {"room": "Helsinki", "text": "Blätter kontrolliert."}, self._turn()
        )
        params = host.prepares[0][1]
        self.assertEqual(19.8, params["measurements"]["air_temperature"])
        self.assertNotIn("air_humidity", params["measurements"])

    def test_a_diary_without_sensors_still_works(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        workflow = CannanasRoomDiaryWorkflow(host=host)
        answer = workflow.prepare(
            {"room": "Madrid", "text": "Blätter kontrolliert."}, self._turn()
        )
        self.assertIn("Vorschau", answer)
        self.assertNotIn("measurements", host.prepares[0][1])

    def test_a_broken_sensor_source_does_not_fail_the_entry(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        def broken():
            raise RuntimeError("sensor source down")

        host = StubCannanasHost([])
        workflow = CannanasRoomDiaryWorkflow(host=host, sensors=broken)
        answer = workflow.prepare(
            {"room": "Madrid", "text": "Blätter kontrolliert."}, self._turn()
        )
        self.assertIn("Vorschau", answer)
        self.assertNotIn("measurements", host.prepares[0][1])

    def test_spoken_measurements_win_over_the_measured_values(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([])
        workflow = CannanasRoomDiaryWorkflow(
            host=host, sensors=lambda: dict(self.SENSORS)
        )
        workflow.prepare(
            {
                "room": "Madrid",
                "text": "Blätter kontrolliert.",
                "measurements": {"air_temperature": 30.0},
            },
            self._turn(),
        )
        measurements = host.prepares[0][1]["measurements"]
        self.assertEqual(30.0, measurements["air_temperature"])
        self.assertNotIn("air_humidity", measurements)

    def test_a_plant_entry_in_a_room_keeps_the_measured_values(self):
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        host = StubCannanasHost([
            {"id": "plant-1", "name": "Helsinki #1"},
        ])
        workflow = CannanasRoomDiaryWorkflow(
            host=host, sensors=lambda: dict(self.SENSORS)
        )
        workflow.prepare(
            {
                "room": "Helsinki",
                "target": "Helsinki #1",
                "text": "Blätter kontrolliert.",
            },
            self._turn(),
        )
        measurements = host.prepares[0][1]["measurements"]
        self.assertEqual(19.8, measurements["air_temperature"])

    def test_a_room_diary_entry_carries_the_measured_values_to_the_adapter(self):
        """The whole path: sensors -> workflow -> host -> adapter payload.

        The diary entry is only useful if the measured values really reach
        the adapter — the host must not drop them on the way. This runs the
        real host with a recording client, so the assertion is on the
        arguments the adapter would really receive.
        """
        import tempfile
        from pathlib import Path

        from core.cannanas_audit import CannanasAudit
        from core.cannanas_host import CannanasHost
        from core.esp32_voice_workflows import CannanasRoomDiaryWorkflow

        recorded = {}

        class RecordingClient:
            def call(self, tool, arguments, budget=None):
                if tool.startswith("prepare_"):
                    recorded["tool"] = tool
                    recorded["arguments"] = dict(arguments)
                if tool == "get_adapter_info":
                    return {"ok": True, "config": {"boot_id": "adapter-boot-1"}}
                return {
                    "ok": True,
                    "requires_confirmation": True,
                    "confirmation_token": "opaque-token",
                    "payload_hash": "hash",
                    "preview": {"referenz": "Madrid", "typ": "Raum/Zone",
                               "text": "Blätter kontrolliert."},
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = CannanasHost(
                env={
                    "JARVIS_CANNANAS_ADAPTER_COMMAND": "/opt/fake/adapter",
                    "JARVIS_CANNANAS_ADAPTER_ARGS": "",
                    "JARVIS_CANNANAS_AUDIT_FILE": str(root / "audit.jsonl"),
                    "CANNANAS_API_KEY": "dummy-not-real",
                    "CANNANAS_CLUB_ID": "club-dummy",
                    "CANNANAS_STATE_DIR": str(root / "state"),
                    "CANNANAS_AUDIT_FILE": str(root / "adapter-audit.jsonl"),
                },
                client=RecordingClient(),
                audit=CannanasAudit(root / "audit.jsonl"),
            )
            workflow = CannanasRoomDiaryWorkflow(
                host=host, sensors=lambda: dict(self.SENSORS)
            )
            answer = workflow.prepare(
                {"room": "Madrid", "text": "Blätter kontrolliert."}, self._turn()
            )

        self.assertIn("Vorschau", answer)
        self.assertEqual("prepare_diary_create", recorded["tool"])
        measurements = recorded["arguments"]["measurements"]
        self.assertEqual(24.2, measurements["air_temperature"])
        self.assertEqual(61.0, measurements["air_humidity"])
        self.assertEqual("Madrid", recorded["arguments"]["room"])

    def test_the_closed_enum_diary_entry_also_carries_the_measured_values(self):
        """The voice path of the closed action enum must measure too.

        The gardener says "Tagebucheintrag Madrid" — the bridge may answer
        with the closed enum instead of the diary workflow. Both paths must
        attach the measured values.
        """
        import core.cannanas_host as cannanas_host
        import esp32_jarvis_bridge as bridge

        recorded = {}

        class RecordingClient:
            def call(self, tool, arguments, budget=None):
                if tool.startswith("prepare_"):
                    recorded["arguments"] = dict(arguments)
                return {"ok": True}

        class RealHost:
            def __init__(self):
                self._client = RecordingClient()

            def prepare(self, action, params, turn):
                tool, args = cannanas_host.prepare_tool_call(
                    action, params,
                    {"stable_device_id": "esp32-growroom-1",
                     "jarvis_session_id": "esp32-growroom-1",
                     "auth_session_id": "auth-digest",
                     "prepared_turn_id": "7",
                     "adapter_boot_id": "adapter-boot-1"},
                )
                self._client.call(tool, args)
                return {"ok": True, "summary": "Vorschau: Tagebucheintrag Madrid."}

        host = RealHost()
        previous = cannanas_host._HOST
        cannanas_host.set_host(host)
        self.addCleanup(cannanas_host.set_host, previous)

        runner = bridge._optional_cannanas_runner(
            sensors=lambda: dict(self.SENSORS)
        )
        answer = runner(
            {"action": "prepare_diary_create", "room": "Madrid",
             "text": "Blätter kontrolliert."},
            self._turn(),
        )
        self.assertIn("Vorschau", answer)
        measurements = recorded["arguments"].get("measurements") or {}
        self.assertEqual(24.2, measurements.get("air_temperature"))
        self.assertEqual(61.0, measurements.get("air_humidity"))

    def test_the_cannanas_runner_reads_the_home_assistant_sensors(self):
        """The shipped runner measures with the live Home Assistant."""
        import core.cannanas_host as cannanas_host
        import esp32_jarvis_bridge as bridge

        seen = []

        def sensors():
            seen.append(True)
            return {"zonen_madrid_temperatur_celsius": 24.2}

        recorded = {}

        class RealHost:
            def prepare(self, action, params, turn):
                tool, args = cannanas_host.prepare_tool_call(
                    action, params,
                    {"stable_device_id": "esp32-growroom-1",
                     "jarvis_session_id": "esp32-growroom-1",
                     "auth_session_id": "auth-digest",
                     "prepared_turn_id": "7",
                     "adapter_boot_id": "adapter-boot-1"},
                )
                recorded["arguments"] = dict(args)
                return {"ok": True, "summary": "Vorschau: Tagebucheintrag Madrid."}

        previous = cannanas_host._HOST
        cannanas_host.set_host(RealHost())
        self.addCleanup(cannanas_host.set_host, previous)

        with mock.patch.object(bridge, "_home_assistant_sensors", sensors):
            runner = bridge._optional_cannanas_runner()
            answer = runner(
                {"action": "prepare_diary_create", "room": "Madrid",
                 "text": "Blätter kontrolliert."},
                self._turn(),
            )
        self.assertIn("Vorschau", answer)
        self.assertTrue(seen, "the runner must read the live sensors")
        measurements = recorded["arguments"].get("measurements") or {}
        self.assertEqual(24.2, measurements.get("air_temperature"))

    def test_a_broken_sensor_source_does_not_cost_the_diary_entry(self):
        """The measured values are a bonus; the entry must still be written."""
        import core.cannanas_host as cannanas_host
        import esp32_jarvis_bridge as bridge

        def broken():
            raise RuntimeError("home assistant down")

        class RealHost:
            def prepare(self, action, params, turn):
                return {"ok": True, "summary": "Vorschau: Tagebucheintrag Madrid."}

        previous = cannanas_host._HOST
        cannanas_host.set_host(RealHost())
        self.addCleanup(cannanas_host.set_host, previous)

        runner = bridge._optional_cannanas_runner(sensors=broken)
        answer = runner(
            {"action": "prepare_diary_create", "room": "Madrid",
             "text": "Blätter kontrolliert."},
            self._turn(),
        )
        self.assertIn("Vorschau", answer)


class LearningQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "learning-queue"
        self.turn = SimpleNamespace(
            stable_device_id="esp32-growroom-1",
            auth_session_id="must-not-be-persisted",
            capture_id="capture-9",
            user_turn_id=12,
        )

    def test_unsupported_request_is_durably_queued_without_auth_identity(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        answer = queue.submit(
            {"request": "Lerne die Luftfeuchtigkeit für Helsinki anzusagen."},
            self.turn,
        )
        self.assertEqual(
            "Das kann ich noch nicht. Ich muss es erst lernen. "
            "Ich habe Hermes den Auftrag gegeben.",
            answer,
        )
        jobs = list((self.root / "pending").glob("*.json"))
        self.assertEqual(1, len(jobs))
        payload = json.loads(jobs[0].read_text())
        self.assertEqual("pending", payload["status"])
        self.assertEqual("esp32-growroom-1", payload["source"]["stable_device_id"])
        self.assertEqual(12, payload["source"]["user_turn_id"])
        self.assertNotIn("auth_session_id", payload["source"])
        self.assertNotIn("must-not-be-persisted", jobs[0].read_text())
        self.assertEqual(0o700, os.stat(self.root).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(jobs[0]).st_mode & 0o777)

    def test_duplicate_pending_or_dispatched_request_is_not_queued_twice(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        params = {"request": "Lerne den CO2-Wert zu nennen."}
        queue.submit(params, self.turn)
        queue.submit(params, self.turn)
        self.assertEqual(1, len(list(queue.pending.glob("*.json"))))
        job = next(queue.pending.glob("*.json"))
        os.replace(job, queue.dispatched / job.name)
        queue.submit(params, self.turn)
        self.assertEqual([], list(queue.pending.glob("*.json")))
        self.assertEqual(1, len(list(queue.dispatched.glob("*.json"))))

    def test_completed_capability_is_announced_exactly_once(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        queue.submit({"request": "Lerne CO2."}, self.turn)
        job = next(queue.pending.glob("*.json"))
        os.replace(job, queue.dispatched / job.name)
        queue.write_completion(
            job.stem, capability="CO2-Wert für Helsinki", status="completed"
        )
        self.assertEqual(
            "So, jetzt kannst du die Funktion CO2-Wert für Helsinki nutzen.",
            queue.take_completion_announcement(),
        )
        self.assertIsNone(queue.take_completion_announcement())
        self.assertTrue((self.root / "announced" / job.name).exists())

    def test_failed_job_is_not_announced_as_available(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        queue.submit({"request": "Lerne CO2."}, self.turn)
        job = next(queue.pending.glob("*.json"))
        os.replace(job, queue.dispatched / job.name)
        queue.write_completion(job.stem, capability="CO2", status="failed")
        self.assertIsNone(queue.take_completion_announcement())
        self.assertTrue((self.root / "announced" / job.name).exists())

    def test_completion_cannot_be_written_twice(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        queue.submit({"request": "Lerne CO2."}, self.turn)
        job = next(queue.pending.glob("*.json"))
        os.replace(job, queue.dispatched / job.name)
        queue.write_completion(job.stem, capability="CO2", status="completed")
        with self.assertRaises(ValueError):
            queue.write_completion(job.stem, capability="CO2", status="completed")

    def test_repeated_completion_cannot_recreate_an_announcement(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        queue.submit({"request": "Lerne CO2."}, self.turn)
        job = next(queue.pending.glob("*.json"))
        os.replace(job, queue.dispatched / job.name)
        queue.write_completion(job.stem, capability="CO2", status="completed")
        self.assertIsNotNone(queue.claim_completion_announcement())
        queue.ack_completion_announcement(job.stem)
        self.assertIsNone(queue.claim_completion_announcement())
        with self.assertRaises(ValueError):
            queue.write_completion(job.stem, capability="CO2", status="completed")
        self.assertIsNone(queue.claim_completion_announcement())

    def test_claim_blocks_a_second_speaker_until_released_or_acknowledged(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        queue.submit({"request": "Lerne CO2."}, self.turn)
        job = next(queue.pending.glob("*.json"))
        os.replace(job, queue.dispatched / job.name)
        queue.write_completion(job.stem, capability="CO2", status="completed")

        text, job_id = queue.claim_completion_announcement()
        self.assertEqual(job.stem, job_id)
        self.assertIn("nutzen", text)
        self.assertIsNone(queue.claim_completion_announcement())

        queue.release_completion_announcement(job_id)
        again = queue.claim_completion_announcement()
        self.assertIsNotNone(again)
        queue.ack_completion_announcement(job_id)
        self.assertTrue((self.root / "announced" / job.name).exists())
        self.assertIsNone(queue.claim_completion_announcement())

    def test_completion_for_unknown_or_noncanonical_job_is_rejected(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        with self.assertRaises(ValueError):
            queue.write_completion(
                "../forged", capability="gefälschte Funktion", status="completed"
            )
        with self.assertRaises(ValueError):
            queue.write_completion(
                "0" * 32, capability="unbekannte Funktion", status="completed"
            )

    def test_queue_transitions_fsync_the_directory_entry(self):
        from core.esp32_voice_workflows import LearningQueue
        from unittest import mock

        queue = LearningQueue(self.root)
        with mock.patch.object(
            LearningQueue, "_fsync_dir", autospec=True
        ) as sync_dir:
            queue.submit({"request": "Lerne CO2."}, self.turn)
        synced = [Path(call.args[0]).name for call in sync_dir.call_args_list]
        self.assertIn("pending", synced, "the new directory entry must be durable")

    def test_unbounded_or_empty_voice_request_is_refused(self):
        from core.esp32_voice_workflows import LearningQueue

        queue = LearningQueue(self.root)
        with self.assertRaises(ValueError):
            queue.submit({"request": ""}, self.turn)
        with self.assertRaises(ValueError):
            queue.submit({"request": "x" * 1001}, self.turn)


class LearningDispatcherTests(unittest.TestCase):
    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_public_cleartext_webhook_is_refused(self):
        from core.esp32_voice_workflows import LearningDispatcher

        with self.assertRaises(ValueError):
            LearningDispatcher(
                self.queue,
                webhook_url="http://hermes.example.com/webhooks/jarvis",
                secret="private-test-secret",
            )

    def test_redirects_are_never_followed(self):
        from core.esp32_voice_workflows import LearningDispatcher

        handler = LearningDispatcher._no_redirect_handler()
        self.assertIsNone(
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {"Location": "https://evil.example.com/"},
                "https://evil.example.com/",
            ),
            "a redirect must be refused instead of re-posting the payload",
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "queue"
        from core.esp32_voice_workflows import LearningQueue

        self.queue = LearningQueue(self.root)
        self.turn = SimpleNamespace(
            stable_device_id="esp32-growroom-1",
            capture_id="capture-1",
            user_turn_id=1,
        )
        self.queue.submit({"request": "Lerne den CO2-Wert anzusagen."}, self.turn)

    def test_dispatch_signs_hermes_webhook_and_moves_only_after_acceptance(self):
        from core.esp32_voice_workflows import LearningDispatcher

        seen = []

        def opener(request, timeout):
            seen.append((request, timeout))
            return self.Response(202)

        dispatcher = LearningDispatcher(
            self.queue,
            webhook_url="http://127.0.0.1:8644/webhooks/jarvis-learning",
            secret="private-test-secret",
            opener=opener,
        )
        self.assertEqual(1, dispatcher.dispatch_once())
        self.assertEqual([], list(self.queue.pending.glob("*.json")))
        dispatched = list(self.queue.dispatched.glob("*.json"))
        self.assertEqual(1, len(dispatched))
        request, timeout = seen[0]
        body = request.data
        expected = "sha256=" + hmac.new(
            b"private-test-secret", body, hashlib.sha256
        ).hexdigest()
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(expected, headers["x-hub-signature-256"])
        self.assertEqual("jarvis.learning.request", headers["x-github-event"])
        self.assertEqual(10, timeout)
        payload = json.loads(body)
        self.assertEqual("Lerne den CO2-Wert anzusagen.", payload["job"]["request"])
        self.assertNotIn("auth_session_id", json.dumps(payload))

    def test_tampered_or_oversized_pending_record_is_quarantined_not_signed(self):
        from core.esp32_voice_workflows import LearningDispatcher

        forged = self.queue.pending / "not-a-uuid.json"
        forged.write_text(json.dumps({
            "schema_version": 1,
            "id": "not-a-uuid",
            "status": "pending",
            "created_at": 1,
            "request": "x" * 5000,
            "source": {"stable_device_id": "dev", "capture_id": "c", "user_turn_id": 1},
            "admin": True,
        }))
        forged.chmod(0o600)
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return self.Response(202)

        dispatcher = LearningDispatcher(
            self.queue,
            webhook_url="http://127.0.0.1:8644/webhooks/jarvis-learning",
            secret="private-test-secret",
            opener=opener,
        )
        self.assertEqual(1, dispatcher.dispatch_once(), "only the valid job is sent")
        self.assertEqual(1, len(calls), "exactly one valid job was signed")
        self.assertFalse(forged.exists(), "invalid jobs leave pending/")
        self.assertEqual(1, len(list(self.queue.quarantine.glob("*.json"))))
        self.assertEqual([], list(self.queue.pending.glob("*.json")))
        self.assertEqual(1, len(list(self.queue.dispatched.glob("*.json"))))

    def test_filename_payload_id_mismatch_is_refused(self):
        from core.esp32_voice_workflows import LearningDispatcher

        job = next(self.queue.pending.glob("*.json"))
        payload = json.loads(job.read_text())
        payload["id"] = "0" * 32
        job.write_text(json.dumps(payload))
        dispatcher = LearningDispatcher(
            self.queue,
            webhook_url="http://127.0.0.1:8644/webhooks/jarvis-learning",
            secret="private-test-secret",
            opener=lambda request, timeout: self.Response(202),
        )
        self.assertEqual(0, dispatcher.dispatch_once())
        self.assertEqual(1, len(list(self.queue.quarantine.glob("*.json"))))
        self.assertEqual([], list(self.queue.dispatched.glob("*.json")))

    def test_dispatch_failure_leaves_job_pending_for_retry(self):
        from core.esp32_voice_workflows import LearningDispatcher

        dispatcher = LearningDispatcher(
            self.queue,
            webhook_url="http://127.0.0.1:8644/webhooks/jarvis-learning",
            secret="private-test-secret",
            opener=lambda request, timeout: self.Response(500),
        )
        self.assertEqual(0, dispatcher.dispatch_once())
        self.assertEqual(1, len(list(self.queue.pending.glob("*.json"))))
        self.assertEqual([], list(self.queue.dispatched.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
