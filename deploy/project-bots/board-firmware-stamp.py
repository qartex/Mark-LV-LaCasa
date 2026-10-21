#!/usr/bin/env python3
"""Liest den Firmware-Stand des Satelliten-Board aus.

Läuft auf lacasa-server, weil dort der ESPHome-Key liegt. Gibt nur den
Kompilierzeitpunkt aus - kein Geheimnis.
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, '/home/lacasa/Mark-LV-LaCasa-esp32/scripts')

from esp32_jarvis_bridge import EspHomeVoiceClient, load_api_key  # noqa: E402

KEY = pathlib.Path('/home/lacasa/waveshare-va-test/api-key.txt')


async def main():
    key = load_api_key(KEY)
    client = EspHomeVoiceClient('growraum-sprache-1-home-test.local', 6053, key)
    await client.connect_and_subscribe(
        lambda *a: 0, lambda *a: None, lambda *a: None)
    try:
        info = await client._client.device_info()
        print(info.compilation_time)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
