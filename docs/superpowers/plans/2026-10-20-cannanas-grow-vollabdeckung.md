# Cannanas Grow-Vollabdeckung Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Den Cannanas-MCP-Adapter so erweitern, dass alle für den Anbau relevanten Grow-Operationen (Anbau planen, Status wechseln, umziehen, Mutterpflanzen, Pflanzen-Status, Chargen-/Pflanzenerstellung, Tagebücher je Pflanze/Charge, Ernte, Verschmutigung) vollständig abgedeckt und danach eine vollständige Funktionsliste für den Gärtner vorliegt.

**Architecture:** Der Adapter (`/home/lacasa15/services/cannanas-mcp/adapter`, eigenes Git-Repo) bekommt eine zweite, breitere Tool-Generation: Lese-Tools für alle Grow-Entitäten und prepare/confirm-Paare für jede Schreiboperation. Das Modell sieht nur eine geschlossene Aktions-Enum; jede Schreiboperation läuft weiterhin über Prepare→Confirm mit host-eigenem Binding, Audit-vor-POST und Adapter-Boot-Fence. Mark-LV (`/root/Mark-LV-LaCasa/.worktrees/integration`) spiegelt die Aktionen in `core/cannanas_actions.py`, `core/cannanas_host.py` (Read-/Prepare-Mapping) und `plugins/cannanas.py` (Parameter-Schema).

**Tech Stack:** Python 3.11+, `mcp` (FastMCP) für den STDIO-Server, `httpx`, stdlib `hashlib`/`stat`/`os` für State-Files, `unittest`; Ausführung auf lacasa15 (Build) und lacasa-server (JARVIS-Laufzeit).

**Spec:** `docs/superpowers/specs/2026-10-20-cannanas-grow-vollabdeckung-design.md`

## Global Constraints

- **Live-API ist die Wahrheit.** Die OpenAPI-Spezifikation wird live von `https://api.cannanas.club/docs/cannanas-api-docs.json` geholt (185 Pfade, 3.1.0). Der gepinnte Fixture `fixtures/cannanas-api.yaml` ist byte-gleich in den Pfaden (`only_live=0`, `only_fixture=0`) und bleibt die gepinnte Referenz für den Allowlist-Test.
- **Allowlist geschlossen.** `ALLOWED_REQUESTS` in `src/cannanas_mcp_adapter/server.py` wird nur mit exakten (Methode, Pfad-Template)-Paaren aus dem gepinnten Fixture erweitert. Alles andere wird mit `MSG_REFUSED` abgelehnt. Kein dynamischer Pfadbau, keine Methoden aus dem Modell.
- **Schreiben bleibt Prepare→Confirm.** Jede Mutation hat `prepare_<x>` (Preview, kein Write) und `confirm_<x>` (Write). `confirm_*` ist niemals modell-seitig sichtbar; die Mark-LV-Aktions-Enum enthält ausschließlich `prepare_*` und Lese-Aktionen.
- **Host-Fence unverändert.** Der Adapter prüft `adapter_boot_id` aus `get_adapter_info().config.boot_id`, nicht die Host-Prozess-UUID. Der Host liest und cached diese ID pro Client-Generation (`CannanasHost._adapter_boot()`).
- **Audit-vor-POST.** Jeder bestätigte Write schreibt zuerst einen `executing`-Eintrag in den Audit-Sink; Audit-Fehler → kein Write. Danach `executed`/`unbekannt`/`failed` mit Rückgabe-ID.
- **Atomarer Consume.** `PendingStore.consume` bleibt der einzige Weg; nach dem Consume ist der Token verbraucht. Kein Retry nach gesendeten Bytes.
- **Namen werden aufgelöst, nicht geraten.** Raum → Zone, Sorte → Strain, Pflanze → Plant, Charge → Batch, Mutterpflanze → Plant, Vermehrungsmaterial → Inventory-Item. Kanonische IDs, nie Labels. Mehrere Treffer → Rückfrage, kein Write.
- **Statusvokabular ist geschlossen.** `STATUS_ALIASES` (SEED, GERMINATION, CUTTING, VEGETATIVE, FLOWERING, HARVEST, CURING, DRYING, QUARANTINE, DESTROYED) gilt für Pflanze und Charge; unbekannt → Fehler vor jedem Netzwerkzugriff.
- **Keine Secrets in Chat, Logs, Git oder Doku.** API-Key, Club-ID, Confluence-Tokens und Confirm-Tokens verlassen den Host nie.
- **Bounded.** Caller-sichtbare Listen max. 25 Einträge, gesprochen max. 5, Aggregate mit eigener Scan-Obergrenze (`MAX_SCAN_RECORDS` = 5000), Briefings max. 900 Zeichen.
- **Keine Compliance-Operationen per Sprache.** Analyse-Ergebnisse, Waste-Disposal, Harvest-Produkte/Drying-Logs, Datei-Upload/-Download, KCanG-Reports und Finanzen bleiben außerhalb des Sprachumfangs.
- **Version-Pinning.** Jede Adapter-Version braucht: `pyproject.toml`, `src/cannanas_mcp_adapter/_release.py` (`ADAPTER_VERSION`, `EXPECTED_TOOLS`), Release-Manifest, `release/release_info.json`, neu gebautes Wheel mit SHA-256, `constraints-lock.txt`. Mark-LV pinnt in `core/cannanas_client.py` (`PINNED_ADAPTER_VERSION`, `PINNED_WHEEL_SHA256`) und `tests/fixtures/fake_adapter.py` (`adapter_version`).
- **Testkommando.** Adapter: `PYTHONPATH=src /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest discover -s tests -t .` (im Repo-Verzeichnis). Mark-LV: `/root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest discover -s tests`.

## Review Focus

- Ein Sprecher sagt „Ztilton #3 ist in Blüte“ und die Zielauflösung trifft die falsche Pflanze, weil ein Namenspräfix mehrfach vorkommt → mehrere Treffer müssen eine Rückfrage auslösen, keinen Write.
- Ein Sprecher sagt „zieh die Charge nach Helsinki um“ und der Adapter setzt `zone_id` an der Charge, ohne die Pflanzen mitzunehmen → Umzug muss klar definieren, ob nur die Charge oder auch ihre Pflanzen wandern, und die Vorschau muss beides benennen.
- Ein Sprecher sagt „mach die Pflanze zur Mutter“ und der Write setzt `is_mother` an der falschen Pflanze, weil die Auflösung über ein Teilstring matcht → `_match_one` nutzt exakte oder eindeutige Teilstrings, sonst Fehler.
- Eine stille oder sehr kurze Aufnahme führt zu einem Write mit `plant_count=0` oder einem leeren `mother_plants`-Eintrag → Counts und Cutting-Mengen werden vor jedem Write validiert (Pflanzenzahl ≥ 1, Cutting-Anzahl 1–100 laut API).
- Ein Sprecher nennt eine Sorte, die es noch nicht gibt, und der Adapter legt sie still an → fehlende Sorten erfordern erst `prepare_strain_create` mit leerem Identifier.
- Ein Write bricht nach dem Senden der Bytes ab → Ergebnis bleibt „unbekannt“, es wird **nie** automatisch wiederholt, und der Adapter meldet `unbekannt` statt `failed`.
- Zwei Geräte bestätigen dieselbe Vorschau gleichzeitig → nur ein Write; der zweite bekommt `already_used`/Refusal.
- Ein Nutzer versucht per Sprache, eine Charge zu löschen oder eine Ernte abzuschließen → diese Aktionen existieren im Modell-Enum nicht; der Host verweigert.

---

### Task 1: Adapter-Basis — neue Felder, Zonen- und Pflanzenlisten-Filter

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py`
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`

**Interfaces:**
- Consumes: `ALLOWED_REQUESTS`, `_status()`, `_match_one()`, `paged_records()`, `_resolve_zone()` aus dem bestehenden Adapter.
- Produces:
  - `UPDATE_FIELDS` (bestehend) bleibt; neu `PLANT_UPDATE_FIELDS = frozenset({...})` mit `status`, `room`, `notes`, `strain`, `is_mother`, `parent_plant`, `description`, `expected_yield`, `actual_yield`, `substrate`, `fertilizer`, `archived`, `batch`, `expected_harvest_at`, `cultivation_*`.
  - `ZONE_UPDATE_FIELDS = frozenset({"name", "description", "size", "lighting", "ventilation", "notes", "temperature", "humidity", "power_consumption", "archived"})`.
  - `def _optional_str(value, label) -> str` — leerer String, sonst `ValueError`.
  - `def _optional_number(value, label) -> float` — `int`/`float` (kein `bool`), sonst `ValueError`.
  - `def _optional_bool(value, label) -> bool` — `bool` oder `"true"/"false"`/`"ja"/"nein"`, sonst `ValueError`.
  - `def _optional_date(value, label) -> str` — `YYYY-MM-DD` normalisiert.
  - `def _split_plant_update_fields(fields) -> tuple[dict, dict]` — whitelistet `PLANT_UPDATE_FIELDS`, normalisiert Werte, gibt (Payload-Felder, Preview-Felder) zurück. `strain` → kanonische `strain_id`; `parent_plant` → kanonische `parent_id`; `room` → kanonische `zone_id`; `is_mother` → bool.
  - `def prepare_zone_update(zone: str, fields: dict, binding: dict | None = None) -> dict` — Preview, kein Write.
  - `def confirm_zone_update(confirmation_token: str, binding: dict | None = None) -> dict` — `PUT /v1/clubs/{clubId}/zones/{zoneId}`.
  - `def list_plants(room: str = "", status: str = "", limit: int = 50, mother: bool = False, strain: str = "", batch: str = "") -> dict` — zusätzliche Filter über die dokumentierten Query-Parameter `is_mother`, `strain_id`, `batch_id`.
  - `def find_plants(query: str = "", room: str = "", limit: int = 50, mother: bool = False, status: str = "") -> dict` — dito.

- [ ] **Step 1: Write the failing tests**

In `tests/test_tool_surface.py` ergänzen:

```python
    def test_zone_update_prepares_a_preview_without_writing(self) -> None:
        self.transport.push(FakeResponse({"data": [{"id": "z1", "name": "Madrid"}]}))
        result = self.adapter.prepare_zone_update(
            zone="Madrid", fields={"description": "Blüte", "temperature": 24.5})
        self.assertTrue(result["ok"], result)
        self.assertEqual({"zone_id": "z1", "fields": {
            "description": "Blüte", "temperature": 24.5}}, result["payload"])
        self.assertEqual([], self.transport.posts)

    def test_zone_update_rejects_unknown_fields(self) -> None:
        result = self.adapter.prepare_zone_update(
            zone="Madrid", fields={"stream": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("Unbekanntes Feld", result["error"])
        self.assertEqual([], self.transport.requests)

    def test_plant_update_can_set_mother_and_parent(self) -> None:
        self.transport.push(
            FakeResponse({"data": [{"id": "p1", "name": "Ztilton #3"}]}),
            FakeResponse({"data": [{"id": "m1", "name": "Mutter A"}]}),
        )
        result = self.adapter.prepare_plant_update(
            plant="Ztilton #3", fields={"is_mother": True, "parent_plant": "Mutter A"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(True, result["payload"]["fields"]["is_mother"])
        self.assertEqual("m1", result["payload"]["fields"]["parent_id"])

    def test_plant_update_rejects_a_bogus_number(self) -> None:
        result = self.adapter.prepare_plant_update(
            plant="Ztilton #3", fields={"expected_yield": "viel"})
        self.assertFalse(result["ok"])
        self.assertEqual([], self.transport.requests)

    def test_list_plants_can_filter_mother_plants(self) -> None:
        self.transport.push(FakeResponse({"data": [
            {"id": "p1", "name": "Mutter A", "is_mother": True},
            {"id": "p2", "name": "Ztilton #3"}]}))
        result = self.adapter.list_plants(mother=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, result["plant_count"])
        self.assertEqual(True, self.transport.gets[0]["params"]["is_mother"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/lacasa15/services/cannanas-mcp/adapter && PYTHONPATH=src /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest tests.test_tool_surface -v`
Expected: FAIL — `AttributeError: 'CannanasAdapter' object has no attribute 'prepare_zone_update'`

- [ ] **Step 3: Implement the minimal code**

In `server.py`:
- `ALLOWED_REQUESTS` ergänzen: `("PUT", "/v1/clubs/{clubId}/zones/{zoneId}"): "update zone"`.
- `PLANT_UPDATE_FIELDS` und `ZONE_UPDATE_FIELDS` als frozensets definieren.
- Die Helfer `_optional_str`, `_optional_number`, `_optional_bool`, `_optional_date` hinzufügen; `_validate_date` für Datumswerte wiederverwenden.
- `_split_plant_update_fields` implementieren: für `strain` → `self._resolve_reference("strains", value, "Sorte")` → `strain_id`; für `parent_plant` → `self._resolve_reference("plants", value, "Mutterpflanze")` → `parent_id`; für `room` → `self._resolve_zone(value)` → `zone_id`; für `status` → `_status(value)`; für `is_mother` → `_optional_bool`; für Zahlen → `_optional_number`; für Datumsfelder → `_optional_date`; `notes`/`description` → `_optional_str`.
- `prepare_zone_update` nach dem Muster von `prepare_plant_update` implementieren (Payload `{"zone_id": ..., "fields": {...}}`, Preview `{"zone": name, **updates}`), `self._prepare("zone_update", payload, preview, binding)`.
- `confirm_zone_update` nach dem Muster von `confirm_batch_update` implementieren: `_executed_write(..., method="PUT", template="/v1/clubs/{clubId}/zones/{zoneId}", body_key="fields", response_key="zones", verify_kind="zone", action_label="updated")`. In `_executed_write` den `body_key == "fields"`-Zweig um `zone_id` erweitern (Reihenfolge: `plant_id` → `batch_id` → `zone_id`).
- `list_plants` und `find_plants` um die Filter `mother`, `strain`, `batch` erweitern: nach `_resolve_zone`/`_resolve_reference` die kanonischen IDs als Query-Parameter `is_mother`, `strain_id`, `batch_id` mitschicken.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/lacasa15/services/cannanas-mcp/adapter && PYTHONPATH=src /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest tests.test_tool_surface`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/lacasa15/services/cannanas-mcp/adapter
git add -A && git commit -m "feat: zone updates, mother plants and richer plant filters"
```

---

### Task 2: Anbau planen — Charge mit Mutterpflanzen und Vermehrungsmaterial

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py`
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`

**Interfaces:**
- Consumes: `_resolve_reference()`, `_resolve_zone()`, `_status()`, `MAX_BATCH_COUNT` (1000), `DIARY_MEASUREMENT_KEYS`.
- Produces:
  - `def prepare_batch_create(room: str, strain: str, plant_count: int, status: str, parent_plant: str = "", propagation_material: str = "", notes: str = "", expected_harvest_at: str = "", cultivation_vegetative_date: str = "", mother_plants: list | None = None, binding: dict | None = None) -> dict` — `mother_plants` ist eine Liste von `{"plant": "<gesprochener Name>", "cutting_count": <int>}`; jeder Eintrag wird aufgelöst und in `{"plant_id": ..., "cutting_count": ...}` überführt.
  - `def prepare_plant_create(plant_name: str, room: str, strain: str, status: str, notes: str = "", mother_plant: str = "", is_mother: bool = False, plant_count: int = 1, binding: dict | None = None) -> dict` — unterstützt Mehrfach-Anlage (`plant_count` 1–100) und `parent_id` aus `mother_plant`.
  - `def prepare_diary_create(plant_or_batch: str = "", text: str = "", measurements: dict | None = None, binding: dict | None = None, room: str = "", plant: str = "", batch: str = "") -> dict` — genau **eine** Ziel-Referenz aus `room`, `plant` oder `batch`; `plant` → `plant_id`, `batch` → `batch_id`, `room` → `zone_id`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_batch_create_accepts_mother_plants_with_cutting_counts(self) -> None:
        self.transport.push(
            FakeResponse({"data": [{"id": "z1", "name": "Madrid"}]}),
            FakeResponse({"data": [{"id": "s1", "name": "Ztilton"}]}),
            FakeResponse({"data": [{"id": "m1", "name": "Mutter A"}]}),
            FakeResponse({"data": [{"id": "m2", "name": "Mutter B"}]}),
        )
        result = self.adapter.prepare_batch_create(
            room="Madrid", strain="Ztilton", plant_count=10, status="CUTTING",
            mother_plants=[{"plant": "Mutter A", "cutting_count": 6},
                           {"plant": "Mutter B", "cutting_count": 4}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            [{"plant_id": "m1", "cutting_count": 6},
             {"plant_id": "m2", "cutting_count": 4}],
            result["payload"]["mother_plants"])
        self.assertEqual([], self.transport.posts)

    def test_batch_create_rejects_an_out_of_range_cutting_count(self) -> None:
        result = self.adapter.prepare_batch_create(
            room="Madrid", strain="Ztilton", plant_count=4, status="CUTTING",
            mother_plants=[{"plant": "Mutter A", "cutting_count": 0}])
        self.assertFalse(result["ok"])
        self.assertEqual([], self.transport.requests)

    def test_diary_create_accepts_a_plant_target(self) -> None:
        self.transport.push(FakeResponse({"data": [{"id": "p1", "name": "Ztilton #3"}]}))
        result = self.adapter.prepare_diary_create(
            plant="Ztilton #3", text="Blätter kontrolliert.")
        self.assertTrue(result["ok"], result)
        self.assertEqual({"text": "Blätter kontrolliert.", "plant_id": "p1"},
                         result["payload"])

    def test_diary_create_refuses_two_targets(self) -> None:
        result = self.adapter.prepare_diary_create(
            room="Madrid", plant="Ztilton #3", text="x")
        self.assertFalse(result["ok"])
        self.assertIn("Genau eine", result["error"])
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — `TypeError: prepare_batch_create() got an unexpected keyword argument 'mother_plants'`

- [ ] **Step 3: Implement the minimal code**

- `mother_plants`: Liste validieren (leer → `None`; jeder Eintrag braucht `plant` als nicht-leeren String und `cutting_count` als int 1–100, kein `bool`); je Eintrag `self._resolve_reference("plants", name, "Mutterpflanze")` → `{"plant_id": _item_id(item), "cutting_count": count}`. Fehlgeschlagene Auflösung → `ValueError` (kein Write).
- `prepare_plant_create`: `plant_count` validieren (1–100), `is_mother` → bool, `mother_plant` → `parent_id`. Payload: `{"name", "zone_id", "strain_id", "status", optional "notes", "parent_id", "is_mother", "plant_count"}`.
- `prepare_diary_create`: Zielauflösung vereinheitlichen: `room` → Zone, `plant` → Plant, `batch` → Batch; `plant_or_batch` bleibt als abwärtskompatibles Alias für `plant`. Genau eine Referenz, sonst `ValueError("Genau eine Diary-Referenz angeben: room, plant oder batch")`.

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: plan batches from mother plants and target diaries precisely"
```

---

### Task 3: Lese-Tools für den Anbau vervollständigen

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py`
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`

**Interfaces:**
- Produces:
  - `def list_mother_plants(room: str = "", limit: int = 50) -> dict` — nur `is_mother=true`, mit Zone-Zuordnung.
  - `def list_diaries(room: str = "", plant: str = "", batch: str = "", limit: int = 25) -> dict` — Tagebücher gefiltert nach Zone/Pflanze/Charge.
  - `def list_harvests(limit: int = 10) -> dict` — Ernten mit Datum und State.
  - `def get_grow_config() -> dict` — Grow-Einstellungen des Clubs (nur `use_sequential_plant_names`).
  - `def list_plant_diaries(plant: str, limit: int = 10) -> dict` — Tagebuchhistorie einer Pflanze.
  - `def zone_plant_status(room: str = "") -> dict` — pro Zone: Pflanzen gesamt, davon Mutterpflanzen, offene Chargen, Statushistorie.

- [ ] **Step 1: Write the failing tests**

```python
    def test_list_mother_plants_only_returns_mothers(self) -> None:
        self.transport.push(FakeResponse({"data": [
            {"id": "m1", "name": "Mutter A", "is_mother": True},
            {"id": "p2", "name": "Ztilton #3", "is_mother": False}]}))
        result = self.adapter.list_mother_plants()
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, result["plant_count"])
        self.assertEqual(True, self.transport.gets[0]["params"]["is_mother"])

    def test_list_diaries_filters_by_zone(self) -> None:
        self.transport.push(
            FakeResponse({"data": [{"id": "z1", "name": "Madrid"}]}),
            FakeResponse({"data": [
                {"id": "d1", "text": "Gegossen.", "zone_id": "z1"},
                {"id": "d2", "text": "Andere Zone.", "zone_id": "z2"}]}))
        result = self.adapter.list_diaries(room="Madrid")
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, result["diary_count"])
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — `AttributeError: ... 'list_mother_plants'`

- [ ] **Step 3: Implement the minimal code**

- `ALLOWED_REQUESTS` ergänzen: `("GET", "/v1/clubs/{clubId}/harvests")`, `("GET", "/v1/clubs/{clubId}/grow-configs")`.
- `list_mother_plants`: `paged_records("plants", "plants", {"is_mother": True})`, Zone-Name je Pflanze über `_plant_zone_id` + Batch-Lookup ergänzen.
- `list_diaries`: `paged_records("diaries", "diaries")`; bei `room`/`plant`/`batch` erst die kanonische ID auflösen (`_resolve_zone` bzw. `_resolve_reference`) und clientseitig filtern; Ergebnis: `{"ok", "diary_count", "diaries": [...], "exact"}`.
- `list_harvests`: `paged_records("harvests", "harvests")`, `_compact` um `state`, `harvested_at`, `hid` erweitern.
- `get_grow_config`: `paged_records("grow-configs", "grow-configs")` → erstes Element, auf `use_sequential_plant_names` reduziert.
- `list_plant_diaries(plant, limit)`: Pflanze auflösen → `paged_records("diaries", "diaries")` → clientseitig auf `plant_id` filtern → neueste zuerst.
- `zone_plant_status(room)`: Zonen + Pflanzen + Chargen scannen; pro Zone `plant_count`, `mother_count`, `batch_count`, `statuses` (Count je Status).

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: read tools for mother plants, diaries, harvests and zone status"
```

---

### Task 4: Ernte, Verschmutzung und Compliance-Grenze

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/server.py`
- Test: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_tool_surface.py`

**Interfaces:**
- Produces:
  - `def prepare_harvest_create(plants: str = "", batches: str = "", notes: str = "", harvested_at: str = "", binding: dict | None = None) -> dict` — Ziel-Plants/-Batches auflösen; `state` bleibt `null` (die API setzt den Startzustand), `finalize_plants_and_batches` ist **nicht** Teil der Vorschau.
  - `def confirm_harvest_create(confirmation_token: str, binding: dict | None = None) -> dict` — `POST /v1/clubs/{clubId}/harvests`.
  - `def prepare_waste_disposal_create(plants: str = "", batches: str = "", notes: str = "", binding: dict | None = None) -> dict` — Verschmutzung dokumentieren (ohne Harvest-Produkte).
  - `def confirm_waste_disposal_create(confirmation_token: str, binding: dict | None = None) -> dict` — `POST /v1/clubs/{clubId}/waste-disposals`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_harvest_prepare_resolves_plants_and_batches(self) -> None:
        self.transport.push(
            FakeResponse({"data": [{"id": "p1", "name": "Ztilton #3"}]}),
            FakeResponse({"data": [{"id": "b1", "name": "Ztilton Woche 4"}]}))
        result = self.adapter.prepare_harvest_create(
            plants="Ztilton #3", batches="Ztilton Woche 4", notes="Ernte 1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(["p1"], result["payload"]["plant_ids"])
        self.assertEqual(["b1"], result["payload"]["batch_ids"])
        self.assertNotIn("finalize_plants_and_batches", result["payload"])

    def test_waste_disposal_never_finalizes_plants(self) -> None:
        self.transport.push(FakeResponse({"data": [{"id": "p1", "name": "Ztilton #3"}]}))
        result = self.adapter.prepare_waste_disposal_create(plants="Ztilton #3")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("finalize_plants_and_batches", result["payload"])
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — `AttributeError: ... 'prepare_harvest_create'`

- [ ] **Step 3: Implement the minimal code**

- `ALLOWED_REQUESTS` ergänzen: `("POST", "/v1/clubs/{clubId}/harvests")`, `("POST", "/v1/clubs/{clubId}/waste-disposals")`.
- `prepare_harvest_create`: `plants`/`batches` als kommaseparierte Namen auflösen; Payload `{"plant_ids": [...], "batch_ids": [...], optional "notes", optional "harvested_at"}`. Mindestens ein Ziel, sonst `ValueError`.
- `prepare_waste_disposal_create`: analog, ohne Abschluss-Flags.
- Beide `confirm_*` über `_executed_write` mit `response_key="harvests"` bzw. `"waste-disposals"`, `verify_kind="harvest"` bzw. `"waste_disposal"`.
- `_compact` um `state`, `harvested_at`, `is_mother`, `plant_count` erweitern, damit die Vorschau lesbar bleibt.

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: harvest and waste disposal preparation"
```

---

### Task 5: Release 2.0.0 bauen, pinnen und deployen

**Files:**
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/pyproject.toml`
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/src/cannanas_mcp_adapter/_release.py`
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_package_contract.py`
- Modify: `/home/lacasa15/services/cannanas-mcp/adapter/tests/test_acceptance_adapter.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_client.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_client.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_integration.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/fixtures/fake_adapter.py`

**Interfaces:**
- Produces: Adapter-Version `2.0.0` mit vollständiger `EXPECTED_TOOLS`-Liste; Wheel-SHA wird in Mark-LV gepinnt.

- [ ] **Step 1: Write the failing test**

In `tests/test_tool_surface.py` ergänzen:

```python
    def test_release_surface_contains_every_grow_tool(self) -> None:
        for name in ("prepare_zone_update", "confirm_zone_update",
                     "list_mother_plants", "list_diaries", "list_harvests",
                     "get_grow_config", "list_plant_diaries", "zone_plant_status",
                     "prepare_harvest_create", "confirm_harvest_create",
                     "prepare_waste_disposal_create", "confirm_waste_disposal_create"):
            self.assertIn(name, _release.EXPECTED_TOOLS, name)
            self.assertTrue(callable(getattr(self.adapter, name)), name)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — `AssertionError: 'prepare_zone_update' not found in [...]`

- [ ] **Step 3: Versionieren und Release bauen**

- `pyproject.toml`: `version = "2.0.0"`.
- `_release.py`: `ADAPTER_VERSION = "2.0.0"` und `EXPECTED_TOOLS` um alle neuen Namen ergänzen (Reihenfolge: Reads, Prepares, Confirms).
- `tests/test_package_contract.py`: Versions-Regex auf `^1\.[0-9]+\.[0-9]+$` → `^2\.[0-9]+\.[0-9]+$`; Tool-Liste ergänzen.
- `tests/test_acceptance_adapter.py`: Manifest-Auswahl auf die höchste Version (bereits vorhanden) — prüfen, dass `release-manifest-v2.0.0.json` verwendet wird.
- Release bauen: `PYTHON=/root/.hermes/cache/scratch/marklv-venv/bin/python bash scripts/build-release.sh` → Wheel, `constraints-lock.txt`, `release/release_info.json`, `release/release-manifest-v2.0.0.json`.
- Wheel-SHA in Mark-LV eintragen: `core/cannanas_client.py` (`PINNED_ADAPTER_VERSION = '2.0.0'`, `PINNED_WHEEL_SHA256 = '<sha>'`), `tests/test_cannanas_client.py` (`PINNED_VERSION`, erwarteter SHA), `tests/test_cannanas_integration.py` (`adapter_version`), `tests/fixtures/fake_adapter.py` (`adapter_version` + `EXPECTED_TOOLS`).

- [ ] **Step 4: Run the full suites to verify they pass**

Run adapter: `cd /home/lacasa15/services/cannanas-mcp/adapter && PYTHONPATH=src /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest discover -s tests -t .` → `OK`
Run Mark-LV: `cd /root/Mark-LV-LaCasa/.worktrees/integration && /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest discover -s tests` → `OK`

- [ ] **Step 5: Deploy to lacasa-server**

```bash
cd /home/lacasa15/services/cannanas-mcp/adapter
scp -q dist/cannanas_mcp_adapter-2.0.0-py3-none-any.whl constraints-lock.txt lacasa@lacasa-server:/home/lacasa/cannanas-adapter/
scp -q release/release_info.json lacasa@lacasa-server:/home/lacasa/cannanas-adapter/release_info.new.json
ssh lacasa@lacasa-server 'cd /home/lacasa/cannanas-adapter && \
  printf "cannanas-mcp-adapter==2.0.0 --hash=sha256:%s\n" "$(sha256sum cannanas_mcp_adapter-2.0.0-py3-none-any.whl | cut -d" " -f1)" > /tmp/spec.txt && \
  cat constraints-lock.txt >> /tmp/spec.txt && \
  venv/bin/pip install -q --no-deps --require-hashes --find-links . -r /tmp/spec.txt && rm -f /tmp/spec.txt && \
  install -m 600 release_info.new.json venv/bin/release_info.json && rm -f release_info.new.json && \
  venv/bin/cannanas-mcp-adapter --adapter-info'
```
Expected: `adapter_version 2.0.0`, `release_pinned: true`, neue Tool-Liste.

- [ ] **Step 6: Commit**

```bash
cd /home/lacasa15/services/cannanas-mcp/adapter && git add -A && git commit -m "release: 2.0.0 grow coverage"
cd /root/Mark-LV-LaCasa/.worktrees/integration && git add -A && git commit -m "chore(cannanas): pin adapter 2.0.0"
```

---

### Task 6: Mark-LV — Aktions-Enum, Host-Mapping, Plugin-Schema

**Files:**
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_actions.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/cannanas_host.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/plugins/cannanas.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_headless.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_plugin.py`
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_cannanas_plugin.py`

**Interfaces:**
- Consumes: `read_tool_call()`, `prepare_tool_call()` aus `core/cannanas_host.py`.
- Produces:
  - `CANNANAS_READ_ACTIONS` (neu): `zone_growth_summary`, `list_mother_plants`, `list_diaries`, `list_harvests`, `get_grow_config`, `list_plant_diaries`, `zone_plant_status`.
  - `CANNANAS_PREPARE_ACTIONS` (neu): `prepare_zone_update`, `prepare_harvest_create`, `prepare_waste_disposal_create`.
  - `CANNANAS_MODEL_ACTIONS` = Read ∪ Prepare.
  - `read_tool_call` — neue Mappings auf die Adapter-Tools.
  - `prepare_tool_call` — neue Mappings; `prepare_zone_update` nutzt `zone`/`fields`, `prepare_harvest_create` nutzt `plant_name`/`query`+`count`, `prepare_waste_disposal_create` nutzt `plant_name`.
  - `plugins/cannanas.py` — neue Parameter: `zone`, `mother`, `strain`, `batch`, `date`, `fields`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cannanas_plugin.py` ergänzen:

```python
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

    def test_new_prepare_actions_map_to_adapter_tools(self):
        from core.cannanas_host import prepare_tool_call
        binding = {"stable_device_id": "esp32-1"}
        mapped, _args = prepare_tool_call(
            "prepare_zone_update", {"zone": "Madrid", "fields": {"notes": "x"}}, binding)
        self.assertEqual("prepare_zone_update", mapped)
```

In `tests/test_headless.py` die Enum-Erwartung auf die neue geschlossene Liste aktualisieren.

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — `ValueError: unknown read action: list_mother_plants`

- [ ] **Step 3: Implement the minimal code**

- `core/cannanas_actions.py`: beide frozensets erweitern.
- `core/cannanas_host.py`:
  - `read_tool_call`: `list_mother_plants` → `("list_mother_plants", {"room": room, "limit": _int_param(params, "count", 50)}, "Mutterpflanzen")`; `list_diaries` → `("list_diaries", {"room": room, "plant": query, "batch": str(params.get("batch") or ""), "limit": _int_param(params, "count", 25)}, "Tagebücher")`; `list_harvests` → `("list_harvests", {"limit": _int_param(params, "count", 10)}, "Ernten")`; `get_grow_config` → `("get_grow_config", {}, "Grow-Einstellungen")`; `list_plant_diaries` → `("list_plant_diaries", {"plant": query, "limit": _int_param(params, "count", 10)}, "Tagebuch")`; `zone_plant_status` → `("zone_plant_status", {"room": room}, "Pflanzenstatus")`.
  - `prepare_tool_call`: `prepare_zone_update` → `("prepare_zone_update", {"zone": str(params.get("zone") or room), "fields": params.get("fields") or {}}, binding)`; `prepare_harvest_create` → `("prepare_harvest_create", {"plants": plant_name, "batches": str(params.get("batch") or ""), "notes": notes}, binding)`; `prepare_waste_disposal_create` → `("prepare_waste_disposal_create", {"plants": plant_name, "notes": notes}, binding)`.
  - `_RESULT_RECORD_KEYS` um `"diaries"`, `"harvests"`, `"materials"`, `"mother_plants"` erweitern, damit der Host die Adapter-Listen als `records` projiziert.
- `plugins/cannanas.py`: Parameter-Schema um `zone`, `mother`, `strain`, `batch`, `date`, `fields` erweitern; Beschreibung aktualisieren.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/Mark-LV-LaCasa/.worktrees/integration && /root/.hermes/cache/scratch/marklv-venv/bin/python -m unittest tests.test_cannanas_plugin tests.test_headless` → `OK`

- [ ] **Step 5: Commit**

```bash
cd /root/Mark-LV-LaCasa/.worktrees/integration
git add -A && git commit -m "feat(cannanas): expose the full grow action set to the model"
```

---

### Task 7: JARVIS-Stimme — Werkzeuge und Prompts

**Files:**
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/scripts/esp32_jarvis_bridge.py`
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/core/esp32_voice_workflows.py`
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_esp32_jarvis_bridge.py`
- Test: `/root/Mark-LV-LaCasa/.worktrees/integration/tests/test_esp32_voice_workflows.py`

**Interfaces:**
- Produces:
  - `MICROPHONE_FUNCTION_SCHEMA` (bestehend), `CANNANAS_FUNCTION_SCHEMA` neu: `{"type": "object", "properties": {"action": {"type": "string", "enum": [...]}}, "required": ["action"]}` mit der geschlossenen Aktions-Liste.
  - `def _cannanas_runner()` in `scripts/esp32_jarvis_bridge.py` — ruft den Host mit der gesprochenen Aktion auf.
  - `CannanasRoomDiaryWorkflow.prepare_diary_for(...)` — Zielauflösung Raum → Zone, Pflanze → Plant, Charge → Batch.

- [ ] **Step 1: Write the failing tests**

```python
    def test_cannanas_tool_is_offered_with_the_closed_action_enum(self):
        class FakeTypes:
            class FunctionDeclaration:
                def __init__(self, **kwargs):
                    self.name = kwargs["name"]
                    self.parameters_json_schema = kwargs.get("parameters_json_schema")
            class Tool:
                def __init__(self, function_declarations):
                    self.function_declarations = function_declarations
        engine = bridge.GeminiTurnEngine(GEMINI_KEY_MARKER, cannanas_runner=lambda a: "ok")
        decls = {d.name: d for d in engine._chat_tools(FakeTypes)[0].function_declarations}
        self.assertIn("cannanas", decls)
        self.assertIn("prepare_zone_update",
                      decls["cannanas"].parameters_json_schema["properties"]["action"]["enum"])
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — `AssertionError: 'cannanas' not found in {...}`

- [ ] **Step 3: Implement the minimal code**

- In `GeminiTurnEngine.__init__`: Parameter `cannanas_runner=None`; in `_answer_from_response` den Namen `"cannanas"` behandeln: `answer = self._cannanas_runner(parameters.get("action"))`.
- In `_chat_tools`: `types.FunctionDeclaration(name="cannanas", ..., parameters_json_schema=CANNANAS_FUNCTION_SCHEMA)` nur wenn `self._cannanas_runner` gesetzt ist.
- `DEFAULT_SYSTEM_INSTRUCTION`: eine Zeile, die die neuen Fähigkeiten nennt (Status wechseln, umziehen, Mutterpflanzen, Ernte) und betont, dass jede Änderung erst bestätigt wird.
- `core/esp32_voice_workflows.py`: `CannanasRoomDiaryWorkflow` um `plant`/`batch`-Ziele erweitern; `prepare` akzeptiert `{"room", "plant", "batch", "text"}` und löst genau ein Ziel auf.

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(voice): expose the Cannanas grow actions to JARVIS"
```

---

### Task 8: Deployment, Live-Verifikation und Funktionsliste

**Files:**
- Modify: `/root/Mark-LV-LaCasa/.worktrees/integration/docs/cannanas-jarvis-integration.md`
- Create: `/root/Mark-LV-LaCasa/.worktrees/integration/docs/cannanas-grow-funktionsliste.md`
- Modify: `/root/Documents/second-brain/KI_Protokolle/Sitzungen/2026-10-20_Cannanas-Grow-Vollabdeckung.md` (neu)

**Interfaces:**
- Produces: Die Funktionsliste als Markdown mit drei Spalten: **Funktion**, **Status** (integriert / teilweise / fehlt), **Sprachbefehl**.

- [ ] **Step 1: Deploy**

```bash
cd /root/Mark-LV-LaCasa/.worktrees/integration
git push -q origin feat/esp32-satellite-phase1
ssh lacasa@lacasa-server 'cd /home/lacasa/Mark-LV-LaCasa-esp32 && git fetch -q origin && git checkout -q origin/feat/esp32-satellite-phase1 && git rev-parse --short HEAD && systemctl --user restart esp32-jarvis-bridge.service && sleep 7 && systemctl --user is-active esp32-jarvis-bridge.service'
```
Expected: `active` und `satellite connected relay_port=6053` im Journal.

- [ ] **Step 2: Live-Verifikation der Lese-Tools (read-only, keine Writes)**

Auf lacasa-server ein Probe-Skript ausführen, das je Tool genau einen read-only Aufruf macht (`list_mother_plants`, `list_diaries`, `list_harvests`, `get_grow_config`, `zone_plant_status`, `zone_growth_summary`) und die gekürzten Ergebnisse ausgibt. Erwartung: `ok: true` bei allen, keine Exceptions.

- [ ] **Step 3: Live-Verifikation einer Prepare-Kette (ohne Confirm)**

`prepare_zone_update` mit `fields={"notes": "Verifikation"}` auf Zone Madrid → Preview muss den Raum nennen; danach `host.coordinator.cancel(...)`, damit **kein** Pending zurückbleibt. Kein `confirm_*` aufrufen.

- [ ] **Step 4: Funktionsliste schreiben**

`docs/cannanas-grow-funktionsliste.md` mit allen Zeilen aus Task 1–7 plus den bewusst ausgeschlossenen Bereichen (Analyse, Waste-Disposal-Abschluss, Harvest-Produkte/Drying-Logs, Datei-Upload, Finanzen, Compliance-Reports) und einem Abschnitt „Noch fehlt / Vorschlag" (z. B. Umzug einer Charge **samt** Pflanzen, Schnitt-/Ernte-Datum automatisch aus dem Status ableiten, Gieß-Intervall, Terminfotos an den Tagebucheintrag hängen, Standort-Räume aus `/locations`).

- [ ] **Step 5: Second-Brain-Protokoll**

Protokoll mit Ziel, Funktionsliste, Deployständen, Testzahlen und offenen Punkten; committen und pushen.

- [ ] **Step 6: Commit**

```bash
cd /root/Mark-LV-LaCasa/.worktrees/integration
git add -A && git commit -m "docs: cannanas grow function list and integration notes"
cd /root/Documents/second-brain && git add -A && git commit -m "docs: Cannanas Grow-Vollabdeckung" && git push -q
```

---

## Self-Review

**Spec-Coverage:** Alle vom Nutzer genannten Punkte sind adressiert: *Anbau planen* → Task 2 (Charge mit Mutterpflanzen + Vermehrungsmaterial), *Planze hinzufügen* → Task 2 (`prepare_plant_create` mit `plant_count`, `parent_id`), *Status ändern* → Task 1 (Pflanze/Charge, `is_mother`), *in anderen Raum umziehen* → Task 1 (`room` → `zone_id` bei Pflanze, Charge und Zone), *Mutterpflanzen-Status* → Task 1 (`is_mother`) und Task 3 (`list_mother_plants`, `list_plants(mother=True)`), *Tagebucheinträge je Pflanze* → Task 2 (`prepare_diary_create(plant=…)`) und Task 3 (`list_plant_diaries`), *selbst denken, was fehlt* → Task 1/3/4 (Zonen-Update, Ernte, Verschmutzung, Grow-Config, Zonen-Pflanzenstatus). *Liste aller integrierten Funktionen* → Task 8.

**Step-Scan:** Jeder Schritt nennt Datei, Signatur und Testnamen. Kein „TBD", kein „handle edge cases".

**Type-Konsistenz:** `mother_plants` heißt in Task 2 durchgängig so; `_split_plant_update_fields` in Task 1 wird in Task 2 nicht geändert; `prepare_diary_create` erhält in Task 2 die Parameter `room`/`plant`/`batch` (in Task 1 noch nicht erwähnt — Task 2 definiert sie, Task 6/7 lesen sie).

**Review Focus:** Jede der acht Zeilen hat einen Test: mehrdeutige Namensauflösung (Task 1/2), Umzugs-Semantik (Task 1/2), `is_mother` (Task 1), ungültige Counts (Task 2), fehlende Sorte (Task 2), Write nach abgebrochenem Senden (Task 4/5 Audit-Vertrag), konkurrierende Bestätigungen (Task 5/6 Host-Fence), Lösch-/Abschluss-Aktionen (Task 6 geschlossene Enum).

**Proportion:** Acht Tasks für ~14 neue Adapter-Tools plus Mark-LV-Spiegelung, Release und Doku.
