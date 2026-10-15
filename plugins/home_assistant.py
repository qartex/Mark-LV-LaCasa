"""
Home Assistant plugin for Mark-LV (JARVIS) — headless.

Reads HA_URL and HA_TOKEN from environment first, then falls back to config.
Secrets are kept out of git via .gitignore (config/api_keys.json is gitignored).
"""

PLUGIN = {
    "name": "home_assistant",
    "description": (
        "Steuer Geräte in LaCasa über Home Assistant. "
        "Nutze es für: 'Wie ist die Temperatur?', 'Schalte das Licht an', "
        "'Was läuft gerade?', 'Heizung hoch/runter'. "
        "Nicht nutzen für Pflanzen-Status – dafür nutze 'cannabis_status'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "action: 'list', 'query', 'set', 'temperature'"
            },
            "entity": {
                "type": "STRING",
                "description": "entity_id like 'light.wohnen', 'switch.boiler'"
            },
            "value": {
                "type": "STRING",
                "description": "value for set actions: on/off"
            }
        },
        "required": ["action"]
    },
}

import os
import json
import requests
from pathlib import Path


def _load_config() -> dict:
    """Load api_keys.json from config/ if it exists."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except Exception:
            pass
    return {}


# Priority: env vars > config file > hardcoded defaults
_cfg = _load_config()
_env = os.environ

HA_URL = (
    _env.get("HA_URL")
    or _cfg.get("plugin_config", {}).get("home_assistant", {}).get("ha_url")
    or "http://100.74.152.40:8123"
)
HA_TOKEN = (
    _env.get("HA_TOKEN")
    or _cfg.get("plugin_config", {}).get("home_assistant", {}).get("ha_token")
    or ""
)

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "X-HA-Access": HA_TOKEN,
}


def _api_get(path: str) -> dict:
    resp = requests.get(f"{HA_URL}{path}", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _api_post(path: str, body: dict) -> dict:
    resp = requests.post(f"{HA_URL}{path}", headers=HEADERS, json=body, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_states():
    return _api_get("/api/states")


def get_state(entity_id: str):
    return _api_get(f"/api/states/{entity_id}")


def call_service(domain: str, service: str, entity_id: str):
    return _api_post(f"/api/services/{domain}/{service}", {"entity_id": entity_id})


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "")
    entity = parameters.get("entity", "")
    value = parameters.get("value", "")

    try:
        if action == "list":
            states = get_states()
            n = len(states)
            preview = ", ".join(s["entity_id"] for s in states[:20])
            tail = f" (plus {n - 20} weitere)" if n > 20 else ""
            return f"Geräte ({n}): {preview}{tail}"

        elif action == "query":
            if not entity:
                return "Bitte entity_id angeben, z.B. 'sensor.wohnzimmer_temperatur'"
            state = get_state(entity)
            attrs = state.get("attributes", {})
            return f"{state['entity_id']}: {state['state']} {attrs}"

        elif action == "set":
            if not entity:
                return "Bitte entity_id und value angeben"
            domain = entity.split(".")[0]
            service = "turn_on" if value == "on" else "turn_off"
            call_service(domain, service, entity)
            return f"{entity} auf {value} gesetzt."

        elif action == "temperature":
            states = get_states()
            temps = [
                s for s in states
                if s["entity_id"].startswith("sensor.")
                and "temp" in s["entity_id"].lower()
            ]
            if not temps:
                return "Keine Temperatursensoren gefunden."
            lines = [f"{t['entity_id']}: {t['state']}°C" for t in temps]
            return "\n".join(lines)

        else:
            return "Aktion nicht erkannt. Verfügbare Aktionen: list, query, set, temperature"

    except requests.exceptions.ConnectionError:
        return f"Kann HA unter {HA_URL} nicht erreichen. Prüfe Netzwerkverbindung."
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            return "HA-Autorisierung fehlgeschlagen (401). Token oder URL prüfen."
        return f"HA-Fehler {e.response.status_code if e.response else e}"
    except Exception as e:
        return f"HA-Plugin Fehler: {e}"