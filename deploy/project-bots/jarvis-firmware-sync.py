#!/usr/bin/env python3
"""Haelt die Satelliten-Firmware auf dem Mac aktuell.

Die Vendor-Dateien (core.yaml, provenance.json) sind die Quelle fuer
den ESPHome-Build auf dem Mac. Sie liegen im Repository auf lacasa-server
und muessen von dort auf den Mac, sonst baut der Mac eine Firmware ohne
die aktuellen Fixes - genau das ist am 2026-10-21 passiert: der Mac
baute ein Artefakt ohne den Reconnect-Pfad.

Der Wächter laeuft auf lacasa15 (dieser Host), weil er von hier aus auf
beide Maschinen kommt. Er uebertraegt nur, wenn sich der Hash aendert,
und meldet sich, wenn der Mac nicht erreichbar ist.

Er schreibt auf dem Mac ausschliesslich die beiden Vendor-Dateien. Kein
Flash, kein Build - der Build bleibt ein bewusster Schritt des Gaertners.
"""

import hashlib
import subprocess
import sys
import time

SERVER = 'lacasa@lacasa-server'
MAC = 'qartex@100.115.163.32'
REPO_DIR = '/home/lacasa/Mark-LV-LaCasa-esp32'
MAC_VENDOR = '/Users/qartex/waveshare-va-test/config/esp32-satellite/vendor'
FILES = ('core.yaml', 'provenance.json')
LOG = '/root/.hermes/logs/jarvis-firmware-sync.log'


def log(message):
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG, 'a', encoding='utf-8') as handle:
        handle.write(f'[{stamp}] {message}\n')


def run(cmd, timeout=120):
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return None, '', f'{type(exc).__name__}: {exc}'


def remote_hash(host, path):
    code, out, _ = run(f"ssh -o ConnectTimeout=15 -o BatchMode=yes {host} "
                       f"shasum -a 256 '{path}' 2>/dev/null || sha256sum '{path}' 2>/dev/null")
    if code != 0:
        return None
    parts = out.split()
    return parts[0] if parts else None


def main():
    findings = []
    for name in FILES:
        server_path = f'{REPO_DIR}/deploy/esp32-satellite/vendor/{name}'
        mac_path = f'{MAC_VENDOR}/{name}'

        server_hash = remote_hash(SERVER, server_path)
        if server_hash is None:
            findings.append(f'QUELLE_FEHLT: {name} ist auf lacasa-server '
                            'nicht lesbar - der Sync wurde uebersprungen')
            continue

        mac_hash = remote_hash(MAC, mac_path)
        if mac_hash is None:
            # Auf dem Mac fehlt die Datei: uebertragen.
            code, _, err = run(
                f"scp -q -o ConnectTimeout=15 -o BatchMode=yes {SERVER}:{server_path} "
                f"{MAC}:{mac_path}")
            if code != 0:
                findings.append(f'MAC_UNERREICHBAR: {name} konnte nicht '
                                f'uebertragen werden ({err.strip()[:80]})')
                continue
            log(f'uebertragen (neu): {name} {server_hash[:12]}')
            findings.append(f'FIRMWARE_QUELLE_ERNEUERT: {name} wurde auf den '
                            'Mac uebertragen - vor dem naechsten Build '
                            'neu bauen')
            continue

        if mac_hash != server_hash:
            code, _, err = run(
                f"scp -q -o ConnectTimeout=15 -o BatchMode=yes {SERVER}:{server_path} "
                f"{MAC}:{mac_path}")
            if code != 0:
                findings.append(f'MAC_UNERREICHBAR: {name} konnte nicht '
                                f'aktualisiert werden ({err.strip()[:80]})')
                continue
            log(f'uebertragen (geaendert): {name} {mac_hash[:12]} -> '
                f'{server_hash[:12]}')
            findings.append(f'FIRMWARE_QUELLE_ERNEUERT: {name} auf dem Mac war '
                            'veraltet und wurde aktualisiert - vor dem '
                            'naechsten Build neu bauen')

    if findings:
        print('\n'.join(findings))
    return 0


if __name__ == '__main__':
    sys.exit(main())
