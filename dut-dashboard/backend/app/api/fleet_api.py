"""Admin-only lifecycle and on-demand link capture for SSH-backed DUTs."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.services import auth_service
from app.services.wifi_clients import parse_wlanconfig_list, signal_band

router = APIRouter(prefix="/api/fleet", tags=["fleet"])
_ADMIN = Depends(auth_service.require_role("admin"))

_REMOTE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@%+-]*$")
_DEVICE = re.compile(r"^/dev/[A-Za-z0-9._/-]+$")
MAX_REMOTE_NODES = 4


class RemoteNodeBody(BaseModel):
    id: str
    label: str | None = None
    host: str
    user: str
    key_path: str
    port: int = Field(default=22, ge=1, le=65535)
    device: str = "/dev/ttyUSB0"
    baudrate: int = Field(default=115200, ge=1)
    is_mesh: bool = True
    backhaul_iface: str | None = None

    @field_validator("host", "user")
    @classmethod
    def safe_ssh_token(cls, value: str) -> str:
        if not _REMOTE_TOKEN.fullmatch(value):
            raise ValueError("must contain only SSH host/user characters")
        return value

    @field_validator("device")
    @classmethod
    def safe_device(cls, value: str) -> str:
        if not _DEVICE.fullmatch(value) or ".." in value:
            raise ValueError("must be an absolute /dev path")
        return value

    @field_validator("backhaul_iface")
    @classmethod
    def safe_iface(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"ath\d+", value):
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
    try:
        context = registry.get(body.id)
    except KeyError:
        if remote_count >= MAX_REMOTE_NODES:
            raise HTTPException(status_code=400, detail="Remote node limit reached (4)")
        try:
            context = registry.register_dut(body.id, label=body.label)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if context.remote is None and remote_count >= MAX_REMOTE_NODES:
        raise HTTPException(status_code=400, detail="Remote node limit reached (4)")
    remote = body.model_dump(include={
        "host", "user", "key_path", "port", "device", "baudrate", "is_mesh", "backhaul_iface"
    })
    try:
        registry.configure_remote(context.dut_id, remote)
    except ValueError as exc:
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
    return {"ok": True, "dut": dut_id, "mode": "ssh"}


@router.post("/nodes/{dut_id}/disconnect")
def disconnect_node(dut_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    _context(request, dut_id).serial_worker.close()
    return {"ok": True, "dut": dut_id}


@router.post("/nodes/{dut_id}/rssi")
def capture_rssi(dut_id: str, request: Request, _admin: dict = _ADMIN) -> dict:
    context = _context(request, dut_id)
    remote = context.remote
    if remote is None:
        raise HTTPException(status_code=400, detail="DUT has no remote SSH configuration")
    if not remote["is_mesh"]:
        return {"dut": dut_id, "applicable": False, "rssi": None, "band": None}
    try:
        output = context.serial_worker.capture_command(
            f"wlanconfig {remote['backhaul_iface']} list"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    clients = parse_wlanconfig_list(output, remote["backhaul_iface"])
    rssi = clients[0]["rssi"] if clients else None
    context.remote_rssi = rssi
    context.remote_rssi_band = signal_band(rssi)
    return {
        "dut": dut_id,
        "applicable": True,
        "rssi": rssi,
        "band": context.remote_rssi_band,
    }
