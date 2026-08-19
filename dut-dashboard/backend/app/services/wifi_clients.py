"""Parse Wi-Fi client detail and per-VAP SSID capability from on-demand DUT
console captures.

Wi-Fi clients — two console sources (QCA/Atheros AP, verified on AP6 840E):
  * ``iwconfig``            -> active VAPs (athN Master + ESSID + channel).
  * ``wlanconfig <vap> list`` -> association table + verbose tail (SNR, phymode).

SSID capability — three console sources (verified on AP6 840E, 2026-06):
  * ``iw dev``              -> BSSID (addr), SSID, freq_MHz, channel, width.
  * ``iwconfig``            -> IEEE mode token (e.g. 802.11axa) for generation.
  * ``for f in /etc/hostapd*.conf; do printf '====CONF====%s\\n' "$f"; cat "$f"; done``
                            -> wpa_key_mgmt / ieee80211w / ieee80211k /
                               bss_transition / mobility_domain / ciphers.

All captures are on-demand serial RPCs; never background-poll.
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


def _norm_band(band: str) -> str:
    """Normalise the firmware's verbose 'Operating band' (e.g. '6GHz', '5 GHz')
    to the iface-derived form ('2.4G' / '5G' / '6G') so the field is consistent."""
    return band.strip().upper().replace("GHZ", "G").replace(" ", "")


def signal_pct(rssi: int | None) -> int | None:
    """Rough dBm -> 0-100% quality (2*(rssi+100), clamped)."""
    if rssi is None:
        return None
    return max(0, min(100, 2 * (rssi + 100)))


def signal_band(rssi: int | None) -> str | None:
    """Coarse proximity wording for a raw RSSI; never imply measured distance."""
    if rssi is None:
        return None
    if rssi >= -55:
        return "near"
    if rssi >= -70:
        return "mid"
    return "far"


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
                clients[-1]["band"] = _norm_band(band.group(1))
    return clients


# Per-station `apstats -s -m <MAC>` fields we surface. Each is a `Label = value`
# line; chainmask (NSS) is a special `tx(N) rx(N)` form handled separately.
_APSTATS_FIELDS: dict[str, str] = {
    "tx_bytes": "Tx Data Bytes",
    "rx_bytes": "Rx Data Bytes",
    "avg_tx_kbps": "Average Tx Rate (kbps)",
    "avg_rx_kbps": "Average Rx Rate (kbps)",
    "tx_bytes_1s": "Tx bytes for last one second",
    "rx_bytes_1s": "Rx bytes for last one second",
    "band_width": "Band Width",
    "rx_rssi": "Rx RSSI",
    "per": "Last Packet Error Rate (PER)",
}
_APSTATS_NSS_RE = re.compile(r"chainmask\s*\(NSS\)\s+tx\((\d+)\)\s+rx\((\d+)\)")


def parse_apstats(text: str) -> dict:
    """Parse `apstats -s -m <MAC>` into per-client deep stats. Missing fields → None
    (tolerant of firmware variation)."""
    stats: dict = {}
    for key, label in _APSTATS_FIELDS.items():
        m = re.search(re.escape(label) + r"\s*=\s*(-?\d+)", text)
        stats[key] = int(m.group(1)) if m else None
    nss = _APSTATS_NSS_RE.search(text)
    stats["tx_nss"] = int(nss.group(1)) if nss else None
    stats["rx_nss"] = int(nss.group(2)) if nss else None
    return stats


# ---------------------------------------------------------------------------
# Mesh backhaul link facts (verified on AP6 420 + AP6 840E, 2026-08)
# ---------------------------------------------------------------------------

# A VAP header from `iwconfig`. Deliberately not anchored to `ath\d+`: the
# classifier below must not care what the radios are called.
_IW_HEAD_RE = re.compile(r'^(\S+)\s+IEEE\s+\S+\s+ESSID:"([^"]*)"')
_IW_SILENT_RE = re.compile(r"^\S+\s+no wireless extensions")
_IW_FREQ_GHZ_RE = re.compile(r"Frequency[:=]\s*([\d.]+)\s*GHz")
_IW_PEER_RE = re.compile(r"Access Point:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|Not-Associated)")
_IW_LQ_RE = re.compile(r"Link Quality[:=](\d+)/(\d+)")
_IW_SIGNAL_RE = re.compile(r"Signal level[:=](-?\d+)\s*dBm")
_IW_NOISE_RE = re.compile(r"Noise level[:=](-?\d+)\s*dBm")


def parse_iwconfig_links(text: str) -> list[dict]:
    """Per-VAP link facts from `iwconfig`. Missing fields stay ``None``.

    Lines are stripped before matching as cheap normalisation of console
    output; no captured sample has needed it, so do not read it as a fix for
    an observed defect.

    ``associated`` is the field the caller should trust, and it is not "does
    this VAP report a signal". A Master VAP reports one too — the noise floor,
    at ``Link Quality=0/94`` — so its ``Signal level`` is never a measurement
    of a link. It also reports an ``Access Point``, which is its own BSSID
    rather than a peer. Only a Managed VAP with a live link quality and a real
    peer MAC is hearing anything.
    """
    links: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        head = _IW_HEAD_RE.match(line)
        if head is not None:
            current = {
                "iface": head.group(1),
                "essid": head.group(2),
                "mode": None,
                "freq_ghz": None,
                "band": None,
                "peer_mac": None,
                "link_quality": None,
                "link_quality_max": None,
                "rssi": None,
                "noise": None,
                "snr": None,
                "associated": False,
            }
            links.append(current)
            continue
        if _IW_SILENT_RE.match(line):
            current = None  # a non-wireless device ends the block it follows
            continue
        if current is None:
            continue
        mode = _MODE_RE.search(line)
        if mode is not None and current["mode"] is None:
            current["mode"] = mode.group(1)
        freq = _IW_FREQ_GHZ_RE.search(line)
        if freq is not None and current["freq_ghz"] is None:
            current["freq_ghz"] = float(freq.group(1))
            current["band"] = _band_from_freq(int(current["freq_ghz"] * 1000))
        peer = _IW_PEER_RE.search(line)
        if peer is not None and current["peer_mac"] is None:
            current["peer_mac"] = None if peer.group(1) == "Not-Associated" else peer.group(1).lower()
        quality = _IW_LQ_RE.search(line)
        if quality is not None and current["link_quality"] is None:
            current["link_quality"] = int(quality.group(1))
            current["link_quality_max"] = int(quality.group(2))
        signal = _IW_SIGNAL_RE.search(line)
        if signal is not None and current["rssi"] is None:
            current["rssi"] = int(signal.group(1))
        noise = _IW_NOISE_RE.search(line)
        if noise is not None and current["noise"] is None:
            current["noise"] = int(noise.group(1))

    for link in links:
        link["associated"] = bool(
            link["mode"] == "Managed"
            and link["peer_mac"]
            and (link["link_quality"] or 0) > 0
        )
        if not link["associated"]:
            # Refuse to hand back a noise-floor reading dressed as a link.
            link["rssi"] = None
            link["snr"] = None
        elif link["rssi"] is not None and link["noise"] is not None:
            link["snr"] = link["rssi"] - link["noise"]
    return links


def classify_backhaul(links: list[dict]) -> dict:
    """Split the backhaul into the link up and the VAP children associate to.

    A mesh backhaul is a pair of VAPs sharing one ESSID on one band: the
    Managed side carries this node's link to its parent, the Master side is
    what its own children join. The pairing is what identifies them — not the
    interface number, which is numbered differently on an AP6 420 than on an
    AP6 840E, and not the SSID's text, which is operator-chosen and was
    observed changing between two captures twenty minutes apart.

    A root has no associated Managed VAP, so it has no uplink and its downlink
    cannot be found by pairing; the caller falls back to the configured
    interface there.
    """
    uplink = next((link for link in links if link["associated"]), None)
    if uplink is None:
        return {"uplink": None, "downlink": None}
    downlink = next(
        (
            link
            for link in links
            if link["mode"] == "Master"
            and link["essid"] == uplink["essid"]
            and link["band"] == uplink["band"]
            and link["iface"] != uplink["iface"]
        ),
        None,
    )
    return {"uplink": uplink, "downlink": downlink}


# ---------------------------------------------------------------------------
# SSID capability parsers (verified on AP6 840E, 2026-06)
# ---------------------------------------------------------------------------

# `iw dev` block anchors.  Match any Interface line; only athX are kept.
_IW_IFACE_RE = re.compile(r"^\s+Interface\s+(\S+)\s*$")
_IW_ADDR_RE = re.compile(r"^\s+addr\s+([0-9a-f:]{17})\s*$")
_IW_SSID_RE = re.compile(r"^\s+ssid\s+(.+?)\s*$")
_IW_CHAN_RE = re.compile(r"^\s+channel\s+(\d+)\s+\((\d+)\s+MHz\),\s+width:\s+(\d+)\s+MHz")
_IW_TYPE_RE = re.compile(r"^\s+type\s+(\S+)\s*$")

# `iwconfig` IEEE mode per interface (athN    IEEE 802.11axa  ESSID:...)
_IWCONF_MODE_RE = re.compile(r"^(ath\d+)\s+IEEE\s+(802\.\d+\S+)")

# hostapd conf dump sentinel produced by the shell loop.
_CONF_SEP_RE = re.compile(r"^====CONF====(.+?)\s*$")
_CONF_FIELD_RE = re.compile(r"^([a-zA-Z0-9_]+)=(.*)$")


def _parse_iw_dev(text: str) -> dict[str, dict]:
    """Parse `iw dev` output into {iface: {bssid, ssid, freq_mhz, channel, width_mhz}}.
    Only athX interfaces in AP type with an SSID are included."""
    result: dict[str, dict] = {}
    current_iface: str | None = None
    current: dict = {}

    def _flush() -> None:
        if (
            current_iface
            and re.match(r"ath\d+$", current_iface)
            and current.get("ssid")
            and current.get("iface_type") == "AP"
        ):
            result[current_iface] = {k: v for k, v in current.items() if k != "iface_type"}

    for line in text.splitlines():
        m_iface = _IW_IFACE_RE.match(line)
        if m_iface:
            _flush()
            current_iface = m_iface.group(1)
            current = {}
            continue
        if current_iface is None:
            continue
        if (m := _IW_ADDR_RE.match(line)):
            current["bssid"] = m.group(1)
        elif (m := _IW_SSID_RE.match(line)):
            current["ssid"] = m.group(1)
        elif (m := _IW_TYPE_RE.match(line)):
            current["iface_type"] = m.group(1)
        elif (m := _IW_CHAN_RE.match(line)):
            current["channel"] = int(m.group(1))
            current["freq_mhz"] = int(m.group(2))
            current["width_mhz"] = int(m.group(3))
    _flush()
    return result


def _parse_iwconfig_modes(text: str) -> dict[str, str]:
    """Parse `iwconfig` output into {iface: ieee_mode_token} e.g. {'ath8': '802.11axa'}."""
    modes: dict[str, str] = {}
    for line in text.splitlines():
        m = _IWCONF_MODE_RE.match(line)
        if m:
            modes[m.group(1)] = m.group(2)
    return modes


def _parse_hostapd_confs(text: str) -> dict[str, dict]:
    """Parse the shell-loop dump of all /etc/hostapd*.conf files.

    Expected input produced by:
        for f in /etc/hostapd*.conf; do printf '====CONF====%s\\n' "$f"; cat "$f"; done

    Returns {iface: {wpa_key_mgmt: list[str], wpa_pairwise: list[str],
    group_mgmt_cipher: str|None, ieee80211w: int, ieee80211k: bool,
    bss_transition: bool, dot11r: bool}}.
    """
    result: dict[str, dict] = {}
    current: dict[str, str] = {}

    def _flush_conf(fields: dict[str, str]) -> None:
        iface = fields.get("interface", "").strip()
        if not iface:
            return
        def _bool(key: str) -> bool:
            return fields.get(key, "0").strip() == "1"
        def _int(key: str, default: int = 0) -> int:
            try:
                return int(fields.get(key, str(default)).strip())
            except ValueError:
                return default
        akm = [k.strip() for k in fields.get("wpa_key_mgmt", "").split() if k.strip()]
        pairwise = [k.strip() for k in fields.get("wpa_pairwise", "").split() if k.strip()]
        has_ft = any(k.startswith("FT-") for k in akm)
        has_mobility = bool(fields.get("mobility_domain", "").strip())
        dot11r = has_ft or has_mobility or _bool("ft_psk_generate_local") or _bool("ft_over_ds")
        result[iface] = {
            "wpa": _int("wpa"),
            "wpa_key_mgmt": akm,
            "wpa_pairwise": pairwise,
            "group_mgmt_cipher": fields.get("group_mgmt_cipher", "").strip() or None,
            "ieee80211w": _int("ieee80211w"),
            "ieee80211k": _bool("ieee80211k"),
            "bss_transition": _bool("bss_transition"),
            "dot11r": dot11r,
        }

    for line in text.splitlines():
        m_sep = _CONF_SEP_RE.match(line)
        if m_sep:
            _flush_conf(current)
            current = {}
            continue
        m_field = _CONF_FIELD_RE.match(line.strip())
        if m_field and not line.strip().startswith("#"):
            current[m_field.group(1)] = m_field.group(2)
    _flush_conf(current)
    return result


def _derive_generation(ieee_token: str, freq_mhz: int | None) -> str | None:
    """Map iwconfig IEEE token + freq to a Wi-Fi generation label."""
    t = ieee_token.lower()
    if "be" in t:
        return "Wi-Fi 7"
    if "ax" in t:
        if freq_mhz and freq_mhz >= 5945:
            return "Wi-Fi 6E"
        return "Wi-Fi 6"
    if "ac" in t:
        return "Wi-Fi 5"
    if "n" in t or "ht" in t:
        return "Wi-Fi 4"
    return None


def _classify_security(
    akm_list: list[str], ieee80211w: int
) -> tuple[str, str | None, str]:
    """Return (security_label, category, pmf_label) from hostapd fields."""
    has_sae = any(k in ("SAE", "FT-SAE") for k in akm_list)
    has_psk = any(k in ("WPA-PSK", "FT-PSK") for k in akm_list)
    has_suite_b = "WPA-EAP-SUITE-B-192" in akm_list
    has_eap = any(k.startswith("WPA-EAP") or k == "IEEE8021X" for k in akm_list)

    if has_suite_b:
        security, category = "WPA3-Enterprise-192", "enterprise"
    elif has_eap:
        security, category = "WPA2-Enterprise", "enterprise"
    elif has_sae and has_psk:
        security, category = "WPA2/WPA3-Personal", "personal"
    elif has_sae:
        security, category = "WPA3-Personal", "personal"
    elif has_psk:
        security, category = "WPA2-Personal", "personal"
    else:
        security, category = "Open", None

    pmf = {0: "disabled", 1: "optional", 2: "required"}.get(ieee80211w, "disabled")
    return security, category, pmf


def _band_from_freq(freq_mhz: int | None) -> str | None:
    if freq_mhz is None:
        return None
    if freq_mhz >= 5945:
        return "6GHz"
    if freq_mhz >= 4900:
        return "5GHz"
    return "2.4GHz"


def get_ssid_capabilities(
    worker: "object",  # SerialWorker — avoids a circular import
) -> list[dict]:
    """Gather per-VAP SSID capability from three on-demand serial captures.

    Returns a list of SsidCapability dicts; missing fields are None (tolerant).
    Raises RuntimeError (passed through) only when the serial port is not open.
    """
    from app.serial.serial_worker import SerialWorker  # local import avoids circular

    assert isinstance(worker, SerialWorker)

    # --- Capture 1: iw dev (BSSID / SSID / freq / channel / width) ---
    iw_text = worker.capture_command("iw dev", timeout=6.0)
    iw_vaps = _parse_iw_dev(iw_text)

    # --- Capture 2: iwconfig (IEEE mode token for generation) ---
    try:
        iwc_text = worker.capture_command("iwconfig", timeout=8.0)
    except RuntimeError:
        iwc_text = ""
    iwc_modes = _parse_iwconfig_modes(iwc_text)

    # --- Capture 3: hostapd conf dump (security / PMF / k/v/r) ---
    conf_cmd = r"for f in /etc/hostapd*.conf; do printf '====CONF====%s\n' \"$f\"; cat \"$f\"; done"
    try:
        conf_text = worker.capture_command(conf_cmd, timeout=10.0)
    except RuntimeError:
        conf_text = ""
    conf_map = _parse_hostapd_confs(conf_text)

    capabilities: list[dict] = []
    for iface, vap in iw_vaps.items():
        freq_mhz: int | None = vap.get("freq_mhz")
        ieee_token: str = iwc_modes.get(iface, "")
        conf: dict = conf_map.get(iface, {})

        akm_list: list[str] = conf.get("wpa_key_mgmt", [])
        ieee80211w: int = conf.get("ieee80211w", 0)
        security, category, pmf = _classify_security(akm_list, ieee80211w)

        gen = _derive_generation(ieee_token, freq_mhz) if ieee_token else None

        capabilities.append({
            "iface": iface,
            "bssid": vap.get("bssid"),
            "ssid": vap.get("ssid"),
            "band": _band_from_freq(freq_mhz),
            "freq_mhz": freq_mhz,
            "channel": vap.get("channel"),
            "channel_width": f"{vap['width_mhz']} MHz" if vap.get("width_mhz") else None,
            "generation": gen,
            "security": security,
            "category": category,
            "akm": akm_list,
            "pairwise_cipher": conf.get("wpa_pairwise", []),
            "group_mgmt_cipher": conf.get("group_mgmt_cipher"),
            "pmf": pmf,
            "dot11k": conf.get("ieee80211k", False) or None if conf else None,
            "dot11v": conf.get("bss_transition", False) or None if conf else None,
            "dot11r": conf.get("dot11r", False) or None if conf else None,
        })

    # Sort by band (2.4 → 5 → 6) then iface number for consistent ordering.
    def _sort_key(c: dict) -> tuple[int, int]:
        freq = c.get("freq_mhz") or 0
        n = int(re.search(r"\d+", c["iface"]).group()) if re.search(r"\d+", c["iface"]) else 0  # type: ignore[union-attr]
        return (0 if freq < 4900 else 1 if freq < 5945 else 2, n)

    capabilities.sort(key=_sort_key)
    return capabilities
