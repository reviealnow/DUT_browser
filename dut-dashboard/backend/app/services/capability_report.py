"""Reconcile per-VAP DUT config (Source A) with host-side iw scan (Source B).

Source A = get_ssid_capabilities (serial serial RPC): what the DUT is configured
Source B = get_wifi_survey (host iw scan / nmcli): what is visible over the air

Reconciliation joins on BSSID (lowercase) and diffs key security / roaming /
generation fields. Rows that fall in a band not seen in the scan results carry
a caveat (the host may simply lack a 6 GHz or 5 GHz adapter).
"""

from __future__ import annotations

from typing import Any

_DIFF_FIELDS = (
    ("security", "Security"),
    ("pmf", "PMF"),
    ("generation", "Generation"),
    ("dot11k", "802.11k"),
    ("dot11v", "802.11v"),
    ("dot11r", "802.11r"),
)


def _norm_bool(v: Any) -> bool | None:
    """Normalise truthy/None/False to bool|None for consistent diff."""
    if v is None:
        return None
    return bool(v)


def build_capability_report(
    ssids: list[dict],
    survey: dict,
) -> dict:
    """Produce a reconciliation report from Source A and Source B.

    Args:
        ssids:  list of SsidCapability dicts (from get_ssid_capabilities).
        survey: survey dict (from get_wifi_survey); may have available=False.

    Returns a dict with:
      available_b   – bool, whether the survey succeeded
      scannable_bands – bands actually seen in the scan
      rows          – list of reconciliation rows, one per Source-A SSID
      captured_at_a – timestamp from Source A (passed through)
      captured_at_b – timestamp from Source B
    """
    available_b: bool = survey.get("available", False)
    scannable_bands: list[str] = survey.get("scannable_bands", [])
    captured_at_b: str | None = survey.get("captured_at")

    # Build BSSID index for Source B.
    bss_index: dict[str, dict] = {}
    if available_b:
        for bss in survey.get("bss", []):
            bssid = (bss.get("bssid") or "").lower()
            if bssid:
                bss_index[bssid] = bss

    rows: list[dict] = []
    for a in ssids:
        bssid_a = (a.get("bssid") or "").lower()
        band_a = a.get("band")  # "2.4GHz" / "5GHz" / "6GHz"

        b = bss_index.get(bssid_a) if bssid_a else None
        match = b is not None

        diffs: list[dict] = []
        if match and b is not None:
            for field, label in _DIFF_FIELDS:
                val_a = a.get(field)
                val_b = b.get(field)
                # Normalise bool-ish fields so True/"required" differences are
                # caught without false-positives between None and False.
                if field in ("dot11k", "dot11v", "dot11r"):
                    val_a = _norm_bool(val_a)
                    val_b = _norm_bool(val_b)
                if val_a != val_b and not (val_a is None and val_b is None):
                    diffs.append({"field": field, "label": label, "config": val_a, "observed": val_b})

        # caveat: band not scanned at all → we simply couldn't see it.
        caveat: str | None = None
        if available_b and not match and band_a and band_a not in scannable_bands:
            caveat = f"Band {band_a} not scanned — host adapter may not support it"

        rows.append({
            "iface": a.get("iface"),
            "bssid": bssid_a or None,
            "ssid": a.get("ssid"),
            "band": band_a,
            "freq_mhz": a.get("freq_mhz"),
            "channel": a.get("channel"),
            "channel_width": a.get("channel_width"),
            # Source A fields (config).
            "config_generation": a.get("generation"),
            "config_security": a.get("security"),
            "config_pmf": a.get("pmf"),
            "config_dot11k": a.get("dot11k"),
            "config_dot11v": a.get("dot11v"),
            "config_dot11r": a.get("dot11r"),
            # Source B fields (observed). None when no match.
            "observed_generation": b.get("generation") if b else None,
            "observed_security": b.get("security") if b else None,
            "observed_pmf": b.get("pmf") if b else None,
            "observed_dot11k": b.get("dot11k") if b else None,
            "observed_dot11v": b.get("dot11v") if b else None,
            "observed_dot11r": b.get("dot11r") if b else None,
            "observed_signal_dbm": b.get("signal_dbm") if b else None,
            # Reconciliation.
            "match": match,
            "diffs": diffs,
            "caveat": caveat,
        })

    return {
        "available_b": available_b,
        "scannable_bands": scannable_bands,
        "captured_at_b": captured_at_b,
        "rows": rows,
    }
