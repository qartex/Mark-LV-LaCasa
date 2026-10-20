"""End-to-end voice path of the closed Cannanas action enum.

The chain under test is the real one:

    model tool call -> bridge tool 'cannanas' -> runner -> cannanas plugin
    -> host.prepare -> adapter prepare_* -> preview -> spoken answer

Nothing on that path is mocked: the real bridge tool declaration, the real
runner, the real plugin, the real host and the real parameter mapping run.
Only the two outside actors are replaced — the MCP child process (the pinned
adapter) by a recording fake and Gemini by one canned function call.

The fake adapter refuses every ``confirm_*`` call: a broken fence fails the
test instead of writing. No real Cannanas id, club id, key or secret appears
here — every identifier is an obvious dummy.
"""
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import core.cannanas_host as cannanas_host  # noqa: E402
import esp32_jarvis_bridge as bridge  # noqa: E402
from core.cannanas_actions import CANNANAS_MODEL_ACTIONS  # noqa: E402
from core.cannanas_audit import CannanasAudit  # noqa: E402
from core.turn_provenance import CHANNEL_ESP32_PCM, IngressEnvelope  # noqa: E402
from core.user_turn import finalize_turn  # noqa: E402

DEVICE_ID = 'esp32-growroom-voice-1'
AUTH_SESSION = 'auth-digest-dummy'
GEMINI_KEY = 'TEST-GEMINI-KEY-DUMMY-NOT-REAL'

HOST_ENV = {
    'JARVIS_CANNANAS_ADAPTER_COMMAND':
        '/opt/cannanas/adapter/venv/bin/cannanas-mcp-adapter',
    'JARVIS_CANNANAS_ADAPTER_ARGS': '',
    'JARVIS_CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
    'CANNANAS_API_KEY': 'dummy-not-real',
    'CANNANAS_CLUB_ID': 'club-dummy-not-real',
    'CANNANAS_STATE_DIR': 'SET-IN-TESTS',
    'CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
}


class FakeAdapter:
    """The pinned adapter as a recorder: prepares answer with a preview.

    The preview is built from the arguments the adapter received, so the
    spoken preview proves that the fields really travelled.
    """

    def __init__(self, allow_confirm=False):
        self.calls = []
        self.lock = threading.Lock()
        self.allow_confirm = allow_confirm
        self.results = {}

    def call(self, tool, arguments, budget=None):
        with self.lock:
            self.calls.append({'tool': tool, 'arguments': dict(arguments or {})})
        if str(tool).startswith('confirm_'):
            if not self.allow_confirm:
                raise AssertionError(
                    f'no confirm may run in the voice prepare path: {tool}')
            return {'ok': True, 'result_id': 'object-id-dummy'}
        if tool == 'get_adapter_info':
            return {'ok': True, 'config': {'boot_id': 'adapter-boot-dummy'}}
        if str(tool).startswith('prepare_'):
            return {
                'ok': True,
                'requires_confirmation': True,
                'confirmation_token': 'opaque-token-dummy',
                'payload_hash': 'payload-hash-dummy',
                'preview': self._preview(tool, arguments or {}),
            }
        return dict(self.results.get(tool, {'ok': True}))

    @staticmethod
    def _preview(tool, args):
        fields = dict(args.get('fields') or {})
        if tool == 'prepare_plant_update':
            return {'plant': args.get('plant'), **fields}
        if tool == 'prepare_batch_update':
            return {'batch': args.get('batch'), **fields}
        if tool == 'prepare_batch_plant_names':
            return {'batch': args.get('batch'),
                    'namen': [f'{args.get("prefix") or args.get("batch")} (1)']}
        if tool == 'prepare_zone_update':
            return {'zone': args.get('zone'), **fields}
        if tool == 'prepare_diary_create':
            target = args.get('room') or args.get('plant') or args.get('batch')
            return {'referenz': target, 'typ': 'Raum/Zone',
                    'text': args.get('text', '')}
        if tool == 'prepare_plant_create':
            preview = {'plant_name': args.get('plant_name'),
                       'room': args.get('room'), 'strain': args.get('strain'),
                       'status': args.get('status')}
            for key in ('mother_plant', 'is_mother', 'plant_count', 'notes'):
                if key in args:
                    preview[key] = args[key]
            return preview
        if tool == 'prepare_batch_create':
            preview = {'room': args.get('room'), 'strain': args.get('strain'),
                       'plant_count': args.get('plant_count'),
                       'status': args.get('status')}
            if 'mother_plants' in args:
                preview['mother_plants'] = args['mother_plants']
            return preview
        if tool == 'prepare_harvest_create':
            return {'plants': args.get('plants'), 'batches': args.get('batches'),
                    'notes': args.get('notes'),
                    'harvested_at': args.get('harvested_at')}
        if tool == 'prepare_waste_disposal_create':
            return {'plants': args.get('plants'), 'batches': args.get('batches'),
                    'notes': args.get('notes')}
        if tool == 'prepare_strain_create':
            return {'name': args.get('name'), 'notes': args.get('notes', '')}
        return {}


class FakeGeminiTypes:
    """The parts of google.genai.types the bridge answer path uses."""

    class FunctionDeclaration:
        def __init__(self, **kwargs):
            self.name = kwargs.get('name')
            self.description = kwargs.get('description')
            self.parameters_json_schema = kwargs.get('parameters_json_schema')

    class Tool:
        def __init__(self, function_declarations):
            self.function_declarations = function_declarations

    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AutomaticFunctionCallingConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def fake_genai_modules(response, requests):
    """A google.genai stand-in that answers with one canned function call."""
    types = FakeGeminiTypes

    class _Models:
        def generate_content(self, model, contents, config):
            requests.append({'model': model, 'config': config})
            return response

    class _Client:
        def __init__(self, **kwargs):
            self.models = _Models()

    module = types_module('google.genai')
    module.Client = _Client
    module.types = types
    google = types_module('google')
    google.genai = module
    return google, module


def types_module(name):
    return __import__('types').ModuleType(name)


def function_call_response(name, arguments):
    """The model answer: one function call, no free text."""
    return SimpleNamespace(
        text=None,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(function_call=SimpleNamespace(
                name=name, args=arguments)),
        ]))],
    )


class VoicePathFixture(unittest.TestCase):
    """One real host behind the real bridge runner, a fake adapter below."""

    ALLOW_CONFIRM = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = FakeAdapter(allow_confirm=self.ALLOW_CONFIRM)
        env = dict(HOST_ENV)
        env['JARVIS_CANNANAS_AUDIT_FILE'] = str(self.root / 'host-audit.jsonl')
        env['CANNANAS_STATE_DIR'] = str(self.root / 'state')
        env['CANNANAS_AUDIT_FILE'] = str(self.root / 'adapter-audit.jsonl')
        self.host = cannanas_host.CannanasHost(
            env=env, client=self.adapter,
            audit=CannanasAudit(env['JARVIS_CANNANAS_AUDIT_FILE']))
        # The plugin resolves the host through the process singleton.
        self._previous_host = cannanas_host._HOST
        cannanas_host.set_host(self.host)
        self.addCleanup(cannanas_host.set_host, self._previous_host)
        self.turn = self.make_turn(user_turn_id=1)
        self.engine = bridge.GeminiTurnEngine(
            GEMINI_KEY, cannanas_runner=bridge._optional_cannanas_runner())
        self.engine.set_turn_context(self.turn)

    def make_turn(self, user_turn_id=1, device=DEVICE_ID):
        envelope = IngressEnvelope(
            stable_device_id=device, auth_session_id=AUTH_SESSION,
            capture_id='session:conversation:1', user_turn_id=user_turn_id,
            channel=CHANNEL_ESP32_PCM, final=True)
        return finalize_turn(envelope)

    # -- the model path ---------------------------------------------------

    def call_cannanas(self, arguments):
        """The model calls the 'cannanas' tool; return the spoken answer."""
        response = function_call_response('cannanas', arguments)
        requests = []
        google, module = fake_genai_modules(response, requests)
        with unittest.mock.patch.dict(sys.modules, {'google': google,
                                                    'google.genai': module}):
            answer = self.engine._answer_on_key(module, b'\x00\x01')
        self.requests = requests
        return answer

    def offered_tools(self):
        """The declarations the model was offered in the last call."""
        config = self.requests[-1]['config']
        declarations = []
        for tool in getattr(config, 'tools', None) or []:
            for declaration in tool.function_declarations:
                declarations.append(declaration)
        return {d.name: d for d in declarations}

    # -- the adapter side --------------------------------------------------

    def prepare_calls(self):
        return [c for c in self.adapter.calls if c['tool'].startswith('prepare_')]

    def prepare_arguments(self, tool):
        calls = [c for c in self.prepare_calls() if c['tool'] == tool]
        self.assertTrue(calls, f'no {tool} reached the adapter: '
                               f'{[c["tool"] for c in self.adapter.calls]}')
        return calls[0]['arguments']


class TestVoicePrepareReachesTheAdapter(VoicePathFixture):
    """Every spoken change survives the whole voice path."""

    def test_plant_update_forwards_the_spoken_fields(self):
        answer = self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #6',
            'fields': {'is_mother': True, 'notes': 'Mutter'},
        })
        args = self.prepare_arguments('prepare_plant_update')
        self.assertEqual('Ztilton #6', args['plant'])
        self.assertTrue(args['fields']['is_mother'])
        self.assertEqual('Mutter', args['fields']['notes'])
        # The preview the speaker hears names the plant and the field.
        self.assertIn('Ztilton #6', answer)
        self.assertIn('is_mother', answer)
        self.assertIn('bestätigen', answer.lower())

    def test_batch_update_forwards_room_and_status(self):
        self.call_cannanas({
            'action': 'prepare_batch_update',
            'plant_name': 'Ztilton Woche 4',
            'room': 'Helsinki',
            'status': 'Blüte',
        })
        args = self.prepare_arguments('prepare_batch_update')
        self.assertEqual('Ztilton Woche 4', args['batch'])
        self.assertEqual('Helsinki', args['fields']['room'])
        self.assertEqual('Blüte', args['fields']['status'])

    def test_zone_update_forwards_zone_and_fields(self):
        self.call_cannanas({
            'action': 'prepare_zone_update',
            'zone': 'Madrid',
            'fields': {'notes': 'Lampe auf 18 Uhr'},
        })
        args = self.prepare_arguments('prepare_zone_update')
        self.assertEqual('Madrid', args['zone'])
        self.assertEqual('Lampe auf 18 Uhr', args['fields']['notes'])

    def test_diary_create_forwards_room_and_text(self):
        self.call_cannanas({
            'action': 'prepare_diary_create',
            'room': 'Madrid',
            'text': 'Heute gegossen.',
        })
        args = self.prepare_arguments('prepare_diary_create')
        self.assertEqual('Madrid', args['room'])
        self.assertEqual('Heute gegossen.', args['text'])

    def test_harvest_create_forwards_the_plants(self):
        self.call_cannanas({
            'action': 'prepare_harvest_create',
            'plants': 'Ztilton #6',
        })
        args = self.prepare_arguments('prepare_harvest_create')
        self.assertEqual('Ztilton #6', args['plants'])

    def test_plant_create_forwards_mother_plant_and_count(self):
        self.call_cannanas({
            'action': 'prepare_plant_create',
            'plant_name': 'Ztilton #8',
            'room': 'Madrid',
            'strain': 'Ztilton',
            'status': 'CUTTING',
            'mother_plant': 'Ztilton #3',
            'plant_count': 5,
        })
        args = self.prepare_arguments('prepare_plant_create')
        self.assertEqual('Ztilton #3', args['mother_plant'])
        self.assertEqual(5, args['plant_count'])
        self.assertEqual('Ztilton', args['strain'])
        self.assertEqual('Madrid', args['room'])

    def test_batch_create_forwards_the_mother_plants(self):
        mothers = [{'plant': 'Ztilton #3', 'cutting_count': 4}]
        self.call_cannanas({
            'action': 'prepare_batch_create',
            'room': 'Madrid',
            'strain': 'Ztilton',
            'status': 'CUTTING',
            'plant_count': 4,
            'mother_plants': mothers,
        })
        args = self.prepare_arguments('prepare_batch_create')
        self.assertEqual(mothers, args['mother_plants'])
        self.assertEqual(4, args['plant_count'])
        self.assertEqual('Madrid', args['room'])
        self.assertEqual('Ztilton', args['strain'])

    def test_waste_disposal_create_forwards_the_plants(self):
        self.call_cannanas({
            'action': 'prepare_waste_disposal_create',
            'plants': 'Ztilton #6',
        })
        args = self.prepare_arguments('prepare_waste_disposal_create')
        self.assertEqual('Ztilton #6', args['plants'])

    def test_the_naming_reaches_the_adapter_with_the_batch(self):
        """'Name the plants of the batch' travels with the batch label."""
        self.call_cannanas({
            'action': 'prepare_batch_plant_names',
            'plant_name': 'Ztilton #6',
        })
        args = self.prepare_arguments('prepare_batch_plant_names')
        self.assertEqual('Ztilton #6', args['batch'])
        # Without a spoken word the batch label is the base of the names.
        self.assertEqual('', args['prefix'])

    def test_the_naming_takes_the_spoken_word_as_the_prefix(self):
        """A spoken word becomes the base name of the new names."""
        self.call_cannanas({
            'action': 'prepare_batch_plant_names',
            'plant_name': 'Ztilton #6',
            'strain': 'Ztilton',
        })
        args = self.prepare_arguments('prepare_batch_plant_names')
        self.assertEqual('Ztilton #6', args['batch'])
        self.assertEqual('Ztilton', args['prefix'])


class TestTheTargetNameSurvives(VoicePathFixture):
    """The name of the object to change is never lost on the way."""

    def test_the_target_named_as_plant_reaches_the_adapter(self):
        self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant': 'Ztilton #6',
            'fields': {'is_mother': True},
        })
        args = self.prepare_arguments('prepare_plant_update')
        self.assertEqual('Ztilton #6', args['plant'])

    def test_the_target_named_as_query_reaches_the_adapter(self):
        # For an update the search term IS the plant to change.
        self.call_cannanas({
            'action': 'prepare_plant_update',
            'query': 'Ztilton #6',
            'status': 'Blüte',
        })
        args = self.prepare_arguments('prepare_plant_update')
        self.assertEqual('Ztilton #6', args['plant'])
        self.assertEqual('Blüte', args['fields']['status'])

    def test_the_batch_named_as_batch_reaches_the_adapter(self):
        self.call_cannanas({
            'action': 'prepare_batch_update',
            'batch': 'Ztilton Woche 4',
            'room': 'Helsinki',
        })
        args = self.prepare_arguments('prepare_batch_update')
        self.assertEqual('Ztilton Woche 4', args['batch'])
        self.assertEqual('Helsinki', args['fields']['room'])

    def test_the_flat_change_parameters_reach_the_adapter(self):
        # Spoken as flat parameters, not inside "fields": they are changes
        # all the same.
        self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #6',
            'is_mother': True,
            'parent_plant': 'Ztilton #3',
            'description': 'Zweite Mutter',
        })
        args = self.prepare_arguments('prepare_plant_update')
        self.assertTrue(args['fields']['is_mother'])
        self.assertEqual('Ztilton #3', args['fields']['parent_plant'])
        self.assertEqual('Zweite Mutter', args['fields']['description'])

    def test_the_diary_text_is_the_spoken_text(self):
        self.call_cannanas({
            'action': 'prepare_diary_create',
            'room': 'Madrid',
            'text': 'Heute gegossen.',
            'query': 'Ztilton',
        })
        args = self.prepare_arguments('prepare_diary_create')
        self.assertEqual('Heute gegossen.', args['text'])
        self.assertEqual('Madrid', args['room'])

    def test_the_diary_text_is_never_the_strain(self):
        # The strain is a different thing: it must not become the diary
        # text of a room diary.
        self.call_cannanas({
            'action': 'prepare_diary_create',
            'room': 'Madrid',
            'strain': 'Ztilton',
        })
        args = self.prepare_arguments('prepare_diary_create')
        self.assertNotEqual('Ztilton', args['text'])
        self.assertEqual('', args['text'])


class TestVoicePrepareIsFenced(VoicePathFixture):
    """The closed enum and the host bookkeeping stay host-side."""

    def test_an_action_outside_the_enum_is_refused_without_any_call(self):
        for action in ('confirm_plant_update', 'delete_plant', 'cancel_pending',
                       'cancel_plant_update', 'cancel_confirmation',
                       'cancel_pending_confirmation', 'confirm_pending',
                       'confirm_diary_create', 'mcp__find_plants',
                       'raw_mcp_call', 'prepare_destructive_purge', ''):
            answer = self.call_cannanas({'action': action})
            self.assertIn('nicht freigegeben', answer, action)
            self.assertEqual([], self.adapter.calls, action)

    def test_a_host_only_parameter_never_reaches_the_adapter(self):
        self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #6',
            'fields': {
                'is_mother': True,
                'club_id': 'club-dummy-not-real',
                'confirm_token': 'token-dummy-not-real',
                'confirmation_token': 'token-dummy-not-real',
                'payload_hash': 'hash-dummy-not-real',
            },
            'club_id': 'club-dummy-not-real',
            'confirm_token': 'token-dummy-not-real',
            'confirmation_token': 'token-dummy-not-real',
            'payload_hash': 'hash-dummy-not-real',
        })
        args = self.prepare_arguments('prepare_plant_update')
        for name in ('club_id', 'confirm_token', 'confirmation_token',
                     'payload_hash', 'opaque_mcp_token'):
            self.assertNotIn(name, args, name)
            self.assertNotIn(name, args['fields'], name)
        self.assertEqual({'is_mother': True}, args['fields'])
        # The binding is the host's own provenance: it carries the device
        # and turn identity, never a club id or a confirmation token.
        binding = args['binding']
        self.assertEqual(DEVICE_ID, binding['stable_device_id'])
        for name in ('club_id', 'confirmation_token', 'opaque_mcp_token',
                     'payload_hash'):
            self.assertNotIn(name, binding, name)

    def test_the_preview_arms_the_confirmation_without_writing(self):
        self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #6',
            'fields': {'is_mother': True},
        })
        pending = self.host.coordinator.current(DEVICE_ID)
        self.assertIsNotNone(pending)
        self.assertEqual('pending', pending.state)
        self.assertEqual('plant_update', pending.action)
        # The whole voice path prepared exactly once and never confirmed.
        self.assertEqual(['get_adapter_info', 'prepare_plant_update'],
                         [c['tool'] for c in self.adapter.calls])

    def test_a_read_action_answers_without_a_preview(self):
        self.adapter.results['find_plants'] = {
            'ok': True,
            'plants': [{'name': 'Ztilton #6', 'status': 'SEED'}],
            'exact': True,
        }
        answer = self.call_cannanas({
            'action': 'find_plants',
            'query': 'Ztilton',
        })
        self.assertIn('Ztilton #6', answer)
        self.assertEqual(['find_plants'], [c['tool'] for c in self.adapter.calls])
        # A read never arms a confirmation.
        self.assertIsNone(self.host.coordinator.current(DEVICE_ID))

    # ── the watering interval (Task 7) ───────────────────────────────────
    def test_the_watering_interval_reaches_the_adapter_and_is_spoken(self):
        """The whole voice path: the room travels, the days are spoken."""
        self.adapter.results['watering_intervals'] = {
            'ok': True,
            'raeume': [{'raum': 'Madrid', 'tage_seit_letztem_gies': 9,
                        'letzter_eintrag': 'Giessen'}],
            'hints': {'giessen': ['Madrid']},
        }
        answer = self.call_cannanas({
            'action': 'watering_intervals',
            'room': 'Madrid',
        })
        self.assertEqual(['watering_intervals'],
                         [c['tool'] for c in self.adapter.calls])
        self.assertEqual({'room': 'Madrid'},
                         self.adapter.calls[0]['arguments'])
        self.assertIn('Madrid', answer)
        self.assertIn('9', answer)
        # A read never arms a confirmation.
        self.assertIsNone(self.host.coordinator.current(DEVICE_ID))


class TestTheSpokenYesConfirmsThePreview(VoicePathFixture):
    """The second half of the voice path: preview, answer, 'ja', confirm."""

    ALLOW_CONFIRM = True

    def test_the_yes_after_the_preview_writes_exactly_once(self):
        # No diary workflow: the host owns the pending state, so the
        # spoken "ja" confirms a preview of the closed action enum too.
        self.engine.set_turn_context(self.make_turn(user_turn_id=1))
        answer = self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #6',
            'fields': {'is_mother': True},
        })
        self.assertIn('Ztilton #6', answer)
        # The pending is armed: the next turn is decided by the host
        # grammar, not by the model.
        self.assertTrue(self.engine.has_pending_confirmation())
        self.engine.set_turn_context(self.make_turn(user_turn_id=2))
        spoken = self.engine.evaluate_pending_confirmation('ja')
        self.assertIn('Erledigt', spoken)
        tools = [c['tool'] for c in self.adapter.calls]
        self.assertEqual('confirm_plant_update', tools[-1])
        self.assertEqual(1, tools.count('confirm_plant_update'))
        # The confirm carried the host's own binding, never a spoken one.
        confirm_args = self.adapter.calls[-1]['arguments']
        self.assertEqual(DEVICE_ID, confirm_args['binding']['stable_device_id'])
        self.assertNotIn('club_id', confirm_args)

    def test_the_abort_after_the_preview_writes_nothing(self):
        self.engine.set_turn_context(self.make_turn(user_turn_id=1))
        self.call_cannanas({
            'action': 'prepare_plant_update',
            'plant_name': 'Ztilton #6',
            'fields': {'is_mother': True},
        })
        self.engine.set_turn_context(self.make_turn(user_turn_id=2))
        spoken = self.engine.evaluate_pending_confirmation('abbrechen')
        self.assertIn('Abgebrochen', spoken)
        self.assertEqual([], [c['tool'] for c in self.adapter.calls
                              if c['tool'].startswith('confirm_')])


class TestTheModelFacingContract(VoicePathFixture):
    """What the model is offered: the closed enum and the spoken parameters."""

    def test_the_offered_tool_carries_the_closed_action_enum(self):
        self.call_cannanas({'action': 'overview', 'query': 'Ztilton'})
        declaration = self.offered_tools()['cannanas']
        enum = declaration.parameters_json_schema['properties']['action']['enum']
        self.assertEqual(sorted(CANNANAS_MODEL_ACTIONS), sorted(enum))
        # The count is derived: the enum grows with every new tool, a
        # written-down number would only be a race.
        self.assertEqual(len(CANNANAS_MODEL_ACTIONS), len(enum))
        self.assertIn('prepare_batch_plant_names', enum)
        for forbidden in ('confirm_plant_update', 'confirm_diary_create',
                          'cancel_pending', 'delete_plant'):
            self.assertNotIn(forbidden, enum)

    def test_every_spoken_parameter_is_model_facing(self):
        self.call_cannanas({'action': 'overview'})
        schema = self.offered_tools()['cannanas'].parameters_json_schema
        properties = set(schema['properties'])
        for parameter in cannanas_host.CANNANAS_SPOKEN_PARAMETERS:
            self.assertIn(parameter, properties, parameter)

    def test_the_schema_offers_nothing_the_host_does_not_read(self):
        self.call_cannanas({'action': 'overview'})
        schema = self.offered_tools()['cannanas'].parameters_json_schema
        for parameter in schema['properties']:
            self.assertIn(parameter, cannanas_host.CANNANAS_SPOKEN_PARAMETERS,
                          parameter)

    def test_the_system_instruction_names_the_grow_capabilities(self):
        instruction = bridge.DEFAULT_SYSTEM_INSTRUCTION
        for phrase in ('Status', 'Charge', 'Mutterpflanze', 'Ernte',
                       'Tagebuch', 'Verschmutzung', 'Zone'):
            self.assertIn(phrase, instruction, phrase)
