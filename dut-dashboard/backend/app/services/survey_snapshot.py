"""Persist site-survey results to disk so they survive a backend restart.

The channel-recommendation endpoint echoes the full survey (recommendations +
neighbors + vaps) but only the small recommendation rows are cached in memory
(see survey_cache). This module additionally writes each successful survey to a
timestamped JSON + CSV pair under SURVEY_SNAPSHOT_DIR so users can download the
raw neighbor table from the Downloads page and bundle it into a log ZIP, and so
the in-memory recommendation cache can be rebuilt on startup (Overview / Fleet
band badges survive a restart with no new off-channel scan).

Files are named ``site-survey-<dut>-<YYYYmmdd-HHMMSS>.{json,csv}``. DUT ids may
contain hyphens, so the trailing ``\\d{8}-\\d{6}`` timestamp is the disambiguator
when parsing a name back into its dut id.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from app.config import SURVEY_SNAPSHOT_DIR
from app.services.survey_cache import remember_recommendation

logger = logging.getLogger(__name__)

# CSV columns, in order, pulled straight from each neighbor dict.
_CSV_COLUMNS = ["band", "channel", "ssid", "bssid", "signal_dbm", "security"]

# site-survey-<dut>-<YYYYmmdd-HHMMSS>.<ext> — the timestamp anchors the split so a
# hyphenated dut id parses unambiguously.
_NAME_RE = re.compile(r"^site-survey-(?P<dut>.+)-(?P<ts>\d{8}-\d{6})\.(?P<ext>json|csv)$")


def write_snapshot(
    dut_id: str,
    recommendations: list[dict],
    neighbors: list[dict],
    vaps: list[dict],
    captured_at: str,
    recommendation_computed: bool = True,
) -> list[Path]:
    """Write a JSON (+ CSV) snapshot for one survey. Returns the paths written.

    The JSON holds the full payload (for restore + programmatic use); the CSV is
    a flat neighbor table for spreadsheet users. Raising is fine — the caller
    wraps this so a write failure never fails the originating request.

    ``recommendation_computed`` records whether ``recommendations`` is a result
    or an absence, because an empty list cannot tell those apart and
    :func:`restore_cache` has to. ``/api/wifi/channel-recommendation`` runs the
    recommendation and passes True even when it comes back empty (a DUT with no
    own VAPs is a real, current answer); the bare ``/api/wifi/site-survey``
    write-through never runs it at all and passes False. Defaulting to True is
    what makes pre-C2 snapshots readable: the channel-recommendation path was
    the only writer that existed, and it always computed.

    **A survey that observed nothing at all writes nothing and returns ``[]``**,
    and a survey with VAPs but no neighbors keeps its JSON and writes no CSV.
    Same rule, same reason as context_snapshot.write_capture (contract §7): a
    header-only neighbor CSV sitting in a bundle is indistinguishable from a
    real measurement of zero. Applied per artifact — zero neighbors *is* a
    reading when the scan itself ran, so the JSON that records it stays.
    """
    if not recommendations and not neighbors and not vaps:
        return []
    SURVEY_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"site-survey-{dut_id}-{stamp}"
    json_path = SURVEY_SNAPSHOT_DIR / f"{base}.json"
    csv_path = SURVEY_SNAPSHOT_DIR / f"{base}.csv"

    json_path.write_text(
        json.dumps(
            {
                "dut_id": dut_id,
                "captured_at": captured_at,
                "recommendations": recommendations,
                "recommendation_computed": recommendation_computed,
                "neighbors": neighbors,
                "vaps": vaps,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if not neighbors:
        return [json_path]

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_CSV_COLUMNS)
        for n in neighbors:
            writer.writerow([n.get(col) for col in _CSV_COLUMNS])

    return [json_path, csv_path]


def _snapshot_names() -> list[tuple[str, str, str, Path]]:
    """Return (dut, ts, ext, path) for every well-formed snapshot file."""
    if not SURVEY_SNAPSHOT_DIR.is_dir():
        return []
    out: list[tuple[str, str, str, Path]] = []
    for path in SURVEY_SNAPSHOT_DIR.iterdir():
        m = _NAME_RE.match(path.name)
        if m and path.is_file():
            out.append((m["dut"], m["ts"], m["ext"], path))
    return out


def latest_for(dut_id: str) -> list[Path]:
    """Newest json+csv pair for one DUT (both, existing), or [] if none."""
    stamps = sorted(
        (ts for dut, ts, ext, _ in _snapshot_names() if dut == dut_id and ext == "json"),
        reverse=True,
    )
    if not stamps:
        return []
    base = SURVEY_SNAPSHOT_DIR / f"site-survey-{dut_id}-{stamps[0]}"
    return [p for p in (base.with_suffix(".json"), base.with_suffix(".csv")) if p.is_file()]


def list_snapshots() -> list[dict]:
    """All snapshot files as {name,size,mtime}, newest first (for /api/logs)."""
    items: list[dict] = []
    for _dut, _ts, _ext, path in _snapshot_names():
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return sorted(items, key=lambda item: item["mtime"], reverse=True)


def restore_cache() -> None:
    """Feed each DUT's newest *computed* recommendation back into survey_cache.

    Newest-first with a fallback, not strictly newest: /api/wifi/site-survey now
    persists its scan too, and that endpoint never runs the recommendation (it
    has no SSID-capability capture to reconcile against and must not spend a
    second serial round-trip getting one). Restoring one of those over a real
    recommendation would blank the Overview/Fleet band badges after every
    restart — the exact regression the cache exists to prevent.

    The skip is keyed on ``recommendation_computed``, **not** on the list being
    empty. An empty list is a legitimate current answer when the recommendation
    actually ran and the DUT has no own VAPs; skipping it on emptiness alone
    would resurrect a stale badge from an older snapshot and keep showing it
    indefinitely. So: a *computed* snapshot wins on recency even when empty, and
    only a not-computed one is passed over.

    A snapshot with no flag is treated as computed. Before C2 the
    channel-recommendation path was the only writer of survey snapshots and it
    always computes, so every legacy file on disk is a computed one.

    Best-effort: a missing/corrupt file is skipped so one bad snapshot never
    blocks startup.
    """
    stamps_by_dut: dict[str, list[str]] = {}
    for dut, ts, ext, _path in _snapshot_names():
        if ext == "json":
            stamps_by_dut.setdefault(dut, []).append(ts)

    for dut, stamps in stamps_by_dut.items():
        for ts in sorted(stamps, reverse=True):
            json_path = SURVEY_SNAPSHOT_DIR / f"site-survey-{dut}-{ts}.json"
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                recommendations = data["recommendations"]
                captured_at = data["captured_at"]
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("skipping unreadable survey snapshot: %s", json_path.name)
                continue
            if data.get("recommendation_computed", True) is False:
                continue
            if isinstance(recommendations, list) and isinstance(captured_at, str):
                remember_recommendation(dut, recommendations, captured_at)
                break
