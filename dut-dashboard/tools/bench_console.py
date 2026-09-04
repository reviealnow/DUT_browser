#!/usr/bin/env python3
"""Ask a DUT who it is and what it says about its mesh, over one console.

The question every bench session starts with -- *what is actually on this
port?* -- answered without starting the stack. Every DUT route in the app is
engineer-gated, so reaching them from a script means a role passcode and a
session; this borrows the app's own `SerialWorker` instead and talks to the
console directly. Same transport the product uses, none of the auth.

Read-only. It runs `hostname`, a core count, the shipped mesh command and
`iw dev`, and hands the port back in a `finally` -- the serial line is
single-owner and shared with other sessions (see ../../CLAUDE.md).

    python3 tools/bench_console.py --port /dev/cu.PL2303G-USBtoUART1130
    python3 tools/bench_console.py --ssh-host 192.168.30.124 \\
        --ssh-user nelson --ssh-key ~/.ssh/dut_fleet_ed25519 \\
        --ssh-device /dev/ttyUSB0

`lsof /dev/cu.* /dev/tty.*` first: if the backend is holding the port this fails
with `Resource busy`, and if this is holding it the backend cannot open it. Both
node families, because a `minicom` opens the *tty* twin of the same adapter and
blocks the `cu.*` node without showing up in a `cu.*`-only listing at all.

**A console that answers nothing is usually waiting for a login.** The DUT
requires one after every reboot and only a person can give it; an empty
`hostname` here means ask the operator rather than retrying.

This is a diagnostic aid, not a test. What it prints is whatever the device
said -- the parsers behind it are covered in `backend/tests/test_dut_model.py`,
`test_mesh_topology.py` and `test_mesh_probe.py`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.parser.sysmon_parser import SysMonParser  # noqa: E402
from app.serial.serial_worker import SerialWorker  # noqa: E402
from app.services import dut_model, mesh_topology  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial device, e.g. /dev/cu.PL2303G-USBtoUART1130")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--ssh-host", help="Pi holding the console; switches to SSH transport")
    parser.add_argument("--ssh-user", default="nelson")
    parser.add_argument("--ssh-key", default="~/.ssh/dut_fleet_ed25519")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-device", default="/dev/ttyUSB0")
    parser.add_argument(
        "--interfaces",
        action="store_true",
        help="also run `iw dev` -- long, and it is what shows a Managed (uplink) VAP",
    )
    args = parser.parse_args(argv)
    if not args.port and not args.ssh_host:
        parser.error("give --port for a cabled DUT or --ssh-host for one on a Pi")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ssh = None
    if args.ssh_host:
        ssh = {
            "host": args.ssh_host,
            "user": args.ssh_user,
            "key_path": str(Path(args.ssh_key).expanduser()),
            "port": args.ssh_port,
            "device": args.ssh_device,
        }
        where = f"{args.ssh_user}@{args.ssh_host}:{args.ssh_device}"
    else:
        where = args.port

    worker = SerialWorker(SysMonParser(on_event=lambda event: None), name="bench-console")
    print(f"opening {where} ...")
    try:
        worker.open(
            args.port or "",
            args.baud,
            mode="ssh" if ssh else "serial",
            session_label="bench-console",
            ssh=ssh,
        )
        time.sleep(2.0 if ssh else 1.5)

        raw_host = worker.capture_command("hostname", timeout=15.0)
        device_id = dut_model.detect_device_id(raw_host)
        model = dut_model.detect_model(raw_host)
        if not raw_host.strip():
            print("\n  the console answered nothing.")
            print("  Most likely it is waiting for a login, which only a person can give.")
            print("  Log in until the prompt appears, then run this again.")
            return 2

        print(f"\n  hostname     : {raw_host.strip()!r}")
        print(f"  device_id    : {device_id}")
        print(f"  model        : {model}")
        print(f"  model says   : {dut_model.cores_for(model)} cores,"
              f" {dut_model.vaps_per_band(model)} VAPs/band,"
              f" bands {list(dut_model.bands_for(model))}")
        # Last non-empty line, not the whole capture: a shell announcing a
        # finished background job ("[1]+ Done  sh /mnt/data/sysMon001.sh") lands
        # in the window ahead of the answer, and printing that as a core count
        # is exactly the kind of confident nonsense this tool exists to avoid.
        cores_raw = worker.capture_command("grep -c ^processor /proc/cpuinfo", timeout=15.0)
        cores = next((ln.strip() for ln in reversed(cores_raw.splitlines()) if ln.strip()), "")
        print(f"  cores (real) : {cores or '(no answer)'}")

        # Three answers, and the third is the one that matters: an empty member
        # list is the device saying it stands alone, an error is "could not
        # tell", and they must not be read as the same thing.
        result = mesh_topology.probe_mesh_over_console(worker, timeout=25.0)
        print(f"\n  mesh         : {result['mesh']}  ({result['detail'] or 'members listed'})")
        for member in result["members"]:
            print(f"    {member['ip']:<16} {str(member['role']):<5}"
                  f" hop={member['hop']}  {member['mac']}  rssi={member['rssi']}")
        if result["mesh"] is None:
            print("    ^ could not tell -- NOT the same as 'no mesh'")
        # `node` and `hop` are relative to whoever was asked; see
        # backend/app/services/mesh_topology.py.
        if result["members"]:
            print("    (hop counts are from THIS device, not from the root)")

        if args.interfaces:
            print("\n--- iw dev ---")
            print(worker.capture_command("iw dev", timeout=25.0))
    except RuntimeError as exc:
        print(f"\nconsole unusable: {exc}", file=sys.stderr)
        return 1
    finally:
        worker.close()
        print("\n[console released]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
