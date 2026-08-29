#!/usr/bin/env python3
"""Does a short console command still work while the line is busy? Measure it.

`/api/dut/identify` runs one `hostname` on connect, and connect is exactly when
the console is least quiet. This drives the shipped endpoint against a loaded
line and counts what comes back.

**It proves the load before it believes the result, and that is the whole
point.** The first version of this test reported 8/10 successes against a
console the load had never actually reached -- sysMon rejects bare numbers and
prints its usage line instead, so nothing started, and "under load" was another
idle line. Every attempt returned in 0.0s, exactly like the baseline, which was
the only tell. So this samples console throughput quiet and loaded, and
**exits non-zero without reporting anything** when the loaded window is not
measurably busier. A result measured under no load is not a weaker result, it
is a different experiment wearing its name.

    # sysMon at a 1s step -- note the unit suffixes, bare numbers are refused
    python3 tools/bench_identify_load.py --port /dev/cu.PL2303G-USBtoUART1130 \\
        --expect AP6840E-PD1005VMG3KJH9C

    # a heavier load: the survey backlog is the case sysMon does not cover
    python3 tools/bench_identify_load.py --port /dev/cu.… \\
        --load-command "iw dev ath0 scan" --settle 20

Read-only apart from starting the DUT's own telemetry script, which is what the
product does in normal use. The console is released in a `finally`; sysMon keeps
running on the device until its duration expires.

A diagnostic, not a test: nothing here runs in CI, and the identify path itself
is covered by `backend/tests/test_dut_identify.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import app.config as config  # noqa: E402
import app.dut.registry as registry_mod  # noqa: E402

# Point every persisted path at a throwaway directory BEFORE the registry reads
# them. The real logs/duts.json describes the operator's bench and must not be
# rewritten by a diagnostic.
_TMP = Path(tempfile.mkdtemp(prefix="bench-load-"))
registry_mod.DUTS_FILE = _TMP / "duts.json"
config.LOG_DIR = _TMP
registry_mod.snapshot_file_for = lambda dut_id: _TMP / f"snapshots-{dut_id}.jsonl"

from app import main as main_mod  # noqa: E402
from app.dut.registry import DutRegistry  # noqa: E402

DUT_ID = "loadtest"


class _CountingWs:
    """Counts what actually crosses the wire, which is how the load is proven."""

    def __init__(self) -> None:
        self.lines = 0

    def emit_from_thread(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "console_line":
            self.lines += 1
        elif kind == "console_line_batch":
            self.lines += len(event.get("lines") or [])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True, help="serial device of the DUT under test")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--expect", help="the hostname identify should return; inferred if omitted")
    parser.add_argument(
        "--load-command",
        default="sh /mnt/data/sysMon001.sh 1s 180s &",
        help="what to run on the DUT to busy the console. sysMon's arguments NEED"
        " unit suffixes (1s 180s); bare numbers print a usage line and start nothing",
    )
    parser.add_argument("--settle", type=int, default=10, help="seconds to let the load build")
    parser.add_argument("--sample", type=int, default=10, help="seconds per throughput sample")
    parser.add_argument("--attempts", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ws = _CountingWs()
    registry = DutRegistry(ws_manager=ws, loop=asyncio.new_event_loop())
    main_mod.app.state.dut_registry = registry
    context = registry.create_dut(DUT_ID, label="Load test")
    worker = context.serial_worker

    def traffic(seconds: int, label: str) -> float:
        before = ws.lines
        time.sleep(seconds)
        rate = (ws.lines - before) / seconds
        print(f"  {label:8} {rate:7.1f} console lines/s")
        return rate

    def identify(label: str) -> tuple[str | None, float]:
        started = time.monotonic()
        try:
            got = main_mod.identify_dut(DUT_ID)["device_id"]
        except Exception as exc:  # noqa: BLE001 - failures are the measurement
            got = f"RAISED {type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started
        print(f"  {label:16} {elapsed:5.2f}s -> {got!r}")
        return got, elapsed

    try:
        worker.open(args.port, args.baud, mode="serial", session_label="bench-load")
        time.sleep(2)

        print("1. console traffic BEFORE the load")
        quiet_rate = traffic(args.sample, "quiet")
        if quiet_rate > 1:
            # Usually an earlier run's sysMon still streaming -- it keeps going
            # on the DUT for its whole duration, long after the console was
            # handed back. The comparison below stays valid (a busy baseline
            # only makes the load harder to confirm, which is the safe
            # direction) but "quiet" is not what was measured.
            print("   ^ the baseline is NOT quiet -- something is already streaming.")
            print("     Wait for it to finish if you want a clean comparison.")

        print("\n2. identify on the quiet line")
        expected = args.expect
        first, _ = identify("quiet #1")
        if expected is None:
            expected = first if isinstance(first, str) and not first.startswith("RAISED") else None
            print(f"   (taking {expected!r} as the expected answer)")
        if expected is None:
            print("\n   identify failed on a QUIET line. The load is not the variable;"
                  " fix that first.", file=sys.stderr)
            return 2
        quiet = [first] + [identify(f"quiet #{i + 2}")[0] for i in range(2)]

        print(f"\n3. start the load: {args.load_command}")
        # The newline is not decoration. `SerialWorker.send` is a raw write and
        # adds nothing -- the app's /api/serial/send passes the caller's text
        # through untouched for the same reason -- so without this the command
        # sits on the DUT's input line unexecuted. It then goes out attached to
        # whatever is written next, which is how the throwaway version of this
        # script appeared to work: its very next call was a capture, and that
        # capture's own newline ran both. The load only looked real because
        # something else pressed Enter.
        worker.send(args.load_command + "\n")
        time.sleep(args.settle)

        print("\n4. console traffic DURING the load")
        load_rate = traffic(args.sample, "loaded")
        confirmed = load_rate > quiet_rate * 3 + 1
        print(f"\n   {quiet_rate:.1f} -> {load_rate:.1f} lines/s   "
              f"{'LOAD CONFIRMED' if confirmed else 'NO REAL LOAD'}")
        if not confirmed:
            print(
                "\n   The console is not measurably busier, so nothing below would be a"
                "\n   test of identify under load. Check the load command actually ran"
                "\n   -- sysMon needs unit suffixes and fails quietly without them.",
                file=sys.stderr,
            )
            return 3

        print(f"\n5. identify under proven load, {args.attempts} attempts")
        loaded = [identify(f"under load #{i + 1}")[0] for i in range(args.attempts)]

        ok = sum(1 for r in loaded if r == expected)
        none = sum(1 for r in loaded if r is None)
        print("\n=== result ===")
        print(f"  quiet  : {sum(1 for r in quiet if r == expected)}/{len(quiet)}")
        print(f"  loaded : {ok}/{len(loaded)} identified, {none} learned nothing,"
              f" {len(loaded) - ok - none} other")
        for r in loaded:
            if r not in (None, expected):
                print(f"    {r!r}")
        # A `None` is the honest "the read learned nothing" path, not a crash:
        # the endpoint leaves the stored identity alone when that happens.
        return 0 if ok == len(loaded) else 1
    except RuntimeError as exc:
        print(f"\nconsole unusable: {exc}", file=sys.stderr)
        return 1
    finally:
        worker.close()
        print("\n[console released -- the load keeps running on the DUT until it expires]")


if __name__ == "__main__":
    raise SystemExit(main())
