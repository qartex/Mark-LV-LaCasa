# Cannanas Gärtner-Werkzeuge (Batch-Bewegung, Namensgebung, Datum, Stecklinge) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Die sieben Vorschläge aus `docs/cannanas-grow-funktionsliste.md` als echte Sprachfunktionen umsetzen: Charge samt Pflanzen umziehen, Status aus dem Datum ableiten, Stecklingsbilanz, Tagebuch aus Home-Assistant-Werten, Chargen-Pflanzen benennen, Mutterpflanzen in Reserve markieren, Gieß-Intervall aus dem Tagebuch.

**Architecture:** Die Erweiterung folgt der bestehenden Dreischichtung: Adapter (Cannanas-API, `prepare_*` + Lese-Tools) → Mark-LV-Host (`core/cannanas_host.py`, geschlossene Enum) → Bridge (`scripts/esp32_jarvis_bridge.py`, Tool-Schema). Jede neue Fähigkeit braucht eine Adapter-Vorbereitung (Vorschau, kein Write), eine Host-Abbildung und einen geschlossenen Modellnamen. Der Adapter bleibt die einzige Schicht, die kanonische IDs auflöst.

**Tech Stack:** Python 3.14, `mcp` (FastMCP), pytest-unittest, `google-genai`, Home Assistant REST, systemd-User-Services.

**Spec:** `docs/superpowers/specs/2026-10-20-cannanas-grow-vollabdeckung-design.md` (Grundlagen) + `docs/cannanas-grow-funktionsliste.md` Abschnitt 10 (die sieben Vorschläge).

## Global Constraints

- Adapter-Allowlist: nur Pfade aus `fixtures/cannanas-api.yaml`. Neue HTTP-Pfade brauchen einen Allowlist-Eintrag.
- Jede Schreibaktion bleibt `prepare_*` → gesprochene Vorschau → „ja“ → `confirm_*`. Der Adapter führt **nie** selbst einen Write aus.
- Bestätigungen werden gegen die **Adapter**-Boot-ID geprüft, nicht gegen eine Host-ID. Ohne lesbare Adapter-Boot-ID: fail-closed.
- `confirm_*` und `cancel_*` bleiben host-side; das Modell darf sie nie auslösen.
- Die Modell-Aktions-Enum bleibt geschlossen. Jede neue Aktion kommt in `CANNANAS_MODEL_ACTIONS` **und** in die Bridge-Schema-Enum, sonst driften sie auseinander.
- Keine Geheimnisse in Code, Tests, Commits oder Doku. Keine Club-IDs, keine API-Keys.
- Ein gesprochenes Feld, das die Zielschicht nicht kennt, wird **abgelehnt** — nie stillschweigend verworfen.
- Meldungen an den Gärtner sind Deutsch, eine Zeile, ohne technische Bezeichner.
- Adapter-Release: Version in `pyproject.toml` **und** `src/cannanas_mcp_adapter/_release.py` bumpen, `scripts/build-release.sh` laufen lassen, den Wheel-SHA aus `release/release-manifest-vX.Y.Z.json` lesen. Mark-LV pinnt danach `PINNED_ADAPTER_VERSION` und `PINNED_WHEEL_SHA256` in `core/cannanas_client.py`.

## Review Focus

Die fünf Fehlerbilder, die kein Test abdecken wird, aber ein Gärtner sofort bemerkt:

1. **„Zieh die Charge Ztilton #6 mit allen 12 Pflanzen nach Helsinki“** — der Umzug muss die Charge **und** ihre Pflanzen verschieben, nicht nur die Charge. Ein halber Umzug ist schlimmer als keiner.
2. **„Ztilton #3 ist seit drei Wochen in Blüte“** — der Gärtner nennt eine Dauer, kein Datum. Ohne Umrechnung bleibt der Status unverändert und die Antwort muss das sagen.
3. **„Wie viele Stecklinge habe ich von Mutter A?“** — die Antwort kommt aus den Chargen, in denen die Mutter als `mother_plants` steht. Wenn die Mutter nie als Mutter geführt wurde, ist die Antwort „0“, nicht ein Fehler.
4. **„Tagebucheintrag Madrid“** ohne Messwerte — die Werte sollen aus Home Assistant kommen. Ohne HA-Anbindung muss der Eintrag trotzdem funktionieren (nur ohne Messwerte).
5. **„Benenne alle Pflanzen der Charge Ztilton #6 mit Nummerierung“** — 12 Pflanzen brauchen 12 eindeutige Namen. Ein Name, der schon vergeben ist, darf nicht stillschweigend doppelt vergeben werden.

---

### Task 1: Charge samt Pflanzen umziehen

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (`prepare_batch_update`)
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (neue `_batch_with_plants()`)
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py` (`prepare_tool_call`, `prepare_batch_update`-Zweig)
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_actions.py` (`CANNANAS_MODEL_ACTIONS`)
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_voice_e2e.py`

**Interfaces:**
- Consumes: `_resolve_batch(query)`, `_batch_members(plants, batch)`, `prepare_batch_update(batch, fields, binding)` (bestehend, Task 1 dieser Serie)
- Produces: neues Adapter-Werkzeug `prepare_batch_move(batch: str, room: str, with_plants: bool = True, binding: dict | None = None) -> dict`; Adapter liefert `preview` mit `{"batch": <label>, "zone": <label>, "pflanzen": <int>, "raum": <label>}`; Host-Aktion `prepare_batch_move` mit Parametern `plant_name` (Ziel-Charge) und `room` (Ziel-Raum); neues Modell-Feld `with_plants` (bool, default True).

- [ ] **Step 1: Write the failing test**

Im Adapter-Test `tests/test_tool_surface.py`:

```python
def test_prepare_batch_move_carries_the_plants_of_the_batch(self) -> None:
    self.transport.push(
        FakeResponse({"data": [{"id": "b1", "name": None, "notes": "Ztilton #6",
                                "zone_id": "z1"}]}),
        FakeResponse({"data": [{"id": "z2", "name": "Helsinki"}]}),
        FakeResponse({"data": [{"id": "p1", "name": None, "batch_id": "b1"},
                                {"id": "p2", "name": None, "batch_id": "b1"}]}),
    )
    result = self.adapter.prepare_batch_move(batch="Ztilton #6", room="Helsinki")
    self.assertTrue(result["ok"], result)
    self.assertEqual(2, result["preview"]["pflanzen"])
    self.assertEqual("Helsinki", result["preview"]["raum"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lacasa15/services/cannanas-mcp/adapter && PYTHONPATH=src /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest tests.test_tool_surface.ToolSurfaceTest.test_prepare_batch_move_carries_the_plants_of_the_batch`
Expected: FAIL with `AttributeError: 'CannanasAdapter' object has no attribute 'prepare_batch_move'`

- [ ] **Step 3: Implement `prepare_batch_move` in `src/cannanas_mcp_adapter/server.py`**

```python
def prepare_batch_move(self, batch: str, room: str,
                       with_plants: bool = True,
                       binding: dict | None = None) -> dict[str, Any]:
    """Prepare moving a batch, optionally with all of its plants.

    The preview names the target room and the number of plants that move
    with the batch, so the grower hears the whole effect before saying
    yes. Nothing is written here.
    """
```

Der Body: `batch_item = self._resolve_batch(batch)`; `zone = self._resolve_zone(room)`; `members = self._batch_members(self.paged_records("plants", "plants")[0], batch_item)`; leeres `room` → `ValueError("Zielraum fehlt")`; `with_plants=False` → `members = []`. Der Payload enthält `batch_id`, `zone_id` und — wenn `with_plants` — `plant_ids` (alle Mitglieder-IDs). Vorschau: `{"batch": _grow_label(batch_item), "raum": _item_name(zone), "pflanzen": len(members), "zone": _item_name(zone)}`. `_prepare("batch_move", payload, preview, binding)` aufrufen.

Der Adapter-Werkzeugname `prepare_batch_move` braucht einen Eintrag in `EXPECTED_TOOLS` in `src/cannanas_mcp_adapter/_release.py` und in `ALLOWED_REQUESTS` (die verwendeten Pfade: `GET /v1/clubs/{clubId}/batches`, `GET /v1/clubs/{clubId}/zones`, `GET /v1/clubs/{clubId}/plants` — die drei stehen bereits in der Allowlist, es kommt **kein** neuer Pfad dazu).

- [ ] **Step 4: Run test to verify it passes**

Run: dieselbe Zeile wie Step 2.
Expected: PASS

- [ ] **Step 5: Add the host mapping and the model action**

In `core/cannanas_host.py`, im `prepare_tool_call`-Zweig für `prepare_batch_update` einen neuen Zweig `prepare_batch_move` ergänzen:

```python
if action == 'prepare_batch_move':
    args = {'batch': plant_name, 'room': room, 'binding': binding}
    with_plants = params.get('with_plants')
    if with_plants is not None:
        args['with_plants'] = bool(with_plants)
    return action, args
```

In `core/cannanas_actions.py` `CANNANAS_MODEL_ACTIONS` das Literal `'prepare_batch_move'` ergänzen. In `scripts/esp32_jarvis_bridge.py` die Tool-Schema-Enum aus `CANNANAS_MODEL_ACTIONS` ableiten (sie ist bereits eine Quelle der Wahrheit) und `with_plants` als Boolean-Parameter aufnehmen.

- [ ] **Step 6: Write the failing bridge test**

In `tests/test_cannanas_voice_e2e.py`:

```python
def test_a_batch_move_reaches_the_adapter_with_its_plants(self):
    # Der Runner übergibt Ziel-Charge und Ziel-Raum an das Adapter-Werkzeug.
    ...
    self.assertEqual("Ztilton #6", args["batch"])
    self.assertEqual("Helsinki", args["room"])
```

- [ ] **Step 7: Run the bridge test to verify it fails, then fix, then verify it passes**

Run: `cd /root/Mark-LV-LaCasa/.worktrees/integration && /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest tests.test_cannanas_voice_e2e`
Expected: FAIL (Aktion noch nicht in der Enum), nach der Enum-Ergänzung PASS.

- [ ] **Step 8: Run the full suites**

Run: Adapter `PYTHONPATH=src ... -m unittest discover -s tests -t .` und Mark-LV `... -m unittest discover -s tests`
Expected: beide grün.

- [ ] **Step 9: Commit**

```bash
git add src/cannanas_mcp_adapter/server.py src/cannanas_mcp_adapter/_release.py tests/test_tool_surface.py
git commit -m "feat(cannanas): move a batch with its plants"
```
(im Adapter-Repo) und im Mark-LV-Repo `git add core/cannanas_host.py core/cannanas_actions.py scripts/esp32_jarvis_bridge.py tests/test_cannanas_voice_e2e.py` mit `git commit -m "feat(voice): move a batch with its plants"`.

---

### Task 2: Status aus dem Datum ableiten

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (neue `_status_from_date()`)
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (`prepare_plant_update`: Feld `status` darf ein Alter annehmen)
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py` (Status-Aufbereitung)
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_plugin.py`

**Interfaces:**
- Consumes: `_status(value)`, `_validate_date(value, label)`, `PLANT_UPDATE_FIELDS` (bestehend)
- Produces: Adapter akzeptiert in `prepare_plant_update` den Wert `"seit 3 Wochen in Blüte"` **oder** ein Dict `{"status": "FLOWERING", "since_days": 21}` — der Host normalisiert die gesprochene Dauer zu diesem Dict, der Adapter berechnet daraus das Datum.

- [ ] **Step 1: Write the failing test**

```python
def test_a_status_since_days_sets_the_cultivation_date(self) -> None:
    self.transport.push(
        FakeResponse({"data": [{"id": "p1", "name": "Ztilton #3"}]}),
    )
    result = self.adapter.prepare_plant_update(
        plant="Ztilton #3",
        fields={"status": "FLOWERING", "since_days": 21},
    )
    self.assertTrue(result["ok"], result)
    # Das Datum liegt 21 Tage in der Vergangenheit.
    self.assertIn("cultivation_flowering_date", result["payload"])
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL with `Unbekanntes Feld 'since_days'`

- [ ] **Step 3: Implement the date derivation**

In `_split_plant_update_fields` (Adapter) den Zweig für `status` erweitern: ist `value` ein Dict mit `status` und `since_days`, dann `canonical = _status(value["status"])`, `since = int(value["since_days"])` (1..365, sonst `ValueError("since_days muss zwischen 1 und 365 liegen")`), und `clean["status"] = canonical` plus `clean["cultivation_flowering_date"] = _date_days_ago(since)`. Die Vorschau zeigt `status` und `cultivation_flowering_date`.

Neue Hilfsfunktion im Adapter:

```python
def _date_days_ago(days: int) -> str:
    """The ISO date that was `days` days ago (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).date().isoformat()
```

`datetime`/`timezone`/`timedelta` aus `datetime` importieren.

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Normalize the spoken duration in the host**

In `core/cannanas_host.py` eine Hilfsfunktion:

```python
_SPOKEN_DURATION = re.compile(
    r"(?:seit\s+)?(\d{1,3})\s*(tag|tagen|woche|wochen|monat|monaten|monate|jahre|jahr)",
    re.IGNORECASE)

def _spoken_since_days(value: str) -> int | None:
    """Days from a spoken duration ('seit 3 Wochen' -> 21). None if not one."""
```

Wörterbuch: `tag`/`tagen` = 1, `woche`/`wochen` = 7, `monat`/`monaten`/`monate` = 30, `jahr`/`jahre` = 365. Der Host baut aus dem gesprochenen `status` ein Dict `{'status': <kanonisch>, 'since_days': <tage>}` **nur** wenn eine Dauer erkannt wurde; sonst bleibt es ein einfacher Statusstring. Die kanonische Statusumrechnung (`Blüte` → `FLOWERING`) hat der Adapter, also schickt der Host das **Wort** weiter und das Dict nur mit `since_days` — der Adapter normalisiert beides. Konkret: schickt der Host `{'status': 'Blüte', 'since_days': 21}`, akzeptiert der Adapter das, indem `_status('Blüte')` intern die Wortliste nutzt.

Der Adapter-Status-Wortschatz steht in `server.py` als `STATUS_WORDS` (englische Kanons plus deutsche Synonyme) — er wird für `status` bereits benutzt. `since_days` wird **vor** der Feld-Aufteilung aus dem `status`-Wert extrahiert.

- [ ] **Step 6: Write the failing host test**

```python
def test_a_spoken_duration_becomes_since_days(self):
    from core.cannanas_host import prepare_tool_call
    _tool, args = prepare_tool_call(
        "prepare_plant_update",
        {"plant_name": "Ztilton #3", "status": "seit 3 Wochen in Blüte"},
        {"stable_device_id": "esp32-1"})
    self.assertEqual(21, args["fields"]["since_days"])
```

- [ ] **Step 7: Run the host test to verify it fails, then implement, then verify it passes**

Expected: FAIL (Feld fehlt), nach der Implementierung PASS.

- [ ] **Step 8: Run the full suites and commit**

Beide Suiten grün, dann Adapter `git commit -m "feat(cannanas): derive a status from a spoken duration"` und Mark-LV `git commit -m "feat(voice): derive a status from a spoken duration"`.

---

### Task 3: Stecklingsbilanz (Mutterpflanze → Stecklinge)

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (neues Lese-Werkzeug `mother_cutting_summary`)
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/_release.py` (`EXPECTED_TOOLS`)
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_client.py` (`ADAPTER_TOOLS`, `MCP_READ_TOOLS`)
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_actions.py` (`CANNANAS_READ_ACTIONS`, `CANNANAS_MODEL_ACTIONS`)
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py` (`read_tool_call`)
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_plugin.py`

**Interfaces:**
- Consumes: `paged_records("batches", "batches")`, `_grow_label(item)`, `_batch_candidates(records, query)` (bestehend)
- Produces: Adapter-Werkzeug `mother_cutting_summary(mother: str = "") -> dict` mit `{"ok": True, "mutters": [{"mother": <label>, "stecklinge": <int>, "chargen": <int>}], "total_stecklinge": <int>}`; leeres `mother` liefert alle Mütter.

- [ ] **Step 1: Write the failing test**

```python
def test_the_cutting_summary_counts_the_cuttings_per_mother(self) -> None:
    self.transport.push(
        FakeResponse({"data": [{"id": "b1", "name": None, "notes": "Ztilton #6",
                                "mother_plants": [{"plant_id": "p1", "cutting_count": 6}]},
                               {"id": "b2", "name": None, "notes": "Ztilton #5",
                                "mother_plants": [{"plant_id": "p1", "cutting_count": 4}]}]}),
        FakeResponse({"data": [{"id": "p1", "name": "Ztilton #1"}]}),
    )
    result = self.adapter.mother_cutting_summary(mother="Ztilton #1")
    self.assertTrue(result["ok"], result)
    self.assertEqual(10, result["total_stecklinge"])
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL with `AttributeError: ... 'mother_cutting_summary'`

- [ ] **Step 3: Implement the summary**

```python
def mother_cutting_summary(self, mother: str = "") -> dict[str, Any]:
    """How many cuttings came from each mother plant.

    The numbers come from the batches that were planned with mother
    plants. A mother that was never planned with counts as zero.
    """
```

Der Body: `batches, _ = self.paged_records("batches", "batches")`; je Batch `mother_plants` (Liste von `{"plant_id", "cutting_count"}`) durchgehen und je `plant_id` die Summe bilden; `plants, _ = self.paged_records("plants", "plants")` für die Label der Mütter; Filter auf `mother`, falls angegeben (über `_match_candidates` auf den Pflanzen, dann die passenden `plant_id`s). Rückgabe: `{"ok": True, "mutters": [...], "total_stecklinge": sum}`.

Der Adapter braucht dafür **keinen** neuen HTTP-Pfad (`GET /batches` und `GET /plants` stehen in der Allowlist).

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Wire it through the host**

`core/cannanas_client.py`: `mother_cutting_summary` in `ADAPTER_TOOLS()` **und** in `MCP_READ_TOOLS` ergänzen (fehlt eines, bricht der Read still aus).

`core/cannanas_actions.py`: `'mother_cutting_summary'` in `CANNANAS_READ_ACTIONS` und in `CANNANAS_MODEL_ACTIONS`.

`core/cannanas_host.py`, im `read_tool_call`-Zweig:

```python
if action == 'mother_cutting_summary':
    return ('mother_cutting_summary',
            {'mother': plant_name or strain or query}, binding)
```

- [ ] **Step 6: Write the failing host test**

```python
def test_the_cutting_summary_read_forwards_the_mother(self):
    from core.cannanas_host import read_tool_call
    tool, args, _ = read_tool_call(
        "mother_cutting_summary", {"query": "Ztilton #1"}, {"stable_device_id": "esp32-1"})
    self.assertEqual("mother_cutting_summary", tool)
    self.assertEqual("Ztilton #1", args["mother"])
```

- [ ] **Step 7: Run the host test to verify it fails, then implement, then verify it passes**

Expected: FAIL, nach Task-5-Schritt PASS.

- [ ] **Step 8: Run the full suites and commit**

Beide Suiten grün, dann Adapter `git commit -m "feat(cannanas): count the cuttings per mother plant"` und Mark-LV `git commit -m "feat(voice): report the cuttings per mother plant"`.

---

### Task 4: Tagebuch aus Home-Assistant-Werten

**Files:**
- Create: `/root/Mark-LV-LaCasa/.worktrees/integration/core/growroom_measurements.py`
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_growroom_measurements.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/esp32_voice_workflows.py` (Tagebuch-Workflow: Messungen aus HA, wenn keine gesprochen wurden)
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/scripts/esp32_jarvis_bridge.py` (HA-Client für die Messwerte)

**Interfaces:**
- Consumes: der bestehende Home-Assistant-Client in der Bridge (liest `sensor.zonen_*_temperatur_celsius`); `core/growroom_briefing.py` (liest dieselben Sensoren)
- Produces: `core/growroom_measurements.py` mit `def room_measurements(room: str, sensors: dict) -> dict` → `{"air_temperature": <float>, "air_humidity": <float>}` (nur vorhandene Werte); leeres Dict, wenn nichts bekannt.

- [ ] **Step 1: Write the failing test**

`tests/test_growroom_measurements.py`:

```python
def test_the_room_measurements_map_the_sensors(self):
    sensors = {"zonen_madrid_temperatur_celsius": 24.2,
               "zonen_madrid_luftfeuchte": 61.0,
               "zonen_helsinki_temperatur_celsius": 19.8}
    result = room_measurements("Madrid", sensors)
    self.assertEqual(24.2, result["air_temperature"])
    self.assertEqual(61.0, result["air_humidity"])
    self.assertNotIn("soil_moisture", result)
```

und

```python
def test_an_unknown_room_yields_no_measurements(self):
    self.assertEqual({}, room_measurements("Atlantis", {}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/Mark-LV-LaCasa/.worktrees/integration && /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest tests.test_growroom_measurements`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.growroom_measurements'`

- [ ] **Step 3: Implement the module**

```python
"""Room measurements for a diary entry, taken from the Home Assistant sensors.

The gardener can say "Tagebucheintrag Madrid" and get the measured
values of that room attached. Values that the sensors do not know stay
out; the entry is still worth writing without them.
"""
```

`room_measurements(room, sensors)` normalisiert den Raumnamen (`casefold`, Leerzeichen weg, Umlaute ersetzt) und sucht die Sensoren mit dem passenden Präfix. Die Erkennungsreihenfolge ist exakt die des Briefings (`core/growroom_briefing.py`), damit Briefing und Tagebuch dieselbe Zahl sprechen. Die Schlüssel der Messwerte sind die des Cannanas-`measurements`-Objekts: `air_temperature`, `air_humidity`.

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS (2 Tests)

- [ ] **Step 5: Wire it into the diary workflow**

In `core/esp32_voice_workflows.py` dort, wo der Tagebuch-Prepare die `measurements` setzt: sind keine gesprochenen Messwerte vorhanden **und** ein Raum ist benannt, dann `measurements = room_measurements(room, self._home_assistant_sensors())`. Die Quelle der Sensoren ist eine Callable, die die Bridge injiziert; ohne sie bleibt `measurements` leer und der Eintrag funktioniert trotzdem.

- [ ] **Step 6: Write the failing workflow test**

```python
def test_a_room_diary_takes_the_measured_values(self):
    # Ohne gesprochene Werte, aber mit Sensoren, hängt der Eintrag die
    # gemessenen Werte an.
    ...
    self.assertEqual(24.2, args["measurements"]["air_temperature"])
```

- [ ] **Step 7: Run the workflow test to verify it fails, then implement, then verify it passes**

Expected: FAIL, nach der Implementierung PASS.

- [ ] **Step 8: Run the full suite and commit**

`... -m unittest discover -s tests` grün, dann `git add core/growroom_measurements.py tests/test_growroom_measurements.py core/esp32_voice_workflows.py scripts/esp32_jarvis_bridge.py` mit `git commit -m "feat(voice): fill a room diary with the measured values"`.

---

### Task 5: Chargen-Pflanzen benennen

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (neues Werkzeug `prepare_batch_plant_names`)
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_actions.py`
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_voice_e2e.py`

**Interfaces:**
- Consumes: `_resolve_batch(query)`, `_batch_members(plants, batch)`, `_plant_by_id(id)` (bestehend)
- Produces: Adapter-Werkzeug `prepare_batch_plant_names(batch: str, prefix: str = "", binding: dict | None = None) -> dict`; benennt die Pflanzen **nur** in der Vorschau (`preview["namen"]` = Liste), und der Payload enthält je Pflanze `{"plant_id": ..., "name": ...}`.

- [ ] **Step 1: Write the failing test**

```python
def test_batch_plant_names_preview_lists_one_name_per_plant(self) -> None:
    self.transport.push(
        FakeResponse({"data": [{"id": "b1", "name": None, "notes": "Ztilton #6"}]}),
        FakeResponse({"data": [{"id": "p1", "name": None, "batch_id": "b1"},
                                {"id": "p2", "name": None, "batch_id": "b1"}]}),
    )
    result = self.adapter.prepare_batch_plant_names(batch="Ztilton #6")
    self.assertTrue(result["ok"], result)
    self.assertEqual(2, len(result["preview"]["namen"]))
    self.assertEqual("Ztilton #6 (1)", result["preview"]["namen"][0])
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL with `AttributeError: ... 'prepare_batch_plant_names'`

- [ ] **Step 3: Implement the naming**

```python
def prepare_batch_plant_names(self, batch: str, prefix: str = "",
                              binding: dict | None = None) -> dict[str, Any]:
    """Prepare names for the nameless plants of one batch.

    The plants of a batch carry no name of their own, so they cannot be
    addressed by voice. This prepares one distinct name per plant. A name
    that is already taken in the club is refused, because a second plant
    with the same name would be unaddressable again.
    """
```

Der Body: `batch_item = self._resolve_batch(batch)`; `plants = self.paged_records("plants", "plants")[0]`; `members = self._batch_members(plants, batch_item)`; leere Liste → `ValueError("Diese Charge hat keine Pflanzen")`. Der Basisname ist `prefix.strip()` oder `_grow_label(batch_item)`. Kandidaten: `f"{basis} ({i})"` für `i` ab 1 in der Reihenfolge der Pflanzen-IDs (stabil sortiert). Prüfung: ein Kandidat, der schon als `name` einer Pflanze im Club vorkommt → `ValueError(f"Der Name {name!r} ist schon vergeben")`. Vorschau: `{"batch": _grow_label(batch_item), "namen": <kandidaten>}`. Payload: `{"batch_id": ..., "plants": [{"plant_id": ..., "name": ...}]}`.

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Add the host mapping, the model action and the bridge test**

Host, in `prepare_tool_call`:

```python
if action == 'prepare_batch_plant_names':
    return action, {'batch': plant_name, 'prefix': strain or '',
                    'binding': binding}
```

`core/cannanas_actions.py`: `'prepare_batch_plant_names'` in `CANNANAS_PREPARE_ACTIONS` und `CANNANAS_MODEL_ACTIONS`.

Bridge-Test in `tests/test_cannanas_voice_e2e.py`:

```python
def test_the_naming_reaches_the_adapter_with_the_batch(self):
    ...
    self.assertEqual("Ztilton #6", args["batch"])
```

- [ ] **Step 6: Run the bridge test to verify it fails, then implement, then verify it passes**

- [ ] **Step 7: Run the full suites and commit**

Beide Suiten grün, dann Adapter `git commit -m "feat(cannanas): prepare names for the plants of a batch"` und Mark-LV `git commit -m "feat(voice): name the plants of a batch"`.

---

### Task 6: Mutterpflanze in Reserve markieren

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (`prepare_plant_update`: Feld `in_reserve`)
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py` (`CANNANAS_PLANT_CHANGE_PARAMETERS`)
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_plugin.py`

**Interfaces:**
- Consumes: `PLANT_UPDATE_FIELDS` (Adapter), `_optional_bool(value, label)` (Adapter)
- Produces: Das Adapter-Feld `in_reserve` (bool) landet in `notes` als Marker? **Nein** — der Adapter kennt kein Feld dafür. Stattdessen: `prepare_plant_update` akzeptiert `archived` bereits; „in Reserve“ wird über ein **neues** Feld `in_reserve` geführt, das der Adapter in `tags` schreibt, weil `tags` in `UpdatePlantRequest` existiert. Der Vorschau zeigt `tags`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_plant_can_be_marked_as_in_reserve(self) -> None:
    self.transport.push(
        FakeResponse({"data": [{"id": "p1", "name": "Ztilton #1"}]}),
    )
    result = self.adapter.prepare_plant_update(
        plant="Ztilton #1", fields={"in_reserve": True})
    self.assertTrue(result["ok"], result)
    self.assertIn("reserve", " ".join(result["payload"].get("tags") or []))
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL with `Unbekanntes Feld 'in_reserve'`

- [ ] **Step 3: Implement the marker**

In `_split_plant_update_fields` (Adapter) vor der Feld-Schleife: `in_reserve` aus `fields` herausnehmen, als `bool` prüfen (`_optional_bool`), und `clean["tags"] = sorted(set(bestehende_tags) | {"reserve"})` bzw. `{"reserve"}` entfernen. `in_reserve` **nicht** in `PLANT_UPDATE_FIELDS` aufnehmen (es ist keine API-Feld-Name). Die Vorschau zeigt `in_reserve: True/False`.

Das `tags`-Feld muss in `PLANT_UPDATE_FIELDS` ergänzt werden, damit der Adapter es weiterreicht — es ist ein echtes API-Feld (`UpdatePlantRequest.tags`).

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Wire it into the host**

In `core/cannanas_host.py` `CANNANAS_PLANT_CHANGE_PARAMETERS` `'in_reserve'` ergänzen. Der Host reicht es als Feld durch; der Adapter übersetzt es in `tags`.

- [ ] **Step 6: Write the failing host test**

```python
def test_in_reserve_is_forwarded_as_a_change(self):
    from core.cannanas_host import prepare_tool_call
    _tool, args = prepare_tool_call(
        "prepare_plant_update",
        {"plant_name": "Ztilton #1", "fields": {"in_reserve": True}},
        {"stable_device_id": "esp32-1"})
    self.assertEqual(True, args["fields"]["in_reserve"])
```

- [ ] **Step 7: Run the host test to verify it fails, then implement, then verify it passes**

- [ ] **Step 8: Run the full suites and commit**

Beide Suiten grün, dann Adapter `git commit -m "feat(cannanas): mark a mother plant as in reserve"` und Mark-LV `git commit -m "feat(voice): mark a mother plant as in reserve"`.

---

### Task 7: Gieß-Intervall aus dem Tagebuch

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py` (neues Lese-Werkzeug `watering_intervals`)
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_client.py` (`ADAPTER_TOOLS`, `MCP_READ_TOOLS`)
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_actions.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py` (`read_tool_call`)
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_plugin.py`

**Interfaces:**
- Consumes: `paged_records("diaries", "diaries")`, `_resolve_zone(query)` (bestehend)
- Produces: Adapter-Werkzeug `watering_intervals(room: str = "") -> dict` mit `{"ok": True, "raeume": [{"raum": <label>, "tage_seit_letztem_gies": <int>|None, "letzter_eintrag": <text>}], "hints": {"giessen": ["<label>"]}}`; `giessen` listet die Räume, die länger nicht gegossen wurden.

- [ ] **Step 1: Write the failing test**

```python
def test_the_watering_interval_names_the_driest_rooms(self) -> None:
    self.transport.push(
        FakeResponse({"data": [{"id": "z1", "name": "Madrid"}]}),
        FakeResponse({"data": [
            {"id": "d1", "zone_id": "z1", "text": "Giessen",
             "created_at": "2026-10-01T09:00:00Z"}]}),
    )
    result = self.adapter.watering_intervals(room="Madrid")
    self.assertTrue(result["ok"], result)
    entry = result["raeume"][0]
    self.assertGreaterEqual(entry["tage_seit_letztem_gies"], 19)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL with `AttributeError: ... 'watering_intervals'`

- [ ] **Step 3: Implement the interval**

```python
def watering_intervals(self, room: str = "") -> dict[str, Any]:
    """How long each room has gone without a watering entry.

    A diary entry counts as watering when its text mentions watering.
    Rooms without any entry are not guessed.
    """
```

Der Body: `zones, _ = self.paged_records("zones", "zones")`; `diaries, _ = self.paged_records("diaries", "diaries")`; je Zone die jüngste Tagebuchzeile mit Giessen-Bezug (`_WATER_WORDS = ("giess", "gieß", "wass", "bewässer", "bewaesser", "watering")` in `casefold`); `tage_seit_letztem_gies = (heute - created_at.date()).days`; `letzter_eintrag` = der Text des jüngsten Eintrags (auch eines Nicht-Gieß-Eintrags). `hints["giessen"]` = die Räume, deren `tage_seit_letztem_gies` größer als der konfigurierbare Schwellwert (Default 7) ist. Ohne jeden Eintrag: `tage_seit_letztem_gies` = `None`, Raum erscheint nicht in `hints`.

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Wire it through the host**

`core/cannanas_client.py`: `watering_intervals` in `ADAPTER_TOOLS()` **und** `MCP_READ_TOOLS`. `core/cannanas_actions.py`: in `CANNANAS_READ_ACTIONS` und `CANNANAS_MODEL_ACTIONS`. `core/cannanas_host.py`, `read_tool_call`:

```python
if action == 'watering_intervals':
    return ('watering_intervals', {'room': plant_name or room or query}, binding)
```

- [ ] **Step 6: Write the failing host test**

```python
def test_the_watering_interval_read_forwards_the_room(self):
    from core.cannanas_host import read_tool_call
    tool, args, _ = read_tool_call(
        "watering_intervals", {"room": "Madrid"}, {"stable_device_id": "esp32-1"})
    self.assertEqual("watering_intervals", tool)
    self.assertEqual("Madrid", args["room"])
```

- [ ] **Step 7: Run the host test to verify it fails, then implement, then verify it passes**

- [ ] **Step 8: Run the full suites and commit**

Beide Suiten grün, dann Adapter `git commit -m "feat(cannanas): read the watering intervals per room"` und Mark-LV `git commit -m "feat(voice): report the watering intervals"`.

---

### Task 8: Release, Deployment, Live-Verifikation, Doku

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/pyproject.toml`, `src/cannanas_mcp_adapter/_release.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_client.py` (Pinning)
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/docs/cannanas-grow-funktionsliste.md`
- Modify: `/root/Documents/second-brain/KI_Protokolle/Sitzungen/2026-10-20_Cannanas-Grow-Vollabdeckung.md`

**Interfaces:**
- Consumes: alle Adapter-Werkzeuge aus Task 1–7 und die `EXPECTED_TOOLS`-Liste
- Produces: Adapter-Release 2.5.0 auf lacasa-server, gepinnter Wheel-SHA in Mark-LV, aktualisierte Funktionsliste, Protokoll

- [ ] **Step 1: Bump the adapter version and build the release**

```bash
cd /home/lacasa15/services/cannanas-mcp/adapter
# pyproject.toml version = "2.5.0", _release.py ADAPTER_VERSION = "2.5.0"
PYTHON=/root/.hermes/cache/scratch/marklv-venv/bin/python bash scripts/build-release.sh
```

`EXPECTED_TOOLS` in `_release.py` muss die neuen Werkzeuge enthalten: `prepare_batch_move`, `prepare_batch_plant_names`, `mother_cutting_summary`, `watering_intervals`.

- [ ] **Step 2: Run the adapter suite and read the wheel SHA**

Run: `cd /home/lacasa15/services/cannanas-mcp/adapter && PYTHONPATH=src ... -m unittest discover -s tests -t .`
Expected: grün. Den Wheel-SHA aus `release/release-manifest-v2.5.0.json` ablesen.

- [ ] **Step 3: Install on lacasa-server**

```bash
scp -q dist/cannanas_mcp_adapter-2.5.0-py3-none-any.whl constraints-lock.txt lacasa@lacasa-server:/home/lacasa/cannanas-adapter/
scp -q release/release_info.json lacasa@lacasa-server:/home/lacasa/cannanas-adapter/release_info.new.json
ssh lacasa@lacasa-server 'cd /home/lacasa/cannanas-adapter && printf "cannanas-mcp-adapter==2.5.0 --hash=sha256:<SHA>\n" "$(sha256sum cannanas_mcp_adapter-2.5.0-py3-none-any.whl | cut -d" " -f1)" > /tmp/spec.txt && cat constraints-lock.txt >> /tmp/spec.txt && venv/bin/pip install -q --no-deps --require-hashes --find-links . -r /tmp/spec.txt && rm -f /tmp/spec.txt && install -m 600 release_info.new.json venv/bin/release_info.json && rm -f release_info.new.json && venv/bin/cannanas-mcp-adapter --adapter-info | head -c 400'
```

Expected: `VERSION=2.5.0`, `PINNED=true`, `TOOLS=38`.

- [ ] **Step 4: Pin the new release in Mark-LV**

In `core/cannanas_client.py`: `PINNED_ADAPTER_VERSION = '2.5.0'` und `PINNED_WHEEL_SHA256 = '<SHA> aus Step 2>'`. Dieselbe Werte in `tests/test_cannanas_client.py`, `tests/test_cannanas_integration.py` und `tests/fixtures/fake_adapter.py` (dort die `adapter_version`).

- [ ] **Step 5: Run the Mark-LV suite**

Run: `cd /root/Mark-LV-LaCasa/.worktrees/integration && /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest discover -s tests`
Expected: grün.

- [ ] **Step 6: Commit and push both repos**

Adapter: `git add -A && git commit -m "release: 2.5.0 gardener tools"`.
Mark-LV: `git add core/cannanas_client.py tests/... && git commit -m "chore(cannanas): pin adapter 2.5.0"` und `git push origin feat/esp32-satellite-phase1`.

- [ ] **Step 7: Deploy to lacasa-server and restart the bridge**

```bash
ssh lacasa@lacasa-server 'cd /home/lacasa/Mark-LV-LaCasa-esp32 && git fetch origin feat/esp32-satellite-phase1 -q && git checkout -q --detach origin/feat/esp32-satellite-phase1 && git rev-parse --short HEAD && systemctl --user restart esp32-jarvis-bridge.service && sleep 8 && systemctl --user is-active esp32-jarvis-bridge.service'
```

Expected: `active`, Satellit verbunden (`satellite connected` im Journal).

- [ ] **Step 8: Live verification (read-only, no confirm)**

Ein Skript `/home/lacasa/waveshare-va-test/verify_250.py` auf lacasa-server, das über den Adapter `mother_cutting_summary`, `watering_intervals` und die Lese-Tools `list_mother_plants`, `zone_growth_summary` aufruft und je `ok`/Zusammenfassung ausgibt. **Kein** `confirm_*`, **kein** Write. Erwartung: alle `ok=true` mit Inhalt.

- [ ] **Step 9: Update the feature list and the protocol**

In `docs/cannanas-grow-funktionsliste.md`: die sieben Vorschläge von 🟡 auf ✅ (mit dem Adapter-Stand im Text) setzen, den Abschnitt „Was die Sprachsteuerung heute einschränkt“ um die Punkte ergänzen, die jetzt gelten (Pflanzen sind nach dem Benennen ansprechbar). Testzahlen und Adapter-Version aktualisieren.

Im Second-Brain-Protokoll `2026-10-20_Cannanas-Grow-Vollabdeckung.md`: die Release-Tabelle um 2.5.0 ergänzen, die sieben Funktionen mit their Sprachbeispielen nennen, die Nachweise aktualisieren. Commit `docs: Cannanas Gaertner-Werkzeuge` und pushen.

- [ ] **Step 10: Independent review**

Ein frischer Review-Agent prüft die sieben Task-Diffs: die Allowlist (kein neuer Pfad ohne Eintrag), die geschlossene Enum (Modell-Aktion ↔ Bridge-Schema ↔ Adapter-Werkzeug), die Boot-Fence (Pending trägt die Adapter-Boot-ID), das Fail-closed (kein Schreibweg ohne Vorschau), die Geheimnisfreiheit und die Genauigkeit der Funktionsliste. Befunde mit Schwere beheben, dann beide Suiten erneut grün.

---

## Abhängigkeiten

```
Task 1 (Batch-Bewegung)  ─┐
Task 2 (Status aus Datum) ─┤
Task 3 (Stecklinge)      ─┼─> Task 8 (Release, Deploy, Doku)
Task 4 (HA-Messwerte)    ─┤
Task 5 (Benennung)       ─┤
Task 6 (Reserve)         ─┤
Task 7 (Gieß-Intervall)  ─┘
```

Task 1–7 sind untereinander unabhängig (verschiedene Werkzeuge, verschiedene Host-Zweige) und können parallel mit Subagenten umgesetzt werden. Task 8 braucht alle sieben, weil ein Release sie gemeinsam pinnt.
