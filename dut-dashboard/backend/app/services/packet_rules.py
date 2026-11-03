"""Packet Monitor findings: what is wrong in a capture, said in words.

Rules run on decoded rows (``packet_modes.decode_row``), never inside tshark,
so each one is testable from a dict. Two kinds:

* a **row rule** looks at one frame and returns ``(severity, message)`` or
  ``None``;
* a **group rule** looks at every frame of one group (one BSSID) at once.

A rule whose inputs are not in the row -- the Host's tshark could not resolve
the field -- stays silent. A key that is present with value ``None`` means the
frame did not carry the field, which some rules do read (no RSN element at
6 GHz is itself the finding).

A busy capture has thousands of beacons per BSS, so identical findings from one
group are collapsed into one, listing every frame they came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from app.services.packet_decode import (
    AKM_SUITES,
    CIPHER_SUITES,
    ENTERPRISE_SHA1,
    LEGACY_CIPHERS,
    OWE,
    PSK_FAMILY,
    SAE_FAMILY,
    band_of,
)

Severity = Literal["fail", "warn", "info"]
Row = dict[str, Any]


@dataclass
class Finding:
    rule: str
    severity: Severity
    message: str
    #: The value of the mode's ``group_by`` column (a BSSID), or None.
    group: Any = None
    frames: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "group": self.group,
            "frames": list(self.frames),
        }


def _has(row: Row, *keys: str) -> bool:
    return all(key in row for key in keys)


def _akm_names(akms: Iterable[int]) -> str:
    return " / ".join(AKM_SUITES.get(a, f"AKM {a}") for a in akms)


def _cipher_names(ciphers: Iterable[int]) -> str:
    return ", ".join(CIPHER_SUITES.get(c, f"Cipher {c}") for c in ciphers)


def _wpa3_akms(akms: list[int] | None) -> set[int]:
    return set(akms or ()) & (SAE_FAMILY | {OWE})


def _band(row: Row) -> str | None:
    band = band_of(row.get("freq"))
    if band is None and row.get("six_ghz_primary") is not None:
        return "6"
    return band


# --- row rules -----------------------------------------------------------------


def wpa3_requires_mfpr(row: Row):
    """WPA3-only (SAE family and/or OWE, nothing else) requires MFPR = 1.

    WPA3 Specification v3.x §2.2/§2.3 (SAE-only mode) and §2.5 (OWE): PMF
    required. Transition mode (PSK + SAE) is deliberately excluded: it
    advertises MFPC = 1, MFPR = 0 by design.
    """
    if not _has(row, "akm", "mfpr"):
        return None
    akms = row["akm"]
    if not akms or row["mfpr"] is not False:
        return None
    if set(akms) <= (SAE_FAMILY | {OWE}):
        return ("fail", f"AKM is {_akm_names(akms)} only (WPA3) but MFPR = 0; "
                        "WPA3-only modes require Management Frame Protection.")
    return None


def sae_requires_mfpc(row: Row):
    """Any SAE-family AKM requires MFPC = 1, including transition mode."""
    if not _has(row, "akm", "mfpc"):
        return None
    if _wpa3_akms(row["akm"]) & SAE_FAMILY and row["mfpc"] is False:
        return ("fail", f"AKM includes {_akm_names(sorted(set(row['akm']) & SAE_FAMILY))} "
                        "but MFPC = 0; SAE, including WPA3 transition mode, "
                        "requires Management Frame Protection capable.")
    return None


def mfpr_without_mfpc(row: Row):
    """MFPR = 1 with MFPC = 0 is an invalid RSN Capabilities combination.

    IEEE 802.11-2020 12.6.3: an entity that requires MFP shall also advertise
    that it is capable of it, whatever the AKM.
    """
    if not _has(row, "mfpr", "mfpc"):
        return None
    if row["mfpr"] is True and row["mfpc"] is False:
        return ("fail", "MFPR = 1 but MFPC = 0; an AP that requires Management "
                        "Frame Protection must also advertise it is capable of it.")
    return None


def no_legacy_cipher_with_wpa3(row: Row):
    """WPA3 (SAE family or OWE) must not be offered with TKIP or WEP."""
    if not _has(row, "akm", "group_cipher", "pairwise_cipher"):
        return None
    if not _wpa3_akms(row["akm"]):
        return None
    used = set(row["pairwise_cipher"] or ())
    if row["group_cipher"] is not None:
        used.add(row["group_cipher"])
    legacy = sorted(used & LEGACY_CIPHERS)
    if legacy:
        return ("fail", f"WPA3 AKM advertised together with {_cipher_names(legacy)}; "
                        "WPA3 does not allow TKIP or WEP.")
    return None


def rsn_not_evaluated(row: Row):
    """Say so when a Multiple BSSID beacon's own security could not be isolated.

    Silence would read as "nothing wrong"; see
    ``packet_modes._keep_transmitting_rsn`` for when this happens.
    """
    if row.get("rsn_unresolved"):
        return ("info", "Beacon carries a Multiple BSSID element and its own RSN "
                        "element could not be told apart from those of its "
                        "nontransmitted BSSes; security rules were not evaluated "
                        "for this frame.")
    return None


def six_ghz_security(row: Row):
    """A 6 GHz BSS: no open network, no legacy PSK or 802.1X SHA-1, no TKIP/WEP, MFPR = 1.

    6 GHz allows no WPA2 and no transition modes. In particular the WPA3
    Specification (§11.2, as referenced from its Enterprise section) says an
    AP does not operate a BSS in WPA3-Enterprise Transition Mode -- AKMs 1 + 5
    -- in the 6 GHz band, which rules out 802.1X SHA-1 (AKM 1). FT-802.1X
    (AKM 3) is part of WPA3-Enterprise Only Mode and stays allowed.
    """
    if not _has(row, "akm", "mfpr") or _band(row) != "6":
        return None
    akms = row["akm"]
    if not akms:
        return ("fail", "6 GHz BSS without an RSN element (open network); "
                        "6 GHz allows only RSN with PMF, e.g. SAE or OWE.")
    problems = []
    legacy_akms = sorted(set(akms) & (PSK_FAMILY | ENTERPRISE_SHA1))
    if legacy_akms:
        problems.append(f"offers {_akm_names(legacy_akms)}")
    used = set(row.get("pairwise_cipher") or ())
    if row.get("group_cipher") is not None:
        used.add(row["group_cipher"])
    legacy = sorted(used & LEGACY_CIPHERS)
    if legacy:
        problems.append(f"uses {_cipher_names(legacy)}")
    if row["mfpr"] is False:
        problems.append("has MFPR = 0")
    if problems:
        return ("fail", "6 GHz BSS " + "; ".join(problems) +
                        ". 6 GHz requires WPA3 (SAE/OWE/Enterprise) with PMF required.")
    return None


def hidden_ssid(row: Row):
    """Empty or all-NUL SSID: a hidden network. Information, not a fault."""
    if "ssid" not in row or row["ssid"] is None:
        return None
    if row["ssid"].strip("\x00") == "":
        return ("info", "Hidden SSID (empty or null SSID in beacons).")
    return None


def beacon_timing(row: Row):
    """Beacon interval other than 100 TU, or DTIM period above 3."""
    notes = []
    interval = row.get("beacon_interval")
    if interval is not None and interval != 100:
        notes.append(f"beacon interval is {interval} TU (common default 100)")
    dtim = row.get("dtim_period")
    if dtim is not None and dtim > 3:
        notes.append(f"DTIM period is {dtim} (higher values delay buffered "
                     "broadcast for power-saving clients)")
    if notes:
        return ("info", "Non-default timing: " + "; ".join(notes) + ".")
    return None


# --- group rules -----------------------------------------------------------------

#: Keys whose value should not change between beacons of one BSS in one capture.
STABLE_KEYS = ("ssid", "channel", "akm", "group_cipher", "pairwise_cipher", "mfpr", "mfpc")


def _comparable(value: Any) -> Any:
    # An AKM or cipher list in a different order is the same configuration.
    return tuple(sorted(value)) if isinstance(value, list) else value


def bss_config_changed(rows: list[Row]) -> list[tuple[Severity, str, list[int]]]:
    """The same BSSID advertises different settings at different times.

    Needs every beacon of the BSS, so it runs on the full row set; collapsing
    the table to one row per BSS is a display step that comes after.
    """
    out = []
    for key in STABLE_KEYS:
        if not all(key in row for row in rows):
            continue
        seen: dict[str, list[int]] = {}
        for row in rows:
            seen.setdefault(repr(_comparable(row[key])), []).append(row.get("frame"))
        if len(seen) > 1:
            frames = sorted(f for fs in seen.values() for f in fs if f is not None)
            out.append(("warn", f"{key} changed between beacons of this BSS "
                                f"({len(seen)} different values).", frames))
    return out


RowRule = Callable[[Row], "tuple[Severity, str] | None"]
GroupRule = Callable[[list[Row]], "list[tuple[Severity, str, list[int]]]"]

ROW_RULES: dict[str, RowRule] = {
    "wpa3_requires_mfpr": wpa3_requires_mfpr,
    "sae_requires_mfpc": sae_requires_mfpc,
    "mfpr_without_mfpc": mfpr_without_mfpc,
    "no_legacy_cipher_with_wpa3": no_legacy_cipher_with_wpa3,
    "six_ghz_security": six_ghz_security,
    "rsn_not_evaluated": rsn_not_evaluated,
    "hidden_ssid": hidden_ssid,
    "beacon_timing": beacon_timing,
}
GROUP_RULES: dict[str, GroupRule] = {
    "bss_config_changed": bss_config_changed,
}
RULES = frozenset(ROW_RULES) | frozenset(GROUP_RULES)

_SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2}


def evaluate(rows: list[Row], rule_ids: Iterable[str], group_by: str | None = None) -> list[Finding]:
    """Run the named rules over decoded rows; fail first, then warn, then info.

    Identical findings (same rule, group and message) collapse into one with
    every frame listed. A rule that raises is a bug in the rule; it is not
    swallowed here.
    """
    rule_ids = list(dict.fromkeys(rule_ids))
    unknown = [r for r in rule_ids if r not in RULES]
    if unknown:
        raise ValueError(f"Unknown rule(s): {unknown}")

    collapsed: dict[tuple[str, str | None, str], Finding] = {}

    def add(rule: str, severity: Severity, message: str, group: str | None, frames: list[int]) -> None:
        key = (rule, group, message)
        finding = collapsed.get(key)
        if finding is None:
            finding = collapsed[key] = Finding(rule, severity, message, group)
        finding.frames.extend(f for f in frames if f is not None)

    for row in rows:
        group = row.get(group_by) if group_by else None
        for rule in rule_ids:
            check = ROW_RULES.get(rule)
            if check is None:
                continue
            result = check(row)
            if result is not None:
                add(rule, result[0], result[1], group, [row.get("frame")])

    # Without a grouping key every frame would be one "group", and comparing
    # beacons of different networks with each other means nothing.
    group_rules = [r for r in rule_ids if r in GROUP_RULES]
    if group_rules and group_by:
        groups: dict[Any, list[Row]] = {}
        for row in rows:
            # A row whose group value is missing belongs to no known BSS;
            # pooling all of them would compare unrelated networks.
            if row.get(group_by) is not None:
                groups.setdefault(row[group_by], []).append(row)
        for group, members in groups.items():
            for rule in group_rules:
                for severity, message, frames in GROUP_RULES[rule](members):
                    add(rule, severity, message, group, frames)

    findings = list(collapsed.values())
    for finding in findings:
        finding.frames.sort()
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.rule,
                                 "" if f.group is None else str(f.group)))
    return findings
