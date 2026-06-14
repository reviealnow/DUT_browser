from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

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
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self.ws = _StubWsManager()

    def _registry(self) -> DutRegistry:
        return DutRegistry(ws_manager=self.ws, loop=self._loop)

    def _snap(self, name: str) -> Path:
        return Path(self._dir.name) / f"{name}.jsonl"

    def test_create_and_get_returns_wired_context(self) -> None:
        reg = self._registry()
        ctx = reg.create_dut(DEFAULT_DUT_ID, snapshot_file=self._snap("default"))
        self.assertIsInstance(ctx, DutContext)
        self.assertEqual(ctx.dut_id, DEFAULT_DUT_ID)
        for attr in ("parser", "serial_worker", "snapshot_store", "console_buffer", "terminal_manager"):
            self.assertIsNotNone(getattr(ctx, attr))
        self.assertIs(reg.get(DEFAULT_DUT_ID), ctx)
        self.assertEqual(reg.ids(), [DEFAULT_DUT_ID])

    def test_unknown_dut_raises(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, snapshot_file=self._snap("default"))
        with self.assertRaises(KeyError):
            reg.get("bogus")

    def test_on_event_tags_dut_id_and_routes_to_own_buffers(self) -> None:
        reg = self._registry()
        ctx = reg.create_dut("dutA", snapshot_file=self._snap("dutA"))
        # The parser's on_event is the per-DUT closure built by create_dut.
        ctx.parser.on_event({"type": "console_line", "text": "hello"})
        self.assertEqual(ctx.console_buffer.recent(10), ["hello"])
        self.assertTrue(self.ws.events)
        self.assertEqual(self.ws.events[-1]["dut_id"], "dutA")


if __name__ == "__main__":
    unittest.main()
