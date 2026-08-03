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
# Extra offline post-processing tools run over a session directory *after* the
# primary analyzer, in this order. Both invocation points (the Download flow in
# serial_api.run_analyzer_for_session and the Analyze flow in AnalyzerService)
# read this one list, so a new tool is wired by editing exactly one line.
#
# analyzer3.py is deliberately NOT listed: it is ANALYZER_SCRIPT above — the
# fail-hard primary, and the module attribute the download-workflow tests patch
# to point the loop at a stub. It used to head a per-module copy of this list,
# where it was pure decoration (both call sites sliced it off and used
# ANALYZER_SCRIPT instead), so editing that entry changed nothing.
#
# Everything here is best-effort: a missing tool is skipped, and a failing one
# is logged and reported in context/capture-report.txt, never fatal.
OFFLINE_TOOL_NAMES: tuple[str, ...] = ("wifi_timeseries.py", "context_render.py")
# Persisted site-survey snapshots (json+csv pairs; runtime state, gitignored).
SURVEY_SNAPSHOT_DIR = LOG_DIR / "site-surveys"
# Connect-time DUT context captures other than the site survey (Wi-Fi clients,
# SSID capability), one subdirectory per kind. Site surveys keep their original
# directory above so the P68 files stay where users already look for them.
CONTEXT_DIR = LOG_DIR / "context"
# Per-session context copied out for an analyzed log. Deliberately NOT under
# ANALYZER_OUTPUT_DIR, which analyzer_service clears on every run.
CONTEXT_BUNDLE_DIR = LOG_DIR / "context-bundles"

# Workspace module (LAN file-sharing + bulletin). Uploader/author stay free
# text. All runtime state lives under data/ (gitignored).
DATA_DIR = BASE_DIR / "data"
WORKSPACE_DB = DATA_DIR / "workspace.db"
UPLOAD_DIR = DATA_DIR / "uploads"
# HMAC key for session cookies; generated on first use, never committed.
SESSION_SECRET_FILE = DATA_DIR / "session_secret"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTENSIONS = {
    # Encrypted firmware images for the management-API transport (P72b). The web
    # UI transport takes ".sig" below; the DUT accepts only its own type on each,
    # so both extensions have to be uploadable.
    "bin",
    "csv",
    "gif",
    "jpeg",
    "jpg",
    "json",
    "log",
    "pcap",
    "pcapng",
    "pdf",
    "png",
    # Customer-signed firmware images for the admin upgrade flow (P72b). Real
    # ones run 32-38 MB, comfortably inside MAX_UPLOAD_BYTES.
    "sig",
    "txt",
}
# Host-side Wi-Fi survey (Source B for SSID capability reconciliation).
# Set SURVEY_WIFI_IFACE=wlan0 (or similar) to enable; absent → available:false.
import os as _os
SURVEY_WIFI_IFACE: str | None = _os.getenv("SURVEY_WIFI_IFACE")

# Production build of the frontend (npm run build). Served by the backend at
# "/" only when it exists; in dev it is absent and Vite serves the UI instead.
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
