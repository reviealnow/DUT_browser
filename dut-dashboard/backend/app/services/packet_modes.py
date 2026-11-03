"""Packet Monitor Test Modes: which frames, which fields, which rules.

A Test Mode is data, not code (docs/design-packet-monitor.md §B6). Each column
names an ordered list of candidate tshark fields. Resolving a mode against one
Host's ``tshark -G fields`` keeps every candidate that Host knows, and decoding
a frame takes the first candidate that has a value. One mechanism covers both
problems this has to absorb:

* **renames between tshark releases** -- ``_ws.col.Protocol`` in 3.x is
  ``_ws.col.protocol`` in 4.x, so only one of the two resolves;
* **per-frame fallbacks** -- the channel a BSS *advertises* comes from the HE
  Operation element's 6 GHz information, else HT Operation, else the DS
  Parameter Set, and only as a last resort from the radio header -- which is
  the channel the *sniffer* was tuned to, and on 2.4 GHz is routinely one or
  two channels off. The radio channel is shown separately as "Captured On".

A column none of whose candidates resolve is left out of the decoded row
altogether, which is how a rule tells "this tshark cannot see the field"
(skip) from "this frame does not carry it" (``None``).

**Multiple BSSID.** A beacon can carry the RSN elements of nontransmitted
BSSes inside its Multiple BSSID element, and tshark reports them under the
same field names as the transmitting BSS's own. With ``occurrence=a`` they
would merge: an SAE-only BSS would read as PSK + SAE, an open one as SAE.
:func:`decode_row` therefore keeps only the transmitting BSS's own RSN element
-- see :func:`_keep_transmitting_rsn` -- and says so when it cannot tell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services import packet_decode, packet_rules

#: Field names reach a remote shell (quoted) and must also be tshark field
#: names, so the set is kept narrow on purpose. Always used with fullmatch:
#: ``$`` alone would let a trailing newline through.
FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")

DecoderName = Literal[
    "int", "int_list", "float", "bool", "text", "mac", "rates",
    "akm_list", "cipher", "cipher_list",
]


class Column(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    fields: list[str] = Field(min_length=1)
    decoder: DecoderName = "text"
    #: Extracted for rules but not shown as a table column.
    hidden: bool = False

    @field_validator("fields")
    @classmethod
    def _field_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not FIELD_RE.fullmatch(name):
                raise ValueError(f"not a tshark field name: {name!r}")
        return value


class TestMode(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    description: str
    display_filter: str
    #: Column key whose value groups frames (one BSS = one group), or None.
    group_by: str | None = None
    columns: list[Column]
    rules: list[str] = []

    @model_validator(mode="after")
    def _consistent(self) -> "TestMode":
        keys = [c.key for c in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.id}: duplicate column key")
        if "frame" not in keys:
            raise ValueError(f"{self.id}: every mode needs a 'frame' column")
        if self.group_by is not None and self.group_by not in keys:
            raise ValueError(f"{self.id}: group_by {self.group_by!r} is not a column")
        if len(self.rules) != len(set(self.rules)):
            raise ValueError(f"{self.id}: duplicate rule")
        for rule in self.rules:
            if rule not in packet_rules.RULES:
                raise ValueError(f"{self.id}: unknown rule {rule!r}")
            if rule in packet_rules.GROUP_RULES and self.group_by is None:
                raise ValueError(f"{self.id}: group rule {rule!r} needs group_by")
        return self


# --- column building blocks ----------------------------------------------------

_FRAME = Column(key="frame", label="No.", fields=["frame.number"], decoder="int")
_BSSID = Column(key="bssid", label="BSSID", fields=["wlan.bssid"], decoder="mac")
_SSID = Column(key="ssid", label="SSID", fields=["wlan.ssid"], decoder="text")
_CHANNEL = Column(
    key="channel", label="Channel", decoder="int",
    fields=[
        "wlan.ext_tag.he_operation.6ghz.primary_channel",
        "wlan.ht.info.primarychannel",
        "wlan.ds.current_channel",
        "wlan_radio.channel",
    ],
)
#: Where the sniffer was tuned, not what the AP advertises; never used by rules.
_CAPTURED_ON = Column(
    key="captured_on", label="Captured On (ch)", decoder="int",
    fields=["wlan_radio.channel"],
)
_FREQ = Column(
    key="freq", label="Frequency (MHz)", decoder="int",
    fields=["wlan_radio.frequency", "radiotap.channel.freq"],
)
#: Present only in a 6 GHz BSS's HE Operation element: the band signal when a
#: capture carries no radio header to read a frequency from.
_SIX_GHZ_PRIMARY = Column(
    key="six_ghz_primary", label="6 GHz Primary Channel", decoder="int", hidden=True,
    fields=["wlan.ext_tag.he_operation.6ghz.primary_channel"],
)
_PRIVACY = Column(key="privacy", label="Privacy", fields=["wlan.fixed.capabilities.privacy"],
                  decoder="bool")
_SECURITY = [
    _PRIVACY,
    # The pre-RSN WPA (v1) vendor element: its presence is the finding.
    Column(key="wpa_version", label="WPA1 IE Version", fields=["wlan.wfa.ie.wpa.version"],
           decoder="int"),
    Column(key="wpa_group_cipher", label="WPA1 Group Cipher", fields=["wlan.wfa.ie.wpa.mcs.type"],
           decoder="cipher"),
    Column(key="wpa_pairwise_cipher", label="WPA1 Pairwise Ciphers",
           fields=["wlan.wfa.ie.wpa.ucs.type"], decoder="cipher_list"),
    Column(key="group_cipher", label="RSN Group Cipher", fields=["wlan.rsn.gcs.type"], decoder="cipher"),
    Column(key="pairwise_cipher", label="RSN Pairwise Ciphers", fields=["wlan.rsn.pcs.type"], decoder="cipher_list"),
    Column(key="akm", label="RSN AKM", fields=["wlan.rsn.akms.type"], decoder="akm_list"),
    Column(key="mfpr", label="MFPR", fields=["wlan.rsn.capabilities.mfpr"], decoder="bool"),
    Column(key="mfpc", label="MFPC", fields=["wlan.rsn.capabilities.mfpc"], decoder="bool"),
    # Element structure, so decode_row can keep the transmitting BSS's own RSN.
    Column(key="tag_numbers", label="Element IDs", fields=["wlan.tag.number"],
           decoder="int_list", hidden=True),
    Column(key="akm_count", label="AKM Count", fields=["wlan.rsn.akms.count"],
           decoder="int_list", hidden=True),
    Column(key="pcs_count", label="Pairwise Cipher Count", fields=["wlan.rsn.pcs.count"],
           decoder="int_list", hidden=True),
]
_BEACON_TIMING = [
    Column(key="beacon_interval", label="Beacon Interval (TU)", fields=["wlan.fixed.beacon"], decoder="int"),
    Column(key="dtim_period", label="DTIM Period", fields=["wlan.tim.dtim_period"], decoder="int"),
]

BEACON_FILTER = "wlan.fc.type_subtype == 0x0008"

MODES: tuple[TestMode, ...] = (
    TestMode(
        id="beacon_security",
        label="Single Beacon Monitor",
        description="One row per BSS: SSID, channel and the RSN security it advertises.",
        display_filter=BEACON_FILTER,
        group_by="bssid",
        columns=[_FRAME, _BSSID, _SSID, _CHANNEL, _CAPTURED_ON, _FREQ, _SIX_GHZ_PRIMARY, *_SECURITY],
        rules=[
            "rsn_not_evaluated",
            "wpa3_requires_mfpr",
            "sae_requires_mfpc",
            "mfpr_without_mfpc",
            "no_legacy_cipher_with_wpa3",
            "six_ghz_security",
            "legacy_security",
            "open_network",
            "bss_config_changed",
        ],
    ),
    TestMode(
        id="wifi_basic",
        label="Wi-Fi Basic Settings Test",
        description="SSID, band, channel, country, rates and beacon timing per BSS.",
        display_filter=BEACON_FILTER,
        group_by="bssid",
        columns=[
            _FRAME, _BSSID, _SSID, _CHANNEL, _CAPTURED_ON, _FREQ, _SIX_GHZ_PRIMARY,
            Column(key="country", label="Country", fields=["wlan.country_info.code"], decoder="text"),
            Column(key="rates", label="Supported Rates (Mbps)",
                   fields=["wlan.supported_rates"], decoder="rates"),
            Column(key="ext_rates", label="Extended Rates (Mbps)",
                   fields=["wlan.extended_supported_rates"], decoder="rates"),
            Column(key="signal", label="Signal (dBm)",
                   fields=["wlan_radio.signal_dbm", "radiotap.dbm_antsignal"], decoder="int"),
            _PRIVACY,
            *_BEACON_TIMING,
        ],
        rules=["open_network", "hidden_ssid", "beacon_timing", "bss_config_changed"],
    ),
    TestMode(
        id="wifi_advanced",
        label="Wi-Fi Advanced Settings Test",
        description="Beacon timing and capability bits per BSS.",
        display_filter=BEACON_FILTER,
        group_by="bssid",
        columns=[
            _FRAME, _BSSID, _SSID, _CHANNEL,
            *_BEACON_TIMING,
            Column(key="dtim_count", label="DTIM Count", fields=["wlan.tim.dtim_count"], decoder="int"),
            Column(key="short_slot", label="Short Slot Time",
                   fields=["wlan.fixed.capabilities.short_slot_time"], decoder="bool"),
            Column(key="short_preamble", label="Short Preamble",
                   fields=["wlan.fixed.capabilities.short_preamble"], decoder="bool"),
            Column(key="ht_width_any", label="HT Any Channel Width",
                   fields=["wlan.ht.info.chanwidth"], decoder="bool"),
            Column(key="vht_width", label="VHT Channel Width",
                   fields=["wlan.vht.op.channelwidth"], decoder="int"),
        ],
        rules=["beacon_timing"],
    ),
    TestMode(
        id="manual",
        label="Manual",
        description="Every frame the display filter keeps, Wireshark's summary columns.",
        display_filter="",
        columns=[
            _FRAME,
            Column(key="time", label="Time", fields=["frame.time_relative"], decoder="float"),
            Column(key="src", label="Source", fields=["wlan.ta", "wlan.sa", "eth.src"], decoder="mac"),
            Column(key="dst", label="Destination", fields=["wlan.ra", "wlan.da", "eth.dst"], decoder="mac"),
            Column(key="protocol", label="Protocol",
                   fields=["_ws.col.protocol", "_ws.col.Protocol"], decoder="text"),
            Column(key="info", label="Info", fields=["_ws.col.info", "_ws.col.Info"], decoder="text"),
        ],
    ),
)

_BY_ID = {mode.id: mode for mode in MODES}
if len(_BY_ID) != len(MODES):
    raise ValueError("duplicate Test Mode id")


def get_mode(mode_id: str) -> TestMode:
    try:
        return _BY_ID[mode_id]
    except KeyError:
        raise ValueError(f"Unknown Test Mode: {mode_id!r}") from None


# --- resolving against one Host's tshark ------------------------------------------


@dataclass(frozen=True)
class ResolvedColumn:
    key: str
    label: str
    decoder: str
    hidden: bool
    #: Candidates this tshark knows, in the mode's order, with their FT_* type.
    fields: tuple[tuple[str, str], ...]


def resolve_columns(mode: TestMode, available: Mapping[str, str]) -> list[ResolvedColumn]:
    """Keep the columns this tshark can produce.

    ``available`` maps field name → FT_* type, from ``tshark -G fields``.
    """
    resolved = []
    for column in mode.columns:
        known = tuple((name, available[name]) for name in column.fields if name in available)
        if known:
            resolved.append(ResolvedColumn(
                key=column.key, label=column.label, decoder=column.decoder,
                hidden=column.hidden, fields=known,
            ))
    return resolved


def extraction_fields(columns: list[ResolvedColumn]) -> list[str]:
    """The ``-e`` list for tshark: every resolved candidate, each once."""
    seen: dict[str, None] = {}
    for column in columns:
        for name, _ftype in column.fields:
            seen.setdefault(name, None)
    return list(seen)


MBSSID_ELEMENT = 71
RSN_ELEMENT = 48
RSN_KEYS = ("akm", "group_cipher", "pairwise_cipher", "mfpr", "mfpc")
WPA_KEYS = ("wpa_version", "wpa_group_cipher", "wpa_pairwise_cipher")
#: Set on a row whose transmitting-BSS security could not be isolated.
RSN_UNRESOLVED = "rsn_unresolved"


def _keep_transmitting_rsn(row: dict[str, Any]) -> None:
    """Reduce the RSN columns to the transmitting BSS's own RSN element.

    ``wlan.tag.number`` lists every element in the frame in order, nested ones
    included, but carries no nesting. What *can* be told from it: an element
    before the first Multiple BSSID element is not inside one. IEEE 802.11
    orders RSN ahead of Multiple BSSID in a beacon, so a compliant AP's own
    RSN element is the first one, and its AKM and pairwise-cipher lists are the
    first ``akms.count`` / ``pcs.count`` values. Scalars (group cipher, MFPR,
    MFPC) already decode to their first occurrence.

    When the first RSN element comes *after* a Multiple BSSID element, the
    transmitting BSS is either open or non-compliant in its element order. The
    Privacy capability bit -- a fixed field, always the transmitting BSS's own
    -- tells the two apart: Privacy = 0 means it is open, so its RSN columns
    are ``None`` and the open-network rules judge it (at 6 GHz, a fail).
    Privacy = 1, or Privacy unknown, stays ambiguous.

    Ambiguous rows have their RSN columns dropped -- so no rule judges another
    BSS's settings -- and are flagged. That also happens when the frame has
    more than one RSN element and the per-element counts are missing (an
    older tshark without ``akms.count`` / ``pcs.count``) or do not add up to
    the lists: without them the lists cannot be split, and a merged list is
    exactly the wrong answer this function exists to prevent.

    The structure helper columns are removed either way.
    """
    tags = row.pop("tag_numbers", None)
    counts = {"akm": row.pop("akm_count", None), "pairwise_cipher": row.pop("pcs_count", None)}
    if tags and MBSSID_ELEMENT in tags:
        # The WPA (v1) element is vendor-specific (221), the same element ID as
        # WMM and every other vendor element, so its position cannot say whose
        # it is. In a Multiple BSSID beacon the WPA columns are dropped rather
        # than attributed to the transmitting BSS on a guess.
        for key in WPA_KEYS:
            row.pop(key, None)
    if not tags or RSN_ELEMENT not in tags:
        return

    def unresolved() -> None:
        for key in RSN_KEYS:
            row.pop(key, None)
        row[RSN_UNRESOLVED] = True

    first_rsn = tags.index(RSN_ELEMENT)
    if MBSSID_ELEMENT in tags and tags.index(MBSSID_ELEMENT) < first_rsn:
        if row.get("privacy") is False:
            for key in RSN_KEYS:
                row[key] = None
        else:
            unresolved()
        return
    several = tags.count(RSN_ELEMENT) > 1
    for key, per_element in counts.items():
        values = row.get(key)
        if values is None:
            continue
        if not per_element:
            if several:
                unresolved()
                return
            continue
        if sum(per_element) != len(values):
            unresolved()
            return
        row[key] = values[: per_element[0]]


def decode_row(columns: list[ResolvedColumn], raw: Mapping[str, str | None]) -> dict[str, Any]:
    """One frame's cells → ``{column key: value}``; first candidate with a value wins.

    Rows of a mode with the RSN structure columns are then reduced to the
    transmitting BSS's own security (:func:`_keep_transmitting_rsn`).
    """
    row: dict[str, Any] = {}
    for column in columns:
        value = None
        for name, ftype in column.fields:
            value = packet_decode.decode(column.decoder, raw.get(name), ftype)
            if value is not None:
                break
        row[column.key] = value
    if "tag_numbers" in row:
        _keep_transmitting_rsn(row)
    return row
