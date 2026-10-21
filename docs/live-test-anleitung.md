# Live-Test am ESP32-Satellit — Donnerstag, 21.10.2026

Alles, was fuer den Test in der LaCasa vorbereitet ist. Der einzige
Handgriff, der bleibt, ist das Einstecken des Boards in den Mac.

## Der eine Handgriff: Board einstecken

Auf dem Mac laeuft bereits ein Waechter, der auf das Board per USB wartet:

```sh
# laeuft seit dem 21.10. und wartet
launchctl list | grep satellite-flash
```

**Du musst nur das ESP32-Board per USB-Kabel an den Mac anstecken.**
Der Waechter erkennt den Port, prueft das Artefakt, macht das Backup,
holt die Bestaetigung, flasht und prueft den geschriebenen Bereich —
bevor der Chip zurueckgesetzt wird. Danach laeuft der Wacht durch
alles von selbst.

Falls der Waechter nicht laeuft:

```sh
sh ~/waveshare-va-test/flash-when-plugged.sh
```

Beobachte das Log:

```sh
tail -f ~/waveshare-va-test/logs/flash-when-plugged.log
```

Erwartet am Ende:

```
FLASH ERFOLGREICH. Das Board startet neu und holt sich sein WLAN.
```

## Danach: der Abnahmelauf

Auf lacasa-server:

```sh
cd /home/lacasa/Mark-LV-LaCasa-esp32
PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:/home/lacasa/Mark-LV-LaCasa-esp32/scripts \
  /home/lacasa/waveshare-va-test/venv/bin/python scripts/esp32_live_test.py
```

Das Skript stoppt die Bridge kurz (sie haelt den einzigen Voice-Assistant-Slot
des Boards, sonst sieht der Test keinen Turn), prueft Firmware, Wakeword,
Mikrofon und wartet 30 Sekunden auf dein "Hey Jarvis". Danach startet es die
Bridge wieder.

```
1. FIRMWARE
   Kompiliert: 2026-10-21 ...
   -> OK, die aktuelle Firmware ist drauf.
2. WAKEWORD
   Schwelle (Schieber) : 0.6
   Empfindlichkeit     : 'Slightly sensitive'
3. MIKROFON
   Mic gain (ES7210)   : 24.0 dB
4. ERKENNUNG - jetzt "Hey Jarvis" sagen (30 Sekunden)
   -> ERKANNT (1 Turn). Der Satellit hoert zu.
```

### Wenn dort "ALTE FIRMWARE" steht
Der Flash hat nicht stattgefunden. Auf dem Mac das Log pruefen:
`~/waveshare-va-test/logs/flash-when-plugged.log`.

### Wenn dort "NICHT ERKANNT" steht
Das Board laeuft, die Engine hoert aber nicht. Dann die Logs:

```sh
cd /home/lacasa/waveshare-va-test && ./venv/bin/python satellite_wakeword_monitor.py 60
```

Erwartet: `Sensitivity 'Slightly sensitive': Hey Jarvis cutoff 153` (153 = 0.6).
247 waere noch der alte Modell-Default.

## Danach: das Sprecher-Enrollment

Erst nach dem Flash - das Board streamt Mikrofon-Audio nur, wenn es selbst
einen Turn startet, und das geht nur mit laufender Engine.

```sh
systemctl --user start jarvis-speaker-capture@lukas
```

Dann **vor dem Board "Hey Jarvis" sagen und etwa 4 Sekunden sprechen**.
Das Skript nimmt genau diese Sekunden auf und schreibt daraus das
Sprecherprofil (owner-only).

## Was du danach testen kannst

| Spruch | Erwartung |
|---|---|
| „Hey Jarvis" | Der Satellit antwortet |
| „Wie warm ist es in Madrid?" | Die Growroom-Temperatur (aktuell 26,9 °C) |
| „Welche Muetter haben Stecklinge?" | Die Stecklingsbilanz mit echten Zahlen |
| „Wie lange wurde Madrid nicht gegossen?" | Das Giess-Intervall |
| „Status von Madrid" | Pflanzen, Chargen, Phase |
| „Mic muten" | Die Bridge verstummt, der Wakeword bleibt |
| „Hey Jarvis" nach dem Muten | Der Dialog kommt zurueck |
| „Spiele Queen auf Spotify" | Spotify laeuft leiser waehrend der Ansage, danach wieder so laut wie vorher |
| „Zeig mir die Temperaturen" | Das Morgen-Briefing mit deinen Todos |

### Das Morgen-Briefing
„Guten Morgen" spricht dein Briefing: deine Todos, die Raeume mit
Pflanzenzahlen und Phasen, die Temperaturen und der letzte Tagebucheintrag.
Die Sprecherdatei liegt owner-only unter
`/home/lacasa/Mark-LV-LaCasa/config/briefing-speakers.json` — deine Todo-Liste
ist derzeit **leer**. Wenn du morgen Todos willst, sag sie mir, dann trage
ich sie ein.

## Was vorbereitet ist

- **Firmware**: `c62c377a…` (1721712 Bytes), gebaut auf dem Mac aus dem
  geprueften Repository. Beide Fixes sind im Binary nachgewiesen:
  „Wake word engine restarted after the client connected" und
  „Sensitivity '%s': %s cutoff %u%s".
- **Guard**: Das Artefakt ist im Guard registriert, das 16-MB-Backup
  (`ed881f10…`) ist vorhanden, der Geraete-Fingerabdruck passt
  (`a0:f2:62:e3:48:58`).
- **Secrets im Binary**: SSID, Passwort und API-Schluessel sind einkompiliert
  — das Board kommt nach dem Flash von selbst ins WLAN.
- **Bridge, Timer, PC-JARVIS**: laufen.
- **Adapter 2.8.0**: gepinnt, 40 Werkzeuge, Hash-Kette konsistent.
- **Vier Betreuer**: melden sich bei Abweichungen per Telegram.
- **Spotify**: `raspotify (lacasa-server)` laeuft, das OAuth-Store ist
  owner-only, der Token wurde am 21.10. erfolgreich erneuert (gueltig bis
  17.04.2027). Der Zugriffstoken wird bei Bedarf automatisch erneuert.
- **Briefing**: Die Sprecherdatei ist owner-only (600) — ohne diese Rechte
  verweigert die Bridge das Briefing mit einem PermissionError. Der
  Betreuer meldet das, falls es wieder passiert.
- **Sprecher**: „Lukas / Head of Grow" ist eingetragen, die Todo-Liste ist
  **leer**. Wenn du morgen Todos willst, sag sie mir.

## Wenn etwas klemmt

| Symptom | Ursache und Loesung |
|---|---|
| Der Flash-Waechter laeuft nicht | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.lacasa.satellite-flash.plist` |
| Board kommt nach dem Flash nicht ins WLAN | Der Mac muss im selben Netz sein; `ping 192.168.1.192` vom Server pruefen |
| „NICHT ERKANNT" | `satellite_wakeword_monitor.py 60` zeigt, ob die Engine laeuft |
| Kein Ton | Der Board-Lautsprecher braucht `5V` am JST-Anschluss |
| Briefing sagt nichts | Rechte: `chmod 600 /home/lacasa/Mark-LV-LaCasa/config/briefing-speakers.json` |
