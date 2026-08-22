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
import uuid
from dataclasses import dataclass, field
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


def _identity_token(material: list) -> str:
    """Hash the parts that make one console distinct from another.

    Hashed rather than joined: the value is for equality only, and no part of a
    DUT's configuration should be published in a field nobody meant as a
    disclosure.
    """
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]


def console_id(remote: dict | None) -> str | None:
    """An opaque name for the console a reading was taken on.

    Published so the frontend can decide whether a reading it is holding still
    describes this DUT **by the same rule the registry uses** — the two ends had
    diverged, each with its own list of fields, so an edit could clear the
    capture here and leave the browser re-serving it. A token means there is one
    rule, computed once, and the client compares rather than re-derives.
    """
    if remote is None:
        return None
    # `name`, not `field`: this module imports dataclasses.field, and a loop
    # variable shadowing it reads like a bug even where it is not one.
    return _identity_token([remote.get(name) for name in CONSOLE_IDENTITY_FIELDS])


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
    #
    # Named for the measurement, not for the transport: both commands run over
    # `capture_command`, which a cabled console answers exactly as an SSH one
    # does, so the fleet's own root — often the DUT on the desk — is measurable
    # too. Calling these `remote_*` said otherwise for as long as they existed.
    backhaul_uplink: dict | None = None
    backhaul_downlink: dict | None = None
    # "node" when a capture found this DUT's uplink; "root" when it found none
    # and something backs the claim that this DUT is nevertheless in the mesh —
    # an admin declaring a remote node `is_mesh`, or a peer's uplink naming one
    # of this DUT's VAPs. None otherwise, which is why `backhaul_captured`
    # exists: for a cabled DUT nobody declared, "no uplink" is not by itself a
    # root, and a standalone AP must not be shown as one.
    backhaul_role: str | None = None
    # Whether a capture ever parsed this DUT's VAPs. Separates "measured, and
    # there is no backhaul here" from "nobody has measured this yet" — the two
    # are otherwise identical (role, uplink and downlink all None) and the card
    # would have to call both of them "Not captured".
    backhaul_captured: bool = False
    # The console the stored capture was actually read from — see console_token.
    # Held rather than re-derived, because the transport a reading came from is
    # not recoverable from the configuration afterwards.
    backhaul_console: str | None = None
    # This registration, as distinct from this DUT id. An id is re-usable: remove
    # `lab2` and register another device as `lab2` on the same adapter, and every
    # part of the console's identity is byte-identical — so a browser still
    # holding the old device's capture would go on showing it as this one's. The
    # transport cannot tell those apart, and nothing else about a fresh context
    # is unique, so the identity carries one value that is.
    registration: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


def console_token(ctx: DutContext, mode: str | None = None) -> str:
    """Which console a reading on this DUT came from, or would come from now.

    A remote node's console is its SSH configuration (`console_id`). A cabled
    DUT's is the serial device it was last opened on: moving the cable to
    another device under the same DUT id is the local equivalent of re-pointing
    a node at another Pi, and the port is the only part of it the app can see.

    **The transport is not a property of the configuration.** A DUT registered
    with an SSH console can be opened on a cable at this desk — `/api/serial/open`
    takes any DUT id and `capture_command` writes to whichever transport the
    worker actually holds — and then the reading came from the cable, however
    the DUT is configured. `mode` is the worker's transport where the caller
    knows it; without it, the configuration is the best guess available.

    Every token carries `ctx.registration`, so two devices registered under one
    id in turn never share one however identical their consoles are.
    """
    over_ssh = ctx.remote is not None if mode is None else mode == "ssh"
    if over_ssh and ctx.remote is not None:
        return _identity_token([ctx.registration, "ssh", console_id(ctx.remote)])
    port = ctx.last_serial["port"] if ctx.last_serial else None
    return _identity_token([ctx.registration, "local", port])


def _forget_backhaul(ctx: DutContext) -> None:
    """Drop a capture that no longer describes the console behind this DUT.

    A capture describes the console it was read from, so anything that changes
    which console that is revokes it. Kept, the old device's role, uplink and
    children go out through /api/duts as the new device's current state, where
    nothing downstream can tell them from a fresh measurement. Dropping them
    says "not captured", which is true.
    """
    ctx.backhaul_uplink = None
    ctx.backhaul_downlink = None
    ctx.backhaul_role = None
    ctx.backhaul_captured = False
    ctx.backhaul_console = None


def _forget_if_another_console(ctx: DutContext, opening: str) -> None:
    """Revoke a stored capture when the console being opened is not its own.

    One rule, two callers — a serial open and an SSH connect — because a DUT can
    be opened either way regardless of how it is configured, and each of those
    opens is the moment the card starts describing a different device.
    """
    if ctx.backhaul_console is not None and ctx.backhaul_console != opening:
        _forget_backhaul(ctx)


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
            # The cabled half of the rule `configure_remote` applies to a node:
            # a different serial device is a different console, so whatever was
            # captured on the old one stops describing this DUT. This fires for
            # a registered node too, and must: opening one on a cable at this
            # desk is a supported thing to do, and the reading it is holding
            # came from a Pi. Baud is not part of the comparison, for the same
            # reason it is absent from CONSOLE_IDENTITY_FIELDS — it changes how
            # to talk to a console, not which one it is.
            # Recorded first so the token comes from `console_token` rather than
            # being assembled here: a second copy of that rule is a second
            # answer to it, and this one had already drifted out of step with
            # the first the moment the token grew a part.
            ctx.last_serial = cleaned
            _forget_if_another_console(ctx, console_token(ctx, "serial"))
            self._save_locked()

    def _holding(self, dut_id: str, console: str, mode: str | None) -> DutContext | None:
        """The context, iff `console` is still the console behind this DUT.

        Call holding the lock. Separate from the writes it guards only so the
        two of them can share it — the whole point is that the check and the
        write are one operation.
        """
        ctx = self._duts.get(dut_id)
        if ctx is None or console_token(ctx, mode) != console:
            return None
        return ctx

    def store_backhaul_reading(
        self, dut_id: str, console: str, mode: str | None, uplink: dict | None, role: str | None
    ) -> bool:
        """Commit a capture's uplink and role. False when the console moved.

        Under the registry's lock, with every console-open hook, because a
        capture takes seconds and a console can be opened in the middle of one.
        Testing the console and then writing outside the lock is not a guard: a
        console opened between the two revokes the old reading and this write
        puts it straight back, still labelled with the console that has gone.
        """
        with self._lock:
            ctx = self._holding(dut_id, console, mode)
            if ctx is None:
                return False
            ctx.backhaul_uplink = uplink
            ctx.backhaul_role = role
            ctx.backhaul_captured = True
            ctx.backhaul_console = console
            return True

    def store_backhaul_downlink(
        self, dut_id: str, console: str, mode: str | None, downlink: dict | None
    ) -> bool:
        """Commit a capture's child list. False when the console moved."""
        with self._lock:
            ctx = self._holding(dut_id, console, mode)
            if ctx is None:
                return False
            ctx.backhaul_downlink = downlink
            return True

    def note_console_open(self, dut_id: str, mode: str) -> None:
        """A console was opened by a path with nothing else to persist.

        The serial path already goes through `record_serial_params`, which has a
        port to remember as well. An SSH connect has nothing to store and still
        changes which console a held reading would be shown against — a node
        captured over a cable and then reconnected to its Pi must not serve the
        cable's numbers as the Pi's.
        """
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None:
                return
            _forget_if_another_console(ctx, console_token(ctx, mode))

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
                # Re-pointing an id at another Pi is a supported edit (the
                # Settings card calls it "Update node"), so the reading it was
                # holding is somebody else's. A credential-only edit is not a
                # different console, and neither is re-registering the same
                # values, so both keep the reading rather than spending a
                # serial RPC to learn the same thing again.
                _forget_backhaul(ctx)
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
                    },
                    # Published for every DUT, not inside `remote`, because the
                    # measurement does not belong to the transport: the two
                    # commands behind it run over a cabled console exactly as
                    # they do over SSH. Nested under `remote`, a locally cabled
                    # mesh root could be captured and the reading would have
                    # nowhere to be served from.
                    "backhaul": {
                        # Whether asking is meaningful at all. Only an admin
                        # declaring a remote node standalone can answer "no" —
                        # nothing declares a cabled DUT either way, so the
                        # capture is offered and its result does the talking.
                        "applicable": True if ctx.remote is None else ctx.remote["is_mesh"],
                        "captured": ctx.backhaul_captured,
                        # The client holds captures of its own and needs the
                        # registry's rule for when they stop applying, not a
                        # second guess at it. The console a reading came from,
                        # once there is one — which is not always the console
                        # the configuration implies.
                        "console_id": ctx.backhaul_console or console_token(ctx),
                        "role": ctx.backhaul_role,
                        "uplink": ctx.backhaul_uplink,
                        "downlink": ctx.backhaul_downlink,
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
