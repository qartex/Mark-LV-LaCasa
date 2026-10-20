"""Tests for the measured room values of a diary entry (no network).

The diary entry and the morning briefing must name the same number for the
same room, so the tests below compare both paths over one Home Assistant
state list.
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _path in (str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core.growroom_measurements import (  # noqa: E402
    room_measurements,
    zone_sensor_values,
    zone_temperatures,
)

HOME_ASSISTANT_STATES = [
    {"entity_id": "sensor.zonen_madrid_temperatur_celsius", "state": "24.2"},
    {"entity_id": "sensor.zonen_madrid_luftfeuchte", "state": "61.0"},
    {"entity_id": "sensor.zonen_helsinki_temperatur_celsius", "state": "19.8"},
]


class RoomMeasurementTests(unittest.TestCase):
    def test_the_room_measurements_map_the_sensors(self):
        sensors = {"zonen_madrid_temperatur_celsius": 24.2,
                   "zonen_madrid_luftfeuchte": 61.0,
                   "zonen_helsinki_temperatur_celsius": 19.8}
        result = room_measurements("Madrid", sensors)
        self.assertEqual(24.2, result["air_temperature"])
        self.assertEqual(61.0, result["air_humidity"])
        self.assertNotIn("soil_moisture", result)

    def test_an_unknown_room_yields_no_measurements(self):
        self.assertEqual({}, room_measurements("Atlantis", {}))

    def test_only_the_cannanas_measurement_keys_are_used(self):
        result = room_measurements("Madrid", {
            "zonen_madrid_temperatur_celsius": 24.2,
            "zonen_madrid_luftfeuchte": 61.0,
        })
        self.assertEqual({"air_temperature", "air_humidity"}, set(result))

    def test_the_room_name_is_matched_regardless_of_case_and_umlauts(self):
        sensors = {"zonen_muenchen_temperatur_celsius": 22.0}
        self.assertEqual(
            22.0, room_measurements("München", sensors)["air_temperature"]
        )
        self.assertEqual(
            22.0, room_measurements("muenchen", sensors)["air_temperature"]
        )

    def test_a_room_with_two_words_matches_the_sensor_slug(self):
        sensors = {"zonen_new_york_temperatur_celsius": 21.0}
        self.assertEqual(
            21.0, room_measurements("New York", sensors)["air_temperature"]
        )

    def test_a_room_without_a_humidity_sensor_gets_no_humidity(self):
        result = room_measurements(
            "Helsinki", {"zonen_helsinki_temperatur_celsius": 19.8}
        )
        self.assertEqual({"air_temperature": 19.8}, result)

    def test_a_sensor_without_a_readable_value_is_left_out(self):
        result = room_measurements("Madrid", {
            "zonen_madrid_temperatur_celsius": "unavailable",
            "zonen_madrid_luftfeuchte": 61.0,
        })
        self.assertEqual({"air_humidity": 61.0}, result)

    def test_the_measurements_are_numbers(self):
        result = room_measurements(
            "Madrid", {"zonen_madrid_temperatur_celsius": "24.2"}
        )
        self.assertIsInstance(result["air_temperature"], float)


class SensorReadingTests(unittest.TestCase):
    def test_the_state_list_becomes_the_zone_sensors(self):
        values = zone_sensor_values(HOME_ASSISTANT_STATES)
        self.assertEqual(24.2, values["zonen_madrid_temperatur_celsius"])
        self.assertEqual(61.0, values["zonen_madrid_luftfeuchte"])

    def test_a_sensor_without_a_readable_value_is_skipped(self):
        values = zone_sensor_values([
            {"entity_id": "sensor.zonen_tokio_temperatur_celsius",
             "state": "unavailable"},
        ])
        self.assertEqual({}, values)

    def test_the_room_temperatures_are_keyed_by_the_room_name(self):
        temperatures = zone_temperatures(HOME_ASSISTANT_STATES)
        self.assertEqual(24.2, temperatures["Madrid"])
        self.assertEqual(19.8, temperatures["Helsinki"])
        self.assertNotIn("zonen_madrid_temperatur_celsius", temperatures)


class BriefingParityTests(unittest.TestCase):
    """The diary entry and the briefing must speak the same number."""

    def _bridge(self):
        import esp32_jarvis_bridge as bridge

        return bridge

    def test_the_briefing_and_the_diary_read_the_same_temperature(self):
        bridge = self._bridge()
        with unittest.mock.patch.object(
            bridge, "_ha_states", return_value=HOME_ASSISTANT_STATES
        ):
            temperatures = bridge._briefing_temperatures()
            sensors = bridge._home_assistant_sensors()
        self.assertEqual(24.2, temperatures["Madrid"])
        self.assertEqual(
            24.2, room_measurements("Madrid", sensors)["air_temperature"]
        )

    def test_the_briefing_reads_the_shared_zone_sensors(self):
        bridge = self._bridge()
        with unittest.mock.patch.object(
            bridge, "_ha_states", return_value=HOME_ASSISTANT_STATES
        ):
            temperatures = bridge._briefing_temperatures()
        self.assertEqual(zone_temperatures(HOME_ASSISTANT_STATES), temperatures)

    def test_the_bridge_hands_the_live_sensors_to_the_diary_workflow(self):
        import tempfile

        bridge = self._bridge()
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                bridge, "_ha_states", return_value=HOME_ASSISTANT_STATES
            ):
                diary, _queue = bridge._optional_voice_workflows(tmp)
                self.assertIsNotNone(diary)
                sensors = diary._sensors()
        self.assertEqual(24.2, sensors["zonen_madrid_temperatur_celsius"])


if __name__ == "__main__":
    unittest.main()
