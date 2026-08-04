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

# Recommendation-state tombstone: "the recommendation ran at T and came back
# empty". Needed because an all-empty survey writes no snapshot at all (§7 — an
# empty container in a bundle is indistinguishable from a measurement of zero),
# which used to leave restore_cache with nothing newer than the last non-empty
# snapshot and so resurrected a stale band badge on every restart.
#
# **It is a state, not a measurement**, and carries the same invisibility
# guarantees as C1's `.skip.json` markers: the name has no `-<ts>` segment
# before its extension, so `_NAME_RE` here and `_NAME_RE`/`_SKIP_RE` in
# context_snapshot all fail to match it. It therefore never reaches
# `_snapshot_names()`, `latest_for()`, the Downloads listing,
# `snapshot_entries()`, the log-ZIP bundle, or context_render.
#
# **Lifecycle: exactly one file per DUT, overwritten in place.** A timestamped
# series was the alternative and is rejected deliberately — the *history* of
# "the recommendation was empty" has no analytic value (the sysMon log is the
# time series), and this tree already carries two unbounded accumulations
# (survey snapshots and C2's scan volume). A third one needing a prune policy
# buys nothing. Stale tombstones are never deleted either: recency is settled
# by comparing the `ts` field against snapshot stamps, which removes the whole
# class of delete-failed-halfway bugs.
_EMPTY_STATE_SUFFIX = ".empty.json"
_EMPTY_STATE_RE = re.compile(r"^site-survey-(?P<dut>.+)\.empty\.json$")


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

    **A survey that observed nothing at all writes no snapshot**, and a survey
    with VAPs but no neighbors keeps its JSON and writes no CSV. Same rule, same
    reason as context_snapshot.write_capture (contract §7): a header-only
    neighbor CSV sitting in a bundle is indistinguishable from a real
    measurement of zero. Applied per artifact — zero neighbors *is* a reading
    when the scan itself ran, so the JSON that records it stays.

    A *computed* empty recommendation additionally drops a recommendation-state
    tombstone, which is not a snapshot and is not covered by that rule. Without
    it the all-empty computed case (recommendation ran, DUT has no own VAPs, and
    the scan saw nothing either) would persist nothing at all, and the next
    restart would restore the last non-empty snapshot and put a stale band badge
    back on Overview/Fleet. See :func:`write_empty_state`.
    """
    written: list[Path] = []
    if recommendation_computed and not recommendations:
        written.append(write_empty_state(dut_id, captured_at))
    if not recommendations and not neighbors and not vaps:
        return written
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

    written.append(json_path)
    if not neighbors:
        return written

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_CSV_COLUMNS)
        for n in neighbors:
            writer.writerow([n.get(col) for col in _CSV_COLUMNS])

    written.append(csv_path)
    return written


def write_empty_state(dut_id: str, captured_at: str) -> Path:
    """Record that the recommendation ran for ``dut_id`` and came back empty.

    One file per DUT, overwritten in place (see ``_EMPTY_STATE_SUFFIX`` for why
    this is a single latest-state file rather than a timestamped series, and why
    it is never deleted). It carries its own ``ts`` in the snapshots' stamp
    format so :func:`restore_cache` can order it against them with the same
    string comparison it already uses.

    This is deliberately *not* a snapshot: it holds no observations, is
    unmatchable by the snapshot name patterns, and so never appears in a
    listing, a bundle or a render. Raising is fine — the caller wraps it.
    """
    SURVEY_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SURVEY_SNAPSHOT_DIR / f"site-survey-{dut_id}{_EMPTY_STATE_SUFFIX}"
    path.write_text(
        json.dumps(
            {
                "dut_id": dut_id,
                "state": "recommendation-empty",
                "recommendation_computed": True,
                "recommendations": [],
                "captured_at": captured_at,
                "ts": datetime.now().strftime("%Y%m%d-%H%M%S"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _read_empty_state(dut_id: str) -> tuple[str, str] | None:
    """``(ts, captured_at)`` from a DUT's tombstone, or None if there is none.

    Best-effort like everything else on the restore path: an unreadable or
    malformed tombstone is treated as absent rather than allowed to block
    startup or to displace a perfectly good snapshot.
    """
    path = SURVEY_SNAPSHOT_DIR / f"site-survey-{dut_id}{_EMPTY_STATE_SUFFIX}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data["ts"]
        captured_at = data["captured_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(ts, str) or not isinstance(captured_at, str):
        return None
    return ts, captured_at


def _empty_state_duts() -> set[str]:
    """Every DUT that has a tombstone (they may have no snapshot at all)."""
    if not SURVEY_SNAPSHOT_DIR.is_dir():
        return set()
    out: set[str] = set()
    for path in SURVEY_SNAPSHOT_DIR.iterdir():
        m = _EMPTY_STATE_RE.match(path.name)
        if m and path.is_file():
            out.add(m["dut"])
    return out


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

    The all-empty computed case writes no snapshot at all (§7), so its state
    lives in a tombstone instead; the two are ordered against each other by
    timestamp and the newer one wins. A DUT with only a tombstone restores an
    empty recommendation; a DUT with neither restores nothing, exactly as
    before this feature existed.

    Best-effort: a missing/corrupt file is skipped so one bad snapshot never
    blocks startup.
    """
    stamps_by_dut: dict[str, list[str]] = {}
    for dut, ts, ext, _path in _snapshot_names():
        if ext == "json":
            stamps_by_dut.setdefault(dut, []).append(ts)

    for dut in set(stamps_by_dut) | _empty_state_duts():
        newest = _newest_computed_snapshot(dut, stamps_by_dut.get(dut, []))
        tombstone = _read_empty_state(dut)
        # Newest computed *state* wins, and a tombstone is a state. Ties go to
        # the snapshot: the only way to tie is one call writing both, and then
        # they agree (the snapshot's recommendation list is empty too).
        if tombstone is not None and (newest is None or tombstone[0] > newest[0]):
            remember_recommendation(dut, [], tombstone[1])
        elif newest is not None:
            remember_recommendation(dut, newest[1], newest[2])


def _newest_computed_snapshot(dut: str, stamps: list[str]) -> tuple[str, list[dict], str] | None:
    """``(ts, recommendations, captured_at)`` of the DUT's newest computed
    snapshot, or None when it has none.

    Walks newest-first and passes over not-computed snapshots (bare
    site-survey write-throughs) and unreadable ones alike.
    """
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
            return ts, recommendations, captured_at
    return None
