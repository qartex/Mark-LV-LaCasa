"""Headless policy and local pairing client; no desktop/audio imports."""
import argparse
import copy
import json
import os
import socket
from pathlib import Path


def enabled() -> bool:
    return os.environ.get('JARVIS_HEADLESS', 'true').lower() not in ('0', 'false', 'no', 'off')


def capture_source() -> str:
    source = os.environ.get('JARVIS_CAPTURE_SOURCE', 'phone' if enabled() else 'local')
    if source not in ('phone', 'local'):
        raise ValueError('JARVIS_CAPTURE_SOURCE must be phone or local')
    return source


def tool_allowed(name: str, args: dict) -> bool:
    return name == 'home_assistant' and args.get('action') in ('list', 'query', 'temperature')


def error_text(error):
    if isinstance(error, BaseExceptionGroup):
        return '\n'.join(error_text(child) for child in error.exceptions)
    return str(error)


def output_device(name, hardware):
    """Resolve exact output names serially, without desktop input-rate probes."""
    if not name or name == 'System default':
        return None
    for index, device in enumerate(hardware.query_devices()):
        if device['name'] == name and device.get('max_output_channels', 0) > 0:
            return index
    raise RuntimeError('Configured output device unavailable; refusing headless fallback')


def read_only_registry():
    # Import exactly one trusted plugin. Do not discover/import unused desktop
    # actions or arbitrary plugins merely to hide their declarations afterward.
    from core.plugin_loader import PluginRegistry, PluginRecord
    from plugins import home_assistant
    meta = copy.deepcopy(home_assistant.PLUGIN)
    meta['description'] = 'Read Home Assistant states and temperatures in °C. Read-only.'
    meta['parameters']['properties']['action'] = {
        'type': 'STRING', 'enum': ['list', 'query', 'temperature']}
    meta['parameters']['properties'].pop('value', None)
    record = PluginRecord(name=meta['name'], description=meta['description'],
                          parameters=meta['parameters'], run=home_assistant.run, valid=True)
    return PluginRegistry({record.name: record}, print)


def drain(queue) -> None:
    if queue is not None:
        while not queue.empty():
            queue.get_nowait()


def control_path() -> Path:
    base = Path(os.environ.get('XDG_RUNTIME_DIR', str(Path.home() / '.cache')))
    return Path(os.environ.get('JARVIS_CONTROL_SOCKET', str(base / 'jarvis' / 'control.sock')))


def request_pair(path=None) -> str:
    """Ask the running assistant, never create a disconnected dashboard."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path or control_path()))
        sock.sendall(b'pair\n')
        result = b''
        while b'\n' not in result and len(result) < 4096:
            chunk = sock.recv(4096)
            if not chunk:
                break
            result += chunk
    reply = json.loads(result)
    if 'url' not in reply:
        raise RuntimeError(reply.get('error', 'Pairing failed'))
    return reply['url']


def main():
    parser = argparse.ArgumentParser(description='Private control of the running JARVIS instance')
    parser.add_argument('command', choices=['pair'])
    parser.add_argument('--socket', type=Path, default=control_path())
    args = parser.parse_args()
    try:
        print(request_pair(args.socket))  # explicit operator output, not a service log
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'JARVIS pairing unavailable: {exc}\n')


if __name__ == '__main__':
    main()
