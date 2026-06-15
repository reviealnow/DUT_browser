"""Dynamic DUT registry endpoints.

List / add / remove the DUTs the dashboard monitors. Each DUT gets its own
runtime context (serial worker, snapshot store, ...) keyed by ``dut_id``; the
per-DUT serial/snapshots/console/terminal/wifi endpoints already accept ``?dut=``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/duts", tags=["duts"])


class DutCreateRequest(BaseModel):
    id: str
    label: str | None = None


@router.get("")
def list_duts(request: Request) -> dict:
    return {"duts": request.app.state.dut_registry.describe()}


@router.post("")
def add_dut(body: DutCreateRequest, request: Request) -> dict:
    registry = request.app.state.dut_registry
    try:
        ctx = registry.register_dut(body.id, label=body.label)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": ctx.dut_id, "label": ctx.label}


@router.delete("/{dut_id}")
def delete_dut(dut_id: str, request: Request) -> dict:
    registry = request.app.state.dut_registry
    try:
        registry.remove_dut(dut_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
