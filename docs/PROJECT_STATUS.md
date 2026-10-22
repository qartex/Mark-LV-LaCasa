# Mark-LV LaCasa — Projektstatus

**Stand:** 22.10.2026
**Repo:** https://github.com/qartex/Mark-LV-LaCasa (Branch `feat/esp32-satellite-phase1`)
**Basis:** Mark-LV von FatihMakes (MIT, persönlich/nicht-kommerziell)

## Wo die Teile laufen

| Komponente | Host | Zustand |
|---|---|---|
| JARVIS (PC) | lacasa-server | `jarvis.service` — active |
| ESP32-Satellit-Bridge | lacasa-server | `esp32-jarvis-bridge.service` — active |
| Lernpipeline-Dispatcher | lacasa-server | `jarvis-learning-dispatch.timer` — active |
| Spotify-Receiver | lacasa-server | `raspotify` — active |
| Home Assistant | lacasa17 | API antwortet mit 200, sechs Zonensensoren |
| Cannanas-MCP-Adapter | lacasa-server | 2.8.0, gepinnt, 40 Werkzeuge |
| Hermes-Adapter | lacasa15 | nur Hermes, kein JARVIS |
| ESP32-Satellit | Growroom Madrid | 192.168.1.192, API 6053, verschlüsselt |
| Build-/Flash-Wirt | Mac (qartex) | `waveshare-va-test`, Board per USB |

## Bewiesen

- **Der Satellit hört.** `Detected 'Hey Jarvis' with sliding average
  probability is 0.69` — per Loopback über den eigenen Lautsprecher, ohne
  Menschen im Raum. Nachgewiesen am 22.10.2026.
- **Die Firmware passt.** `compilation_time 2026-10-21 17:27:55 +0200`,
  geschrieben und verifiziert: `verified bytes=1721712
  sha256=c62c377a…`, der Reset erfolgte erst nach der Prüfung.
- **Der Schwellwert greift.** `cutoff 153 (active)` — 153 = 0.6, also
  wirkt der Regler jetzt in jeder Empfindlichkeits-Position.
- **Ein kompletter Turn läuft durch.** Wakeword → Mikrofon → Gemini →
  TTS (`Charon`, 0.65) → Audio zum Board: `turn finished chars=33
  audio_bytes=113280`.
- **Der Dauerdialog steht.** Nach der Antwort streamt das Board weiter;
  beenden nur host-eigene Befehle wie „Mic muten".
- **Die Schreibkette ist geschlossen.** Modellseitig nur Prepare; die
  Confirms (`confirm_batch_move`, `confirm_batch_plant_names`,
  `confirm_batch_update`, `confirm_plant_update`) bleiben host-side. Der
  Boot-Fence nutzt ausschließlich die vom Adapter gemeldete Boot-ID.
- **Die Live-Tests waren read-only.** Kein produktiver Cannanas-Write
  wurde ausgelöst.

## Offen — und wem es gehört

| Punkt | Was fehlt | Wer |
|---|---|---|
| Sprecher-Enrollment | `speaker-profiles.json` fehlt; ein Test mit der Synthese-Stimme wurde bewusst gelöscht, weil es dich aussperren würde | **Gärtner** — 5 Sekunden sprechen |
| Physische Abnahme | Dauerdialog, unbegrenzte Folgefragen, „Mic muten", Reaktivierung, neue Sprachbefehle | **Gärtner** + Beistand |
| Produktiver Cannanas-Write | Halbumzug- und Originalnamensemantik sind implementiert, aber nie gegen Produktionsdaten geprüft | Testclub |
| Mutterbeziehungen | 34 Mütter und 164 Beziehungen existieren nur in **archivierten** Chargen; die aktuellen (Ztilton #1-#6, Psycho #1-#4) tragen keine | Datenlage |
| Gießintervalle | Die Funktion ist implementiert, echte Gießdaten fehlen im Tagebuch | Datenlage |
| Kompromittierte Gemini-Schlüssel | Die im Chat veröffentlichten Schlüssel sind widerrufen und außerhalb des Chats zu ersetzen. Von vier Keys antworten nur zwei, und nur **einer** hat Sprachkontingent — das Free-Tier erlaubt 10 Anfragen pro Tag und Modell, also **10 Sprachantworten pro Tag** | **Gärtner** |
| Telegram | Die Vorgabe lautet „Telegram komplett raus". Die Betreiber-Cronjobs nutzen Telegram derzeit als Alarmkanal; der **Produktpfad** ist telegramfrei | Entscheidung |

## Grenzen, die man kennen muss

- **Ein Client darf den Voice-Assistant-Slot halten.** Die Bridge hält
  ihn dauerhaft; jeder Test, der selbst zugreift, muss sie vorher
  stoppen — sonst sieht er keinen Turn, obwohl das Board perfekt hört.
- **Ein abgebrochener Flash lässt den Chip im Bootloader.** Dann
  antwortet das Board nicht mehr im WLAN. `esptool --before usb-reset
  --after hard-reset flash-id` setzt es zurück.
- **`erase-flash` leert die NVS.** Nach jedem Flash steht der
  Schwellwert wieder auf 0.97 und muss neu gesetzt werden.
- **Ein Flash-Wächter darf nie ungefragt flashen.** Er fragt den
  Produktionsserver, welche Firmware läuft, und fasst das Board nicht an,
  wenn sie aktuell ist.
- **Die Board-Boot-Logs sind über die API nicht sichtbar.** Die Zeilen
  entstehen vor dem WLAN. „0 wake-word-related log lines" ist ein
  Fehlbefund, kein Beweis.
- **Port 8088** ist bewusst LAN-gebunden; alle Datenrouten verlangen ein
  gültiges Token, und im Tailnet ist nur `/app` publiziert.
- **HA zeigt die Board-Entitäten als `unavailable`** — gewollt. Die
  ESPHome-Anbindung in HA hat einen anderen API-Key; sie darf während der
  Abnahme **nicht** repariert werden, weil sie sonst um den
  Voice-Assistant-Slot konkurriert, den die Bridge hält. Der
  Board-Key ist identisch mit dem Build-Eingang `api_encryption_key` auf
  dem Mac (Fingerabdruck `e91f47f7`), er ändert sich also nicht durch
  ein Flash.

## Wie man den Zustand prüft

```sh
# Fortschritt (meldet nur, was offen ist)
python3 /root/.hermes/scripts/jarvis-progress.py

# Projekt-Betreuer (Dienste, Adapter-Pin, beide Checkouts, Board, Sensoren)
bash /root/.hermes/scripts/jarvis-project-keeper.sh

# Sicherheitsaudit (die sechs Projektregeln)
bash /root/.hermes/scripts/jarvis-security-audit.sh

# Live am Board
cd /home/lacasa/Mark-LV-LaCasa-esp32
PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:/home/lacasa/Mark-LV-LaCasa-esp32/scripts \
  /home/lacasa/waveshare-va-test/venv/bin/python scripts/esp32_live_test.py
```

Alle vier Betreiber sind still, wenn gesund, und melden nur Abweichungen.

## Weiterführende Dokumente

- `docs/live-test-anleitung.md` — der Test in der LaCasa, Schritt für Schritt
- `docs/cannanas-grow-funktionsliste.md` — was integriert, was teilweise, was offen
- `docs/jarvis-learning-pipeline.md` — die Lernpipeline
- `deploy/esp32-satellite/README.md` — das Flash-Runbook
- `docs/superpowers/plans/` — die Umsetzungspläne

## Skills, die dieses Projekt betreffen

- `mark-lv` — Headless-Betrieb, Flash-Fallstricke, Live-Abnahme
- `home-assistant-operational-excellence` — HA-Verwaltung
- `cannanas-mcp-development` — der Adapter
- `subagent-driven-development` / `writing-plans` — Plan-Ausführung mit Reviews
