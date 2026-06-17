from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api import analyzer_api
from app.api.analyzer_api import AnalyzerRunSessionRequest, run_analyzer_for_session_log
from app.services.analyzer_service import _concise_error


class FakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, log_path: str) -> dict:
        self.calls.append(log_path)
        return {"ok": True, "log_path": log_path, "files": ["cpu_usage.csv", "memory.csv"]}


def _request(service: FakeAnalyzer) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(analyzer_service=service)))


class RunSessionEndpointTests(unittest.TestCase):
    def _call(self, name: str, service: FakeAnalyzer):
        return run_analyzer_for_session_log(AnalyzerRunSessionRequest(name=name), _request(service))

    def test_rejects_traversal_and_non_session_names(self) -> None:
        service = FakeAnalyzer()
        for bad in ["../etc/passwd", "dut-session-../x.log", "notes.txt", "snapshots.jsonl"]:
            with self.assertRaises(HTTPException) as ctx:
                self._call(bad, service)
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(service.calls, [])  # never reached the analyzer

    def test_missing_session_log_is_404(self) -> None:
        service = FakeAnalyzer()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(analyzer_api, "LOG_DIR", Path(d)):
                with self.assertRaises(HTTPException) as ctx:
                    self._call("dut-session-20260101-000000.log", service)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(service.calls, [])

    def test_valid_name_runs_analyzer_on_resolved_path(self) -> None:
        service = FakeAnalyzer()
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "dut-session-20260609-114447.log"
            log.write_text("= Test Time: 1, 2026-06-09 03:45:34 =\n", encoding="utf-8")
            with patch.object(analyzer_api, "LOG_DIR", Path(d)):
                result = self._call("dut-session-20260609-114447.log", service)
        self.assertTrue(result["ok"])
        self.assertIn("memory.csv", result["files"])
        self.assertEqual(service.calls, [str(log)])  # resolved under LOG_DIR


class ConciseErrorTests(unittest.TestCase):
    def test_extracts_error_lines_from_noisy_stdout(self) -> None:
        stdout = (
            "[INFO] detected 1 log\n"
            "[INFO] Output Prefix = run_\n"
            "[ERROR] no snapshots parsed (records=0)\n"
        )
        self.assertEqual(_concise_error("", stdout), "[ERROR] no snapshots parsed (records=0)")

    def test_falls_back_when_no_error_marker(self) -> None:
        self.assertEqual(_concise_error("boom", ""), "boom")
        self.assertEqual(_concise_error("", ""), "analyzer3.py failed")


if __name__ == "__main__":
    unittest.main()
