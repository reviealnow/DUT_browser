from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.dut.registry as registry_mod
from app.dut.registry import DEFAULT_DUT_ID, DutContext, DutRegistry


class _StubWsManager:
    """Records the events the per-DUT on_event closure broadcasts."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit_from_thread(self, event: dict) -> None:
        self.events.append(event)


class DutRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._tmp = Path(self._dir.name)
        # Keep all snapshot files + the persisted DUTs list inside the tempdir.
        self._duts_file = self._tmp / "duts.json"
        patches = [
            mock.patch.object(registry_mod, "DUTS_FILE", self._duts_file),
            mock.patch.object(registry_mod, "snapshot_file_for", lambda d: self._tmp / f"snap-{d}.jsonl"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self.ws = _StubWsManager()

    def _registry(self) -> DutRegistry:
        return DutRegistry(ws_manager=self.ws, loop=self._loop)

    def test_create_and_get_returns_wired_context(self) -> None:
        reg = self._registry()
        ctx = reg.create_dut(DEFAULT_DUT_ID, label="Default")
        self.assertIsInstance(ctx, DutContext)
        self.assertEqual(ctx.dut_id, DEFAULT_DUT_ID)
        self.assertEqual(ctx.label, "Default")
        for attr in ("parser", "serial_worker", "snapshot_store", "console_buffer", "terminal_manager"):
            self.assertIsNotNone(getattr(ctx, attr))
        self.assertIs(reg.get(DEFAULT_DUT_ID), ctx)
        self.assertEqual(reg.ids(), [DEFAULT_DUT_ID])

    def test_unknown_dut_raises(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        with self.assertRaises(KeyError):
            reg.get("bogus")

    def test_on_event_tags_dut_id_and_routes_to_own_buffers(self) -> None:
        reg = self._registry()
        ctx = reg.create_dut("duta")
        ctx.parser.on_event({"type": "console_line", "text": "hello"})
        self.assertEqual(ctx.console_buffer.recent(10), ["hello"])
        self.assertTrue(self.ws.events)
        self.assertEqual(self.ws.events[-1]["dut_id"], "duta")

    def test_register_lists_and_removes(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.register_dut("lab2", label="Replay DUT")
        ids = {d["id"]: d for d in reg.describe()}
        self.assertEqual(set(ids), {DEFAULT_DUT_ID, "lab2"})
        self.assertEqual(ids["lab2"]["label"], "Replay DUT")
        self.assertTrue(ids["lab2"]["removable"])
        self.assertFalse(ids[DEFAULT_DUT_ID]["removable"])
        reg.remove_dut("lab2")
        self.assertEqual(reg.ids(), [DEFAULT_DUT_ID])

    def test_register_rejects_duplicate(self) -> None:
        reg = self._registry()
        reg.register_dut("lab2")
        with self.assertRaises(KeyError):
            reg.register_dut("lab2")

    def test_register_rejects_bad_id(self) -> None:
        reg = self._registry()
        for bad in ("Lab2", "ab/cd", "-x", "", "a" * 40):
            with self.assertRaises(ValueError):
                reg.register_dut(bad)

    def test_cannot_remove_default(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        with self.assertRaises(ValueError):
            reg.remove_dut(DEFAULT_DUT_ID)

    def test_persistence_round_trip(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.register_dut("lab2", label="Replay DUT")
        # A fresh registry that loads the persisted file gets lab2 back (not default).
        reg2 = self._registry()
        reg2.create_dut(DEFAULT_DUT_ID, label="Default")
        reg2.load_persisted()
        self.assertEqual(set(reg2.ids()), {DEFAULT_DUT_ID, "lab2"})
        self.assertEqual(reg2.get("lab2").label, "Replay DUT")

    # --- Phase 67: remembered serial params ------------------------------------

    def test_record_serial_params_exposed_in_describe(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        self.assertIsNone(reg.describe()[0]["last_serial"])
        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.usbserial", 115200)
        self.assertEqual(
            reg.describe()[0]["last_serial"], {"port": "/dev/cu.usbserial", "baudrate": 115200}
        )

    def test_record_serial_params_unknown_dut_is_noop(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        reg.record_serial_params("ghost", "/dev/cu.x", 115200)  # must not raise
        self.assertFalse(self._duts_file.exists())

    def test_record_serial_params_rejects_malformed(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        for port, baud in (("", 115200), ("/dev/x", 0), ("/dev/x", -1)):
            reg.record_serial_params(DEFAULT_DUT_ID, port, baud)
        self.assertIsNone(reg.get(DEFAULT_DUT_ID).last_serial)

    def test_last_serial_survives_restart_default_and_registered(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.register_dut("lab2", label="Replay DUT")
        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.default", 115200)
        reg.record_serial_params("lab2", "/dev/cu.lab2", 9600)
        # Fresh registry: default is re-created by build, lab2 restored from file;
        # both get their remembered params back.
        reg2 = self._registry()
        reg2.create_dut(DEFAULT_DUT_ID, label="Default")
        reg2.load_persisted()
        self.assertEqual(reg2.get(DEFAULT_DUT_ID).last_serial, {"port": "/dev/cu.default", "baudrate": 115200})
        self.assertEqual(reg2.get("lab2").last_serial, {"port": "/dev/cu.lab2", "baudrate": 9600})

    def test_legacy_file_without_last_serial_loads(self) -> None:
        # A duts.json written by a pre-P67 build has no last_serial key.
        self._duts_file.write_text('[{"id": "lab2", "label": "Legacy DUT"}]', encoding="utf-8")
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.load_persisted()
        self.assertEqual(reg.get("lab2").label, "Legacy DUT")
        self.assertIsNone(reg.get("lab2").last_serial)

    def test_malformed_last_serial_in_file_is_ignored(self) -> None:
        self._duts_file.write_text(
            '[{"id": "lab2", "label": "L", "last_serial": {"port": "", "baudrate": "fast"}}]',
            encoding="utf-8",
        )
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.load_persisted()
        self.assertIsNone(reg.get("lab2").last_serial)


class SerialOpenRecordingTests(unittest.TestCase):
    """The /api/serial/open handler records params on serial-mode opens only."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        tmp = Path(self._dir.name)
        patches = [
            mock.patch.object(registry_mod, "DUTS_FILE", tmp / "duts.json"),
            mock.patch.object(registry_mod, "snapshot_file_for", lambda d: tmp / f"snap-{d}.jsonl"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self.reg = DutRegistry(ws_manager=_StubWsManager(), loop=self._loop)
        ctx = self.reg.create_dut(DEFAULT_DUT_ID, label="Default")
        # Stub the worker open so no real serial/replay file is needed.
        ctx.serial_worker.open = lambda **kw: None  # type: ignore[assignment]
        self.request = mock.Mock()
        self.request.app.state.dut_registry = self.reg

    def _open(self, **kw):
        from app.api.serial_api import SerialOpenRequest, open_serial

        body = SerialOpenRequest(**kw)
        return open_serial(body, self.request, dut=DEFAULT_DUT_ID)

    def test_serial_open_records_params(self) -> None:
        self._open(mode="serial", port="/dev/cu.usbserial", baudrate=115200)
        self.assertEqual(
            self.reg.get(DEFAULT_DUT_ID).last_serial,
            {"port": "/dev/cu.usbserial", "baudrate": 115200},
        )

    def test_replay_open_does_not_record(self) -> None:
        self._open(mode="replay", replay_path="/tmp/x.log")
        self.assertIsNone(self.reg.get(DEFAULT_DUT_ID).last_serial)

    def test_serial_open_without_port_does_not_record(self) -> None:
        self._open(mode="serial", port="", baudrate=115200)
        self.assertIsNone(self.reg.get(DEFAULT_DUT_ID).last_serial)


if __name__ == "__main__":
    unittest.main()
