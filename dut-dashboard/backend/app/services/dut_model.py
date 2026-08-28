"""Which AP6 model a DUT is, and how its athN interfaces map onto radio bands.

The numbering is not a constant, and a width alone does not describe it. Two
things vary::

    AP6 420    2.4GHz ath0-7    5GHz ath8-15                     no 6GHz radio
    AP6 420E   2.4GHz ath0-7    5GHz ath8-15    6GHz ath16-23
    AP6 420X   2.4GHz ath0-7    5GHz ath8-15                     no 6GHz radio
    AP6 840    2.4GHz ath0-15   5GHz ath16-31                    no 6GHz radio
    AP6 840E   2.4GHz ath0-15   5GHz ath16-31   6GHz ath32-47

`band_for_iface` used to assume sixteen-wide, three-band, for everything. That
is the 840E row, and it is wrong twice over:

* on the 420 family the blocks are eight wide, so ath8-15 (5GHz) came back
  "2.4G" and ath16-23 (6GHz) came back "5G";
* on any model without a 6GHz radio there is no third block at all, so an
  interface past the second one is not "6G" -- it is a number that model cannot
  produce, and answering "6G" invents a band the hardware does not have.

Both matter most exactly where the guess is used. A mesh backhaul may sit on
2.4, 5 or 6 GHz, and the VAP behind it is named by number in `wlanconfig`
output, which states no frequency at all.

The model is read from the console prompt, which every DUT prints unprompted
and which therefore costs no serial time -- the connect-time capture races
sysMon for the line and loses, so anything needing a command is unreliable
here. `hostname` says the same thing in a different spelling
(``AP6420E-PB1005QPCFVFMA8``); both are accepted.

Measured on the bench 420E: ath0/1/6 at 2412 MHz, ath8/9 at 5660 MHz, ath16/17
at 6775 MHz. The block *starts* -- 0, 8, 16 -- are therefore observed, which is
what fixes the width at eight. `iw dev` also lists one ``wifiN`` radio per band
(wifi0/1/2 there), an independent witness that this model has three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BAND_24 = "2.4G"
BAND_5 = "5G"
BAND_6 = "6G"

# Anchored to a line start so a model name quoted inside somebody's log line is
# not mistaken for the device saying what it is. The suffix is part of the
# identity: E has a 6GHz radio, X does not.
_MODEL_RE = re.compile(r"(?m)^\s*(AP6)_?(\d{3})([EX]?)\b", re.IGNORECASE)

# The device's own name -- ``AP6420E-PB1005QPCFVFMA8`` -- which unlike the prompt
# identifies one physical unit rather than a model. That distinction is the whole
# reason this exists: the prompt cannot tell two 420Es apart, so a reading taken
# on one of them survives being swapped for the other with nothing to say so.
#
# Not anchored to a line start the way `_MODEL_RE` is, and it does not need to
# be: the model prefix AND a serial are both required, which no prose line about
# a device happens to contain. It is read from the output of `hostname`, where a
# prompt echoed on the same line would otherwise defeat an anchor.
_DEVICE_ID_RE = re.compile(r"\bAP6\d{3}[EX]?-[A-Z0-9]{6,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class ModelSpec:
    """Everything this codebase knows a model decides.

    One table rather than one per property. `cores` arrived after `per_band`
    and `bands`, and a second dict keyed by the same model names is how two
    tables come to disagree about which models exist.
    """

    per_band: int
    bands: tuple[str, ...]
    #: Physical CPU cores. Not a band fact, but a model fact, and the card needs
    #: it to notice a snapshot recorded on different hardware.
    cores: int


# What the code assumed for every model before any of this existed. Kept as the
# fallback for an unrecognised device so that case is no worse off than it was.
DEFAULT_SPEC = ModelSpec(per_band=16, bands=(BAND_24, BAND_5, BAND_6), cores=4)

_SPECS: dict[str, ModelSpec] = {
    "AP6_420": ModelSpec(8, (BAND_24, BAND_5), cores=2),
    "AP6_420E": ModelSpec(8, (BAND_24, BAND_5, BAND_6), cores=2),
    "AP6_420X": ModelSpec(8, (BAND_24, BAND_5), cores=2),
    "AP6_840": ModelSpec(16, (BAND_24, BAND_5), cores=4),
    "AP6_840E": ModelSpec(16, (BAND_24, BAND_5, BAND_6), cores=4),
}


def detect_model(text: str) -> str | None:
    """The AP6 model named in a console prompt or hostname, or None.

    Returns the canonical spelling used everywhere else in this codebase --
    ``AP6_420E`` -- whichever spelling the device used.
    """
    match = _MODEL_RE.search(text or "")
    if not match:
        return None
    return f"AP6_{match.group(2)}{match.group(3).upper()}"


def detect_device_id(text: str) -> str | None:
    """The one unit named in console output, or None.

    ``AP6420E-PB1005QPCFVFMA8``: model, then the serial that makes it this
    device and not another of the same model. Upper-cased so two spellings of
    one device never compare unequal and get read as a swap.

    None means "no answer", never "a different device". Every caller has to keep
    those apart -- a `hostname` that raced sysMon and came back empty must leave
    a stored identity alone, not overwrite it with nothing.
    """
    match = _DEVICE_ID_RE.search(text or "")
    return match.group(0).upper() if match else None


def spec_for(model: str | None) -> ModelSpec:
    """What is known about a model, or the old sixteen-wide assumption."""
    if not model:
        return DEFAULT_SPEC
    canonical = detect_model(model)
    if canonical is None:
        return DEFAULT_SPEC
    return _SPECS.get(canonical, DEFAULT_SPEC)


def vaps_per_band(model: str | None) -> int:
    """How many VAPs this model puts in each band."""
    return spec_for(model).per_band


def bands_for(model: str | None) -> tuple[str, ...]:
    """The radio bands this model has, in interface order."""
    return spec_for(model).bands


def cores_for(model: str | None) -> int | None:
    """This model's CPU core count, or None when the model is unknown.

    None rather than a default on purpose. The caller compares this against a
    snapshot's core count to spot a reading taken on other hardware, and a
    guessed number there would manufacture the very mismatch it is looking for.
    """
    if not model or detect_model(model) not in _SPECS:
        return None
    return spec_for(model).cores
