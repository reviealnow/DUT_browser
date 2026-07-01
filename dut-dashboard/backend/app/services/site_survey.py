"""DUT-side site survey + channel recommendation.

Source: `iw dev <vap> scan` run *on the DUT* (serial RPC via
`SerialWorker.capture_command`), one call per active VAP (radio). Verified on
AP6 840E (2026-07): the DUT's busybox `iw` emits the exact same text format as
the host-side `iw dev <iface> scan` that `wifi_survey.py` already parses (BSS
anchor / freq / signal / SSID / HT-VHT-HE-EHT capability blocks / RSN block) —
so this module reuses that parser instead of duplicating it. This is a third,
independent data source from `wifi_survey.get_wifi_survey` (host-side network
card) and `wifi_clients.get_ssid_capabilities` (DUT hostapd config); neither of
those is modified here.

Off-channel scans are slower than the other on-demand captures (a busy 5/6 GHz
VAP can return 20+ neighboring BSS): the default timeout is generous and the
scan still queues behind (never rejected by) `SerialWorker`'s single-capture
gate, same as every other `capture_command` caller.
"""

from __future__ import annotations

from datetime import datetime

from app.services.wifi_clients import discover_vaps
from app.services.wifi_survey import _parse_iw_scan as parse_iw_scan_text

SCAN_TIMEOUT_SEC = 20.0

# discover_vaps() (wifi_clients.py) labels bands "2.4G"/"5G"/"6G"; the reused
# iw-scan parser labels them "2.4GHz"/"5GHz"/"6GHz" (freq-derived, matches
# get_ssid_capabilities() too). Normalise the vaps list to the GHz form so a
# single site-survey response doesn't mix both spellings.
_BAND_TO_GHZ = {"2.4G": "2.4GHz", "5G": "5GHz", "6G": "6GHz"}

# Standard non-overlapping 2.4 GHz channels (20 MHz spacing); always considered
# as recommendation candidates even if a VAP happens to sit off this grid.
_NONOVERLAP_24G = (1, 6, 11)

# Rough signal-to-disruption weight: a strong neighbor on a channel matters a
# lot more than a barely-audible one. Buckets, not a full propagation model.
def _signal_weight(signal_dbm: float | None) -> float:
    if signal_dbm is None:
        return 1.0
    if signal_dbm >= -60:
        return 3.0
    if signal_dbm >= -75:
        return 2.0
    return 1.0


def get_site_survey(worker: "object") -> dict:  # SerialWorker — avoids circular import
    """Scan every active VAP's radio for neighboring BSS on the DUT.

    Returns {"vaps": [...], "neighbors": [ObservedNeighbor, ...], "captured_at": iso}.
    Each neighbor carries "iface" (which VAP's radio saw it) in addition to the
    usual iw-scan fields (bssid/ssid/band/channel/freq_mhz/signal_dbm/...).
    Raises RuntimeError (passed through) only when the serial port is not open.
    """
    from app.serial.serial_worker import SerialWorker  # local import avoids circular

    assert isinstance(worker, SerialWorker)

    iwconfig_text = worker.capture_command("iwconfig", timeout=6.0)
    vaps = discover_vaps(iwconfig_text)
    for vap in vaps:
        vap["band"] = _BAND_TO_GHZ.get(vap["band"], vap["band"])

    neighbors: list[dict] = []
    for vap in vaps:
        try:
            out = worker.capture_command(f"iw dev {vap['iface']} scan", timeout=SCAN_TIMEOUT_SEC)
        except RuntimeError:
            continue
        for neighbor in parse_iw_scan_text(out):
            neighbor["iface"] = vap["iface"]
            neighbors.append(neighbor)

    return {
        "vaps": vaps,
        "neighbors": neighbors,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


def channel_recommendation(neighbors: list[dict], own_vaps: list[dict]) -> list[dict]:
    """Pure function: recommend the least-occupied channel per band.

    Args:
        neighbors: ObservedNeighbor dicts from get_site_survey()["neighbors"].
        own_vaps:  SsidCapability dicts from get_ssid_capabilities() — supplies
                   each band's current channel and lets us exclude the DUT's
                   own BSSIDs from its own neighbor tally (an AP commonly sees
                   itself in its own scan).

    Returns one row per band the DUT has a VAP on:
        {band, iface, current_channel, recommended_channel, score,
         occupancy: {channel: score, ...}, reasoning, caveat}
    """
    own_bssids = {v["bssid"].lower() for v in own_vaps if v.get("bssid")}

    rows: list[dict] = []
    for own in own_vaps:
        band = own.get("band")
        current_channel = own.get("channel")
        if band is None or current_channel is None:
            continue

        band_neighbors = [
            n for n in neighbors
            if n.get("band") == band and (n.get("bssid") or "").lower() not in own_bssids
        ]

        occupancy: dict[int, float] = {}
        for n in band_neighbors:
            ch = n.get("channel")
            if ch is None:
                continue
            occupancy[ch] = occupancy.get(ch, 0.0) + _signal_weight(n.get("signal_dbm"))

        if band == "2.4GHz":
            candidates = sorted(set(_NONOVERLAP_24G) | {current_channel})
        else:
            candidates = sorted({n["channel"] for n in band_neighbors if n.get("channel")} | {current_channel})

        # Tie-break toward the current channel (don't recommend hopping when a
        # candidate is merely *equally* clear — channel changes cost
        # reassociation), then toward the lowest channel number.
        recommended = min(
            candidates,
            key=lambda c: (occupancy.get(c, 0.0), 0 if c == current_channel else 1, c),
        )
        score = occupancy.get(recommended, 0.0)

        if not band_neighbors:
            reasoning = "No neighboring APs detected in this band — current channel is clear."
        elif recommended == current_channel:
            reasoning = f"Current channel {current_channel} is already the least-occupied candidate (score {score:g})."
        else:
            current_score = occupancy.get(current_channel, 0.0)
            reasoning = (
                f"Channel {recommended} is less occupied (score {score:g}) than "
                f"current channel {current_channel} (score {current_score:g})."
            )

        rows.append({
            "band": band,
            "iface": own.get("iface"),
            "current_channel": current_channel,
            "recommended_channel": recommended,
            "score": score,
            "occupancy": occupancy,
            "reasoning": reasoning,
            "caveat": None,
        })

    return rows
