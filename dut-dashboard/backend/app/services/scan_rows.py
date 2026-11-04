"""Scanned BSSes as rows for the findings rules (#213 A8).

``wifi_survey._parse_iw_scan`` is the one ``iw scan`` parser; this module only
maps its dicts onto the row keys ``packet_rules`` reads, so the same rules run
on a scan and on the beacon Test Modes (``packet_modes.decode_row``) alike.

Every key the rules read is emitted, ``None`` when the BSS did not carry the
element. The rules read a *missing* key as "this source cannot see the field"
and stay silent, so dropping keys would make open and WEP networks vanish from
the findings. A key is left out on purpose in two cases only, where the scan
cannot tell:

* ``akm`` -- an RSN element whose AKMs did not map to any suite number (an
  unknown vendor suite, or a block cut short). ``None`` would read as "no
  RSN", and with the Privacy bit set that is a WEP fail.
* the WPA (v1) keys -- the Privacy bit is set but ``iw`` did not name it on
  the capability line. That is the DUT's busybox ``iw``, which prints no WPA
  element either, so WEP and WPA1 look alike there.

The kernel splits a Multiple BSSID set into one entry per BSS, so nothing here
needs ``rsn_not_evaluated``.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.packet_decode import AKM_SUITES, CIPHER_SUITES

#: ``iw``'s names for suite selectors (``util.c``); unknown ones print as
#: ``00-0f-ac:<n>``, which ``_suites`` reads directly.
_AKM_NAMES = {
    "IEEE 802.1X": 1, "PSK": 2, "FT/IEEE 802.1X": 3, "FT/PSK": 4,
    "IEEE 802.1X/SHA-256": 5, "PSK/SHA-256": 6, "TDLS/TPK": 7, "SAE": 8,
    "FT/SAE": 9, "IEEE 802.1X/SUITE-B": 11, "IEEE 802.1X/SUITE-B-192": 12,
    "FT/IEEE 802.1X/SHA-384": 13, "FILS/SHA-256": 14, "FILS/SHA-384": 15,
    "FT/FILS/SHA-256": 16, "FT/FILS/SHA-384": 17, "OWE": 18,
}
_CIPHER_NAMES = {
    "Use group cipher suite": 0, "WEP-40": 1, "TKIP": 2, "CCMP": 4, "WEP-104": 5,
    "AES-128-CMAC": 6, "NO-GROUP": 7, "GCMP-128": 8, "GCMP-256": 9, "CCMP-256": 10,
    "BIP-GMAC-128": 11, "BIP-GMAC-256": 12, "BIP-CMAC-256": 13,
}
assert set(_AKM_NAMES.values()) <= set(AKM_SUITES)
assert set(_CIPHER_NAMES.values()) <= set(CIPHER_SUITES)

_UNKNOWN_SUITE_RE = re.compile(r"00-0f-ac:(\d+)", re.I)


def _suites(text: str, names: dict[str, int]) -> list[int]:
    """``"PSK FT/PSK 00-0f-ac:24"`` → ``[2, 4, 24]``. Names may contain spaces.

    ``_parse_iw_scan`` splits its lists on whitespace, so callers join them
    back first: ``["IEEE", "802.1X"]`` is the one name ``IEEE 802.1X``.
    """
    ordered = sorted(names, key=len, reverse=True)
    out: list[int] = []
    rest = text.strip()
    while rest:
        m = _UNKNOWN_SUITE_RE.match(rest)
        if m:
            out.append(int(m.group(1)))
            rest = rest[m.end():].lstrip()
            continue
        for name in ordered:
            if rest.startswith(name) and (len(rest) == len(name) or rest[len(name)] == " "):
                out.append(names[name])
                rest = rest[len(name):].lstrip()
                break
        else:
            # A name this table does not know (another OUI): skip the token.
            rest = rest.partition(" ")[2]
    return out


def _cipher_list(tokens: list[str]) -> list[int] | None:
    return _suites(" ".join(tokens), _CIPHER_NAMES) or None


def _cipher(name: str | None) -> int | None:
    found = _suites(name or "", _CIPHER_NAMES)
    return found[0] if found else None


def to_rule_row(bss: dict[str, Any], frame: int) -> dict[str, Any]:
    """One ``_parse_iw_scan`` dict → one ``packet_rules`` row."""
    row: dict[str, Any] = {
        "frame": frame,
        "bssid": bss["bssid"],
        "ssid": bss["ssid"],
        "freq": bss["freq_mhz"],
        # The channel the AP advertises; a 2.4 GHz scan can hear it next door.
        "channel": (bss["advertised_channel"] if bss["advertised_channel"] is not None
                    else bss["channel"]),
        "signal": bss["signal_dbm"],
        "country": bss["country"],
        "beacon_interval": bss["beacon_interval_tu"],
        "dtim_period": bss["dtim_period"],
        "last_seen_ms": bss["last_seen_ms"],
        "privacy": bss["privacy"],
        "akm": None, "group_cipher": None, "pairwise_cipher": None,
        "mfpr": None, "mfpc": None,
        "wpa_version": None, "wpa_group_cipher": None, "wpa_pairwise_cipher": None,
    }
    if bss["rsn_present"]:
        akm = _suites(" ".join(bss["akm"]), _AKM_NAMES)
        if akm:
            row["akm"] = akm
        else:
            del row["akm"]
        row["group_cipher"] = _cipher(bss["group_cipher"])
        row["pairwise_cipher"] = _cipher_list(bss["pairwise_cipher"])
        row["mfpr"], row["mfpc"] = bss["mfpr"], bss["mfpc"]
    wpa1 = bss["wpa1"]
    if wpa1 is not None:
        row["wpa_version"] = wpa1["version"] if wpa1["version"] is not None else 1
        row["wpa_group_cipher"] = _cipher(wpa1["group_cipher"])
        row["wpa_pairwise_cipher"] = _cipher_list(wpa1["pairwise_cipher"])
    elif bss["privacy"] and "Privacy" not in bss["capability_names"]:
        for key in ("wpa_version", "wpa_group_cipher", "wpa_pairwise_cipher"):
            del row[key]
    return row


def rule_rows(bss_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every scanned BSS as a rule row, numbered from 1 like capture frames."""
    return [to_rule_row(bss, i) for i, bss in enumerate(bss_list, start=1)]
