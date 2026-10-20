"""Host-side Cannanas wiring: boot identity, confirmation grammar, lifecycle.

This module owns everything the model never sees: configuration checks (fail
before network I/O, naming only missing environment variables), the adapter
client lifecycle with the pinned release, the pending-confirmation
coordinator, the deterministic German confirmation grammar, the host audit,
and degraded readiness — an adapter failure never exits JARVIS, Spotify or
Home Assistant.

No network imports here: the coordinator/grammar are pure host state
machines and the MCP client imports its SDK only when the child starts.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

from core.cannanas_audit import CannanasAudit, CannanasAuditClosed
from core.cannanas_client import (
    ADAPTER_TOOLS,
    PINNED_ADAPTER_VERSION,
    PINNED_WHEEL_SHA256,
    CannanasClient,
    CannanasPreSendRefusal,
    CannanasUnavailable,
    child_environment,
)
from core.cannanas_confirmation import (
    ConfirmationCoordinator,
    DEFAULT_PENDING_TTL_SECONDS,
    OpaqueToken,
    PendingConfirmation,
)

# ── boot identity ──────────────────────────────────────────────────────────

# Fresh per JARVIS process: supplied with every prepare/confirm as
# adapter_boot_id so a restart fences ordinary pending confirmations.
_ADAPTER_BOOT_ID = None


def adapter_boot_id() -> str:
    """Process-lifetime boot identity (uuid4), identical within one process."""
    global _ADAPTER_BOOT_ID
    if _ADAPTER_BOOT_ID is None:
        _ADAPTER_BOOT_ID = uuid.uuid4().hex
    return _ADAPTER_BOOT_ID


# ── deterministic, bounded German confirmation grammar ─────────────────────
# Exact bounded forms only. Everything outside these sets is 'none' — no fuzzy
# matching, no model interpretation. The grammar decision belongs to the host,
# never to Gemini.
_AFFIRM_FORMS = frozenset({
    'ja', 'ok', 'okay', 'bestätigen', 'bestätige', 'bitte bestätigen',
    'ja bitte', 'speichern', 'speichere',
})
_DENY_FORMS = frozenset({
    'nein', 'abbrechen', 'brich ab', 'abbruch', 'stop', 'stopp', 'stoppe',
    'stoppen',
})
_TRAILING_PUNCTUATION = '.,!?:;'


def _normalize(text) -> str:
    if not isinstance(text, str):
        return ''
    text = text.strip().lower()
    # Trim trailing punctuation only; never fuzzy-match inside the utterance.
    return text.rstrip(_TRAILING_PUNCTUATION)


def confirmation_grammar(text) -> str:
    """{'affirm' | 'deny' | 'none'} for one finalized utterance."""
    normalized = _normalize(text)
    if not normalized:
        return 'none'
    if normalized in _AFFIRM_FORMS:
        return 'affirm'
    if normalized in _DENY_FORMS:
        return 'deny'
    return 'none'


# ── configuration (host env) ───────────────────────────────────────────────

ENV_ADAPTER_COMMAND = 'JARVIS_CANNANAS_ADAPTER_COMMAND'
ENV_ADAPTER_ARGS = 'JARVIS_CANNANAS_ADAPTER_ARGS'
ENV_HOST_AUDIT = 'JARVIS_CANNANAS_AUDIT_FILE'
ADAPTER_ENV_VARS = ('CANNANAS_API_KEY', 'CANNANAS_CLUB_ID',
                    'CANNANAS_STATE_DIR', 'CANNANAS_AUDIT_FILE')
REQUIRED_ENV_VARS = (ENV_ADAPTER_COMMAND, ENV_HOST_AUDIT) + ADAPTER_ENV_VARS

MAX_RECORDS = 25
SPOKEN_RECORD_LIMIT = 5

_UNAVAILABLE = 'Cannanas ist derzeit nicht verfügbar.'
_NOT_CONFIRMABLE_SHORT = ('Die Vorschau ist nicht mehr gültig. '
                          'Bitte erneut vorbereiten.')
_CONFIRM_INSTRUCTION = ('Bitte bestätigen Sie mit einem klaren „ja“, '
                        'oder sagen Sie „abbrechen“.')

REFUSAL_SPOKEN = {
    'wrong_session': _NOT_CONFIRMABLE_SHORT,
    'wrong_device': ('Diese Vorschau gehört zu einem anderen Gerät. '
                     'Bitte erneut auf demselben Gerät bestätigen.'),
    'wrong_auth_session': _NOT_CONFIRMABLE_SHORT,
    'wrong_turn': ('Die Bestätigung kommt nicht nach der Vorschau. '
                   'Bitte erneut vorbereiten.'),
    'wrong_boot': _NOT_CONFIRMABLE_SHORT,
    'wrong_payload': _NOT_CONFIRMABLE_SHORT,
    'expired': _NOT_CONFIRMABLE_SHORT,
    'already_used': ('Diese Vorschau wurde bereits bearbeitet. '
                     'Bitte erneut vorbereiten.'),
    'cancelled': ('Die Vorschau wurde abgebrochen. Bitte erneut vorbereiten.'),
}


def missing_config_variables(env=None) -> tuple:
    """The missing variable NAMES in a fixed order — values never leak."""
    source = os.environ if env is None else env
    return tuple(name for name in REQUIRED_ENV_VARS if not source.get(name))


def _unavailability_text(missing) -> str:
    if missing:
        return (f'Cannanas ist derzeit nicht verfügbar. '
                f'Es fehlt: {", ".join(missing)}.')
    return _UNAVAILABLE


def prepare_binding_for(turn, adapter_boot: str | None = None) -> dict | None:
    """The adapter binding from a finalized turn; None without a device.

    ``adapter_boot`` must be the adapter process's own boot id: the adapter
    rejects a confirm whose binding carries any other one. The host process id
    is only the last-resort fence when the adapter never reported its own.
    """
    stable_device_id = getattr(turn, 'stable_device_id', None)
    if not stable_device_id:
        return None
    return {
        'jarvis_session_id': stable_device_id,
        'stable_device_id': stable_device_id,
        'auth_session_id': getattr(turn, 'auth_session_id', ''),
        'prepared_turn_id': str(getattr(turn, 'user_turn_id', '')),
        'adapter_boot_id': adapter_boot or adapter_boot_id(),
    }


# ── bounded, secret-free record projection ─────────────────────────────────

_UNSAFE_KEY_MARKERS = ('token', 'secret', 'password', 'bearer', 'authorization')


def _compact_record(record) -> dict:
    if not isinstance(record, dict):
        return {}
    safe = {}
    for key, value in record.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _UNSAFE_KEY_MARKERS):
            continue
        safe[key] = value
    return safe


_RESULT_RECORD_KEYS = (
    'plants', 'zones', 'strains', 'batches', 'materials',
    'diaries', 'harvests', 'mother_plants',
)


def _result_records(result) -> list:
    for key in _RESULT_RECORD_KEYS:
        records = result.get(key)
        if isinstance(records, list):
            return records
    return []


def build_read_summary(prefix, records, exact, truncated) -> str:
    """Speak at most five records; qualify unproven totals ('mindestens')."""
    names = ', '.join(
        str(record.get('name') or record.get('room') or '?')
        for record in records[:SPOKEN_RECORD_LIMIT])
    if not names:
        return f'{prefix}: keine Einträge.'
    spoken = f'{prefix}: {names}.'
    extra = ''
    if len(records) > SPOKEN_RECORD_LIMIT:
        extra += (f' (Erste {SPOKEN_RECORD_LIMIT} von {len(records)} — '
                  'weitere auf Anfrage.)')
    if truncated:
        extra += ' (Liste gekürzt.)'
    if not exact:
        spoken = f'{prefix} (mindestens {len(records)}): {names}.'
    spoken += extra
    return spoken


def _record_payload(prefix, result, exact, truncated) -> dict:
    records = [_compact_record(record) for record in _result_records(result)]
    return {
        'ok': True,
        'summary': build_read_summary(prefix, records, exact, truncated),
        'records': records[:MAX_RECORDS],
        'spoken_records': min(SPOKEN_RECORD_LIMIT, len(records)),
        'total_records': len(records),
        'exact': bool(exact),
        'truncated': bool(truncated),
        'requires_confirmation': False,
    }


# ── adapter tool mapping (read + prepare) ──────────────────────────────────

def _int_param(params, key, default, maximum=MAX_RECORDS):
    try:
        value = int(params.get(key))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def read_tool_call(action: str, params: dict) -> 'tuple[str, dict, str]':
    """Map the closed model action to (adapter tool, adapter args, spoken prefix)."""
    params = params or {}
    query = str(params.get('query') or '').strip()
    room = str(params.get('room') or '').strip()
    status = str(params.get('status') or '').strip()
    if action == 'overview':
        return 'get_cannanas_overview', {}, 'Übersicht'
    if action == 'find_zones':
        return 'find_zones', {'query': query,
                              'limit': _int_param(params, 'count', 20)}, 'Räume'
    if action == 'find_strains':
        return 'find_strains', {'query': query,
                                'limit': _int_param(params, 'count', 20)}, 'Sorten'
    if action == 'find_plants':
        return 'find_plants', {'query': query, 'room': room,
                               'limit': _int_param(params, 'count', 20)}, 'Pflanzen'
    if action == 'find_propagation_material':
        return ('find_propagation_material',
                {'query': query, 'limit': _int_param(params, 'count', 20)},
                'Vermehrungsmaterial')
    if action == 'list_recent_batches':
        return ('list_recent_batches',
                {'limit': _int_param(params, 'count', 10)}, 'Chargen')
    if action == 'list_plants':
        return 'list_plants', {'room': room, 'status': status,
                               'limit': _int_param(params, 'count', 50)}, 'Pflanzen'
    if action == 'count_plants_by_room':
        return 'count_plants_by_room', {'room': room}, 'Pflanzen je Raum'
    if action == 'zone_growth_summary':
        return ('zone_growth_summary',
                {'limit': _int_param(params, 'count', 50, maximum=100)}, 'Räume')
    if action == 'list_mother_plants':
        return ('list_mother_plants',
                {'room': room, 'limit': _int_param(params, 'count', 50)},
                'Mutterpflanzen')
    if action == 'list_diaries':
        return ('list_diaries',
                {'room': room, 'plant': query,
                 'batch': str(params.get('batch') or ''),
                 'limit': _int_param(params, 'count', 25)}, 'Tagebücher')
    if action == 'list_plant_diaries':
        return ('list_plant_diaries',
                {'plant': query, 'limit': _int_param(params, 'count', 10)},
                'Tagebuch')
    if action == 'list_harvests':
        return ('list_harvests',
                {'limit': _int_param(params, 'count', 10)}, 'Ernten')
    if action == 'get_grow_config':
        return 'get_grow_config', {}, 'Grow-Einstellungen'
    if action == 'zone_plant_status':
        return 'zone_plant_status', {'room': room}, 'Pflanzenstatus'
    raise ValueError(f'unknown read action: {action}')


# ── spoken values (host → adapter) ────────────────────────────────────────
#
# The pinned adapter owns the field whitelist: it rejects an unknown field
# name and it resolves the spoken names "room", "strain" and "parent_plant"
# to canonical ids itself. The host therefore forwards the spoken names
# unchanged, keeps every set value and never invents a field the adapter
# does not know.


def _is_spoken_value(value) -> bool:
    """A set spoken value: not None, not empty text, not an empty container.

    A spoken False or 0 is a value and stays — "is_mother: false" is a
    change, not an absence.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _spoken_value(params: dict, key: str, default=None):
    """One spoken value: the flat parameter first, then the nested fields."""
    value = params.get(key)
    if not _is_spoken_value(value):
        nested = params.get('fields')
        if isinstance(nested, dict):
            value = nested.get(key)
    return value if _is_spoken_value(value) else default


def _spoken_target(params: dict, *keys: str) -> str:
    """The name of the object to change: the first spoken name that fits.

    The model may name the target as "plant_name", "plant", "batch",
    "plants" or "query" — the search term is the same object for an
    update, not a second field. Every spoken name is taken, so the target
    never goes missing on the way to the adapter.
    """
    for key in keys:
        value = params.get(key)
        if _is_spoken_value(value) and isinstance(value, str):
            spoken = value.strip()
            if spoken:
                return spoken
    return ''


def _spoken_count(value):
    """A spoken count as an int; never silently changed.

    The adapter validates the range (1–100 plants, 1–1000 batch plants) and
    refuses an impossible count. Lowering or replacing it here would change
    what the grower said without telling anyone, so the number travels as
    spoken and the adapter decides.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


# Host-internal names are never a spoken change. They belong to the host
# bookkeeping (the club, the confirmation token, the payload hash) and must
# never reach the adapter as a field of the changed object.
_HOST_INTERNAL_NAMES = frozenset({
    'club_id',
    'confirmation_token',
    'confirm_token',
    'opaque_mcp_token',
    'payload_hash',
    'binding',
    'prepared_turn_id',
    'stable_device_id',
    'auth_session_id',
    'jarvis_session_id',
    'adapter_boot_id',
    'action',
    'behavior',
})


def _is_host_internal_name(name) -> bool:
    """True for a name the host owns and the adapter must never see."""
    return str(name).strip().lower() in _HOST_INTERNAL_NAMES


def _spoken_update_fields(params: dict, extra: dict | None = None) -> dict:
    """Every spoken change field: the flat parameters and the nested fields.

    Only set values are taken — never None, never an empty text, never an
    empty list. Nothing is dropped silently: what was spoken is forwarded
    under the name the adapter knows, and the adapter refuses a field it
    does not know. The nested "fields" object is the explicit change list
    and wins over the flat convenience parameters.

    A host-internal name (a club id, a confirmation token, a payload hash)
    is never a change: it is not a field of the object, so it is dropped
    here and can never travel to the adapter.
    """
    fields: dict = {}
    for key, value in (extra or {}).items():
        if _is_spoken_value(value) and not _is_host_internal_name(key):
            fields[key] = value
    nested = params.get('fields')
    if isinstance(nested, dict):
        for key, value in nested.items():
            name = str(key)
            if _is_spoken_value(value) and not _is_host_internal_name(name):
                fields[name] = value
    return fields


# ── model-facing spoken parameters (host → bridge function schema) ──────
#
# One source of truth: the bridge builds the model's function schema from
# this tuple. So the model can never be offered a parameter the host does
# not understand, and the host never silently drops a parameter the model
# was allowed to send.

CANNANAS_SPOKEN_PARAMETERS = (
    'action',
    'room',
    'query',
    'plant_name',
    'plant',
    'plants',
    'batch',
    'batches',
    'strain',
    'status',
    'count',
    'plant_count',
    'text',
    'notes',
    'date',
    'harvested_at',
    'zone',
    'is_mother',
    'mother_plant',
    'mother_plants',
    'parent_plant',
    'description',
    'measurements',
    'propagation_material',
    'expected_harvest_at',
    'expected_yield',
    'actual_yield',
    'substrate',
    'fertilizer',
    'archived',
    'fields',
)

# Flat change fields a plant update may carry next to the nested "fields"
# object. The pinned adapter owns the real whitelist and refuses a field it
# does not know; these are the names the model may also speak directly.
CANNANAS_PLANT_CHANGE_PARAMETERS = (
    'is_mother',
    'parent_plant',
    'description',
    'expected_harvest_at',
    'expected_yield',
    'actual_yield',
    'substrate',
    'fertilizer',
    'archived',
    'batch',
)

# The same for a zone: name and climate belong into the nested "fields"
# object, these are the spoken conveniences.
CANNANAS_ZONE_CHANGE_PARAMETERS = (
    'description',
    'notes',
)


def _refusal_text(result) -> str:
    """One bounded German line for a refused prepare; no internals."""
    reason = str(result.get('error') or '').strip()
    if not reason:
        reason = 'Die Angaben passen nicht zu dieser Aktion.'
    if len(reason) > 160:
        reason = reason[:160].rstrip() + '…'
    return f'Die Änderung wurde abgelehnt: {reason}'


def prepare_tool_call(action: str, params: dict, binding: dict) -> 'tuple[str, dict]':
    """Map the closed prepare action to (adapter tool, args incl. binding)."""
    from core.cannanas_actions import CANNANAS_PREPARE_ACTIONS
    params = params or {}
    # The named object. The create actions need an explicit plant name —
    # their "query" is the strain, not a plant. The update actions may also
    # name the target with the search term: there it IS the plant to
    # change, never a second field.
    explicit_plant = _spoken_target(params, 'plant_name', 'plant', 'plants')
    target_name = _spoken_target(params, 'plant_name', 'plant', 'plants',
                                 'query')
    room = str(params.get('room') or '').strip()
    status = str(params.get('status') or '').strip()
    # The create actions take the strain from the spoken "strain" or from
    # the search term; the update actions never read the search term as a
    # strain — there it is the name of the plant to change.
    strain = str(_spoken_value(params, 'strain', params.get('query')) or '').strip()
    spoken_strain = str(params.get('strain') or '').strip()
    notes = str(_spoken_value(params, 'notes', params.get('text')) or '').strip()
    spoken_date = str(params.get('date') or '').strip()
    # The spoken count travels as spoken: a missing count means one, an
    # impossible count is the adapter's to refuse.
    spoken_plant_count = _spoken_count(_spoken_value(params, 'plant_count',
                                                     params.get('count')))
    count = spoken_plant_count if spoken_plant_count is not None else 1
    if action not in CANNANAS_PREPARE_ACTIONS:
        raise ValueError(f'unknown prepare action: {action}')
    if action == 'prepare_plant_create':
        args = {'plant_name': explicit_plant, 'room': room, 'strain': strain,
                'status': status}
        if notes:
            args['notes'] = notes
        mother_plant = _spoken_value(params, 'mother_plant')
        if mother_plant is not None:
            args['mother_plant'] = str(mother_plant).strip()
        is_mother = _spoken_value(params, 'is_mother')
        if is_mother is not None:
            args['is_mother'] = is_mother
        if spoken_plant_count is not None:
            args['plant_count'] = spoken_plant_count
        return action, {**args, 'binding': binding}
    if action == 'prepare_strain_create':
        args: dict = {'name': explicit_plant or strain, 'identifier': ''}
        if notes:
            args['notes'] = notes
        description = _spoken_value(params, 'description')
        if description is not None:
            args['description'] = str(description).strip()
        return action, {**args, 'binding': binding}
    if action == 'prepare_batch_create':
        args = {'room': room, 'strain': strain, 'plant_count': count,
                'status': status}
        if notes:
            args['notes'] = notes
        for key in ('parent_plant', 'propagation_material',
                    'expected_harvest_at', 'cultivation_vegetative_date'):
            value = _spoken_value(params, key)
            if value is not None:
                args[key] = str(value).strip()
        mother_plants = _spoken_value(params, 'mother_plants')
        if mother_plants is not None:
            args['mother_plants'] = mother_plants
        return action, {**args, 'binding': binding}
    if action == 'prepare_plant_update':
        extra = {}
        if status:
            extra['status'] = status
        if notes:
            extra['notes'] = notes
        if room:
            extra['room'] = room
        if spoken_strain:
            extra['strain'] = spoken_strain
        if spoken_date:
            extra['expected_harvest_at'] = spoken_date
        # The flat change parameters are spoken changes too: without them
        # they would be lost between the model and the adapter. The nested
        # "fields" object still wins over the flat convenience parameters.
        for key in CANNANAS_PLANT_CHANGE_PARAMETERS:
            value = _spoken_value(params, key)
            if value is not None:
                extra[key] = value
        # The target name is not a change: the adapter rejects a "name"
        # field, so it never goes into the fields.
        fields = _spoken_update_fields(params, extra)
        return action, {'plant': target_name, 'fields': fields,
                        'binding': binding}
    if action == 'prepare_batch_update':
        extra = {}
        if status:
            extra['status'] = status
        if notes:
            extra['notes'] = notes
        if room:
            extra['room'] = room
        if spoken_date:
            extra['expected_harvest_at'] = spoken_date
        fields = _spoken_update_fields(params, extra)
        # The batch is named as "batch" or as the generic target name.
        batch_target = _spoken_target(params, 'batch') or target_name
        return action, {'batch': batch_target, 'fields': fields,
                        'binding': binding}
    if action == 'prepare_diary_create':
        measurements = _spoken_value(params, 'measurements')
        # The diary text is the spoken text — never the strain or the
        # search term. A diary without text is refused by the adapter.
        spoken_text = str(_spoken_value(params, 'text', notes) or '').strip()
        args = {'text': spoken_text, 'measurements': measurements,
                'binding': binding}
        batch_target = str(_spoken_value(params, 'batch') or '').strip()
        plant_target = str(_spoken_value(params, 'plant') or '').strip()
        if not plant_target:
            plant_target = explicit_plant
        if batch_target:
            args['batch'] = batch_target
        elif plant_target:
            args['plant'] = plant_target
        else:
            args['room'] = room
        return action, args
    if action == 'prepare_zone_update':
        zone_name = str(params.get('zone') or room or '').strip()
        extra = {}
        if notes:
            extra['notes'] = notes
        # The spoken zone changes are changes too: without them they would
        # be lost between the model and the adapter.
        for key in CANNANAS_ZONE_CHANGE_PARAMETERS:
            value = _spoken_value(params, key)
            if value is not None:
                extra[key] = value
        fields = _spoken_update_fields(params, extra)
        if not fields:
            raise ValueError('für eine Raumänderung wird ein Feld gebraucht')
        return action, {'zone': zone_name, 'fields': fields, 'binding': binding}
    if action == 'prepare_harvest_create':
        plants = str(_spoken_value(params, 'plants', explicit_plant) or '').strip()
        batches = str(_spoken_value(params, 'batches',
                                    params.get('batch')) or '').strip()
        harvested_at = str(_spoken_value(params, 'harvested_at',
                                         params.get('date')) or '').strip()
        return action, {
            'plants': plants,
            'batches': batches,
            'notes': notes,
            'harvested_at': harvested_at,
            'binding': binding,
        }
    if action == 'prepare_waste_disposal_create':
        plants = str(_spoken_value(params, 'plants', explicit_plant) or '').strip()
        batches = str(_spoken_value(params, 'batches',
                                    params.get('batch')) or '').strip()
        return action, {
            'plants': plants,
            'batches': batches,
            'notes': notes,
            'binding': binding,
        }
    raise ValueError(f'unknown prepare action: {action}')


# ── process singleton ──────────────────────────────────────────────────────

_HOST: 'CannanasHost | None' = None
_HOST_LOCK = threading.Lock()


def get_host(env=None) -> 'CannanasHost':
    """The one host for the JARVIS process; tests may set_host instead."""
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = CannanasHost(env=env)
        return _HOST


def set_host(host) -> None:
    """Explicit override (tests); the process singleton follows it."""
    global _HOST
    with _HOST_LOCK:
        _HOST = host


def complete_preview_text(preview: dict) -> str:
    """Complete, deterministic German preview line from the adapter preview."""
    preview = preview if isinstance(preview, dict) else {}
    if preview.get('referenz') and preview.get('typ'):
        # Diary previews: speak one sentence instead of raw field names — this
        # text is read aloud and stored in the audit unchanged.
        reference = ' '.join(str(preview.get('referenz')).split())
        text = ' '.join(str(preview.get('text') or '').split())
        spoken = f'Tagebucheintrag {reference}'
        return f'{spoken}: {text}' if text else spoken
    order = ('plant_name', 'name', 'room', 'strain', 'status',
             'plant_count', 'count', 'notes', 'identifier')
    parts = []
    for key in order:
        value = preview.get(key)
        if key in preview and value not in (None, ''):
            parts.append(f'{key}: {value}')
    for key, value in preview.items():
        if key not in order and key != 'message' and value not in (None, ''):
            parts.append(f'{key}: {value}')
    if not parts:
        return 'Eintrag unverändert.'
    return ' '.join(parts)


# ── the host ───────────────────────────────────────────────────────────────

class CannanasHost:
    """Configuration, client lifecycle, coordinator, audit and confirm flow."""

    def __init__(self, *, env=None, client=None, audit=None,
                 coordinator: ConfirmationCoordinator | None = None) -> None:
        self._env = os.environ if env is None else env
        self._missing = missing_config_variables(self._env)
        self._client = client
        self._client_started = client is not None
        self._audit = audit if audit is not None else None
        self.coordinator = coordinator if coordinator is not None \
            else ConfirmationCoordinator()
        self._boot = adapter_boot_id()
        self._degraded_reason = ''
        self._lock = threading.RLock()
        if self._missing:
            self._degraded_reason = _unavailability_text(self._missing)
        elif self._audit is None:
            self._audit = CannanasAudit(self._env[ENV_HOST_AUDIT])

    # ── readiness ──────────────────────────────────────────────────────────

    def available(self) -> bool:
        """Read readiness. Writes additionally require a verified release."""
        with self._lock:
            if self._missing:
                return False
            if self._degraded_reason and 'reads allowed' in self._degraded_reason:
                return True   # read-only-safe degraded state: reads continue
            if self._degraded_reason:
                return False
            if self._client is not None and getattr(self._client, 'degraded', False):
                return False
            return True

    def availability_text(self) -> str:
        with self._lock:
            if self._missing:
                return _unavailability_text(self._missing)
            if self._degraded_reason:
                return f'Cannanas ist derzeit nicht verfügbar ({self._degraded_reason}).'
            if getattr(self._client, 'degraded', False) if self._client else False:
                return (f'Cannanas ist derzeit nicht verfügbar '
                        f'(client: {self._client.degraded_reason}).')
            return ''

    def boot_id(self) -> str:
        return self._boot

    def has_pending_for(self, turn) -> bool:
        """True when this turn's device owns a prepared, unconfirmed preview.

        The voice layer asks the host, not a workflow: a preview prepared
        through the closed Cannanas action enum must be confirmable by the
        spoken "ja" even when no other workflow is wired.
        """
        if turn is None:
            return False
        session_id = getattr(turn, 'stable_device_id', None) or ''
        if not session_id:
            return False
        try:
            pending = self.coordinator.current(session_id)
        except Exception:
            return False
        return bool(pending is not None
                    and getattr(pending, 'state', '') == 'pending')

    def mark_degraded(self, reason: str) -> None:
        with self._lock:
            self._degraded_reason = reason

    def client_if_started(self):
        return self._client if self._client_started else None

    def missing_variables(self) -> tuple:
        return self._missing

    def on_client_restart(self) -> None:
        """Client restart fence: adapter restart invalidates pending confirms."""
        self._adapter_boot_cache = (None, '')
        self.coordinator.invalidate_all_on_adapter_restart()

    def _adapter_boot(self) -> str:
        """The live adapter's own boot id, cached per client generation.

        The adapter fences every confirm on its own boot id, so the host
        process uuid can never pass. If the id cannot be read, the host uuid
        stays and the adapter refuses the confirm itself: fail closed, never a
        wrong write.
        """
        client = self.require_client()
        cached_client, cached_value = getattr(self, '_adapter_boot_cache', (None, ''))
        if cached_client is client and cached_value:
            return cached_value
        value = ''
        try:
            info = client.call('get_adapter_info', {})
            config = info.get('config') if isinstance(info, dict) else None
            value = str((config or {}).get('boot_id') or '').strip()
        except Exception:
            value = ''
        if value:
            self._adapter_boot_cache = (client, value)
            return value
        return self._boot

    # ── client lifecycle ───────────────────────────────────────────────────

    def require_client(self):
        with self._lock:
            if self._missing:
                raise CannanasUnavailable(_unavailability_text(self._missing))
            if self._client is None:
                self._client = CannanasClient({
                    'command': self._env[ENV_ADAPTER_COMMAND],
                    'args': (self._env.get(ENV_ADAPTER_ARGS) or '').split(),
                    'adapter_version': PINNED_ADAPTER_VERSION,
                    'artifact_sha256': PINNED_WHEEL_SHA256,
                    'expected_tools': ADAPTER_TOOLS(),
                    'adapter_env': child_environment({
                        key: self._env.get(key, '') for key in ADAPTER_ENV_VARS}),
                }, on_restart=self.on_client_restart)
                started = self._client.start()
                self._client_started = True
                if not started:
                    if not self._degraded_reason:
                        self.mark_degraded(
                            f'client: {getattr(self._client, "degraded_reason", "start failed")}')
                    raise CannanasUnavailable(self.availability_text())
                if self._client.read_only_degraded:
                    self._degraded_reason = (
                        'release verification failed: only reads allowed')
                    # Reads stay allowed in the read-only degraded state.
                    return self._client
                return self._client
            self._client_started = True
            if getattr(self._client, 'degraded', False):
                if not self._degraded_reason:
                    self.mark_degraded(f'client: {self._client.degraded_reason}')
                raise CannanasUnavailable(self.availability_text())
            if (getattr(self._client, 'read_only_degraded', False)
                    and (getattr(self, '_degraded_reason', '') == ''
                         or 'reads allowed' not in str(self._degraded_reason))):
                self._degraded_reason = 'release verification failed: only reads allowed'
            return self._client

    # ── plugin-facing flows ────────────────────────────────────────────────

    def call_read(self, action: str, params: dict) -> dict:
        """One read through the client; audit failure is non-fatal for reads."""
        tool, arguments, prefix = read_tool_call(action, params)
        if not self.available():
            raise CannanasUnavailable(self.availability_text())
        client = self.require_client()
        result = client.call(tool, arguments)
        exact = bool(result.get('exact', True))
        truncated = bool(result.get('truncated', False))
        payload = _record_payload(prefix, result, exact, truncated)
        if action == 'count_plants_by_room':
            room = str(result.get('room') or '')
            if room:
                count = result.get('plant_count')
                summary = f'Pflanzen im Raum {room}: {count}.'
            else:
                by_room = result.get('by_room') or {}
                total = result.get('total_plants')
                listing = ', '.join(f'{room}: {count}'
                                    for room, count in by_room.items())
                summary = (f'Pflanzen je Raum: {listing}.' if listing
                           else f'Insgesamt {total} Pflanzen.')
            payload['summary'] = summary
            payload['records'] = []
        self._audit_read(action)
        return payload

    def _audit_read(self, action: str) -> None:
        audit = self._audit
        if audit is None:
            return
        try:
            audit.record_read(
                'read', action=action, payload_hash='',
                stable_device_id='', auth_session_id='',
                user_turn_id=0, state='read',
                warn=lambda: print(
                    '[Cannanas] Prüfunterlage für Lesen nicht verfügbar '
                    '(einmalige Warnung).'))  # one bounded operator warning
        except CannanasAuditClosed:
            pass  # reads degrade, never fail JARVIS

    # ── prepare ────────────────────────────────────────────────────────────

    def prepare(self, action: str, params: dict, turn) -> dict:
        """Run the adapter prepare_*; store the pending; return the preview."""
        if not self.available():
            raise CannanasUnavailable(self.availability_text())
        if getattr(self._client, 'read_only_degraded', False) if self._client else False:
            raise CannanasUnavailable(
                'Cannanas-Schreibvorgänge sind deaktiviert: '
                'Release-Verifizierung fehlgeschlagen.')
        binding = prepare_binding_for(turn, self._adapter_boot())
        if binding is None:
            raise CannanasUnavailable(
                'Cannanas-Änderungen sind nur für zugeordnete Geräte möglich.')
        tool, arguments = prepare_tool_call(action, params, binding)
        client = self.require_client()
        result = client.call(tool, arguments)
        if not result.get('ok', True):
            # The adapter refused the change (unknown field, unresolvable
            # name, impossible count). Nothing was prepared and nothing may
            # be confirmed: speak the reason, store no pending, write no
            # "prepared" audit entry. Never report a refusal as a preview.
            return {
                'ok': False,
                'summary': _refusal_text(result),
                'preview': {},
                'requires_confirmation': False,
                'exact': bool(result.get('exact', True)),
            }
        club_id = str(self._env.get('CANNANAS_CLUB_ID') or 'club-bound')
        pending = PendingConfirmation(
            action=action.removeprefix('prepare_'),
            club_id=club_id,
            payload_hash=str(result.get('payload_hash') or ''),
            complete_preview=complete_preview_text(result.get('preview') or {}),
            jarvis_session_id=binding['jarvis_session_id'],
            stable_device_id=binding['stable_device_id'],
            auth_session_id=binding['auth_session_id'],
            prepared_turn_id=int(binding['prepared_turn_id']),
            adapter_boot_id=self._boot,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + DEFAULT_PENDING_TTL_SECONDS,
            state='pending',
            opaque_mcp_token=OpaqueToken(str(result.get('confirmation_token') or '')),
        )
        self.coordinator.store(pending)
        audit = self._audit
        if audit is not None:
            try:
                audit.record(
                    'prepared', action=pending.action,
                    payload_hash=pending.payload_hash,
                    preview=pending.complete_preview,
                    stable_device_id=pending.stable_device_id,
                    auth_session_id=pending.auth_session_id,
                    user_turn_id=pending.prepared_turn_id,
                    state='pending', result_id='')
            except CannanasAuditClosed:
                # Nothing written yet: cancel instead of an unaccounted preview.
                self.coordinator.cancel(pending.jarvis_session_id)
                raise CannanasUnavailable(
                    'Cannanas-Schreibprüfung nicht verfügbar. '
                    'Bitte erneut versuchen.')
        preview = dict(result.get('preview') or {}) \
            if isinstance(result.get('preview') or {}, dict) else {}
        payload = {
            'ok': True,
            'summary': (f'Vorschau: {pending.complete_preview} '
                        f'{_CONFIRM_INSTRUCTION}'),
            'preview': preview,
            'requires_confirmation': True,
            'exact': bool(result.get('exact', True)),
        }
        return payload

    # ── host confirm flow (outside the model path) ─────────────────────────

    def evaluate(self, turn, text) -> dict:
        """Grammar check after a finalized turn; handled by the host only."""
        try:
            session_id = getattr(turn, 'stable_device_id', None) or ''
            pending = self.coordinator.current(session_id)
        except Exception:
            pending = None
        if turn is None or pending is None or pending.state != 'pending':
            return {'decision': 'none', 'spoken': '', 'handled': False}
        decision = confirmation_grammar(text)
        if decision == 'none':
            # Unrelated speech leaves the pending untouched (until TTL).
            return {'decision': 'none', 'spoken': '', 'handled': False}
        if decision == 'deny':
            self.coordinator.cancel(session_id)
            return {'decision': 'deny',
                    'spoken': 'Abgebrochen — die Vorschau wurde verworfen.',
                    'handled': True}
        outcome = self.confirm(turn)
        outcome['decision'] = 'affirm'
        return outcome

    def confirm(self, turn) -> dict:
        """Atomic consume → audit-before-POST → classify; never retried."""
        session_id = getattr(turn, 'stable_device_id', None) or ''
        pending = self.coordinator.current(session_id)
        expected = {
            'action': pending.action,
            'club_id': pending.club_id,
            'payload_hash': pending.payload_hash,
            'adapter_boot_id': pending.adapter_boot_id,
        } if pending is not None else {}
        executing, refusal = self.coordinator.consume(session_id, turn, expected)
        if executing is None:
            return {'decision': 'affirm',
                    'spoken': REFUSAL_SPOKEN.get(refusal, _NOT_CONFIRMABLE_SHORT),
                    'handled': True, 'refusal': refusal,
                    'classification': 'refused'}
        try:
            with self._audit.pre_post(
                    action=executing.action,
                    payload_hash=executing.payload_hash,
                    preview=executing.complete_preview,
                    stable_device_id=executing.stable_device_id,
                    auth_session_id=executing.auth_session_id,
                    user_turn_id=getattr(turn, 'user_turn_id', 0)) as finish:
                binding = {
                    'jarvis_session_id': executing.jarvis_session_id,
                    'stable_device_id': executing.stable_device_id,
                    'auth_session_id': executing.auth_session_id,
                    'prepared_turn_id': str(executing.prepared_turn_id),
                    'adapter_boot_id': executing.adapter_boot_id,
                }
                tool = f'confirm_{executing.action}'
                try:
                    result = self.require_client().call(
                        tool, {'confirmation_token': executing.opaque_mcp_token.get(),
                               'binding': binding})
                except CannanasPreSendRefusal:
                    # Refused before the transport: provably no bytes were sent.
                    finish('failed', '')
                    self.coordinator.mark_consumed(session_id)
                    return {'decision': 'affirm',
                            'spoken': ('Cannanas ist derzeit nicht verfügbar. '
                                       'Kein Eintrag vorgenommen. Bitte erneut vorbereiten.'),
                            'handled': True, 'classification': 'failed'}
                except Exception:
                    # Bytes may have been sent: never retried, stays unknown.
                    finish('unknown', '')
                    self.coordinator.mark_consumed(session_id)
                    return {'decision': 'affirm',
                            'spoken': ('Unklar — der Vorgang wird nicht wiederholt. '
                                       'Bitte in Cannanas kontrollieren.'),
                            'handled': True, 'classification': 'unknown'}
            classification, result_id, spoken = classify_write_result(result)
            finish(classification, result_id)
            self.coordinator.mark_consumed(session_id)
            return {'decision': 'affirm', 'spoken': spoken, 'handled': True,
                    'classification': classification, 'result_id': result_id}
        except CannanasAuditClosed:
            # The audit refused before any POST: the pending is spent.
            self.coordinator.mark_consumed(session_id)
            return {'decision': 'affirm',
                    'spoken': ('Cannanas-Schreibprüfung nicht verfügbar. '
                               'Kein Eintrag vorgenommen. Bitte erneut vorbereiten.'),
                    'handled': True, 'classification': 'failed'}


def classify_write_result(result) -> 'tuple[str, str, str]':
    """ok with id → succeeded; ok w/o id → created_but_unverified; else failed."""
    result = result if isinstance(result, dict) else {}
    result_id = str(result.get('result_id') or result.get('id')
                    or result.get('returned_object_id') or '')
    if result.get('ok'):
        if result_id:
            return ('succeeded', result_id, f'Erledigt — Objekt-ID {result_id}.')
        return ('created_but_unverified', '',
                'Der Vorgang war vermutlich erfolgreich, konnte aber nicht über '
                'die Objekt-ID bestätigt werden.')
    return ('failed', '', 'Der Eintrag ist fehlgeschlagen. '
            'Es wurde nichts wiederholt.')