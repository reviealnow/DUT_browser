"""Admin-only lifecycle for edge log collectors.

Sibling of `fleet_api.py` and gated the same way, in the router rather than in
`main.py`: every route here reaches a machine with an operator's credentials,
which is the reach the fleet and firmware routes are already gated on, and a
split gate would be one more place for that policy to drift.

**No response body here contains a password**, and none can be used to recover
one. `has_password` says only whether a login is currently possible -- which the
UI needs, because passwords live in memory and a restart forgets them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.collector import hostkey as host_keys
from app.collector import probe as collector_probe
from app.collector.registry import MAX_COLLECTORS, PORT_MAX, PORT_MIN, CollectorError
from app.collector.ssh_session import CollectorSshError
from app.dut.registry import REMOTE_DEVICE_RE, REMOTE_IFACE_RE
from app.services import auth_service

router = APIRouter(prefix="/api/collectors", tags=["collectors"])
_ADMIN = Depends(auth_service.require_role("admin"))


class CollectorBody(BaseModel):
    id: str
    label: str | None = None
    ip: str
    #: What the box is expected to call itself, checked at login against what it
    #: answers. Optional: a collector converted from a remote node carries
    #: nobody's expectation, and inventing one would manufacture a mismatch.
    hostname: str | None = None
    user: str
    port: int = Field(default=22, ge=PORT_MIN, le=PORT_MAX)
    #: Optional so that re-pointing a collector's address does not require
    #: retyping the login. Absent means "leave whatever is held in memory".
    password: str | None = None
    #: The other way in. A path on this machine, not a secret in itself, and
    #: persisted exactly as a remote node's always was.
    #
    # Both of these have to be declared here or pydantic drops them before the
    # registry ever sees them -- which it did: the form offered a key, the row
    # came back asking for a password, and every test passed because they all
    # called the registry directly. Found by running the page.
    key_path: str | None = None
    auth: str | None = None


class PasswordBody(BaseModel):
    password: str


class TrustKeyBody(BaseModel):
    """The fingerprint the operator confirmed, and nothing else.

    Naming it is what separates this from `StrictHostKeyChecking=accept-new`:
    the server re-reads the host and writes only if the key still matches what
    was on screen. A body that said "trust whatever is there" would be the
    option this one was chosen over.
    """

    fingerprint: str


class AttachBody(BaseModel):
    """One DUT console behind a collector, as an admin asks for it."""

    device: str
    baudrate: int = Field(default=115200, ge=1)
    #: Optional so the common case is one press. Derived from the collector and
    #: the device when absent, which is stable across attaches of the same port.
    dut_id: str | None = None
    label: str | None = None
    #: Whether this DUT is part of a mesh, and which VAP to fall back to for its
    #: downward backhaul. Both are declarations nobody can measure -- the two
    #: fields the old node-registration form carried, and the reason retiring
    #: that form needed them here rather than simply dropping them.
    is_mesh: bool = False
    backhaul_iface: str | None = None

    @field_validator("backhaul_iface")
    @classmethod
    def safe_iface(cls, value: str | None) -> str | None:
        if value is not None and not REMOTE_IFACE_RE.fullmatch(value):
            raise ValueError("must be an ath interface")
        return value

    @field_validator("device")
    @classmethod
    def safe_device(cls, value: str) -> str:
        # Interpolated into a socat command line on the far end, so it is held
        # to the same expression the DUT registry accepts.
        if not REMOTE_DEVICE_RE.fullmatch(value) or ".." in value:
            raise ValueError("must be an absolute /dev path")
        return value


def _console_dut_id(collector_id: str, device: str) -> str:
    """A stable id for "this port on this collector".

    Derived rather than random so re-attaching the same physical console lands
    on the DUT that already has its history, and so two ports on one Pi cannot
    collide.
    """
    tail = device.rsplit("/", 1)[-1].lower()
    return f"{collector_id}-{tail}"[:32]


def _duts_on(dut_registry, collector_id: str) -> dict[str, tuple[str, bool]]:
    """Which DUT is registered against each device here, and whether it is open.

    The second half used to be missing, and the card believed the first half on
    its own: a DUT registered against a device made that row read "Attached here
    as …" for the life of the registration, console or no console. Detach could
    not clear it either -- it closes the worker and leaves the registration
    standing, which is correct (the DUT keeps its history and its settings) and
    left the row claiming a session that had ended, with no way back to Attach.

    Reported from the bench on 2026-09-16: a console that had been closed for an
    hour still read as attached, and pressing Detach changed nothing visible.
    """
    found: dict[str, tuple[str, bool]] = {}
    for dut_id in dut_registry.ids():
        context = dut_registry.get(dut_id)
        remote = context.remote
        if remote and remote.get("collector_id") == collector_id:
            found[remote["device"]] = (dut_id, context.serial_worker.is_open)
    return found


def _registry(request: Request):
    return request.app.state.collector_registry


def _known(request: Request, collector_id: str):
    registry = _registry(request)
    try:
        registry.get(collector_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown collector: {collector_id}") from exc
    return registry


@router.get("")
def list_collectors(request: Request, _admin: dict = _ADMIN) -> dict:
    return {"collectors": _registry(request).list_status(), "limit": MAX_COLLECTORS}


@router.post("")
def configure_collector(body: CollectorBody, request: Request, _admin: dict = _ADMIN) -> dict:
    registry = _registry(request)
    try:
        collector = registry.configure(
            body.model_dump(exclude={"password"}),
            # Only for a password collector: `configure` refuses to hold one for
            # a key collector, and passing it would store a credential nothing
            # will ever use.
            password=body.password if body.auth != "key" else None,
        )
    except CollectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **registry.status(collector.id)}


@router.post("/{collector_id}/password")
def set_password(
    collector_id: str, body: PasswordBody, request: Request, _admin: dict = _ADMIN
) -> dict:
    """Hand back a password a restart forgot, without re-sending the address."""
    registry = _known(request, collector_id)
    registry.set_password(collector_id, body.password)
    return {"ok": True, **registry.status(collector_id)}


@router.post("/{collector_id}/connect")
def connect_collector(collector_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    registry = _known(request, collector_id)
    try:
        result = registry.connect(collector_id)
    except CollectorError as exc:
        # 400: this side is not ready -- no password is held. Nothing was sent.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CollectorSshError as exc:
        # 502, matching `read_mesh` next door: the dashboard did its part and
        # the machine upstream refused, was unreachable, or is unidentified.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, **registry.status(collector_id), **result}


@router.get("/{collector_id}/hostkey")
def read_hostkey(collector_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    """What key this host presents, and what this machine already has on record.

    Reads only. The scan reaches the box; the record is local. A scan that
    fails is reported in `scan_error` with ssh-keyscan's own words rather than
    failing the request -- the record is still worth showing, and an
    unreachable box is a different problem from a changed key.
    """
    registry = _known(request, collector_id)
    collector = registry.get(collector_id)
    try:
        return host_keys.status(collector.ip, collector.port)
    except host_keys.HostKeyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{collector_id}/hostkey/trust")
def trust_hostkey(
    collector_id: str, body: TrustKeyBody, request: Request, _admin: dict = _ADMIN
) -> dict:
    """Record the key with this fingerprint, so a login can be attempted.

    This is the decision an operator used to make by SSHing to the box by hand
    and answering `yes`. It is the same decision, made where the fingerprint is
    on screen -- and it is still theirs: nothing here accepts a key nobody named.
    """
    registry = _known(request, collector_id)
    collector = registry.get(collector_id)
    try:
        return {"ok": True, **host_keys.trust(collector.ip, collector.port, body.fingerprint)}
    except host_keys.HostKeyError as exc:
        # 409: the host, or this machine's record of it, is not in the state the
        # operator was looking at. Nothing was written.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{collector_id}/consoles")
def list_consoles(collector_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    """What DUT consoles are behind this collector, and what is in the way.

    Everything here is read over the session that is already open, so it costs
    no login and no serial time -- and it answers, from the dashboard, the four
    questions `docs/fleet-remote-nodes.md` currently makes somebody SSH in by
    hand to check.
    """
    registry = _known(request, collector_id)
    try:
        session = registry.session_for(collector_id)
    except CollectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        found = collector_probe.probe(session)
    except CollectorSshError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    known = _duts_on(request.app.state.dut_registry, collector_id)
    for device in found["devices"]:
        # Which of these this dashboard is already holding. Distinct from
        # `busy`, which is whatever the box says has the port -- that includes a
        # minicom somebody left running, and knowing which of the two it is
        # decides whether the answer is "press Detach" or "go and look".
        dut_id, is_open = known.get(device["device"], (None, False))
        device["attached_dut"] = dut_id if is_open else None
        # Registered here, console closed. Worth saying rather than showing the
        # row as untouched: attaching lands on that same DUT, with the history
        # and the label it already has, because the id is derived from the
        # device rather than invented per attach.
        device["registered_dut"] = dut_id if not is_open else None
    return {
        "collector": collector_id,
        **found,
        "blockers": collector_probe.readiness(found),
    }


@router.post("/{collector_id}/consoles/attach")
def attach_console(
    collector_id: str, body: AttachBody, request: Request, _admin: dict = _ADMIN
) -> dict:
    """Open a DUT console on a serial device behind this collector.

    The DUT that appears is an ordinary one: same parser, same snapshot ring,
    same console buffer, same log session. Only its transport differs, and only
    in how the login is authenticated.

    The password is fetched from the collector registry's memory here and handed
    straight to the transport. It is deliberately not part of what
    `configure_remote` persists -- a password in `duts.json` is a password on
    disk, which is the one thing this feature promises never to do.
    """
    if body.is_mesh and not body.backhaul_iface:
        # Same rule the node-registration route has always applied. A mesh node
        # with no fallback VAP produces a root whose children silently read as
        # an empty list.
        raise HTTPException(
            status_code=400, detail="backhaul_iface is required for a mesh node"
        )
    registry = _known(request, collector_id)
    collector = registry.get(collector_id)
    dut_registry = request.app.state.dut_registry
    try:
        # Both refuse with a sentence rather than a KeyError: not connected, or
        # connected in a process that has since forgotten the password.
        registry.session_for(collector_id)
        credentials = registry.credentials_for(collector_id)
    except CollectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dut_id = (body.dut_id or _console_dut_id(collector_id, body.device)).strip()
    label = body.label or f"{collector.label} {body.device.rsplit('/', 1)[-1]}"
    remote = {
        "host": collector.ip,
        "user": collector.user,
        "key_path": "",
        "collector_id": collector_id,
        "port": collector.port,
        "device": body.device,
        "baudrate": body.baudrate,
        # Declared, not measured. Left false unless somebody says otherwise:
        # a backhaul capture on a DUT that is not meshed is a wrong answer
        # rather than a missing one.
        "is_mesh": body.is_mesh,
        "backhaul_iface": body.backhaul_iface if body.is_mesh else None,
    }
    created = False
    try:
        dut_registry.get(dut_id)
    except KeyError:
        try:
            dut_registry.register_dut(dut_id, label=label)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        created = True
    try:
        dut_registry.configure_remote(dut_id, remote)
    except (KeyError, ValueError) as exc:
        if created:
            dut_registry.remove_dut(dut_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    context = dut_registry.get(dut_id)
    try:
        context.serial_worker.open(
            port=body.device,
            baudrate=body.baudrate,
            mode="ssh",
            session_label=context.label,
            # The collector says how it authenticates; this route forwards it
            # rather than deciding. A key path overrides the empty one stored on
            # the console, and a password is never stored at all.
            ssh={**context.remote, **credentials},
        )
    except RuntimeError as exc:
        # The registration stays: it is what an operator edits and retries, and
        # dropping it would also throw away a DUT that may already have history
        # from an earlier attach. Only a DUT this request invented is removed.
        if created:
            dut_registry.remove_dut(dut_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    dut_registry.note_console_open(dut_id, "ssh")
    return {"ok": True, "dut": dut_id, "label": context.label, "device": body.device}


@router.post("/{collector_id}/consoles/detach")
def detach_console(
    collector_id: str, body: AttachBody, request: Request, _admin: dict = _ADMIN
) -> dict:
    """Close the console on one device, releasing the port on the collector."""
    _known(request, collector_id)
    dut_registry = request.app.state.dut_registry
    dut_id, is_open = _duts_on(dut_registry, collector_id).get(body.device, (None, False))
    if dut_id is None or not is_open:
        raise HTTPException(status_code=404, detail=f"No console attached on {body.device}")
    dut_registry.get(dut_id).serial_worker.close()
    return {"ok": True, "dut": dut_id, "device": body.device}


@router.post("/{collector_id}/disconnect")
def disconnect_collector(collector_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    registry = _known(request, collector_id)
    registry.disconnect(collector_id)
    return {"ok": True, **registry.status(collector_id)}


@router.delete("/{collector_id}")
def remove_collector(collector_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    """Drop a registration. Its session is closed; nothing on the box is touched."""
    registry = _known(request, collector_id)
    registry.remove(collector_id)
    return {"ok": True, "id": collector_id}
