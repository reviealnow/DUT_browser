"""Host-side Wi-Fi survey via `iw dev <iface> scan` (Linux) or nmcli fallback.

Runs on the dashboard host (not on the DUT). Requires a Wi-Fi interface set via
the SURVEY_WIFI_IFACE environment variable and appropriate scan permissions:
    sudo setcap cap_net_admin+ep $(which iw)   # or run backend as root

Graceful degradation:
  - SURVEY_WIFI_IFACE unset  → available:false, reason:"SURVEY_WIFI_IFACE not set"
  - iw/nmcli unavailable     → available:false, reason:<detail>
  - Scan permission denied   → available:false, reason:<stderr>
  - macOS / no iw            → falls through to nmcli, then available:false

Parsed fields per BSS (iw scan, verified on iw 5.x):
  BSS anchor       : BSS <bssid>(on ...)
  freq             : freq: NNNN
  signal           : signal: -NN.NN dBm
  SSID             : SSID: <text>  (empty string = hidden)
  HT/VHT/HE/EHT   : capability blocks → generation
  RSN block        : Authentication suites / Pairwise ciphers / Group cipher /
                     Capabilities (MFP-required / MFP-capable)
  RM capabilities  : → dot11k
  Extended caps    : BSS Transition → dot11v
  MDE / FT AKMs    : → dot11r
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime

from app.config import SURVEY_WIFI_IFACE

# ---------------------------------------------------------------------------
# iw scan parsers
# ---------------------------------------------------------------------------

_BSS_RE = re.compile(r"^BSS\s+([0-9a-f:]{17})", re.I)
_FREQ_RE = re.compile(r"^\s+freq:\s+(\d+)")
_SIGNAL_RE = re.compile(r"^\s+signal:\s+(-?[\d.]+)\s+dBm")
_SSID_RE = re.compile(r"^\s+SSID:\s*(.*?)\s*$")
_AUTH_RE = re.compile(r"Authentication suites:\s*(.+)", re.I)
_PAIRWISE_RE = re.compile(r"Pairwise ciphers:\s*(.+)", re.I)
_GROUP_RE = re.compile(r"Group cipher:\s*(.+)", re.I)
_RSN_CAP_RE = re.compile(r"Capabilities:\s*(.*)", re.I)
_MFP_REQ_RE = re.compile(r"\bMFP-required\b", re.I)
_MFP_CAP_RE = re.compile(r"\bMFP-capable\b", re.I)
_RM_CAP_RE = re.compile(r"^\s+RM enabled capabilities", re.I)
_BSS_TRANS_RE = re.compile(r"\*\s+BSS Transition\b", re.I)
_MDE_RE = re.compile(r"^\s+MDE:", re.I)
_HT_RE = re.compile(r"^\s+HT capabilities:", re.I)
_VHT_RE = re.compile(r"^\s+VHT capabilities:", re.I)
_HE_RE = re.compile(r"^\s+HE capabilities:", re.I)
_EHT_RE = re.compile(r"^\s+EHT capabilities:", re.I)

# AKM OUIs / suite-selector names that appear in `iw scan` RSN output
_FT_AKM_RE = re.compile(r"\b(FT/PSK|FT/SAE|FT/802\.1X|00-0f-ac:3|00-0f-ac:4|00-0f-ac:9|00-0f-ac:13)\b", re.I)


def _freq_to_band(freq_mhz: int | None) -> str | None:
    if freq_mhz is None:
        return None
    if freq_mhz >= 5945:
        return "6GHz"
    if freq_mhz >= 4900:
        return "5GHz"
    return "2.4GHz"


def _freq_to_channel(freq_mhz: int | None) -> int | None:
    """Approximate channel from MHz (sufficient for display)."""
    if freq_mhz is None:
        return None
    if 2412 <= freq_mhz <= 2484:
        return (freq_mhz - 2407) // 5
    if 5170 <= freq_mhz <= 5885:
        return (freq_mhz - 5000) // 5
    if freq_mhz >= 5945:
        return (freq_mhz - 5950) // 5 + 1
    return None


def _parse_iw_scan(text: str) -> list[dict]:
    """Parse `iw dev <iface> scan` output into ObservedBss dicts."""
    bss_list: list[dict] = []
    cur: dict | None = None
    in_rsn = False
    in_ext_caps = False

    def _commit() -> None:
        if cur is not None:
            # Derive generation from capability flags (highest wins).
            if cur.pop("_eht", False):
                cur["generation"] = "Wi-Fi 7"
            elif cur.pop("_he", False):
                cur["generation"] = "Wi-Fi 6E" if (cur.get("freq_mhz") or 0) >= 5945 else "Wi-Fi 6"
            elif cur.pop("_vht", False):
                cur["generation"] = "Wi-Fi 5"
            elif cur.pop("_ht", False):
                cur["generation"] = "Wi-Fi 4"
            else:
                cur["generation"] = None
            bss_list.append(cur)

    for line in text.splitlines():
        # New BSS block.
        m_bss = _BSS_RE.match(line)
        if m_bss:
            _commit()
            cur = {
                "bssid": m_bss.group(1).lower(),
                "ssid": None,
                "freq_mhz": None,
                "band": None,
                "channel": None,
                "signal_dbm": None,
                "generation": None,
                "security": None,
                "category": None,
                "akm": [],
                "pairwise_cipher": [],
                "group_cipher": None,
                "pmf": None,
                "dot11k": False,
                "dot11v": False,
                "dot11r": False,
                "source": "iw",
                "_ht": False, "_vht": False, "_he": False, "_eht": False,
            }
            in_rsn = False
            in_ext_caps = False
            continue

        if cur is None:
            continue

        if (m := _FREQ_RE.match(line)):
            cur["freq_mhz"] = int(m.group(1))
            cur["band"] = _freq_to_band(cur["freq_mhz"])
            cur["channel"] = _freq_to_channel(cur["freq_mhz"])
            continue

        if (m := _SIGNAL_RE.match(line)):
            try:
                cur["signal_dbm"] = float(m.group(1))
            except ValueError:
                pass
            continue

        if (m := _SSID_RE.match(line)):
            cur["ssid"] = m.group(1)  # empty string = hidden
            in_rsn = False
            in_ext_caps = False
            continue

        # Capability header blocks.
        if _EHT_RE.match(line):
            cur["_eht"] = True
            continue
        if _HE_RE.match(line):
            cur["_he"] = True
            continue
        if _VHT_RE.match(line):
            cur["_vht"] = True
            continue
        if _HT_RE.match(line):
            cur["_ht"] = True
            continue

        # RSN block.
        if line.strip().startswith("RSN:"):
            in_rsn = True
            in_ext_caps = False
            continue

        if in_rsn:
            if (m := _AUTH_RE.search(line)):
                cur["akm"] = [a.strip() for a in m.group(1).split() if a.strip()]
                # Classify security from AKM list.
                akms = [a.upper() for a in cur["akm"]]
                has_sae = any("SAE" in a for a in akms)
                has_psk = any("PSK" in a and "SAE" not in a for a in akms)
                has_eap_sb = any("SUITE-B" in a for a in akms)
                has_eap = any("802.1X" in a or "EAP" in a for a in akms) or has_eap_sb
                if has_eap_sb:
                    cur["security"], cur["category"] = "WPA3-Enterprise-192", "enterprise"
                elif has_eap:
                    cur["security"], cur["category"] = "WPA2-Enterprise", "enterprise"
                elif has_sae and has_psk:
                    cur["security"], cur["category"] = "WPA2/WPA3-Personal", "personal"
                elif has_sae:
                    cur["security"], cur["category"] = "WPA3-Personal", "personal"
                elif has_psk:
                    cur["security"], cur["category"] = "WPA2-Personal", "personal"
            elif (m := _PAIRWISE_RE.search(line)):
                cur["pairwise_cipher"] = [c.strip() for c in m.group(1).split() if c.strip()]
            elif (m := _GROUP_RE.search(line)):
                cur["group_cipher"] = m.group(1).strip()
            elif (m := _RSN_CAP_RE.search(line)):
                caps = m.group(1)
                if _MFP_REQ_RE.search(caps):
                    cur["pmf"] = "required"
                elif _MFP_CAP_RE.search(caps):
                    cur["pmf"] = "optional"
                else:
                    cur["pmf"] = "disabled"
            # RSN block ends when we hit a non-indented continuation or a new top-level block.
            if line and not line[0].isspace():
                in_rsn = False

        # dot11k: RM enabled capabilities block presence.
        if _RM_CAP_RE.match(line):
            cur["dot11k"] = True

        # dot11v: BSS Transition in Extended capabilities.
        if line.strip().startswith("Extended capabilities:"):
            in_ext_caps = True
        elif in_ext_caps:
            if _BSS_TRANS_RE.search(line):
                cur["dot11v"] = True
            if line and not line[0].isspace():
                in_ext_caps = False

        # dot11r: Mobility Domain Element or FT AKMs.
        if _MDE_RE.match(line):
            cur["dot11r"] = True
        if in_rsn and _FT_AKM_RE.search(line):
            cur["dot11r"] = True

        # No RSN → treat as Open if no security yet set.
    _commit()

    for bss in bss_list:
        if bss["security"] is None:
            bss["security"] = "Open"
            bss["category"] = None

    return bss_list


def _split_nmcli(line: str) -> list[str]:
    """Split an nmcli -t line on unescaped colons (nmcli escapes embedded : as \\:)."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
        elif line[i] == ":":
            parts.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(line[i])
            i += 1
    parts.append("".join(cur))
    return parts


def _parse_nmcli(text: str) -> list[dict]:
    """Parse `nmcli -t -f BSSID,SSID,CHAN,FREQ,SECURITY dev wifi list` output.

    Returns ObservedBss dicts with security populated but k/v/r = None (nmcli
    does not expose those). Marks source='nmcli'.
    """
    bss_list: list[dict] = []
    for line in text.splitlines():
        parts = _split_nmcli(line)
        if len(parts) < 5:
            continue
        bssid_raw, ssid, chan, freq_raw, security_raw = parts[0], parts[1], parts[2], parts[3], ":".join(parts[4:])
        bssid = bssid_raw.lower()
        if not re.match(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}$", bssid):
            continue
        freq_mhz: int | None = None
        try:
            # nmcli reports "5180 MHz" or "2437 MHz"
            freq_mhz = int(re.search(r"\d+", freq_raw).group())  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
        channel: int | None = None
        try:
            channel = int(chan)
        except ValueError:
            pass

        sec = security_raw.strip().upper()
        if "WPA3" in sec:
            security, category = "WPA3-Personal", "personal"
        elif "WPA2" in sec and "ENTERPRISE" in sec:
            security, category = "WPA2-Enterprise", "enterprise"
        elif "WPA2" in sec or "WPA1" in sec:
            security, category = "WPA2-Personal", "personal"
        elif "WEP" in sec:
            security, category = "WEP", "personal"
        elif "--" in sec or not sec:
            security, category = "Open", None
        else:
            security, category = sec, None

        bss_list.append({
            "bssid": bssid,
            "ssid": ssid or "",
            "freq_mhz": freq_mhz,
            "band": _freq_to_band(freq_mhz),
            "channel": channel,
            "signal_dbm": None,
            "generation": None,
            "security": security,
            "category": category,
            "akm": [],
            "pairwise_cipher": [],
            "group_cipher": None,
            "pmf": None,
            "dot11k": None,
            "dot11v": None,
            "dot11r": None,
            "source": "nmcli",
        })
    return bss_list


def _scannable_bands(bss_list: list[dict]) -> list[str]:
    """Infer scannable bands from what actually appeared in the scan results."""
    seen: set[str] = set()
    for bss in bss_list:
        b = bss.get("band")
        if b:
            seen.add(b)
    order = ["2.4GHz", "5GHz", "6GHz"]
    return [b for b in order if b in seen]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_wifi_survey() -> dict:
    """Run a host-side Wi-Fi scan and return ObservedBss list + metadata.

    Returns {"available": True, "bss": [...], ...} on success, or
    {"available": False, "reason": "..."} when no scan is possible.
    Never raises — all errors are captured into the reason field.
    """
    if not SURVEY_WIFI_IFACE:
        return {"available": False, "reason": "SURVEY_WIFI_IFACE not set", "bss": []}

    captured_at = datetime.now().isoformat(timespec="seconds")

    # --- Try iw first ---
    if shutil.which("iw"):
        try:
            result = subprocess.run(
                ["iw", "dev", SURVEY_WIFI_IFACE, "scan"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                bss = _parse_iw_scan(result.stdout)
                return {
                    "available": True,
                    "iface": SURVEY_WIFI_IFACE,
                    "bss": bss,
                    "scannable_bands": _scannable_bands(bss),
                    "captured_at": captured_at,
                    "source": "iw",
                }
            # Permission denied or interface error → fall through to nmcli.
            iw_error = (result.stderr or result.stdout).strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            iw_error = str(exc)
    else:
        iw_error = "iw not found"

    # --- Fall back to nmcli ---
    if shutil.which("nmcli"):
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "BSSID,SSID,CHAN,FREQ,SECURITY", "dev", "wifi", "list"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                bss = _parse_nmcli(result.stdout)
                return {
                    "available": True,
                    "iface": SURVEY_WIFI_IFACE,
                    "bss": bss,
                    "scannable_bands": _scannable_bands(bss),
                    "captured_at": captured_at,
                    "source": "nmcli",
                }
        except (subprocess.TimeoutExpired, OSError):
            pass

    return {
        "available": False,
        "reason": iw_error,
        "bss": [],
        "captured_at": captured_at,
    }
