from __future__ import annotations

import unittest

from app.parser.sysmon_parser import SysMonParser

# A real CPU line from the AP6 840E sysmon dump.
_CPU0 = "CPU0:   0.0% usr   4.9% sys   0.0% nic  86.4% idle   0.0% io   1.0% irq   7.8%% sirq"


def _merged_memory(events: list[dict]) -> dict:
    """Accumulate memory across snapshot_update/snapshot_delta in stream order
    (last write wins), mirroring how the store/frontend reconstruct it."""
    mem: dict = {}
    for event in events:
        if event["type"] == "snapshot_update":
            mem.update(event["snapshot"].get("memory") or {})
        elif event["type"] == "snapshot_delta":
            mem.update(event["delta"].get("memory") or {})
    return mem


def _console_lines(events: list[dict]) -> list[str]:
    out: list[str] = []
    for event in events:
        if event["type"] == "console_line_batch":
            out.extend(event["lines"])
        elif event["type"] == "console_line":
            out.append(event["text"])
    return out


class SysMonParserMemoryTests(unittest.TestCase):
    def _parse(self, lines: list[str]) -> list[dict]:
        events: list[dict] = []
        parser = SysMonParser(events.append)
        for line in lines:
            parser.feed(line)
        parser.flush()
        return events

    def test_meminfo_keys_recorded_into_snapshot(self) -> None:
        events = self._parse(
            [
                "= Test Time: 1, 2026-06-09 03:45:34 =",
                _CPU0,
                "MemTotal:         843132 kB",
                "MemFree:          391540 kB",
                "MemAvailable:     475472 kB",
                "Slab:             164708 kB",
                "SUnreclaim:       158908 kB",
            ]
        )
        memory = _merged_memory(events)
        self.assertEqual(memory["MemTotal"], 843132)
        self.assertEqual(memory["MemAvailable"], 475472)
        self.assertEqual(memory["SUnreclaim"], 158908)
        self.assertEqual(memory["Slab"], 164708)

    def test_unwanted_keys_excluded_but_kept_in_console(self) -> None:
        events = self._parse(
            [
                "= Test Time: 1, 2026-06-09 03:45:34 =",
                _CPU0,
                "MemAvailable:     475472 kB",
                "Mlocked:               0 kB",
            ]
        )
        memory = _merged_memory(events)
        self.assertIn("MemAvailable", memory)
        self.assertNotIn("Mlocked", memory)  # not in MEM_KEYS
        # The raw meminfo lines are preserved in the console stream unchanged.
        console = _console_lines(events)
        self.assertIn("MemAvailable:     475472 kB", console)
        self.assertIn("Mlocked:               0 kB", console)

    def test_memory_change_propagates_across_blocks(self) -> None:
        events = self._parse(
            [
                "= Test Time: 1, 2026-06-09 03:45:34 =",
                _CPU0,
                "MemAvailable:     475472 kB",
                "= Test Time: 2, 2026-06-09 03:46:48 =",
                _CPU0,
                "MemAvailable:     470000 kB",
            ]
        )
        # Last write wins: the merged value reflects block 2.
        self.assertEqual(_merged_memory(events)["MemAvailable"], 470000)


if __name__ == "__main__":
    unittest.main()
