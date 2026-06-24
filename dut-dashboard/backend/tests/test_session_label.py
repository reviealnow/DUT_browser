from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.parser.sysmon_parser import SysMonParser
from app.serial import serial_worker
from app.serial.serial_worker import SerialWorker, sanitize_session_label


class SanitizeSessionLabelTests(unittest.TestCase):
    def test_keeps_safe_label(self) -> None:
        self.assertEqual(sanitize_session_label("AP6420E"), "AP6420E")
        self.assertEqual(sanitize_session_label("ap-6_420.E"), "ap-6_420.E")

    def test_strips_path_separators_and_junk(self) -> None:
        # No traversal can survive: slashes and spaces are removed.
        self.assertEqual(sanitize_session_label("../../etc/passwd"), "....etcpasswd")
        self.assertEqual(sanitize_session_label("my dut!@# 6420"), "mydut6420")

    def test_empty_and_none(self) -> None:
        self.assertEqual(sanitize_session_label(""), "")
        self.assertEqual(sanitize_session_label(None), "")
        self.assertEqual(sanitize_session_label("   "), "")  # spaces removed → empty

    def test_length_capped(self) -> None:
        self.assertEqual(len(sanitize_session_label("A" * 100)), 40)


class SessionLogFilenameTests(unittest.TestCase):
    """The (sanitized) label names the file; the dut-session- prefix is preserved."""

    def _worker(self, name: str = "") -> SerialWorker:
        return SerialWorker(SysMonParser(lambda event: None), name=name)

    def _open_and_name(self, label: str, name: str = "") -> str:
        worker = self._worker(name=name)
        with tempfile.TemporaryDirectory() as tmp:
            original = serial_worker.LOG_DIR
            serial_worker.LOG_DIR = Path(tmp)
            try:
                worker._start_log_session(mode="serial", port="x", replay_path=None, label=label)
                path = Path(worker.current_log_path)
            finally:
                worker._close_log_session()
                serial_worker.LOG_DIR = original
        return path.name

    def test_label_becomes_filename(self) -> None:
        fname = self._open_and_name(sanitize_session_label("AP6420E"))
        self.assertTrue(fname.startswith("dut-session-AP6420E-"))
        self.assertTrue(fname.endswith(".log"))

    def test_empty_label_falls_back_to_dut_name(self) -> None:
        fname = self._open_and_name(sanitize_session_label(""), name="lab2")
        self.assertTrue(fname.startswith("dut-session-lab2-"))

    def test_empty_label_and_default_dut_keeps_original_naming(self) -> None:
        fname = self._open_and_name(sanitize_session_label(""), name="")
        # default DUT, no label → dut-session-<ts>.log (no extra prefix token)
        self.assertTrue(fname.startswith("dut-session-"))
        self.assertFalse(fname.startswith("dut-session--"))


if __name__ == "__main__":
    unittest.main()
