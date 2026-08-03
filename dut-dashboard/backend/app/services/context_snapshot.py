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

A capture that observed nothing writes **no file at all** and leaves a
``.skip.json`` marker instead, and ``bundle_context`` turns markers plus the
files it copied into ``context/capture-report.txt``. Absence with a stated
reason is honest; a header-only CSV sitting in a bundle is indistinguishable
from a real measurement of zero, and a 40-hour run shipped three of them.
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
# Same stem, so a marker lands in its session's window exactly like the capture
# it stands in for; the extension is deliberately one _NAME_RE cannot match, so
# markers never reach snapshot_entries(), the Downloads listing, or the bundle.
_SKIP_EXT = ".skip.json"
_SKIP_RE = re.compile(r"^(?P<dut>.+)-(?P<ts>\d{8}-\d{6})\.skip\.json$")
# dut-session-[<label>-]<YYYYmmdd-HHMMSS>.log — the label is optional (the
# default DUT has none) and may itself contain hyphens.
_SESSION_RE = re.compile(r"^dut-session-(?:.+-)?(?P<ts>\d{8}-\d{6})\.log$")

# Bundle-side accounting for what context/ does and does not contain.
CAPTURE_REPORT_NAME = "capture-report.txt"
# Reported for a kind with neither a capture nor a marker in the window: every
# log from before the feature existed, and every replay session.
NO_CAPTURE_REASON = "no capture recorded in this session window"


def dir_for(kind: str) -> Path:
    """Directory holding one kind's snapshots. Raises KeyError for unknown kinds."""
    return _KIND_DIRS[kind]


def _csv_cell(value) -> str | None:
    """Flatten one JSON field into a CSV cell (lists become space-joined)."""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return value


def payload_is_empty(payload: dict) -> bool:
    """True when a capture observed nothing at all.

    Every payload field is a list of observations (clients / vaps / ssids), so
    "nothing was observed" is exactly "no field holds anything". A predicate
    rather than an inline check, so the guard and its test agree on the
    definition instead of each carrying their own.
    """
    return not any(payload.values())


def write_capture(
    kind: str,
    dut_id: str,
    payload: dict,
    rows: list[dict],
    captured_at: str,
) -> list[Path]:
    """Write one capture as a JSON + CSV pair and return the paths written.

    ``payload`` is stored verbatim (plus dut_id/captured_at); ``rows`` is the
    flat table rendered into the CSV using the kind's column list. Raising is
    fine — every caller wraps this so a write failure never fails the capture it
    came from.

    **An empty payload writes nothing and returns ``[]``.** The container is the
    bug, not the absence: an `[]`-body JSON and a header-only CSV are
    indistinguishable from a real measurement of zero once they are sitting in a
    bundle, and a starved serial line at connect time shipped exactly that.
    Callers record the reason via :func:`write_skip`, which reaches the operator
    as ``context/capture-report.txt``.

    The guard is per artifact, per contract §7: a capture that saw VAPs but no
    associated clients keeps its JSON — zero clients is then a real reading —
    and still writes no client CSV, because there is no row to put in it.
    """
    columns = _CSV_COLUMNS[kind]
    directory = _KIND_DIRS[kind]
    if payload_is_empty(payload):
        return []
    directory.mkdir(parents=True, exist_ok=True)

    base = f"{kind}-{dut_id}-{datetime.now().strftime(_TS_FMT)}"
    json_path = directory / f"{base}.json"
    csv_path = directory / f"{base}.csv"

    json_path.write_text(
        json.dumps({"dut_id": dut_id, "kind": kind, "captured_at": captured_at, **payload}, indent=2),
        encoding="utf-8",
    )
    written = [json_path]

    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_csv_cell(row.get(col)) for col in columns])
        written.append(csv_path)

    return written


def write_skip(kind: str, dut_id: str, reason: str, captured_at: str) -> Path:
    """Record that a capture produced no snapshot, and why.

    The marker shares a snapshot's ``<kind>-<dut>-<ts>`` stem so the session
    window selects it the same way, but carries ``.skip.json`` — it is an
    explanation, never data, and must not be listed or bundled as a capture.
    Raising is fine; the caller keeps a failed connect from ever surfacing.
    """
    directory = _KIND_DIRS[kind]
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = directory / f"{kind}-{dut_id}-{now.strftime(_TS_FMT)}{_SKIP_EXT}"
    path.write_text(
        json.dumps(
            {
                "kind": kind,
                "dut_id": dut_id,
                "reason": reason,
                "captured_at": captured_at,
                "skipped_at": now.isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_clients(dut_id: str, clients: list[dict], vaps: list[dict], captured_at: str) -> list[Path]:
    """Persist a Wi-Fi client capture (association tables + the VAPs scanned)."""
    return write_capture(
        WIFI_CLIENTS, dut_id, {"clients": clients, "vaps": vaps}, clients, captured_at
    )


def write_capability(dut_id: str, ssids: list[dict], captured_at: str) -> list[Path]:
    """Persist an SSID capability capture (the DUT's own VAP configuration)."""
    return write_capture(SSID_CAPABILITY, dut_id, {"ssids": ssids}, ssids, captured_at)


def _scan(kinds: tuple[str, ...], pattern: re.Pattern[str]) -> list[dict]:
    """Files under each kind's directory whose name matches `pattern`.

    Shared by the snapshot index and the skip-marker index so the two can never
    disagree about which directories exist or how a hyphenated dut id parses;
    the pattern is the only difference between them.
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
            m = pattern.match(path.name[len(prefix) :])
            if m:
                out.append({"kind": kind, **m.groupdict(), "path": path})
    return out


def snapshot_entries(kinds: tuple[str, ...] = KINDS) -> list[dict]:
    """Every well-formed snapshot file as {kind,dut,ts,ext,path}.

    Built once and passed around so listing N session logs does not re-scan the
    snapshot directories N times. Skip markers are not snapshots and never
    appear here — `_NAME_RE` cannot match their extension.
    """
    return _scan(kinds, _NAME_RE)


def skip_entries(kinds: tuple[str, ...] = KINDS) -> list[dict]:
    """Every skip marker as {kind,dut,ts,path} — why a capture wrote nothing."""
    return _scan(kinds, _SKIP_RE)


def describe(entries: list[dict]) -> list[dict]:
    """Listing shape ``{kind,name,size,mtime}`` for already-selected entries.

    Shared by `list_snapshots` (everything) and by the per-session rows in
    /api/logs, so the flat Downloads table and a session's own context list can
    never describe the same file differently. A file that vanished between the
    scan and the stat is dropped rather than reported with a guessed size.
    """
    items: list[dict] = []
    for entry in entries:
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
    return items


def list_snapshots(kinds: tuple[str, ...] = (WIFI_CLIENTS, SSID_CAPABILITY)) -> list[dict]:
    """Snapshot files as {kind,name,size,mtime}, newest first (for /api/logs).

    Site surveys are excluded by default: they already have their own Downloads
    table from P68 and listing them twice would just be confusing.
    """
    return sorted(describe(snapshot_entries(kinds)), key=lambda item: item["mtime"], reverse=True)


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
    if entries is None:
        entries = snapshot_entries()
    selected = _within(entries, window, dut_id)
    selected.sort(key=lambda e: (e["kind"], e["ts"], e["ext"]))
    return selected


def _within(entries: list[dict], window: tuple[str, str], dut_id: str | None) -> list[dict]:
    """Index rows stamped inside `window`, optionally narrowed to one DUT."""
    start, end = window
    return [e for e in entries if start <= e["ts"] <= end and (dut_id is None or e["dut"] == dut_id)]


def select_skips_for_session(
    log_path: Path,
    dut_id: str | None = None,
    entries: list[dict] | None = None,
) -> list[dict]:
    """Skip markers recorded while `log_path`'s session was recording.

    Same window and the same "empty beats wrong" rule as the snapshots: a marker
    from another session explains nothing about this one.
    """
    window = session_window(log_path)
    if window is None:
        return []
    if entries is None:
        entries = skip_entries()
    selected = _within(entries, window, dut_id)
    selected.sort(key=lambda e: (e["kind"], e["ts"]))
    return selected


def select_for_session(
    log_path: Path,
    dut_id: str | None = None,
    entries: list[dict] | None = None,
) -> list[Path]:
    """Paths of every context file captured while `log_path`'s session ran."""
    return [e["path"] for e in select_entries_for_session(log_path, dut_id, entries)]


def _csv_row_count(path: Path) -> int:
    """Data rows in a snapshot CSV (header excluded), 0 if it cannot be read.

    Parsed rather than line-counted so a quoted field containing a newline
    counts as the one row it is.
    """
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return max(sum(1 for _ in csv.reader(handle)) - 1, 0)
    except OSError:
        return 0


def _skip_reason(entry: dict) -> tuple[str, str]:
    """``(reason, timestamp)`` from a skip marker, with honest fallbacks.

    An unreadable marker still proves the capture was attempted and produced
    nothing, so it reports that much rather than being dropped.
    """
    try:
        data = json.loads(entry["path"].read_text(encoding="utf-8"))
        reason = str(data.get("reason") or "").strip()
        stamp = str(data.get("skipped_at") or "").strip()
    except (OSError, ValueError):
        reason, stamp = "", ""
    if not stamp:
        stamp = datetime.strptime(entry["ts"], _TS_FMT).isoformat(timespec="seconds")
    return reason or "capture produced nothing (reason unreadable)", stamp


def capture_report_lines(entries: list[dict], skips: list[dict]) -> list[str]:
    """One line per snapshot kind, in the fixed contract §5 format.

    ``"<kind>: ok, <n> rows, <files>"`` when the session carries that kind,
    ``"<kind>: skipped — <reason>, <timestamp>"`` when it does not. Every kind
    gets a line whether or not anything happened to it: a kind quietly missing
    from the report is the same silence the empty snapshots used to create.
    """
    checked_at = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = []
    for kind in KINDS:
        captured = [e for e in entries if e["kind"] == kind]
        if captured:
            rows = sum(_csv_row_count(e["path"]) for e in captured if e.get("ext") == "csv")
            files = " ".join(e["path"].name for e in captured)
            lines.append(f"{kind}: ok, {rows} rows, {files}")
            continue
        marked = [s for s in skips if s["kind"] == kind]
        if marked:
            reason, stamp = _skip_reason(marked[-1])
        else:
            reason, stamp = NO_CAPTURE_REASON, checked_at
        lines.append(f"{kind}: skipped — {reason}, {stamp}")
    return lines


def bundle_context(dest_dir: Path, log_path: Path, dut_id: str | None = None) -> list[Path]:
    """Copy a session's context into ``dest_dir/context/<kind>/``.

    Also writes ``context/capture-report.txt`` accounting for all three kinds,
    so an operator opening the ZIP reads why a snapshot is missing instead of
    discovering the gap by hand. The report is written whenever the session has
    anything to account for — a capture or a skip marker; a session that never
    attempted one (every log from before P73, every replay) still produces no
    directory at all rather than a report saying "not captured" three times.

    Returns the paths written. Best-effort: a copy error is logged and never
    propagated, because every caller is a download or an analyze that must still
    succeed without its context.
    """
    written: list[Path] = []
    try:
        entries = select_entries_for_session(log_path, dut_id)
        skips = select_skips_for_session(log_path, dut_id)
        for entry in entries:
            target_dir = dest_dir / "context" / entry["kind"]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / entry["path"].name
            shutil.copy2(entry["path"], target)
            written.append(target)
        if entries or skips:
            report = dest_dir / "context" / CAPTURE_REPORT_NAME
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                "\n".join(capture_report_lines(entries, skips)) + "\n", encoding="utf-8"
            )
            written.append(report)
    except Exception:  # noqa: BLE001 — bundling is best-effort
        logger.exception("failed to bundle session context for %s", log_path.name)
    return written
