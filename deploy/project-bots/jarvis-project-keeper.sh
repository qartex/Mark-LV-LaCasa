#!/bin/bash
# ============================================================================
# jarvis-project-keeper.sh - Der Betreuer fuer das Projekt Mark-LV-LaCasa
#                             (headless JARVIS + Cannanas-MCP-Adapter)
#
# Zweck:
#   Der Wächter schaut in einem Durchgang auf den produktiven Zustand des
#   Projekts und meldet Abweichungen. Er ist ein Wächter, kein Verwalter:
#   er liest, prüft und meldet - er startet nichts neu, installiert nichts,
#   committet nichts und repariert nichts.
#
# Geprüft wird (alles auf lacasa-server, sofern nicht anders vermerkt):
#   1. Die drei systemd-Benutzerdienste laufen:
#      esp32-jarvis-bridge.service, jarvis-learning-dispatch.timer,
#      jarvis.service - jeder einzeln benannt.
#   2. Der produktive Adapter ist installiert und passt zum Repo-Pin:
#      Version, Wheel-SHA-256 und Werkzeuganzahl aus core/cannanas_client.py
#      des deployten Baums /home/lacasa/Mark-LV-LaCasa-esp32.
#   3. Beide Checkouts (Satellit und PC-JARVIS) stehen auf demselben Commit
#      wie origin/feat/esp32-satellite-phase1, und die erwarteten Dateien
#      sind dort unverändert. Im PC-Baum sind höchstens die zwei bekannten
#      lokalen Dateien erlaubt.
#   4. Das ESP32-Board ist erreichbar (TCP 6053, mit Timeout) und antwortet.
#   5. Die Home-Assistant-Zonensensoren sind lesbar und die 6 Zonensensoren
#      liefern Werte (nicht None/leer) - gelesen über genau denselben Weg
#      wie die Bridge (core/growroom_measurements.py + _ha_states aus
#      scripts/esp32_jarvis_bridge.py) mit dem Bridge-Checkout im PYTHONPATH.
#   6. Die Learning-Queue läuft nicht volllauf (> 50 Einträge in pending
#      oder quarantine) - ein Zeichen, dass der Dispatcher hängt.
#
# Vertrag:
#   - STILLE BEI GESUND: leere stdout-Ausgabe, wenn alles in Ordnung ist.
#   - EINMAL ALARMIERT: gemeldet wird nur, wenn sich der Zustand ändert
#     (ein neuer Befund taucht auf). Der letzte Zustand steht im eigenen Log
#     und wird von dort gelesen - kein zweiter Dateischreibvorgang.
#   - FAIL-CLOSED: kann ein Prüfschritt nicht laufen (Host nicht erreichbar,
#     Werkzeug fehlt, Adapter antwortet nicht), ist das ein Befund und wird
#     gemeldet - niemals ein gesunder Zustand.
#   - IDEMPOTENT: mehrfach laufen lassen ändert am Produktivsystem nichts.
#   - KEINE GEHEIMNISSE: es werden nur Hashes, Versionsnummern, ja/nein und
#     Anzahlen ausgegeben. Keine API-Keys, keine Tokens, keine Club-IDs,
#     keine WLAN-Daten.
#   - SCHREIBT NUR INS EIGENE LOG: /root/.hermes/logs/jarvis-project-keeper.log
#     (eigene Rotation, max ~1 MB, zwei Generationen).
#
# Aufruf:
#   jarvis-project-keeper.sh                 # still bei gesund
#   JPK_ADAPTER_CMD=/falsch/pfad ...         # Stoerfall-Tests
#
# Exitcode: 0 - der Lauf war erfolgreich (die Meldung IST das Ergebnis).
#           1 - der Wächter selbst ist kaputt (Aufruf/Log unbrauchbar).
# ============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# Konfiguration - alle Pfade absolut, alles per Environment uebersteuerbar
# ---------------------------------------------------------------------------
LOG_FILE="${JPK_LOG_FILE:-/root/.hermes/logs/jarvis-project-keeper.log}"
LOG_MAX_BYTES="${JPK_LOG_MAX_BYTES:-1048576}"

SSH_BIN="${JPK_SSH_BIN:-/usr/bin/ssh}"
SSH_OPTS_RAW="${JPK_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR -o ServerAliveInterval=10}"
SERVER="${JPK_SERVER:-lacasa@lacasa-server}"
SSH_TIMEOUT="${JPK_SSH_TIMEOUT:-60}"
# shellcheck disable=SC2206
SSH_OPTS=(${SSH_OPTS_RAW})

# Die drei Dienste, die laufen muessen (leerraumgetrennt, fuer Stoerfalltests).
SERVICE_UNITS_RAW="${JPK_SERVICE_UNITS:-esp32-jarvis-bridge.service jarvis-learning-dispatch.timer jarvis.service}"
# Achtung: ssh zerlegt ein Argument an Leerzeichen, bevor es die Gegenstelle
# sieht. Die Liste geht deshalb kommagetrennt und ohne Leerzeichen raus.
SERVICE_UNITS_ARG=$(printf '%s' "$SERVICE_UNITS_RAW" | /usr/bin/tr ' ' ',')

REMOTE_SAT_DIR="${JPK_SAT_DIR:-/home/lacasa/Mark-LV-LaCasa-esp32}"
REMOTE_PC_DIR="${JPK_PC_DIR:-/home/lacasa/Mark-LV-LaCasa}"
REMOTE_BRANCH="${JPK_BRANCH:-feat/esp32-satellite-phase1}"
REMOTE_PYTHON="${JPK_REMOTE_PYTHON:-/usr/bin/python3}"
ADAPTER_CMD="${JPK_ADAPTER_CMD:-/home/lacasa/cannanas-adapter/venv/bin/cannanas-mcp-adapter}"
ADAPTER_VENV="${JPK_ADAPTER_VENV:-/home/lacasa/cannanas-adapter/venv}"
BOARD_HOST="${JPK_BOARD_HOST:-growraum-sprache-1-home-test.local}"
BOARD_PORT="${JPK_BOARD_PORT:-6053}"
BOARD_PYTHON="${JPK_BOARD_PYTHON:-/home/lacasa/waveshare-va-test/venv/bin/python}"
BOARD_KEY_FILE="${JPK_BOARD_KEY_FILE:-/home/lacasa/waveshare-va-test/api-key.txt}"
BOARD_PY_PATH="${JPK_BOARD_PY_PATH:-/home/lacasa/waveshare-va-test}"
QUEUE_DIR="${JPK_QUEUE_DIR:-/home/lacasa/waveshare-va-test/learning-queue}"
QUEUE_MAX="${JPK_QUEUE_MAX:-50}"
EXPECTED_SENSORS="${JPK_EXPECTED_SENSORS:-6}"
EXPECTED_TOOLS="${JPK_EXPECTED_TOOLS:-40}"

# Firmware-Sollwert des Boards. Leer (Vorgabe) = der Firmware-Stand wird nur
# protokolliert; der taegliche Fortschrittsbot (jarvis-progress.py) meldet
# den offenen Flash bereits mit dem fertigen Befehl. Auf ein Datum
# (YYYY-MM-DD) setzen, um eine veraltete Firmware zusaetzlich als Befund zu
# melden.
FIRMWARE_MIN_DATE="${JPK_FIRMWARE_MIN_DATE:-}"

# Die zwei bekannten, erlaubten lokalen Aenderungen im PC-JARVIS-Baum.
# Alles andere in git status ist ein Befund.
PC_ALLOWED_STATUS=(" M core/spotify.py" "?? config/briefing-speakers.json")

# ---------------------------------------------------------------------------
# Zustand
# ---------------------------------------------------------------------------
PROBLEMS=()
NOTES=()          # nur ins Log, nie nach stdout
DEBUG="${JPK_DEBUG:-0}"
DEADLINE=$(( $(/usr/bin/date +%s) + ${JPK_DEADLINE_SECONDS:-150} ))

add_problem() { PROBLEMS+=("$1"); }
add_note()    { NOTES+=("$1"); }

note_raw() {
  [ "$DEBUG" = "1" ] || return 0
  printf '### %s\n%s\n' "$1" "$2" >&2
}

deadline_reached() { [ "$(/usr/bin/date +%s)" -ge "$DEADLINE" ]; }

baum_name() {
  case "$1" in
    sat) printf 'Satelliten' ;;
    pc)  printf 'PC-JARVIS' ;;
    *)   printf '%s' "$1" ;;
  esac
}

# ---------------------------------------------------------------------------
# Log: Sperre, Zustand merken, schreiben, rotieren
# ---------------------------------------------------------------------------
LOG_DIR="$(/usr/bin/dirname "$LOG_FILE")"
/usr/bin/mkdir -p "$LOG_DIR" || { printf 'LOGDIR_UNBRAUCHBAR: %s\n' "$LOG_DIR" >&2; exit 1; }

# Das Log selbst traegt die Sperre: ein zweiter gleichzeitiger Lauf wartet,
# statt das Log zu zerreissen. Die Rotation (siehe unten) behaelt den Inode,
# damit die Sperre nicht an einem verschobenen Dateihandle haengt.
exec 9>>"$LOG_FILE" || { printf 'LOG_UNBRAUCHBAR: %s\n' "$LOG_FILE" >&2; exit 1; }
/usr/bin/flock -w 60 9 || { printf 'LOG_GESPERRT: %s\n' "$LOG_FILE" >&2; exit 1; }

# Vorheriger Zustand aus dem eigenen Log lesen.
# Form: "STATE state=<hash|ok> problems=<n>"
read_previous_state() {
  local line
  line=$(/usr/bin/grep -a 'STATE state=' "$LOG_FILE" 2>/dev/null | /usr/bin/tail -1)
  PREV_STATE=$(printf '%s' "$line" | /usr/bin/sed -n 's/.*STATE state=\([0-9a-z]*\).*/\1/p')
  PREV_COUNT=$(printf '%s' "$line" | /usr/bin/sed -n 's/.*problems=\([0-9]*\).*/\1/p')
  [ -n "$PREV_STATE" ] || PREV_STATE="keiner"
  [ -n "$PREV_COUNT" ] || PREV_COUNT="0"
}
read_previous_state

state_hash() {
  if [ "${#PROBLEMS[@]}" -eq 0 ]; then
    printf 'ok'
    return 0
  fi
  printf '%s\n' "${PROBLEMS[@]}" | LC_ALL=C sort | /usr/bin/sha256sum | /usr/bin/cut -c1-16
}

# Log-Rotation: haelt das Log bei ~1 MB und reicht den Zustand weiter,
# damit die Rotation keine erneute Alarmmeldung ausloest.
# Die Datei wird am Platz gekuerzt (gleicher Inode): der offene Sperrgriff
# (fd 9) bleibt gueltig und die Historie bleibt lesbar.
rotate_log_if_needed() {
  local size keep
  size=$(/usr/bin/stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
  [ "$size" -gt "$LOG_MAX_BYTES" ] || return 0
  # Die letzten LOG_MAX_BYTES Bytes behalten, die erste (halbe) Zeile
  # abschneiden, damit keine verstümmelte Zeile im Log steht.
  keep=$(/usr/bin/tail -c "$LOG_MAX_BYTES" "$LOG_FILE" 2>/dev/null)
  keep="${keep#*$'\n'}"
  printf '%s\n' "$keep" >"$LOG_FILE" 2>/dev/null || return 0
  /usr/bin/chmod 600 "$LOG_FILE" 2>/dev/null || true
  printf '[%s] STATE state=%s problems=%s (Log gekuerzt auf %s Bytes, Zustand uebernommen)\n' \
    "$(/usr/bin/date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" "$LOG_MAX_BYTES" >>"$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Fernabfragen
# ---------------------------------------------------------------------------

# 1) Dienste + Adapter + Pin
remote_services_adapter() {
  /usr/bin/timeout "$SSH_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" -T "$SERVER" bash -s -- \
    "$ADAPTER_CMD" "$ADAPTER_VENV" "$REMOTE_SAT_DIR" "$REMOTE_PYTHON" "$SERVICE_UNITS_ARG" <<'REMOTE_A'
set -u
adapter_cmd="$1"; adapter_venv="$2"; sat_dir="$3"; python="$4"; units="$5"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
for unit in $(printf '%s' "$units" | tr ',' ' '); do
  active=$(systemctl --user is-active "$unit" 2>/dev/null || true)
  enabled=$(systemctl --user is-enabled "$unit" 2>/dev/null || true)
  printf 'UNIT\t%s\t%s\t%s\n' "$unit" "${active:-unbekannt}" "${enabled:-unbekannt}"
done
ADAPTER_CMD="$adapter_cmd" ADAPTER_VENV="$adapter_venv" SAT_DIR="$sat_dir" \
"$python" - <<'PYEOF_A' 2>/dev/null || printf 'PY\tERROR\n'
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

adapter_cmd = os.environ.get("ADAPTER_CMD", "")
adapter_venv = os.environ.get("ADAPTER_VENV", "")
sat_dir = os.environ.get("SAT_DIR", "")


def emit(*parts):
    print("\t".join(str(part) for part in parts))


# --- Adapter-Info (liest nur) -------------------------------------------
try:
    proc = subprocess.run([adapter_cmd, "--adapter-info"],
                          capture_output=True, text=True, timeout=60)
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not lines:
        emit("ADAPTER", "ERROR", "exit_%d" % proc.returncode)
    else:
        data = json.loads(lines[-1])
        emit("ADAPTER",
             str(data.get("adapter_version") or ""),
             str(data.get("artifact_sha256") or ""),
             "true" if data.get("release_pinned") else "false",
             len(data.get("tools") or []))
except Exception as exc:
    emit("ADAPTER", "ERROR", type(exc).__name__)

# --- Pin aus dem deployten Repo-Baum ------------------------------------
pin_version = ""
pin_sha = ""
pin_tools = -1
try:
    text = open(os.path.join(sat_dir, "core", "cannanas_client.py"),
                encoding="utf-8").read()
    hit = re.search(r"PINNED_ADAPTER_VERSION\s*=\s*'([^']*)'", text)
    pin_version = hit.group(1) if hit else ""
    hit = re.search(r"PINNED_WHEEL_SHA256\s*=\s*'([^']*)'", text)
    pin_sha = hit.group(1) if hit else ""
except Exception as exc:
    emit("PIN", "ERROR", type(exc).__name__)

if pin_version and pin_sha:
    try:
        sys.path.insert(0, sat_dir)
        from core.cannanas_client import ADAPTER_TOOLS
        pin_tools = len(ADAPTER_TOOLS())
    except Exception:
        # Rueckfall: die Werkzeugliste direkt aus der Quelle zaehlen.
        try:
            text = open(os.path.join(sat_dir, "core", "cannanas_client.py"),
                        encoding="utf-8").read()
            block = text.split("def ADAPTER_TOOLS()", 1)[-1].split("\n]", 1)[0]
            pin_tools = len(re.findall(r"^\s*'[^']+',?\s*$", block, re.M))
        except Exception:
            pin_tools = -1
    emit("PIN", pin_version, pin_sha, pin_tools)

# --- installierte Distribution ------------------------------------------
try:
    versions = []
    pattern = os.path.join(adapter_venv, "lib", "python*", "site-packages",
                            "cannanas_mcp_adapter-*.dist-info")
    for path in glob.glob(pattern):
        base = os.path.basename(path)
        # "cannanas_mcp_adapter-2.8.0.dist-info" -> "2.8.0".
        # split("-") greift nicht: "dist-info" enthaelt selbst einen Bindestrich.
        hit = re.search(r"cannanas_mcp_adapter-(.+)\.dist-info$", base)
        if hit:
            versions.append(hit.group(1))
    emit("DIST", ",".join(sorted(versions)) or "keine")
except Exception as exc:
    emit("DIST", "ERROR", type(exc).__name__)

# --- liegt das gepinnte Artefakt (Wheel) ueberhaupt auf dem Server? ------
try:
    found = "nein"
    if pin_sha:
        for wheel in sorted(glob.glob(os.path.join(adapter_venv, "..", "dist", "*.whl"))):
            with open(wheel, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            if digest == pin_sha:
                found = "ja"
                break
    emit("WHEEL", found)
except Exception as exc:
    emit("WHEEL", "ERROR", type(exc).__name__)
PYEOF_A
REMOTE_A
}

# 2) Git: beide Checkouts
remote_git() {
  /usr/bin/timeout "$SSH_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" -T "$SERVER" bash -s -- \
    "$REMOTE_SAT_DIR" "$REMOTE_PC_DIR" "$REMOTE_BRANCH" <<'REMOTE_B'
set -u
sat_dir="$1"; pc_dir="$2"; branch="$3"
for label in sat pc; do
  case "$label" in
    sat) d="$sat_dir" ;;
    pc)  d="$pc_dir" ;;
  esac
  if [ ! -d "$d/.git" ]; then
    printf 'GIT\t%s\tERROR\tERROR\tunbekannt\n' "$label"
    continue
  fi
  head=$(git -C "$d" rev-parse HEAD 2>/dev/null || echo ERROR)
  track=$(git -C "$d" rev-parse "origin/$branch" 2>/dev/null || echo ERROR)
  remote=""
  if [ "$label" = "sat" ]; then
    remote=$(git -C "$d" ls-remote origin "refs/heads/$branch" 2>/dev/null | cut -f1)
  fi
  printf 'GIT\t%s\t%s\t%s\t%s\n' "$label" "$head" "$track" "${remote:-unbekannt}"
  git --no-optional-locks -C "$d" status --porcelain 2>/dev/null | while IFS= read -r line; do
    [ -n "$line" ] || continue
    printf 'GITSTATUS\t%s\t%s\n' "$label" "$line"
  done
  if git --no-optional-locks -C "$d" diff --quiet HEAD -- \
      core/cannanas_client.py core/growroom_measurements.py \
      scripts/esp32_jarvis_bridge.py 2>/dev/null; then
    printf 'DATEIEN\t%s\tunveraendert\n' "$label"
  else
    printf 'DATEIEN\t%s\tgeaendert\n' "$label"
  fi
done
for f in core/cannanas_client.py core/growroom_measurements.py scripts/esp32_jarvis_bridge.py; do
  a=$(sha256sum "$sat_dir/$f" 2>/dev/null | cut -d' ' -f1)
  b=$(sha256sum "$pc_dir/$f" 2>/dev/null | cut -d' ' -f1)
  if [ -n "$a" ] && [ -n "$b" ] && [ "$a" = "$b" ]; then
    printf 'QUER\t%s\tidentisch\n' "$f"
  else
    printf 'QUER\t%s\tabweichend\n' "$f"
  fi
done
REMOTE_B
}

# 3) Board: DNS, TCP, Protokoll
remote_board() {
  /usr/bin/timeout "$SSH_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" -T "$SERVER" bash -s -- \
    "$BOARD_HOST" "$BOARD_PORT" "$BOARD_PYTHON" "$BOARD_KEY_FILE" "$BOARD_PY_PATH" <<'REMOTE_C'
set -u
host="$1"; port="$2"; py="$3"; key="$4"; py_path="$5"
if getent hosts "$host" 2>/dev/null | awk '{print $1}' | head -1 | grep -q .; then
  printf 'DNS\tok\n'
else
  printf 'DNS\tfehler\n'
fi
if timeout 8 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
  printf 'TCP\tok\n'
else
  printf 'TCP\tfehler\n'
fi
BOARD_HOST="$host" BOARD_PORT="$port" BOARD_KEY="$key" BOARD_PYPATH="$py_path" \
"$py" - <<'PYEOF_C' 2>/dev/null || printf 'PROTO\tERROR\n'
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, os.environ.get("BOARD_PYPATH", ""))


async def probe():
    from aioesphomeapi import APIClient
    from esp32_api_acceptance import load_api_key
    key = load_api_key(pathlib.Path(os.environ["BOARD_KEY"]))
    client = APIClient(address=os.environ["BOARD_HOST"],
                       port=int(os.environ["BOARD_PORT"]),
                       password=None, noise_psk=key)
    await client.connect()
    try:
        info = await client.device_info()
        print("PROTO\tok\t%s\t%s" % (info.name, info.compilation_time))
    finally:
        await client.disconnect()


try:
    asyncio.run(asyncio.wait_for(probe(), timeout=30))
except Exception as exc:
    print("PROTO\tERROR\t%s" % type(exc).__name__)
PYEOF_C
REMOTE_C
}

# 4) Home-Assistant-Sensoren ueber genau den Weg der Bridge
remote_sensors() {
  /usr/bin/timeout "$SSH_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" -T "$SERVER" bash -s -- \
    "$REMOTE_SAT_DIR" "$EXPECTED_SENSORS" "$REMOTE_PYTHON" <<'REMOTE_D'
set -u
sat_dir="$1"; expected="$2"; python="$3"
SAT_DIR="$sat_dir" EXPECTED="$expected" "$python" - <<'PYEOF_D' 2>/dev/null || printf 'SENSOR\tERROR\n'
import importlib.util
import os
import sys

sat_dir = os.environ["SAT_DIR"]
sys.path.insert(0, sat_dir)
sys.path.insert(0, os.path.join(sat_dir, "scripts"))

spec = importlib.util.spec_from_file_location(
    "jbridge", os.path.join(sat_dir, "scripts", "esp32_jarvis_bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

from core.growroom_measurements import zone_temperatures

states = bridge._ha_states(bridge.JARVIS_CONFIG_PATH)
temperatures = zone_temperatures(states)

missing = []
for state in states:
    entity = str(state.get("entity_id") or "")
    if entity.startswith("sensor.zonen_") and entity.endswith("_temperatur_celsius"):
        room = entity[len("sensor.zonen_"):-len("_temperatur_celsius")]
        try:
            float(state.get("state"))
        except (TypeError, ValueError):
            missing.append(room)

print("SENSOR\tok\t%d\t%d\t%s" % (len(temperatures), int(os.environ["EXPECTED"]),
                                  ",".join(sorted(missing))))
PYEOF_D
REMOTE_D
}

# 5) Learning-Queue
remote_queue() {
  /usr/bin/timeout "$SSH_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" -T "$SERVER" bash -s -- \
    "$QUEUE_DIR" "$QUEUE_MAX" <<'REMOTE_E'
set -u
q="$1"; max="$2"
if [ ! -d "$q" ]; then
  printf 'QUEUE\tERROR\t0\t0\t%s\n' "$max"
  exit 0
fi
pending=0
quarantine=0
[ -d "$q/pending" ] && pending=$(find "$q/pending" -maxdepth 1 -type f 2>/dev/null | wc -l)
[ -d "$q/quarantine" ] && quarantine=$(find "$q/quarantine" -maxdepth 1 -type f 2>/dev/null | wc -l)
printf 'QUEUE\tok\t%s\t%s\t%s\n' "$pending" "$quarantine" "$max"
REMOTE_E
}

# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------

check_services_and_adapter() {
  local out rc
  out=$(remote_services_adapter)
  rc=$?
  note_raw "dienste+adapter" "$out"
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    add_problem "SERVER_UNERREICHBAR: lacasa-server antwortet nicht (SSH fehlgeschlagen) - Dienste, Adapter und Pin konnten nicht geprueft werden"
    return 1
  fi

  # --- Dienste: jeder einzeln ---
  local kind unit active enabled
  while IFS=$'\t' read -r kind unit active enabled; do
    [ "$kind" = "UNIT" ] || continue
    if [ "$active" != "active" ]; then
      add_problem "DIENST_DOWN: ${unit} laeuft nicht (Zustand: ${active})"
    fi
    if [ "$enabled" != "enabled" ]; then
      add_problem "DIENST_NICHT_AKTIVIERT: ${unit} ist nicht aktiviert (Zustand: ${enabled}) - nach einem Neustart kommt er nicht von allein"
    fi
  done <<< "$(printf '%s\n' "$out" | /usr/bin/grep -a '^UNIT')"

  local adapter_line pin_line dist_line wheel_line
  adapter_line=$(printf '%s\n' "$out" | /usr/bin/grep -a '^ADAPTER' | /usr/bin/head -1)
  pin_line=$(printf '%s\n' "$out" | /usr/bin/grep -a '^PIN' | /usr/bin/head -1)
  dist_line=$(printf '%s\n' "$out" | /usr/bin/grep -a '^DIST' | /usr/bin/head -1)
  wheel_line=$(printf '%s\n' "$out" | /usr/bin/grep -a '^WHEEL' | /usr/bin/head -1)

  # --- Pin aus dem Repo ---
  local pin_version pin_sha pin_tools
  IFS=$'\t' read -r _ pin_version pin_sha pin_tools <<< "$pin_line"
  if [ -z "$pin_version" ] || [ -z "$pin_sha" ] || [ "$pin_version" = "ERROR" ]; then
    add_problem "PIN_UNPRUEFBAR: der Adapter-Pin in core/cannanas_client.py des Satelliten-Baums ist nicht lesbar"
    return 0
  fi
  if [ "$pin_tools" -ge 0 ] 2>/dev/null && [ "$pin_tools" != "$EXPECTED_TOOLS" ]; then
    add_problem "PIN_WERKZEUGZAHL_ABWEICHEND: der gepinnte Adapter zaehlt ${pin_tools} Werkzeuge, erwartet sind ${EXPECTED_TOOLS}"
  fi

  # --- Adapter ---
  local a_version a_sha a_pinned a_tools
  IFS=$'\t' read -r _ a_version a_sha a_pinned a_tools <<< "$adapter_line"
  if [ -z "$a_version" ] || [ "$a_version" = "ERROR" ]; then
    add_problem "ADAPTER_UNPRUEFBAR: der Adapter meldet sich nicht (${ADAPTER_CMD} --adapter-info lieferte keine lesbare Ausgabe)"
    return 0
  fi

  if [ "$a_version" != "$pin_version" ]; then
    add_problem "ADAPTER_VERALTET: installiert ist Version ${a_version}, gepinnt ist ${pin_version} - ein Adapter-Update steht aus"
  fi
  if [ -z "$a_sha" ]; then
    add_problem "ADAPTER_OHNE_ARTEFAKT: der Adapter meldet keinen Artefakt-Hash - die Sidecar release_info.json fehlt neben dem Entry-Point"
  elif [ "$a_sha" != "$pin_sha" ]; then
    add_problem "ADAPTER_ARTEFAKT_FALSCH: der Adapter meldet Version ${a_version}, aber sein Artefakt-Hash ${a_sha:0:12} weicht vom Pin ${pin_sha:0:12} ab - die Sidecar release_info.json neben dem Entry-Point ist veraltet; JARVIS verweigert deshalb alle Cannanas-Schreibvorgaenge"
  fi
  if [ "$a_pinned" != "true" ]; then
    add_problem "ADAPTER_UNGEPINNT: der Adapter meldet release_pinned=false - die ausgelieferte Version ist nicht fest verdrahtet"
  fi
  local expected_tools="$EXPECTED_TOOLS"
  if [ "$pin_tools" -ge 0 ] 2>/dev/null; then expected_tools="$pin_tools"; fi
  if [ -n "$a_tools" ] && [ "$a_tools" != "$expected_tools" ]; then
    add_problem "ADAPTER_WERKZEUGZAHL_ABWEICHEND: der Adapter meldet ${a_tools} Werkzeuge, gepinnt sind ${expected_tools}"
  fi

  # --- installierte Distribution ---
  local dist
  IFS=$'\t' read -r _ dist <<< "$dist_line"
  if [ -n "$dist" ] && [ "$dist" != "ERROR" ] && [ "$dist" != "keine" ]; then
    if [ "$dist" != "$pin_version" ]; then
      add_problem "ADAPTER_INSTALL_ABWEICHEND: installiert ist ${dist}, gepinnt ist ${pin_version}"
    fi
  fi

  # --- gepinntes Wheel auf dem Server? ---
  local wheel
  IFS=$'\t' read -r _ wheel <<< "$wheel_line"
  if [ "$wheel" = "nein" ]; then
    add_problem "ADAPTER_ARTIFAKT_FEHLT: das gepinnte Wheel (${pin_sha:0:12}) liegt nicht im dist-Ordner des Servers"
  fi
}

check_checkouts() {
  local out rc
  out=$(remote_git)
  rc=$?
  note_raw "git" "$out"
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    add_problem "CHECKOUT_UNPRUEFBAR: die Git-Zustaende auf lacasa-server liessen sich nicht lesen"
    return 1
  fi

  local kind label head track remote
  local sat_head="" sat_track="" sat_remote="" pc_head=""
  while IFS=$'\t' read -r kind label head track remote; do
    [ "$kind" = "GIT" ] || continue
    case "$label" in
      sat) sat_head="$head"; sat_track="$track"; sat_remote="$remote" ;;
      pc)  pc_head="$head" ;;
    esac
  done <<< "$out"

  if [ -z "$sat_head" ] || [ -z "$pc_head" ]; then
    add_problem "CHECKOUT_UNPRUEFBAR: mindestens einer der beiden Checkouts liess sich nicht aus"
    return 1
  fi
  if [ "$sat_head" = "ERROR" ] || [ "$pc_head" = "ERROR" ]; then
    add_problem "CHECKOUT_UNPRUEFBAR: mindestens einer der beiden Checkouts hat keinen lesbaren HEAD"
    return 1
  fi

  # Ziel-Commit: der lebende Remote-Branch, sonst der lokale Tracking-Ref.
  local target="$sat_remote"
  if [ "$target" = "unbekannt" ] || [ -z "$target" ]; then
    target="$sat_track"
  fi
  if [ "$target" = "ERROR" ] || [ -z "$target" ]; then
    add_problem "CHECKOUT_UNPRUEFBAR: der Remote-Branch ${REMOTE_BRANCH} liess sich nicht abfragen"
  else
    if [ "$sat_head" != "$target" ]; then
      add_problem "CHECKOUT_VERSCHIEBEN: der Satelliten-Baum steht auf ${sat_head:0:7}, ${REMOTE_BRANCH} steht auf ${target:0:7} - ein Update steht fuer den Satelliten aus"
    fi
    if [ "$pc_head" != "$target" ]; then
      add_problem "CHECKOUT_VERSCHIEBEN: der PC-JARVIS-Baum steht auf ${pc_head:0:7}, ${REMOTE_BRANCH} steht auf ${target:0:7} - ein Update steht fuer den PC-Baum aus"
    fi
  fi

  if [ "$sat_head" != "$pc_head" ]; then
    add_problem "CHECKOUT_UNTERSCHIEDLICH: Satellit und PC-JARVIS stehen auf verschiedenen Commits (${sat_head:0:7} und ${pc_head:0:7})"
  fi

  # git status: im Satelliten-Baum nichts, im PC-Baum hoechstens die zwei
  # bekannten lokalen Dateien.
  local statuses entry st_label st_line allowed known
  statuses=$(printf '%s\n' "$out" | /usr/bin/grep -a '^GITSTATUS')
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    st_label=$(printf '%s' "$entry" | /usr/bin/cut -f2)
    st_line=$(printf '%s' "$entry" | /usr/bin/cut -f3-)
    [ -n "$st_line" ] || continue
    if [ "$st_label" = "sat" ]; then
      add_problem "CHECKOUT_UNERWARTETE_AENDERUNG: der Satelliten-Baum ist nicht sauber (${st_line}) - erwartet ist ein sauberer Baum"
    else
      allowed="nein"
      for known in "${PC_ALLOWED_STATUS[@]}"; do
        if [ "$st_line" = "$known" ]; then allowed="ja"; break; fi
      done
      if [ "$allowed" != "ja" ]; then
        add_problem "CHECKOUT_UNERWARTETE_AENDERUNG: der PC-JARVIS-Baum enthaelt eine unerwartete Aenderung (${st_line}) - erlaubt sind hoechstens core/spotify.py und config/briefing-speakers.json"
      fi
    fi
  done <<< "$statuses"

  # Erwartete Dateien unveraendert? (Format: DATEIEN\t<baum>\t<zustand>)
  local files_kind files_label files_state
  while IFS=$'\t' read -r files_kind files_label files_state; do
    [ "$files_kind" = "DATEIEN" ] || continue
    if [ "$files_state" != "unveraendert" ]; then
      add_problem "CHECKOUT_DATEI_GEAENDERT: die erwarteten Dateien im $(baum_name "$files_label")-Baum weichen vom Commit ab"
    fi
  done <<< "$(printf '%s\n' "$out" | /usr/bin/grep -a '^DATEIEN')"

  # Quervergleich: sind die erwarteten Dateien in beiden Baeumen gleich?
  # (Format: QUER\t<datei>\t<zustand>) - drei Felder, drei Variablen:
  # "read" legt den Rest sonst in die letzte Variable.
  local quere_kind quere_datei quere_state
  while IFS=$'\t' read -r quere_kind quere_datei quere_state; do
    [ "$quere_kind" = "QUER" ] || continue
    [ -n "$quere_datei" ] || continue
    if [ "$quere_state" != "identisch" ]; then
      add_problem "CHECKOUT_DATEIEN_ABWEICHEND: ${quere_datei} unterscheidet sich zwischen Satellit und PC-JARVIS"
    fi
  done <<< "$(printf '%s\n' "$out" | /usr/bin/grep -a '^QUER')"
}

check_board() {
  local out rc
  out=$(remote_board)
  rc=$?
  note_raw "board" "$out"
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    add_problem "BOARD_UNERREICHBAR: das Satelliten-Board war nicht pruefbar - die Pruefung konnte nicht durchgefuehrt werden"
    return 1
  fi

  local dns tcp
  dns=$(printf '%s\n' "$out" | /usr/bin/grep -a '^DNS' | /usr/bin/head -1 | /usr/bin/cut -f2)
  tcp=$(printf '%s\n' "$out" | /usr/bin/grep -a '^TCP' | /usr/bin/head -1 | /usr/bin/cut -f2)
  if [ "$dns" != "ok" ]; then
    add_problem "BOARD_UNERREICHBAR: ${BOARD_HOST} laesst sich nicht aufloesen"
  fi
  if [ "$tcp" != "ok" ]; then
    add_problem "BOARD_UNERREICHBAR: TCP ${BOARD_PORT} auf ${BOARD_HOST} antwortet nicht"
  fi

  local proto proto_state proto_name proto_stamp
  proto=$(printf '%s\n' "$out" | /usr/bin/grep -a '^PROTO' | /usr/bin/head -1)
  IFS=$'\t' read -r _ proto_state proto_name proto_stamp <<< "$proto"
  if [ "$proto_state" != "ok" ]; then
    add_problem "BOARD_ANTWORTET_NICHT: das Board nimmt die Verbindung an, antwortet aber nicht auf das ESPHome-Protokoll (${proto_state})"
    return 1
  fi

  add_note "BOARD_OK: ${proto_name}, Firmware kompiliert am ${proto_stamp}"
  if [ -n "$FIRMWARE_MIN_DATE" ]; then
    local day="${proto_stamp:0:10}"
    if [ -n "$day" ] && [[ "$day" < "$FIRMWARE_MIN_DATE" ]]; then
      add_problem "FIRMWARE_VERALTET: das Board laeuft die Firmware vom ${day}, erwartet wird mindestens ${FIRMWARE_MIN_DATE} - der Flash braucht den Mac und ist nicht automatisierbar"
    fi
  fi
}

check_sensors() {
  local out rc
  out=$(remote_sensors)
  rc=$?
  note_raw "sensoren" "$out"
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    add_problem "SENSOR_UNPRUEFBAR: die Home-Assistant-Sensoren waren nicht lesbar - die Pruefung konnte nicht durchgefuehrt werden"
    return 1
  fi
  local state count expected missing
  IFS=$'\t' read -r _ state count expected missing <<< "$out"
  if [ "$state" != "ok" ]; then
    add_problem "SENSOR_UNPRUEFBAR: die Zonensensoren liessen sich nicht auslesen"
    return 1
  fi
  if ! printf '%s' "$count" | /usr/bin/grep -qE '^[0-9]+$'; then
    add_problem "SENSOR_UNPRUEFBAR: die Anzahl der Zonensensoren liess sich nicht bestimmen"
    return 1
  fi
  if [ "$count" -lt "$expected" ]; then
    add_problem "SENSOR_FEHLT: nur ${count} von ${expected} Zonensensoren liefern einen Temperaturwert (ohne Wert: ${missing:-keine Angabe})"
  else
    add_note "SENSOR_OK: ${count} von ${expected} Zonensensoren liefern Werte"
  fi
}

# Die Sprecherdatei traegt persoenliche Daten (Name, Aufgaben, Raum) und
# muss owner-only sein. Die Bridge verweigert sie sonst mit einem
# PermissionError - und das faellt nur im Log auf, nicht im Alltag.
check_briefing_permissions() {
  local out rc
  out=$(/usr/bin/timeout "$SSH_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" -T "$SERVER" \
    'for f in /home/lacasa/Mark-LV-LaCasa/config/briefing-speakers.json; do
       if [ -e "$f" ]; then stat -c "%a %U" "$f" 2>/dev/null || echo "FEHLT"; else echo "FEHLT"; fi
     done' 2>/dev/null)
  rc=$?
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    add_problem "BRIEFING_UNPRUEFBAR: die Rechte der Sprecherdatei liessen sich nicht pruefen"
    return 1
  fi
  local mode owner
  mode=$(printf '%s' "$out" | /usr/bin/head -1 | /usr/bin/awk '{print $1}')
  owner=$(printf '%s' "$out" | /usr/bin/head -1 | /usr/bin/awk '{print $2}')
  if [ "$mode" != "600" ]; then
    add_problem "BRIEFING_DATEI_OFFEN: die Sprecherdatei hat die Rechte ${mode:-?} statt 600 - die Bridge verweigert das Morgen-Briefing dann mit einem PermissionError (Fix: chmod 600)"
    return 1
  fi
  # Der Owner muss lacasa sein - der Benutzer, unter dem die Bridge laeuft.
  # Der Wächter selbst laeuft als root, deshalb ist der Vergleich mit
  # $(id -un) hier richtiglos.
  if [ -n "$owner" ] && [ "$owner" != "lacasa" ]; then
    add_problem "BRIEFING_DATEI_FREMDER_OWNER: die Sprecherdatei gehoert ${owner}, die Bridge laeuft als lacasa"
    return 1
  fi
  add_note "BRIEFING_OK: die Sprecherdatei ist owner-only (600)"
}

check_queue() {
  local out rc
  out=$(remote_queue)
  rc=$?
  note_raw "queue" "$out"
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    add_problem "QUEUE_UNPRUEFBAR: der Learning-Queue-Ordner war nicht pruefbar"
    return 1
  fi
  local state pending quarantine max
  IFS=$'\t' read -r _ state pending quarantine max <<< "$out"
  if [ "$state" != "ok" ]; then
    add_problem "QUEUE_UNPRUEFBAR: der Learning-Queue-Ordner fehlt oder liess sich nicht lesen"
    return 1
  fi
  if ! printf '%s%s' "$pending" "$quarantine" | /usr/bin/grep -qE '^[0-9]+$'; then
    add_problem "QUEUE_UNPRUEFBAR: die Anzahl der offenen Queue-Eintraege liess sich nicht bestimmen"
    return 1
  fi
  local total=$(( pending + quarantine ))
  if [ "$total" -gt "$max" ]; then
    add_problem "QUEUE_VOLLAEUFT: die Learning-Queue enthaelt mehr als ${max} offene Eintraege (pending und quarantine zusammen) - der Dispatcher haengt vermutlich"
  else
    add_note "QUEUE_OK: ${total} offene Eintraege (Grenze ${max})"
  fi
}

# ---------------------------------------------------------------------------
# Lauf
# ---------------------------------------------------------------------------
main() {
  check_services_and_adapter
  if ! deadline_reached; then check_checkouts; else add_problem "PRUEFUNG_ABGEBROCHEN: die Zeitgrenze des Laufs war erreicht - nicht alle Pruefungen liefen"; fi
  if ! deadline_reached; then check_board; else add_problem "PRUEFUNG_ABGEBROCHEN: die Zeitgrenze des Laufs war erreicht - nicht alle Pruefungen liefen"; fi
  if ! deadline_reached; then check_sensors; else add_problem "PRUEFUNG_ABGEBROCHEN: die Zeitgrenze des Laufs war erreicht - nicht alle Pruefungen liefen"; fi
  if ! deadline_reached; then check_briefing_permissions; else add_problem "PRUEFUNG_ABGEBROCHEN: die Zeitgrenze des Laufs war erreicht - nicht alle Pruefungen liefen"; fi
  if ! deadline_reached; then check_queue; else add_problem "PRUEFUNG_ABGEBROCHEN: die Zeitgrenze des Laufs war erreicht - nicht alle Pruefungen liefen"; fi

  local now new_state count
  now=$(/usr/bin/date '+%Y-%m-%d %H:%M:%S')
  new_state=$(state_hash)
  count="${#PROBLEMS[@]}"

  # Log schreiben (immer eine Zeile je Lauf, damit der Verlauf lesbar bleibt).
  {
    for note in "${NOTES[@]}"; do
      printf '[%s] %s\n' "$now" "$note"
    done
    for problem in "${PROBLEMS[@]}"; do
      printf '[%s] BEFUND %s\n' "$now" "$problem"
    done
    printf '[%s] STATE state=%s problems=%s\n' "$now" "$new_state" "$count"
  } >>"$LOG_FILE"
  /usr/bin/chmod 600 "$LOG_FILE" 2>/dev/null || true

  # Nur bei Zustandswechsel melden: ein Befund wird genau einmal gemeldet,
  # nicht alle 30 Minuten wiederholt. Genau das macht den Waechter still.
  if [ "$new_state" != "$PREV_STATE" ] && [ "$count" -gt 0 ]; then
    printf '%s\n' "${PROBLEMS[@]}"
  fi

  rotate_log_if_needed "$new_state" "$count"
  return 0
}

main
exit 0
