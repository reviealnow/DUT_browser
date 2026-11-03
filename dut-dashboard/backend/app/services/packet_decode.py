"""Turning `tshark -T fields` output into Python values, and values into text.

Packet Monitor (docs/design-packet-monitor.md) asks tshark for fields with

    -T fields -E header=y -E separator=/t -E occurrence=a -E aggregator=<AGGREGATOR>

so every cell is a string, a repeated field arrives as ``"2<US>8"``, and an
absent field is an empty cell. The aggregator is the ASCII unit separator, not
a comma: an SSID or an Info column may contain a comma, and a split on it would
cut one value in two. Everything here takes those strings and never raises:
a value that does not parse becomes ``None``, the same rule the console parsers
follow, because one odd frame must not cost the whole table.

**Known limit.** tshark exposes the AKM and cipher suite *type* but not, as a
separate field, its OUI. A vendor suite (e.g. 00-40-96 type 8) therefore reads
as the IEEE 00-0F-AC suite with the same number.

**The field type matters, not just the text.** tshark 4.x declares `wlan.ssid`
as ``FT_BYTES`` and prints it as hex (``6f70656e`` for "open"); 3.6 declares it
``FT_STRING`` and prints the text. Guessing from the text would turn an SSID
named "cafe" into two bytes, so decoders are handed the field's type from that
Host's ``tshark -G fields``.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

# --- IEEE 802.11 suite selectors (OUI 00-0F-AC) ------------------------------

#: AKM suite types, IEEE 802.11-2020 Table 9-151 plus the 802.11-2024 and
#: 802.11be additions.
AKM_SUITES: dict[int, str] = {
    1: "802.1X",
    2: "PSK",
    3: "FT-802.1X",
    4: "FT-PSK",
    5: "802.1X-SHA256",
    6: "PSK-SHA256",
    7: "TDLS",
    8: "SAE",
    9: "FT-SAE",
    10: "AP-PeerKey",
    11: "802.1X-Suite-B",
    12: "802.1X-Suite-B-192",
    13: "FT-802.1X-SHA384",
    14: "FILS-SHA256",
    15: "FILS-SHA384",
    16: "FT-FILS-SHA256",
    17: "FT-FILS-SHA384",
    18: "OWE",
    19: "FT-PSK-SHA384",
    20: "PSK-SHA384",
    21: "PASN",
    22: "FT-802.1X-SHA384",
    23: "802.1X-SHA384",
    24: "SAE-EXT-KEY",
    25: "FT-SAE-EXT-KEY",
}

#: Cipher suite types, IEEE 802.11-2020 Table 9-149.
CIPHER_SUITES: dict[int, str] = {
    0: "Use group cipher",
    1: "WEP-40",
    2: "TKIP",
    4: "CCMP-128",
    5: "WEP-104",
    6: "BIP-CMAC-128",
    7: "Group addressed traffic not allowed",
    8: "GCMP-128",
    9: "GCMP-256",
    10: "CCMP-256",
    11: "BIP-GMAC-128",
    12: "BIP-GMAC-256",
    13: "BIP-CMAC-256",
}

#: SAE and its FT / extended-key variants: what "WPA3-Personal" advertises.
SAE_FAMILY = frozenset({8, 9, 24, 25})
OWE = 18
#: Pre-WPA3 PSK AKMs, which a 6 GHz BSS must not offer.
PSK_FAMILY = frozenset({2, 4, 6, 19, 20})
#: 802.1X with SHA-1: WPA2-Enterprise, and the legacy half of WPA3-Enterprise
#: transition mode (AKMs 1 + 5), which the WPA3 specification says an AP does
#: not operate in the 6 GHz band. FT-802.1X (3) stays allowed: it is part of
#: WPA3-Enterprise only mode.
ENTERPRISE_SHA1 = frozenset({1})
#: Ciphers that WPA3 and 6 GHz forbid.
LEGACY_CIPHERS = frozenset({1, 2, 5})

MISSING = "<MISSING>"

#: Joins the occurrences of a repeated field (tshark ``-E aggregator=``).
AGGREGATOR = "\x1f"

_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}
_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2})*")
_INT_RE = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|[0-9]+)")


def _parts(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(AGGREGATOR)]


def _first(raw: str) -> str:
    return _parts(raw)[0]


def _to_int(text: str) -> int | None:
    # Narrower than int(text, 0), which also takes "1_000".
    if not _INT_RE.fullmatch(text):
        return None
    return int(text, 0)


def decode_int(raw: str, ftype: str | None = None) -> int | None:
    return _to_int(_first(raw))


def decode_int_list(raw: str, ftype: str | None = None) -> list[int] | None:
    values = [_to_int(part) for part in _parts(raw)]
    if any(v is None for v in values):
        return None
    return values  # type: ignore[return-value]


def decode_float(raw: str, ftype: str | None = None) -> float | None:
    try:
        value = float(_first(raw))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def decode_bool(raw: str, ftype: str | None = None) -> bool | None:
    text = _first(raw).lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def decode_text(raw: str, ftype: str | None = None) -> str | None:
    """Plain text, or bytes-as-hex decoded to text when the field is FT_BYTES.

    ``<MISSING>`` is tshark's word for a zero-length value -- for an SSID, a
    hidden network -- so it decodes to ``""``, not ``None``: the element was
    there, it was empty.

    A repeated field (a beacon with a Multiple BSSID element carries more than
    one SSID) yields the first occurrence: the one the frame is about.

    Bytes that are not valid UTF-8 -- Big5 SSIDs still turn up on legacy APs --
    are not guessed at, nor shown as replacement characters that would make two
    different SSIDs look alike: the value becomes ``0x`` plus the hex, which is
    exact and still compares equal only to itself.
    """
    first = raw.split(AGGREGATOR, 1)[0]
    if first == MISSING:
        return ""
    if ftype == "FT_BYTES":
        if not _HEX_RE.fullmatch(first):
            return None
        try:
            return bytes.fromhex(first).decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + first.lower()
    return first


def decode_mac(raw: str, ftype: str | None = None) -> str | None:
    text = _first(raw).lower()
    return text if re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text) else None


def decode_rates(raw: str, ftype: str | None = None) -> list[tuple[float, bool]] | None:
    """Supported Rates bytes → ``[(Mbps, is_basic), ...]``.

    Each byte is the rate in 500 kb/s units, top bit set for a basic rate.
    Values 0xFA-0xFF are BSS membership selectors (SAE H2E only, HT/VHT/HE
    PHY, ...), not rates, and are left out.
    """
    values = decode_int_list(raw)
    if values is None:
        return None
    return [((v & 0x7F) / 2, bool(v & 0x80)) for v in values if v < 0xFA]


Decoder = Callable[[str, "str | None"], Any]

DECODERS: dict[str, Decoder] = {
    "int": decode_int,
    "int_list": decode_int_list,
    "float": decode_float,
    "bool": decode_bool,
    "text": decode_text,
    "mac": decode_mac,
    "rates": decode_rates,
    "akm_list": decode_int_list,
    "cipher": decode_int,
    "cipher_list": decode_int_list,
}


def decode(decoder: str, raw: str | None, ftype: str | None = None) -> Any:
    """One cell → value. Empty or unparseable → ``None``; never raises."""
    if raw is None or raw == "":
        return None
    try:
        return DECODERS[decoder](raw, ftype)
    except (ValueError, UnicodeError, IndexError):
        return None


# --- display -----------------------------------------------------------------


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def format_value(decoder: str, value: Any) -> str:
    """A decoded value as the table shows it. ``None`` → ``"—"``."""
    if value is None:
        return "—"
    if decoder == "akm_list":
        return " / ".join(AKM_SUITES.get(v, f"AKM {v}") for v in value)
    if decoder == "cipher":
        return CIPHER_SUITES.get(value, f"Cipher {value}")
    if decoder == "cipher_list":
        return ", ".join(CIPHER_SUITES.get(v, f"Cipher {v}") for v in value)
    if decoder == "bool":
        return "Yes" if value else "No"
    if decoder == "rates":
        return ", ".join(_fmt_num(r) + ("(B)" if basic else "") for r, basic in value)
    if decoder == "int_list":
        return ", ".join(str(v) for v in value)
    if decoder == "text":
        return value if value.strip("\x00") else "(hidden)"
    return str(value)


def band_of(freq_mhz: int | None) -> str | None:
    """Frequency → ``"2.4"`` / ``"5"`` / ``"6"`` GHz band, or ``None``."""
    if freq_mhz is None:
        return None
    if 2400 <= freq_mhz <= 2500:
        return "2.4"
    if 4900 <= freq_mhz < 5925:
        return "5"
    if 5925 <= freq_mhz <= 7125:
        return "6"
    return None


# --- tshark output -------------------------------------------------------------


def rows_from_tsv(text: str) -> tuple[list[dict[str, str | None]], int]:
    """``-T fields -E header=y -E separator=/t`` output → (one dict per frame, dropped).

    Keys are the field names from the header line; an empty cell is ``None``.
    Rows split on ``\\n`` only -- ``str.splitlines`` would also break on
    U+2028, ``\\x0b`` and friends, which a 3.6-era text SSID can contain. A
    line with the wrong number of cells (a tab inside a text value) is dropped
    rather than misaligned, and counted, so the caller can say so.
    """
    lines = [line.rstrip("\r") for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return [], 0
    header = lines[0].split("\t")
    rows: list[dict[str, str | None]] = []
    dropped = 0
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) != len(header):
            dropped += 1
            continue
        rows.append({name: (cell if cell != "" else None) for name, cell in zip(header, cells)})
    return rows, dropped
