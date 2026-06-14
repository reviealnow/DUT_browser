"""Per-DUT runtime context + registry.

A0 of the multi-DUT effort. The five stateful components that make up a single
DUT's live pipeline — parser, serial worker, snapshot store, console buffer and
terminal fan-out — are bundled into a :class:`DutContext` keyed by a ``dut_id``.

For now the app registers exactly **one** DUT (``DEFAULT_DUT_ID``) so behaviour is
identical to the previous single-DUT wiring. The registry is the seam: adding a
second real DUT later is an additive ``register`` call plus a UI switcher, not a
rewrite of the components.

Shared (not per-DUT): the :class:`WebSocketManager` browser fan-out (one stream,
events tagged with ``dut_id``) and the file-based :class:`AnalyzerService`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.config import SNAPSHOT_FILE
from app.parser.sysmon_parser import SysMonParser
from app.serial.serial_worker import SerialWorker
from app.services.console_buffer import ConsoleBuffer
from app.services.snapshot_store import SnapshotStore
from app.websocket.terminal_manager import TerminalManager
from app.websocket.ws_manager import WebSocketManager

DEFAULT_DUT_ID = "default"


@dataclass
class DutContext:
    """The live runtime for one DUT."""

    dut_id: str
    parser: SysMonParser
    serial_worker: SerialWorker
    snapshot_store: SnapshotStore
    console_buffer: ConsoleBuffer
    terminal_manager: TerminalManager


class DutRegistry:
    """Holds the per-DUT contexts plus the shared, cross-DUT services."""

    def __init__(
        self,
        ws_manager: WebSocketManager,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.ws_manager = ws_manager
        self._loop = loop
        self._duts: dict[str, DutContext] = {}

    def create_dut(self, dut_id: str, snapshot_file: Path) -> DutContext:
        """Build and register one DUT's pipeline.

        Mirrors the original inline wiring from ``main.on_startup``: a per-DUT
        ``on_event`` closure feeds this DUT's snapshot store + console buffer and
        then broadcasts on the shared WebSocket, tagging each event with
        ``dut_id`` for forward-looking client routing.
        """
        snapshot_store = SnapshotStore(snapshot_file)
        console_buffer = ConsoleBuffer()
        terminal_manager = TerminalManager()
        terminal_manager.bind_loop(self._loop)

        ws_manager = self.ws_manager

        def on_event(event: dict) -> None:
            event["dut_id"] = dut_id
            snapshot_store.observe(event)
            console_buffer.observe(event)
            ws_manager.emit_from_thread(event)

        parser = SysMonParser(on_event=on_event)
        serial_worker = SerialWorker(parser)
        serial_worker.set_terminal_output(terminal_manager.emit_bytes_from_thread)

        context = DutContext(
            dut_id=dut_id,
            parser=parser,
            serial_worker=serial_worker,
            snapshot_store=snapshot_store,
            console_buffer=console_buffer,
            terminal_manager=terminal_manager,
        )
        self._duts[dut_id] = context
        return context

    def get(self, dut_id: str) -> DutContext:
        """Return a DUT context or raise ``KeyError`` for an unknown id."""
        return self._duts[dut_id]

    def ids(self) -> list[str]:
        return list(self._duts.keys())


def build_default_registry(
    ws_manager: WebSocketManager,
    loop: asyncio.AbstractEventLoop,
) -> DutRegistry:
    """Create a registry holding the single default DUT (uses the original
    ``SNAPSHOT_FILE`` so existing captured history keeps backfilling)."""
    registry = DutRegistry(ws_manager=ws_manager, loop=loop)
    registry.create_dut(DEFAULT_DUT_ID, snapshot_file=SNAPSHOT_FILE)
    return registry
