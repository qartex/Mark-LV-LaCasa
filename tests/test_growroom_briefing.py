"""Tests for the spoken morning briefing (host-owned, no network)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.growroom_briefing import (
    GrowroomBriefing,
    SpeakerBook,
    compose_briefing,
    greeting_for,
    stage_label,
)


def zone(name, *, status=None, days_ago=10, count=1, plants=0, diary=None):
    stages = []
    if status:
        stages.append({
            "status": status,
            "count": count,
            "oldest_created_at": (
                datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return {
        "id": name.strip().lower(),
        "name": name,
        "batch_count": count if status else 0,
        "plant_count": plants,
        "stages": stages,
        "last_diary": diary,
    }


class GreetingTests(unittest.TestCase):
    def test_greeting_follows_the_time_of_day(self):
        morning = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        midday = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
        evening = datetime(2026, 10, 20, 20, 15, tzinfo=timezone.utc)
        self.assertEqual("Guten Morgen", greeting_for(morning))
        self.assertEqual("Hallo", greeting_for(midday))
        self.assertEqual("Guten Abend", greeting_for(evening))

    def test_stage_labels_are_german(self):
        self.assertEqual("Blüte", stage_label("FLOWERING"))
        self.assertEqual("Wachstum", stage_label("VEGETATIVE"))
        self.assertEqual("Ernte", stage_label("HARVEST"))
        self.assertEqual("Keimung", stage_label("GERMINATION"))


class ComposeTests(unittest.TestCase):
    def test_briefing_greets_by_name_and_reports_room_state(self):
        zones = [
            zone(" Madrid", status="FLOWERING", days_ago=45, plants=0,
                 diary={"created_at": (datetime.now(timezone.utc)
                                       - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "text": "Blätter kontrolliert, alles gesund."}),
            zone("Helsinki", status="VEGETATIVE", days_ago=12, plants=24),
        ]
        now = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        spoken = compose_briefing(
            speaker="Lukas", title="Head of Grow", todos=["Madrid gießen"],
            zones=zones, temperatures={"Madrid": 26.0, "Helsinki": 24.2},
            now=now,
        )
        self.assertTrue(spoken.startswith("Guten Morgen, Head of Grow."), spoken)
        self.assertIn("Madrid gießen", spoken)
        self.assertIn("Madrid: Blüte Tag 45", spoken)
        self.assertIn("26,0 Grad", spoken)
        self.assertIn("Blätter kontrolliert", spoken)
        self.assertIn("Helsinki: Wachstum Tag 12", spoken)
        self.assertIn("24,2 Grad", spoken)
        self.assertNotIn("None", spoken)

    def test_room_without_cannanas_state_still_reports_temperature(self):
        now = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        spoken = compose_briefing(
            speaker="Björn", title="Gärtner", todos=[],
            zones=[zone(" Madrid", plants=0)],
            temperatures={"Madrid": 26.4}, now=now,
        )
        self.assertIn("Guten Morgen, Gärtner Björn.", spoken)
        self.assertIn("Madrid:", spoken)
        self.assertIn("26,4 Grad", spoken)
        self.assertIn("keine offene Charge", spoken)

    def test_growers_are_addressed_with_their_name(self):
        now = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        spoken = compose_briefing(
            speaker="Björn", title="Gärtner", todos=[], zones=[], now=now,
        )
        self.assertTrue(spoken.startswith("Guten Morgen, Gärtner Björn."), spoken)

    def test_technical_probes_are_not_spoken_as_rooms(self):
        now = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        spoken = compose_briefing(
            speaker="Lukas", title="Head of Grow", todos=[], zones=[],
            temperatures={"Draussen": 7.5, "Madrid": 20.4}, now=now,
        )
        self.assertNotIn("Draussen", spoken)

    def test_briefing_is_bounded_even_with_many_rooms_and_todos(self):
        zones = [zone(f"Raum{i}", status="FLOWERING", days_ago=i + 1)
                 for i in range(12)]
        now = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        spoken = compose_briefing(
            speaker="Alex", title="Gärtner",
            todos=[f"Aufgabe {i}" for i in range(10)],
            zones=zones, temperatures={}, now=now,
        )
        self.assertLessEqual(len(spoken), 900)
        self.assertIn("Aufgabe 0", spoken)
        self.assertIn("Aufgabe 2", spoken)
        self.assertNotIn("Aufgabe 5", spoken)
        self.assertIn("weitere Räume", spoken)

    def test_ambiguous_room_stage_prefers_the_oldest_active_stage(self):
        entry = zone("Madrid")
        entry["stages"] = [
            {"status": "VEGETATIVE", "count": 2,
             "oldest_created_at": "2026-10-01T06:00:00Z"},
            {"status": "FLOWERING", "count": 5,
             "oldest_created_at": "2026-09-05T06:00:00Z"},
        ]
        entry["batch_count"] = 7
        now = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc)
        spoken = compose_briefing(speaker="Lukas", title="Head of Grow",
                                  todos=[], zones=[entry],
                                  temperatures={}, now=now)
        self.assertIn("Blüte Tag 45", spoken)


class SpeakerBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "speakers.json"

    def _write(self, payload, mode=0o600):
        self.path.write_text(json.dumps(payload))
        os.chmod(self.path, mode)

    def test_profiles_carry_title_and_own_todos(self):
        self._write({
            "default": "lukas",
            "speakers": {
                "lukas": {"name": "Lukas", "title": "Head of Grow",
                          "todos": ["Madrid gießen"]},
                "bjoern": {"name": "Björn", "title": "Gärtner",
                           "todos": ["Helsinki pH prüfen"]},
            },
        })
        book = SpeakerBook(self.path)
        self.assertEqual("Head of Grow", book.get("lukas").title)
        self.assertEqual(["Helsinki pH prüfen"], list(book.get("bjoern").todos))
        # An unknown/unrecognised voice falls back to the default speaker.
        self.assertEqual("lukas", book.get(None).key)

    def test_world_readable_profile_file_is_refused(self):
        self._write({"speakers": {"lukas": {"name": "Lukas"}}}, mode=0o644)
        with self.assertRaises(PermissionError):
            SpeakerBook(self.path)

    def test_missing_file_yields_a_neutral_default_speaker(self):
        book = SpeakerBook(self.path)
        self.assertEqual("Gärtner", book.get("lukas").title)
        self.assertEqual([], list(book.get("lukas").todos))


class BriefingRunnerTests(unittest.TestCase):
    """The briefing pulls live data through the host and degrades bounded."""

    class Host:
        def __init__(self, zones=None, fail=False):
            self.zones = zones or []
            self.fail = fail
            self.calls = []

        def call_read(self, action, params):
            self.calls.append((action, dict(params)))
            if self.fail:
                raise RuntimeError("cannanas unavailable")
            return {"ok": True, "zones": self.zones, "exact": True}

    def test_runner_reads_the_zone_summary_once(self):
        host = self.Host([zone(" Madrid", status="FLOWERING", days_ago=3)])
        briefing = GrowroomBriefing(
            host=host, speakers=SpeakerBook(Path("/nonexistent/speakers.json")),
            temperatures=lambda: {"Madrid": 25.5},
            now=lambda: datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc),
        )
        spoken = briefing.for_speaker("Lukas")
        self.assertEqual([("zone_growth_summary", {"limit": 50})], host.calls)
        self.assertIn("Guten Morgen", spoken)
        self.assertIn("25,5 Grad", spoken)

    def test_runner_still_speaks_todos_when_cannanas_is_down(self):
        host = self.Host(fail=True)
        path = Path(tempfile.mkdtemp()) / "speakers.json"
        path.write_text(json.dumps({"speakers": {"lukas": {
            "name": "Lukas", "title": "Head of Grow", "todos": ["Madrid gießen"]}}}))
        os.chmod(path, 0o600)
        briefing = GrowroomBriefing(
            host=host, speakers=SpeakerBook(path),
            temperatures=lambda: {"Madrid": 24.0},
            now=lambda: datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc),
        )
        spoken = briefing.for_speaker("lukas")
        self.assertIn("Madrid gießen", spoken)
        self.assertIn("24,0 Grad", spoken)
        self.assertNotIn("Traceback", spoken)


if __name__ == "__main__":
    unittest.main()
