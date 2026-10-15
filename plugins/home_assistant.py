"""
Home Assistant plugin for Mark-LV (JARVIS).

Drop into Mark-LV/plugins/ as home_assistant.py.
Sets ha_url and ha_token from config or environment.
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
import requests

HA_URL = os.environ.get("HA_URL", "http://192.168.1.88:8123")
HA_TOKEN = os.environ.get(
    "HA_TOKEN",
    ""  # set via config/api_keys.json or environment variable
)

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "X-HA-Access": HA_TOKEN,
}


def get_states():
    return requests.get(f"{HA_URL}/api/states", headers=HEADERS, timeout=10).json()


def get_state(entity_id):
    return requests.get(f"{HA_URL}/api/states/{entity_id}", headers=HEADERS, timeout=10).json()


def call_service(domain, service, entity_id):
    return requests.post(
        f"{HA_URL}/api/services/{domain}/{service}",
        headers=HEADERS,
        json={"entity_id": entity_id},
        timeout=10,
    ).json()


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "")
    entity = parameters.get("entity", "")
    value = parameters.get("value", "")

    try:
        if action == "list":
            states = get_states()
            return f"Geräte: {', '.join(s['entity_id'] for s in states)}"

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
    except Exception as e:
        return f"HA-Plugin Fehler: {e}"
