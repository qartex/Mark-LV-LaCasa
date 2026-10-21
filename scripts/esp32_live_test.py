#!/usr/bin/env python3
"""Der Live-Test am ESP32-Satelliten - eine Anleitung, Schritt fuer Schritt.

Das Skript prueft den Zustand und sagt dir, was als naechstes zu tun ist.
Es ist read-only: kein Flash, kein Restart, kein Write in fremde Systeme.

Aufruf (auf lacasa-server):
    cd /home/lacasa/Mark-LV-LaCasa-esp32
    PYTHONPATH=/home/lacasa/Mark-LV-LaCasa-esp32:/home/lacasa/Mark-LV-LaCasa-esp32/scripts \
      /home/lacasa/waveshare-va-test/venv/bin/python scripts/esp32_live_test.py
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, '/home/lacasa/Mark-LV-LaCasa-esp32/scripts')

from aioesphomeapi import APIClient  # noqa: E402
from aioesphomeapi.log_runner import LogLevel  # noqa: E402

KEY = pathlib.Path('/home/lacasa/waveshare-va-test/api-key.txt')
HOST = 'growraum-sprache-1-home-test.local'
PORT = 6053

# Die Entity-Keys des Boards. Die Namen sind nicht stabil, die Keys schon.
THRESHOLD_KEY = 3398672240
SENSITIVITY_KEY = 666792156
MIC_GAIN_KEY = 3871751869

state = {'turns': 0, 'wake': None}


async def main():
    from esp32_jarvis_bridge import load_api_key

    print('=' * 64)
    print('DER LIVE-TEST - ESP32-SATELLIT')
    print('=' * 64)
    print()

    # Die Bridge haelt den Voice-Assistant-Slot des Boards. Nur EIN Client
    # darf darauf sein - sonst sieht der Test keinen Turn, auch wenn das
    # Board hoert. Also: Bridge kurz stoppen, testen, wieder starten.
    import subprocess

    def systemctl_ok(action):
        """True, wenn der Befehl erfolgreich war (returncode 0)."""
        return subprocess.run(
            ['systemctl', '--user', action, 'esp32-jarvis-bridge.service'],
            capture_output=True, text=True, timeout=30).returncode == 0

    bridge_was_active = systemctl_ok('is-active')
    if bridge_was_active:
        print('Bridge wird kurz gestoppt (sie haelt den Voice-Assistant-Slot) ...')
        systemctl_ok('stop')
        await asyncio.sleep(2)
    print()

    try:
        await run_test()
    finally:
        if bridge_was_active:
            print()
            print('Bridge wird wieder gestartet ...')
            systemctl_ok('start')
    print()


async def run_test():
    from esp32_jarvis_bridge import load_api_key

    key = load_api_key(KEY)
    # Die Bridge spricht das Board ueber den Noise-PSK an - die API ist
    # verschluesselt, ein blosser Login genuegt nicht.
    client = APIClient(HOST, PORT, noise_psk=key)
    await client.connect()

    states = {}

    def on_state(st):
        states[st.key] = getattr(st, 'state', None)

    def on_start(conversation_id, flags, audio_settings, wake_word_phrase):
        state['turns'] += 1
        state['wake'] = wake_word_phrase
        print(f'  TURN GESTARTET (Wakeword={wake_word_phrase!r})', flush=True)

    client.subscribe_states(on_state)
    client.subscribe_voice_assistant(
        handle_start=on_start, handle_stop=None, handle_audio=None)
    await asyncio.sleep(4)

    # --- 1. Firmware ---------------------------------------------------------
    info = await client.device_info()
    stamp = str(info.compilation_time)
    print('1. FIRMWARE')
    print(f'   Board: {info.name} ({info.mac_address})')
    print(f'   ESPHome: {info.esphome_version}')
    print(f'   Kompiliert: {stamp}')
    if stamp[:10] < '2026-10-20':
        print('   -> ALTE FIRMWARE - der Flash hat nicht stattgefunden.')
        print('      Am Mac: sh ~/waveshare-va-test/flash-when-plugged.sh')
        print('      und das Board per USB anstecken.')
    else:
        print('   -> OK, die aktuelle Firmware ist drauf.')
    print()

    # --- 2. Wakeword --------------------------------------------------------
    print('2. WAKEWORD')
    print(f'   Schwelle (Schieber) : {states.get(THRESHOLD_KEY)}')
    print(f'   Empfindlichkeit     : {states.get(SENSITIVITY_KEY)!r}')
    print()

    # --- 3. Mikrofon --------------------------------------------------------
    print('3. MIKROFON')
    print(f'   Mic gain (ES7210)   : {states.get(MIC_GAIN_KEY)} dB')
    print()

    # --- 4. Erkennung -------------------------------------------------------
    print('4. ERKENNUNG - jetzt "Hey Jarvis" sagen (30 Sekunden)')
    for _ in range(30):
        await asyncio.sleep(1)
        if state['turns']:
            break
    if state['turns']:
        print(f'   -> ERKANNT ({state["turns"]} Turn). Der Satellit hoert zu.')
    else:
        print('   -> NICHT ERKANNT.')
        print('      Logs: cd /home/lacasa/waveshare-va-test && '
              './venv/bin/python satellite_wakeword_monitor.py 60')
    print()

    await client.disconnect()
    print('=' * 64)
    print('Naechster Schritt: das Sprecher-Enrollment')
    print('  systemctl --user start jarvis-speaker-capture@lukas')
    print('  dann "Hey Jarvis" sagen und 4 Sekunden sprechen')
    print('=' * 64)


if __name__ == '__main__':
    asyncio.run(main())
