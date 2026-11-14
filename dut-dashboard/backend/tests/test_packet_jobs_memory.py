"""Packet Monitor analyses: the backend's memory does not grow with the capture.

The analysis runs in the backend process, beside the serial worker, and the
TSV tshark prints may be up to the runner's stdout cap. The job therefore reads
it one row at a time (review of #230, point 6). This runs a large synthetic
capture through the same stand-in tshark as ``test_packet_jobs.py`` and holds
the job's peak Python allocation under a bound that reading the whole TSV,
or keeping every row, exceeds many times over.
"""

from __future__ import annotations

import os
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_packet_jobs import TSV, _JobsBench  # noqa: E402

ROWS = 200_000
BSSES = 64
#: Measured for 200k rows: 17 MiB streaming (findings' frame lists and the
#: per-BSS frame numbers still grow with the capture, by a few ints per row),
#: 407 MiB reading the whole TSV and keeping every row.
PEAK_LIMIT_BYTES = 64 * 1024 * 1024


def write_large_tsv(path: Path, rows: int = ROWS) -> None:
    """The real tshark 4.4.1 fixture's frames, repeated with fresh frame numbers and BSSIDs."""
    lines = [line for line in TSV.read_text(encoding="utf-8").split("\n") if line]
    header = lines[0].split("\t")
    frame_col = header.index("frame.number")
    bssid_col = header.index("wlan.bssid")
    body = [line.split("\t") for line in lines[1:]]
    with open(path, "w", encoding="utf-8", newline="") as out:
        out.write(lines[0] + "\n")
        for index in range(rows):
            cells = list(body[index % len(body)])
            cells[frame_col] = str(index + 1)
            # Fixture frame i, copy k becomes a BSS of its own, so the group
            # rules see BSSES * len(body) groups and each one's history.
            copy = (index // len(body)) % BSSES
            cells[bssid_col] = f"02:00:00:{copy:02x}:{(index % len(body)):02x}:00"
            out.write("\t".join(cells) + "\n")


class AnalysisMemoryTest(_JobsBench):
    def test_a_large_capture_is_analysed_in_bounded_memory(self) -> None:
        tsv = self.root / "large.tsv"
        write_large_tsv(tsv)
        os.environ["FAKE_TSHARK_TSV"] = str(tsv)
        file_id = self.add_file()

        tracemalloc.start()
        try:
            analysis_id = self.jobs.create(
                file_id=file_id, collector_id="pi", mode_id="beacon_security", user_id=7,
            )
            self.assertTrue(self.jobs.wait_idle(600), "the analysis never finished")
            _, peak = tracemalloc.get_traced_memory()
            analysis = self.jobs.get(analysis_id)
        finally:
            tracemalloc.stop()

        self.assertEqual(analysis["status"], "done", analysis["error"])
        self.assertEqual(analysis["frames_shown"], ROWS)
        self.assertEqual(analysis["rows_dropped"], 0)
        self.assertGreater(sum(analysis["findings_summary"].values()), 0)
        self.assertLess(
            peak, PEAK_LIMIT_BYTES,
            f"peak {peak / 2**20:.0f} MiB for {ROWS} rows: the analysis holds the capture in memory",
        )
