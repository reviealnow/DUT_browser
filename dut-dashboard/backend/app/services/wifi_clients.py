"""Parse Wi-Fi client detail from on-demand DUT console captures.

Two console sources (QCA/Atheros AP, verified on a real AP6 840E):
  * ``iwconfig``            -> active VAPs (athN Master + ESSID + channel).
  * ``wlanconfig <vap> list`` -> the association table (one row per client) plus
    a verbose key:value tail (SNR, operating band, max phymode).

The association row is whitespace-delimited but has many columns, some empty, and
a MODE field that itself contains spaces — so we extract the wanted fields with
targeted regexes (tolerant of misses) rather than positional column slicing.
"""

from __future__ import annotations

import re
from typing import Callable

# A VAP header line, e.g.: ath16     IEEE 802.11axa  ESSID:"!!3290-1"
_VAP_RE = re.compile(r'^(ath\d+)\s+IEEE\s+\S+\s+ESSID:"([^"]*)"')
_CHAN_RE = re.compile(r"Frequency:[\d.]+\s*GHz\s*\(Channel\s*(\d+)\)")
_MODE_RE = re.compile(r"Mode:(\w+)")

# Association-row fields.
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
_RATE_RE = re.compile(r"\b(\d+(?:\.\d+)?[MG])\b")  # 864M / 2.4019G
_NEG_RE = re.compile(r"-\d+")
_ASSOC_RE = re.compile(r"\b(\d{1,2}:\d{2}:\d{2})\b")
_PHYMODE_RE = re.compile(r"IEEE80211_MODE_(\S+)")
_NSS_RE = re.compile(r"IEEE80211_MODE_\S+\s+(\d+)\s+(\d+)")
# Verbose tail key:values.
_SNR_RE = re.compile(r"\bSNR\b\s*[:=]\s*(-?\d+)")
_BAND_RE = re.compile(r"Operating band\s*[:=]\s*(\S+)")


def band_for_iface(iface: str) -> str:
    """Map athN to a radio band (16 VAPs per band on this platform)."""
    m = re.match(r"ath(\d+)", iface)
    if not m:
        return "?"
    n = int(m.group(1))
    if n < 16:
        return "2.4G"
    if n < 32:
        return "5G"
    return "6G"


def signal_pct(rssi: int | None) -> int | None:
    """Rough dBm -> 0-100% quality (2*(rssi+100), clamped)."""
    if rssi is None:
        return None
    return max(0, min(100, 2 * (rssi + 100)))


def vendor_for_mac(mac: str) -> str:
    """Locally-administered (randomized) MACs have bit 0x2 set in the first octet.
    Full OUI->vendor name lookup is deferred (avoids bundling a multi-MB table)."""
    try:
        first = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return ""
    if first & 0x2:
        return "Private (randomized)"
    return mac[:8].upper()  # OUI prefix as a placeholder


def width_from_phymode(phymode: str) -> str | None:
    """e.g. 11AXA_HE80 -> 80MHz, 11AC_VHT160 -> 160MHz."""
    m = re.search(r"(?:HE|VHT|HT)(\d+)", phymode)
    return f"{m.group(1)}MHz" if m else None


def discover_vaps(iwconfig_text: str) -> list[dict]:
    """Active VAPs (Master mode, has ESSID) from `iwconfig` output."""
    vaps: list[dict] = []
    current: dict | None = None
    for line in iwconfig_text.splitlines():
        head = _VAP_RE.match(line)
        if head:
            if current:
                vaps.append(current)
            iface = head.group(1)
            current = {"iface": iface, "ssid": head.group(2), "band": band_for_iface(iface), "channel": None, "mode": None}
            # channel/mode may be on the same logical block; check this line too
        if current:
            ch = _CHAN_RE.search(line)
            if ch and current["channel"] is None:
                current["channel"] = int(ch.group(1))
            md = _MODE_RE.search(line)
            if md and current["mode"] is None:
                current["mode"] = md.group(1)
    if current:
        vaps.append(current)
    # Only Master-mode VAPs serve clients.
    return [v for v in vaps if (v["mode"] or "Master") == "Master"]


def parse_wlanconfig_list(text: str, iface: str) -> list[dict]:
    """Parse `wlanconfig <iface> list` into client dicts. Header-only (no clients)
    returns []. Verbose SNR/band tail (if present) attaches to the latest client."""
    clients: list[dict] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        mac_m = _MAC_RE.match(line.strip())
        if mac_m:
            mac = mac_m.group(1).lower()
            rest = line.strip()[len(mac_m.group(1)):].split()
            rates = _RATE_RE.findall(line)
            negs = _NEG_RE.findall(line)
            assoc = _ASSOC_RE.search(line)
            phy = _PHYMODE_RE.search(line)
            nss = _NSS_RE.search(line)
            client = {
                "iface": iface,
                "band": band_for_iface(iface),
                "mac": mac,
                "vendor": vendor_for_mac(mac),
                "aid": int(rest[0]) if rest and rest[0].isdigit() else None,
                "channel": int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None,
                "txrate": rates[0] if rates else None,
                "rxrate": rates[1] if len(rates) > 1 else None,
                "rssi": int(negs[0]) if negs else None,
                "assoc_time": assoc.group(1) if assoc else None,
                "phymode": phy.group(1) if phy else None,
                "width": width_from_phymode(phy.group(1)) if phy else None,
                "rxnss": int(nss.group(1)) if nss else None,
                "txnss": int(nss.group(2)) if nss else None,
                "snr": None,
            }
            client["signal_pct"] = signal_pct(client["rssi"])
            clients.append(client)
            continue
        if clients:  # verbose tail lines attach to the most recent client
            snr = _SNR_RE.search(line)
            if snr and clients[-1]["snr"] is None:
                clients[-1]["snr"] = int(snr.group(1))
            band = _BAND_RE.search(line)
            if band:
                clients[-1]["band"] = band.group(1)
    return clients
