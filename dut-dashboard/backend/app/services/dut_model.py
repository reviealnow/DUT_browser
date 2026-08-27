"""Which AP6 model a DUT is, and how many VAPs its firmware puts in each band.

The interface numbering is not a constant. Measured on the bench, on the DUT
whose prompt reads ``AP6_420E#``::

    ath0  2.412 GHz    ath8  5.66 GHz    ath16  6.775 GHz
    ath1  2.412 GHz    ath9  5.66 GHz    ath17  6.775 GHz
    ath6  2.412 GHz

so the blocks are eight wide, not sixteen:

    AP6 420 / 420E    2.4GHz ath0-7    5GHz ath8-15    6GHz ath16-23
    AP6 840 / 840E    2.4GHz ath0-15   5GHz ath16-31   6GHz ath32-47

`band_for_iface()` assumed sixteen for every model. On this 420E that is wrong
for ath8, ath9, ath16 and ath17 — four of the seven active VAPs — and wrong in
the most expensive way, because "5G" for a 6 GHz interface is a plausible answer
nobody re-checks. It is only ever a fallback for output that states no
frequency, and the one caller in that position discards the value today, so
nothing user-visible was wrong; that is luck, not design.

The model is read from the console prompt, which every DUT prints unprompted
and which therefore costs no serial time — the connect-time capture races
sysMon for the line and loses, so anything that needs a command is unreliable
here. `hostname` says the same thing in a different spelling
(``AP6420E-PB1005QPCFVFMA8``); both are accepted.
"""

from __future__ import annotations

import re

# The prompt (``AP6_420E#``) and the hostname (``AP6420E-PB1005…``) differ only
# by the underscore and what follows, so one pattern reads both. Anchored to a
# line start so a model name quoted inside somebody's log line is not mistaken
# for the device's own identity.
_MODEL_RE = re.compile(r"(?m)^\s*(AP6)_?(\d{3})(E?)\b", re.IGNORECASE)

# VAPs per band, by model number. Unlisted models get no answer rather than a
# guess: a wrong band reads exactly like a right one.
_VAPS_PER_BAND: dict[str, int] = {
    "420": 8,
    "840": 16,
}

DEFAULT_VAPS_PER_BAND = 16


def detect_model(text: str) -> str | None:
    """The AP6 model named in a console prompt or hostname, or None.

    Returns the canonical spelling used everywhere else in this codebase --
    ``AP6_420E`` -- whichever spelling the device used.
    """
    match = _MODEL_RE.search(text or "")
    if not match:
        return None
    return f"AP6_{match.group(2)}{match.group(3).upper()}"


def model_number(model: str | None) -> str | None:
    """The bare number (``"420"``) from a canonical model, for table lookups."""
    if not model:
        return None
    match = _MODEL_RE.search(model)
    return match.group(2) if match else None


def vaps_per_band(model: str | None) -> int:
    """How many VAPs this model puts in each band.

    Falls back to sixteen for an unknown model, which is what
    `band_for_iface()` assumed before any of this existed: the fallback is no
    worse than it was, and a known model is now right.
    """
    number = model_number(model)
    if number is None:
        return DEFAULT_VAPS_PER_BAND
    return _VAPS_PER_BAND.get(number, DEFAULT_VAPS_PER_BAND)
