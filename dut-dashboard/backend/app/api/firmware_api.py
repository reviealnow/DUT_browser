"""Admin firmware upgrade endpoints (P72b).

Flashing can brick the DUT, so this is the only admin-gated operational surface
in the app. The image endpoint is the deliberate exception: the DUT's curl
carries no session cookie, so it is authorised by a single-use token instead.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.dut.registry import DEFAULT_DUT_ID
from app.services import auth_service, file_service, firmware_service

router = APIRouter(prefix="/api/firmware", tags=["firmware"])


class UpgradeBody(BaseModel):
    file_id: int
    dut: str = DEFAULT_DUT_ID


class TemplateBody(BaseModel):
    template: str


@router.get("/config")
def get_config(_admin: dict = Depends(auth_service.require_role("admin"))) -> dict:
    """What the UI needs to tell the operator why a flash would be refused."""
    template = firmware_service.get_upgrade_template()
    return {
        "configured": bool(template),
        "template": template,
        "dry_run": firmware_service.is_dry_run(),
    }


@router.put("/config")
def set_config(
    body: TemplateBody,
    _admin: dict = Depends(auth_service.require_role("admin")),
) -> dict:
    """Set the shell command run on the DUT; `{url}` is replaced with the image
    URL. Stored rather than hardcoded because it is DUT-firmware specific."""
    template = body.template.strip()
    if template and "{url}" not in template:
        raise HTTPException(status_code=400, detail="template must contain {url}")
    firmware_service.set_upgrade_template(template)
    return {"configured": bool(template), "template": template}


@router.get("/image/{token}")
def get_image(token: str) -> FileResponse:
    """Serve the image to the DUT. Deliberately unauthenticated — the DUT has no
    session — and the token is single-use, so a fetch cannot be replayed."""
    path = firmware_service.claim_image(token)
    if path is None:
        raise HTTPException(status_code=404, detail="invalid or expired image token")
    return FileResponse(path=path, media_type="application/octet-stream")


@router.post("/upgrade")
async def upgrade(
    body: UpgradeBody,
    request: Request,
    _admin: dict = Depends(auth_service.require_role("admin")),
) -> dict:
    """Flash `file_id` onto the DUT. Long-running: the flash holds the serial
    link for minutes, so it runs on a worker thread while progress streams over
    the existing /ws (no new socket)."""
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

    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    try:
        return await asyncio.to_thread(
            firmware_service.run_upgrade,
            context.serial_worker,
            row,
            port,
            emit,
        )
    except firmware_service.FirmwareError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Serial not open / another capture in flight.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
