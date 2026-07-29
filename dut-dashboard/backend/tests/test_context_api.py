"""Endpoint-level tests for connect-time context: the capture endpoint's
failure isolation, the download traversal guard, /api/logs' new fields, and the
guarantee that an Analyze does not destroy the context of a previous one.

Handlers are called directly (the role gates have their own coverage in
test_route_protection); the kind → directory map is patched onto a tempdir.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app import main
from app.services import context_snapshot

_SSIDS = [
    {"iface": "ath0", "ssid": "MyAP", "bssid": "11:22:33:44:55:66", "band": "2.4GHz",
     "freq_mhz": 2462, "channel": 11, "channel_width": "20 MHz", "generation": "Wi-Fi 6",
     "security": "WPA2-Personal", "category": "personal", "pmf": "optional",
     "akm": ["WPA-PSK"], "pairwise_cipher": ["CCMP"], "group_mgmt_cipher": None,
     "dot11k": True, "dot11v": None, "dot11r": None},
]


def _stamp(path: Path, ts: str) -> Path:
    """Force a file's mtime, which is the end of its session window."""
    epoch = datetime.strptime(ts, "%Y%m%d-%H%M%S").timestamp()
    os.utime(path, (epoch, epoch))
    return path


class _StubWorker:
    """Serial worker stub whose capture_command is scripted per command."""

    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        self.calls.append(cmd)
        for key, value in self.responses.items():
            if cmd.startswith(key):
                if isinstance(value, Exception):
                    raise value
                return value
        return ""


class _StubContextObj:
    def __init__(self, worker) -> None:
        self.serial_worker = worker


class ContextCaptureEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        p = mock.patch.object(
            context_snapshot,
            "_KIND_DIRS",
            {kind: self.tmp / kind for kind in context_snapshot.KINDS},
        )
        p.start()
        self.addCleanup(p.stop)

    def _capture(self, worker, capability=None) -> dict:
        """Call the endpoint with a stub worker. get_ssid_capabilities is stubbed
        because it asserts a real SerialWorker; its own parsing has coverage in
        test_ssid_capability."""
        cap = capability if capability is not None else (lambda _worker: _SSIDS)
        with mock.patch.object(main, "resolve_dut", return_value=_StubContextObj(worker)), mock.patch.object(
            main, "get_ssid_capabilities", side_effect=cap
        ):
            return main.capture_dut_context(dut="lab2")

    def test_both_kinds_are_captured_and_written(self) -> None:
        worker = _StubWorker({"iwconfig": "", "iw dev": ""})
        result = self._capture(worker)
        self.assertEqual({c["kind"] for c in result["captures"]}, set(context_snapshot.KINDS) - {"site-survey"})
        self.assertTrue(all(c["ok"] for c in result["captures"]))
        entries = context_snapshot.snapshot_entries()
        self.assertEqual({e["kind"] for e in entries}, {"wifi-clients", "ssid-capability"})
        self.assertEqual({e["dut"] for e in entries}, {"lab2"})

    def test_a_failing_kind_neither_raises_nor_blocks_the_other(self) -> None:
        worker = _StubWorker({"iwconfig": RuntimeError("Serial port is not open"), "iw dev": ""})
        result = self._capture(worker)
        clients = next(c for c in result["captures"] if c["kind"] == "wifi-clients")
        capability = next(c for c in result["captures"] if c["kind"] == "ssid-capability")
        self.assertFalse(clients["ok"])
        self.assertIn("Serial port is not open", clients["error"])
        self.assertTrue(capability["ok"])

    def test_no_serial_at_all_still_returns_200_shaped_result(self) -> None:
        boom = RuntimeError("Serial port is not open")
        worker = _StubWorker({"iwconfig": boom})

        def refuse(_worker):
            raise boom

        # Must not raise — a failed capture never fails a connect.
        result = self._capture(worker, capability=refuse)
        self.assertFalse(any(c["ok"] for c in result["captures"]))
        self.assertEqual(context_snapshot.snapshot_entries(), [])

    def test_a_write_failure_is_reported_not_raised(self) -> None:
        worker = _StubWorker({"iwconfig": "", "iw dev": ""})
        with mock.patch.object(context_snapshot, "write_clients", side_effect=OSError("read-only fs")):
            result = self._capture(worker)
        clients = next(c for c in result["captures"] if c["kind"] == "wifi-clients")
        self.assertFalse(clients["ok"])
        self.assertIn("read-only fs", clients["error"])


class ContextDownloadEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.dirs = {kind: self.tmp / kind for kind in context_snapshot.KINDS}
        for path in self.dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        p = mock.patch.object(context_snapshot, "_KIND_DIRS", self.dirs)
        p.start()
        self.addCleanup(p.stop)

    def test_serves_an_existing_capture(self) -> None:
        name = "wifi-clients-lab2-20260729-100000.json"
        (self.dirs["wifi-clients"] / name).write_text("{}", encoding="utf-8")
        resp = main.download_context("wifi-clients", name)
        self.assertEqual(Path(resp.path).name, name)

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_context("../../etc", "passwd.json")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_traversal_rejected(self) -> None:
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_context("wifi-clients", "../../etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_allowed_extension_rejected(self) -> None:
        name = "wifi-clients-lab2-20260729-100000.log"
        (self.dirs["wifi-clients"] / name).write_text("x", encoding="utf-8")
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_context("wifi-clients", name)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_file_is_404(self) -> None:
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_context("wifi-clients", "wifi-clients-lab2-20260729-100000.json")
        self.assertEqual(ctx.exception.status_code, 404)


class ListLogsContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.dirs = {kind: self.tmp / kind for kind in context_snapshot.KINDS}
        for patcher in (
            mock.patch.object(main, "LOG_DIR", self.logs),
            mock.patch.object(main, "ANALYZER_OUTPUT_DIR", self.tmp / "analyzer_output"),
            mock.patch.object(context_snapshot, "_KIND_DIRS", self.dirs),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _place(self, kind: str, dut: str, ts: str) -> None:
        self.dirs[kind].mkdir(parents=True, exist_ok=True)
        (self.dirs[kind] / f"{kind}-{dut}-{ts}.json").write_text("{}", encoding="utf-8")
        (self.dirs[kind] / f"{kind}-{dut}-{ts}.csv").write_text("a\n", encoding="utf-8")

    def test_session_rows_carry_their_in_window_context_count(self) -> None:
        (self.logs / "dut-session-20260729-100000.log").write_text("x", encoding="utf-8")
        self._place("wifi-clients", "lab2", "20260729-100005")
        result = main.list_logs()
        session = next(s for s in result["sessions"] if s["name"].startswith("dut-session-"))
        self.assertEqual(session["context_count"], 2)  # json + csv

    def test_a_log_predating_the_feature_reports_zero(self) -> None:
        old = self.logs / "dut-session-20200101-000000.log"
        old.write_text("x", encoding="utf-8")
        _stamp(old, "20200101-010000")
        self._place("wifi-clients", "lab2", "20260729-100005")
        session = main.list_logs()["sessions"][0]
        self.assertEqual(session["context_count"], 0)

    def test_context_list_excludes_site_surveys(self) -> None:
        self._place("wifi-clients", "lab2", "20260729-100005")
        self._place("site-survey", "lab2", "20260729-100006")
        result = main.list_logs()
        self.assertEqual({item["kind"] for item in result["context"]}, {"wifi-clients"})
        # Surveys keep their own P68 list.
        self.assertIn("surveys", result)


class AnalyzerContextBundleTests(unittest.TestCase):
    """The context bundle must live outside analyzer_output/, which every run
    clears — otherwise a second Analyze would delete the first one's context."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.dirs = {kind: self.tmp / kind for kind in context_snapshot.KINDS}
        p = mock.patch.object(context_snapshot, "_KIND_DIRS", self.dirs)
        p.start()
        self.addCleanup(p.stop)

        from app.services import analyzer_service

        self.analyzer_service = analyzer_service
        self.bundles = self.tmp / "context-bundles"
        q = mock.patch.object(analyzer_service, "CONTEXT_BUNDLE_DIR", self.bundles)
        q.start()
        self.addCleanup(q.stop)

        self.log = self.tmp / "dut-session-20260729-100000.log"
        self.log.write_text("x", encoding="utf-8")
        _stamp(self.log, "20260729-110000")
        self.dirs["wifi-clients"].mkdir(parents=True, exist_ok=True)
        for ext in ("json", "csv"):
            (self.dirs["wifi-clients"] / f"wifi-clients-lab2-20260729-100500.{ext}").write_text(
                "{}", encoding="utf-8"
            )

    def test_bundle_lands_under_its_own_log_directory(self) -> None:
        result = self.analyzer_service._bundle_context(self.log)
        self.assertEqual(len(result["files"]), 2)
        self.assertTrue((self.bundles / self.log.stem / "context" / "wifi-clients").is_dir())

    def test_clearing_analyzer_outputs_does_not_touch_the_bundle(self) -> None:
        self.analyzer_service._bundle_context(self.log)
        outputs = self.tmp / "analyzer_output"
        outputs.mkdir()
        (outputs / "cpu_usage.csv").write_text("x", encoding="utf-8")
        self.analyzer_service._clear_analyzer_outputs(outputs)
        self.assertFalse((outputs / "cpu_usage.csv").exists())
        survivors = list((self.bundles / self.log.stem / "context" / "wifi-clients").iterdir())
        self.assertEqual(len(survivors), 2)

    def test_a_session_with_no_context_reports_nothing(self) -> None:
        old = self.tmp / "dut-session-20200101-000000.log"
        old.write_text("x", encoding="utf-8")
        _stamp(old, "20200101-010000")
        result = self.analyzer_service._bundle_context(old)
        self.assertEqual(result, {"dir": None, "files": []})

    def test_a_degenerate_log_stem_is_refused(self) -> None:
        self.assertEqual(self.analyzer_service._bundle_context(Path("..")), {"dir": None, "files": []})


if __name__ == "__main__":
    unittest.main()
