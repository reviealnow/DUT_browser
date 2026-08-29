#!/usr/bin/env python3
"""Drive a whole mesh through the shipped endpoints, over real consoles.

The Fleet page's claims are assembled from three endpoints, and until this ran
against hardware the interesting one had only ever been exercised with a stub:
**a root cannot name its own backhaul VAP from its own console.** It is named
from another node's uplink, so `capture_rssi` has to see the node first. That
ordering is the reason this script exists rather than a loop.

It calls the product's own endpoint functions -- `main.identify_dut`,
`main.probe_dut_mesh`, `fleet_api.capture_rssi` -- not a reimplementation of
them, so what it exercises is the shipped path. The only thing bypassed is
FastAPI's role gating, which `backend/tests/test_route_protection.py` covers and
which would otherwise mean a passcode and a session for a read-only run.

Describe the bench in a small JSON file and pass it:

    [
      {"id": "node1", "label": "Mesh Node (Pi)", "mgmt_url": "https://192.168.30.176",
       "ssh": {"host": "192.168.30.124", "user": "nelson",
               "key_path": "~/.ssh/dut_fleet_ed25519", "device": "/dev/ttyUSB0"}},
      {"id": "root1", "label": "Mesh Root (desk)", "mgmt_url": "https://192.168.30.121",
       "port": "/dev/cu.PL2303G-USBtoUART140"}
    ]

    python3 tools/bench_fleet_e2e.py --bench bench.json

**List nodes before roots.** The file's order is the capture order, and a root
captured first falls back to whatever interface is configured -- which on this
bench once pointed at a client VAP and drew an ordinary laptop as a mesh child.

Every console is released in a `finally`, and nothing is written outside a
temporary directory: the real `logs/duts.json` describes the operator's bench.

A diagnostic, not a test. It needs the hardware, so nothing here runs in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import app.config as config  # noqa: E402
import app.dut.registry as registry_mod  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="bench-fleet-"))
registry_mod.DUTS_FILE = _TMP / "duts.json"
config.LOG_DIR = _TMP
registry_mod.snapshot_file_for = lambda dut_id: _TMP / f"snapshots-{dut_id}.jsonl"

from app import main as main_mod  # noqa: E402
from app.api import fleet_api  # noqa: E402
from app.dut.registry import DutRegistry  # noqa: E402


class _Ws:
    def emit_from_thread(self, event: dict) -> None:  # noqa: D102
        pass


class _Request:
    """Only what these endpoints touch: `request.app.state.dut_registry`."""

    def __init__(self, app) -> None:
        self.app = app


def _remote(ssh: dict) -> dict:
    """An SSH console in the shape the registry validates."""
    return {
        "host": ssh["host"],
        "user": ssh.get("user", "nelson"),
        "key_path": str(Path(ssh["key_path"]).expanduser()),
        "port": int(ssh.get("port", 22)),
        "device": ssh["device"],
        "baudrate": int(ssh.get("baudrate", 115200)),
        "is_mesh": bool(ssh.get("is_mesh", True)),
        "backhaul_iface": ssh.get("backhaul_iface", "ath7"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bench", required=True, type=Path, help="JSON describing the DUTs")
    parser.add_argument("--skip-rssi", action="store_true", help="identify and probe only")
    args = parser.parse_args(argv)

    duts = json.loads(args.bench.read_text())
    registry = DutRegistry(ws_manager=_Ws(), loop=asyncio.new_event_loop())
    main_mod.app.state.dut_registry = registry
    request = _Request(main_mod.app)

    opened = []
    try:
        for spec in duts:
            context = registry.create_dut(spec["id"], label=spec.get("label"))
            context.mgmt_url = spec.get("mgmt_url", "")
            ssh = _remote(spec["ssh"]) if spec.get("ssh") else None
            if ssh:
                context.remote = ssh
            context.serial_worker.open(
                spec.get("port", ""),
                115200,
                mode="ssh" if ssh else "serial",
                session_label=f"e2e-{spec['id']}",
                ssh=ssh,
            )
            if spec.get("port"):
                registry.record_serial_params(spec["id"], spec["port"], 115200)
            opened.append(context)
            print(f"opened {spec['id']} ({context.serial_worker.mode})")

        print("\n=== POST /api/dut/identify ===")
        for spec in duts:
            print(f"  {spec['id']}: {main_mod.identify_dut(spec['id'])}")

        print("\n=== POST /api/wifi/mesh-probe ===")
        for spec in duts:
            out = main_mod.probe_dut_mesh(spec["id"])
            print(f"  {spec['id']}: mesh={out['mesh']} members={len(out['members'])}"
                  f" {out['detail']!r}")

        if not args.skip_rssi:
            print("\n=== POST /api/fleet/nodes/<id>/rssi — in file order, nodes first ===")
            for spec in duts:
                try:
                    out = fleet_api.capture_rssi(spec["id"], request)
                except Exception as exc:  # noqa: BLE001 - reporting, not deciding
                    print(f"  {spec['id']}: FAILED {type(exc).__name__}: {exc}")
                    continue
                print(f"  {spec['id']}: role={out['role']!r} captured={out['captured']}")
                print(f"      uplink   = {out['uplink']}")
                down = out["downlink"]
                if down:
                    # `source` is the finding: "detected" means a peer's uplink
                    # named this VAP, "configured" means an admin typed it.
                    print(f"      downlink = {down['iface']} source={down['source']}"
                          f" essid={down.get('essid')!r}")
                    for peer in down["peers"]:
                        print(f"        peer: {peer}")
                else:
                    print("      downlink = None")

        print("\n=== what /api/duts publishes ===")
        for info in registry.describe():
            print(f"  {info['id']}: model={info['model']!r} device_id={info['device_id']!r}"
                  f" cores={info['model_cores']} bands={info['bands']}"
                  f" per_band={info['vaps_per_band']}")
    except RuntimeError as exc:
        print(f"\nconsole unusable: {exc}", file=sys.stderr)
        return 1
    finally:
        for context in opened:
            context.serial_worker.close()
        print("\n[all consoles released]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
