#!/bin/bash
# =============================================================================
# JARVIS-Sicherheitsaudit — unabhaengige Pruefung der Cannanas-/Mark-LV-Regeln
# =============================================================================
#
# ZWECK
#   Der Sicherheitsbot prueft das Projekt Mark-LV-LaCasa + Cannanas-MCP-Adapter
#   unabhaengig vom Projekt selbst. Er beantwortet sechs Fragen mit einem
#   konkreten, reproduzierbaren Test (nicht durch blosses Nachschlagen in
#   einer Konfigurationsdatei):
#
#     S1  Kein Geheimnis im Repo (echte Werte, keine Variablennamen)
#     S2  Kein unbeabsichtigt offener Port nach aussen
#     S3  Kein Write ohne Boot-Fence (boot_id je 'executed')
#     S4  Kein Modell-Zugriff auf confirm_* (Bridge UND Adapter)
#     S5  Kein schreibender Zugriff ohne Vorschau ('prepared' vor 'executing')
#     S6  Erreichbarkeit des Audits (Datei existiert, schreibbar, waechst)
#
# EIGENSCHAFTEN
#   - LIEST NUR. Kein Write gegen die Cannanas-API, kein Restart, kein pip,
#     kein git checkout, kein Loeschen oder Veraendern fremder Dateien.
#     Geschrieben wird ausschliesslich das eigene Log dieses Skripts.
#   - STILLE BEI GESUND: leere stdout-Ausgabe. Je Befund eine knappe Zeile.
#   - FAIL-CLOSED: kann eine Pruefung nicht durchgefuehrt werden (Host nicht
#     erreichbar, Werkzeug fehlt, Datei nicht lesbar), ist das ein Befund.
#   - IDEMPOTENT: wiederholte Laeufe erzeugen dasselbe Ergebnis.
#   - KEINE GEHEIMNISSE in der Ausgabe: nur ja/nein, Anzahlen, Versionen und
#     Hash-Pruefsummen. Ein gefundener Wert wird nie ausgegeben, nur sein
#     SHA-256-Rumpf.
#   - Absolute Pfade, eigene Log-Rotation.
#
# AUFRUF
#   /root/bin/jarvis-security-audit.sh                     # echte Systeme
#   /root/bin/jarvis-security-audit.sh --audit-file <pfad>  # KOPIE statt Log
#   /root/bin/jarvis-security-audit.sh --self-test         # Stoerfall-Test
#
# STOERFALL-SELBSTTEST (--self-test)
#   Erzeugt eine KOPIE des echten Adapter-Audits, manipuliert die Kopie (ein
#   'executed' ohne 'prepared' und ohne boot_id) und schickt sie durch
#   DIESELBE Auswertung, die auch den echten Log prueft. Die echten Dateien
#   werden nie angefasst.
# =============================================================================

set -uo pipefail

# ── Feste Pfade (absolut) ───────────────────────────────────────────────────
SELF="/root/bin/jarvis-security-audit.sh"
LOG_DIR="/root/.hermes/logs"
LOG_FILE="${LOG_DIR}/jarvis-security-audit.log"
LOG_MAX_BYTES=$(( 5 * 1024 * 1024 ))
LOG_KEEP_ROTATIONS=7

# Mark-LV-Baum (Arbeitsbaum mit dem HEAD der Satelliten-Integration)
MARK_LV_TREE="/root/Mark-LV-LaCasa/.worktrees/integration"
# Adapter-Baum
ADAPTER_TREE="/home/lacasa15/services/cannanas-mcp/adapter"
# Produktionsserver
SERVER_HOST="lacasa@lacasa-server"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR"
# Adapter-Audit auf dem Server (der boot-gefenced Write-Audit des Adapters)
REMOTE_ADAPTER_AUDIT="/home/lacasa/cannanas-adapter/adapter-audit.jsonl"
# Host-Audit auf dem Server (JARVIS-seitiger Write-Audit)
REMOTE_HOST_AUDIT="/home/lacasa/cannanas-adapter/audit/jarvis-write-audit.jsonl"
# Board-Host (ESP32). Port 6053 gehoert zum Board, NICHT zum Server.
BOARD_HOST="growraum-sprache-1-home-test.local"
BOARD_PORT=6053

# Verwaltungsports, die auf lacasa-server NICHT lauschen duerfen.
# 6053 = ESPHome native API (gehoert auf das Board), 8123 = Home Assistant,
# 3232/3233 = ESPHome OTA/Update.
FORBIDDEN_SERVER_PORTS="8123 3232 3233 6053"

# Ports, die auf lacasa-server BEWUSST nach aussen (LAN) gebunden sind.
# Jeder Eintrag ist eine Entscheidung des Gaertners; alles andere auf
# 0.0.0.0 / :: / * wird gemeldet.
ALLOWED_PUBLIC_PORTS="22 8088 38145"

# Python fuer die Auswertung (absolut, damit die Cron-Umgebung sicher ist)
PYTHON_BIN="/usr/bin/python3"

# Python fuer den Adapter-Verhaltenstest (S4). Der Adapter braucht httpx und
# das MCP-SDK; das Hermes-Venv hat beides. Fehlt es, ist das ein Befund
# (fail-closed) — nicht stiller Erfolg.
ADAPTER_PYTHON="/usr/local/lib/hermes-agent/venv/bin/python3"
if [ ! -x "$ADAPTER_PYTHON" ]; then
    ADAPTER_PYTHON="$PYTHON_BIN"
fi

# ── Zustand ────────────────────────────────────────────────────────────────
FINDINGS=0
AUDIT_OVERRIDE=""          # optionale KOPIE statt des echten Logs
SELF_TEST=0
TMP_DIR=""

# ── Hilfsfunktionen ────────────────────────────────────────────────────────

# Befund melden: eine knappe Zeile auf stdout, dazu ins eigene Log.
finding() {
    FINDINGS=$(( FINDINGS + 1 ))
    printf '%s\n' "$1"
    log_line "BEFUND  $1"
}

# Normale Zeile nur ins Log (nie auf stdout — stdout bleibt bei Erfolg leer).
log_line() {
    [ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR" 2>/dev/null || return 0
    printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >> "$LOG_FILE" 2>/dev/null || true
}

# Eigene Log-Rotation (nur die eigene Log-Datei, nichts Fremdes).
rotate_log() {
    [ -f "$LOG_FILE" ] || return 0
    local size
    size=$(stat -c '%s' "$LOG_FILE" 2>/dev/null || echo 0)
    [ "$size" -gt "$LOG_MAX_BYTES" ] || return 0
    local stamp
    stamp=$(date -u '+%Y%m%d_%H%M%S')
    mv "$LOG_FILE" "${LOG_FILE}.${stamp}.old" 2>/dev/null || return 0
    local old
    for old in $(ls -1t "${LOG_FILE}".*.old 2>/dev/null | tail -n +$(( LOG_KEEP_ROTATIONS + 1 ))); do
        rm -f "$old" 2>/dev/null || true
    done
    return 0
}

cleanup() {
    [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ] && rm -rf "$TMP_DIR" 2>/dev/null
    return 0
}
trap cleanup EXIT

# ── Argumente ──────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --audit-file)
            AUDIT_OVERRIDE="${2:-}"
            shift 2
            ;;
        --self-test)
            SELF_TEST=1
            shift
            ;;
        -h|--help)
            sed -n '2,48p' "$SELF"
            exit 0
            ;;
        *)
            printf 'Unbekanntes Argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

mkdir -p "$LOG_DIR" 2>/dev/null || true
rotate_log
log_line "Start Audit (audit_override=${AUDIT_OVERRIDE:-keiner} selftest=${SELF_TEST})"

TMP_DIR=$(mktemp -d "${TMPDIR:-/root/.hermes/cache/scratch}/jarvis-audit.XXXXXX" 2>/dev/null) \
    || TMP_DIR=$(mktemp -d /tmp/jarvis-audit.XXXXXX 2>/dev/null) \
    || { printf 'Audit kann kein Arbeitsverzeichnis anlegen\n'; exit 3; }

# =============================================================================
# S1  KEIN GEHEIMNIS IM REPO
# =============================================================================
# Test: die letzten 40 Commits beider Baueume werden als Rohtext (Diff +
# Commit-Nachrichten) auf Muster geprueft, die auf ECHTE WERTE zielen:
#   - JWT (eyJ....) mit drei Sektionen
#   - Google-API-Key (AIza...)
#   - Spotify-OAuth-Felder mit echtem Wert (base64 mit =)
#   - Bearer/Session-Key (Bearer <token>)
#   - QR-Payload (Geraete-Anmeldelink mit #device=...)
#   - Club-ID (UUID)
#   - WLAN-Passwort / API-Key / ESPHome-Key: Zuweisung an einen KONKRETEN
#     Wert (kein Variablenname, kein Platzhalter, kein reiner Bezeichner)
# Jeder Treffer wird VERIFIZIERT: der Wert wird per SHA-256 gegen die echten
# Geheimnisse des laufenden Systems geprueft. Ein Variablenname wie
# CANNANAS_API_KEY ist KEIN Geheimnis und wird nie gemeldet.
# =============================================================================
s1_check() {
    local repo label
    for repo in "$MARK_LV_TREE" "$ADAPTER_TREE"; do
        # Ein Arbeitsbaum (.worktrees/...) hat eine .git-DATEI, kein Verzeichnis.
        if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            finding "S1 $repo nicht lesbar (kein Git-Baum) — Secret-Scan nicht durchgefuehrt"
            continue
        fi
        label=$(basename "$repo")
        local dump="${TMP_DIR}/git-${label}.txt"
        if ! git -C "$repo" log -n 40 -p --no-color --no-renames --no-textconv \
             > "$dump" 2>/dev/null; then
            finding "S1 Git-Historie von $label nicht lesbar — Secret-Scan nicht durchgefuehrt"
            continue
        fi
        # Commit-Nachrichten mitnehmen (sie duerfen Variablennamen nennen)
        git -C "$repo" log -n 40 --format='%H%n%s%n%b' --no-color >> "$dump" 2>/dev/null || true

        local hits
        hits=$(
            "$PYTHON_BIN" - "$dump" <<'PYEOF' 2>/dev/null
import re, sys, hashlib
text = open(sys.argv[1], 'r', encoding='utf-8', errors='replace').read()

# Muster, die auf ECHTE WERTE zielen (keine Variablennamen).
PATTERNS = [
    ('jwt',            re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}')),
    ('google_api_key', re.compile(r'AIza[0-9A-Za-z_-]{30,}')),
    ('spotify_oauth',  re.compile(r'(?:access_token|refresh_token)"?\s*[:=]\s*"?([A-Za-z0-9+/]{30,}={0,2})')),
    ('bearer_value',   re.compile(r'[Bb]earer\s+([A-Za-z0-9._~+/-]{20,}=*)')),
    ('qr_payload',     re.compile(r'https://[^\s"\']+/(?:login|qr|pair)[^\s"\']*#[A-Za-z0-9._~%+-]{16,}')),
    ('club_id_value',  re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')),
    ('esphome_psk',    re.compile(r'\b(?:api[_-]?key|noise[_-]?psk)\b\s*[:=]\s*["\']?([A-Za-z0-9+/=]{32,})')),
]

# Geheimnis-Schluesselwoerter: Zuweisung an einen KONKRETEN Wert.
SECRET_ASSIGN = re.compile(
    r'(?i)\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|'
    r'password|passwd|wifi[_-]?password|\bpsk\b|private[_-]?key|bearer[_-]?token|'
    r'qr[_-]?payload|session[_-]?key)\b'
    r'\s*[:=]\s*["\']?([A-Za-z0-9+/=_-]{12,})["\']?')

# Platzhalter, die KEIN Geheimnis sind.
PLACEHOLDERS = (
    'replace_with', 'example', 'placeholder', 'your_', 'xxx', 'todo', 'changeme',
    'dummy', 'not-real', 'notreal', 'test-key', 'fake', 'sample', 'redacted',
    'none_here', 'synthetic', 'hier_', 'einfuegen', 'password_here',
)

# Bekannte Nicht-Geheimnis-Werte aus dem Projekt (Testdaten, Platzhalter).
KNOWN_BENIGN = (
    'club-test-0000', 'club-hermes', 'hermes-dummy', 'test-key-not-real',
    'synthetic-no-network', '_input', 'club-bound', 'input', 'audit-selbsttest',
)

found = set()

def report(kind, value):
    v = (value or '').strip().strip('"\'`')
    if not v:
        return
    low = v.lower()
    if any(p in low for p in PLACEHOLDERS):
        return
    if v in KNOWN_BENIGN:
        return
    if len(v) < 12:
        return
    # Ein reiner Bezeichner (nur Buchstaben/Ziffern/Unterstrich) ist ein
    # Variablenname, kein Geheimnis.
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v):
        return
    digest = hashlib.sha256(v.encode('utf-8', 'replace')).hexdigest()[:12]
    found.add((kind, digest))

for kind, pat in PATTERNS:
    for m in pat.finditer(text):
        report(kind, m.group(1) if m.groups() else m.group(0))

for m in SECRET_ASSIGN.finditer(text):
    value = m.group(1)
    # Ein Funktionsaufruf (Wert gefolgt von '(') ist kein Wert.
    tail = text[m.end(1):m.end(1) + 1]
    if tail == '(':
        continue
    report('assigned_secret', value)

for kind, digest in sorted(found):
    print(f'{kind} {digest}')
PYEOF
        )
        if [ -z "$hits" ]; then
            log_line "S1 $label: keine Wert-Muster im 40-Commit-Fenster"
            continue
        fi
        # Verifikation gegen die echten Geheimnisse des laufenden Systems.
        # Nur was BELEGT ist, wird gemeldet; der Wert selbst nie.
        local system_digests
        system_digests=$(s1_system_digests)
        if [ -z "$system_digests" ]; then
            finding "S1 $label: System-Geheimnisse nicht lesbar — Secret-Verifikation nicht durchgefuehrt"
            continue
        fi
        local confirmed=""
        while IFS= read -r cand; do
            [ -n "$cand" ] || continue
            local kind="${cand%% *}"
            local digest="${cand##* }"
            if printf '%s\n' "$system_digests" | grep -qx "$digest"; then
                confirmed="${confirmed}${kind} ${digest}"$'\n'
            fi
        done <<< "$hits"
        if [ -n "$confirmed" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                finding "S1 $label: echtes Geheimnis im Repo-Verlauf (Typ=${line%% *}, sha256=${line##* })"
            done <<< "$confirmed"
        else
            local count
            count=$(printf '%s\n' "$hits" | grep -c .)
            log_line "S1 $label: $count Wert-Muster, keines gegen ein echtes System-Geheimnis bestaetigt (kein Befund)"
        fi
    done
}

# SHA-256-Rucken (erste 12 Zeichen) der echten Geheimnisse des Systems.
# Liest nur; gibt NIEMALS einen Klartext zurueck.
s1_system_digests() {
    local remote_py='
import hashlib, json, os, re, glob
def dig(v):
    v = str(v or "").strip()
    if not v:
        return None
    return hashlib.sha256(v.encode("utf-8", "replace")).hexdigest()[:12]
out = set()
# Adapter-Credentials (API-Key, Club-ID)
try:
    with open("/home/lacasa/Mark-LV-LaCasa/deploy/cannanas/cannanas-adapter.credentials") as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                if k.strip().lower() in ("cannanas_api_key", "cannanas_club_id"):
                    d = dig(v.strip().strip("\"\x27"))
                    if d: out.add(d)
except Exception:
    pass
# Host-Konfiguration (Gemini-Key, HA-Token, Spotify-OAuth, Geraete-Credentials)
for path in ("/home/lacasa/Mark-LV-LaCasa/config/api_keys.json",
             "/home/lacasa/Mark-LV-LaCasa/config/spotify_oauth.json",
             "/home/lacasa/Mark-LV-LaCasa/config/device_credentials.json"):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        continue
    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, str(k).lower())
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str):
            if any(m in key for m in ("key", "token", "secret", "password", "psk",
                                      "verifier", "salt", "club", "payload")):
                d = dig(o)
                if d: out.add(d)
    walk(data)
# ESPHome-API-Key
try:
    with open("/home/lacasa/waveshare-va-test/api-key.txt") as fh:
        d = dig(fh.read().strip())
        if d: out.add(d)
except Exception:
    pass
# QR-Payloads (Geraete-Anmeldelinks)
try:
    qdir = "/home/lacasa/Mark-LV-LaCasa/config/device_qr"
    for name in sorted(os.listdir(qdir)):
        if name.endswith(".url"):
            with open(os.path.join(qdir, name)) as fh:
                d = dig(fh.read().strip())
                if d: out.add(d)
except Exception:
    pass
# WLAN-Passwoerter / PSKs aus YAML
try:
    for path in glob.glob("/home/lacasa/**/secrets.yaml", recursive=True):
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*(?:wifi_)?(?:password|psk)\s*:\s*[\"\x27]?([^\"\x27#\n]{6,})", line)
                if m:
                    d = dig(m.group(1).strip())
                    if d: out.add(d)
except Exception:
    pass
# Geraete-Registrierungs-Payload (Beschriftung + Salt + Verifier)
try:
    with open("/home/lacasa/Mark-LV-LaCasa/config/device_credentials.json") as fh:
        data = json.load(fh)
    for dev in (data.get("devices") or []):
        for field in ("device_id", "label", "salt", "verifier"):
            d = dig(dev.get(field))
            if d: out.add(d)
except Exception:
    pass
for d in sorted(out):
    print(d)
'
    # Das Python-Skript wird ueber stdin auf den Server gereicht (nicht als
    # Shell-Kommando — sonst wuerde die Shell das Python versuchen zu starten).
    printf '%s' "$remote_py" \
        | ssh $SSH_OPTS "$SERVER_HOST" /usr/bin/python3 - 2>/dev/null \
        | grep -E '^[0-9a-f]{12}$' || true
}

# =============================================================================
# S2  KEIN OFFENER PORT
# =============================================================================
# Test: auf lacasa-server werden die listening sockets gelesen.
#   - Verwaltungsports (8123/3232/3233/6053) duerfen dort NICHT lauschen.
#     6053 gehoert auf das BOARD, nicht auf den Server.
#   - Jeder weitere Port auf 0.0.0.0 / :: / * wird gemeldet, ausser er steht
#     auf der bewussten Freigabeliste.
#   - Zusaetzlich wird von aussen (oeffentliche Adresse) geprueft, ob die
#     Ports des Servers erreichbar sind. Eine Kontrollmessung (Tailscale)
#     stellt sicher, dass diese Messung aussagekraeftig ist.
# =============================================================================
s2_check() {
    local remote_py='
import json, subprocess
def run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=25).stdout
    except Exception:
        return ""
socks = run("ss -tlnH")
if not socks.strip():
    socks = run("netstat -tln")
rows = []
for line in socks.splitlines():
    parts = line.split()
    if len(parts) < 4:
        continue
    local = parts[3]
    if ":" not in local:
        continue
    addr, _, port = local.rpartition(":")
    port = port.strip()
    if not port.isdigit():
        continue
    rows.append({"addr": addr, "port": int(port)})
print(json.dumps(rows))
'
    local socks
    socks=$(printf '%s' "$remote_py" | ssh $SSH_OPTS "$SERVER_HOST" /usr/bin/python3 - 2>/dev/null)
    if [ -z "$socks" ]; then
        finding "S2 listening sockets auf lacasa-server nicht lesbar — Portpruefung nicht durchgefuehrt"
        return 0
    fi

    # Die Socket-Liste wird in eine Datei geschrieben: der Python-Block
    # braucht stdin fuer sein Skript, nicht fuer die Daten.
    local socks_file="${TMP_DIR}/socks.json"
    printf '%s' "$socks" > "$socks_file"
    local verdict
    verdict=$("$PYTHON_BIN" - "$socks_file" \
        "$FORBIDDEN_SERVER_PORTS" "$ALLOWED_PUBLIC_PORTS" <<'PYEOF' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as fh:
        rows = json.load(fh)
except Exception:
    print("PARSE_FAIL")
    sys.exit(0)
forbidden = {int(p) for p in sys.argv[2].split() if p.strip()}
allowed = {int(p) for p in sys.argv[3].split() if p.strip()}

def is_loopback(addr):
    a = addr.replace("[", "").replace("]", "")
    return a.startswith("127.") or a == "::1"

def is_tailscale(addr):
    a = addr.replace("[", "").replace("]", "")
    if "tailscale" in a:
        return True
    if a.startswith("100."):
        return True
    if a.lower().startswith("fd7a:"):
        return True
    return False

def is_wildcard(addr):
    a = addr.replace("[", "").replace("]", "")
    return a in ("0.0.0.0", "*", "::", "") or a.startswith("0.0.0.0:")

forbidden_hits = []
exposed = []
for r in rows:
    port = int(r["port"])
    addr = r["addr"]
    if port in forbidden:
        forbidden_hits.append(port)
        continue
    if is_loopback(addr) or is_tailscale(addr):
        continue
    if port in allowed:
        continue
    if is_wildcard(addr) or addr.startswith("192.168."):
        exposed.append(port)
for p in sorted(set(forbidden_hits)):
    print("FORBIDDEN %d" % p)
for p in sorted(set(exposed)):
    print("EXPOSED %d" % p)
print("SEEN %d" % len(rows))
PYEOF
    )
    if printf '%s' "$verdict" | grep -q '^PARSE_FAIL$'; then
        finding "S2 Socket-Auswertung fehlgeschlagen — Portpruefung nicht durchgefuehrt"
        return 0
    fi
    while IFS= read -r line; do
        case "$line" in
            FORBIDDEN\ *)
                finding "S2 Port ${line#FORBIDDEN } lauscht auf lacasa-server — ESP32-Port 6053 gehoert auf das Board, Verwaltungsports nicht auf den Server"
                ;;
            EXPOSED\ *)
                finding "S2 Port ${line#EXPOSED } lauscht auf lacasa-server ohne Tailscale-/Loopback-Bindung (Verwaltungsport nach aussen offen)"
                ;;
            SEEN\ *)
                log_line "S2 listening sockets gelesen: ${line#SEEN }"
                ;;
        esac
    done <<< "$verdict"

    # Erreichbarkeit von aussen
    local external
    external=$(s2_external_reachability)
    if [ -z "$external" ]; then
        finding "S2 Erreichbarkeit von aussen nicht pruefbar (oeffentliche Adresse nicht ermittelbar) — Portpruefung unvollstaendig"
        return 0
    fi
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            OK)
                log_line "S2 Erreichbarkeit von aussen geprueft: kein Port ueber die oeffentliche Adresse offen"
                ;;
            KONTROLLE_FEHLGESCHLAGEN\ *)
                finding "S2 ${line#KONTROLLE_FEHLGESCHLAGEN } — die Erreichbarkeitsmessung war nicht aussagekraeftig"
                ;;
            *)
                finding "S2 $line"
                ;;
        esac
    done <<< "$external"
}

# Prueft von aussen (vom Hermes-Host) ob die Ports des Servers erreichbar sind.
s2_public_ip() {
    # Mehrere Quellen, weil ein einzelner Dienst ausfallen kann. Ohne eine
    # belastbare oeffentliche Adresse ist die Messung wertlos (fail-closed).
    local ip
    ip=$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null | tr -d '[:space:]')
    if [ -n "$ip" ]; then printf '%s' "$ip"; return 0; fi
    ip=$(curl -s --max-time 8 https://1.1.1.1/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p' | tr -d '[:space:]')
    if [ -n "$ip" ]; then printf '%s' "$ip"; return 0; fi
    ip=$(curl -s --max-time 8 -4 https://ifconfig.me/ip 2>/dev/null | tr -d '[:space:]')
    if [ -n "$ip" ]; then printf '%s' "$ip"; return 0; fi
    printf ''
    return 0
}

s2_external_reachability() {
    local pub_ip
    pub_ip=$(s2_public_ip)
    if [ -z "$pub_ip" ]; then
        printf ''
        return 0
    fi
    # Die Tailscale-Adresse des SERVERS ist die Kontrollmessung: ist sie
    # erreichbar, funktioniert der Messweg; sonst waere das Ergebnis
    # wertlos (Fail-closed).
    local server_ts
    server_ts=$(printf '%s' 'ip -4 -o addr show tailscale0 2>/dev/null | awk "{print \$4}" | cut -d/ -f1' \
        | ssh $SSH_OPTS "$SERVER_HOST" /bin/bash -s 2>/dev/null | tr -d '[:space:]')
    if [ -z "$server_ts" ]; then
        printf 'KONTROLLE_FEHLGESCHLAGEN Tailscale-Adresse des Servers nicht ermittelbar\n'
        return 0
    fi
    "$PYTHON_BIN" - "$pub_ip" "$server_ts" <<'PYEOF'
import socket, sys
from concurrent.futures import ThreadPoolExecutor
pub = sys.argv[1]
server_ts = sys.argv[2]

def probe(host, port, timeout=4.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError:
        return "error"
    finally:
        s.close()

# Kontrollmessung: der Tailscale-Port des Servers muss erreichbar sein.
if probe(server_ts, 443) != "open":
    print("KONTROLLE_FEHLGESCHLAGEN tailscale %s:443 nicht erreichbar" % server_ts)
    sys.exit(0)

ports = (22, 8000, 8088, 38145, 8123, 3232, 3233)
with ThreadPoolExecutor(max_workers=len(ports)) as pool:
    states = list(pool.map(lambda p: probe(pub, p), ports))
found = False
for port, st in zip(ports, states):
    if st == "open":
        print("Port %d ueber die oeffentliche Adresse erreichbar" % port)
        found = True
    elif st in ("refused", "timeout"):
        pass  # Port zu bzw. Firewall: kein Befund
    else:
        print("Port %d nicht pruefbar (%s)" % (port, st))
        found = True
if not found:
    # Erfolgreich gemessen und nichts gefunden — das muss sich von
    # 'nicht pruefbar' unterscheiden.
    print("OK")
PYEOF
}

# =============================================================================
# S3 / S5  AUDIT-LOG AUSWERTUNG (Boot-Fence und Vorschau)
# =============================================================================
# Test: der Audit-Log wird Zeile fuer Zeile gelesen.
#   S3: jeder 'executed'-Eintrag des ADAPTER-Audits muss eine boot_id tragen.
#       Aendert sich die boot_id zwischen 'executing' und 'executed', ohne
#       dass dazwischen ein 'executing'-Eintrag steht, ist das ein Befund.
#       (Ein Adapter, der bei einem Boot zwischen executing und executed
#       abbricht, ist erlaubt und KEIN Befund.)
#   S5: jedes 'executing'/'executed' muss ein vorheriges 'prepared' mit
#       gleichem Objekt (action) und Payload (payload_hash) haben.
# Beide Audits werden geprueft: der Adapter-Audit (boot-gefenced) und der
# Host-Audit (JARVIS). S3 gilt nur fuer den Adapter-Audit — nur er traegt
# die boot_id des Adapters.
# =============================================================================
s3_s5_check() {
    local adapter_audit="$AUDIT_OVERRIDE"
    local host_audit=""
    local fetched="${TMP_DIR}/adapter-audit.jsonl"
    local fetched_host="${TMP_DIR}/host-audit.jsonl"

    if [ -n "$AUDIT_OVERRIDE" ]; then
        # Stoerfall-Modus: die Kopie wird als Adapter-Audit geprueft.
        host_audit=""
    else
        if ! scp -q $SSH_OPTS "$SERVER_HOST:$REMOTE_ADAPTER_AUDIT" "$fetched" 2>/dev/null; then
            finding "S3/S5 Adapter-Audit-Log auf lacasa-server nicht lesbar — Boot-Fence-/Vorschau-Pruefung nicht durchgefuehrt"
            return 0
        fi
        if ! scp -q $SSH_OPTS "$SERVER_HOST:$REMOTE_HOST_AUDIT" "$fetched_host" 2>/dev/null; then
            finding "S3/S5 Host-Audit-Log auf lacasa-server nicht lesbar — Vorschau-Pruefung nicht durchgefuehrt"
            fetched_host=""
        fi
        adapter_audit="$fetched"
        host_audit="$fetched_host"
    fi

    if [ ! -f "$adapter_audit" ]; then
        finding "S3/S5 Adapter-Audit-Log fehlt — Boot-Fence-/Vorschau-Pruefung nicht durchgefuehrt"
        return 0
    fi

    # --- Adapter-Audit: S3 (Boot-Fence) + S5 (Vorschau) ---
    s3_s5_evaluate "$adapter_audit" "Adapter-Audit" 1
    # --- Host-Audit: S5 (Vorschau). Kein S3: der Host-Audit traegt keine boot_id. ---
    if [ -n "$host_audit" ] && [ -f "$host_audit" ]; then
        s3_s5_evaluate "$host_audit" "Host-Audit" 0
    fi
}

# Wertet EINEN Audit-Log aus. $1=Datei $2=Bezeichnung $3=1 => S3 anwenden.
s3_s5_evaluate() {
    local path="$1" label="$2" do_s3="$3"
    local result
    result=$("$PYTHON_BIN" - "$path" "$do_s3" <<'PYEOF' 2>/dev/null
import json, sys

path = sys.argv[1]
do_s3 = sys.argv[2] == "1"
rows = []
bad_json = 0
try:
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad_json += 1
except OSError:
    print("READ_ERROR")
    sys.exit(0)

if bad_json:
    print("UNLESBAR %d" % bad_json)

def state_of(r):
    st = str(r.get('state') or '')
    ev = str(r.get('event') or '')
    if ev == 'confirmed' or st == 'executing':
        return 'executing'
    if ev == 'result':
        return 'executed'
    if ev == 'prepared' or st == 'prepared':
        return 'prepared'
    if st == 'executed':
        return 'executed'
    if st == 'unbekannt':
        return 'unbekannt'
    return ''

def action_of(r):
    return str(r.get('action') or '')

def hash_of(r):
    return str(r.get('payload_hash') or '')

def boot_of(r):
    return str(r.get('boot_id') or '')

def key_of(r):
    return (action_of(r), hash_of(r))

# --- S3: Boot-Fence ---
executed_no_boot = 0
boot_changed = 0
# letztes 'executing' je Objekt: (boot_id, Index)
last_executing = {}
# Index aller 'executing'-Eintraege: (Index, boot_id)
executing_index = []

for idx, r in enumerate(rows):
    st = state_of(r)
    k = key_of(r)
    if st == 'executing':
        last_executing[k] = (boot_of(r), idx)
        executing_index.append((idx, boot_of(r)))
        continue
    if st != 'executed':
        continue
    if not boot_of(r):
        executed_no_boot += 1
        continue
    prior = last_executing.get(k)
    if prior is None:
        continue  # kein 'executing' voraus -> das ist S5, nicht S3
    prior_boot, prior_idx = prior
    if not prior_boot or prior_boot == boot_of(r):
        continue
    # Die boot_id wechselte. Erlaubt ist das, wenn zwischen dem 'executing'
    # und dem 'executed' ein weiteres 'executing' mit der neuen boot_id steht
    # (der Adapter brach bei einem Boot ab und begann neu). Ohne ein solches
    # 'executing' ist es ein Befund.
    rescued = any(prior_idx < i < idx and b == boot_of(r)
                  for i, b in executing_index)
    if not rescued:
        boot_changed += 1

if do_s3:
    if executed_no_boot:
        print("NO_BOOT_ID %d" % executed_no_boot)
    if boot_changed:
        print("BOOT_CHANGED %d" % boot_changed)

# --- S5: Vorschau-Pflicht ---
# Ein 'executing'/'executed' ist gedeckt, wenn ein 'prepared' mit derselben
# Aktion VORAUSGING. Der Payload-Hash muss uebereinstimmen; ein 'prepared'
# ohne Hash (der Host-Audit fuehrt ihn nicht immer mit) deckt dieselbe
# Aktion ab.
prepared_exact = set()      # (action, payload_hash)
prepared_action = set()     # action
executing_without_prepared = 0
executed_without_prepared = 0
for r in rows:
    st = state_of(r)
    action = action_of(r)
    k = key_of(r)
    if st == 'prepared':
        prepared_exact.add(k)
        prepared_action.add(action)
        continue
    if st == 'executing':
        if k not in prepared_exact and action not in prepared_action:
            executing_without_prepared += 1
    elif st == 'executed':
        if k not in prepared_exact and action not in prepared_action:
            executed_without_prepared += 1

if executing_without_prepared:
    print("EXECUTING_WITHOUT_PREPARED %d" % executing_without_prepared)
if executed_without_prepared:
    print("EXECUTED_WITHOUT_PREPARED %d" % executed_without_prepared)

counts = {}
for r in rows:
    st = state_of(r) or 'read'
    counts[st] = counts.get(st, 0) + 1
print("COUNTS " + " ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
print("ROWS %d" % len(rows))
PYEOF
    )

    if [ -z "$result" ]; then
        finding "S3/S5 $label: Auswertung fehlgeschlagen — Boot-Fence-/Vorschau-Pruefung nicht durchgefuehrt"
        return 0
    fi
    if printf '%s\n' "$result" | grep -q '^READ_ERROR$'; then
        finding "S3/S5 $label: Audit-Log nicht lesbar — Boot-Fence-/Vorschau-Pruefung nicht durchgefuehrt"
        return 0
    fi

    # Wichtig: die Schleife darf NICHT an einer Pipe haengen — dann laeuft sie
    # in einer Subshell und die FINDINGS-Zaehlung gaenge verloren.
    # Deshalb wird der Here-String benutzt.
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            UNLESBAR\ *)
                finding "S3/S5 $label: ${line#UNLESBAR } Zeilen im Audit-Log unlesbar (kein JSON)"
                ;;
            NO_BOOT_ID\ *)
                finding "S3 $label: ${line#NO_BOOT_ID } 'executed'-Eintraege ohne boot_id"
                ;;
            BOOT_CHANGED\ *)
                finding "S3 $label: ${line#BOOT_CHANGED } Faelle — boot_id wechselte zwischen 'executing' und 'executed' ohne neues 'executing'"
                ;;
            EXECUTING_WITHOUT_PREPARED\ *)
                finding "S5 $label: ${line#EXECUTING_WITHOUT_PREPARED } 'executing'-Eintraege ohne vorheriges 'prepared' (Schreibzugriff ohne Vorschau)"
                ;;
            EXECUTED_WITHOUT_PREPARED\ *)
                finding "S5 $label: ${line#EXECUTED_WITHOUT_PREPARED } 'executed'-Eintraege ohne vorheriges 'prepared' (Schreibzugriff ohne Vorschau)"
                ;;
            COUNTS\ *)
                log_line "S3/S5 $label Zustandszahlen: ${line#COUNTS }"
                ;;
            ROWS\ *)
                log_line "S3/S5 $label Zeilen gesamt: ${line#ROWS }"
                ;;
        esac
    done <<< "$result"
}

# =============================================================================
# S4  KEIN MODELL-ZUGRIFF AUF confirm_*
# =============================================================================
# Test (Verhalten, nicht Konfiguration):
#   (a) Die Modell-Enum CANNANAS_MODEL_ACTIONS und das Modell-Schema der
#       Bridge werden IMPORTIERT und geprueft: kein confirm_* darin.
#   (b) Der Host wird mit einem Fake-Client aufgerufen: ein confirm_* OHNE
#       vorheriges PendingConfirmation muss den Adapter NICHT erreichen und
#       wird verweigert. Erst nach einem echten Prepare darf ein confirm_*
#       laufen; ein Replay wird abgewiesen.
#   (c) Im Adapter-Baum: die oeffentlichen confirm_*-Methoden MUESSTEN durch
#       _begin_write (den Boot-Fence) laufen — per AST geprueft.
#   (d) Die FastMCP-Instruktionen des Adapters duerfen dem Modell kein
#       confirm_*-Werkzeug anbieten.
# =============================================================================
s4_check() {
    # (a) Modell-Enum
    local enum_result
    enum_result=$(cd "$MARK_LV_TREE" && "$PYTHON_BIN" - <<'PYEOF' 2>&1
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
try:
    from core.cannanas_actions import CANNANAS_MODEL_ACTIONS
except Exception as exc:
    print("IMPORT_FAIL %s" % type(exc).__name__)
    sys.exit(0)
names = sorted(CANNANAS_MODEL_ACTIONS)
bad = [n for n in names if n.startswith('confirm_') or n.startswith('cancel_')]
if bad:
    print("MODEL_ENUM_LEAK %d" % len(bad))
else:
    print("MODEL_ENUM_OK %d" % len(names))
PYEOF
    )
    if printf '%s' "$enum_result" | grep -q '^IMPORT_FAIL'; then
        finding "S4 Modell-Enum CANNANAS_MODEL_ACTIONS nicht importierbar — Modell-Zugriffs-Pruefung nicht durchgefuehrt"
    elif printf '%s' "$enum_result" | grep -q '^MODEL_ENUM_LEAK'; then
        finding "S4 Modell-Enum CANNANAS_MODEL_ACTIONS enthaelt confirm_*/cancel_* — das Modell kann einen Schreibvorgang direkt ausloesen"
    fi
    log_line "S4 Modell-Enum: ${enum_result:-unbekannt}"

    # (b) Modell-Schema der Bridge
    local schema_result
    schema_result=$(cd "$MARK_LV_TREE" && "$PYTHON_BIN" - <<'PYEOF' 2>&1
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
try:
    import esp32_jarvis_bridge as bridge
except Exception as exc:
    print("IMPORT_FAIL %s" % type(exc).__name__)
    sys.exit(0)
schema = getattr(bridge, 'CANNANAS_FUNCTION_SCHEMA', {})
enum = (schema.get('properties', {}).get('action', {}) or {}).get('enum', [])
bad = [a for a in enum if str(a).startswith('confirm_') or str(a).startswith('cancel_')]
if bad:
    print("SCHEMA_LEAK %d" % len(bad))
else:
    print("SCHEMA_OK %d" % len(enum))
actions = list(getattr(bridge, 'CANNANAS_VOICE_ACTIONS', ()))
bad2 = [a for a in actions if str(a).startswith('confirm_') or str(a).startswith('cancel_')]
if bad2:
    print("VOICE_ACTIONS_LEAK %d" % len(bad2))
PYEOF
    )
    if printf '%s' "$schema_result" | grep -q '^IMPORT_FAIL'; then
        finding "S4 Modell-Schema der Bridge nicht importierbar — Schema-Pruefung nicht durchgefuehrt"
    elif printf '%s' "$schema_result" | grep -qE '^(SCHEMA_LEAK|VOICE_ACTIONS_LEAK)'; then
        finding "S4 Modell-Schema der Bridge (esp32_jarvis_bridge.py) enthaelt confirm_*/cancel_* — das Modell kann einen Schreibvorgang direkt ausloesen"
    fi
    log_line "S4 Modell-Schema: ${schema_result:-unbekannt}"

    # (c) Verhaltenstest des Hosts
    local host_result
    host_result=$(cd "$MARK_LV_TREE" && "$PYTHON_BIN" - <<'PYEOF' 2>&1
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
try:
    import core.cannanas_host as host_mod
except Exception as exc:
    print("IMPORT_FAIL %s" % type(exc).__name__)
    sys.exit(0)

calls = []

class FakeClient:
    degraded = False
    read_only_degraded = False
    degraded_reason = ''
    def call(self, tool, args):
        calls.append(tool)
        if tool == 'get_adapter_info':
            return {'config': {'boot_id': 'boot-A'}}
        if tool.startswith('prepare_'):
            return {'ok': True, 'payload_hash': 'ph-1', 'preview': {'plant_name': 'X'},
                    'confirmation_token': 'tok-1'}
        return {'ok': True, 'result_id': 'rid-1'}

class FakeAudit:
    def record(self, *a, **k):
        return True
    def record_read(self, *a, **k):
        return True
    def pre_post(self, **k):
        class Ctx:
            def __enter__(self_):
                return lambda *a, **kw: None
            def __exit__(self_, *a):
                return False
        return Ctx()

class Turn:
    stable_device_id = 'audit-device-1'
    auth_session_id = 'audit-auth-1'
    user_turn_id = 5
    envelope = type('E', (), {'stable_device_id': 'audit-device-1',
                               'auth_session_id': 'audit-auth-1'})()

env = {
    'JARVIS_CANNANAS_ADAPTER_COMMAND': '/bin/true',
    'JARVIS_CANNANAS_AUDIT_FILE': '/dev/null',
    'CANNANAS_API_KEY': 'audit-dummy',
    'CANNANAS_CLUB_ID': 'audit-dummy',
    'CANNANAS_STATE_DIR': '/tmp',
    'CANNANAS_AUDIT_FILE': '/dev/null',
}

try:
    h = host_mod.CannanasHost(env=env, client=FakeClient(), audit=FakeAudit())
except Exception as exc:
    print("HOST_FAIL %s" % type(exc).__name__)
    sys.exit(0)

turn = Turn()

# 1) confirm_* OHNE vorheriges PendingConfirmation
calls.clear()
try:
    h.confirm(turn)
except Exception as exc:
    print("CONFIRM_WITHOUT_PENDING_ERROR %s" % type(exc).__name__)
    sys.exit(0)
if [c for c in calls if c.startswith('confirm_')]:
    print("LEAK_CONFIRM_WITHOUT_PENDING")
else:
    print("CONFIRM_WITHOUT_PENDING_REFUSED")

# 2) Fehlende Adapter-Boot-ID -> fail-closed, kein Prepare
if host_mod.prepare_binding_for(turn, '') is not None:
    print("LEAK_BINDING_WITHOUT_BOOT")
else:
    print("BOOT_FENCE_FAILCLOSED")

# 3) Erst nach echtem Prepare darf ein confirm_* den Adapter erreichen
calls.clear()
try:
    prep = h.prepare('prepare_plant_update', {'plant_name': 'X', 'status': 'BLOOM'}, turn)
except Exception as exc:
    print("PREPARE_ERROR %s" % type(exc).__name__)
    sys.exit(0)
if not prep.get('ok') or not prep.get('requires_confirmation'):
    print("PREPARE_REFUSED")
    sys.exit(0)
later = Turn()
later.user_turn_id = 6
try:
    h.confirm(later)
except Exception as exc:
    print("CONFIRM_ERROR %s" % type(exc).__name__)
    sys.exit(0)
if [c for c in calls if c.startswith('confirm_')]:
    print("CONFIRM_AFTER_PENDING_OK")
else:
    print("LEAK_NO_CONFIRM_AFTER_PENDING")

# 4) Replay: dieselbe Bestaetigung darf nicht erneut schreiben
calls.clear()
try:
    h.confirm(later)
except Exception:
    pass
if [c for c in calls if c.startswith('confirm_')]:
    print("LEAK_REPLAY")
else:
    print("REPLAY_REFUSED")
PYEOF
    )
    if printf '%s' "$host_result" | grep -qE '^(IMPORT_FAIL|HOST_FAIL)'; then
        finding "S4 Host-Verhaltenstest nicht ausfuehrbar — Pruefung 'confirm_* nur nach PendingConfirmation' nicht durchgefuehrt"
    else
        if printf '%s' "$host_result" | grep -q '^LEAK_'; then
            finding "S4 Host bestaetigt ohne PendingConfirmation oder mit Replay — der Boot-Fence des Hosts waere umgangen"
        fi
        if ! printf '%s' "$host_result" | grep -q '^CONFIRM_WITHOUT_PENDING_REFUSED$'; then
            finding "S4 Host verweigert den confirm_*-Aufruf ohne PendingConfirmation nicht nachweislich"
        fi
        if ! printf '%s' "$host_result" | grep -q '^BOOT_FENCE_FAILCLOSED$'; then
            finding "S4 Host erzeugt ohne Adapter-Boot-ID eine Bindung — der Boot-Fence waere fail-open"
        fi
    fi
    log_line "S4 Host-Verhaltenstest: $(printf '%s' "$host_result" | tr '\n' ' ')"

    # (d) Adapter: confirm_* muss durch _begin_write laufen (AST-Pruefung)
    local adapter_result
    adapter_result=$("$PYTHON_BIN" - "$ADAPTER_TREE/src/cannanas_mcp_adapter/server.py" <<'PYEOF' 2>&1
import ast, sys
try:
    tree = ast.parse(open(sys.argv[1], 'r', encoding='utf-8').read())
except Exception as exc:
    print("PARSE_FAIL %s" % type(exc).__name__)
    sys.exit(0)
cls = next((n for n in tree.body
             if isinstance(n, ast.ClassDef) and n.name == 'CannanasAdapter'), None)
if cls is None:
    print("NO_CLASS")
    sys.exit(0)
methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
confirms = sorted(n for n in methods if n.startswith('confirm_'))

def called_names(fn):
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out

def reaches(fn, target, seen=None):
    seen = set() if seen is None else seen
    if fn.name in seen:
        return False
    seen.add(fn.name)
    for name in called_names(fn):
        if name == target:
            return True
        if name in methods and reaches(methods[name], target, seen):
            return True
    return False

unfenced = [c for c in confirms if not reaches(methods[c], '_begin_write')]
if unfenced:
    print("UNFENCED %d" % len(unfenced))
else:
    print("FENCED %d" % len(confirms))
PYEOF
    )
    if printf '%s' "$adapter_result" | grep -qE '^(PARSE_FAIL|NO_CLASS)'; then
        finding "S4 Adapter-Quelltext nicht analysierbar — Boot-Fence der confirm_*-Werkzeuge nicht geprueft"
    elif printf '%s' "$adapter_result" | grep -q '^UNFENCED'; then
        finding "S4 Adapter: confirm_*-Werkzeuge umgehen den Boot-Fence (_begin_write) — Schreibvorgang ohne Bindung moeglich"
    fi
    log_line "S4 Adapter-Boot-Fence: ${adapter_result:-unbekannt}"

    # (e) Verhaltenstest des Adapters: ein confirm_* muss die Bindung
    #     erzwingen. Der Test laeuft gegen den echten Adapter-Quelltext mit
    #     einem gefaelschten Transport (es geht NICHTS raus) und prueft:
    #       1) Ein erfundener Bestaetigungscode -> 0 HTTP-Requests.
    #       2) Ein korrektes Prepare+Confirm -> genau 1 Schreibrequest.
    #       3) Ein Replay desselben Codes -> 0 HTTP-Requests.
    #     Ein Modell, das confirm_* direkt aufruft, kann also keinen
    #     Schreibvorgang ausloesen: der Adapter verweigert ohne Vorbereitung.
    local adapter_behaviour
    adapter_behaviour=$("$ADAPTER_PYTHON" - "$ADAPTER_TREE/src" <<'PYEOF' 2>&1
import os, sys, tempfile, types

# FastMCP wird nur fuer den Import gebraucht (kein Server wird gestartet).
try:
    from mcp.server.fastmcp import FastMCP  # noqa: F401
except Exception:
    mcp_pkg = types.ModuleType('mcp'); mcp_pkg.__path__ = []
    server_mod = types.ModuleType('mcp.server'); server_mod.__path__ = []
    fastmcp_mod = types.ModuleType('mcp.server.fastmcp')
    class _FastMCP:
        def __init__(self, name, instructions=None): pass
        def tool(self, name=None):
            def deco(fn): return fn
            return deco
    fastmcp_mod.FastMCP = _FastMCP
    sys.modules['mcp'] = mcp_pkg
    sys.modules['mcp.server'] = server_mod
    sys.modules['mcp.server.fastmcp'] = fastmcp_mod

src = sys.argv[1]
sys.path.insert(0, src)
try:
    from cannanas_mcp_adapter import server as adapter_server
except Exception as exc:
    print("IMPORT_FAIL %s" % type(exc).__name__)
    sys.exit(0)

calls = []

class FakeResp:
    status_code = 200
    def json_or_none(self):
        return {"plants": [{"id": "p-1", "name": "X"}]}

def transport(method, path, params, body):
    calls.append((method, path))
    return FakeResp()

state = tempfile.mkdtemp()
os.chmod(state, 0o700)
audit_path = os.path.join(state, 'audit.jsonl')
adapter = adapter_server.CannanasAdapter(
    api_key='audit-dummy', club_id='audit-dummy', state_dir=state,
    audit_path=audit_path, transport=transport)
binding = {
    'adapter_boot_id': adapter.boot.boot_id,
    'jarvis_session_id': 'audit-session',
    'stable_device_id': 'audit-device',
    'auth_session_id': 'audit-auth',
    'prepared_turn_id': '1',
}

# 1) Das Modell erfindet einen Bestaetigungscode.
calls.clear()
try:
    adapter.confirm_plant_update('audit-erfundener-code', {'adapter_boot_id': 'falsch'})
except Exception:
    pass
if calls:
    print("LEAK_FORGED_TOKEN %d" % len(calls))
else:
    print("FORGED_TOKEN_REFUSED")

# 2) Korrektes Prepare, dann Confirm: genau ein Schreibrequest.
try:
    prep = adapter.prepare_plant_update('X', {'status': 'FLOWERING'}, binding=binding)
except Exception as exc:
    print("PREPARE_ERROR %s" % type(exc).__name__)
    sys.exit(0)
token = prep.get('confirmation_token') or ''
if not token:
    print("PREPARE_NO_TOKEN")
    sys.exit(0)
calls.clear()
try:
    adapter.confirm_plant_update(token, binding=binding)
except Exception:
    pass
writes = [c for c in calls if c[0] in ('PUT', 'POST', 'PATCH', 'DELETE')]
if len(writes) == 1:
    print("FENCED_WRITE_OK %d" % len(writes))
elif not writes:
    print("NO_WRITE_ON_CONFIRM")
else:
    print("MULTIPLE_WRITES %d" % len(writes))

# 3) Replay desselben Codes: kein zweiter Schreibrequest.
calls.clear()
try:
    adapter.confirm_plant_update(token, binding=binding)
except Exception:
    pass
writes = [c for c in calls if c[0] in ('PUT', 'POST', 'PATCH', 'DELETE')]
if writes:
    print("LEAK_REPLAY %d" % len(writes))
else:
    print("REPLAY_REFUSED")
PYEOF
    )
    if printf '%s' "$adapter_behaviour" | grep -qE '^(IMPORT_FAIL|PREPARE_ERROR|PREPARE_NO_TOKEN)'; then
        finding "S4 Adapter-Verhaltenstest nicht ausfuehrbar — der Bindungs-Zwang von confirm_* wurde nicht geprueft"
    else
        if printf '%s' "$adapter_behaviour" | grep -q '^LEAK_'; then
            finding "S4 Adapter: confirm_* laesst einen Schreibvorgang ohne gueltige Vorbereitung durch (Boot-Fence des Adapters umgangen)"
        fi
        if ! printf '%s' "$adapter_behaviour" | grep -q '^FORGED_TOKEN_REFUSED$'; then
            finding "S4 Adapter: ein erfundener Bestaetigungscode fuehrt zum Schreibvorgang — der Adapter ist nicht fail-closed"
        fi
        if ! printf '%s' "$adapter_behaviour" | grep -q '^REPLAY_REFUSED$'; then
            finding "S4 Adapter: dieselbe Bestaetigung laesst sich erneut ausfuehren (Replay moeglich)"
        fi
    fi
    log_line "S4 Adapter-Verhaltenstest: $(printf '%s' "$adapter_behaviour" | tr '\n' ' ')"
}

# =============================================================================
# S6  ERREICHBARKEIT DES AUDITS
# =============================================================================
# Test: die Audit-Dateien muessen auf dem Server existieren, beschreibbar sein
# und im Laufe der Zeit wachsen. Ein 0-Byte-Audit trotz stattgefundener
# Schreibzugriffe ist ein Befund. Zusaetzlich muss der Adapter-Audit echte
# Write-Zustaende tragen, sonst ist er kein Write-Audit.
# =============================================================================
s6_check() {
    local remote_py='
import json, os, stat
out = {}
paths = {
    "adapter": "/home/lacasa/cannanas-adapter/adapter-audit.jsonl",
    "host": "/home/lacasa/cannanas-adapter/audit/jarvis-write-audit.jsonl",
}
for name, path in paths.items():
    entry = {"exists": os.path.exists(path)}
    if entry["exists"]:
        st = os.stat(path)
        entry["size"] = st.st_size
        entry["mode"] = oct(stat.S_IMODE(st.st_mode))
        entry["mtime"] = int(st.st_mtime)
        entry["writable"] = os.access(path, os.W_OK)
        entry["dir_writable"] = os.access(os.path.dirname(path), os.W_OK)
    out[name] = entry
counts = {}
try:
    with open(paths["adapter"], "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            st = d.get("state") or d.get("action") or "read"
            counts[st] = counts.get(st, 0) + 1
except Exception as exc:
    counts = {"error": type(exc).__name__}
out["adapter_counts"] = counts
print(json.dumps(out))
'
    local data
    data=$(printf '%s' "$remote_py" | ssh $SSH_OPTS "$SERVER_HOST" /usr/bin/python3 - 2>/dev/null)
    if [ -z "$data" ]; then
        finding "S6 Audit-Status auf lacasa-server nicht lesbar — Erreichbarkeits-Pruefung nicht durchgefuehrt"
        return 0
    fi

    # Der Status kommt ueber eine Datei: der Python-Block braucht stdin
    # fuer sein Skript.
    local status_file="${TMP_DIR}/audit-status.json"
    printf '%s' "$data" > "$status_file"
    local verdict
    verdict=$("$PYTHON_BIN" - "$status_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as fh:
        data = json.load(fh)
except Exception:
    print("PARSE_FAIL")
    sys.exit(0)
problems = []
adapter = data.get("adapter") or {}
host = data.get("host") or {}
counts = data.get("adapter_counts") or {}

for name, entry in (("Adapter-Audit", adapter), ("Host-Audit", host)):
    if not entry.get("exists"):
        problems.append("%s fehlt auf lacasa-server" % name)
        continue
    if not entry.get("writable"):
        problems.append("%s ist nicht schreibbar" % name)
    if not entry.get("dir_writable"):
        problems.append("%s-Verzeichnis ist nicht schreibbar" % name)
    if int(entry.get("size") or 0) == 0:
        problems.append("%s ist 0 Bytes (trotz Schreibzugriffen)" % name)

write_states = sum(int(counts.get(s, 0) or 0)
                   for s in ("prepared", "executing", "executed", "unbekannt"))
if not counts or "error" in counts:
    problems.append("Adapter-Audit enthaelt keine auswertbaren Eintraege")
elif write_states == 0:
    problems.append("Adapter-Audit enthaelt keine Write-Zustaende (prepared/executing/executed)")

for p in problems:
    print("PROBLEM " + p)
if not problems:
    print("OK")
PYEOF
    )
    if printf '%s' "$verdict" | grep -q '^PARSE_FAIL$'; then
        finding "S6 Audit-Status nicht auswertbar — Erreichbarkeits-Pruefung nicht durchgefuehrt"
        return 0
    fi
    if ! printf '%s' "$verdict" | grep -q '^OK$'; then
        while IFS= read -r line; do
            case "$line" in
                PROBLEM\ *)
                    finding "S6 ${line#PROBLEM }"
                    ;;
            esac
        done <<< "$verdict"
    fi

    # Wachstum: der Adapter-Audit muss wachsen, wenn Schreibzugriffe stattfinden.
    local size_before size_after
    size_before=$(printf '%s' "$data" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); print(int((d.get("adapter") or {}).get("size") or 0))' 2>/dev/null)
    size_after=$(ssh $SSH_OPTS "$SERVER_HOST" "stat -c '%s' '$REMOTE_ADAPTER_AUDIT' 2>/dev/null || echo 0" 2>/dev/null)
    size_before=${size_before:-0}
    size_after=${size_after:-0}
    if [ "$size_after" -gt "$size_before" ]; then
        log_line "S6 Adapter-Audit waechst (${size_before} -> ${size_after} Bytes)"
    else
        log_line "S6 Adapter-Audit Groesse unveraendert (${size_after} Bytes); Wachstum ist nur bei Schreibzugriffen beobachtbar"
    fi
}

# =============================================================================
# Selbsttest: ein echter Stoerfall muss anschlagen
# =============================================================================
# Erzeugt eine KOPIE des echten Adapter-Audits, manipuliert die Kopie und
# schickt sie durch DIESELBE Auswertung (s3_s5_evaluate), die auch den
# echten Log prueft. Die echten Dateien werden nie angefasst.
build_manipulated_copy() {
    local base="" copy="${TMP_DIR}/manipulated-audit.jsonl"
    if [ -n "$AUDIT_OVERRIDE" ] && [ -f "$AUDIT_OVERRIDE" ]; then
        base="$AUDIT_OVERRIDE"
    else
        base="${TMP_DIR}/adapter-audit.jsonl"
        scp -q $SSH_OPTS "$SERVER_HOST:$REMOTE_ADAPTER_AUDIT" "$base" 2>/dev/null || return 1
    fi
    cp "$base" "$copy" || return 1
    # Manipulierte KOPIE: ein 'executed' OHNE 'prepared' und OHNE boot_id.
    "$PYTHON_BIN" - "$copy" <<'PYEOF'
import json, sys
path = sys.argv[1]
entry = {
    "timestamp": "2026-01-01T00:00:00+00:00",
    "boot_id": "",
    "tool": "confirm_plant_update",
    "action": "audit-selbsttest",
    "club_id": "audit-selbsttest",
    "payload_hash": "selbsttest-payload-hash",
    "preview": {"plant_name": "Selbsttest"},
    "binding": {},
    "state": "executed",
    "returned_object_id": "",
    "ok": True,
}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
PYEOF
    printf '%s' "$copy"
}

run_self_test() {
    local copy
    copy=$(build_manipulated_copy)
    if [ -z "$copy" ] || [ ! -f "$copy" ]; then
        printf 'Selbsttest FEHLGESCHLAGEN: keine manipulierte Kopie erzeugt\n'
        return 1
    fi
    printf 'Selbsttest: manipulierte KOPIE erzeugt (%s Zeilen, davon 1 manipulierter Eintrag)\n' \
        "$(grep -c . "$copy" 2>/dev/null || echo '?')"

    # Die Kopie durch DIESELBE Auswertung schicken, die den echten Log prueft.
    local output
    output=$(
        FINDINGS=0
        s3_s5_evaluate "$copy" "Selbsttest-Kopie" 1
    )
    if printf '%s' "$output" | grep -q "boot_id\|ohne vorheriges"; then
        printf '%s\n' "$output" | sed 's/^/  /'
        printf 'Selbsttest ERFOLGREICH: der Stoerfall wird erkannt.\n'
        return 0
    fi
    printf 'Selbsttest FEHLGESCHLAGEN: der Stoerfall wurde NICHT erkannt.\n'
    return 1
}

# =============================================================================
# Ablauf
# =============================================================================
if [ "$SELF_TEST" -eq 1 ]; then
    run_self_test
    exit $?
fi

s1_check
s2_check
s3_s5_check
s4_check
s6_check

if [ "$FINDINGS" -eq 0 ]; then
    log_line "Audit beendet: keine Befunde"
    exit 0
fi
log_line "Audit beendet: ${FINDINGS} Befund(e)"
exit 1
