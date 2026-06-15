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


if __name__ == "__main__":
    unittest.main()
