"""Persist the DUT context captured at connect time, and select it back by
session window.

Three kinds of "what the site looked like when we picked this DUT up" snapshot
are written as a timestamped JSON + CSV pair, generalising the P68 site-survey
snapshot idiom:

* ``site-survey``     — the neighbour scan (``iw scan`` per VAP). Written by
  :mod:`app.services.survey_snapshot` and left in its original directory; this
  module only reads it back.
* ``wifi-clients``    — the association tables (``wlanconfig <vap> list``).
* ``ssid-capability`` — the DUT's own VAP configuration.

The last two are *also* collected on every sysMon step, so the connect-time
capture is a fixed arrival reference point, **not** the primary source — the
log's time series is strictly richer. ``iw scan`` is the one thing sysMon never
runs, so the site survey genuinely exists nowhere else.

Files are named ``<kind>-<dut>-<YYYYmmdd-HHMMSS>.{json,csv}``. DUT ids may
contain hyphens, so the trailing ``\\d{8}-\\d{6}`` timestamp anchors the parse
back into a dut id.

Selection is by **session window**, never "newest": the context bundled with a
session log is the captures taken between that log's own start (parsed from its
filename) and its last write. An empty window yields nothing — handing a
week-old log today's survey is worse than handing it none.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from app.config import CONTEXT_DIR, SURVEY_SNAPSHOT_DIR

logger = logging.getLogger(__name__)

SITE_SURVEY = "site-survey"
WIFI_CLIENTS = "wifi-clients"
SSID_CAPABILITY = "ssid-capability"

# Where each kind lives. Site surveys keep the P68 directory (not migrated);
# the newer kinds get one subdirectory each under CONTEXT_DIR.
_KIND_DIRS: dict[str, Path] = {
    SITE_SURVEY: SURVEY_SNAPSHOT_DIR,
    WIFI_CLIENTS: CONTEXT_DIR / WIFI_CLIENTS,
    SSID_CAPABILITY: CONTEXT_DIR / SSID_CAPABILITY,
}

KINDS: tuple[str, ...] = tuple(_KIND_DIRS)

# Flat CSV view of each kind, for spreadsheet users. List-valued fields are
# space-joined; the JSON sibling keeps the full structure.
_CSV_COLUMNS: dict[str, list[str]] = {
    SITE_SURVEY: ["band", "channel", "ssid", "bssid", "signal_dbm", "security"],
    WIFI_CLIENTS: [
        "iface",
        "band",
        "ssid",
        "mac",
        "vendor",
        "aid",
        "channel",
        "txrate",
        "rxrate",
        "rssi",
        "phymode",
        "width",
        "assoc_time",
    ],
    SSID_CAPABILITY: [
        "iface",
        "ssid",
        "bssid",
        "band",
        "freq_mhz",
        "channel",
        "channel_width",
        "generation",
        "security",
        "category",
        "pmf",
        "akm",
        "pairwise_cipher",
        "group_mgmt_cipher",
        "dot11k",
        "dot11v",
        "dot11r",
    ],
}

_TS_FMT = "%Y%m%d-%H%M%S"
# <kind>-<dut>-<YYYYmmdd-HHMMSS>.<ext>; the fixed-width timestamp disambiguates
# a hyphenated dut id.
_NAME_RE = re.compile(r"^(?P<dut>.+)-(?P<ts>\d{8}-\d{6})\.(?P<ext>json|csv)$")
# dut-session-[<label>-]<YYYYmmdd-HHMMSS>.log — the label is optional (the
# default DUT has none) and may itself contain hyphens.
_SESSION_RE = re.compile(r"^dut-session-(?:.+-)?(?P<ts>\d{8}-\d{6})\.log$")


def dir_for(kind: str) -> Path:
    """Directory holding one kind's snapshots. Raises KeyError for unknown kinds."""
    return _KIND_DIRS[kind]


def _csv_cell(value) -> str | None:
    """Flatten one JSON field into a CSV cell (lists become space-joined)."""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return value


def write_capture(
    kind: str,
    dut_id: str,
    payload: dict,
    rows: list[dict],
    captured_at: str,
) -> list[Path]:
    """Write one capture as a JSON + CSV pair and return the two paths.

    ``payload`` is stored verbatim (plus dut_id/captured_at); ``rows`` is the
    flat table rendered into the CSV using the kind's column list. Raising is
    fine — every caller wraps this so a write failure never fails the capture it
    came from.
    """
    columns = _CSV_COLUMNS[kind]
    directory = _KIND_DIRS[kind]
    directory.mkdir(parents=True, exist_ok=True)

    base = f"{kind}-{dut_id}-{datetime.now().strftime(_TS_FMT)}"
    json_path = directory / f"{base}.json"
    csv_path = directory / f"{base}.csv"

    json_path.write_text(
        json.dumps({"dut_id": dut_id, "kind": kind, "captured_at": captured_at, **payload}, indent=2),
        encoding="utf-8",
    )

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(row.get(col)) for col in columns])

    return [json_path, csv_path]


def write_clients(dut_id: str, clients: list[dict], vaps: list[dict], captured_at: str) -> list[Path]:
    """Persist a Wi-Fi client capture (association tables + the VAPs scanned)."""
    return write_capture(
        WIFI_CLIENTS, dut_id, {"clients": clients, "vaps": vaps}, clients, captured_at
    )


def write_capability(dut_id: str, ssids: list[dict], captured_at: str) -> list[Path]:
    """Persist an SSID capability capture (the DUT's own VAP configuration)."""
    return write_capture(SSID_CAPABILITY, dut_id, {"ssids": ssids}, ssids, captured_at)


def snapshot_entries(kinds: tuple[str, ...] = KINDS) -> list[dict]:
    """Every well-formed snapshot file as {kind,dut,ts,ext,path}.

    Built once and passed around so listing N session logs does not re-scan the
    snapshot directories N times.
    """
    out: list[dict] = []
    for kind in kinds:
        directory = _KIND_DIRS[kind]
        if not directory.is_dir():
            continue
        prefix = f"{kind}-"
        for path in directory.iterdir():
            if not path.name.startswith(prefix) or not path.is_file():
                continue
            m = _NAME_RE.match(path.name[len(prefix) :])
            if m:
                out.append({"kind": kind, "dut": m["dut"], "ts": m["ts"], "ext": m["ext"], "path": path})
    return out


def list_snapshots(kinds: tuple[str, ...] = (WIFI_CLIENTS, SSID_CAPABILITY)) -> list[dict]:
    """Snapshot files as {kind,name,size,mtime}, newest first (for /api/logs).

    Site surveys are excluded by default: they already have their own Downloads
    table from P68 and listing them twice would just be confusing.
    """
    items: list[dict] = []
    for entry in snapshot_entries(kinds):
        path: Path = entry["path"]
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "kind": entry["kind"],
                "name": path.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return sorted(items, key=lambda item: item["mtime"], reverse=True)


def session_window(log_path: Path) -> tuple[str, str] | None:
    """``(start, end)`` timestamps for a session log, or None if unusable.

    Start comes from the log's own filename (when the session was opened), end
    from its last write. Both are ``YYYYmmdd-HHMMSS`` strings — fixed width and
    zero padded, so plain string comparison orders them correctly. Returns None
    for a name that is not a session log, or a file that cannot be stat'ed;
    callers treat that as "no context", never as "everything".
    """
    m = _SESSION_RE.match(log_path.name)
    if not m:
        return None
    try:
        end = datetime.fromtimestamp(log_path.stat().st_mtime).strftime(_TS_FMT)
    except OSError:
        return None
    start = m["ts"]
    if end < start:
        # A log whose mtime predates its own name (clock change, restored copy):
        # the window is degenerate, so treat the start instant as the whole of it.
        end = start
    return start, end


def select_entries_for_session(
    log_path: Path,
    dut_id: str | None = None,
    entries: list[dict] | None = None,
) -> list[dict]:
    """`snapshot_entries` rows captured while `log_path`'s session was recording.

    Optionally narrowed to one DUT. Sorted by kind then timestamp for a stable
    bundle layout. Empty beats wrong: an unparseable log name, a window with no
    captures, or a missing directory all yield ``[]`` — never a "newest"
    fallback, which is how a ZIP for last week's log used to end up carrying
    today's survey.
    """
    window = session_window(log_path)
    if window is None:
        return []
    start, end = window
    if entries is None:
        entries = snapshot_entries()
    selected = [
        e for e in entries if start <= e["ts"] <= end and (dut_id is None or e["dut"] == dut_id)
    ]
    selected.sort(key=lambda e: (e["kind"], e["ts"], e["ext"]))
    return selected


def select_for_session(
    log_path: Path,
    dut_id: str | None = None,
    entries: list[dict] | None = None,
) -> list[Path]:
    """Paths of every context file captured while `log_path`'s session ran."""
    return [e["path"] for e in select_entries_for_session(log_path, dut_id, entries)]


def bundle_context(dest_dir: Path, log_path: Path, dut_id: str | None = None) -> list[Path]:
    """Copy a session's context into ``dest_dir/context/<kind>/``.

    Returns the paths written (empty when the session captured nothing, in which
    case no directory is created either). Best-effort: a copy error is logged and
    never propagated, because every caller is a download or an analyze that must
    still succeed without its context.
    """
    written: list[Path] = []
    try:
        for entry in select_entries_for_session(log_path, dut_id):
            target_dir = dest_dir / "context" / entry["kind"]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / entry["path"].name
            shutil.copy2(entry["path"], target)
            written.append(target)
    except Exception:  # noqa: BLE001 — bundling is best-effort
        logger.exception("failed to bundle session context for %s", log_path.name)
    return written
