from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"
SNAPSHOT_FILE = LOG_DIR / "snapshots.jsonl"
# Persisted list of dynamically-registered DUTs (runtime state; gitignored).
DUTS_FILE = LOG_DIR / "duts.json"


def snapshot_file_for(dut_id: str) -> Path:
    """Per-DUT snapshot ring file. The default DUT keeps the original file so its
    captured history keeps backfilling; others get their own."""
    if dut_id == "default":
        return SNAPSHOT_FILE
    return LOG_DIR / f"snapshots-{dut_id}.jsonl"
TOOLS_DIR = BASE_DIR / "tools"
ANALYZER_SCRIPT = TOOLS_DIR / "analyzer3.py"
ANALYZER_OUTPUT_DIR = LOG_DIR / "analyzer_output"
# Production build of the frontend (npm run build). Served by the backend at
# "/" only when it exists; in dev it is absent and Vite serves the UI instead.
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
