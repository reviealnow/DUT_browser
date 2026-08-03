"""Track C2: on-demand Wi-Fi scans are persisted (write-through), and a failing
best-effort offline tool says so in the bundle's capture report.

The load-bearing claim of C2a is a *negative* one — that persisting is invisible
to the caller — so every endpoint test here compares the live response against a
**baseline response captured with `_persist_scan` patched to a no-op**, which is
exactly what these handlers did before C2. Asserting a hand-written literal
would only prove the literal was copied correctly; this proves the endpoint did
not move.

Handlers are called directly (role gates have their own coverage in
test_route_protection) with the kind → directory map patched onto a tempdir,
following test_context_api.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from app import main
from app.api import serial_api
from app.services import context_snapshot, survey_cache, survey_snapshot

_SSIDS = [
    {"iface": "ath0", "ssid": "MyAP", "bssid": "11:22:33:44:55:66", "band": "2.4GHz",
     "freq_mhz": 2462, "channel": 11, "channel_width": "20 MHz", "generation": "Wi-Fi 6",
     "security": "WPA2-Personal", "category": "personal", "pmf": "optional",
     "akm": ["WPA-PSK"], "pairwise_cipher": ["CCMP"], "group_mgmt_cipher": None,
     "dot11k": True, "dot11v": None, "dot11r": None},
]

_NEIGHBORS = [
    {"band": "2.4GHz", "channel": 11, "ssid": "HomeNet", "bssid": "aa:bb:cc:dd:ee:ff",
     "signal_dbm": -55.0, "security": "WPA2-Personal", "iface": "ath0"},
]
_SURVEY_VAPS = [{"iface": "ath0", "ssid": "MyAP", "band": "2.4GHz", "channel": 11}]

_IWCONFIG = """ath0      IEEE 802.11axg  ESSID:"LabAP"
          Mode:Master  Frequency:2.437 GHz (Channel 6)  Access Point: C8:4F:86:95:CE:E4
          Bit Rate:573.5 Mb/s   Tx-Power:29 dBm
"""
_WLANCONFIG = """wlanconfig ath0 list
ADDR               AID CHAN TXRATE RXRATE RSSI  ANT_RSSI MINRSSI MAXRSSI IDLE  TXSEQ  RXSEQ  CAPS XCAPS ACAPS     ERP    STATE MAXRATE(DOT11) HTCAPS   VHTCAPS ASSOCTIME    IEs   MODE RXNSS TXNSS                   PSMODE
aa:bb:cc:dd:ee:ff    1    6 573M   573M   -48  40/40/40     -61     -54    0      0   65535   EPR   EBO NULL    0          3        1201000               Q              00 00:01:40     RSN WME IEEE80211_MODE_11AXG_HE20  2 2   0
"""


class _StubWorker:
    """Serial worker stub whose capture_command is scripted per command."""

    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        for key, value in self.responses.items():
            if cmd.startswith(key):
                if isinstance(value, Exception):
                    raise value
                return value
        return ""


class _StubContextObj:
    def __init__(self, worker) -> None:
        self.serial_worker = worker


def _without_timestamps(response: dict) -> dict:
    """A response minus its clock-derived fields, for baseline comparison.

    ``captured_at`` is wall-clock and so differs between two runs of the same
    endpoint for reasons that have nothing to do with C2; the key's *presence*
    is asserted separately, so dropping the value here cannot hide a dropped
    field.
    """
    return {k: v for k, v in response.items() if not k.startswith("captured_at")}


class _WriteThroughCase(unittest.TestCase):
    """Shared fixture: snapshot dirs on a tempdir, plus the baseline harness."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.dirs = {kind: self.tmp / kind for kind in context_snapshot.KINDS}
        for patcher in (
            mock.patch.object(context_snapshot, "_KIND_DIRS", self.dirs),
            # Site surveys are written through survey_snapshot, which reaches
            # for its own module-level directory constant.
            mock.patch.object(survey_snapshot, "SURVEY_SNAPSHOT_DIR", self.dirs["site-survey"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        survey_cache.clear()

    def files_written(self) -> list[str]:
        """Every snapshot file now on disk, by name."""
        return sorted(p.name for d in self.dirs.values() if d.is_dir() for p in d.iterdir())

    def assert_matches_baseline(self, live: dict, baseline: dict) -> None:
        """The C2 claim: persisting changed neither the keys nor the values."""
        self.assertEqual(set(live), set(baseline))
        self.assertEqual(_without_timestamps(live), _without_timestamps(baseline))


class WifiClientsWriteThroughTests(_WriteThroughCase):
    def _call(self, worker, persist: bool = True) -> dict:
        with mock.patch.object(main, "resolve_dut", return_value=_StubContextObj(worker)):
            if persist:
                return main.get_wifi_clients(dut="lab2")
            with mock.patch.object(main, "_persist_scan"):
                return main.get_wifi_clients(dut="lab2")

    def test_a_successful_scan_is_persisted(self) -> None:
        worker = _StubWorker({"iwconfig": _IWCONFIG, "wlanconfig": _WLANCONFIG})
        response = self._call(worker)

        entries = context_snapshot.snapshot_entries()
        self.assertEqual({e["kind"] for e in entries}, {"wifi-clients"})
        self.assertEqual({e["dut"] for e in entries}, {"lab2"})
        self.assertEqual({e["ext"] for e in entries}, {"json", "csv"})
        # The snapshot holds the scan, not an empty container.
        payload = json.loads(
            next(e["path"] for e in entries if e["ext"] == "json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["clients"], response["clients"])
        self.assertEqual(payload["captured_at"], response["captured_at"])
        self.assertEqual(payload["kind"], "wifi-clients")

    def test_the_response_is_unchanged_by_persisting(self) -> None:
        worker = _StubWorker({"iwconfig": _IWCONFIG, "wlanconfig": _WLANCONFIG})
        baseline = self._call(worker, persist=False)
        self.assertEqual(self.files_written(), [])  # baseline really did not write
        live = self._call(worker)
        self.assert_matches_baseline(live, baseline)
        self.assertTrue(live["clients"] and live["vaps"], "fixture must be non-empty to prove anything")
        datetime.fromisoformat(live["captured_at"])

    def test_an_empty_scan_writes_nothing_and_no_skip_marker(self) -> None:
        """No files is the whole point of C1; C2 must not reintroduce empties.

        And no ``.skip.json`` either: a marker claims the connect-time capture
        was attempted, which a read-only GET has no standing to assert.
        """
        response = self._call(_StubWorker({"iwconfig": ""}))
        self.assertEqual(response["clients"], [])
        self.assertEqual(self.files_written(), [])
        self.assertEqual(context_snapshot.snapshot_entries(), [])
        self.assertEqual(context_snapshot.skip_entries(), [])

    def test_a_writer_failure_leaves_the_response_untouched(self) -> None:
        worker = _StubWorker({"iwconfig": _IWCONFIG, "wlanconfig": _WLANCONFIG})
        baseline = self._call(worker, persist=False)
        with mock.patch.object(context_snapshot, "write_clients", side_effect=OSError("read-only fs")):
            live = self._call(worker)
        self.assert_matches_baseline(live, baseline)
        self.assertEqual(self.files_written(), [])


class WifiCapabilityWriteThroughTests(_WriteThroughCase):
    def _call(self, handler, capability=lambda _w: _SSIDS, persist: bool = True, **kwargs) -> dict:
        worker = _StubWorker({})
        stack = [
            mock.patch.object(main, "resolve_dut", return_value=_StubContextObj(worker)),
            mock.patch.object(main, "get_ssid_capabilities", side_effect=capability),
        ]
        if not persist:
            stack.append(mock.patch.object(main, "_persist_scan"))
        for patcher in stack:
            patcher.start()
        try:
            return handler(dut="lab2", **kwargs)
        finally:
            for patcher in reversed(stack):
                patcher.stop()

    def test_capabilities_scan_is_persisted(self) -> None:
        response = self._call(main.get_wifi_capabilities)
        entries = context_snapshot.snapshot_entries()
        self.assertEqual({e["kind"] for e in entries}, {"ssid-capability"})
        payload = json.loads(
            next(e["path"] for e in entries if e["ext"] == "json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["ssids"], response["ssids"])

    def test_capabilities_response_is_unchanged_by_persisting(self) -> None:
        baseline = self._call(main.get_wifi_capabilities, persist=False)
        self.assertEqual(self.files_written(), [])
        live = self._call(main.get_wifi_capabilities)
        self.assert_matches_baseline(live, baseline)
        self.assertEqual(live["ssids"], _SSIDS)

    def test_empty_capabilities_write_nothing(self) -> None:
        self._call(main.get_wifi_capabilities, capability=lambda _w: [])
        self.assertEqual(self.files_written(), [])
        self.assertEqual(context_snapshot.skip_entries(), [])

    def test_capability_report_persists_source_a(self) -> None:
        with mock.patch.object(main, "get_wifi_survey", return_value={"available": False, "networks": []}):
            baseline = self._call(main.get_wifi_capability_report, persist=False)
            self.assertEqual(self.files_written(), [])
            live = self._call(main.get_wifi_capability_report)
        self.assert_matches_baseline(live, baseline)
        self.assertEqual({e["kind"] for e in context_snapshot.snapshot_entries()}, {"ssid-capability"})

    def test_a_writer_failure_leaves_the_capabilities_response_untouched(self) -> None:
        baseline = self._call(main.get_wifi_capabilities, persist=False)
        with mock.patch.object(context_snapshot, "write_capability", side_effect=OSError("read-only fs")):
            live = self._call(main.get_wifi_capabilities)
        self.assert_matches_baseline(live, baseline)
        self.assertEqual(self.files_written(), [])


class SiteSurveyWriteThroughTests(_WriteThroughCase):
    SURVEY = {"vaps": _SURVEY_VAPS, "neighbors": _NEIGHBORS, "captured_at": "2026-08-03T10:00:00"}
    EMPTY = {"vaps": [], "neighbors": [], "captured_at": "2026-08-03T10:00:00"}

    def _call(self, survey: dict, persist: bool = True) -> dict:
        stack = [
            mock.patch.object(main, "resolve_dut", return_value=_StubContextObj(_StubWorker({}))),
            mock.patch.object(main, "get_site_survey", return_value=survey),
            mock.patch.object(main, "_survey_progress_emitter", return_value=lambda _p: None),
        ]
        if not persist:
            stack.append(mock.patch.object(main, "_persist_scan"))
        for patcher in stack:
            patcher.start()
        try:
            return main.get_wifi_site_survey(dut="lab2")
        finally:
            for patcher in reversed(stack):
                patcher.stop()

    def test_a_successful_survey_is_persisted(self) -> None:
        self._call(self.SURVEY)
        names = self.files_written()
        self.assertEqual(len(names), 2, names)
        self.assertTrue(all(n.startswith("site-survey-lab2-") for n in names), names)
        payload = json.loads(
            (self.dirs["site-survey"] / next(n for n in names if n.endswith(".json"))).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(payload["neighbors"], _NEIGHBORS)
        # No recommendation was computed by this endpoint, so none is claimed.
        self.assertEqual(payload["recommendations"], [])

    def test_the_survey_response_is_unchanged_by_persisting(self) -> None:
        baseline = self._call(self.SURVEY, persist=False)
        self.assertEqual(self.files_written(), [])
        live = self._call(self.SURVEY)
        self.assertEqual(live, baseline)
        self.assertEqual(set(live), {"vaps", "neighbors", "captured_at"})

    def test_an_empty_survey_writes_nothing(self) -> None:
        self._call(self.EMPTY)
        self.assertEqual(self.files_written(), [])
        self.assertEqual(context_snapshot.skip_entries(), [])

    def test_a_writer_failure_leaves_the_survey_response_untouched(self) -> None:
        baseline = self._call(self.SURVEY, persist=False)
        with mock.patch.object(survey_snapshot, "write_snapshot", side_effect=OSError("read-only fs")):
            live = self._call(self.SURVEY)
        self.assertEqual(live, baseline)
        self.assertEqual(self.files_written(), [])

    def test_a_recommendation_free_snapshot_never_becomes_the_restored_cache(self) -> None:
        """The write-through must not blank the band badges after a restart.

        A bare site-survey snapshot is newer than the real recommendation it
        follows, so a strictly-newest restore would resurrect an empty
        recommendation for the DUT.
        """
        recommendations = [{"band": "2.4GHz", "recommended_channel": 1}]
        # Written by hand at an unambiguously older stamp: a real write_snapshot
        # call in the same second would reuse the filename this test needs to
        # stay distinct from the bare survey's.
        self.dirs["site-survey"].mkdir(parents=True, exist_ok=True)
        (self.dirs["site-survey"] / "site-survey-lab2-20200101-000000.json").write_text(
            json.dumps(
                {
                    "dut_id": "lab2",
                    "captured_at": "2020-01-01T00:00:00",
                    "recommendations": recommendations,
                    "neighbors": _NEIGHBORS,
                    "vaps": _SURVEY_VAPS,
                }
            ),
            encoding="utf-8",
        )
        self._call(self.SURVEY)
        names = self.files_written()
        self.assertEqual(len(names), 3, names)  # the old json, plus the bare survey's json+csv
        self.assertTrue(any(n != "site-survey-lab2-20200101-000000.json" for n in names))

        survey_cache.clear()
        survey_snapshot.restore_cache()
        cached = survey_cache.last_recommendation("lab2")
        assert cached is not None
        self.assertEqual(cached["recommendations"], recommendations)


class ChannelRecommendationWriteThroughTests(_WriteThroughCase):
    SURVEY = {"vaps": _SURVEY_VAPS, "neighbors": _NEIGHBORS, "captured_at": "2026-08-03T10:00:00"}

    def _call(self, persist: bool = True, capability=lambda _w: _SSIDS) -> dict:
        stack = [
            mock.patch.object(main, "resolve_dut", return_value=_StubContextObj(_StubWorker({}))),
            mock.patch.object(main, "get_site_survey", return_value=self.SURVEY),
            mock.patch.object(main, "get_ssid_capabilities", side_effect=capability),
            mock.patch.object(main, "_survey_progress_emitter", return_value=lambda _p: None),
        ]
        if not persist:
            stack.append(mock.patch.object(main, "_persist_scan"))
        for patcher in stack:
            patcher.start()
        try:
            return main.get_wifi_channel_recommendation(dut="lab2")
        finally:
            for patcher in reversed(stack):
                patcher.stop()

    def test_both_the_survey_and_the_capability_are_persisted(self) -> None:
        """The survey half predates C2; the capability half is what C2 adds."""
        self._call()
        self.assertEqual(
            {e["kind"] for e in context_snapshot.snapshot_entries()},
            {"site-survey", "ssid-capability"},
        )

    def test_the_recommendation_response_is_unchanged_by_persisting(self) -> None:
        baseline = self._call(persist=False)
        self.assertEqual(self.files_written(), [])
        live = self._call()
        self.assertEqual(live, baseline)
        self.assertEqual(set(live), {"recommendations", "neighbors", "survey_vaps", "captured_at"})

    def test_a_capability_writer_failure_leaves_the_response_untouched(self) -> None:
        baseline = self._call(persist=False)
        with mock.patch.object(context_snapshot, "write_capability", side_effect=OSError("read-only fs")):
            live = self._call()
        self.assertEqual(live, baseline)
        # The survey half still persisted — one failing writer does not stop the other.
        self.assertEqual({e["kind"] for e in context_snapshot.snapshot_entries()}, {"site-survey"})


class PersistedScanReachesTheBundleTests(unittest.TestCase):
    """End to end: a scan persisted today is in the ZIP of a log downloaded now.

    This is the whole point of C2 — the acceptance is not "a file was written"
    but "the operator's bundle contains it and the capture report says so".
    """

    LOG = (
        "= Test Time: 1, 2026-03-23 10:00:00\n"
        "= Test Time: 2, 2026-03-23 10:00:01\n"
    ) + "".join(f"filler line {i}\n" for i in range(1, 121))

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = Path(self._dir.name)
        self.log_dir = self.base / "logs"
        self.log_dir.mkdir(parents=True)
        self.dirs = {kind: self.base / "context" / kind for kind in context_snapshot.KINDS}
        for patcher in (
            mock.patch.object(context_snapshot, "_KIND_DIRS", self.dirs),
            mock.patch.object(serial_api, "LOG_DIR", self.log_dir),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_scanned_snapshot_is_bundled_and_reported_ok(self) -> None:
        worker = _StubWorker({"iwconfig": _IWCONFIG, "wlanconfig": _WLANCONFIG})
        with mock.patch.object(main, "resolve_dut", return_value=_StubContextObj(worker)):
            main.get_wifi_clients(dut="lab2")
        written = sorted(self.dirs["wifi-clients"].iterdir())
        self.assertEqual(len(written), 2, written)

        # A session log whose window brackets the scan we just persisted.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_name = f"dut-session-{stamp}.log"
        (self.log_dir / log_name).write_text(self.LOG, encoding="utf-8")

        analyzer_script = self.base / "tools" / "analyzer3.py"
        analyzer_script.parent.mkdir(parents=True)
        analyzer_script.write_text("print('stub')\n", encoding="utf-8")

        def fake_run(*_args, **kwargs):
            cwd = Path(kwargs["cwd"])
            (cwd / "cpu_usage.csv").write_text("cpu\n", encoding="utf-8")
            (cwd / "memory.csv").write_text("mem\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with (
            mock.patch.object(serial_api, "ANALYZER_SCRIPT", analyzer_script),
            mock.patch.object(serial_api.subprocess, "run", side_effect=fake_run),
        ):
            response = serial_api.download_log(log_name)

        with zipfile.ZipFile(Path(response.path)) as zf:
            names = zf.namelist()
            report = zf.read(
                next(n for n in names if n.endswith("context/capture-report.txt"))
            ).decode()
        bundled = [n for n in names if "/context/wifi-clients/" in n]
        self.assertEqual(len(bundled), 2, names)
        self.assertIn("wifi-clients: ok, 1 rows", report)
        # The kinds nobody scanned are still accounted for, not silently absent.
        self.assertIn("ssid-capability: skipped", report)


class OfflineToolFailureReportTests(unittest.TestCase):
    """C2b: a best-effort tool that fails leaves a line in the capture report."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = Path(self._dir.name)
        self.session_dir = self.base / "session"
        self.session_dir.mkdir()
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.analyzer = self.tools / "analyzer3.py"
        self.analyzer.write_text("# analyzer\n", encoding="utf-8")
        (self.tools / "wifi_timeseries.py").write_text("# wifi\n", encoding="utf-8")
        patcher = mock.patch.object(serial_api, "LOG_DIR", self.base / "logs")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _report(self) -> str:
        path = self.session_dir / "context" / context_snapshot.CAPTURE_REPORT_NAME
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _run(self, side_effect) -> None:
        with (
            mock.patch.object(serial_api, "ANALYZER_SCRIPT", self.analyzer),
            mock.patch.object(serial_api.subprocess, "run", side_effect=side_effect),
        ):
            serial_api.run_analyzer_for_session(self.session_dir)

    def test_a_nonzero_exit_is_reported(self) -> None:
        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "analyzer3.py":
                return subprocess.CompletedProcess(command, 0, "ok\n", "")
            return subprocess.CompletedProcess(command, 2, "", "Traceback\nValueError: bad log\n")

        self._run(fake_run)
        report = self._report()
        self.assertIn("offline-tool wifi_timeseries.py: failed — Traceback,", report)
        # One line only: the first line of stderr, not the whole dump.
        self.assertEqual(len(report.strip().splitlines()), 1)
        datetime.fromisoformat(report.strip().rsplit(", ", 1)[1])

    def test_a_tool_that_cannot_start_is_reported(self) -> None:
        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "analyzer3.py":
                return subprocess.CompletedProcess(command, 0, "ok\n", "")
            raise OSError("Exec format error")

        self._run(fake_run)
        self.assertIn("offline-tool wifi_timeseries.py: failed — Exec format error,", self._report())

    def test_the_failure_line_is_appended_to_an_existing_report(self) -> None:
        """bundle_session_context writes the report first; C2b must not clobber it."""
        report_dir = self.session_dir / "context"
        report_dir.mkdir()
        (report_dir / context_snapshot.CAPTURE_REPORT_NAME).write_text(
            "wifi-clients: ok, 3 rows, wifi-clients-lab2-20260803-100000.csv\n", encoding="utf-8"
        )

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "analyzer3.py":
                return subprocess.CompletedProcess(command, 0, "ok\n", "")
            return subprocess.CompletedProcess(command, 1, "", "boom")

        self._run(fake_run)
        lines = self._report().strip().splitlines()
        self.assertEqual(lines[0], "wifi-clients: ok, 3 rows, wifi-clients-lab2-20260803-100000.csv")
        self.assertIn("offline-tool wifi_timeseries.py: failed — boom,", lines[1])

    def test_a_successful_run_adds_no_line(self) -> None:
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, "ok\n", "")

        self._run(fake_run)
        self.assertEqual(self._report(), "")

    def test_an_unwritable_report_does_not_fail_the_run(self) -> None:
        # context/ occupied by a regular file, so both the mkdir and the open
        # fail: reporting a failure must not turn into a second failure.
        (self.session_dir / "context").write_text("not a directory\n", encoding="utf-8")

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "analyzer3.py":
                return subprocess.CompletedProcess(command, 0, "ok\n", "")
            return subprocess.CompletedProcess(command, 1, "", "boom")

        self._run(fake_run)  # must not raise


class OfflineToolListConsolidationTests(unittest.TestCase):
    """C2c: one list, shared, with the patch seam intact at both call sites."""

    def test_both_invocation_points_read_the_one_config_list(self) -> None:
        from app.config import OFFLINE_TOOL_NAMES
        from app.services import analyzer_service

        self.assertIs(serial_api.OFFLINE_TOOL_NAMES, OFFLINE_TOOL_NAMES)
        self.assertIs(analyzer_service.OFFLINE_TOOL_NAMES, OFFLINE_TOOL_NAMES)
        # analyzer3.py is ANALYZER_SCRIPT, not a list entry — the old decorative
        # index 0 that both call sites sliced away is gone.
        self.assertNotIn("analyzer3.py", OFFLINE_TOOL_NAMES)
        self.assertFalse(hasattr(serial_api, "OFFLINE_TOOLS"))
        self.assertFalse(hasattr(analyzer_service, "_OFFLINE_TOOL_NAMES"))


if __name__ == "__main__":
    unittest.main()
