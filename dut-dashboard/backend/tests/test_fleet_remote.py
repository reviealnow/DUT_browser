from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.dut.registry as registry_mod
from app.dut.registry import DutRegistry
from app.parser.sysmon_parser import SysMonParser
from app.serial.serial_worker import SSH_CAPTURE_TIMEOUT_SEC, SerialWorker


REMOTE = {
    "host": "pi-node-1.local",
    "user": "dut",
    "key_path": "/run/secrets/fleet_key",
    "port": 22,
    "device": "/dev/ttyUSB0",
    "baudrate": 115200,
    "is_mesh": True,
    "backhaul_iface": "ath16",
}


class _Ws:
    def emit_from_thread(self, event: dict) -> None:
        pass


class RemoteRegistryTests(unittest.TestCase):
    def test_remote_round_trip_keeps_secret_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(registry_mod, "DUTS_FILE", root / "duts.json"),
                mock.patch.object(registry_mod, "snapshot_file_for", lambda d: root / f"{d}.jsonl"),
            ):
                loop = asyncio.new_event_loop()
                self.addCleanup(loop.close)
                registry = DutRegistry(_Ws(), loop)
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                persisted = json.loads((root / "duts.json").read_text())
                self.assertEqual(persisted[0]["remote"]["key_path"], REMOTE["key_path"])

                restored = DutRegistry(_Ws(), loop)
                restored.load_persisted()
                self.assertEqual(restored.get("mesh1").remote, REMOTE)
                public = restored.describe()[0]["remote"]
                self.assertEqual(public["host"], REMOTE["host"])
                self.assertNotIn("key_path", public)
                self.assertNotIn("user", public)


class SshWorkerTests(unittest.TestCase):
    def test_system_ssh_uses_batch_timeout_and_keeps_host_checking(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        process.stderr.fileno.return_value = 12
        with mock.patch("app.serial.serial_worker.subprocess.Popen", return_value=process) as popen:
            with mock.patch("app.serial.serial_worker.select.select", return_value=([12], [], [])):
                with mock.patch("app.serial.serial_worker.os.read", return_value=b"__DUT_FLEET_READY__\n"):
                    worker._open_ssh(REMOTE, 115200)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertTrue(any(str(arg).startswith("ConnectTimeout=") for arg in command))
        self.assertFalse(any("StrictHostKeyChecking" in str(arg) for arg in command))
        self.assertIn("exec socat - /dev/ttyUSB0,b115200,raw,echo=0", command[-1])

    def test_close_terminates_and_reaps_ssh(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        worker._mode = "ssh"
        worker._ssh = process
        worker.close()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=1.0)

    def test_ssh_capture_uses_longer_named_timeout(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        process.stdin = mock.Mock()
        worker._mode = "ssh"
        worker._ssh = process
        with mock.patch.object(worker._capture_done, "wait", return_value=False) as wait:
            worker.capture_command("iwconfig", timeout=0.1)
        wait.assert_called_once_with(timeout=SSH_CAPTURE_TIMEOUT_SEC)

    def test_missing_socat_error_is_actionable(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        worker._ssh_stderr = [b"sh: socat: command not found\n"]
        self.assertIn("install socat", worker._ssh_error_detail())


if __name__ == "__main__":
    unittest.main()
