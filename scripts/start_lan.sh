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
echo "[start_lan] backend  -> http://${BIND_HOST}:${BACKEND_PORT}"
(cd "$BACKEND_DIR" && exec python3 -m uvicorn app.main:app --host "$BIND_HOST" --port "$BACKEND_PORT") &
PIDS+=($!)

if [ "$PROD" -eq 1 ]; then
  echo "[start_lan] PROD: open http://${BIND_HOST}:${BACKEND_PORT}   <-- UI + API + WS on one port"
else
  echo "[start_lan] frontend -> http://${BIND_HOST}:${FRONTEND_PORT}   <-- open this on the LAN"
  (cd "$FRONTEND_DIR" && exec npm run dev -- --host "$BIND_HOST" --port "$FRONTEND_PORT") &
  PIDS+=($!)
fi

echo "[start_lan] running. Press Ctrl-C to stop."
wait
