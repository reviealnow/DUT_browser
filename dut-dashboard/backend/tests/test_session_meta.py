"""A session log says where it came from, and a Download is named for it.

Every bundle used to be `dut-session-<the moment Download was pressed>.zip`, so a
folder of them said nothing about which DUT, host or model each held -- and one
cable on this bench carried an AP6_420E in July and an AP6_840E from the 28th
under names that differed only in the time.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.dut.registry as registry_mod
from app import main
from app.dut.registry import DutRegistry
from app.serial import serial_worker
from app.services import context_snapshot, session_meta
from app.services.session_meta import format_meta_line, read_session_meta

START = {"kind": "start", "dut_id": "pi2-ttyusb0", "label": "pi2 ttyUSB0",
         "host": "192.168.30.124", "collector_id": "pi2"}
IDENTITY = {"kind": "identity", "device_id": "AP6420-PA10054DDHWVF2D", "model": "AP6_420"}


def _log(tmp: Path, *lines: str, name: str = "dut-session-pi2ttyUSB0-20260916-115810.log") -> Path:
    path = tmp / name
    path.write_text("".join(lines), encoding="utf-8")
    return path


class ReadSessionMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

    def test_a_log_names_its_dut_host_and_unit(self) -> None:
        path = _log(
            self.tmp,
            "# mode=ssh source=/dev/ttyUSB0\n",
            format_meta_line(START),
            "= Test Time: 1, 2026-09-16 11:58:12\n",
            format_meta_line(IDENTITY),
        )
        self.assertEqual(
            read_session_meta(path),
            {
                "mode": "ssh",
                "source": "/dev/ttyUSB0",
                "dut_id": "pi2-ttyusb0",
                "label": "pi2 ttyUSB0",
                "host": "192.168.30.124",
                "collector_id": "pi2",
                "device_id": "AP6420-PA10054DDHWVF2D",
                "model": "AP6_420",
            },
        )

    def test_every_field_is_present_when_the_log_says_nothing(self) -> None:
        """A stable shape: the listing's reader must not need to tell a missing
        key from a missing value."""
        meta = read_session_meta(_log(self.tmp, "nothing here\n"))
        self.assertEqual(set(meta), set(session_meta.FIELDS))
        self.assertTrue(all(value is None for value in meta.values()))

    def test_an_unreadable_log_describes_nothing(self) -> None:
        meta = read_session_meta(self.tmp / "missing.log")
        self.assertTrue(all(value is None for value in meta.values()))

    def test_a_legacy_log_still_gives_its_transport_and_model(self) -> None:
        """Written before these records existed: the header and the prompt are
        all it has, and they are enough to tell a 420E from an 840E."""
        path = _log(
            self.tmp,
            "# mode=serial source=/dev/cu.PL2303G-USBtoUART1130\n",
            "sh /mnt/data/sysMon001.sh 30 60\n",
            "AP6_840E# \n",
        )
        meta = read_session_meta(path)
        self.assertEqual(meta["mode"], "serial")
        self.assertEqual(meta["source"], "/dev/cu.PL2303G-USBtoUART1130")
        self.assertEqual(meta["model"], "AP6_840E")
        self.assertIsNone(meta["device_id"])

    def test_another_units_hostname_is_not_this_units_model(self) -> None:
        """A mesh probe lists every member by hostname, one per line. Read as a
        model, the root on this desk would be named after its Pi node."""
        path = _log(
            self.tmp,
            "# mode=serial source=/dev/cu.PL2303G-USBtoUART140\n",
            "AP6420-PA10054DDHWVF2D\n",
        )
        self.assertIsNone(read_session_meta(path)["model"])

    def test_the_measured_model_outranks_the_prompt(self) -> None:
        path = _log(
            self.tmp,
            "AP6_840E# \n",
            format_meta_line(START),
            format_meta_line(IDENTITY),
        )
        self.assertEqual(read_session_meta(path)["model"], "AP6_420")

    def test_an_identity_before_any_start_is_not_this_sessions(self) -> None:
        path = _log(self.tmp, format_meta_line(IDENTITY), "# mode=serial source=x\n")
        self.assertIsNone(read_session_meta(path)["device_id"])

    def test_a_replay_is_this_session_on_the_data_of_the_unit_it_replays(self) -> None:
        """A replay writes its own header and start record, then every line of
        the log it replays -- that log's header and records included. The
        transport is the replay; the unit is the one the data came from."""
        path = _log(
            self.tmp,
            "# mode=replay source=logs/old.log\n",
            format_meta_line({"kind": "start", "dut_id": "default", "label": "Bench"}),
            "# mode=ssh source=/dev/ttyUSB0\n",
            format_meta_line(START),
            format_meta_line(IDENTITY),
        )
        meta = read_session_meta(path)
        self.assertEqual((meta["mode"], meta["source"]), ("replay", "logs/old.log"))
        self.assertEqual(meta["dut_id"], "default")
        self.assertIsNone(meta["host"])
        self.assertEqual(meta["device_id"], "AP6420-PA10054DDHWVF2D")

    def test_only_the_head_is_read(self) -> None:
        """/api/logs reads every session log on each listing, and a long run is
        tens of megabytes."""
        path = _log(
            self.tmp,
            format_meta_line(START),
            "x" * 64 + "\n",
            format_meta_line(IDENTITY),
        )
        head = len(format_meta_line(START)) + 65
        self.assertIsNone(read_session_meta(path, limit=head)["device_id"])
        self.assertEqual(read_session_meta(path)["device_id"], "AP6420-PA10054DDHWVF2D")

    def test_a_record_cut_off_by_the_head_is_skipped(self) -> None:
        line = format_meta_line(IDENTITY)
        path = _log(self.tmp, format_meta_line(START), line)
        limit = len(format_meta_line(START)) + len(line) // 2
        self.assertIsNone(read_session_meta(path, limit=limit)["device_id"])


class _StubWsManager:
    def emit_from_thread(self, event: dict) -> None:
        pass


class StartRecordTests(unittest.TestCase):
    """What is written when a console opens -- through the real worker and log."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        for patcher in (
            mock.patch.object(registry_mod, "DUTS_FILE", self.tmp / "duts.json"),
            mock.patch.object(registry_mod, "snapshot_file_for", lambda d: self.tmp / f"snap-{d}.jsonl"),
            mock.patch.object(serial_worker, "LOG_DIR", self.tmp),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self.registry = DutRegistry(ws_manager=_StubWsManager(), loop=self._loop)
        self.ctx = self.registry.create_dut("pi2-ttyusb0", label="pi2 ttyUSB0")
        self.ctx.remote = {"host": "192.168.30.124", "user": "pi", "key_path": "",
                           "collector_id": "pi2", "port": 22, "device": "/dev/ttyUSB0",
                           "baudrate": 115200, "is_mesh": False, "backhaul_iface": None}

    def _open_log(self, mode: str) -> Path:
        worker = self.ctx.serial_worker
        worker._start_log_session(mode=mode, port="/dev/ttyUSB0", replay_path=None, label="")
        path = Path(worker.current_log_path)
        self.registry.note_console_open(self.ctx.dut_id, mode)
        worker._close_log_session()
        return path

    def _open(self, mode: str) -> dict:
        return read_session_meta(self._open_log(mode))

    def test_an_ssh_open_records_the_dut_and_the_host(self) -> None:
        meta = self._open("ssh")
        self.assertEqual(meta["dut_id"], "pi2-ttyusb0")
        self.assertEqual(meta["label"], "pi2 ttyUSB0")
        self.assertEqual(meta["host"], "192.168.30.124")
        self.assertEqual(meta["collector_id"], "pi2")

    def test_a_cable_open_of_a_remote_dut_names_no_host(self) -> None:
        """A DUT registered behind a Pi can be opened on a cable at this desk,
        and then no Pi was involved."""
        meta = self._open("serial")
        self.assertEqual(meta["dut_id"], "pi2-ttyusb0")
        self.assertIsNone(meta["host"])
        self.assertIsNone(meta["collector_id"])

    def test_the_last_known_unit_is_not_recorded_as_this_sessions(self) -> None:
        """On a same-model swap on the same cable the registry still holds the
        unit that left; only an identify says who is on the console now."""
        self.ctx.device_id = "AP6420-PA10054DDHWVF2D"
        self.ctx.model = "AP6_420"
        # The record as written, not as read back: the reader ignores a unit in
        # a start record, so asking it would pass whatever the writer did.
        records = [
            json.loads(line[len(session_meta.META_PREFIX):])
            for line in self._open_log("ssh").read_text(encoding="utf-8").splitlines()
            if line.startswith(session_meta.META_PREFIX)
        ]
        self.assertEqual([record["kind"] for record in records], ["start"])
        self.assertNotIn("device_id", records[0])
        self.assertNotIn("model", records[0])


class ListLogsOriginTests(unittest.TestCase):
    def test_each_session_row_says_where_it_came_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            _log(logs, "# mode=ssh source=/dev/ttyUSB0\n", format_meta_line(START), format_meta_line(IDENTITY))
            with (
                mock.patch.object(main, "LOG_DIR", logs),
                mock.patch.object(main, "ANALYZER_OUTPUT_DIR", Path(tmp) / "analyzer_output"),
                mock.patch.object(
                    context_snapshot, "_KIND_DIRS", {kind: Path(tmp) / kind for kind in context_snapshot.KINDS}
                ),
            ):
                sessions = main.list_logs()["sessions"]
        self.assertEqual(len(sessions), 1)
        origin = sessions[0]["origin"]
        self.assertEqual(origin["host"], "192.168.30.124")
        self.assertEqual(origin["device_id"], "AP6420-PA10054DDHWVF2D")
        self.assertEqual(origin["label"], "pi2 ttyUSB0")


if __name__ == "__main__":
    unittest.main()
