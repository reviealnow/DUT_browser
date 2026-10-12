"""Regression tests for the CPU line regex in tools/analyzer3.py.

analyzer3 is a script that does all of its work at import time, so these
tests run it as a subprocess in a scratch directory holding one synthetic log,
then read the cpu_usage.csv it writes. That covers the real path: the
line.strip() before matching, the record flush, and the CSV columns.

A CPU line that no longer matches is not an error in analyzer3 -- it exits 0,
prints "Done" and simply leaves the CPU columns out -- so every case asserts
the column is present and carries the right value.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ANALYZER = Path(__file__).resolve().parents[2] / "tools" / "analyzer3.py"

TS_LINE = "= Test Time: 1, 2026-09-04 00:59:51                    ="

# Byte-for-byte the shape busybox top prints on the DUT: padded columns, a
# doubled "%%" before sirq and a CRLF ending (every CPU line in the captured
# session logs has this form).
DUT_CPU0 = "CPU0:   0.0% usr   1.0% sys   0.0% nic  73.7% idle   0.0% io   2.0% irq  23.2%% sirq"
DUT_CPU1 = "CPU1:   4.0% usr   5.0% sys   0.0% nic  85.0% idle   0.0% io   1.0% irq   5.0%% sirq"


def _run_analyzer(log_text: str) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory() as d:
        Path(d, "dut-session-test.log").write_bytes(log_text.encode("utf-8"))
        env = {**os.environ, "MPLBACKEND": "Agg"}
        proc = subprocess.run(
            [sys.executable, str(ANALYZER)],
            cwd=d,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"analyzer3 exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
            )
        (csv_path,) = Path(d).glob("*cpu_usage.csv")
        with csv_path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def _log(*cpu_lines: str, newline: str = "\r\n") -> str:
    return newline.join([TS_LINE, *cpu_lines, ""])


class Analyzer3CpuPatternTests(unittest.TestCase):
    def test_dut_format_with_double_percent(self) -> None:
        (row,) = _run_analyzer(_log(DUT_CPU0, DUT_CPU1))
        # usage = usr + sys + irq + sirq
        self.assertEqual(row.get("CPU0_UsagePct"), "26.20")
        self.assertEqual(row.get("CPU1_UsagePct"), "15.00")

    def test_single_percent_before_sirq(self) -> None:
        line = DUT_CPU0.replace("%% sirq", "% sirq")
        (row,) = _run_analyzer(_log(line))
        self.assertEqual(row.get("CPU0_UsagePct"), "26.20")

    def test_no_space_between_percent_and_label(self) -> None:
        line = "CPU0: 0.0%usr 1.0%sys 0.0%nic 73.7%idle 0.0%io 2.0%irq 23.2%%sirq"
        (row,) = _run_analyzer(_log(line))
        self.assertEqual(row.get("CPU0_UsagePct"), "26.20")

    def test_lf_line_endings(self) -> None:
        (row,) = _run_analyzer(_log(DUT_CPU0, newline="\n"))
        self.assertEqual(row.get("CPU0_UsagePct"), "26.20")


if __name__ == "__main__":
    unittest.main()
