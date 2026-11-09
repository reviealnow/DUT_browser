"""Packet Monitor Hosts: the §B5 probe and the §B4 staged-capture store.

The Host is this machine behind ``fixtures/fake_ssh.py`` with HOME in a temp
directory (see test_tshark_runner.py); the workspace DB is a temp file.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import workspace
from app.services import packet_hosts as ph
from app.services import tshark_runner as tr

sys.path.insert(0, str(Path(__file__).parent))
from test_tshark_runner import FAKE_SSH, PASSWORD, install_fake_ionice  # noqa: E402

PROBE_TSHARK = """#!/bin/sh
# Stand-in for tshark on a Host: a version, a field list, nothing else.
case "$1" in
  --version) echo "TShark (Wireshark) ${FAKE_TSHARK_VERSION:-4.4.1} (Git v4.4.1 packaged)";;
  -G) echo x >> "$HOME/fields-calls"
      printf 'P\\tIEEE 802.11 wireless LAN\\twlan\\n'
      printf 'F\\tSSID\\twlan.ssid\\tFT_BYTES\\twlan\\t\\tBASE_NONE\\t0x0\\t\\n'
      printf 'F\\tBSS Id\\twlan.bssid\\tFT_ETHER\\twlan\\t\\tBASE_NONE\\t0x0\\t\\n';;
esac
"""


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Bench(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        tshark = self.bin / "tshark"
        tshark.write_text(PROBE_TSHARK)
        tshark.chmod(0o755)
        self._env = patch.dict(os.environ, {
            "HOME": str(self.home),
            "FAKE_SSH_PASSWORD": PASSWORD,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        })
        self._env.start()
        for key in ("FAKE_SSH_MODE", "FAKE_SSH_ANSWERED", "FAKE_SSH_DENIAL", "FAKE_TSHARK_VERSION"):
            os.environ.pop(key, None)
        self._db = patch.object(workspace, "WORKSPACE_DB", self.root / "workspace.db")
        self._db.start()
        workspace.init_db()
        self.fields_dir = self.root / "packets"
        self.store = ph.ProbeStore()
        self.target = tr.HostTarget(
            collector_id="c1", host="10.0.0.9", user="dut", password=PASSWORD, ssh_binary=FAKE_SSH,
        )
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for child in self._children:
            if child.poll() is None:
                child.kill()
                child.wait()
        self._db.stop()
        self._env.stop()
        self._tmp.cleanup()

    @property
    def captures(self) -> Path:
        return self.home / ".dut-browser" / "captures"

    @property
    def run_dir(self) -> Path:
        return self.home / ".dut-browser" / "run"

    def probe(self, keep: list[str] = (), active: list[str] = ()) -> ph.HostProbe:
        return ph.probe_host(
            self.target, self.store, active_jobs=list(active),
            keep_sha256s=list(keep), fields_dir=self.fields_dir,
        )

    def spawn(self, name: str) -> subprocess.Popen:
        """A long-lived process whose command name is ``name`` (a renamed sleep)."""
        link = self.root / "procs" / str(len(self._children)) / name
        link.parent.mkdir(parents=True)
        link.symlink_to(shutil.which("sleep"))
        child = subprocess.Popen([str(link), "60"])
        self._children.append(child)
        return child

    def stage_rows(self) -> dict[str, int]:
        rows = workspace.query_all(
            "SELECT file_sha256, size_bytes FROM packet_host_stage WHERE collector_id = ?", ("c1",)
        )
        return {row["file_sha256"]: row["size_bytes"] for row in rows}


class ProbeTest(_Bench):
    def test_probe_finds_the_tools_and_sweeps_the_workspace(self) -> None:
        keep, stale, gone = "a" * 64, "b" * 64, "c" * 64
        self.captures.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        (self.captures / f"{keep}.pcapng").write_bytes(b"0123456789")
        (self.captures / f"{stale}.pcapng").write_bytes(b"customer data")
        old_tmp = self.captures / f"{keep}.1111111111111111.tmp"
        old_tmp.write_bytes(b"half")
        two_hours_ago = time.time() - 7200
        os.utime(old_tmp, (two_hours_ago, two_hours_ago))
        fresh_tmp = self.captures / f"{keep}.2222222222222222.tmp"
        fresh_tmp.write_bytes(b"uploading")

        orphan = self.spawn("tshark")
        (self.run_dir / "3333333333333333.pid").write_text(f"{orphan.pid}\n")
        mine = self.spawn("tshark")
        (self.run_dir / "4444444444444444.pid").write_text(f"{mine.pid}\n")
        bystander = self.spawn("sleep")
        (self.run_dir / "5555555555555555.pid").write_text(f"{bystander.pid}\n")
        workspace.execute(
            "INSERT INTO packet_host_stage (collector_id, file_sha256, remote_path, size_bytes)"
            " VALUES ('c1', ?, 'x', 5)", (gone,),
        )

        probe = self.probe(keep=[keep], active=["4444444444444444"])

        self.assertEqual(probe.problems, [])
        self.assertTrue(probe.selectable)
        self.assertEqual(probe.tshark_version, "4.4.1")
        self.assertEqual(probe.tshark_path, str(self.bin / "tshark"))
        self.assertIn(probe.checksum_tool, ("sha256sum", "shasum"))
        self.assertIn(probe.base64_flag, ("-d", "-D"))
        self.assertGreater(probe.free_bytes, ph.MIN_FREE_BYTES)
        # §B4 sweeps
        self.assertEqual(probe.staged, {keep: 10})
        self.assertEqual(probe.swept, [stale])
        self.assertFalse((self.captures / f"{stale}.pcapng").exists())
        self.assertFalse(old_tmp.exists())
        self.assertTrue(fresh_tmp.exists())
        # §B3 run/ sweep: the orphaned tshark dies, ours and a non-tshark live
        orphan.wait(timeout=5)
        self.assertEqual(probe.killed, ["3333333333333333"])
        self.assertIsNone(mine.poll())
        self.assertIsNone(bystander.poll())
        self.assertTrue((self.run_dir / "4444444444444444.pid").exists())
        self.assertFalse((self.run_dir / "5555555555555555.pid").exists())
        # DB says what the Host holds
        self.assertEqual(self.stage_rows(), {keep: 10})
        # field table cached per version, fetched once
        self.assertTrue((self.fields_dir / "fields-4.4.1.json").is_file())
        self.assertIs(self.store.get("c1"), probe)
        self.probe(keep=[keep])
        self.assertEqual((self.home / "fields-calls").read_text().count("x"), 1)

    def test_a_tshark_older_than_3_6_is_refused_with_its_version(self) -> None:
        os.environ["FAKE_TSHARK_VERSION"] = "3.4.16"
        probe = self.probe()
        self.assertFalse(probe.selectable)
        self.assertIn("3.4.16", probe.problems[0])
        self.assertFalse((self.home / "fields-calls").exists())

    def test_a_password_that_does_not_work_is_a_login_error(self) -> None:
        os.environ["FAKE_SSH_MODE"] = "denied"
        with self.assertRaises(tr.RemoteError) as caught:
            self.probe()
        self.assertNotIn(PASSWORD, str(caught.exception))


class EvaluateProbeTest(unittest.TestCase):
    BASE = {
        "os": ["Linux"], "tshark_path": ["/usr/bin/tshark"],
        "tshark_version": ["TShark (Wireshark) 3.6.2 (Git v3.6.2 packaged as 3.6.2-2)"],
        "checksum": ["sha256sum"], "base64": ["-d"], "free_kb": [str(1024 * 1024)],
    }

    def evaluate(self, **override) -> ph.HostProbe:
        return ph.evaluate_probe("c1", {**self.BASE, **override})

    def test_3_6_is_the_floor(self) -> None:
        self.assertTrue(self.evaluate().selectable)
        self.assertTrue(self.evaluate(tshark_version=["TShark (Wireshark) 3.6.0"]).selectable)
        refused = self.evaluate(tshark_version=["TShark (Wireshark) 3.5.9"])
        self.assertEqual(refused.problems, ["tshark 3.5.9 is older than 3.6, the oldest supported"])

    def test_no_tshark_gives_the_install_command_for_the_os(self) -> None:
        linux = self.evaluate(tshark_path=[""], tshark_version=[])
        self.assertEqual(linux.problems, ["tshark not installed"])
        self.assertEqual(linux.install_hint, "sudo apt install tshark")
        mac = self.evaluate(os=["Darwin"], tshark_path=[""], tshark_version=[])
        self.assertEqual(mac.install_hint, "brew install wireshark")

    def test_less_than_200_mb_free_is_not_selectable(self) -> None:
        self.assertTrue(self.evaluate(free_kb=[str(200 * 1024)]).selectable)
        low = self.evaluate(free_kb=[str(200 * 1024 - 1)])
        self.assertFalse(low.selectable)
        self.assertIn("199 MB free", low.problems[0])
        self.assertFalse(self.evaluate(free_kb=[""]).selectable)

    def test_missing_tools_are_named(self) -> None:
        probe = self.evaluate(checksum=[""], base64=[""])
        self.assertEqual(len(probe.problems), 2)

    def test_malformed_staged_lines_are_ignored(self) -> None:
        probe = self.evaluate(staged=["a" * 64 + " 10", "../x 5", "b" * 64 + " big"])
        self.assertEqual(probe.staged, {"a" * 64: 10})


class PlanEvictionTest(unittest.TestCase):
    def test_least_recently_used_goes_first_until_within_budget(self) -> None:
        rows = [("a", 60), ("b", 60), ("c", 60), ("d", 60)]
        self.assertEqual(ph.plan_eviction(rows, set(), 150), (["a", "b"], False))
        self.assertEqual(ph.plan_eviction(rows, set(), 240), ([], False))

    def test_pinned_captures_are_never_evicted(self) -> None:
        rows = [("a", 60), ("b", 60), ("c", 60), ("d", 60)]
        self.assertEqual(ph.plan_eviction(rows, {"a"}, 150), (["b", "c"], False))

    def test_pinned_alone_over_budget_is_reported(self) -> None:
        rows = [("a", 100), ("b", 100), ("c", 10)]
        self.assertEqual(ph.plan_eviction(rows, {"a", "b"}, 150), (["c"], True))


class StagingTest(_Bench):
    def setUp(self) -> None:
        super().setUp()
        self.ready = self.probe()
        self.assertTrue(self.ready.selectable, self.ready.problems)

    def source(self, name: str, data: bytes) -> tuple[Path, str]:
        path = self.root / name
        path.write_bytes(data)
        return path, sha_of(data)

    def test_a_capture_is_staged_once_under_its_digest(self) -> None:
        path, sha = self.source("a.pcapng", os.urandom(300_000))
        remote = ph.stage_capture(self.target, self.ready, sha256=sha, source=path)
        self.assertEqual(remote, f"~/.dut-browser/captures/{sha}.pcapng")
        staged = self.captures / f"{sha}.pcapng"
        self.assertEqual(staged.read_bytes(), path.read_bytes())
        self.assertEqual(list(self.captures.glob("*.tmp")), [])
        self.assertEqual(self.stage_rows(), {sha: 300_000})
        # Already there: not uploaded again (the marker would be overwritten).
        staged.write_bytes(b"marker")
        ph.stage_capture(self.target, self.ready, sha256=sha, source=path)
        self.assertEqual(staged.read_bytes(), b"marker")

    def test_upload_and_checksum_run_in_the_idle_io_class(self) -> None:
        install_fake_ionice(self.bin, works=True)
        path, sha = self.source("a.pcapng", os.urandom(10_000))
        ph.stage_capture(self.target, self.ready, sha256=sha, source=path)
        calls = (self.home / "ionice-calls").read_text()
        self.assertIn("-c3 nice -n 19 cat", calls)
        self.assertIn(f"-c3 nice -n 19 {tr.CHECKSUM_COMMANDS[self.ready.checksum_tool]}", calls)
        self.assertEqual((self.captures / f"{sha}.pcapng").read_bytes(), path.read_bytes())

    def test_a_checksum_mismatch_fails_and_leaves_nothing(self) -> None:
        path, _ = self.source("a.pcapng", b"what was uploaded")
        wrong = sha_of(b"what the row says")
        with self.assertRaises(ph.StageError) as caught:
            ph.stage_capture(self.target, self.ready, sha256=wrong, source=path)
        self.assertIn("checksum mismatch", str(caught.exception))
        self.assertEqual(list(self.captures.iterdir()), [])
        self.assertEqual(self.stage_rows(), {})

    def test_a_host_without_room_refuses_before_writing(self) -> None:
        path, sha = self.source("a.pcapng", b"x" * 1000)
        with patch.object(ph, "MIN_FREE_BYTES", 10**18):
            with self.assertRaises(ph.StageError) as caught:
                ph.stage_capture(self.target, self.ready, sha256=sha, source=path)
        self.assertIn("KB free on this Host", str(caught.exception))
        self.assertEqual(list(self.captures.iterdir()), [])

    def test_the_byte_budget_evicts_least_recently_used(self) -> None:
        first, first_sha = self.source("1.pcapng", os.urandom(1000))
        second, second_sha = self.source("2.pcapng", os.urandom(1000))
        with patch.object(ph, "STAGE_BUDGET_BYTES", 1500):
            ph.stage_capture(self.target, self.ready, sha256=first_sha, source=first)
            ph.stage_capture(self.target, self.ready, sha256=second_sha, source=second)
        self.assertFalse((self.captures / f"{first_sha}.pcapng").exists())
        self.assertTrue((self.captures / f"{second_sha}.pcapng").exists())
        self.assertEqual(set(self.stage_rows()), {second_sha})

    def test_a_capture_a_running_analysis_needs_is_pinned(self) -> None:
        first, first_sha = self.source("1.pcapng", os.urandom(1000))
        second, second_sha = self.source("2.pcapng", os.urandom(1000))
        workspace.execute(
            "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status)"
            " VALUES (1, ?, 'c1', 'single_beacon', 'running')", (first_sha,),
        )
        with patch.object(ph, "STAGE_BUDGET_BYTES", 1500):
            ph.stage_capture(self.target, self.ready, sha256=first_sha, source=first)
            # The older capture is pinned; the one just staged is protected.
            ph.stage_capture(self.target, self.ready, sha256=second_sha, source=second)
            self.assertTrue((self.captures / f"{first_sha}.pcapng").exists())
            self.assertTrue((self.captures / f"{second_sha}.pcapng").exists())
            # Both pinned: the budget stays exceeded, and that is reported.
            workspace.execute(
                "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status)"
                " VALUES (2, ?, 'c1', 'single_beacon', 'queued')", (second_sha,),
            )
            self.assertTrue(ph.evict_over_budget(self.target, self.ready.base64_flag))
        self.assertTrue((self.captures / f"{first_sha}.pcapng").exists())
        self.assertTrue((self.captures / f"{second_sha}.pcapng").exists())

    def test_an_unready_host_is_refused(self) -> None:
        path, sha = self.source("a.pcapng", b"x")
        self.ready.problems.append("tshark not installed")
        with self.assertRaises(ph.StageError):
            ph.stage_capture(self.target, self.ready, sha256=sha, source=path)


class FileDigestTest(_Bench):
    def test_a_legacy_row_gets_its_digest_on_first_use(self) -> None:
        path = self.root / "old.pcap"
        path.write_bytes(b"legacy capture")
        file_id = workspace.execute(
            "INSERT INTO files (filename, filepath, size) VALUES ('old.pcap', ?, 14)", (str(path),)
        )
        row = dict(workspace.query_one("SELECT * FROM files WHERE id = ?", (file_id,)))
        self.assertIsNone(row["sha256"])
        self.assertEqual(ph.ensure_file_sha256(row, path), sha_of(b"legacy capture"))
        stored = workspace.query_one("SELECT sha256 FROM files WHERE id = ?", (file_id,))
        self.assertEqual(stored["sha256"], sha_of(b"legacy capture"))
        self.assertEqual(ph.known_sha256s(), [sha_of(b"legacy capture")])

    def test_an_existing_digest_is_not_recomputed(self) -> None:
        row = {"id": 1, "sha256": "d" * 64}
        self.assertEqual(ph.ensure_file_sha256(row, self.root / "missing"), "d" * 64)


class SchemaTest(_Bench):
    def test_analysis_status_is_constrained(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            workspace.execute(
                "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status)"
                " VALUES (1, 'x', 'c1', 'm', 'bogus')"
            )

    def test_an_analysis_can_record_a_warning(self) -> None:
        analysis_id = workspace.execute(
            "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id, status, warning)"
            " VALUES (1, 'x', 'c1', 'm', 'done', 'capture truncated')"
        )
        row = workspace.query_one("SELECT status, warning FROM packet_analyses WHERE id = ?", (analysis_id,))
        self.assertEqual((row["status"], row["warning"]), ("done", "capture truncated"))

    def test_one_stage_row_per_host_and_capture(self) -> None:
        insert = (
            "INSERT INTO packet_host_stage (collector_id, file_sha256, remote_path, size_bytes)"
            " VALUES ('c1', 'x', 'p', 1)"
        )
        workspace.execute(insert)
        with self.assertRaises(sqlite3.IntegrityError):
            workspace.execute(insert)


if __name__ == "__main__":
    unittest.main()
