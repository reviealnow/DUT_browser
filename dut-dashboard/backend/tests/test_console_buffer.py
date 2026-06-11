from __future__ import annotations

import unittest

from app.services.console_buffer import ConsoleBuffer


class ConsoleBufferTests(unittest.TestCase):
    def test_observe_line_and_batch(self) -> None:
        buf = ConsoleBuffer(maxlen=100)
        buf.observe({"type": "console_line", "text": "a"})
        buf.observe({"type": "console_line_batch", "lines": ["b", "c"]})
        self.assertEqual(buf.recent(10), ["a", "b", "c"])

    def test_cap_keeps_most_recent(self) -> None:
        buf = ConsoleBuffer(maxlen=3)
        for ch in "abcde":
            buf.observe({"type": "console_line", "text": ch})
        self.assertEqual(buf.recent(10), ["c", "d", "e"])

    def test_recent_limit(self) -> None:
        buf = ConsoleBuffer(maxlen=100)
        buf.observe({"type": "console_line_batch", "lines": ["a", "b", "c", "d"]})
        self.assertEqual(buf.recent(2), ["c", "d"])

    def test_ignores_non_console_and_malformed(self) -> None:
        buf = ConsoleBuffer()
        buf.observe({"type": "snapshot_update", "snapshot": {}})
        buf.observe({"type": "console_line"})  # no text
        buf.observe({"type": "console_line_batch", "lines": [1, "ok", None]})  # filters non-str
        self.assertEqual(buf.recent(10), ["ok"])


if __name__ == "__main__":
    unittest.main()
