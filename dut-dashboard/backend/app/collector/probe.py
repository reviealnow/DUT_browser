"""What a collector can tell us about the DUT consoles behind it.

The three things that stop a console from opening are always the same, and the
existing fleet guide makes somebody SSH in by hand to check each one: is there a
serial device, is `socat` installed, and is the login in `dialout`. A fourth --
is the port already busy -- is the one that costs a bench half an hour, because
`socat` fails with a permission error on a device that plainly exists and the
message never mentions the `minicom` somebody left running yesterday.

All four are answered over the session that is already open, so asking costs no
login and no serial time.

**An unanswerable question is reported as unanswered.** `fuser` is not on every
image; where it is missing, a device's `busy` is `None` and the caller says
"not checked" rather than "free". The whole point of this module is to stop
people guessing, so it must not guess either.
"""

from __future__ import annotations

from app.dut.registry import REMOTE_DEVICE_RE

#: Where USB serial adapters land on a Pi. Both, because a CDC-ACM adapter is
#: as ordinary as an FTDI one and a bench that has one will not have the other.
SERIAL_PATTERNS = ("/dev/ttyUSB*", "/dev/ttyACM*")

#: Membership that lets the login open a serial device at all. Without it socat
#: fails with `Permission denied` on a device that exists and is free.
SERIAL_GROUP = "dialout"


def _lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def probe(session) -> dict:
    """Ask one collector what it has. Returns a plain dict for the API."""
    listing, _ = session.run(f"ls -1 {' '.join(SERIAL_PATTERNS)} 2>/dev/null; true")
    # Filtered against the same expression the DUT registry accepts, because
    # every one of these ends up interpolated into a socat command line. A box
    # with a creatively named device gets it dropped rather than quoted and
    # hoped for.
    devices = [line for line in _lines(listing) if REMOTE_DEVICE_RE.fullmatch(line)]

    socat_path, socat_status = session.run("command -v socat 2>/dev/null; true")
    socat_path = socat_path.strip()

    groups_output, _ = session.run("id -nG 2>/dev/null; true")
    groups = groups_output.split()

    have_fuser, _ = session.run("command -v fuser >/dev/null 2>&1 && echo yes || echo no")
    can_check_busy = have_fuser.strip() == "yes"

    busy: dict[str, str] = {}
    if can_check_busy and devices:
        # One pass over every device rather than a command each: this is a
        # remote shell over SSH and the round trip dominates.
        script = "; ".join(
            f"printf '%s\\t' '{device}'; fuser '{device}' 2>/dev/null | tr -d '\\n'; printf '\\n'"
            for device in devices
        )
        table, _ = session.run(script)
        for line in _lines(table):
            name, _, holders = line.partition("\t")
            busy[name.strip()] = holders.strip()

    return {
        "hostname": session.reported_hostname,
        "socat": {"present": bool(socat_path), "path": socat_path or None},
        "serial_group": {
            "name": SERIAL_GROUP,
            "member": SERIAL_GROUP in groups,
            "groups": groups,
        },
        # Named so a reader can tell "checked, and free" from "nobody looked".
        "busy_check": "fuser" if can_check_busy else "unavailable",
        "devices": [
            {
                "device": device,
                # None means not checked. False means checked and free.
                "busy": bool(busy.get(device)) if can_check_busy else None,
                "held_by": busy.get(device) or None,
            }
            for device in devices
        ],
    }


def readiness(result: dict) -> list[str]:
    """Everything standing between this collector and an open console.

    Returned as sentences rather than flags: this list goes straight onto a card
    and the reader's next action is different for each one.
    """
    problems: list[str] = []
    if not result["socat"]["present"]:
        problems.append("socat is not installed on this collector (apt install socat).")
    if not result["serial_group"]["member"]:
        problems.append(
            f"The login is not in {SERIAL_GROUP}, so it cannot open a serial device "
            f"(usermod -aG {SERIAL_GROUP}, then log out and back in)."
        )
    if not result["devices"]:
        problems.append("No USB serial devices are present on this collector.")
    return problems
