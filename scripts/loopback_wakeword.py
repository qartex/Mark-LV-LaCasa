#!/usr/bin/env python3
"""Beweist die Wakeword-Erkennung, ohne dass ein Mensch am Geraet sein muss.

Die TTS spricht "Hey Jarvis", der Server liefert die WAV ueber HTTP, das
Board spielt sie ueber seinen eigenen Lautsprecher ab - und die Engine
sollte es im Mikrofon hoeren. Der Board-Log zeigt dann
``Detected 'Hey Jarvis' with sliding average probability is ...``.

Das Board ist der einzige, der den Ton erzeugt. Der Server spielt nichts
ueber seine eigene Hardware aus.

Voraussetzungen:
  * der Board laeuft im WLAN und ist ueber die verschluesselte API erreichbar
  * die Produktions-Bridge ist GESTOPPT - nur ein Client darf den
    Voice-Assistant-Slot halten

Aufruf (auf lacasa-server):
  PYTHONPATH=<checkout>:<checkout>/scripts python3 loopback_wakeword.py
"""

import asyncio
import http.server
import json
import pathlib
import struct
import sys
import threading
import wave

from aioesphomeapi import APIClient
from aioesphomeapi.log_runner import LogLevel

WAV_PATH = pathlib.Path('/tmp/hey_jarvis.wav')
BOARD = 'growraum-sprache-1-home-test.local'
PORT = 6053
VOICE = 'Charon'
TTS_MODEL = 'gemini-3.8-flash-lite-tts'
API_KEYS = pathlib.Path('/home/lacasa/Mark-LV-LaCasa/config/api_keys.json')
NOISE_PSK = pathlib.Path('/home/lacasa/waveshare-va-test/api-key.txt')
SERVER_IP = '192.168.1.180'


def api_key() -> str:
    """Der Gemini-Key aus der Konfiguration, ohne ihn auszugeben."""
    cfg = json.loads(API_KEYS.read_text(encoding='utf-8'))
    key = cfg.get('gemini_api_key')
    if not isinstance(key, str) or not key:
        raise SystemExit('kein Gemini-Key in der Konfiguration')
    return key


def tts_pcm(text: str) -> tuple[bytes, int]:
    """Gemini-TTS: rohes PCM und die Abtastrate."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key())
    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=['AUDIO'],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=VOICE))),
        ))
    for part in response.candidates[0].content.parts:
        if getattr(part, 'inline_data', None) and part.inline_data.data:
            return part.inline_data.data, 24000
    raise SystemExit('die TTS lieferte kein Audio')


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Mono-PCM16 von src_rate auf dst_rate."""
    if src_rate == dst_rate:
        return pcm
    count = len(pcm) // 2
    samples = struct.unpack(f'<{count}h', pcm[:count * 2])
    ratio = dst_rate / src_rate
    out = []
    for i in range(int(count * ratio)):
        pos = i / ratio
        left = int(pos)
        frac = pos - left
        a = samples[left] if left < count else 0
        b = samples[left + 1] if left + 1 < count else a
        out.append(int(a + (b - a) * frac))
    return struct.pack(f'<{len(out)}h', *out)


def write_wav(path: pathlib.Path, pcm: bytes, rate: int = 48000):
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def serve_once(path: pathlib.Path) -> str:
    """Ein HTTP-Server, der die WAV genau einmal ausliefert."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(('0.0.0.0', 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()
    return f'http://{SERVER_IP}:{port}/hey_jarvis.wav'


async def main():
    print('1. TTS erzeugt "Hey Jarvis" ...', flush=True)
    pcm, rate = tts_pcm('Hey Jarvis.')
    pcm = resample(pcm, rate, 48000)
    write_wav(WAV_PATH, pcm)
    print(f'   {len(pcm) // 2} Samples, 48 kHz mono', flush=True)

    url = serve_once(WAV_PATH)
    print(f'2. WAV wird serviert: {url}', flush=True)

    psk = NOISE_PSK.read_text(encoding='utf-8').strip()
    client = APIClient(BOARD, PORT, noise_psk=psk)
    await client.connect()

    detected = []
    started = []

    def on_log(msg):
        text = str(getattr(msg, 'message', msg))
        for line in text.splitlines():
            if 'Detected' in line or 'wake word' in line.lower():
                detected.append(line)
                print('   LOG:', line, flush=True)

    def on_start(conversation_id, flags, audio_settings, wake_word_phrase):
        started.append(wake_word_phrase)
        print(f'   *** TURN GESTARTET, Wakeword={wake_word_phrase!r}',
              flush=True)

    client.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_DEBUG)
    client.subscribe_voice_assistant(
        handle_start=on_start, handle_stop=None, handle_audio=None)
    await asyncio.sleep(2)

    # Der media_player traegt in der YAML "name: None" - er ist also die
    # einzige Entitaet ohne Namen. Adressiert wird er ueber seinen KEY.
    listed = await client.device_info_and_list_entities()
    entities = listed[1] if isinstance(listed, tuple) else listed
    player_key = None
    for entity in entities:
        if not str(getattr(entity, 'name', '') or '').strip():
            player_key = getattr(entity, 'key', None)
            break
    if player_key is None:
        print('   kein media_player auf dem Board gefunden', flush=True)
        await client.disconnect()
        return 1

    print('3. Board spielt die Datei ab ...', flush=True)
    client.media_player_command(
        player_key, media_url=url, volume=0.35, announcement=False)
    for _ in range(25):
        await asyncio.sleep(1)
        if started or detected:
            await asyncio.sleep(2)
            break

    print()
    if started:
        print('ERGEBNIS: ERKANNT - der Turn kam zustande '
              f'(Wakeword {started[0]!r})')
    elif detected:
        print('ERGEBNIS: ERKANNT - der Log zeigt eine Erkennung')
    else:
        print('ERGEBNIS: NICHT ERKANNT - der eigene Lautsprecher ist dem '
              'Mikrofon zu nah oder zu laut.')
    await client.disconnect()
    return 0 if (started or detected) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
