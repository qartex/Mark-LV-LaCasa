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

**Die sechs Fixes, die jetzt drin sind:**

1. **Der Mikrofon-Fix** (der wichtigste): `voice_assistant.stop` bei
   jedem Client-Wechsel stoppte die Mikrofonquelle. Die Quelle ist nicht
   passiv, ihr Datengate schloss sich — und die **laufende** Engine
   bekam nie wieder Ton. Sie meldete ehrlich „cutoff 153 (active)" und
   war trotzdem taub. Der Reconnect-Pfad prüft jetzt
   `microphone.is_capturing` und startet die Engine neu, wenn die Quelle
   zu ist.
2. **Der Ring** zeigt jetzt die Wakeword-Erkennung. Vorher rief nur
   `control_leds` bei Boot und Client-Wechsel auf, nie bei einer
   Phasenumstellung — der Ring blieb dunkel, während das Board zuhörte.
3. Der Schwellwert-Regler wirkt in **jeder** Empfindlichkeits-Position
   (vorher nur in „Custom", sonst hat der Modell-Default 0.97 gewonnen).
4. Der Guard setzt `--before usb-reset` — am nativen USB-JTAG kommt der
   ESP32-S3 sonst nicht zuverlässig in den Download-Modus.
5. Das Backup liest den Flash in **4-MB-Blöcken**: ein 16-MB-Read
   bricht am nativen USB-Link bei 34 % ab („No more data to read from
   the serial port"), eine niedrigere Baudrate hilft nicht.
6. Die Bridge fragt ein Sprachmodell, das den Stil abgelehnt hat, beim
   nächsten Mal **ohne** Stil — vorher kostete jeder Turn zwei
   TTS-Anfragen statt einer. Das war die gemeldete Latenz.

## Schritt 1: das Sprecher-Enrollment — erledigt

Das Board streamt Mikrofon-Audio nur, wenn es selbst einen Turn startet —
also erst nach dem Flash, und nur mit deiner Stimme. Die Datei
`speaker-profiles.json` wurde nach einem Test mit der Synthese-Stimme
gelöscht: ein Profil aus einer fremden Stimme würde dich **aussperren**.

**Am 22.10. mit deiner Stimme erledigt:** `speaker-profiles.json`
(owner-only, `0600`) enthält den Sprecher `lukas` mit dem Modell
`res2net.onnx`, Schwelle 0.62. Die Bridge meldet
`speaker identification status=enabled speakers=1`.

Falls du das Enrollment wiederholen willst (etwa mit besserer Aufnahme):

```sh
systemctl --user start jarvis-speaker-capture@lukas
```

Dann **vor dem Board "Hey Jarvis" sagen und etwa 4-5 Sekunden sprechen**.
Das Skript nimmt genau diese Sekunden auf und schreibt daraus das
Sprecherprofil (owner-only).

Prüfen, dass es geklappt hat:

```sh
ls -l /home/lacasa/waveshare-va-test/speaker-profiles.json
```

Fehlt die Datei, sagt der Dienst dir, woran es lag:

```sh
journalctl --user -u jarvis-speaker-capture@lukas -n 20 --no-pager
```

**Wichtig:** der Capture-Dienst stoppt die Bridge vorher selbst (er
braucht den Voice-Assistant-Slot) und startet sie danach wieder. Ohne
diesen Stopp sieht der Capture keinen Turn und endet mit „no speech
arrived", obwohl gesprochen wurde.

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

## Wichtig: HA zeigt den Satelliten als "unavailable" — das ist so gewollt

In Home Assistant erscheinen die Entitaeten des Boards
(`select.growraum_sprache_1_assistent`, `switch.growraum_sprache_1_microphone_mute`,
`media_player.growraum_sprache_1` und die uebrigen) als **unavailable**, und
im HA-Fehlerlog steht eine Zeile:

```
[aioesphomeapi.connection] 192.168.1.192: Connection error occurred:
192.168.1.192: Connection requires encryption
```

**Das ist erwartet und darf waehrend des Tests nicht "repariert" werden.**
Die ESPHome-Anbindung in HA hat einen anderen API-Key gespeichert, und
wuerde sich nach dem Flash auch nicht von allein erwaehnen. Zwei Gruende
sprechen dafuer, den Satelliten waehrend der Abnahme **nicht** in HA zu
holen:

1. Der Board-ESP32 darf nur **einen** Client auf dem Voice-Assistant-Slot
   haben. Die produktive Bridge haelt ihn. Wuerde sich HA zusaetzlich
   anmelden, kaeme es zum Konkurrenzkampf um genau diesen Slot — und der
   Sprachpfad, den du testen willst, waere die erste, die leidet.
2. Die Projektvorgabe lautet: **keine produktive HA-Integration fuer
   Tests.**

Die Stimme laeuft unabhaengig davon ueber die Bridge, und die Bridge
arbeitet direkt mit dem Board — ohne HA dazwischen. Temperaturen und
Briefing kommen aus HA, aber aus **anderen** Geraeten (den sechs
`sensor.zonen_*_temperatur_celsius`), nicht vom Board.

Wer den Satelliten spaeter doch in HA haben will, braucht den
Board-Key (identisch mit dem Build-Eingang `api_encryption_key` auf dem
Mac) **und** muss den Voice-Assistant-Slot eindeutig zuordnen. Vor dem
Test bitte nicht anfassen.

## Das Gemini-Kontingent: die harte Grenze fuer den Live-Test

Das Gemini-Free-Tier erlaubt **10 Anfragen pro Tag und Modell**. Jeder
Turn des Satelliten kostet **eine** davon — die Sprachausgabe. Zehn
Antworten, und die Stimme ist fuer den Tag still. Das ist keine
Eigenheit des Projekts, das ist das Kontingent des Anbieters.

Der Stand vom 22.10., gemessen (nur die Werte, nie die Keys selbst):

| Key | Sprachausgabe | Chat |
|---|---|---|
| `gemini-key.txt` | Kontingent leer (429) | OK |
| `gemini-key-2.txt` | OK | OK |
| `gemini-key-3.txt` | 401 — widerrufen | 401 |
| `gemini-key-4.txt` | 401 — widerrufen | 401 |

Die Bridge rotiert ueber alle vier und faengt sowohl 429 als auch 401 ab,
die Stimme faellt also nicht beim ersten toten Key aus. **Aber:** von den
vier Keys antwortet nur einer mit Kontingent, und die beiden 401er sind
vermutlich genau die Keys, die wegen der Veroeffentlichung im Chat
widerrufen wurden.

**Was das fuer den Test bedeutet:**

1. Der Live-Test sollte **unter zehn Fragen** bleiben, sonst geht die
   Stimme mitten im Test aus. Der Fortschrittsbot meldet
   `TTS_KONTINGENT_KNAPP`, wenn die Zahl kritisch wird.
2. Die Keys im Chat veroeffentlicht sind kompromittiert und **muss der
   Gaertner selbst ersetzen** — mit Keys, die Kontingent haben. Das ist
   der eine Schritt, den ich nicht tun kann, und ohne ihn bleibt die
   Reserve bei einem Key.
3. Jeder **manuelle** TTS-Aufruf (der Loopback-Test, das
   Sprecher-Enrollment) verbraucht dieselbe Tagesquote wie eine echte
   Frage. Wenn du morgen viele Tests machst, plane die Reihenfolge so,
   dass die **echten Fragen** zuerst kommen.

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
