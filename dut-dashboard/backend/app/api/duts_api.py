"""Dynamic DUT registry endpoints.

List / add / remove the DUTs the dashboard monitors. Each DUT gets its own
runtime context (serial worker, snapshot store, ...) keyed by ``dut_id``; the
per-DUT serial/snapshots/console/terminal/wifi endpoints already accept ``?dut=``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.services import auth_service

router = APIRouter(prefix="/api/duts", tags=["duts"])

_ENGINEER = Depends(auth_service.require_role("engineer"))


class DutCreateRequest(BaseModel):
    id: str
    label: str | None = None


class DutRenameRequest(BaseModel):
    label: str


# Split gate, like settings_api and for the same reason: the two halves of this
# router serve different audiences. GET is what the DUT switcher reads on every
# page load, including a guest browser that is only watching telemetry, so
# gating the whole router would empty the switcher for guests. Every mutating
# route is engineer+ -- they are the second exception to the router-level
# policy in main.py. Add a route here and it gets `_ENGINEER` unless it is
# genuinely readable by a guest; the gate is per-route precisely because this
# router is mixed.
@router.get("")
def list_duts(request: Request) -> dict:
    return {"duts": request.app.state.dut_registry.describe()}


@router.post("", dependencies=[_ENGINEER])
def add_dut(body: DutCreateRequest, request: Request) -> dict:
    registry = request.app.state.dut_registry
    try:
        ctx = registry.register_dut(body.id, label=body.label)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": ctx.dut_id, "label": ctx.label}


@router.patch("/{dut_id}", dependencies=[_ENGINEER])
def rename_dut(dut_id: str, body: DutRenameRequest, request: Request) -> dict:
    """Rename a DUT. Engineer+, matching add and remove.

    PATCH, not POST: the id is the identity and does not move, so this is an
    edit of one field on an existing DUT, not a new one. It exists because the
    built-in DUT could not be renamed by any means at all -- POST 409s on an
    existing id, DELETE refuses the default, and a label in ``duts.json`` was
    ignored on load for a DUT that already existed. Renaming by removing and
    re-adding would also throw away that DUT's snapshot history, which is keyed
    on the id.
    """
    registry = request.app.state.dut_registry
    try:
        ctx = registry.rename_dut(dut_id, body.label)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": ctx.dut_id, "label": ctx.label}


@router.delete("/{dut_id}", dependencies=[_ENGINEER])
def delete_dut(dut_id: str, request: Request) -> dict:
    """Remove a DUT. Engineer+, because this closes that DUT's serial worker --
    an anonymous caller could otherwise end someone else's capture mid-run."""
    registry = request.app.state.dut_registry
    try:
        registry.remove_dut(dut_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
