"""The cannanas plugin: closed enum, reads, prepare previews, degraded readiness."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from core.cannanas_actions import CANNANAS_MODEL_ACTIONS
from core.cannanas_client import CannanasUnavailable
from core.cannanas_confirmation import ConfirmationCoordinator
from core.cannanas_host import CannanasHost, prepare_tool_call
from core.cannanas_audit import CannanasAudit
from core.headless import tool_allowed, trusted_headless_registry
from core.turn_provenance import CHANNEL_PHONE_PCM, IngressEnvelope
from core.user_turn import FinalizedTurn
from plugins import cannanas as cannanas_plugin

HOST_ENV = {
    'JARVIS_CANNANAS_ADAPTER_COMMAND': '/opt/cannanas/adapter/venv/bin/cannanas-mcp-adapter',
    'JARVIS_CANNANAS_ADAPTER_ARGS': '',
    'JARVIS_CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
    'CANNANAS_API_KEY': 'dummy-not-real',
    'CANNANAS_CLUB_ID': 'club-fixed',
    'CANNANAS_STATE_DIR': 'SET-IN-TESTS',
    'CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
}


class FakeClient:
    """Recording stub of the persistent MCP client.

    ``get_adapter_info`` reports the adapter's own boot id, because the host
    reads it to build the write fence and refuses a write without it.
    """

    def __init__(self, results=None, fail_on=None):
        self.calls = []
        self.results = results or {}
        self.fail_on = fail_on or {}
        self.lock = threading.Lock()

    def call(self, tool, arguments, budget=None):
        with self.lock:
            self.calls.append({'tool': tool, 'arguments': dict(arguments)})
        if tool in self.fail_on:
            raise self.fail_on[tool]
        if tool == 'get_adapter_info' and tool not in self.results:
            return {'ok': True, 'config': {'boot_id': 'adapter-boot-1'}}
        return dict(self.results.get(tool, {'ok': True}))


def make_turn(user_turn_id=20, *, device='device-durable-1', auth='auth-digest-1'):
    envelope = IngressEnvelope(stable_device_id=device, auth_session_id=auth,
                               capture_id='1:1', user_turn_id=user_turn_id,
                               channel=CHANNEL_PHONE_PCM, final=True)
    return FinalizedTurn(envelope=envelope, finalized_at=0.0,
                         identity=f'{envelope.channel}:{user_turn_id}')


def make_host(tmpdir, *, client=None, env=None):
    audit_file = tmpdir / 'host-audit.jsonl'
    full_env = dict(HOST_ENV)
    full_env['JARVIS_CANNANAS_AUDIT_FILE'] = str(audit_file)
    full_env['CANNANAS_STATE_DIR'] = str(tmpdir / 'state')
    full_env['CANNANAS_AUDIT_FILE'] = str(tmpdir / 'adapter-audit.jsonl')
    if env:
        full_env.update(env)
    host = CannanasHost(env=full_env, client=client,
                        audit=CannanasAudit(audit_file))
    return host


class PluginFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = FakeClient()
        self.host = make_host(self.root, client=self.client)
        self.turn = make_turn()

    def run_plugin(self, parameters, *, host=None, session_memory='unset'):
        return cannanas_plugin.run(
            parameters, session_memory=self.turn if session_memory == 'unset'
            else session_memory,
            host=host if host is not None else self.host)


class TestEnumClosedAtRegistry(PluginFixture):
    def test_registry_declares_exact_closed_enum_without_host_ops(self):
        registry = trusted_headless_registry()
        self.assertTrue(registry.has('cannanas'))
        declaration = registry.get_tool_declarations()
        by_name = {d['name']: d for d in declaration}
        self.assertEqual(sorted(by_name['cannanas']['parameters']['properties']['action']['enum']),
                         sorted(CANNANAS_MODEL_ACTIONS))
        self.assertNotIn('behavior', by_name['cannanas'])
        # belt and braces: the gate refuses host operations separately
        for action in ('confirm_pending', 'cancel_pending', 'confirm_plant_create'):
            self.assertFalse(tool_allowed('cannanas', {'action': action}))


class TestReadActionCallsAdapterAndProjects(PluginFixture):
    def test_zone_summary_read_maps_to_the_adapter_aggregate(self):
        from core.cannanas_host import read_tool_call

        tool, args, prefix = read_tool_call("zone_growth_summary", {})
        self.assertEqual("zone_growth_summary", tool)
        self.assertEqual({"limit": 50}, args)
        self.assertTrue(prefix)

    def test_read_maps_to_adapter_tool_and_returns_bounded_german_payload(self):
        self.client.results['count_plants_by_room'] = {
            'ok': True, 'room': 'Madrid', 'plant_count': 26, 'exact': True}
        result = self.run_plugin({'action': 'count_plants_by_room', 'room': 'Madrid'})
        self.assertEqual(self.client.calls[0]['tool'], 'count_plants_by_room')
        self.assertEqual(self.client.calls[0]['arguments'], {'room': 'Madrid'})
        self.assertEqual(result['summary'], 'Pflanzen im Raum Madrid: 26.')
        self.assertTrue(result['exact'])

    def test_read_limit_and_truncation_semantics(self):
        records = [{'name': f'Pflanze {i}', 'status': 'SEED'} for i in range(26)]
        self.client.results['find_plants'] = {
            'ok': True, 'plants': records, 'exact': True, 'truncated': True,
            'total': 26}
        result = self.run_plugin({'action': 'find_plants', 'query': 'Pflanze'})
        self.assertLessEqual(len(result['records']), 25)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['spoken_records'], 5)
        self.assertIn('Erste', result['summary'])

    def test_read_failure_is_one_bounded_german_notice(self):
        client = FakeClient(fail_on={'get_cannanas_overview':
                                     CannanasUnavailable('call: closedresourceerror')})
        host = make_host(self.root, client=client)
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertFalse(result.get('ok', False))
        self.assertNotIn('closedresourceerror', result['summary'])
        self.assertIn('Cannanas', result['summary'])

    # ── the watering interval (Task 7) ───────────────────────────────────
    def test_the_watering_interval_read_forwards_the_room(self):
        from core.cannanas_host import read_tool_call

        tool, args, prefix = read_tool_call("watering_intervals", {"room": "Madrid"})
        self.assertEqual("watering_intervals", tool)
        self.assertEqual("Madrid", args["room"])
        self.assertTrue(prefix)

    def test_the_watering_interval_read_without_a_room_asks_for_every_room(self):
        from core.cannanas_host import read_tool_call

        tool, args, _prefix = read_tool_call("watering_intervals", {})
        self.assertEqual("watering_intervals", tool)
        self.assertEqual("", args["room"])

    def test_the_watering_interval_read_accepts_the_room_as_the_search_term(self):
        """The gardener names the room in the slot the model chose."""
        from core.cannanas_host import read_tool_call

        _tool, args, _prefix = read_tool_call("watering_intervals", {"query": "Madrid"})
        self.assertEqual("Madrid", args["room"])

    def test_the_watering_interval_is_a_model_action(self):
        from core.cannanas_actions import (
            CANNANAS_MODEL_ACTIONS,
            CANNANAS_READ_ACTIONS,
        )

        self.assertIn("watering_intervals", CANNANAS_READ_ACTIONS)
        self.assertIn("watering_intervals", CANNANAS_MODEL_ACTIONS)

    def test_the_watering_interval_read_reaches_the_adapter(self):
        self.client.results['watering_intervals'] = {
            'ok': True,
            'raeume': [{'raum': 'Madrid', 'tage_seit_letztem_gies': 9,
                        'letzter_eintrag': 'Giessen'}],
            'hints': {'giessen': ['Madrid']},
        }
        result = self.run_plugin({'action': 'watering_intervals', 'room': 'Madrid'})
        self.assertEqual('watering_intervals', self.client.calls[0]['tool'])
        self.assertEqual({'room': 'Madrid'}, self.client.calls[0]['arguments'])
        self.assertTrue(result['ok'])
        # The gardener hears the room and the days without water.
        self.assertIn('Madrid', result['summary'])
        self.assertIn('9', result['summary'])


class TestPrepareStoresPendingAndReturnsPreview(PluginFixture):
    def test_prepare_binding_uses_the_adapters_own_boot_id(self):
        """The adapter fences writes on ITS boot id, not the host process id."""
        self.client.results['get_adapter_info'] = {
            'ok': True, 'config': {'boot_id': 'adapter-boot-1'}}
        self.client.results['prepare_plant_create'] = {
            'ok': True, 'requires_confirmation': True,
            'confirmation_token': 'opaque-token-1', 'payload_hash': 'payload-hash-1',
            'preview': {'plant_name': 'Brain Damage'}, 'club_id': 'club-fixed'}
        self.run_plugin({'action': 'prepare_plant_create', 'plant_name':
                         'Brain Damage', 'room': 'Madrid', 'status': 'SEED'})
        args = self.client.calls[-1]['arguments']
        self.assertEqual('adapter-boot-1', args['binding']['adapter_boot_id'])
        self.assertNotEqual(self.host.boot_id(), args['binding']['adapter_boot_id'])

    def test_room_diary_maps_to_adapter_zone_argument(self):
        binding = {"stable_device_id": "esp32-growroom-1"}
        tool, args = prepare_tool_call(
            "prepare_diary_create",
            {"room": "Madrid", "text": "Heute gegossen."},
            binding,
        )
        self.assertEqual("prepare_diary_create", tool)
        self.assertEqual("Madrid", args["room"])
        self.assertEqual("Heute gegossen.", args["text"])
        self.assertNotIn("plant_or_batch", args)
        self.assertIs(binding, args["binding"])

    def test_room_diary_preview_is_spoken_as_a_sentence(self):
        from core.cannanas_host import complete_preview_text

        spoken = complete_preview_text(
            {"referenz": " Madrid", "typ": "Raum/Zone",
             "text": "Heute gegossen.", "messungen": {}}
        )
        self.assertEqual("Tagebucheintrag Madrid: Heute gegossen.", spoken)

    def test_new_read_actions_map_to_adapter_tools(self):
        from core.cannanas_host import read_tool_call

        for action, tool in (
            ("list_mother_plants", "list_mother_plants"),
            ("list_diaries", "list_diaries"),
            ("list_harvests", "list_harvests"),
            ("get_grow_config", "get_grow_config"),
            ("list_plant_diaries", "list_plant_diaries"),
            ("zone_plant_status", "zone_plant_status"),
        ):
            mapped, _args, _prefix = read_tool_call(action, {})
            self.assertEqual(tool, mapped, action)

    def test_plant_update_forwards_the_spoken_fields(self):
        from core.cannanas_host import prepare_tool_call

        binding = {"stable_device_id": "esp32-1"}
        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "status": "Blüte",
             "fields": {"is_mother": True, "notes": "Mutterpflanze"}},
            binding)
        self.assertEqual("Ztilton #3", args["plant"])
        self.assertEqual("Blüte", args["fields"]["status"])
        self.assertEqual(True, args["fields"]["is_mother"])
        self.assertEqual("Mutterpflanze", args["fields"]["notes"])

    def test_batch_update_forwards_the_spoken_room(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_batch_update",
            {"plant_name": "Ztilton #6", "room": "Madrid",
             "status": "Blüte"},
            {"stable_device_id": "esp32-1"})
        self.assertEqual("Madrid", args["fields"]["room"])
        self.assertEqual("Blüte", args["fields"]["status"])

    def test_new_prepare_actions_map_to_adapter_tools(self):
        from core.cannanas_host import prepare_tool_call

        binding = {"stable_device_id": "esp32-1"}
        mapped, args = prepare_tool_call(
            "prepare_zone_update",
            {"zone": "Madrid", "fields": {"notes": "x"}}, binding)
        self.assertEqual("prepare_zone_update", mapped)
        self.assertEqual("Madrid", args["zone"])
        self.assertEqual({"notes": "x"}, args["fields"])

        mapped, args = prepare_tool_call(
            "prepare_harvest_create", {"plant_name": "Ztilton #3"}, binding)
        self.assertEqual("prepare_harvest_create", mapped)
        self.assertEqual("Ztilton #3", args["plants"])

        mapped, args = prepare_tool_call(
            "prepare_waste_disposal_create", {"plant_name": "Ztilton #3"}, binding)
        self.assertEqual("prepare_waste_disposal_create", mapped)
        self.assertEqual("Ztilton #3", args["plants"])

    def test_zone_update_without_fields_is_refused(self):
        from core.cannanas_host import prepare_tool_call

        with self.assertRaises(ValueError):
            prepare_tool_call("prepare_zone_update", {"zone": "Madrid"},
                              {"stable_device_id": "esp32-1"})

    def test_the_pending_carries_the_adapters_boot_id(self):
        """The confirm is fenced on the adapter's boot id, not the host's.

        The adapter compares the binding of a confirm against its own boot
        id, so the pending confirmation must store that one. With the host's
        process id every confirm would be refused as a restart.
        """
        self.client.results['get_adapter_info'] = {
            'ok': True, 'config': {'boot_id': 'adapter-boot-1'}}
        self.client.results['prepare_plant_update'] = {
            'ok': True, 'requires_confirmation': True,
            'confirmation_token': 'opaque-token-1',
            'payload_hash': 'payload-hash-1',
            'preview': {'status': 'FLOWERING'}, 'club_id': 'club-fixed'}
        self.run_plugin({'action': 'prepare_plant_update',
                         'plant_name': 'Ztilton #6', 'status': 'Blüte'})

        pending = self.host.coordinator.current('device-durable-1')
        self.assertIsNotNone(pending)
        self.assertEqual('adapter-boot-1', pending.adapter_boot_id)
        self.assertNotEqual(self.host.boot_id(), pending.adapter_boot_id,
                            'the host boot id never stands in for the adapter')

    def test_the_confirm_sends_the_adapters_boot_id(self):
        """The id that reaches the adapter on confirm is the adapter's own."""
        self.client.results['get_adapter_info'] = {
            'ok': True, 'config': {'boot_id': 'adapter-boot-1'}}
        self.client.results['prepare_plant_update'] = {
            'ok': True, 'requires_confirmation': True,
            'confirmation_token': 'opaque-token-1',
            'payload_hash': 'payload-hash-1',
            'preview': {'status': 'FLOWERING'}, 'club_id': 'club-fixed'}
        self.client.results['confirm_plant_update'] = {
            'ok': True, 'result_id': 'plant-42'}
        self.run_plugin({'action': 'prepare_plant_update',
                         'plant_name': 'Ztilton #6', 'status': 'Blüte'})

        self.host.confirm(make_turn(user_turn_id=21))

        confirms = [c for c in self.client.calls
                    if c['tool'].startswith('confirm_')]
        self.assertEqual(1, len(confirms))
        self.assertEqual('adapter-boot-1',
                         confirms[0]['arguments']['binding']['adapter_boot_id'])
        self.assertNotEqual(self.host.boot_id(),
                            confirms[0]['arguments']['binding']['adapter_boot_id'])

    def test_a_plant_create_refuses_a_field_it_cannot_carry(self):
        """A spoken field that would be dropped must be refused, not lost."""
        from core.cannanas_host import prepare_tool_call

        with self.assertRaises(ValueError) as caught:
            prepare_tool_call(
                "prepare_plant_create",
                {"plant_name": "Ztilton #8", "room": "Madrid",
                 "strain": "Ztilton", "status": "CUTTING",
                 "fields": {"substrate": "Coco", "description": "Wichtig"}},
                {"stable_device_id": "esp32-1"})
        self.assertIn("substrate", str(caught.exception))
        self.assertIn("description", str(caught.exception))

    def test_a_plant_create_accepts_the_fields_it_can_carry(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_create",
            {"plant_name": "Ztilton #8", "room": "Madrid",
             "strain": "Ztilton", "status": "CUTTING",
             "mother_plant": "Ztilton #3", "plant_count": 4,
             "is_mother": True},
            {"stable_device_id": "esp32-1"})
        self.assertEqual("Ztilton #3", args["mother_plant"])
        self.assertEqual(4, args["plant_count"])
        self.assertEqual(True, args["is_mother"])

    def test_a_batch_create_refuses_a_field_it_cannot_carry(self):
        from core.cannanas_host import prepare_tool_call

        with self.assertRaises(ValueError) as caught:
            prepare_tool_call(
                "prepare_batch_create",
                {"room": "Madrid", "strain": "Ztilton", "plant_count": 5,
                 "status": "CUTTING",
                 "fields": {"cultivation_seed_date": "2026-10-01"}},
                {"stable_device_id": "esp32-1"})
        self.assertIn("cultivation_seed_date", str(caught.exception))

    def test_prepare_response_maps_to_adapter_and_stores_pending(self):
        self.client.results['prepare_plant_create'] = {
            'ok': True,
            'requires_confirmation': True,
            'confirmation_token': 'opaque-token-1',
            'payload_hash': 'payload-hash-1',
            'preview': {'plant_name': 'Brain Damage', 'status': 'SEED',
                        'room': 'Madrid', 'strain': 'Brain Damage Strain'},
            'club_id': 'club-fixed',
        }
        result = self.run_plugin({
            'action': 'prepare_plant_create', 'plant_name': 'Brain Damage',
            'room': 'Madrid', 'status': 'SEED',
            'query': 'Brain Damage Strain'})
        # The host first reads the adapter's own boot id for the write fence.
        prepares = [c for c in self.client.calls if c['tool'] == 'prepare_plant_create']
        tool, args = prepares[0]['tool'], prepares[0]['arguments']
        self.assertEqual(tool, 'prepare_plant_create')
        self.assertEqual(args['plant_name'], 'Brain Damage')
        binding = args['binding']
        self.assertEqual(binding['stable_device_id'], 'device-durable-1')
        self.assertEqual(binding['prepared_turn_id'], '20')
        self.assertEqual(binding['adapter_boot_id'], 'adapter-boot-1',
                         'the fence carries the adapter\'s own boot id')
        self.assertNotEqual(binding['adapter_boot_id'], self.host.boot_id(),
                            'the host boot id never stands in for the adapter')
        pending = self.host.coordinator.current('device-durable-1')
        self.assertIsNotNone(pending)
        self.assertEqual(pending.payload_hash, 'payload-hash-1')
        self.assertEqual(pending.state, 'pending')
        self.assertNotEqual(pending.expires_at - pending.created_at, 0)
        # The model result never contains the token or club id.
        rendered = json.dumps(result)
        self.assertNotIn('opaque-token-1', rendered)
        self.assertNotIn('club-fixed', rendered)
        self.assertNotIn('payload-hash-1', rendered)
        self.assertIn('bitte bestätigen', result['summary'].lower())
        self.assertIsInstance(result['preview'], dict)


class TestPrepareForwardsSpokenFields(PluginFixture):
    """Every spoken change reaches the adapter — none is dropped silently.

    The pinned adapter (2.0.0) owns the field whitelist: an unknown field
    name rejects the whole prepare, so the host must forward exactly the
    names the adapter understands.
    """

    BINDING = {"stable_device_id": "esp32-1"}

    def test_plant_update_forwards_every_spoken_change_field(self):
        from core.cannanas_host import prepare_tool_call

        fields = {
            'room': 'Helsinki',
            'strain': 'Northern Lights',
            'parent_plant': 'Mutter A',
            'is_mother': True,
            'description': 'Zweite Mutter',
            'expected_yield': 120,
            'actual_yield': 95,
            'substrate': 'Coco',
            'fertilizer': 'BioGrow',
            'archived': False,
            'expected_harvest_at': '2026-11-01',
            'cultivation_flowering_date': '2026-10-20',
        }
        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "fields": fields},
            self.BINDING)
        self.assertEqual("Ztilton #3", args["plant"])
        for key, value in fields.items():
            self.assertEqual(value, args["fields"][key], key)

    def test_plant_update_never_sends_a_name_field(self):
        """The adapter rejects 'name' — the target name is not a change."""
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "status": "Blüte"},
            self.BINDING)
        self.assertNotIn("name", args["fields"])
        self.assertEqual("Blüte", args["fields"]["status"])

    def test_plant_update_takes_room_and_strain_from_the_spoken_parameters(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "room": "Helsinki",
             "strain": "Northern Lights", "text": "Umgezogen"},
            self.BINDING)
        self.assertEqual("Helsinki", args["fields"]["room"])
        self.assertEqual("Northern Lights", args["fields"]["strain"])
        self.assertEqual("Umgezogen", args["fields"]["notes"])

    def test_plant_update_does_not_turn_the_search_term_into_a_strain(self):
        """query == plant_name is the same plant, not a second field."""
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "query": "Ztilton #3",
             "status": "Blüte"},
            self.BINDING)
        self.assertNotIn("strain", args["fields"])

    def test_plant_update_keeps_a_spoken_false_and_drops_an_empty_text(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3",
             "fields": {"is_mother": False, "notes": "", "description": None}},
            self.BINDING)
        self.assertEqual(False, args["fields"]["is_mother"])
        self.assertNotIn("notes", args["fields"])
        self.assertNotIn("description", args["fields"])

    def test_batch_update_forwards_room_and_notes_from_the_spoken_parameters(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_batch_update",
            {"plant_name": "Ztilton Woche 4", "room": "Helsinki",
             "status": "Blüte", "text": "Charge umgezogen"},
            self.BINDING)
        self.assertEqual("Ztilton Woche 4", args["batch"])
        self.assertEqual("Helsinki", args["fields"]["room"])
        self.assertEqual("Blüte", args["fields"]["status"])
        self.assertEqual("Charge umgezogen", args["fields"]["notes"])
        self.assertNotIn("name", args["fields"])

    def test_plant_create_forwards_the_mother_plant_and_the_plant_count(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_create",
            {"plant_name": "Ztilton #8", "room": "Madrid",
             "strain": "Ztilton", "status": "CUTTING", "count": 5,
             "fields": {"mother_plant": "Ztilton #3", "is_mother": True}},
            self.BINDING)
        self.assertEqual("Ztilton #3", args["mother_plant"])
        self.assertEqual(True, args["is_mother"])
        self.assertEqual(5, args["plant_count"])
        self.assertEqual("Ztilton", args["strain"])

    def test_plant_create_omits_the_plant_count_when_none_was_spoken(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_create",
            {"plant_name": "Ztilton #8", "room": "Madrid",
             "strain": "Ztilton", "status": "CUTTING"},
            self.BINDING)
        self.assertNotIn("plant_count", args)

    def test_batch_create_forwards_the_mother_plants_with_cutting_counts(self):
        from core.cannanas_host import prepare_tool_call

        mother_plants = [{"plant": "Mutter A", "cutting_count": 6},
                         {"plant": "Mutter B", "cutting_count": 4}]
        _tool, args = prepare_tool_call(
            "prepare_batch_create",
            {"room": "Madrid", "strain": "Ztilton", "count": 10,
             "status": "CUTTING", "fields": {"mother_plants": mother_plants}},
            self.BINDING)
        self.assertEqual(mother_plants, args["mother_plants"])
        self.assertEqual(10, args["plant_count"])

    def test_batch_create_never_clamps_a_spoken_plant_count(self):
        """A spoken count is never silently lowered; the adapter validates."""
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_batch_create",
            {"room": "Madrid", "strain": "Ztilton", "count": 300,
             "status": "CUTTING"},
            self.BINDING)
        self.assertEqual(300, args["plant_count"])

    def test_an_impossible_spoken_count_is_not_replaced_by_a_usable_one(self):
        """Zero plants is not silently turned into one plant."""
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_batch_create",
            {"room": "Madrid", "strain": "Ztilton", "count": 0,
             "status": "CUTTING"},
            self.BINDING)
        self.assertEqual(0, args["plant_count"])

        _tool, args = prepare_tool_call(
            "prepare_plant_create",
            {"plant_name": "Ztilton #8", "room": "Madrid", "strain": "Ztilton",
             "status": "CUTTING", "count": 0},
            self.BINDING)
        self.assertEqual(0, args["plant_count"])

    def test_harvest_create_takes_the_plants_and_the_batches(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_harvest_create",
            {"plant_name": "Ztilton #3, Ztilton #4", "batch": "Ztilton Woche 4",
             "text": "Ernte 1", "date": "2026-10-20"},
            self.BINDING)
        self.assertEqual("Ztilton #3, Ztilton #4", args["plants"])
        self.assertEqual("Ztilton Woche 4", args["batches"])
        self.assertEqual("Ernte 1", args["notes"])
        self.assertEqual("2026-10-20", args["harvested_at"])

    def test_waste_disposal_create_takes_the_plants_and_the_batches(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_waste_disposal_create",
            {"plant_name": "Ztilton #3", "batch": "Ztilton Woche 4"},
            self.BINDING)
        self.assertEqual("Ztilton #3", args["plants"])
        self.assertEqual("Ztilton Woche 4", args["batches"])

    def test_diary_create_for_a_batch_target(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_diary_create",
            {"batch": "Ztilton Woche 4", "text": "Schnitt ok"},
            self.BINDING)
        self.assertEqual("Ztilton Woche 4", args["batch"])
        self.assertEqual("Schnitt ok", args["text"])
        self.assertNotIn("room", args)


class TestRefusedPrepareNeverBecomesAPending(PluginFixture):
    """A refused prepare is spoken as a refusal — never as 'unverändert'."""

    def test_refused_prepare_says_why_and_stores_no_pending(self):
        self.client.results['prepare_plant_update'] = {
            'ok': False, 'error': "Unbekanntes Feld 'name'"}
        result = self.run_plugin({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #3', 'status': 'Blüte'})
        self.assertFalse(result.get('ok', True), result)
        self.assertIn('abgelehnt', result['summary'].lower())
        self.assertNotIn('Eintrag unverändert', result['summary'])
        # No pending may survive a refused prepare.
        self.assertIsNone(self.host.coordinator.current('device-durable-1'))
        self.assertFalse(result.get('requires_confirmation', False))

    def test_refused_prepare_never_reaches_a_confirm(self):
        class GuardedClient(FakeClient):
            def call(self, tool, arguments, budget=None):
                if tool.startswith('confirm_'):
                    raise AssertionError(f'POST attempted: {tool}')
                return super().call(tool, arguments, budget)

        client = GuardedClient(results={'prepare_plant_update': {
            'ok': False, 'error': 'Unbekanntes Feld'}})
        host = make_host(self.root, client=client)
        self.run_plugin({'action': 'prepare_plant_update',
                         'plant_name': 'Ztilton #3', 'status': 'Blüte'},
                        host=host)
        self.assertEqual([c['tool'] for c in client.calls],
                         ['get_adapter_info', 'prepare_plant_update'])

    def test_refused_prepare_writes_no_prepared_audit_entry(self):
        client = FakeClient(results={'prepare_plant_update': {
            'ok': False, 'error': 'Unbekanntes Feld'}})
        host = make_host(self.root, client=client)
        self.run_plugin({'action': 'prepare_plant_update',
                         'plant_name': 'Ztilton #3', 'status': 'Blüte'},
                        host=host)
        audit_file = self.root / 'host-audit.jsonl'
        written = audit_file.read_text() if audit_file.exists() else ''
        self.assertNotIn('"prepared"', written)

    def test_a_refusal_reason_never_leaks_internals(self):
        self.client.results['prepare_plant_update'] = {
            'ok': False, 'error': 'x' * 400}
        result = self.run_plugin({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #3', 'status': 'Blüte'})
        self.assertFalse(result.get('ok', True), result)
        self.assertLessEqual(len(result['summary']), 400)


class TestPrepareZeroPOSTs(PluginFixture):
    def test_prepare_never_touches_a_confirm_tool(self):
        class GuardedClient(FakeClient):
            def call(self, tool, arguments, budget=None):
                if tool.startswith('confirm_'):
                    raise AssertionError(f'POST attempted during prepare: {tool}')
                return super().call(tool, arguments, budget)
        client = GuardedClient(results={'prepare_plant_create': {
            'ok': True, 'confirmation_token': 't', 'payload_hash': 'h',
            'preview': {'plant_name': 'X'}, 'club_id': 'club-fixed'}})
        host = make_host(self.root, client=client)
        result = self.run_plugin({'action': 'prepare_plant_create',
                                  'plant_name': 'X', 'room': 'Madrid',
                                  'status': 'SEED'}, host=host)
        self.assertTrue(result['requires_confirmation'])
        # Only the boot-id read precedes the single prepare: still exactly one
        # prepare and never a confirm.
        self.assertEqual([c['tool'] for c in client.calls],
                         ['get_adapter_info', 'prepare_plant_create'])


class TestUnknownActionRefused(PluginFixture):
    def test_unknown_action_gets_fixed_german_refusal(self):
        result = self.run_plugin({'action': 'delete_plant'})
        self.assertFalse(result.get('ok', True))
        self.assertEqual(len(self.client.calls), 0)
        result = self.run_plugin({'action': 'confirm_pending'})
        self.assertFalse(result.get('ok', True))
        self.assertEqual(len(self.client.calls), 0)


class TestMissingConfigFailsBeforeNetwork(PluginFixture):
    def test_missing_configuration_names_only_the_variable(self):
        env = dict(HOST_ENV)
        env.pop('CANNANAS_API_KEY')
        host = CannanasHost(env=env, client=None)
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertFalse(result.get('ok', True))
        self.assertIn('CANNANAS_API_KEY', result['summary'])
        # Nothing else about configuration leaks, and no client was created.
        self.assertFalse(host.available())
        self.assertIsNone(host.client_if_started())

    def test_missing_host_audit_var_degrades_everything(self):
        env = dict(HOST_ENV)
        env.pop('JARVIS_CANNANAS_AUDIT_FILE')
        audit = CannanasAudit(self.root / 'unused.jsonl')
        host = CannanasHost(env=env, client=self.client, audit=audit)
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertIn('JARVIS_CANNANAS_AUDIT_FILE', result['summary'])


class TestDegradedCannanasKeepsJARVISAlive(PluginFixture):
    def test_degraded_host_returns_german_notice_not_exception(self):
        client = FakeClient(fail_on={'get_cannanas_overview':
                                     CannanasUnavailable('call: x')})
        host = make_host(self.root, client=client)
        host.mark_degraded('client: failure in tests')
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertFalse(result.get('ok', True))
        self.assertIn('Cannanas', result['summary'])
        # The plugin never raises; JARVIS keeps running.
        self.assertTrue(True)


class TestCannabisStatusStillDenied(PluginFixture):
    def test_cannabis_status_denied_before_dispatch(self):
        self.assertFalse(tool_allowed('cannabis_status', {'action': 'status'}))
        registry = trusted_headless_registry()
        self.assertFalse(registry.has('cannabis_status'))



class TestSpokenDurationBecomesSinceDays(PluginFixture):
    """A spoken duration becomes the day the phase began.

    The gardener says "seit 3 Wochen in Bluete" — a duration, not a date.
    The host owns the spoken language, so it turns the duration into days
    and hands the adapter a dict; the adapter owns the date arithmetic.
    """

    BINDING = {"stable_device_id": "esp32-1"}

    def test_a_spoken_duration_becomes_since_days(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "status": "seit 3 Wochen in Bluete"},
            self.BINDING)
        self.assertEqual(
            {"status": "Bluete", "since_days": 21}, args["fields"]["status"])

    def test_a_spoken_duration_keeps_the_spelled_status_word(self):
        """The host sends the word; the adapter owns the canonical status."""
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "status": "seit 10 Tagen in Blüte"},
            self.BINDING)
        self.assertEqual("Blüte", args["fields"]["status"]["status"])
        self.assertEqual(10, args["fields"]["status"]["since_days"])

    def test_every_spoken_unit_becomes_its_days(self):
        from core.cannanas_host import _spoken_since_days

        self.assertEqual(1, _spoken_since_days("seit 1 Tag"))
        self.assertEqual(10, _spoken_since_days("seit 10 Tagen"))
        self.assertEqual(21, _spoken_since_days("seit 3 Wochen"))
        self.assertEqual(60, _spoken_since_days("seit 2 Monaten"))
        self.assertEqual(90, _spoken_since_days("seit 3 Monate"))
        self.assertEqual(365, _spoken_since_days("seit 1 Jahr"))
        self.assertEqual(365, _spoken_since_days("seit einem Jahr"))

    def test_a_spoken_duration_is_found_without_the_word_seit(self):
        from core.cannanas_host import _spoken_since_days

        self.assertEqual(21, _spoken_since_days("3 Wochen in Bluete"))

    def test_a_spoken_duration_is_found_in_any_case(self):
        from core.cannanas_host import _spoken_since_days

        self.assertEqual(21, _spoken_since_days("SEIT 3 WOCHEN"))

    def test_a_status_without_a_duration_stays_a_plain_string(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #3", "status": "Bluete"},
            self.BINDING)
        self.assertEqual("Bluete", args["fields"]["status"])

    def test_a_batch_update_uses_the_same_duration(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_batch_update",
            {"plant_name": "Ztilton #6", "status": "seit 3 Wochen in Bluete"},
            self.BINDING)
        self.assertEqual(
            {"status": "Bluete", "since_days": 21}, args["fields"]["status"])

    def test_a_text_without_a_duration_has_no_days(self):
        from core.cannanas_host import _spoken_since_days

        self.assertIsNone(_spoken_since_days("Bluete"))
        self.assertIsNone(_spoken_since_days(""))

    def test_the_status_word_is_separated_from_the_duration(self):
        """'seit 3 Wochen in Bluete' is a status plus a duration."""
        from core.cannanas_host import _spoken_status_with_duration

        self.assertEqual(
            ("Bluete", 21),
            _spoken_status_with_duration("seit 3 Wochen in Bluete"))
        self.assertEqual(
            ("Bluete", None), _spoken_status_with_duration("Bluete"))



    def test_the_reserve_marker_is_forwarded_as_a_change(self):
        """'In reserve' is a change the adapter knows: it must not be lost.

        The host does not translate it — the adapter turns it into the tag
        the API knows. A dropped marker would leave the grower believing
        the plant is in reserve while nothing changed.
        """
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #1", "fields": {"in_reserve": True}},
            self.BINDING)
        self.assertEqual(True, args["fields"]["in_reserve"])

    def test_the_reserve_marker_is_also_spoken_as_a_flat_parameter(self):
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #1", "in_reserve": True},
            self.BINDING)
        self.assertEqual(True, args["fields"]["in_reserve"])

    def test_a_plant_leaving_the_reserve_keeps_the_spoken_false(self):
        """'false' is a change, not an absence: the marker must be removed."""
        from core.cannanas_host import prepare_tool_call

        _tool, args = prepare_tool_call(
            "prepare_plant_update",
            {"plant_name": "Ztilton #1", "fields": {"in_reserve": False}},
            self.BINDING)
        self.assertEqual(False, args["fields"]["in_reserve"])

    def test_the_reserve_marker_is_a_spoken_plant_change_parameter(self):
        """The model may only speak what the host forwards."""
        from core.cannanas_host import CANNANAS_PLANT_CHANGE_PARAMETERS

        self.assertIn("in_reserve", CANNANAS_PLANT_CHANGE_PARAMETERS)

if __name__ == '__main__':
    unittest.main()