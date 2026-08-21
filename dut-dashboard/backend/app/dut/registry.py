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
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import DUTS_FILE, snapshot_file_for
from app.parser.sysmon_parser import SysMonParser
from app.serial.serial_worker import SerialWorker
from app.services.console_buffer import ConsoleBuffer
from app.services.snapshot_store import SnapshotStore
from app.websocket.terminal_manager import TerminalManager
from app.websocket.ws_manager import WebSocketManager

DEFAULT_DUT_ID = "default"
MAX_DUTS = 16
_DUT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Every part of a remote node's configuration ends up as an argument to ssh or
# inside a command string a shell runs — on the Pi for the console, on the DUT
# for the RSSI capture. Defined here because this is where the persisted entry
# is cleaned; fleet_api imports them so the API body and the file on disk cannot
# drift into disagreeing about what is acceptable.
#
# The leading character of a host or user is deliberately alphanumeric: a value
# starting with "-" would reach ssh as an option rather than a name.
REMOTE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@%+-]*$")
REMOTE_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9._/-]+$")
REMOTE_IFACE_RE = re.compile(r"^ath\d+$")
#: What makes a reading belong to one console rather than another. Everything
#: here changes what was measured or how it must be read: which Pi, which port,
#: which serial device, whether the DUT is meshed at all, and which interface a
#: root falls back to for its backhaul.
#:
#: `user`, `key_path` and `baudrate` are deliberately NOT here. They decide how
#: to log in and how to talk to the console, not what is on the other end of it:
#: rotating a key or fixing a typo in the login name leaves a captured RSSI as
#: true as it was, and dropping it there costs a synchronous serial RPC to learn
#: the same thing again.
CONSOLE_IDENTITY_FIELDS = ("host", "port", "device", "is_mesh", "backhaul_iface")

REMOTE_PORT_MIN = 1
REMOTE_PORT_MAX = 65535


def _clean_last_serial(value: object) -> dict | None:
    """Validate a persisted/recorded ``last_serial`` payload; ``None`` if malformed.

    Guards the load path against a hand-edited or stale ``duts.json`` (bad type,
    empty port, non-int baud) so a bad entry silently disables Connect rather
    than crashing the registry.
    """
    if not isinstance(value, dict):
        return None
    port = value.get("port")
    baudrate = value.get("baudrate")
    if not isinstance(port, str) or not port:
        return None
    # bool is an int subclass — reject it explicitly.
    if not isinstance(baudrate, int) or isinstance(baudrate, bool) or baudrate <= 0:
        return None
    return {"port": port, "baudrate": baudrate}


def console_id(remote: dict | None) -> str | None:
    """An opaque name for the console a reading was taken on.

    Published so the frontend can decide whether a reading it is holding still
    describes this DUT **by the same rule the registry uses** — the two ends had
    diverged, each with its own list of fields, so an edit could clear the
    capture here and leave the browser re-serving it. A token means there is one
    rule, computed once, and the client compares rather than re-derives.

    Hashed rather than a joined string: the value is for equality only, and no
    part of a DUT's configuration should be published in a field nobody meant as
    a disclosure.
    """
    if remote is None:
        return None
    material = json.dumps(
        [remote.get(field) for field in CONSOLE_IDENTITY_FIELDS],
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _clean_remote(value: object) -> dict | None:
    """Validate persisted SSH console configuration without inventing a store."""
    if not isinstance(value, dict):
        return None
    required = ("host", "user", "key_path", "device")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        return None
    host = value["host"].strip()
    user = value["user"].strip()
    if not REMOTE_TOKEN_RE.fullmatch(host) or not REMOTE_TOKEN_RE.fullmatch(user):
        return None
    port = value.get("port", 22)
    baudrate = value.get("baudrate", 115200)
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in (port, baudrate)):
        return None
    if not REMOTE_PORT_MIN <= port <= REMOTE_PORT_MAX:
        return None
    device = value["device"].strip()
    if not REMOTE_DEVICE_RE.fullmatch(device) or ".." in device:
        return None
    is_mesh = value.get("is_mesh", True)
    backhaul_iface = value.get("backhaul_iface")
    if not isinstance(is_mesh, bool):
        return None
    if isinstance(backhaul_iface, str) and not REMOTE_IFACE_RE.fullmatch(backhaul_iface.strip()):
        return None
    if is_mesh and not isinstance(backhaul_iface, str):
        return None
    return {
        "host": host,
        "user": user,
        "key_path": value["key_path"].strip(),
        "port": port,
        "device": device,
        "baudrate": baudrate,
        "is_mesh": is_mesh,
        "backhaul_iface": backhaul_iface.strip() if isinstance(backhaul_iface, str) else None,
    }


@dataclass
class DutContext:
    """The live runtime for one DUT."""

    dut_id: str
    label: str
    parser: SysMonParser
    serial_worker: SerialWorker
    snapshot_store: SnapshotStore
    console_buffer: ConsoleBuffer
    terminal_manager: TerminalManager
    # Last successful serial-open params ({"port", "baudrate"}), remembered so the
    # Fleet view can offer one-click Connect. None until a serial-mode open.
    last_serial: dict | None = None
    # Origin of this DUT's management API (e.g. "https://192.168.30.50"), set by
    # an admin. Empty means the firmware upgrade refuses rather than guessing a
    # host to PUT an image at.
    mgmt_url: str = ""
    # Server-side SSH configuration. describe() deliberately exposes only the
    # Pi identity needed by FleetStrip, never the user or private-key path.
    remote: dict | None = None
    # The two halves of a mesh backhaul are different measurements and are kept
    # apart on purpose: uplink is how well this node hears its parent (iwconfig
    # on the Managed VAP), downlink is how well it hears each child (wlanconfig
    # on the Master VAP). Collapsing them into one "RSSI" makes the number on
    # the card unreadable — you cannot tell which direction it describes.
    remote_uplink: dict | None = None
    remote_downlink: dict | None = None
    # "root" once a capture has parsed this DUT's VAPs and found no uplink,
    # "node" when it found one, None while nothing has been captured. Without
    # it the card cannot tell a root from a DUT nobody has measured yet.
    remote_role: str | None = None


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
        self._lock = threading.Lock()

    def create_dut(self, dut_id: str, label: str | None = None) -> DutContext:
        """Build and register one DUT's pipeline.

        Mirrors the original inline wiring from ``main.on_startup``: a per-DUT
        ``on_event`` closure feeds this DUT's snapshot store + console buffer and
        then broadcasts on the shared WebSocket, tagging each event with
        ``dut_id`` for forward-looking client routing.
        """
        snapshot_store = SnapshotStore(snapshot_file_for(dut_id))
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
        # Non-default DUTs weave their id into the session-log filename so
        # concurrent DUTs don't collide; the default keeps the original naming.
        worker_name = "" if dut_id == DEFAULT_DUT_ID else dut_id
        serial_worker = SerialWorker(parser, name=worker_name)
        serial_worker.set_terminal_output(terminal_manager.emit_bytes_from_thread)

        def on_serial_disconnected(detail: str) -> None:
            """Push the drop to the browser so "Connected" stops lying.

            Emitted straight on the shared /ws (same shape as survey_progress and
            firmware_progress) rather than through the parser's on_event, because
            this is worker state, not DUT telemetry — the snapshot store and
            console buffer have nothing to do with it.
            """
            ws_manager.emit_from_thread(
                {"type": "serial_disconnected", "dut_id": dut_id, "detail": detail}
            )

        serial_worker.set_disconnect_handler(on_serial_disconnected)

        context = DutContext(
            dut_id=dut_id,
            label=label or dut_id,
            parser=parser,
            serial_worker=serial_worker,
            snapshot_store=snapshot_store,
            console_buffer=console_buffer,
            terminal_manager=terminal_manager,
        )
        self._duts[dut_id] = context
        return context

    def register_dut(self, dut_id: str, label: str | None = None) -> DutContext:
        """Add a new DUT at runtime (validated, deduped, capped) and persist it."""
        if not _DUT_ID_RE.match(dut_id):
            raise ValueError("DUT id must match ^[a-z0-9][a-z0-9_-]{0,31}$")
        with self._lock:
            if dut_id in self._duts:
                raise KeyError(f"DUT already exists: {dut_id}")
            if len(self._duts) >= MAX_DUTS:
                raise ValueError(f"DUT limit reached ({MAX_DUTS})")
            context = self.create_dut(dut_id, label=label)
            self._save_locked()
        return context

    def record_serial_params(self, dut_id: str, port: str, baudrate: int) -> None:
        """Remember a DUT's last successful serial-open params and persist them.

        Called on a serial-mode open only (replay skipped). Persistence is
        best-effort — a lost write just leaves Connect disabled next time.
        """
        cleaned = _clean_last_serial({"port": port, "baudrate": baudrate})
        if cleaned is None:
            return
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None:
                return
            ctx.last_serial = cleaned
            self._save_locked()

    def record_mgmt_url(self, dut_id: str, mgmt_url: str) -> None:
        """Set a DUT's management API origin and persist it. Empty clears it."""
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None:
                return
            ctx.mgmt_url = mgmt_url
            self._save_locked()

    def configure_remote(self, dut_id: str, remote: dict) -> DutContext:
        cleaned = _clean_remote(remote)
        if cleaned is None:
            raise ValueError("Invalid remote SSH console configuration")
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None:
                raise KeyError(f"Unknown DUT: {dut_id}")
            if console_id(ctx.remote) != console_id(cleaned):
                # A capture describes the console it was read from, and this
                # call has just changed which console that is. Re-pointing an
                # id at another Pi is a supported edit, and keeping the reading
                # served the new device the old device's role, uplink and
                # children through /api/duts — indistinguishable, at the card,
                # from a fresh measurement. Dropping it says "not captured",
                # which is true. A credential-only edit is not a different
                # console, and neither is re-registering the same values, so
                # both keep the reading rather than spending a serial RPC to
                # learn the same thing again.
                ctx.remote_uplink = None
                ctx.remote_downlink = None
                ctx.remote_role = None
            ctx.remote = cleaned
            self._save_locked()
            return ctx

    def remove_dut(self, dut_id: str) -> None:
        """Stop and drop a DUT (frees its serial port). The default DUT is fixed."""
        if dut_id == DEFAULT_DUT_ID:
            raise ValueError("Cannot remove the default DUT")
        with self._lock:
            context = self._duts.get(dut_id)
            if context is None:
                raise KeyError(f"Unknown DUT: {dut_id}")
            context.serial_worker.close()
            del self._duts[dut_id]
            self._save_locked()

    def get(self, dut_id: str) -> DutContext:
        """Return a DUT context or raise ``KeyError`` for an unknown id."""
        return self._duts[dut_id]

    def ids(self) -> list[str]:
        return list(self._duts.keys())

    def describe(self) -> list[dict]:
        """Summary of every DUT for the list endpoint (id, label, serial status)."""
        out: list[dict] = []
        for ctx in self._duts.values():
            worker = ctx.serial_worker
            out.append(
                {
                    "id": ctx.dut_id,
                    "label": ctx.label,
                    "mode": worker.mode,
                    "serial_open": worker.is_open,
                    "log_path": worker.current_log_path,
                    "removable": ctx.dut_id != DEFAULT_DUT_ID,
                    "last_serial": ctx.last_serial,
                    "mgmt_url": ctx.mgmt_url,
                    "remote": None if ctx.remote is None else {
                        "host": ctx.remote["host"],
                        "port": ctx.remote["port"],
                        "device": ctx.remote["device"],
                        "is_mesh": ctx.remote["is_mesh"],
                        # The client holds captures of its own and needs the
                        # registry's rule for when they stop applying, not a
                        # second guess at it.
                        "console_id": console_id(ctx.remote),
                        "role": ctx.remote_role,
                        "uplink": ctx.remote_uplink,
                        "downlink": ctx.remote_downlink,
                    },
                }
            )
        return out

    @staticmethod
    def _entry_for(ctx: DutContext) -> dict:
        entry: dict = {"id": ctx.dut_id, "label": ctx.label}
        if ctx.last_serial is not None:
            entry["last_serial"] = ctx.last_serial
        if ctx.mgmt_url:
            entry["mgmt_url"] = ctx.mgmt_url
        if ctx.remote is not None:
            entry["remote"] = ctx.remote
        return entry

    def _save_locked(self) -> None:
        """Persist the DUT list (call holding ``self._lock``).

        Non-default DUTs always persist so they survive a restart. The default
        DUT is re-created by ``build_default_registry`` on boot, so it is only
        written once it has remembered serial params — never a bare entry that a
        reload would try to re-create.
        """
        entries = [
            self._entry_for(ctx)
            for ctx in self._duts.values()
            if ctx.dut_id != DEFAULT_DUT_ID or ctx.last_serial is not None or ctx.mgmt_url or ctx.remote
        ]
        try:
            DUTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            DUTS_FILE.write_text(json.dumps(entries), encoding="utf-8")
        except OSError:
            pass  # persistence is best-effort; never break a register/remove

    def load_persisted(self) -> None:
        """Re-create DUTs saved in ``DUTS_FILE`` (best-effort, skips malformed).

        An id that already exists (the default DUT, created before this runs) is
        not re-created — only its remembered ``last_serial`` is merged in.
        """
        try:
            entries = json.loads(DUTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(entries, list):
            return
        for entry in entries:
            try:
                dut_id = entry["id"]
            except (TypeError, KeyError):
                continue
            if not _DUT_ID_RE.match(str(dut_id)):
                continue
            last_serial = _clean_last_serial(entry.get("last_serial")) if isinstance(entry, dict) else None
            remote = _clean_remote(entry.get("remote")) if isinstance(entry, dict) else None
            existing = self._duts.get(dut_id)
            if existing is not None:
                if last_serial is not None:
                    existing.last_serial = last_serial
                mgmt = entry.get("mgmt_url") if isinstance(entry, dict) else None
                if isinstance(mgmt, str) and mgmt:
                    existing.mgmt_url = mgmt
                if remote is not None:
                    # Merge, never clear: an entry written before the DUT was
                    # given an SSH console — or one this version rejects — must
                    # not silently strip a live configuration, which the next
                    # save would then erase from disk too.
                    existing.remote = remote
                continue
            ctx = self.create_dut(dut_id, label=entry.get("label"))
            ctx.last_serial = last_serial
            ctx.remote = remote


def build_default_registry(
    ws_manager: WebSocketManager,
    loop: asyncio.AbstractEventLoop,
) -> DutRegistry:
    """Create a registry holding the single default DUT (uses the original
    ``SNAPSHOT_FILE`` so existing captured history keeps backfilling)."""
    registry = DutRegistry(ws_manager=ws_manager, loop=loop)
    registry.create_dut(DEFAULT_DUT_ID, label="Default")
    registry.load_persisted()
    return registry
