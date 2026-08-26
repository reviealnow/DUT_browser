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
# If either child exits -- or stays STOPPED, which holds the port while serving
# nothing -- the launcher stops the other one and exits non-zero: half a stack
# keeps answering, out of a backend nobody meant to be running. For the same
# reason it refuses to start at all on a port already taken.
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

# --- Refuse to start on a port somebody else already holds -------------------
# A second launcher used to half-start, and say nothing useful about it: Vite
# hops to the next free port and keeps running, while uvicorn refuses to bind
# and shuts down. What survives is a NEW frontend proxying to the OLD backend --
# frontend/vite.config.ts hard-codes the proxy target at 127.0.0.1:8000 -- so
# the UI looks perfectly healthy while running last session's code, and the
# launcher's own output still names the port the OLD instance took.
#
# Check every port we are about to take, and check them ALL before starting
# anything: checking as we go would leave an orphaned backend behind whenever
# the frontend port is the busy one, which is the thing being killed off here.
# --prod runs no Vite, so its frontend port is not ours to claim.
CHECK_PORTS=("$BACKEND_PORT:backend")
[ "$PROD" -eq 1 ] || CHECK_PORTS+=("$FRONTEND_PORT:frontend")

PORT_CONFLICT=0
STOPPED_HOLDER=0
if command -v lsof >/dev/null 2>&1; then
  for entry in "${CHECK_PORTS[@]}"; do
    port="${entry%%:*}"
    role="${entry##*:}"
    holders="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
    [ -n "$holders" ] || continue
    PORT_CONFLICT=1
    echo "[start_lan] ERROR: $role port $port is already in use by:" >&2
    for pid in $holders; do
      # State, not just the command line. A holder in state T is STOPPED: it
      # still owns the port but runs nothing, so `kill` sits pending and looks
      # like it did nothing at all. That cost a deploy a long detour once --
      # say it here rather than leaving it for `ps` to reveal later. The state
      # letter carries flags (a stopped, niced process reads "TN"), so match
      # the prefix.
      state="$(ps -o state= -p "$pid" 2>/dev/null | tr -d ' ')"
      label=""
      case "$state" in T*) label="  [STOPPED]"; STOPPED_HOLDER=1 ;; esac
      echo "[start_lan]   PID $pid$label  $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-90)" >&2
    done
  done
else
  echo "[start_lan] WARNING: lsof not found -- cannot check that the ports are free." >&2
fi

if [ "$PORT_CONFLICT" -eq 1 ]; then
  if [ "$STOPPED_HOLDER" -eq 1 ]; then
    echo "[start_lan] A holder marked [STOPPED] is not running: it will not act on" >&2
    echo "[start_lan] SIGTERM until it is resumed, so a plain \`kill\` appears to do" >&2
    echo "[start_lan] nothing. Resume it first, and the pending signal lands:" >&2
    echo "[start_lan]     kill -CONT <PID>   # then it exits on the kill you already sent" >&2
  fi
  echo "[start_lan] Nothing was started. Either stop the process above:" >&2
  echo "[start_lan]     kill <PID>" >&2
  echo "[start_lan] or launch on ports nobody holds:" >&2
  if [ "$PROD" -eq 1 ]; then
    echo "[start_lan]     BACKEND_PORT=$((BACKEND_PORT + 1)) $0 --prod" >&2
  else
    echo "[start_lan]     BACKEND_PORT=$((BACKEND_PORT + 1)) FRONTEND_PORT=$((FRONTEND_PORT + 1)) $0" >&2
    echo "[start_lan]   NOTE: in dev, a changed BACKEND_PORT also needs the proxy target in" >&2
    echo "[start_lan]   frontend/vite.config.ts updated to match -- it is hard-coded to" >&2
    echo "[start_lan]   127.0.0.1:8000, so otherwise the new frontend talks to the OLD backend." >&2
  fi
  exit 1
fi

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
# PID_LABELS is a parallel array (bash 3.2 has no associative arrays) so the
# supervisor at the bottom can name which half of the stack died.
PIDS=()
PID_LABELS=()
cleanup() {
  echo
  echo "[start_lan] stopping..."
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    # SIGCONT first, or this whole guard is decorative. A stopped child never
    # runs its SIGTERM handler -- and uvicorn has one -- so a bare `kill` leaves
    # it stopped and STILL HOLDING ITS PORT, which is exactly the state the
    # supervisor below exits over.
    #
    # The kernel does resume an orphaned process group that has stopped members,
    # but only at the moment the group *becomes* orphaned; a group that was
    # already orphaned gets nothing, and whether that applies depends on how the
    # launcher was invoked. Measured both ways: relying on it leaves the child
    # alive. Sending it ourselves makes the outcome the same everywhere, and is
    # harmless for a child that is already running.
    kill -CONT "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

# --- Backend (no --reload: this is a deploy launcher) -----------------------
# In prod the backend also serves the built dist/ at "/" (single port).
echo "[start_lan] backend  -> ${SCHEME}://${BIND_HOST}:${BACKEND_PORT}"
# ${arr[@]+"${arr[@]}"} is the set -u-safe way to expand a possibly-unset array:
# it yields ZERO words when unset. "${arr[@]:-}" does not -- it yields one EMPTY
# word, which uvicorn rejects with `Got unexpected extra argument ()`. The array
# is only assigned on the --prod TLS path, so plain dev runs hit exactly that.
(cd "$BACKEND_DIR" && exec python3 -m uvicorn app.main:app \
  --host "$BIND_HOST" --port "$BACKEND_PORT" ${UVICORN_TLS_ARGS[@]+"${UVICORN_TLS_ARGS[@]}"}) &
PIDS+=($!)
PID_LABELS+=("backend (uvicorn on :$BACKEND_PORT)")

if [ "$PROD" -eq 1 ]; then
  echo "[start_lan] PROD: open ${SCHEME}://${BIND_HOST}:${BACKEND_PORT}   <-- UI + API + WS on one port"
else
  echo "[start_lan] frontend -> http://${BIND_HOST}:${FRONTEND_PORT}   <-- open this on the LAN"
  # --strictPort: without it Vite answers a port clash by moving to the next
  # free port and staying up, so the URL printed on the line above is not the
  # URL that works -- and the frontend that does answer on :5173 belongs to an
  # older instance. Fail the way uvicorn does instead.
  (cd "$FRONTEND_DIR" && exec npm run dev -- \
    --host "$BIND_HOST" --port "$FRONTEND_PORT" --strictPort) &
  PIDS+=($!)
  PID_LABELS+=("frontend (vite on :$FRONTEND_PORT)")
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

# --- Supervise: one child dying takes the whole stack down -------------------
# The port guard above only covers the moment of launch. A backend that dies
# later does the same damage: a crash, an operator kill, or another session
# winning the race between our check and uvicorn's bind all leave a live
# frontend proxying to a backend that is not the one this launcher started.
#
# Polling rather than `wait -n`, which needs bash 4.3; macOS ships 3.2.57 and
# this launcher is run there. One second is plenty of resolution for a launcher.
#
# "Alive" is not enough, though: `kill -0` SUCCEEDS for a process in state T,
# so a STOPPED child reads as healthy. That is not academic -- a deploy host
# was found with a stopped backend and a stopped Vite holding :8000 and :5173
# for hours, serving nothing, with SIGTERM sitting pending against them. Treat
# a child that stays stopped as dead.
#
# Debounced, because Ctrl-C's neighbour Ctrl-Z is legitimate: SIGTSTP goes to
# the whole foreground process group, so suspending the launcher and `fg`-ing
# it back must NOT kill the stack, and a child can still read as T for an
# instant after the group resumes. A real `fg` clears in milliseconds; a child
# that is genuinely stuck stays T forever. Three consecutive seconds separates
# them. Anything we cannot read a state for counts as fine -- `kill -0` above
# already covers actual death, and a `ps` hiccup must never kill a live stack.
STOP_STRIKES=()
for ((i = 0; i < ${#PIDS[@]}; i++)); do STOP_STRIKES+=(0); done
STOP_STRIKE_LIMIT=3

while :; do
  # One `ps` per poll for every child, not one per child.
  PS_STATES="$(ps -o pid=,state= $(for p in "${PIDS[@]}"; do printf -- '-p %s ' "$p"; done) 2>/dev/null || true)"
  for ((i = 0; i < ${#PIDS[@]}; i++)); do
    if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
      status=0
      wait "${PIDS[$i]}" 2>/dev/null || status=$?
      echo >&2
      echo "[start_lan] ERROR: ${PID_LABELS[$i]} exited (status $status)." >&2
      echo "[start_lan] Stopping the rest: half a stack serves stale code without saying so." >&2
      exit 1
    fi
    # The state letter carries flags -- a stopped, niced process reads "TN" --
    # so match the prefix. Only T: Linux's "t" is a tracing stop, which means
    # somebody attached a debugger on purpose.
    child_state="$(echo "$PS_STATES" | awk -v p="${PIDS[$i]}" '$1 == p { print $2 }')"
    case "$child_state" in
      T*)
        STOP_STRIKES[$i]=$(( ${STOP_STRIKES[$i]} + 1 ))
        if [ "${STOP_STRIKES[$i]}" -ge "$STOP_STRIKE_LIMIT" ]; then
          echo >&2
          echo "[start_lan] ERROR: ${PID_LABELS[$i]} has been STOPPED (state $child_state) for" >&2
          echo "[start_lan] ${STOP_STRIKE_LIMIT}s. It still holds its port but runs nothing, and it will not" >&2
          echo "[start_lan] act on SIGTERM until resumed (kill -CONT ${PIDS[$i]})." >&2
          echo "[start_lan] Stopping the rest: a port held by a stopped process serves nothing." >&2
          exit 1
        fi
        ;;
      *) STOP_STRIKES[$i]=0 ;;
    esac
  done
  sleep 1
done
