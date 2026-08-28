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
from app.services import dut_model
from app.services.console_buffer import ConsoleBuffer
from app.services.snapshot_store import SnapshotStore
from app.websocket.terminal_manager import TerminalManager
from app.websocket.ws_manager import WebSocketManager

DEFAULT_DUT_ID = "default"
# What the built-in DUT is called before anybody renames it. Named rather than
# inlined because `_save_locked` has to tell "never renamed" from "renamed", and
# comparing against a literal in two places is how those two drift apart.
DEFAULT_DUT_LABEL = "Default"
MAX_DUTS = 16
_DUT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# A label is a display name, not an identifier: it may hold spaces, case and
# punctuation. Bounded, and no control characters -- it ends up in a log
# filename and in the fleet UI.
MAX_LABEL_LEN = 48

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


def clean_label(value: object) -> str | None:
    """A display name, or ``None`` if the input cannot be one.

    Shared by the rename path and the load path so a hand-edited ``duts.json``
    cannot put anything on screen that the API would have refused. Control
    characters are dropped rather than escaped: the label reaches a log
    filename, and a newline or a NUL there is somebody else's bug later.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned or len(cleaned) > MAX_LABEL_LEN:
        return None
    return cleaned


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
    # Which AP6 this is ("AP6_420E"), read from the console prompt as it streams
    # past. It decides how many VAPs sit in each band, and therefore which band
    # an athN belongs to when the output states no frequency -- eight per band
    # on the 420 family, sixteen on the 840. None until a prompt has been seen;
    # `dut_model.vaps_per_band(None)` then answers sixteen, which is what the
    # code assumed for every model before this field existed.
    model: str | None = None
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
    # What the DUT itself said about its mesh, from the console probe. Kept
    # ALONGSIDE `remote["is_mesh"]` and never over it: that field is an admin's
    # declaration and this is a measurement, and the interesting case is exactly
    # when they disagree ("declared standalone, reports two mesh members").
    # Folding one into the other would delete the only signal that says so.
    #
    # Dropped with the rest of a capture when the console changes: it describes
    # the device that was behind this id, not the id.
    mesh_probe: dict | None = None
    # Which physical unit is behind this console -- the device's own hostname,
    # ``AP6420E-PB1005QPCFVFMA8``. `model` cannot do this job: two 420Es share a
    # model and a prompt, so a reading taken on one survives the swap to the
    # other looking exactly like a fresh one.
    #
    # Dropped with the rest of a capture when the console changes, for the same
    # reason they are: it describes the device that was behind this id, and
    # after a console change nobody has asked the new one who it is. Unknown is
    # the honest state, and it must never be read as "a different device".
    device_id: str | None = None
    # The console the identity above was read from -- its own anchor, not the
    # capture's. Sharing `backhaul_console` looked equivalent and is not: that
    # field is None until something has been captured, so an identity learned on
    # a DUT nobody has measured would have survived a recabling with nothing to
    # revoke it, and then answered the next identify as a swap that never
    # happened. Two facts with different lifetimes need two anchors.
    device_console: str | None = None
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
    if mode == "replay":
        # A log file is not a console. No capture can be taken over one —
        # `capture_command` refuses replay — so this token exists only to differ
        # from the live transports, which is what makes opening a replay revoke
        # a reading taken from the device that used to be here.
        return _identity_token([ctx.registration, "replay"])
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
    ctx.mesh_probe = None


def _forget_device_identity(ctx: DutContext) -> None:
    """Drop which unit is behind this DUT, back to "nobody has asked".

    Separate from `_forget_backhaul` because the two are anchored separately and
    a caller may need one without the other: a swapped device revokes the old
    unit's captures but leaves a perfectly good new identity in place.
    """
    ctx.device_id = None
    ctx.device_console = None


def _forget_if_another_console(ctx: DutContext, opening: str) -> None:
    """Revoke what a different console left behind, when one is opened.

    One rule, two callers — a serial open and an SSH connect — because a DUT can
    be opened either way regardless of how it is configured, and each of those
    opens is the moment the card starts describing a different device.

    Two stored things, checked against their own anchors rather than one. The
    capture's console is None until something has been captured, so hanging the
    identity off it would leave an identity learned on an unmeasured DUT to
    survive a recabling — and the next identify would then read a swap that
    never happened onto a device that had simply moved cables.
    """
    if ctx.backhaul_console is not None and ctx.backhaul_console != opening:
        _forget_backhaul(ctx)
    if ctx.device_console is not None and ctx.device_console != opening:
        _forget_device_identity(ctx)


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
            # Stamp the reading with the unit it was taken on, before it reaches
            # either the store that persists it or the browser that renders it.
            #
            # Here rather than in the parser, which has no idea which DUT it
            # belongs to, and here rather than at persist time, so the live
            # stream and the backfill carry the same field and a card does not
            # need two rules to read one number.
            #
            # Only `snapshot_update` carries it. A delta is applied onto a base
            # that already has one, and every chain of deltas starts from an
            # update -- so restamping each one would be repeating a fact that
            # cannot have changed without a fresh update arriving first.
            #
            # None until something has identified the device, and None on every
            # snapshot recorded before this field existed. Both mean "unknown
            # provenance", which every reader has to render as silence: a
            # warning drawn from a missing stamp would fire on the entire
            # existing history.
            if event.get("type") == "snapshot_update":
                snapshot = event.get("snapshot")
                if isinstance(snapshot, dict):
                    ctx = self._duts.get(dut_id)
                    snapshot["device_id"] = None if ctx is None else ctx.device_id
            snapshot_store.observe(event)
            console_buffer.observe(event)
            self._observe_model(dut_id, event)
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

    def rename_dut(self, dut_id: str, label: str) -> DutContext:
        """Change a DUT's display name and persist it.

        The id is the identity and never moves: per-DUT snapshots, logs and
        every `?dut=` caller are keyed on it. Only the label changes, so a
        rename costs nothing and loses no history -- which is the whole reason
        this exists rather than "remove and re-add", a route the built-in DUT
        does not have at all.
        """
        cleaned = clean_label(label)
        if cleaned is None:
            raise ValueError(f"DUT label must be 1-{MAX_LABEL_LEN} printable characters")
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None:
                raise KeyError(f"Unknown DUT: {dut_id}")
            ctx.label = cleaned
            self._save_locked()
        return ctx

    def _observe_model(self, dut_id: str, event: dict) -> None:
        """Read the model out of console output as it streams past.

        The prompt (``AP6_420E#``) arrives on its own, unasked, on every DUT
        and in replay alike, so this costs no serial time -- which matters:
        the connect-time capture races sysMon for the line and loses, so
        anything that needs a command would be unreliable here.

        Runs on the SerialWorker thread, inside the per-DUT ``on_event``
        closure, on every console line. Kept cheap on the hot path: once a
        model is known this returns on an attribute read, and the regex only
        sees lines while it is not.

        Deliberately never *clears* a known model. A blank line or a bootloader
        prompt saying nothing about the hardware is not evidence that the
        hardware changed, and a band mapping that flickers back to the default
        mid-session would be worse than one that is merely stale.
        """
        try:
            ctx = self._duts.get(dut_id)
            if ctx is None or ctx.model is not None:
                return
            event_type = event.get("type")
            if event_type == "console_line":
                texts = [event.get("text")]
            elif event_type == "console_line_batch":
                texts = event.get("lines") or []
            else:
                return
            for text in texts:
                if not isinstance(text, str):
                    continue
                found = dut_model.detect_model(text)
                if found:
                    self.record_model(dut_id, found)
                    return
        except Exception:  # noqa: BLE001 -- never let this break the stream
            return

    def record_model(self, dut_id: str, model: str) -> None:
        """Store a DUT's model and persist it. Best-effort, like the rest."""
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None or ctx.model == model:
                return
            ctx.model = model
            self._save_locked()

    def record_device_id(self, dut_id: str, device_id: str, mode: str | None = None) -> str | None:
        """Store which unit is behind this DUT, and drop what the last one left.

        Returns the identity that was replaced, or None -- so a caller can say
        "this is a different device" without asking twice and racing itself.

        The console-identity rule one level down (`_forget_if_another_console`)
        revokes a capture when the *console* changes: another port, another SSH
        config, a re-registration. It cannot see a device swapped for the same
        model on the same cable, where every part of the console is identical
        and only the hardware moved. That is the gap this closes, and it is the
        commonest swap there is on a bench with two of the same AP.

        Learning an identity for the first time is not a swap. `device_id` is
        None until something asks, and treating "we now know" as "it changed"
        would throw away a good capture on the first identify after a connect.
        """
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is None:
                return None
            # Recorded whether or not the name changed: re-confirming the same
            # unit still says which console it answered on, and an anchor that
            # only moved on a change would stay pinned to a console the DUT has
            # long since left.
            console = console_token(ctx, mode)
            if ctx.device_id == device_id:
                if ctx.device_console != console:
                    ctx.device_console = console
                    self._save_locked()
                return None
            previous = ctx.device_id
            if previous is not None:
                # A different unit answered on the same console. Whatever is
                # stored was measured on the one that left: kept, it would go
                # out through /api/duts as this device's current state, where
                # nothing downstream could tell it from a fresh reading.
                _forget_backhaul(ctx)
            ctx.device_id = device_id
            ctx.device_console = console
            # A hostname names the model as well as the unit, so learning one
            # without the other throws away an answer already in hand.
            #
            # `_observe_model` reads the prompt off the streaming console, and
            # on a quiet DUT that stream can carry nothing for a long time --
            # a capture's echo goes to the capture buffer, not to the parser.
            # Measured on the bench 2026-08-28: the Pi's mesh node published
            # `model: null` with `bands` claiming a 6GHz radio and
            # `vaps_per_band` 16, while its own hostname said AP6_420, which
            # has neither. `band_for_iface` would then have called ath8 "2.4G"
            # on an interface measured at 5.66 GHz -- the exact mistake the
            # model table was added to stop.
            #
            # Set rather than filled in, and set on a change too: this is a
            # fresh read of the device now in front of us, so it outranks a
            # model left behind by whatever used to be on this console.
            implied = dut_model.detect_model(device_id)
            if implied is not None:
                ctx.model = implied
            self._save_locked()
            return previous

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

    def store_mesh_probe(self, dut_id: str, probe: dict) -> None:
        """Commit what the DUT said about its own mesh.

        No console-identity check, unlike the backhaul writers: this probe runs
        on whatever console is open at the time and is dropped wholesale when
        that console changes, so there is no stored reading for a re-point to
        mislabel. It is also cheap to repeat -- one command on the next connect.
        """
        with self._lock:
            ctx = self._duts.get(dut_id)
            if ctx is not None:
                ctx.mesh_probe = probe

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
                    # Null until a prompt has been seen. Published because the
                    # band an athN belongs to depends on it, so a caller
                    # reading interfaces out of this API needs to know which
                    # numbering applies.
                    "model": ctx.model,
                    "vaps_per_band": dut_model.vaps_per_band(ctx.model),
                    # Which bands this model actually HAS, in interface order.
                    # A model without a 6GHz radio has no third block at all,
                    # so a consumer mapping athN -> band needs the list, not
                    # just the width.
                    "bands": list(dut_model.bands_for(ctx.model)),
                    # This model's core count, or null when the model is
                    # unknown. Published so a reader can tell a snapshot
                    # recorded on other hardware from one taken here -- the
                    # card shows CPU from the last snapshot whatever device
                    # that was, and 4 cores under an AP6_420E is the shape of
                    # that mistake.
                    "model_cores": dut_model.cores_for(ctx.model),
                    # Which unit this is, not which model. Null until something
                    # has asked it its name, and null is "we do not know" --
                    # never "a different device". Published because `model` and
                    # `model_cores` above cannot answer the question they were
                    # added for: two 420Es agree on both, so a reading taken on
                    # one of them reads as this one's with nothing to say so.
                    "device_id": ctx.device_id,
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
                    # A sibling of `backhaul`, not a member of it. The backhaul
                    # block is what a console MEASURED off this DUT's radios;
                    # this is what the DUT SAID when asked about its mesh, and
                    # the two answer different questions -- one of them about
                    # members no console here can reach. None until a probe has
                    # run, which is a third state again from `mesh: false`.
                    "mesh_probe": ctx.mesh_probe,
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
        if ctx.model:
            entry["model"] = ctx.model
        if ctx.device_id:
            entry["device_id"] = ctx.device_id
        return entry

    def _save_locked(self) -> None:
        """Persist the DUT list (call holding ``self._lock``).

        Non-default DUTs always persist so they survive a restart. The default
        DUT is re-created by ``build_default_registry`` on boot, so it is only
        written once it has something worth carrying across — never a bare entry
        that a reload would try to re-create.

        A changed label counts as worth carrying across. Without it the built-in
        DUT could be renamed and the new name would vanish on the next restart,
        because a boot-time label of ``Default`` is all a bare entry says.

        So does a detected model. It is learned from console output, so it would
        otherwise be relearned only after the next connect -- and until then the
        band mapping would silently fall back to the 840 layout on a 420.
        Every field added here has to be added to this list too; the failure is
        quiet, and it looks exactly like the feature working.
        """
        entries = [
            self._entry_for(ctx)
            for ctx in self._duts.values()
            if ctx.dut_id != DEFAULT_DUT_ID
            or ctx.last_serial is not None
            or ctx.mgmt_url
            or ctx.remote
            or ctx.label != DEFAULT_DUT_LABEL
            or ctx.model
            or ctx.device_id
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
                # A saved label is the operator's rename and outranks the
                # boot-time default. Merged, never cleared, like the fields
                # below: an entry written before renaming existed carries no
                # label and must not blank the one already in memory.
                saved_label = clean_label(entry.get("label")) if isinstance(entry, dict) else None
                if saved_label is not None:
                    existing.label = saved_label
                if last_serial is not None:
                    existing.last_serial = last_serial
                mgmt = entry.get("mgmt_url") if isinstance(entry, dict) else None
                if isinstance(mgmt, str) and mgmt:
                    existing.mgmt_url = mgmt
                # Run through the same detector as the live path, so a
                # hand-edited file cannot store a model the console could never
                # have produced -- and so both spellings load identically.
                saved_model = dut_model.detect_model(entry.get("model") or "")
                if saved_model is not None:
                    existing.model = saved_model
                # Same treatment, same detector: a hand-edited file must not be
                # able to store an identity no console could have produced, and
                # a missing one must merge rather than clear -- an entry written
                # before this field existed says nothing about the device.
                saved_device_id = dut_model.detect_device_id(entry.get("device_id") or "")
                if saved_device_id is not None:
                    existing.device_id = saved_device_id
                    # `device_console` is deliberately not restored with it. Every
                    # console token carries `registration`, a fresh uuid per boot,
                    # so a saved anchor could never match one computed now -- it
                    # would revoke the identity on the first open, every time.
                    # Leaving it None means the next identify is the authority on
                    # whether the hardware moved, which is the honest answer: it
                    # is the only thing here that has actually asked the device.
                if remote is not None:
                    # Merge, never clear: an entry written before the DUT was
                    # given an SSH console — or one this version rejects — must
                    # not silently strip a live configuration, which the next
                    # save would then erase from disk too.
                    existing.remote = remote
                continue
            # Same cleaning as the rename path: a hand-edited file must not be
            # able to put a label on screen that the API would have refused.
            ctx = self.create_dut(dut_id, label=clean_label(entry.get("label")))
            ctx.last_serial = last_serial
            ctx.remote = remote
            ctx.model = dut_model.detect_model(entry.get("model") or "")
            ctx.device_id = dut_model.detect_device_id(entry.get("device_id") or "")


def build_default_registry(
    ws_manager: WebSocketManager,
    loop: asyncio.AbstractEventLoop,
) -> DutRegistry:
    """Create a registry holding the single default DUT (uses the original
    ``SNAPSHOT_FILE`` so existing captured history keeps backfilling)."""
    registry = DutRegistry(ws_manager=ws_manager, loop=loop)
    registry.create_dut(DEFAULT_DUT_ID, label=DEFAULT_DUT_LABEL)
    registry.load_persisted()
    return registry
