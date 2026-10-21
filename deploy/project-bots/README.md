# Der Fortschrittsbot

Der Bot beantwortet eine Frage: **Was steht am JARVIS-Projekt noch an, und
was muss der Gärtner tun?**

Er liest nur. Er startet nichts neu, schreibt nichts in fremde Systeme und
gibt keine Geheimnisse aus.

## Was er prüft

| Prüfung | Befund, wenn etwas nicht stimmt |
|---|---|
| Firmware des Satelliten-Board | `FLASH_OFFEN` — das Board läuft vor dem Reconnect-Fix, „Hey Jarvis" wird nicht erkannt |
| Sprecher-Enrollment | `SPEAKER_ENROLL_OFFEN` — kein Profil, JARVIS kennt nicht, wer spricht |
| Adapter-Pin | `ADAPTER_VERALTET` / `ADAPTER_ARTEFAKT_FALSCH` / `ADAPTER_OHNE_ARTEFAKT` / `ADAPTER_UNGEPINNT` |
| Checkouts | `CHECKOUT_VERSCHIEBEN` — Satellit und PC-JARVIS stehen auf verschiedenen Commits |

Zwei Fehlerarten, die eine reine Versionsprüfung nicht findet:

- **`ADAPTER_ARTEFAKT_FALSCH`** — der Adapter meldet die richtige Version,
  aber sein Artefakt-Hash weicht vom Pin ab. Ursache ist eine veraltete
  Sidecar (`release_info.json` neben dem Entry-Point), die beim Install
  nicht mitkopiert wurde.
- **`BOARD_UNREACHBAR`** — die Prüfung konnte nicht durchgeführt werden.
  Das ist fail-closed: eine nicht durchführbare Prüfung ist ein Befund,
  nie ein gesunder Zustand.

## Aufbau

```
jarvis-progress.py          der Wächter (dieser Host)
board-firmware-stamp.py     liest den Firmware-Stempel (läuft auf lacasa-server)
```

Der Stempel wird auf dem Server gelesen, weil dort der ESPHome-Key liegt.
Das Skript braucht den Bridge-Checkout im PYTHONPATH:

```sh
cd /home/lacasa/Mark-LV-LaCasa-esp32
PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:/home/lacasa/Mark-LV-LaCasa-esp32/scripts \
  /home/lacasa/waveshare-va-test/venv/bin/python \
  /home/lacasa/waveshare-va-test/board-firmware-stamp.py
```

## Ausgabe

- **stdout**: nur die Befunde, eine Zeile je offenem Punkt. Leer, wenn
  alles in Ordnung ist.
- **stderr**: der Status (`ADAPTER_OK`, `CHECKOUT_OK`, …).

Die Trennung ist wichtig: der Cronjob läuft im `no_agent`-Modus und
stellt **stdout** zu. Der Status auf stderr erreicht dich nicht, der
Handlungsbedarf schon. Ohne diese Trennung käme jeden Morgen eine
Sammelmeldung, auch wenn alles in Ordnung ist.

## Installation

```sh
install -m 0755 jarvis-progress.py /root/.hermes/scripts/jarvis-progress.py
install -m 0755 board-firmware-stamp.py /root/.hermes/scripts/board-firmware-stamp.py
scp board-firmware-stamp.py lacasa@lacasa-server:/home/lacasa/waveshare-va-test/
```

## Cronjob

Täglich um 7 Uhr, `deliver=all`, `no_agent=true`, Script
`jarvis-progress.py`. Der Job liefert die stdout-Ausgabe wörtlich; leere
Ausgabe sendet gar nichts.

## Grenzen

Der Bot **flash't nicht**. Der ESP32 ist nur per USB am Mac erreichbar,
und der Mac war tagelang offline. Der Bot sagt dir, dass der Flash offen
ist, und gibt dir den Befehl — er führt ihn nicht aus.
