#!/usr/bin/env python3
"""Der Fortschrittsbot: was ist am JARVIS-Projekt noch offen?

Der Wächter liest nur. Er startet nichts neu, schreibt nichts in fremde
Systeme und gibt keine Geheimnisse aus. Er beantwortet eine Frage:
"Was steht noch an, und was muss der Gärtner tun?"

Geprueft wird:
  1. Firmware des Satelliten-Board - laeuft sie schon mit den Fixes?
  2. Sprecher-Enrollment - gibt es ein Profil?
  3. Adapter-Pin - laeuft der Host gegen die installierte Version?
  4. Checkouts - stehen Satellit und PC-JARVIS auf demselben Commit?

Stille bei gesund, eine Zeile je offenem Punkt bei Problemen.
"""

import json
import subprocess
import sys

SERVER = 'lacasa@lacasa-server'
SSH = ['ssh', '-o', 'ConnectTimeout=20', '-o', 'BatchMode=yes', SERVER]

findings = []


def finding(text):
    findings.append(text)


def ssh_run(command, timeout=90):
    """Ein Befehl auf dem Server. Gibt (code, stdout) zurueck.

    Bei einem Fehler ist der code ungleich 0 - fail-closed, ein
    fehlgeschlagener Test ist ein Befund, nicht ein gesunder Zustand.
    """
    try:
        proc = subprocess.run(SSH + [command], capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout
    except Exception as exc:
        return None, f'{type(exc).__name__}: {exc}'


def check_board_firmware():
    """Der Board-Stempel wird von einem Skript auf dem Server gelesen."""
    code, out = ssh_run(
        'cd /home/lacasa/Mark-LV-LaCasa-esp32 && '
        'PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:'
        '/home/lacasa/Mark-LV-LaCasa-esp32/scripts '
        '/home/lacasa/waveshare-va-test/venv/bin/python '
        '/home/lacasa/waveshare-va-test/board_firmware_stamp.py', timeout=120)
    if code != 0:
        finding('BOARD_UNREACHBAR: das Satelliten-Board antwortet nicht - '
                'die Pruefung konnte nicht durchgefuehrt werden')
        return
    stamp = (out or '').strip().splitlines()[-1].strip() if out else ''
    if not stamp:
        finding('BOARD_UNREACHBAR: das Board lieferte keinen Firmware-Stempel')
        return
    day = stamp[:10]
    if day < '2026-10-20':
        finding(
            f'FLASH_OFFEN: das Board laeuft die Firmware vom {day} - vor dem '
            'Reconnect-Fix der Wakeword-Engine. Solange die alte Firmware '
            'laeuft, erkennt der Satellit kein "Hey Jarvis". '
            'Handlung: Mac einschalten, ESP32 per USB anstecken, dann '
            'sh ~/waveshare-va-test/flash-satellite.sh')
        # Ohne den Flash gibt es auch kein Sprecher-Enrollment: das Board
        # streamt Mikrofon-Audio nur, wenn es selbst einen Turn startet,
        # und das tut es nur mit laufender Engine. Die ferngesteuerte
        # Alternative (set_voice_assistant_configuration) setzt den
        # Wakeword, loest aber keinen Turn aus - das ist geprueft und
        # bringt nichts.
        finding(
            'SPEAKER_ABHANGIG_VOM_FLASH: das Sprecher-Enrollment laesst sich '
            'erst nach dem Flash durchfuehren - ohne laufende Engine startet '
            'das Board keinen Aufnahme-Turn. Erst flashen, dann '
            'systemctl --user start jarvis-speaker-capture@lukas')
    else:
        print(f'BOARD_OK: Firmware vom {day} - die Fixes sind auf dem Board',
              file=sys.stderr)


def check_speaker_profile():
    code, out = ssh_run(
        'test -s /home/lacasa/waveshare-va-test/speaker-profiles.json '
        '&& echo PROFILE || echo KEIN_PROFIL')
    if code != 0:
        finding('SPEAKER_UNPRUEFBAR: der Sprecher-Status liess sich nicht '
                'pruefen')
        return
    if 'PROFIL' in (out or '') and 'KEIN_PROFIL' not in (out or ''):
        print('SPEAKER_OK: ein Sprecherprofil liegt vor', file=sys.stderr)
        return
    if any('SPEAKER_ABHANGIG_VOM_FLASH' in f for f in findings):
        # Der Flash-Befund sagt es bereits, mit derselben Handlung.
        return
    finding(
        'SPEAKER_ENROLL_OFFEN: es gibt noch kein Sprecherprofil, damit '
        'erkennt JARVIS nicht, wer spricht. Handlung: auf lacasa-server '
        'systemctl --user start jarvis-speaker-capture@lukas, dann vor dem '
        'Board "Hey Jarvis" sagen und etwa 4 Sekunden sprechen')

def check_adapter_pin():
    code, out = ssh_run(
        '/home/lacasa/cannanas-adapter/venv/bin/cannanas-mcp-adapter '
        '--adapter-info 2>/dev/null')
    if code != 0:
        finding('ADAPTER_UNREACHBAR: der Adapter meldet sich nicht')
        return
    try:
        info = json.loads((out or '').strip().splitlines()[-1])
    except Exception:
        finding('ADAPTER_UNREACHBAR: die Adapter-Info ist unlesbar')
        return

    version = str(info.get('adapter_version', '?'))
    pinned = bool(info.get('release_pinned'))
    tools = len(info.get('tools') or [])

    code2, out2 = ssh_run(
        "grep -h PINNED_ADAPTER_VERSION "
        "/home/lacasa/Mark-LV-LaCasa-esp32/core/cannanas_client.py")
    pinned_version = ''
    if code2 == 0 and out2 and "'" in out2:
        pinned_version = out2.split("'")[1]

    # Der Hash muss mitueinstimmen. Die Version allein genuegt nicht:
    # ein Adapter kann die richtige Versionsnummer melden und trotzdem
    # ein anderes Artefakt geladen haben.
    code3, out3 = ssh_run(
        "grep -h PINNED_WHEEL_SHA256 "
        "/home/lacasa/Mark-LV-LaCasa-esp32/core/cannanas_client.py")
    pinned_sha = ''
    if code3 == 0 and out3 and "'" in out3:
        pinned_sha = out3.split("'")[1]
    installed_sha = str(info.get('artifact_sha256') or '')

    if pinned_version and pinned_version != version:
        finding(
            f'ADAPTER_VERALTET: installiert ist {version}, gepinnt ist '
            f'{pinned_version}. Der Host laeuft gegen eine andere Version '
            'als der ausgelieferte Adapter - ein Update steht aus')
        return
    if pinned_sha and installed_sha and pinned_sha != installed_sha:
        finding(
            f'ADAPTER_ARTEFAKT_FALSCH: der Adapter meldet Version {version}, '
            f'aber sein Artefakt-Hash {installed_sha[:12]} weicht vom Pin '
            f'{pinned_sha[:12]} ab. Die Sidecar (release_info.json neben dem '
            'Entry-Point) ist veraltet - beim Install mitkopieren')
        return
    if not installed_sha:
        finding('ADAPTER_OHNE_ARTEFAKT: der Adapter meldet keinen Hash - '
                'die Sidecar release_info.json fehlt neben dem Entry-Point')
        return
    if not pinned:
        finding('ADAPTER_UNGEPINNT: der Adapter meldet release_pinned=False - '
                'die ausgelieferte Version ist nicht fest verdrahtet')
        return
    print(f'ADAPTER_OK: {version}, gepinnt, {tools} Werkzeuge',
          file=sys.stderr)


def check_checkouts():
    code, out = ssh_run(
        'for d in /home/lacasa/Mark-LV-LaCasa-esp32 /home/lacasa/Mark-LV-LaCasa; '
        'do cd $d && git rev-parse --short HEAD; done')
    if code != 0:
        finding('CHECKOUT_UNPRUEFBAR: die Checkouts liessen sich nicht lesen')
        return
    commits = [line.strip() for line in (out or '').splitlines() if line.strip()]
    if len(set(commits)) > 1:
        finding('CHECKOUT_VERSCHIEBEN: der Satellit und der PC-JARVIS '
                f'stehen auf {" und ".join(commits)} - ein Update steht fuer '
                'einen der beiden Baeume aus')
        return
    print(f'CHECKOUT_OK: beide Baeume stehen auf {commits[0]}', file=sys.stderr)


def main():
    check_board_firmware()
    check_speaker_profile()
    check_adapter_pin()
    check_checkouts()
    if findings:
        print('\n'.join(findings))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
