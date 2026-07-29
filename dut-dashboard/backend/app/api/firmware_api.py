"""Admin firmware upgrade endpoints (P72b).

Flashing can brick the DUT, so this is the only admin-gated operational surface
in the app. Credentials are write-only here: they can be set and their presence
reported, but no endpoint ever returns the password.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dut.registry import DEFAULT_DUT_ID
from app.services import auth_service, file_service, firmware_service

router = APIRouter(prefix="/api/firmware", tags=["firmware"])

_ADMIN = Depends(auth_service.require_role("admin"))


class UpgradeBody(BaseModel):
    file_id: int
    dut: str = DEFAULT_DUT_ID
    # The customer's published checksum. Optional, but compared when supplied.
    expected_sha256: str | None = None
    # True forces a rehearsal. None uses the deployment default. False is NOT a
    # way to switch a real flash on: DUT_FIRMWARE_DRY_RUN is a safety flag, so
    # a request can only ever make a run safer, never riskier.
    dry_run: bool | None = None


class CredentialsBody(BaseModel):
    user: str
    password: str


class MgmtUrlBody(BaseModel):
    dut: str = DEFAULT_DUT_ID
    mgmt_url: str


@router.get("/config")
def get_config(request: Request, _admin: dict = _ADMIN) -> dict:
    """What the UI needs to explain why an upgrade would be refused.

    Reports only WHETHER credentials exist — never the password, not even to an
    admin, because a UI that can display it is a UI that can leak it.
    """
    registry = request.app.state.dut_registry
    duts = [
        {"id": dut_id, "label": registry.get(dut_id).label, "mgmt_url": registry.get(dut_id).mgmt_url}
        for dut_id in registry.ids()
    ]
    user, _ = firmware_service.get_credentials()
    return {
        "duts": duts,
        "has_credentials": firmware_service.has_credentials(),
        "user": user,
        "dry_run": firmware_service.is_dry_run(),
        "upgrade_path": firmware_service.UPGRADE_PATH,
    }


@router.put("/credentials")
def set_credentials(body: CredentialsBody, _admin: dict = _ADMIN) -> dict:
    """Store the DUT management API credentials. Write-only by design."""
    user = body.user.strip()
    if not user or not body.password:
        raise HTTPException(status_code=400, detail="user and password are both required")
    firmware_service.set_credentials(user, body.password)
    return {"has_credentials": True, "user": user}


@router.put("/mgmt-url")
def set_mgmt_url(body: MgmtUrlBody, request: Request, _admin: dict = _ADMIN) -> dict:
    """Set one DUT's management origin; empty clears it (and blocks upgrades)."""
    registry = request.app.state.dut_registry
    try:
        context = registry.get(body.dut)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown DUT: {body.dut}") from exc
    registry.record_mgmt_url(body.dut, firmware_service.normalise_mgmt_url(body.mgmt_url))
    return {"dut": body.dut, "mgmt_url": context.mgmt_url}


@router.post("/upgrade")
async def upgrade(body: UpgradeBody, request: Request, _admin: dict = _ADMIN) -> dict:
    """Verify the image and PUT it to the DUT's management API.

    Long-running: the DUT writes flash with the request still open, so this runs
    on a worker thread while progress streams over the existing /ws.
    """
    row = file_service.get_file_by_id(body.file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        file_service.resolve_download_path(row)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    app = request.app
    try:
        context = app.state.dut_registry.get(body.dut)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown DUT: {body.dut}") from exc

    ws_manager = app.state.ws_manager

    def emit(progress: dict) -> None:
        # Same bridge the site survey uses; unknown event types are ignored by
        # frontends that do not handle them, so this stays additive.
        ws_manager.emit_from_thread(
            {"type": "firmware_progress", "dut_id": body.dut, **progress}
        )

    # OR, never override: a deployment-wide dry run cannot be switched off from
    # the browser, but any caller may ask for a rehearsal.
    dry_run = firmware_service.is_dry_run() or bool(body.dry_run)
    try:
        return await asyncio.to_thread(
            firmware_service.run_upgrade,
            context.mgmt_url,
            row,
            emit,
            body.expected_sha256,
            dry_run,
            # Lets the service confirm the DUT actually began flashing instead
            # of trusting the HTTP status alone.
            lambda: context.console_buffer.recent(200),
        )
    except firmware_service.ChecksumMismatch as exc:
        # 409: the request was well-formed, the bytes are not what was expected.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except firmware_service.FirmwareRejected as exc:
        # 502: we reached the DUT, the DUT said no.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except firmware_service.FirmwareAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except firmware_service.FirmwareError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
