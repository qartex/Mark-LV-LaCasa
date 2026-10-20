"""Closed Cannanas model-facing action enum, shared by the headless gate."""
from __future__ import annotations

# The exact Release-1 closed enum. Reads plus prepares only: confirm/cancel
# state-machine operations live host-side and are never model-facing.
CANNANAS_READ_ACTIONS = frozenset({
    'overview',
    'find_zones',
    'find_strains',
    'find_plants',
    'find_propagation_material',
    'list_recent_batches',
    'list_plants',
    'count_plants_by_room',
    'zone_growth_summary',
    'list_mother_plants',
    'list_diaries',
    'list_plant_diaries',
    'list_harvests',
    'get_grow_config',
    'zone_plant_status',
})

CANNANAS_PREPARE_ACTIONS = frozenset({
    'prepare_plant_create',
    'prepare_strain_create',
    'prepare_batch_create',
    'prepare_plant_update',
    'prepare_batch_update',
    'prepare_diary_create',
    'prepare_zone_update',
    'prepare_harvest_create',
    'prepare_waste_disposal_create',
})

CANNANAS_MODEL_ACTIONS = CANNANAS_READ_ACTIONS | CANNANAS_PREPARE_ACTIONS