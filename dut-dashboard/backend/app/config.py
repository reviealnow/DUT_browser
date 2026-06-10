from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"
SNAPSHOT_FILE = LOG_DIR / "snapshots.jsonl"
TOOLS_DIR = BASE_DIR / "tools"
ANALYZER_SCRIPT = TOOLS_DIR / "analyzer3.py"
ANALYZER_OUTPUT_DIR = LOG_DIR / "analyzer_output"
# Production build of the frontend (npm run build). Served by the backend at
# "/" only when it exists; in dev it is absent and Vite serves the UI instead.
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
