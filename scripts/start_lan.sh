#!/usr/bin/env bash
#
# One-click LAN launcher for the DUT monitoring dashboard.
#
# Dev (default): FastAPI backend (:8000) + Vite dev server (:5173).
#   ./scripts/start_lan.sh            # open http://<host>:5173
#
# Prod (--prod): build the frontend, then serve EVERYTHING from the backend on
# a single port (:8000) — no Vite. Best for edge / LAN deployment.
#   ./scripts/start_lan.sh --prod     # open http://<host>:8000
#
# Both bind 0.0.0.0. Overridable via env vars:
#   BIND_HOST (default 0.0.0.0) · BACKEND_PORT (default 8000) · FRONTEND_PORT (default 5173)
#   DUT_ENGINEER_PASSCODE · DUT_ADMIN_PASSCODE (generated on first launch if unset)
#   DUT_LAN_SSID (Wi-Fi name shown to guests; auto-detected on macOS when unset)
#   DUT_LAN_PSK  (optional; with DUT_LAN_SSID, also prints a join-Wi-Fi QR)
#   DUT_QR_INVITE=engineer|admin (optional; QR carries a single-use invite for
#                that role instead of the plain guest URL)
#   DUT_NO_TLS=1 (keep --prod on plain HTTP; TLS is --prod-only either way,
#                since the dev Vite proxy targets the backend over http/ws)
#   NOTE (dev only): the Vite proxy targets 127.0.0.1:8000 (frontend/vite.config.ts);
#   changing BACKEND_PORT in dev also requires updating that proxy target.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/dut-dashboard/backend"
FRONTEND_DIR="$ROOT_DIR/dut-dashboard/frontend"

BIND_HOST="${BIND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

# Stamp a deploy version so the in-app "new version available" banner can detect
# a redeploy. `git describe` only changes when new commits/tags land, so a plain
# restart does not nag users. Override by pre-setting DUT_APP_VERSION yourself.
if [ -z "${DUT_APP_VERSION:-}" ]; then
  DUT_APP_VERSION="$(git -C "$ROOT_DIR" describe --tags --always 2>/dev/null || echo dev)"
fi
export DUT_APP_VERSION
echo "[start_lan] version: $DUT_APP_VERSION"

# Registering as engineer or admin needs the shared passcode for that role, and
# an unset passcode LOCKS the role — on a fresh deploy that leaves the Serial
# Console, Files and Downloads unreachable for everyone, with no admin able to
# open them. Generate a stable pair on first launch rather than shipping a
# default passcode in the repo. An admin can rotate them later via
# POST /api/auth/passcodes; the stored value then takes precedence over these.
PASSCODE_FILE="$ROOT_DIR/dut-dashboard/data/role-passcodes.env"
if [ -z "${DUT_ENGINEER_PASSCODE:-}" ] || [ -z "${DUT_ADMIN_PASSCODE:-}" ]; then
  if [ ! -f "$PASSCODE_FILE" ]; then
    mkdir -p "$(dirname "$PASSCODE_FILE")"
    (
      umask 077
      {
        echo "engineer=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-12)"
        echo "admin=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-12)"
      } > "$PASSCODE_FILE"
    )
    echo "[start_lan] generated role passcodes -> $PASSCODE_FILE"
  fi
  # Fill only what the operator did not supply, so an explicit env var wins.
  DUT_ENGINEER_PASSCODE="${DUT_ENGINEER_PASSCODE:-$(sed -n 's/^engineer=//p' "$PASSCODE_FILE")}"
  DUT_ADMIN_PASSCODE="${DUT_ADMIN_PASSCODE:-$(sed -n 's/^admin=//p' "$PASSCODE_FILE")}"
fi
export DUT_ENGINEER_PASSCODE DUT_ADMIN_PASSCODE
echo "[start_lan] engineer passcode: $DUT_ENGINEER_PASSCODE"
echo "[start_lan] admin passcode:    $DUT_ADMIN_PASSCODE"

PROD=0
for arg in "$@"; do
  case "$arg" in
    --prod) PROD=1 ;;
    *) echo "[start_lan] unknown arg: $arg (use --prod for production)"; exit 2 ;;
  esac
done

# --- Python venv + backend deps ---------------------------------------------
if [ ! -d "$ROOT_DIR/.venv" ]; then
  echo "[start_lan] creating Python venv (.venv)..."
  python3 -m venv "$ROOT_DIR/.venv"
fi
# shellcheck disable=SC1091
source "$ROOT_DIR/.venv/bin/activate"
echo "[start_lan] installing backend deps..."
pip install -q -r "$ROOT_DIR/requirements.txt"

# --- Frontend deps (needed for both build and dev) --------------------------
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "[start_lan] installing frontend deps (npm install)..."
  (cd "$FRONTEND_DIR" && npm install)
fi

# --- Prod: build the static frontend so the backend can serve it ------------
if [ "$PROD" -eq 1 ]; then
  echo "[start_lan] building frontend (npm run build)..."
  (cd "$FRONTEND_DIR" && npm run build)
fi

detect_lan_ip() {
  # macOS: ask the common interfaces directly; Linux: hostname -I.
  local ip
  for iface in en0 en1 en2; do
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}

LAN_IP="$(detect_lan_ip || true)"

# --- TLS (--prod only) -------------------------------------------------------
# Self-signed cert, generated once into the gitignored data/certs. TLS is for
# --prod only: in dev the Vite proxy targets the backend over plain http/ws, so
# putting TLS on :8000 would break the proxy rather than secure anything. Opt
# out entirely with DUT_NO_TLS=1.
#
# The cert pins the LAN IP in subjectAltName, so it stops matching if the host's
# address changes — delete data/certs and relaunch to regenerate.
CERT_DIR="$ROOT_DIR/dut-dashboard/data/certs"
CERT_FILE="$CERT_DIR/dev.crt"
KEY_FILE="$CERT_DIR/dev.key"
UVICORN_TLS_ARGS=()
SCHEME="http"

if [ "$PROD" -eq 1 ] && [ -z "${DUT_NO_TLS:-}" ]; then
  if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    mkdir -p "$CERT_DIR"
    SAN="DNS:localhost,IP:127.0.0.1"
    [ -n "$LAN_IP" ] && SAN="$SAN,IP:$LAN_IP"
    echo "[start_lan] generating self-signed cert ($SAN)..."
    if ! openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -subj "/CN=dut-dashboard" -addext "subjectAltName=$SAN" 2>/dev/null; then
      echo "[start_lan] WARNING: openssl failed — falling back to plain HTTP"
      rm -f "$CERT_FILE" "$KEY_FILE"
    else
      chmod 600 "$KEY_FILE"
    fi
  fi
  if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    UVICORN_TLS_ARGS=(--ssl-keyfile "$KEY_FILE" --ssl-certfile "$CERT_FILE")
    SCHEME="https"
    echo "[start_lan] TLS on (self-signed — expect one browser warning). DUT_NO_TLS=1 disables it."
  fi
fi

# --- Stop child(ren) on exit ------------------------------------------------
PIDS=()
cleanup() {
  echo
  echo "[start_lan] stopping..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

# --- Backend (no --reload: this is a deploy launcher) -----------------------
# In prod the backend also serves the built dist/ at "/" (single port).
echo "[start_lan] backend  -> ${SCHEME}://${BIND_HOST}:${BACKEND_PORT}"
(cd "$BACKEND_DIR" && exec python3 -m uvicorn app.main:app \
  --host "$BIND_HOST" --port "$BACKEND_PORT" "${UVICORN_TLS_ARGS[@]:-}") &
PIDS+=($!)

if [ "$PROD" -eq 1 ]; then
  echo "[start_lan] PROD: open ${SCHEME}://${BIND_HOST}:${BACKEND_PORT}   <-- UI + API + WS on one port"
else
  echo "[start_lan] frontend -> http://${BIND_HOST}:${FRONTEND_PORT}   <-- open this on the LAN"
  (cd "$FRONTEND_DIR" && exec npm run dev -- --host "$BIND_HOST" --port "$FRONTEND_PORT") &
  PIDS+=($!)
fi

# --- Guest onboarding: LAN URL + QR ------------------------------------------
# Landing is guest-by-default (no login needed to browse), so a scannable QR is
# the whole onboarding: join the Wi-Fi, scan, watch. Engineers log in from the
# toolbar with the passcode printed above. All best-effort — any failure here
# degrades to the plain-text URLs already printed and never kills the launcher.

detect_ssid() {
  # Operator override first; else current association (macOS; fails silently
  # on wired-only hosts and newer macOS that dropped the airport tooling).
  if [ -n "${DUT_LAN_SSID:-}" ]; then
    echo "$DUT_LAN_SSID"
    return
  fi
  local wifi_dev
  wifi_dev="$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2; exit}')"
  [ -n "$wifi_dev" ] || return 0
  networksetup -getairportnetwork "$wifi_dev" 2>/dev/null \
    | sed -n 's/^Current Wi-Fi Network: //p'
}

print_qr() {
  # ASCII QR via the venv's qrcode package (in requirements). If it is missing
  # (pre-existing venv that skipped the new dep), just skip the QR.
  python3 - "$1" <<'PY' 2>/dev/null || true
import sys
try:
    import qrcode
except ImportError:
    sys.exit(0)
qr = qrcode.QRCode(border=1)
qr.add_data(sys.argv[1])
qr.make()
qr.print_ascii(invert=True)
PY
}

if [ -n "$LAN_IP" ]; then
  if [ "$PROD" -eq 1 ]; then
    # Only the prod single-port backend carries TLS; the dev Vite server does not.
    DASH_URL="${SCHEME}://${LAN_IP}:${BACKEND_PORT}"
  else
    DASH_URL="http://${LAN_IP}:${FRONTEND_PORT}"
  fi
  SSID="$(detect_ssid || true)"
  echo
  if [ -n "$SSID" ]; then
    echo "[start_lan] guests: join Wi-Fi \"$SSID\", then scan to open the dashboard:"
    if [ -n "${DUT_LAN_PSK:-}" ] && [ -n "${DUT_LAN_SSID:-}" ]; then
      # Wi-Fi join QR (standard WIFI: payload). Only for the operator-supplied
      # pair — never echoes a password we merely guessed at.
      echo "[start_lan] 1) join the Wi-Fi:"
      print_qr "WIFI:T:WPA;S:${DUT_LAN_SSID};P:${DUT_LAN_PSK};;"
      echo "[start_lan] 2) open the dashboard:"
    fi
  else
    echo "[start_lan] guests: scan to open the dashboard (set DUT_LAN_SSID to show the Wi-Fi name):"
  fi

  # Default QR is the plain dashboard URL: browsing is guest-by-default, so a
  # token would buy nothing and would write a users row per scan. Opt in with
  # DUT_QR_INVITE=engineer|admin to mint a single-use invite instead, so the
  # scanner lands already holding that role and never types the passcode.
  SCAN_URL="$DASH_URL"
  if [ -n "${DUT_QR_INVITE:-}" ]; then
    INVITE_TOKEN="$(cd "$BACKEND_DIR" && python3 -m app.invite_cli mint \
      --role "$DUT_QR_INVITE" --label launcher --max-uses 1 2>/dev/null || true)"
    if [ -n "$INVITE_TOKEN" ]; then
      SCAN_URL="${DASH_URL}/?invite=${INVITE_TOKEN}"
      echo "[start_lan] QR carries a single-use ${DUT_QR_INVITE} invite (expires in 7 days)"
    else
      echo "[start_lan] WARNING: could not mint a ${DUT_QR_INVITE} invite — QR falls back to the plain URL"
    fi
  fi
  print_qr "$SCAN_URL"
  echo "[start_lan] dashboard: $DASH_URL   (browsing = guest, no login needed)"
fi

echo "[start_lan] running. Press Ctrl-C to stop."
wait
