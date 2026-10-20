"""Measured room values for a diary entry, taken from the Home Assistant sensors.

The gardener can say "Tagebucheintrag Madrid" and get the measured values of
that room attached instead of dictating them. The values that the sensors do
not know stay out; the entry is still worth writing without them.

The sensor names and the room matching are the ones of the morning briefing
(``core/growroom_briefing.py`` and its bridge reader), so the briefing and the
diary entry speak the same number for the same room. The measurement keys are
the ones the Cannanas diary ``measurements`` object knows; nothing else is
ever sent.
"""
from __future__ import annotations

import math

# The grow sensors live under sensor.zonen_<room>_<value>.
ZONE_SENSOR_PREFIX = "sensor.zonen_"
ZONE_SENSOR_DOMAIN = "sensor."

# The sensor suffix and the diary measurement key it feeds. Only keys the
# Cannanas measurements object knows are allowed here.
SENSOR_MEASUREMENT_KEYS = (
    ("_temperatur_celsius", "air_temperature"),
    ("_luftfeuchtigkeit", "air_humidity"),
    ("_luftfeuchte", "air_humidity"),
)


def _room_key(name: str) -> str:
    """Umlaut- and case-insensitive room identifier for matching.

    The room name of a sensor is a slug ("new_york"), the spoken name has
    spaces ("New York"); both must find the same room.
    """
    return (
        str(name or "").strip().lower()
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("ß", "ss").replace(" ", "").replace("_", "")
    )


def _sensor_name(entity_id: str) -> str:
    """The zone sensor name without the Home Assistant domain prefix."""
    text = str(entity_id or "").strip().lower()
    if text.startswith(ZONE_SENSOR_DOMAIN):
        text = text[len(ZONE_SENSOR_DOMAIN):]
    return text


def _number(value) -> float | None:
    """A finite number, or None for a sensor that cannot be read."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def zone_sensor_values(states) -> dict:
    """The readable grow sensors of one Home Assistant state list.

    The keys are the sensor names without the domain (``zonen_madrid_
    temperatur_celsius``); a sensor whose state is not a number (unavailable,
    unknown, nan) is left out.
    """
    values: dict[str, float] = {}
    for state in states or ():
        if not isinstance(state, dict):
            continue
        entity_id = str(state.get("entity_id") or "")
        if not entity_id.startswith(ZONE_SENSOR_PREFIX):
            continue
        number = _number(state.get("state"))
        if number is None:
            continue
        values[_sensor_name(entity_id)] = number
    return values


def zone_temperatures(states) -> dict:
    """The room temperatures of one state list, keyed by the room name."""
    rooms: dict[str, float] = {}
    for state in states or ():
        if not isinstance(state, dict):
            continue
        entity_id = str(state.get("entity_id") or "")
        if not (entity_id.startswith(ZONE_SENSOR_PREFIX)
                and entity_id.endswith("_temperatur_celsius")):
            continue
        number = _number(state.get("state"))
        if number is None:
            continue
        room = entity_id[len(ZONE_SENSOR_PREFIX):-len("_temperatur_celsius")]
        rooms[room.strip().title()] = number
    return rooms


def room_measurements(room: str, sensors: dict) -> dict:
    """The measured values of one room, in the diary measurement keys.

    Only the values the sensors really know are returned; a room without a
    sensor yields an empty dict, never a guess.
    """
    wanted = _room_key(room)
    if not wanted or not isinstance(sensors, dict):
        return {}
    found: dict[str, float] = {}
    for name, raw in sensors.items():
        key = _sensor_name(name)
        if not key.startswith("zonen_"):
            continue
        for suffix, measurement in SENSOR_MEASUREMENT_KEYS:
            if not key.endswith(suffix):
                continue
            sensor_room = key[len("zonen_"):-len(suffix)]
            if _room_key(sensor_room) == wanted:
                number = _number(raw)
                if number is not None:
                    found[measurement] = number
            break
    return found
