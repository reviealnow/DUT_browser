from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main

SESSION_NAME = "dut-session-20260616-000000.log"


class LogTailTests(unittest.TestCase):
    def _write(self, directory: str, name: str, text: str) -> None:
        (Path(directory) / name).write_text(text, encoding="utf-8")

    def test_returns_last_n_lines(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write(d, SESSION_NAME, "".join(f"line {i}\n" for i in range(100)))
            with patch.object(main, "LOG_DIR", Path(d)):
                result = main.tail_log(name=SESSION_NAME, lines=10)
        self.assertEqual(result["name"], SESSION_NAME)
        self.assertEqual(len(result["lines"]), 10)
        self.assertEqual(result["lines"][-1], "line 99")
        self.assertEqual(result["lines"][0], "line 90")
        self.assertTrue(result["truncated"])  # 100 lines > 10 requested

    def test_short_file_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write(d, SESSION_NAME, "a\nb\nc\n")
            with patch.object(main, "LOG_DIR", Path(d)):
                result = main.tail_log(name=SESSION_NAME, lines=200)
        self.assertEqual(result["lines"], ["a", "b", "c"])
        self.assertFalse(result["truncated"])

    def test_byte_cap_truncates(self) -> None:
        # A file larger than the read cap is reported truncated even when
        # the requested line count exceeds the lines actually returned.
        with tempfile.TemporaryDirectory() as d:
            big = "".join(f"{i:08d} padding-line-content\n" for i in range(20000))
            self._write(d, SESSION_NAME, big)
            with patch.object(main, "LOG_DIR", Path(d)):
                result = main.tail_log(name=SESSION_NAME, lines=2000)
        self.assertTrue(result["truncated"])
        # Last line of the file must survive the tail read.
        self.assertEqual(result["lines"][-1], "00019999 padding-line-content")

    def test_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(main, "LOG_DIR", Path(d)):
                with self.assertRaises(HTTPException) as ctx:
                    main.tail_log(name="../etc/passwd", lines=10)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_session_name_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "duts.json", "{}\n")
            with patch.object(main, "LOG_DIR", Path(d)):
                with self.assertRaises(HTTPException) as ctx:
                    main.tail_log(name="duts.json", lines=10)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_file_404(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(main, "LOG_DIR", Path(d)):
                with self.assertRaises(HTTPException) as ctx:
                    main.tail_log(name="dut-session-nope.log", lines=10)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
