# Live-Test am ESP32-Satellit — Freitag, 23.10.2026

Der Flash ist durch, die Firmware ist bewiesen drauf, und die
Wakeword-Erkennung ist ohne Menschen nachgewiesen. Was bleibt, ist deine
Stimme fuer das Sprecher-Enrollment und der gemeinsame Test vor Ort.

## Der Stand, belegt am 22.10.2026

| Punkt | Befund |
|---|---|
| Flash | `verified bytes=1721712 sha256=c62c377a…` — der geschriebene Bereich wurde gegen den registrierten Hash geprueft, **vor** dem Reset |
| Firmware auf dem Board | `compilation_time 2026-10-21 17:27:55 +0200` (vorher 19.10.) |
| Wakeword | `Sensitivity 'Slightly sensitive': Hey Jarvis cutoff 153 (active)` — 153 = 0.6, also greift der Regler jetzt in **jeder** Position |
| Erkennung | `Detected 'Hey Jarvis' with sliding average probability is 0.69` — per Loopback ueber den eigenen Lautsprecher, ohne Menschen im Raum |
| Bridge | hat einen kompletten Turn verarbeitet: `turn finished chars=33 audio_bytes=113280` |
| Board im WLAN | 192.168.1.192, API 6053, Fingerabdruck `a0:f2:62:e3:48:58` |
| Dienste | `esp32-jarvis-bridge`, `jarvis`, `jarvis-learning-dispatch` — alle `active` |
| Adapter | 2.8.0, gepinnt, 40 Werkzeuge, Hash-Kette konsistent |
| Betreiber | Fortschritt, Projekt-Betreuer, Sicherheitsaudit, Firmware-Sync — schweigen bei gesundem Zustand |

**Die vier Fixes, die jetzt drin sind:**

1. `on_client_connected` startet die Wakeword-Engine neu, wenn sie tot
   ist — ein Bridge-Reconnect macht das Board wieder taub.
2. Der Schwellwert-Regler wirkt in **jeder** Empfindlichkeits-Position
   (vorher nur in „Custom", sonst hat der Modell-Default 0.97 gewonnen).
3. Der Guard setzt `--before usb-reset` — am nativen USB-JTAG kommt der
   ESP32-S3 sonst nicht zuverlaessig in den Download-Modus.
4. Der Flash-Waechter fragt den Produktionsserver, welche Firmware
   laeuft, und fasst das Board nicht an, wenn sie schon aktuell ist.

## Schritt 1: das Sprecher-Enrollment (5 Sekunden von dir)

Das Board streamt Mikrofon-Audio nur, wenn es selbst einen Turn startet —
also erst nach dem Flash, und nur mit deiner Stimme. Die Datei
`speaker-profiles.json` wurde nach einem Test mit der Synthese-Stimme
geloescht: ein Profil aus einer fremden Stimme wuerde dich **aussperren**.

Auf lacasa-server:

```sh
systemctl --user start jarvis-speaker-capture@lukas
```

Dann **vor dem Board "Hey Jarvis" sagen und etwa 4-5 Sekunden sprechen**.
Das Skript nimmt genau diese Sekunden auf und schreibt daraus das
Sprecherprofil (owner-only, `0600`).

Pruefen, dass es geklappt hat:

```sh
ls -l /home/lacasa/waveshare-va-test/speaker-profiles.json
```

Fehlt die Datei, sagt der Dienst dir, woran es lag:

```sh
journalctl --user -u jarvis-speaker-capture@lukas -n 20 --no-pager
```

## Schritt 2: der gemeinsame Test

```sh
cd /home/lacasa/Mark-LV-LaCasa-esp32
PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:/home/lacasa/Mark-LV-LaCasa-esp32/scripts \
  /home/lacasa/waveshare-va-test/venv/bin/python scripts/esp32_live_test.py
```

Das Skript stoppt die Bridge kurz (sie haelt den einzigen
Voice-Assistant-Slot des Boards, sonst sieht der Test keinen Turn),
prueft Firmware, Wakeword und Mikrofon und wartet 30 Sekunden auf dein
"Hey Jarvis". Danach startet es die Bridge wieder.

```
1. FIRMWARE
   Kompiliert: 2026-10-21 17:27:55 +0200
   -> OK, die aktuelle Firmware ist drauf.
2. WAKEWORD
   Schwelle (Schieber) : 0.6
   Empfindlichkeit     : 'Slightly sensitive'
3. MIKROFON
   Mic gain (ES7210)   : 24.0 dB
4. ERKENNUNG - jetzt "Hey Jarvis" sagen (30 Sekunden)
   -> ERKANNT (1 Turn). Der Satellit hoert zu.
```

### Wenn dort "NICHT ERKANNT" steht
Das Board laeuft, die Engine hoert aber nicht. Dann die Logs:

```sh
cd /home/lacasa/waveshare-va-test && ./venv/bin/python satellite_wakeword_monitor.py 60
```

Erwartet: `Sensitivity 'Slightly sensitive': Hey Jarvis cutoff 153` (153 = 0.6).
247 waere noch der alte Modell-Default.

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
„Guten morgen Head of Grow" spricht dein Briefing: deine Todos, die
Raeume mit Pflanzenzahlen und Phasen, die Temperaturen und der letzte
Tagebucheintrag. Die Sprecherdatei liegt owner-only unter
`/home/lacasa/Mark-LV-LaCasa/config/briefing-speakers.json` — deine
Todo-Liste ist derzeit **leer**. Wenn du morgen Todos willst, sag sie
mir, dann trage ich sie ein.

### Der Dauerdialog
Nach einer Antwort hoert JARVIS weiter zu, damit du ohne neues
"Hey Jarvis" nachfragen kannst. Beendet wird er nur durch einen Befehl
von dir: „Mic muten", „Mikrofon stumm", „Sei stumm" oder „Hoer auf
zuzuhoeren". Danach weckt „Hey Jarvis" den Dialog wieder.

## Ohne Menschen pruefen, ob das Board hoert

Die TTS spricht "Hey Jarvis", das Board spielt es ueber seinen eigenen
Lautsprecher ab, und sein Mikrofon hoert es:

```sh
cd /home/lacasa/Mark-LV-LaCasa-esp32
PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:/home/lacasa/Mark-LV-LaCasa-esp32/scripts \
  /home/lacasa/waveshare-va-test/venv/bin/python scripts/loopback_wakeword.py
```

Erwartet:

```
   LOG: ... Detected 'Hey Jarvis' with sliding average probability is 0.69
ERGEBNIS: ERKANNT
```

**Wichtig:** die Bridge muss dafuer gestoppt sein — nur ein Client darf
den Voice-Assistant-Slot halten.

## Wenn etwas klemmt

| Symptom | Ursache und Loesung |
|---|---|
| „NICHT ERKANNT" | `cd /home/lacasa/waveshare-va-test && ./venv/bin/python satellite_wakeword_monitor.py 60` — erwartet `cutoff 153`; 247 waere noch der alte Modell-Default |
| Board kommt nicht ins WLAN | Der Mac muss im selben Netz sein; `ping 192.168.1.192` vom Server pruefen. Nach dem Flash holt sich das Board sein WLAN von selbst, das dauert etwa 15 Sekunden |
| Board antwortet gar nicht | Es koennte im Bootloader stehen (wenn ein Flash abgebrochen wurde): `esptool --chip esp32s3 --port /dev/cu.usbmodem1101 --before usb-reset --after hard-reset flash-id` setzt es zurueck |
| Kein Ton | Der Board-Lautsprecher braucht `5V` am JST-Anschluss |
| Briefing sagt nichts | Rechte: `chmod 600 /home/lacasa/Mark-LV-LaCasa/config/briefing-speakers.json` |
| Der Flash-Waechter stoert | Er fasst das Board nicht an, wenn die Firmware schon aktuell ist. Log: `~/waveshare-va-test/logs/flash-when-plugged.log` |

## Der Flash ist rueckstandsfrei rueckbaubar

Vor jedem Schreibvorgang macht der Guard ein vollstaendiges 16-MB-Backup
und prueft den geschriebenen Bereich gegen den registrierten Hash, bevor
der Chip zurueckgesetzt wird. Das Backup vom 22.10. hat den SHA
`3822b599…` (vorheriges Backup: `ed881f10…`). Ein Restore laeuft ueber
denselben Guard und braucht eine eigene, einmalige Bestaetigung.
