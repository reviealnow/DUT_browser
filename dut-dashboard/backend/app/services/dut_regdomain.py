"""A DUT's SKU regulatory domain, and the countries its beacons may advertise.

The SKU is not a country: an AP6 is built for one of six domains, and each
domain covers a list of countries. A DUT's own beacons are checked against
that list (``packet_rules.country_mismatch``), so an EU unit advertising
``DE`` is fine and one advertising ``US`` is not.

The read is per model family because the command is. AP6 answers
``board_config get domain``; AP7 has its own command, not recorded yet, so
``command_for`` returns None and the caller skips the check rather than guess.

The table is transcribed from the per-SKU country list the QA team works from
(Sophos KBA-000009623). Puerto Rico, Guam and the other US territories are in
Rest of World there, not in the US domain, and are kept where the list puts
them. The US is domain 0 and Canada domain 1: an AP6_420 US SKU answers ``0``
(bench, 2026-11-04), so 0 is a SKU, not "never programmed".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Domain:
    number: int
    name: str
    countries: frozenset[str]

    @property
    def label(self) -> str:
        return f"domain {self.number} ({self.name})"


_EU = (
    "AD AT BE BG HR CY CZ DK EE FI FR DE GI GR HU IS IE IT LV LI LT "
    "LU MT MC NL NO PL PT RO SM SK SI ES SE CH TR GB VA"
)
_ROW = (
    "AF AX AL DZ AS AO AI AQ AG AR AM AW AZ BS BH BD BB BY BZ BJ BM "
    "BT BO BA BW BV BR VG IO BN BF BI KH CM CV KY CF TD CL CN CX CC "
    "CO KM CK CR CI DJ CD DM DO EC EG SV GQ ER SZ ET FK FO FJ GA GM "
    "GE GH GD GP GU GT GG GN GW GY HT HM HN HK ID IQ IL JM JP JO KZ "
    "KE KI KR KW KG LA LB LS LR LY MO MK MG MW MY MV ML MH MQ MR MU "
    "YT MX FM MD MN ME MS MA MZ MM NA NR NP AN NC NI NE NG NU NF MP "
    "OM PK PW PS PA PG PY PE PH PN PR QA CG RE RU RW SH KN LC PM VC "
    "BL MF WS ST SA SN RS SC SL SG SB SO ZA GS SS LK SD SR SJ TW TJ "
    "TZ TH TL TG TK TO TT TN TM TC TV UG UA AE UY UZ VU VE VN WF EH "
    "YE ZM"
)

DOMAINS: dict[int, Domain] = {
    d.number: d
    for d in (
        Domain(0, "US", frozenset({"US"})),
        Domain(1, "CA", frozenset({"CA"})),
        Domain(2, "EU", frozenset(_EU.split())),
        Domain(3, "AU/NZ", frozenset({"AU", "NZ"})),
        Domain(4, "IN", frozenset({"IN"})),
        Domain(5, "Rest of World", frozenset(_ROW.split())),
    )
}

#: Model family (``dut_model.detect_model(...)[:3]``) → console command.
_COMMANDS = {"AP6": "board_config get domain"}

_VALUE_RE = re.compile(r"^\s*(?:domain\s*[=:]?\s*)?(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def command_for(model: str | None) -> str | None:
    """The command that reads ``model``'s domain, or None when it is not known."""
    return _COMMANDS.get((model or "")[:3].upper())


def parse_domain(output: str) -> Domain | None:
    """Console output of the domain command → its Domain, or None.

    Accepts a bare number on its own line, or ``domain=N`` / ``domain: N``.
    The command echo is not a match (it has text before the number). A number
    the table does not know is None: an unknown SKU is not checked, never
    checked against a guessed one.
    """
    for m in _VALUE_RE.finditer(output or ""):
        domain = DOMAINS.get(int(m.group(1)))
        if domain is not None:
            return domain
    return None
