"""Packet Monitor analyses end to end: queue, job, results, cancel, deletion.

The Host is this machine behind ``fixtures/fake_ssh.py`` (key login), with HOME
in a temp directory. "tshark" is a stand-in that answers ``--version`` and
``-G fields`` from the 4.4.1 field table in ``fixtures/packets/`` and, for an
analysis, prints that fixture's real tshark 4.4.1 TSV restricted to the ``-e``
fields it was asked for -- so the decode, rules and storage path runs on what
tshark actually prints.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api import packets_api
from app.db import workspace
from app.services import file_service
from app.services import packet_decode as pd
from app.services import packet_hosts as ph
from app.services import packet_jobs as pj
from app.services import packet_modes as pm
from app.services import packet_rules as pr
from app.services import tshark_runner as tr

sys.path.insert(0, str(Path(__file__).parent))
from test_tshark_runner import FAKE_SSH  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "packets"
FIELD_TYPES = FIXTURES / "field_types_tshark-4.4.1.json"
TSV = FIXTURES / "beacons_tshark-4.4.1.tsv"

STAND_IN = """#!{python}
# Stand-in tshark for the Packet Monitor job tests.
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("TShark (Wireshark) 4.4.1 (Git v4.4.1 packaged)")
    sys.exit(0)
if args[:2] == ["-G", "fields"]:
    for name, ftype in json.load(open(os.environ["FAKE_TSHARK_TYPES"])).items():
        print("F\\t%s\\t%s\\t%s\\tproto\\t\\t" % (name, name, ftype))
    sys.exit(0)
def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None
display_filter = opt("-Y") or ""
if "nope" in display_filter:
    sys.stderr.write('tshark: "wlan.nope" is not a valid protocol or protocol field.\\n')
    sys.exit(4)
if opt("-r") == "-":
    sys.stdin.buffer.read()
    sys.exit(0)
with open(os.path.join(os.environ["HOME"], "tshark-calls"), "a") as log:
    log.write(" ".join(args) + "\\n")
time.sleep(float(os.environ.get("FAKE_TSHARK_SLEEP", "0")))
if opt("-T") == "json":
    print(json.dumps([{{"_source": {{"layers": {{"frame": {{"frame.number": opt("-c")}}}}}}}}]))
    sys.exit(0)
fields = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
lines = open(os.environ["FAKE_TSHARK_TSV"], encoding="utf-8").read().split("\\n")
header = lines[0].split("\\t")
print("\\t".join(fields))
for line in lines[1:]:
    if line:
        cells = dict(zip(header, line.split("\\t")))
        print("\\t".join(cells.get(name, "") for name in fields))
sys.stdout.flush()
if os.environ.get("FAKE_TSHARK_TRUNCATED"):
    sys.stderr.write('tshark: The file "x" appears to have been cut short in the middle of a packet.\\n')
    sys.exit(2)
"""


class FakeRegistry:
    """The two collector-registry calls Packet Monitor makes, for one Host."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def get(self, collector_id):
        if collector_id != "pi":
            raise KeyError(collector_id)
        return SimpleNamespace(ip="10.0.0.9", user="pi", port=22)

    def credentials_for(self, collector_id):
        return {"key_path": "/nonexistent/key", "password": ""}

    def status(self, collector_id):
        self.get(collector_id)
        return {"id": collector_id, "label": "Bench Pi", "connected": self.connected}

    def list_status(self):
        return [self.status("pi")]


def expected_rows(mode_id: str) -> tuple[list[dict], list[pr.Finding]]:
    """What the pipeline should produce, computed straight from the fixture."""
    mode = pm.get_mode(mode_id)
    columns = pm.resolve_columns(mode, json.loads(FIELD_TYPES.read_text()))
    raw, _ = pd.rows_from_tsv(TSV.read_text())
    rows = [pm.decode_row(columns, r) for r in raw]
    return rows, pr.evaluate(rows, mode.rules, mode.group_by)


class _JobsBench(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        tshark = bin_dir / "tshark"
        tshark.write_text(STAND_IN.format(python=sys.executable))
        tshark.chmod(0o755)
        self._env = patch.dict(os.environ, {
            "HOME": str(self.home),
            "FAKE_SSH_MODE": "key",
            "FAKE_TSHARK_TYPES": str(FIELD_TYPES),
            "FAKE_TSHARK_TSV": str(TSV),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        })
        self._env.start()
        for key in ("FAKE_TSHARK_SLEEP", "FAKE_TSHARK_TRUNCATED"):
            os.environ.pop(key, None)
        self._patches = [
            patch.object(workspace, "WORKSPACE_DB", self.root / "workspace.db"),
            patch.object(file_service, "UPLOAD_DIR", self.root / "uploads"),
        ]
        for p in self._patches:
            p.start()
        (self.root / "uploads").mkdir()
        workspace.init_db()
        self.registry = FakeRegistry()
        self.jobs = pj.PacketJobs(
            self.registry,
            results_dir=self.root / "packets",
            fields_dir=self.root / "fields",
            ssh_binary=FAKE_SSH,
        )

    def tearDown(self) -> None:
        self.jobs.stop()
        self.jobs.wait_idle(15)
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    def add_file(self, name: str = "beacons.pcapng", data: bytes | None = None) -> int:
        path = self.root / "uploads" / name
        path.write_bytes(data if data is not None else os.urandom(4096))
        return workspace.execute(
            "INSERT INTO files (filename, filepath, size) VALUES (?, ?, ?)",
            (name, str(path), path.stat().st_size),
        )

    def run_analysis(self, mode_id: str = "beacon_security", **kwargs) -> dict:
        file_id = kwargs.pop("file_id", None) or self.add_file()
        analysis_id = self.jobs.create(
            file_id=file_id, collector_id="pi", mode_id=mode_id, user_id=7, **kwargs
        )
        self.assertTrue(self.jobs.wait_idle(60), "the analysis never finished")
        return self.jobs.get(analysis_id)

    def tshark_calls(self) -> list[str]:
        path = self.home / "tshark-calls"
        return path.read_text().splitlines() if path.exists() else []


class AnalysisTest(_JobsBench):
    def test_an_analysis_runs_end_to_end_on_real_tshark_output(self) -> None:
        analysis = self.run_analysis()
        self.assertEqual(analysis["status"], "done", analysis["error"])
        rows, findings = expected_rows("beacon_security")
        self.assertEqual(analysis["frames_shown"], len(rows))
        self.assertEqual(analysis["rows_dropped"], 0)
        self.assertEqual(analysis["tshark_version"], "4.4.1")
        self.assertEqual(analysis["created_by_user_id"], 7)
        self.assertIsNone(analysis["warning"])
        summary = {"fail": 0, "warn": 0, "info": 0}
        for finding in findings:
            summary[finding.severity] += 1
        self.assertEqual(analysis["findings_summary"], summary)
        self.assertGreater(sum(summary.values()), 0, "the fixture should trip some rules")
        self.assertEqual(
            [f["rule"] for f in self.jobs.findings(analysis["id"])],
            [f.rule for f in findings],
        )
        page = self.jobs.rows(analysis["id"], 0, 500)
        self.assertEqual(page["total"], len(rows))
        self.assertEqual(page["rows"], json.loads(json.dumps(rows, default=str)))

    def test_the_mode_filter_and_the_user_filter_are_both_applied(self) -> None:
        self.run_analysis(display_filter="wlan.ssid")
        call = self.tshark_calls()[-1]
        self.assertIn(f"-Y ({pm.BEACON_FILTER}) && (wlan.ssid)", call)
        self.assertIn("-n -r captures/", call)

    def test_paging_seeks_through_the_index(self) -> None:
        with patch.object(pj, "ROWS_INDEX_EVERY", 5):
            analysis = self.run_analysis()
        everything = self.jobs.rows(analysis["id"], 0, 500)["rows"]
        for offset, limit in ((0, 3), (4, 3), (5, 5), (7, 11), (len(everything) - 2, 10)):
            with self.subTest(offset=offset, limit=limit):
                page = self.jobs.rows(analysis["id"], offset, limit)
                self.assertEqual(page["rows"], everything[offset:offset + limit])
        self.assertEqual(self.jobs.rows(analysis["id"], len(everything), 5)["rows"], [])

    def test_groups_are_one_row_per_bss(self) -> None:
        analysis = self.run_analysis()
        rows, _ = expected_rows("beacon_security")
        groups = self.jobs.groups(analysis["id"])
        self.assertEqual({g["group"] for g in groups}, {r["bssid"] for r in rows})
        self.assertEqual(sum(g["frames"] for g in groups), len(rows))

    def test_csv_export_has_the_visible_columns_and_every_row(self) -> None:
        analysis = self.run_analysis()
        text = "".join(self.jobs.export_csv(analysis["id"]))
        table = list(csv.reader(io.StringIO(text)))
        visible = [c["label"] for c in analysis["columns"] if not c["hidden"]]
        self.assertEqual(table[0], visible)
        self.assertEqual(len(table) - 1, analysis["frames_shown"])

    def test_extra_fields_become_columns(self) -> None:
        analysis = self.run_analysis(mode_id="manual", extra_fields=["wlan.country_info.code"])
        self.assertEqual(analysis["status"], "done", analysis["error"])
        self.assertEqual(analysis["extra_fields"], ["wlan.country_info.code"])
        self.assertIn("wlan.country_info.code", [c["label"] for c in analysis["columns"]])
        self.assertIn("-e wlan.country_info.code", self.tshark_calls()[-1])

    def test_an_extra_field_this_tshark_does_not_know_is_refused(self) -> None:
        with self.assertRaises(pj.PacketError) as caught:
            self.jobs.create(
                file_id=self.add_file(), collector_id="pi", mode_id="manual",
                extra_fields=["wlan.nope"],
            )
        self.assertIn("does not know", str(caught.exception))

    def test_a_bad_filter_is_a_400_with_tsharks_words_and_no_job(self) -> None:
        with self.assertRaises(pj.PacketError) as caught:
            self.jobs.create(
                file_id=self.add_file(), collector_id="pi", mode_id="manual",
                display_filter="wlan.nope",
            )
        self.assertEqual(caught.exception.status, 400)
        self.assertIn('"wlan.nope" is not a valid protocol', str(caught.exception))
        self.assertEqual(self.jobs.recent(), [])

    def test_a_truncated_capture_finishes_with_a_warning(self) -> None:
        os.environ["FAKE_TSHARK_TRUNCATED"] = "1"
        analysis = self.run_analysis()
        self.assertEqual(analysis["status"], "done", analysis["error"])
        self.assertIn("cut short", analysis["warning"])
        self.assertEqual(analysis["frames_shown"], len(expected_rows("beacon_security")[0]))

    def test_a_host_that_is_not_connected_is_refused(self) -> None:
        self.registry.connected = False
        with self.assertRaises(pj.Conflict):
            self.jobs.create(file_id=self.add_file(), collector_id="pi", mode_id="manual")

    def test_unknown_mode_file_and_host(self) -> None:
        file_id = self.add_file()
        with self.assertRaises(pj.PacketError):
            self.jobs.create(file_id=file_id, collector_id="pi", mode_id="nope")
        with self.assertRaises(pj.NotFound):
            self.jobs.create(file_id=999, collector_id="pi", mode_id="manual")
        with self.assertRaises(pj.NotFound):
            self.jobs.create(file_id=file_id, collector_id="elsewhere", mode_id="manual")


class FilterCheckTest(_JobsBench):
    def test_valid_invalid_and_unanswered(self) -> None:
        self.assertEqual(self.jobs.validate_filter("pi", "wlan.ssid"), {"ok": True, "error": None})
        bad = self.jobs.validate_filter("pi", "wlan.nope")
        self.assertIs(bad["ok"], False)
        self.assertIn('"wlan.nope" is not a valid protocol', bad["error"])
        self.registry.connected = False
        silent = self.jobs.validate_filter("pi", "wlan.ssid")
        self.assertIsNone(silent["ok"])

    def test_the_interactive_lane_does_not_wait_behind_an_analysis(self) -> None:
        os.environ["FAKE_TSHARK_SLEEP"] = "4"
        self.jobs.create(file_id=self.add_file(), collector_id="pi", mode_id="manual")
        time.sleep(1.0)  # the analysis is now running its 4 s tshark
        started = time.monotonic()
        self.assertTrue(self.jobs.validate_filter("pi", "wlan.ssid")["ok"])
        self.assertLess(time.monotonic() - started, 3)


class QueueAndCancelTest(_JobsBench):
    def test_one_analysis_runs_per_host_and_a_queued_one_cancels_at_once(self) -> None:
        os.environ["FAKE_TSHARK_SLEEP"] = "3"
        file_id = self.add_file()
        first = self.jobs.create(file_id=file_id, collector_id="pi", mode_id="manual")
        second = self.jobs.create(file_id=file_id, collector_id="pi", mode_id="manual")
        deadline = time.monotonic() + 10
        while self.jobs.get(first)["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.jobs.get(first)["status"], "running")
        self.assertEqual(self.jobs.get(second)["status"], "queued")
        self.jobs.cancel(second)
        self.assertEqual(self.jobs.get(second)["status"], "cancelled")
        self.assertTrue(self.jobs.wait_idle(30))
        self.assertEqual(self.jobs.get(first)["status"], "done")
        self.assertEqual(len(self.tshark_calls()), 1)

    def test_a_running_analysis_can_be_cancelled(self) -> None:
        os.environ["FAKE_TSHARK_SLEEP"] = "20"
        analysis_id = self.jobs.create(file_id=self.add_file(), collector_id="pi", mode_id="manual")
        deadline = time.monotonic() + 10
        while not self.tshark_calls() and time.monotonic() < deadline:
            time.sleep(0.05)
        started = time.monotonic()
        self.jobs.cancel(analysis_id)
        self.assertTrue(self.jobs.wait_idle(30))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(self.jobs.get(analysis_id)["status"], "cancelled")

    def test_a_finished_analysis_cannot_be_cancelled(self) -> None:
        analysis = self.run_analysis(mode_id="manual")
        with self.assertRaises(pj.Conflict):
            self.jobs.cancel(analysis["id"])


class FrameDetailTest(_JobsBench):
    def test_a_frame_is_fetched_once_then_served_from_cache(self) -> None:
        analysis = self.run_analysis(mode_id="manual")
        calls_before = len(self.tshark_calls())
        frame = self.jobs.frame(analysis["id"], 3)
        self.assertEqual(frame["_source"]["layers"]["frame"]["frame.number"], "3")
        detail_call = self.tshark_calls()[-1]
        self.assertIn("-c 3 -Y frame.number==3 -T json", detail_call)
        self.assertEqual(self.jobs.frame(analysis["id"], 3), frame)
        self.assertEqual(len(self.tshark_calls()), calls_before + 1)

    def test_an_evicted_capture_is_staged_again(self) -> None:
        analysis = self.run_analysis(mode_id="manual")
        for staged in (self.home / ".dut-browser" / "captures").glob("*.pcapng"):
            staged.unlink()
        self.jobs.frame(analysis["id"], 1)
        self.assertEqual(len(list((self.home / ".dut-browser" / "captures").glob("*.pcapng"))), 1)


class LifecycleTest(_JobsBench):
    def _insert(self, status: str, finished_days_ago: int | None = None, file_id: int = 1) -> int:
        finished = (
            None if finished_days_ago is None
            else f"datetime('now', '-{finished_days_ago} days')"
        )
        return workspace.execute(
            "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status,"
            f" finished_at) VALUES (?, 'x', 'pi', 'manual', ?, {finished or 'NULL'})",
            (file_id, status),
        )

    def test_a_restart_fails_whatever_was_queued_or_running(self) -> None:
        queued, running, done = self._insert("queued"), self._insert("running"), self._insert("done", 1)
        self.assertEqual(self.jobs.mark_interrupted(), 2)
        for analysis_id in (queued, running):
            row = self.jobs.get(analysis_id)
            self.assertEqual((row["status"], row["error"]), ("failed", "Interrupted by a restart."))
        self.assertEqual(self.jobs.get(done)["status"], "done")

    def test_retention_removes_analyses_older_than_30_days(self) -> None:
        old, recent = self._insert("done", 31), self._insert("done", 29)
        self.jobs.results_dir.mkdir(parents=True)
        (self.jobs.results_dir / f"{old}.jsonl").write_text("{}\n")
        (self.jobs.results_dir / f"{recent}.jsonl").write_text("{}\n")
        self.assertEqual(self.jobs.sweep_retention(), 1)
        with self.assertRaises(pj.NotFound):
            self.jobs.get(old)
        self.assertFalse((self.jobs.results_dir / f"{old}.jsonl").exists())
        self.assertTrue((self.jobs.results_dir / f"{recent}.jsonl").exists())


class FileDeletionTest(_JobsBench):
    """§B4 *Deletion*, driven through what files_api calls after the row is gone."""

    def _delete_file(self, file_id: int) -> threading.Thread | None:
        row = file_service.get_file_by_id(file_id)
        file_service.delete_file(row)
        return self.jobs.file_deleted(file_id, row["sha256"])

    def staged(self) -> list[Path]:
        return list((self.home / ".dut-browser" / "captures").glob("*.pcapng"))

    def test_deleting_a_file_removes_its_analyses_results_and_staged_copy(self) -> None:
        analysis = self.run_analysis(mode_id="manual")
        self.assertEqual(len(self.staged()), 1)
        remover = self._delete_file(analysis["file_id"])
        remover.join(30)
        with self.assertRaises(pj.NotFound):
            self.jobs.get(analysis["id"])
        self.assertEqual(list(self.jobs.results_dir.glob(f"{analysis['id']}.*")), [])
        self.assertEqual(self.staged(), [])
        self.assertEqual(
            workspace.query_all("SELECT * FROM packet_host_stage"), [],
        )

    def test_a_staged_copy_another_file_shares_is_kept(self) -> None:
        data = os.urandom(2048)
        first = self.add_file("a.pcapng", data)
        second = self.add_file("b.pcapng", data)
        ph.ensure_file_sha256(file_service.get_file_by_id(second), self.root / "uploads" / "b.pcapng")
        self.run_analysis(mode_id="manual", file_id=first)
        self.assertIsNone(self._delete_file(first))
        self.assertEqual(len(self.staged()), 1)

    def test_deleting_a_file_cancels_its_running_analysis(self) -> None:
        os.environ["FAKE_TSHARK_SLEEP"] = "20"
        file_id = self.add_file()
        analysis_id = self.jobs.create(file_id=file_id, collector_id="pi", mode_id="manual")
        deadline = time.monotonic() + 10
        while not self.tshark_calls() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._delete_file(file_id)
        self.assertTrue(self.jobs.wait_idle(30))
        with self.assertRaises(pj.NotFound):
            self.jobs.get(analysis_id)
        self.assertEqual(list(self.jobs.results_dir.glob(f"{analysis_id}.*")), [])


class ConcurrentStagingTest(_JobsBench):
    """§B12: two jobs staging one capture at once upload it once and both succeed."""

    def test_the_same_capture_is_uploaded_once(self) -> None:
        file_id = self.add_file()
        row = file_service.get_file_by_id(file_id)
        source = self.root / "uploads" / row["filename"]
        sha = ph.ensure_file_sha256(row, source)
        target = self.jobs._target("pi")
        probe = self.jobs.probe("pi")
        uploads = []
        real_run = tr.run_remote

        def counting(target_, script, **kwargs):
            if "staging needs" in script:
                uploads.append(1)
                time.sleep(0.5)  # hold the lock long enough for the other job to arrive
            return real_run(target_, script, **kwargs)

        results: list[object] = []
        with patch.object(ph.tr, "run_remote", counting):
            threads = [
                threading.Thread(target=lambda: results.append(
                    ph.stage_capture(target, probe, sha256=sha, source=source)
                ))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(uploads), 1)


def _job_threads_alive() -> bool:
    return any(t.name.startswith("packet-jobs-") and t.is_alive() for t in threading.enumerate())


class ReviewFixesTest(_JobsBench):
    """The edge cases edisqa-git found reviewing #230, one test each."""

    def _staged_sha(self) -> tuple[dict, str]:
        analysis = self.run_analysis(mode_id="manual")
        return analysis, analysis["file_sha256"]

    def _wait_for_tshark(self) -> None:
        deadline = time.monotonic() + 10
        while not self.tshark_calls() and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_1_removal_waits_for_a_stage_and_keeps_a_capture_pinned_meanwhile(self) -> None:
        """A remove that passed its pinned check must not pull a capture a new job just found."""
        analysis, sha = self._staged_sha()
        staged = self.home / ".dut-browser" / "captures" / f"{sha}.pcapng"
        lock = ph._stage_lock("pi", sha)
        lock.acquire()  # another job is inside stage_capture for this capture
        outcome: list[BaseException | dict] = []

        def remove() -> None:
            try:
                outcome.append(self.jobs.delete_staged("pi", sha))
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                outcome.append(exc)

        remover = threading.Thread(target=remove)
        remover.start()
        try:
            time.sleep(0.5)
            self.assertTrue(remover.is_alive(), "the removal did not wait for the stage lock")
            self.assertTrue(staged.exists())
            # The other job's analysis is queued while the remover waits.
            workspace.execute(
                "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status)"
                " VALUES (?, ?, 'pi', 'manual', 'queued')",
                (analysis["file_id"], sha),
            )
        finally:
            lock.release()
        remover.join(30)
        self.assertIsInstance(outcome[0], pj.Conflict)
        self.assertTrue(staged.exists())

    def test_1_an_unpinned_staged_copy_is_removed(self) -> None:
        _, sha = self._staged_sha()
        self.assertEqual(self.jobs.delete_staged("pi", sha), {"ok": True})
        self.assertFalse((self.home / ".dut-browser" / "captures" / f"{sha}.pcapng").exists())
        self.assertEqual(workspace.query_all("SELECT * FROM packet_host_stage"), [])

    def test_2_stop_waits_for_a_cancelled_job_to_finish(self) -> None:
        os.environ["FAKE_TSHARK_SLEEP"] = "20"
        analysis_id = self.jobs.create(file_id=self.add_file(), collector_id="pi", mode_id="manual")
        self._wait_for_tshark()
        self.assertTrue(self.jobs.stop())
        self.assertFalse(_job_threads_alive(), "stop() returned while a job was still running")
        self.assertEqual(self.jobs.get(analysis_id)["status"], "cancelled")

    def test_3_a_frame_restage_does_not_hold_the_interactive_lane(self) -> None:
        analysis, sha = self._staged_sha()
        (self.home / ".dut-browser" / "captures" / f"{sha}.pcapng").unlink()
        real_stage = ph.stage_capture

        def slow_stage(*args, **kwargs):
            time.sleep(3)  # a 50 MB re-upload, compressed
            return real_stage(*args, **kwargs)

        with patch.object(pj.ph, "stage_capture", slow_stage):
            fetch = threading.Thread(target=lambda: self.jobs.frame(analysis["id"], 1))
            fetch.start()
            time.sleep(0.5)
            started = time.monotonic()
            check = self.jobs.validate_filter("pi", "wlan.ssid")
            elapsed = time.monotonic() - started
            fetch.join(30)
        self.assertIs(check["ok"], True)
        self.assertLess(elapsed, 2, "the filter check waited behind a re-stage")

    def test_4_a_cancelled_queued_job_leaves_no_bookkeeping(self) -> None:
        os.environ["FAKE_TSHARK_SLEEP"] = "3"
        file_id, other_file = self.add_file("a.pcapng"), self.add_file("b.pcapng")
        self.jobs.create(file_id=file_id, collector_id="pi", mode_id="manual")
        queued = self.jobs.create(file_id=file_id, collector_id="pi", mode_id="manual")
        doomed = self.jobs.create(file_id=other_file, collector_id="pi", mode_id="manual")
        self.jobs.cancel(queued)
        row = file_service.get_file_by_id(other_file)
        file_service.delete_file(row)
        self.jobs.file_deleted(other_file, row["sha256"])
        for analysis_id in (queued, doomed):
            self.assertNotIn(analysis_id, self.jobs._cancel)
            self.assertNotIn(analysis_id, self.jobs._deleted)
        self.assertTrue(self.jobs.wait_idle(30))

    def test_5_rows_are_deleted_before_their_result_files(self) -> None:
        first = self.run_analysis(mode_id="manual")
        second = self.run_analysis(mode_id="manual")
        workspace.execute(
            "UPDATE packet_analyses SET finished_at = datetime('now', '-40 days') WHERE id = ?",
            (second["id"],),
        )
        row_still_there = []
        real_delete = self.jobs._delete_results

        def watching(analysis_id: int) -> None:
            row_still_there.append(
                workspace.query_one("SELECT 1 FROM packet_analyses WHERE id = ?", (analysis_id,)) is not None
            )
            real_delete(analysis_id)

        with patch.object(self.jobs, "_delete_results", watching):
            self.assertEqual(self.jobs.sweep_retention(), 1)
            row = file_service.get_file_by_id(first["file_id"])
            file_service.delete_file(row)
            self.jobs.file_deleted(first["file_id"], None)
        self.assertEqual(row_still_there, [False, False])

    def test_7_eviction_goes_through_the_one_removal_path(self) -> None:
        self._staged_sha()
        with patch.object(ph, "STAGE_BUDGET_BYTES", 0), \
                patch.object(ph, "remove_staged", wraps=ph.remove_staged) as spy:
            ph.evict_over_budget(self.jobs._target("pi"), self.jobs.probe("pi").base64_flag)
        spy.assert_called_once()

    def test_8_export_checks_the_analysis_before_it_streams(self) -> None:
        analysis_id = workspace.execute(
            "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status)"
            " VALUES (1, 'x', 'pi', 'manual', 'running')"
        )
        with self.assertRaises(pj.Conflict):
            self.jobs.export_csv(analysis_id)  # not iterated: the check is up front
        with self.assertRaises(pj.NotFound):
            self.jobs.export_csv(999)


class ApiMappingTest(unittest.TestCase):
    """packets_api turns PacketError into its HTTP status, and nothing else."""

    def test_status_codes(self) -> None:
        for error, status in ((pj.PacketError, 400), (pj.NotFound, 404),
                              (pj.Conflict, 409), (pj.HostUnavailable, 502)):
            with self.subTest(error=error.__name__):
                def boom():
                    raise error("no")
                with self.assertRaises(HTTPException) as caught:
                    packets_api._call(boom)
                self.assertEqual(caught.exception.status_code, status)

    def test_without_packet_jobs_the_routes_say_so(self) -> None:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        with self.assertRaises(HTTPException) as caught:
            packets_api.list_hosts(request)
        self.assertEqual(caught.exception.status_code, 503)


class SchemaTest(_JobsBench):
    def test_extra_fields_column_is_added_to_an_existing_table(self) -> None:
        conn = sqlite3.connect(self.root / "workspace.db")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(packet_analyses)")}
        finally:
            conn.close()
        self.assertIn("extra_fields", columns)


if __name__ == "__main__":
    unittest.main()
