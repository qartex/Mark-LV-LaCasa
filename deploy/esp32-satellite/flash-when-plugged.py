#!/usr/bin/env python3
"""Flash the satellite as soon as the board is on USB - but only if needed.

Der ESP32 laeuft eine Firmware, in der die Wakeword-Engine nach dem Boot
nicht aufkommt. Der Flash geht nur per USB, und USB heisst: jemand steckt
das Board an. Dieser Waechter macht daraus einen einzigen Schritt - er
wartet, bis das Board als USB-Geraet auftaucht, und flasht.

SICHER UND FLAU:
  - Vor dem Flash fragt er den Produktionsserver, welche Firmware auf dem
    Board laeuft. Laeuft schon die richtige, fasst er das Board nicht an.
    Das ist wichtig: der Waechter laeuft staendig, und ein ungefragter
    Flash wuerde das Board jedes Mal zuruecksetzen.
  - Der Flash laeuft ueber flash-satellite.sh, also ueber denselben Guard
    (esp32_flash_guard.py): vollstaendiges 16-MB-Backup vor jedem Schreib-
    Vorgang, ein einmaliger Bestaetigungscode mit 10 Minuten Laufzeit, der
    an Geraet + Backup + Artefakt gebunden ist, und eine Hash-Pruefung des
    geschriebenen Bereichs VOR dem Reset.
  - Es wird NICHTS geflasht, was nicht vorher registriert wurde.
  - Der Geraete-Fingerabdruck wird vom Guard geprueft.

Dieses Skript laeuft auf dem MAC, weil nur dort das Board per USB
erreichbar ist. Es fragt den Server ueber SSH - der Weg ist read-only.

AUFRUF:
   python3 flash-when-plugged.py
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

WORK = pathlib.Path.home() / 'waveshare-va-test'
VENV = WORK / 'venv'
LOG = WORK / 'logs' / 'flash-when-plugged.log'
ARTIFACT = (WORK / 'config/esp32-satellite/.esphome/build'
             / 'growraum-sprache-1-home-test/build/firmware.factory.bin')
EXPECTED_SHA = 'c62c377a92d12b37231af932a1cd9e4538eff83da1997d82cc22a9b019bce9db'
# Der Build-Zeitpunkt der Firmware, die auf dem Board sein soll. Der Server
# liefert genau diesen Stempel.
EXPECTED_BUILD = '2026-10-21'
SERVER = 'lacasa@lacasa-server'
STAMP_CMD = (
    'cd /home/lacasa/Mark-LV-LaCasa-esp32 && '
    'PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:'
    '/home/lacasa/Mark-LV-LaCasa-esp32/scripts '
    '/home/lacasa/waveshare-va-test/venv/bin/python '
    '/home/lacasa/waveshare-va-test/board_firmware_stamp.py')


def log(message):
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as handle:
        handle.write(f'[{stamp}] {message}\n')
    print(message, flush=True)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def board_firmware_stamp():
    """Der Kompilierzeitpunkt der Firmware auf dem Board, oder None."""
    try:
        proc = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=15', '-o', 'BatchMode=yes',
             SERVER, STAMP_CMD],
            capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    lines = [line.strip() for line in (proc.stdout or '').splitlines()
             if line.strip()]
    return lines[-1] if lines else None


def serial_ports():
    """Jeder USB-Serial-Port des Boards, unabhaengig vom Namen."""
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    found = []
    for port in serial.tools.list_ports.comports():
        device = port.device or ''
        if not device.startswith('/dev/cu.'):
            continue
        if any(skip in device for skip in (
                'bluetooth', 'debug-console', 'Bluetooth-Incoming',
                'wlan-debug')):
            continue
        if getattr(port, 'vid', None) is None:
            continue
        found.append((device, port.vid, port.pid))
    return found


def main():
    log('=' * 68)
    log('Flash-Waechter gestartet.')

    # --- 0. Das registrierte Artefakt muss das erwartete sein ----------------
    if not ARTIFACT.exists():
        log(f'FEHLER: das Artefakt fehlt: {ARTIFACT}')
        return 1
    actual = sha256_of(ARTIFACT)
    if actual != EXPECTED_SHA:
        log(f'FEHLER: das Artefakt ist {actual}, erwartet {EXPECTED_SHA} - '
            'es wurde nicht registriert und wird nicht geflasht')
        return 1
    log(f'Artefakt geprueft: {actual}')

    # --- 1. Warten, bis das Board per USB auftaucht --------------------------
    warned = False
    port = None
    while port is None:
        candidates = serial_ports()
        if candidates:
            for device, vid, pid in candidates:
                log(f'Board gefunden: {device} (VID {vid:04x} PID {pid:04x})')
                port = device
                break
        else:
            if not warned:
                log('Warte auf ein per USB angeschlossenes ESP32-Board.')
                log('  Falls das Board schon steckt, aber nicht erscheint:')
                log('   1. ein Kabel mit Datenader verwenden - ein reines')
                log('      Ladekabel uebertraegt keine Daten,')
                log('   2. BOOT gedrueckt halten, waehrend das Kabel')
                log('      einstecken (Download-Modus).')
                warned = True
            time.sleep(3)

    # --- 2. Laeuft auf dem Board schon die richtige Firmware? ---------------
    stamp = board_firmware_stamp()
    if stamp and stamp[:10] >= EXPECTED_BUILD:
        log(f'Das Board laeuft bereits die richtige Firmware ({stamp}) - '
            'es wird NICHT geflasht.')
        log('Nichts zu tun.')
        return 0
    if stamp:
        log(f'Das Board laeuft noch die Firmware vom {stamp} - es wird geflasht.')
    else:
        log('Der Firmware-Stand liess sich nicht lesen - es wird geflasht, '
            'der Guard prueft das Artefakt ohnehin.')

    # --- 3. Der gesicherte Flash --------------------------------------------
    env = dict(os.environ)
    env['PATH'] = f'{VENV / "bin"}:{env.get("PATH", "")}'
    env['PORT'] = port
    log('Starte den gesicherten Flash (Backup, Bestaetigung, Verifikation)...')
    result = subprocess.run(
        ['/bin/sh', str(WORK / 'flash-satellite.sh')],
        env=env, capture_output=True, text=True, timeout=1800)
    output = (result.stdout or '') + (result.stderr or '')
    with open(LOG, 'a', encoding='utf-8') as handle:
        handle.write(output)
        handle.write('\n')
    if result.returncode == 0:
        log('FLASH ERFOLGREICH. Das Board startet neu und holt sich sein WLAN.')
        log('Danach pruefen: ssh lacasa@lacasa-server '
            "'cd /home/lacasa/Mark-LV-LaCasa-esp32 && "
            "PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:"
            "/home/lacasa/Mark-LV-LaCasa-esp32/scripts "
            "/home/lacasa/waveshare-va-test/venv/bin/python "
            "scripts/esp32_live_test.py'")
        return 0
    log('FLASH FEHLGESCHLAGEN - das Board wurde NICHT zurueckgesetzt. '
        f'Details im Log: {LOG}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
