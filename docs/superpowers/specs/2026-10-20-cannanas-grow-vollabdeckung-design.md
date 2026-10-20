# Cannanas Grow-Vollabdeckung — Design

Datum: 2026-10-20
Entscheidung: Gärtner-Briefing, Adapter 2.0.0, Grow-Vollabdeckung mit Prepare→Confirm

## Ziel

Der Cannanas-MCP-Adapter deckt alle Operationen ab, die im laufenden Anbau über
Stimme gesteuert werden sollen: Anbau planen, Pflanzen und Chargen anlegen,
Status wechseln, Pflanzen und Chargen in andere Räume umziehen, Mutterpflanzen
kennzeichnen und finden, Tagebucheinträge je Pflanze/Charge/Raum, Ernte
vorbereiten, Verschmutzung dokumentieren. Dazu kommt eine vollständige
Funktionsliste, aus der sichtbar wird, was bereits geht und was noch fehlt.

## Warum jetzt

Bisher kann der Adapter Pflanzen, Sorten, Chargen und Tagebücher anlegen und
Status/Raum/Daten einer Pflanze oder Charge ändern. Alles, was im Alltag dazukommt,
fehlt: Zonen selbst pflegen, Mutterpflanzen, Mehrfach-Anlage aus einem
Mutterbestand, Tagebücher gezielt an eine Pflanze hängen, Ernte, Verschmutzung
und die Lese-Sicht auf Mutterschaft, Erntehistorie und Zonen-Pflanzenstatus.

## Nicht-Ziele (bewusst ausgeschlossen)

- Analyse-/Laborergebnisse, Waste-Disposal-Abschluss, Harvest-Produkte und
  Drying-Logs: Compliance-relevant, nicht per Sprache.
- Datei-Upload/-Download (Fotos, PDFs): Binärpfad, nicht Sprachpfad.
- Finanzen, Mitglieder, Chat, Cart, Rollen, POS, Transport, Reports.
- Löschen/Archivieren: destruktiv, bleibt außerhalb der Sprachsteuerung.
- `finalize_plants_and_batches`: schließt Pflanzen/Chargen endgültig ab —
  bleibt eine bewusste Entscheidung des Gärtners, nicht per Sprache.

## Aufbau

### Adapter (`/home/lacasa15/services/cannanas-mcp/adapter`)

Der Adapter bekommt eine zweite Werkzeuggeneration. Alles bleibt am bestehenden
Gerüst:

- `ALLOWED_REQUESTS` wird ausschließlich um exakte (Methode, Pfad)-Paare aus dem
  gepinnten OpenAPI-Fixture erweitert. Kein dynamischer Pfadbau.
- Lese-Tools: `list_mother_plants`, `list_diaries`, `list_harvests`,
  `get_grow_config`, `list_plant_diaries`, `zone_plant_status`.
- Prepare/Confirm-Paare: `prepare_zone_update`/`confirm_zone_update`,
  `prepare_harvest_create`/`confirm_harvest_create`,
  `prepare_waste_disposal_create`/`confirm_waste_disposal_create`.
- Bestehende Tools werden erweitert, nicht ersetzt: `prepare_plant_update`
  (is_mother, parent_plant, strain, yields, substrate/fertilizer, archived),
  `prepare_plant_create` (plant_count, mother_plant, is_mother),
  `prepare_batch_create` (mother_plants mit cutting_count),
  `prepare_diary_create` (genau ein Ziel: room | plant | batch),
  `list_plants`/`find_plants` (Filter mother/strain/batch).

### Vertrauensgrenzen (unverändert)

- Modell sieht nur Lese-Aktionen und `prepare_*`. `confirm_*` bleibt
  hostseitig, mit Adapter-Boot-Fence, atomarem Consume und Audit-vor-POST.
- Namen werden aufgelöst, nicht geraten: Raum → Zone, Sorte → Strain,
  Pflanze → Plant, Charge → Batch, Mutterpflanze → Plant.
- Statusvokabular geschlossen (SEED … DESTROYED), Fehler vor jedem
  Netzwerkzugriff.
- Alles Schwreibende bleibt auditpflichtig; der Audit-Sink ist owner-only.

### Mark-LV (`/root/Mark-LV-LaCasa/.worktrees/integration`)

- `core/cannanas_actions.py`: geschlossene Enum erweitern (Reads + Prepares).
- `core/cannanas_host.py`: Read-/Prepare-Mapping auf die neuen Adapter-Tools;
  `_RESULT_RECORD_KEYS` um `diaries`/`harvests` erweitern, damit der Host
  weiterhin auf `records` projiziert.
- `plugins/cannanas.py`: Parameter-Schema um zone/mother/strain/batch/date/
  fields erweitern.
- `scripts/esp32_jarvis_bridge.py`: enges `cannanas`-Werkzeug mit
  geschlossener Aktions-Liste, nur wenn ein Runner konfiguriert ist.

## Datenfluss einer Sprachaktion

1. Sprecher: „Ztilton #3 ist jetzt in Blüte.“
2. Modell ruft `cannanas` mit `action=prepare_plant_update`.
3. Host löst „Ztilton #3“ kanonisch auf, ruft den Adapter auf.
4. Adapter liefert Preview (`{plant, status}`), schreibt **nichts**.
5. Host legt das Pending an, spricht die Vorschau.
6. Sprecher: „Ja.“ → Host-Grammatik bestätigt → Adapter sendet den Write,
   Audit vorher, Rückgabe-ID wird verifiziert.

## Versionierung

Adapter 2.0.0 (Semver-Major: die Werkzeugmenge wächst). Wheel-SHA wird in
Mark-LV gepinnt; die Fake-Adapter-Fixture meldet dieselbe Version.

## Risiken

- **Mehrdeutige Namen:** mehrere Pflanzen mit gleichem Präfix → Rückfrage,
  kein Write. Deshalb `_match_one` unverändert streng.
- **Umzugs-Semantik:** eine Charge umziehen verschiebt nicht automatisch ihre
  Pflanzen. Das wird in der Vorschau benannt (beide Objekte), aber nicht
  automatisch mitgezogen — sonst entstehen stillschweigend getrennte Bestände.
- **Falscher Adressat bei gleichnamigen Zonen:** die Zonenliste enthält „
  Madrid“ mit führendem Leerzeichen; die Auflösung normalisiert Whitespace.
- **Scan-Kosten:** Aggregate scannen bis 5000 Datensätze; der Tagesbericht
  braucht das, ein normaler Abruf nicht (dort bleibt die Caller-Obergrenze 25).
