"""Per-DUT cache of the last successful channel recommendation.

The off-channel site survey is slow and holds the serial capture gate, so it is
only ever run on demand (the connect-time prescan or a manual Re-scan, both via
`GET /api/wifi/channel-recommendation`). This module remembers the *result* of
each such successful run per DUT so read-only surfaces — the Overview mini-card
and the Fleet grid — can show the latest per-band recommendation without
triggering a new scan (no gate usage).

Only the small recommendation rows and their capture timestamp are kept; the
bulky neighbor/vap lists are intentionally dropped (those are for the Site
Survey page, which fetches them live). The cache is in-memory and process-wide,
mirroring the rest of the survey path (nothing here is persisted to disk); a
stale entry survives serial close on purpose so the UI can show "scanned Nm ago"
against the last known result.
"""

from __future__ import annotations

from threading import Lock

# dut_id -> {"recommendations": [...], "captured_at": "..."}
_last: dict[str, dict] = {}
_lock = Lock()


def remember_recommendation(dut_id: str, recommendations: list[dict], captured_at: str) -> None:
    """Store the result of a successful channel-recommendation run for a DUT."""
    with _lock:
        _last[dut_id] = {"recommendations": recommendations, "captured_at": captured_at}


def last_recommendation(dut_id: str) -> dict | None:
    """Return the last remembered recommendation for a DUT, or None if never run."""
    with _lock:
        return _last.get(dut_id)


def clear() -> None:
    """Drop all cached recommendations (test helper)."""
    with _lock:
        _last.clear()
