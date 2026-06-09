#!/usr/bin/env bash
#
# One-click LAN launcher for the DUT monitoring dashboard.
# Starts the FastAPI backend (:8000) and the Vite frontend (:5173), both bound
# to 0.0.0.0 so any machine on the LAN can open the dashboard.
#
# Usage (from anywhere):
#   ./scripts/start_lan.sh
#
# Open in a browser on the same LAN:
#   http://<this-host-ip>:5173
#
# Overridable via env vars:
#   BIND_HOST (default 0.0.0.0) · BACKEND_PORT (default 8000) · FRONTEND_PORT (default 5173)
#   NOTE: the Vite proxy targets 127.0.0.1:8000 (dut-dashboard/frontend/vite.config.ts).
#         If you change BACKEND_PORT you must update that proxy target too.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/dut-dashboard/backend"
FRONTEND_DIR="$ROOT_DIR/dut-dashboard/frontend"

BIND_HOST="${BIND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

# --- Python venv + backend deps ---------------------------------------------
if [ ! -d "$ROOT_DIR/.venv" ]; then
  echo "[start_lan] creating Python venv (.venv)..."
  python3 -m venv "$ROOT_DIR/.venv"
fi
# shellcheck disable=SC1091
source "$ROOT_DIR/.venv/bin/activate"
echo "[start_lan] installing backend deps..."
pip install -q -r "$ROOT_DIR/requirements.txt"

# --- Frontend deps ----------------------------------------------------------
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "[start_lan] installing frontend deps (npm install)..."
  (cd "$FRONTEND_DIR" && npm install)
fi

# --- Stop both children on exit ---------------------------------------------
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
echo "[start_lan] backend  -> http://${BIND_HOST}:${BACKEND_PORT}"
(cd "$BACKEND_DIR" && exec python3 -m uvicorn app.main:app --host "$BIND_HOST" --port "$BACKEND_PORT") &
PIDS+=($!)

# --- Frontend ---------------------------------------------------------------
echo "[start_lan] frontend -> http://${BIND_HOST}:${FRONTEND_PORT}   <-- open this on the LAN"
(cd "$FRONTEND_DIR" && exec npm run dev -- --host "$BIND_HOST" --port "$FRONTEND_PORT") &
PIDS+=($!)

echo "[start_lan] both running. Press Ctrl-C to stop."
wait
