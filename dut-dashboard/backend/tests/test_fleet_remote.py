from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

import app.dut.registry as registry_mod
from app.api.fleet_api import RemoteNodeBody, capture_rssi, configure_node
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


@contextlib.contextmanager
def _registries_under(root: Path):
    """Yield a factory for registries whose duts.json and snapshots live in `root`."""
    loop = asyncio.new_event_loop()
    with (
        mock.patch.object(registry_mod, "DUTS_FILE", root / "duts.json"),
        mock.patch.object(registry_mod, "snapshot_file_for", lambda d: root / f"{d}.jsonl"),
    ):
        try:
            yield lambda: DutRegistry(_Ws(), loop)
        finally:
            loop.close()


class RemoteRegistryTests(unittest.TestCase):
    def test_remote_round_trip_keeps_secret_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _registries_under(root) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                persisted = json.loads((root / "duts.json").read_text())
                self.assertEqual(persisted[0]["remote"]["key_path"], REMOTE["key_path"])

                restored = make_registry()
                restored.load_persisted()
                self.assertEqual(restored.get("mesh1").remote, REMOTE)
                public = restored.describe()[0]["remote"]
                self.assertEqual(public["host"], REMOTE["host"])
                self.assertNotIn("key_path", public)
                self.assertNotIn("user", public)

    def test_load_persisted_merges_and_never_clears_a_live_remote(self) -> None:
        """A saved entry from before the node had an SSH console must not wipe it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _registries_under(root) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)

                (root / "duts.json").write_text(
                    json.dumps([{"id": "mesh1", "label": "Mesh 1"}]), encoding="utf-8"
                )
                registry.load_persisted()

                self.assertEqual(registry.get("mesh1").remote, REMOTE)


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


class RemoteNodeApiTests(unittest.TestCase):
    def test_blank_key_path_is_refused_by_the_body_model(self) -> None:
        with self.assertRaises(ValidationError):
            RemoteNodeBody(id="mesh1", host="pi-node-1.local", user="dut", key_path="   ")

    def test_a_rejected_configuration_leaves_no_dut_behind(self) -> None:
        """The DUT exists only to carry the config; a 400 must not strand one."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _registries_under(root) as make_registry:
                registry = make_registry()
                request = mock.Mock()
                request.app.state.dut_registry = registry
                body = RemoteNodeBody(
                    id="mesh1",
                    host=REMOTE["host"],
                    user=REMOTE["user"],
                    key_path=REMOTE["key_path"],
                    backhaul_iface=REMOTE["backhaul_iface"],
                )
                with mock.patch.object(
                    registry, "configure_remote", side_effect=ValueError("Invalid remote")
                ):
                    with self.assertRaises(HTTPException) as caught:
                        configure_node(body, request, _admin={})

                self.assertEqual(caught.exception.status_code, 400)
                self.assertNotIn("mesh1", registry.ids())
                self.assertEqual(json.loads((root / "duts.json").read_text()), [])


class RssiCaptureTests(unittest.TestCase):
    def test_capture_reuses_wifi_client_parser_and_scopes_result(self) -> None:
        context = mock.Mock()
        context.remote = REMOTE.copy()
        context.serial_worker.capture_command.return_value = (
            "00:11:22:33:44:55 1 36 866M 780M -63 00:01:02 IEEE80211_MODE_11AXA_HE80 2 2\n"
        )
        registry = mock.Mock()
        registry.get.return_value = context
        request = mock.Mock()
        request.app.state.dut_registry = registry
        result = capture_rssi("mesh1", request)
        self.assertEqual(result, {"dut": "mesh1", "applicable": True, "rssi": -63, "band": "mid"})
        context.serial_worker.capture_command.assert_called_once_with("wlanconfig ath16 list")

    def test_standalone_ap_is_not_applicable_without_capture(self) -> None:
        context = mock.Mock()
        context.remote = {**REMOTE, "is_mesh": False, "backhaul_iface": None}
        request = mock.Mock()
        request.app.state.dut_registry.get.return_value = context
        result = capture_rssi("ap1", request)
        self.assertEqual(result["applicable"], False)
        context.serial_worker.capture_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
