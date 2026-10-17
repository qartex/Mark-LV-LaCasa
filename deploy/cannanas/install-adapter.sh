#!/usr/bin/env bash
# Installs the pinned Cannanas MCP adapter release into a dedicated venv on
# lacasa-server. Never starts anything: the JARVIS unit launches the child.
#
# Subcommands (no arguments: usage):
#   --verify WHEEL [--expect-sha SHA256] [--manifest MANIFEST.json]
#       Verify the wheel SHA-256 against the pinned release (default) and,
#       when given, the release manifest's wheel_sha256. Fails closed before
#       installing anything; never echoes wheel/manifest content.
#   --check-env FILE
#       Refuse everything but a mode-0600 file with exactly the four adapter
#       variables and non-empty values.
#   --install WHEEL VENV [CONSTRAINTS] [RELEASE_INFO]
#       verify → create the venv in $CANNANAS_VENV_DIR (default /opt/cannanas/adapter/venv),
#       pip install from the hashed constraints plus the wheel, copy the
#       release_info.json sidecar next to the entry point. Starts nothing.
set -euo pipefail

PINNED_SHA256='8349fbd2fc6a3761334e035c06077e4e2f3eff3ee7da2b7bf001de7c709e113c'
PINNED_VERSION='1.0.0'
ADAPTER_VARS=('CANNANAS_API_KEY' 'CANNANAS_CLUB_ID' 'CANNANAS_STATE_DIR' 'CANNANAS_AUDIT_FILE')

usage() {
  cat >&2 <<'USAGE'
Usage: install-adapter.sh --verify WHEEL [--expect-sha SHA] [--manifest MANIFEST.json]
       install-adapter.sh --check-env FILE
       install-adapter.sh --install WHEEL VENV [CONSTRAINTS] [RELEASE_INFO]
The installer never starts services. Fails closed on any mismatch.
USAGE
  exit 2
}

die() { echo "install-adapter: $1" >&2; exit 1; }

sha256_of() { sha256sum -- "$1" | cut -d' ' -f1; }

cmd_verify() {
  [ $# -ge 1 ] || usage
  local wheel="$1"; shift
  local expect="$PINNED_SHA256"
  local manifest=''
  while [ $# -gt 0 ]; do
    case "$1" in
      --expect-sha) expect="$2"; shift 2 ;;
      --manifest) manifest="$2"; shift 2 ;;
      *) usage ;;
    esac
  done
  [ -f "$wheel" ] || die "wheel not found (sha check)"
  local actual
  actual="$(sha256_of "$wheel")"
  [ "$actual" = "$expect" ] || die "wheel SHA-256 mismatch (refusing to install)"
  if [ -n "$manifest" ];then
    local manifest_sha
    manifest_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("wheel_sha256",""))' "$manifest" 2>/dev/null || true)"
    [ -n "$manifest_sha" ] || die "release manifest unreadable (wheel_sha256 missing)"
    [ "$actual" = "$manifest_sha" ] || die "wheel SHA-256 mismatch against manifest (install refused)"
  fi
  echo "wheel SHA-256 verified (pinned release $PINNED_VERSION)"
  exit 0
}

cmd_check_env() {
  [ $# -eq 1 ] || usage
  local file="$1"
  [ -f "$file" ] || die "credential file not found"
  [ -L "$file" ] && die "credential file must not be a symlink"
  local mode
  mode="$(stat -c '%a' "$file")"
  [ "$mode" = "600" ] || die "credential file must be mode 0600 (found mode refused)"
  local expected owner
  owner="$(stat -c '%u' "$file")"
  expected="$(id -u 2>/dev/null || echo "$owner")"
  [ "$owner" = "$expected" ] || die "credential file must be owned by the service user"
  local -A want
  for name in "${ADAPTER_VARS[@]}"; do
    want[$name]=1
    local value
    value="$(grep -E "^${name}=..*" "$file" | head -1 | cut -d'=' -f2- || true)"
    [ -n "$value" ] || die "missing or empty ${name}"
  done
  local line name
  while IFS= read -r line; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    name="${line%%=*}"
    [ -n "${want[$name]:-}" ] || die "unexpected environment variable in credential file"
  done < "$file"
  echo "credential file verified: mode 0600, four adapter variables set"
  exit 0
}

cmd_install() {
  [ $# -ge 2 ] || usage
  local wheel="$1"; shift
  local venv="$1"; shift
  local constraints="${1:-}"; [ $# -gt 0 ] && shift
  local sidecar="${1:-}"; [ $# -gt 0 ] && shift
  [ -f "$wheel" ] || die "wheel not found"
  local actual
  actual="$(sha256_of "$wheel")"
  [ "$actual" = "$PINNED_SHA256" ] || die "wheel SHA-256 mismatch (refusing to install)"
  [ -n "${CANNANAS_API_URL:-}" ] && die "CANNANAS_API_URL must never be set (fixed origin)"
  local bin_dir
  bin_dir="$(python3 - <<'PY'
import sysconfig
print(sysconfig.get_path('scripts', 'venv'))
PY
)" || bin_dir='bin'
  mkdir -p "$venv"
  python3 -m venv "$venv" || die "venv creation failed"
  local pip_args=(install --no-deps)
  if [ -n "$constraints" ]; then
    [ -f "$constraints" ] || die "constraints lock not found"
    # Constraints (-c), not requirements (-r): the lock also pins the adapter
    # itself, which must come only from the verified wheel argument.
    pip_args+=(-c "$constraints")
  fi
  local wheel_dir pinned_spec
  wheel_dir="$(cd "$(dirname "$wheel")" && pwd)"
  pinned_spec="cannanas-mcp-adapter==${PINNED_VERSION}"
  # --find-links + version pin (not a bare path arg): pip's hash-checking mode
  # demands the hash on the requirement, and the constraint supplies it. The
  # local wheel is found and its hash is verified against the locked value.
  pip_args+=(--find-links "$wheel_dir" --require-hashes "$pinned_spec")
  local install_log
  if ! install_log="$("$venv/bin/pip" "${pip_args[@]}" 2>&1)"; then
    printf '%s\n' "$install_log" | tail -4 >&2
    die "pip install failed"
  fi
  "$venv/bin/python" -c 'import cannanas_mcp_adapter' >/dev/null 2>&1 \
    || die "installed adapter does not import"
  if [ -n "$sidecar" ]; then
    [ -f "$sidecar" ] || die "release_info.json sidecar not found"
    mkdir -p "$venv/bin"
    cp "$sidecar" "$venv/bin/release_info.json"
    chmod 0600 "$venv/bin/release_info.json"
  fi
  echo "adapter pinned release installed into $venv (nothing started)"
  exit 0
}

[ $# -ge 1 ] || usage
case "$1" in
  --verify) shift; cmd_verify "$@" ;;
  --check-env) shift; cmd_check_env "$@" ;;
  --install) shift; cmd_install "$@" ;;
  *) usage ;;
esac