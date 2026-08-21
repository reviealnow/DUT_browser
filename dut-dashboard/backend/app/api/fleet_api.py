"""Admin-only remote-node lifecycle, and on-demand link capture for any DUT.

Configuring, connecting and disconnecting a node are SSH operations and refuse
a DUT that has no remote configuration. The backhaul capture is not: it is two
console commands, and a cabled console runs them as well as an SSH one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.dut.registry import (
    REMOTE_DEVICE_RE,
    REMOTE_IFACE_RE,
    REMOTE_PORT_MAX,
    REMOTE_PORT_MIN,
    REMOTE_TOKEN_RE,
    console_token,
)
from app.services import auth_service
from app.services.wifi_clients import (
    classify_backhaul,
    parse_iwconfig_links,
    parse_wlanconfig_list,
    signal_band,
)

router = APIRouter(prefix="/api/fleet", tags=["fleet"])
_ADMIN = Depends(auth_service.require_role("admin"))

MAX_REMOTE_NODES = 4


class RemoteNodeBody(BaseModel):
    id: str
    label: str | None = None
    host: str
    user: str
    key_path: str
    port: int = Field(default=22, ge=REMOTE_PORT_MIN, le=REMOTE_PORT_MAX)
    device: str = "/dev/ttyUSB0"
    baudrate: int = Field(default=115200, ge=1)
    is_mesh: bool = True
    backhaul_iface: str | None = None

    @field_validator("host", "user")
    @classmethod
    def safe_ssh_token(cls, value: str) -> str:
        if not REMOTE_TOKEN_RE.fullmatch(value):
            raise ValueError("must contain only SSH host/user characters")
        return value

    @field_validator("key_path")
    @classmethod
    def present_key_path(cls, value: str) -> str:
        # The registry rejects a blank key path when it cleans the payload; catch
        # it here as well so the answer names the field instead of arriving as a
        # generic "invalid configuration" after the DUT has been created.
        if not value.strip():
            raise ValueError("must be a path to the private key")
        return value

    @field_validator("device")
    @classmethod
    def safe_device(cls, value: str) -> str:
        if not REMOTE_DEVICE_RE.fullmatch(value) or ".." in value:
            raise ValueError("must be an absolute /dev path")
        return value

    @field_validator("backhaul_iface")
    @classmethod
    def safe_iface(cls, value: str | None) -> str | None:
        if value is not None and not REMOTE_IFACE_RE.fullmatch(value):
            raise ValueError("must be an ath interface")
        return value


def _context(request: Request, dut_id: str):
    try:
        return request.app.state.dut_registry.get(dut_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown DUT: {dut_id}") from exc


@router.post("/nodes")
def configure_node(body: RemoteNodeBody, request: Request, _admin: dict = _ADMIN) -> dict:
    if body.is_mesh and not body.backhaul_iface:
        raise HTTPException(status_code=400, detail="backhaul_iface is required for a mesh node")
    registry = request.app.state.dut_registry
    remote_count = sum(1 for item in registry.ids() if registry.get(item).remote is not None)
    created = False
    try:
        context = registry.get(body.id)
    except KeyError:
        if remote_count >= MAX_REMOTE_NODES:
            raise HTTPException(status_code=400, detail="Remote node limit reached (4)")
        try:
            context = registry.register_dut(body.id, label=body.label)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        created = True
    if context.remote is None and remote_count >= MAX_REMOTE_NODES:
        raise HTTPException(status_code=400, detail="Remote node limit reached (4)")
    remote = body.model_dump(include={
        "host", "user", "key_path", "port", "device", "baudrate", "is_mesh", "backhaul_iface"
    })
    try:
        registry.configure_remote(context.dut_id, remote)
    except ValueError as exc:
        if created:
            # This DUT existed only to carry the configuration that was just
            # rejected. Leaving it behind would put a DUT nobody can connect in
            # the switcher, persist it, and spend one of the MAX_DUTS slots.
            registry.remove_dut(context.dut_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": context.dut_id, "label": context.label}


@router.post("/nodes/{dut_id}/connect")
def connect_node(dut_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    context = _context(request, dut_id)
    if context.remote is None:
        raise HTTPException(status_code=400, detail="DUT has no remote SSH configuration")
    try:
        context.serial_worker.open(
            port=context.remote["device"],
            baudrate=context.remote["baudrate"],
            mode="ssh",
            session_label=context.label,
            ssh=context.remote,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # This node may have been captured over a cable at this desk — a supported
    # thing to do, and a different console from the one just opened.
    request.app.state.dut_registry.note_console_open(dut_id, "ssh")
    return {"ok": True, "dut": dut_id, "mode": "ssh"}


@router.post("/nodes/{dut_id}/disconnect")
def disconnect_node(dut_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    _context(request, dut_id).serial_worker.close()
    return {"ok": True, "dut": dut_id}


def _peer(client: dict) -> dict:
    return {"mac": client["mac"], "rssi": client["rssi"], "rssi_band": signal_band(client["rssi"])}


def _known_uplinks(registry, exclude_dut_id: str) -> tuple[set[str], set[tuple]]:
    """What every other DUT reported as its uplink, for identifying a root.

    A root cannot name its own backhaul VAP from its own console, but a node
    can: the BSSID it associates to is that VAP. This reads what previous
    captures already stored, so it costs no console time — and it stays empty
    until some node has been captured, which is the ordering this depends on.

    Every registered DUT is walked, cabled ones included: the fleet's root is
    frequently the DUT on this desk, and it is named by a node exactly as a
    remote root would be.
    """
    bssids: set[str] = set()
    networks: set[tuple] = set()
    for other_id in registry.ids():
        if other_id == exclude_dut_id:
            continue
        uplink = registry.get(other_id).backhaul_uplink
        if not uplink:
            continue
        if uplink.get("peer_mac"):
            bssids.add(uplink["peer_mac"])
        if uplink.get("essid"):
            # Banded on purpose: an SSID alone does not name one VAP.
            networks.add((uplink["essid"], uplink.get("radio_band")))
    return bssids, networks


@router.post("/nodes/{dut_id}/rssi")
def capture_rssi(dut_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    """Measure both directions of a node's backhaul, and say which is which.

    `wlanconfig <vap> list` only ever answers the downward question: it lists
    the stations associated to a Master VAP. A node's own link to its parent
    lives on its Managed VAP and is only visible through `iwconfig`, so asking
    wlanconfig for it returns an empty table and no error — which is how a
    perfectly healthy -37 dBm uplink reads as "not captured" forever.

    Neither command needs SSH. Both go through `capture_command`, which answers
    the same way on a locally cabled console, so a DUT with no remote is
    captured here too — on this bench the mesh root is the AP6 on the desk, and
    refusing it made the one measurement this endpoint exists for unobtainable
    for the device most likely to be in front of someone. What a cabled DUT does
    not have is the pair of declarations a node's SSH configuration carries:
    nobody said it is meshed, and nobody named a fallback backhaul VAP. Both
    absences are handled below rather than guessed at.
    """
    context = _context(request, dut_id)
    remote = context.remote
    if remote is not None and not remote["is_mesh"]:
        # The only way to know a DUT has no mesh backhaul is for an admin to
        # have said so, which is a field of a remote node's configuration.
        return {
            "dut": dut_id, "applicable": False, "captured": False,
            "role": None, "uplink": None, "downlink": None,
        }

    worker = context.serial_worker
    try:
        links = parse_iwconfig_links(worker.capture_command("iwconfig"))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not links:
        # The console answered without a single wireless VAP, which happens
        # while the radios are being reconfigured. That is not "this DUT has
        # no uplink" — it is "we learned nothing" — so say so and leave the
        # last good reading alone. Storing None here would blank a live card
        # and, worse, strip the key a root needs to name its backhaul VAP.
        raise HTTPException(
            status_code=400,
            detail="Console reported no wireless interfaces; try again in a moment",
        )
    peer_bssids, peer_networks = _known_uplinks(request.app.state.dut_registry, dut_id)
    found = classify_backhaul(links, peer_bssids, peer_networks)

    uplink = None
    if found["uplink"] is not None:
        up = found["uplink"]
        uplink = {
            "iface": up["iface"],
            "rssi": up["rssi"],
            "snr": up["snr"],
            "rssi_band": signal_band(up["rssi"]),
            "radio_band": up["band"],
            "essid": up["essid"],
            "peer_mac": up["access_point"],
        }

    # A root has no uplink to pair against, so its downward VAP can only come
    # from configuration. Detection wins where it works: interface numbering
    # differs between models and firmware renumbers VAPs. A cabled DUT has no
    # configuration to fall back to — and wants none: the fallback is the least
    # trustworthy source here (on this bench a configured VAP was serving a
    # laptop), while detection from a peer's uplink names the VAP exactly.
    detected = found["downlink"]
    downlink_iface = detected["iface"] if detected else (
        remote.get("backhaul_iface") if remote is not None else None
    )

    # The capture parsed real VAPs, so an absent uplink is an answer rather
    # than a gap: this DUT has no parent. The card must be able to tell those
    # apart, and only the side that read the VAPs can.
    #
    # "No parent" is still not the same statement as "root of the mesh", and
    # for a cabled DUT it is the difference between a fact and a guess: a
    # standalone AP on someone's desk has no parent either. So a root is
    # claimed only where something backs it — an admin having declared this
    # node meshed, or a peer's uplink having named one of these VAPs. Neither,
    # and the honest answer is that this capture found no backhaul at all,
    # which `captured` keeps distinguishable from never having looked.
    if uplink is not None:
        role = "node"
    elif remote is not None or detected is not None:
        role = "root"
    else:
        role = None

    # Stored before the second capture. The uplink is already measured, and a
    # console that dies between the two commands must not cost a reading that
    # succeeded — the request still fails, but the fresh value is kept rather
    # than the card being left on a stale one.
    context.backhaul_uplink = uplink
    context.backhaul_role = role
    context.backhaul_captured = True
    # Which console these numbers came from, taken from the transport the
    # worker actually holds rather than from how the DUT is configured. A
    # registered node opened on a cable is captured over the cable, and a
    # reading filed against its Pi would be served back as that Pi's the next
    # time the node connects — the mislabelling console_id exists to prevent.
    context.backhaul_console = console_token(context, worker.mode)

    downlink = None
    if downlink_iface:
        try:
            table = worker.capture_command(f"wlanconfig {downlink_iface} list")
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        configured = next((link for link in links if link["iface"] == downlink_iface), None)
        downlink = {
            "iface": downlink_iface,
            # Where the interface came from, and what SSID it actually serves.
            # The two sources are not equally trustworthy: a detected VAP is
            # paired with a live backhaul, a configured one is whatever an
            # admin typed. On this bench the configured value pointed at a
            # client VAP, whose ordinary laptop would otherwise have been
            # rendered as a mesh child with no way to tell.
            "source": "detected" if detected else "configured",
            "essid": (detected or configured or {}).get("essid"),
            "peers": [_peer(c) for c in parse_wlanconfig_list(table, downlink_iface)],
        }

    context.backhaul_downlink = downlink
    return {
        "dut": dut_id,
        "applicable": True,
        "captured": True,
        "role": role,
        "uplink": uplink,
        "downlink": downlink,
    }
