"""Which serial ports exist, and which one a remembered name now refers to.

A USB serial adapter's device node is not stable. Unplugging the cable and
plugging it back in renumbers it -- `/dev/cu.PL2303G-USBtoUART11130` became
`...11120` within one bench session -- so the port the registry remembers for
one-click Connect stops existing the moment somebody recables the desk, and
Connect fails with a file-not-found on a DUT that is sitting right there.

Re-probing is therefore about repairing a name, not about finding a device to
talk to. That distinction is the whole design here, because the tempting
version -- "the remembered port is gone, open whichever serial port is there"
-- is how a DUT ends up logging under another DUT's id. This bench has several
cables and the registry treats the console a reading came from as load-bearing
(see `console_token`); attaching `default` to the cable that is actually
another device would file its telemetry, its crashes and its backhaul captures
under the wrong name, and nothing downstream could tell.

So the substitution is deliberately narrow:

  * the remembered port still exists -> use it, no substitution, no question;
  * it is gone and exactly ONE port of the SAME ADAPTER FAMILY is present ->
    that is a renumbering, and it is taken;
  * it is gone and SEVERAL candidates match -> refuse and name them. Two
    identical adapters are exactly the case where guessing is worst;
  * it is gone and nothing matches -> hand the name back unchanged and let the
    open fail with the operating system's own error, which is more accurate
    than anything this module could say about a cable it cannot see.

"Same adapter family" is the remembered name with its trailing digits removed:
the instance number is what changes, the model prefix is what does not.
"""

from __future__ import annotations

import os
import re

from serial.tools import list_ports


class PortUnresolved(RuntimeError):
    """No single, unambiguous port to open. The message is shown to the operator."""


def available_ports() -> list[dict]:
    """Every serial port this machine reports, in pyserial's own words."""
    return [
        {
            "device": info.device,
            "description": info.description or "",
            "hwid": info.hwid or "",
        }
        for info in list_ports.comports()
    ]


def adapter_family(port: str) -> str:
    """A remembered port name without its instance number.

    `/dev/cu.PL2303G-USBtoUART11130` -> `/dev/cu.PL2303G-USBtoUART`. Only the
    trailing digits go: a name that is all digits, or has none, is returned
    unchanged rather than collapsing to something that would match everything.
    """
    stem = re.sub(r"\d+$", "", port or "")
    return stem or port


def resolve_port(requested: str, ports: list[dict] | None = None) -> tuple[str, str | None]:
    """The port to actually open, and a note when it is not the one asked for.

    Raises `PortUnresolved` rather than choosing between candidates. Absent or
    blank input is passed straight back: an empty port is the caller's error to
    report, and this is not the place to invent one.
    """
    if not requested:
        return requested, None

    devices = [p["device"] for p in (available_ports() if ports is None else ports)]
    if requested in devices:
        return requested, None
    # `comports()` does not always list a node that is nevertheless openable
    # (a tty the OS knows and the enumerator does not). Trust the filesystem
    # before substituting anything: an existing device is never renumbered away.
    if os.path.exists(requested):
        return requested, None

    family = adapter_family(requested)
    candidates = [d for d in devices if adapter_family(d) == family and d != requested]
    if len(candidates) == 1:
        return candidates[0], (
            f"{requested} is gone; opened {candidates[0]}, the same adapter renumbered"
        )
    if not candidates:
        # Nothing to substitute, so get out of the way: the open proceeds and
        # fails with the operating system's own account of why the device is
        # not there. That is a fact, where a message from here would be this
        # module speculating about a cable it cannot see -- and it keeps every
        # caller that passes a port this enumeration simply does not know
        # working exactly as it did before.
        return requested, None
    raise PortUnresolved(
        f"{requested} no longer exists and {len(candidates)} ports of the same adapter"
        f" are attached ({', '.join(sorted(candidates))}). Pick one explicitly rather"
        " than risk opening another DUT's console."
    )
