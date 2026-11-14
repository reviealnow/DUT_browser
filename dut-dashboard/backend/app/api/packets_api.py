"""Packet Monitor REST routes (docs/design-packet-monitor.md §B9).

Gated in one place, `main.py`: the whole router is mounted with the engineer
dependency (Q1). Connecting, configuring and removing Hosts stays in
`collectors_api`, admin-only; here an engineer can only use a Host an admin has
already connected.

Every analysis is visible to every engineer, as the Files workspace is; who
started one is recorded from the session, never from the client.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services import auth_service
from app.services import packet_modes as pm
from app.services.packet_jobs import PacketError, PacketJobs

router = APIRouter(prefix="/api/packets", tags=["packets"])


class FilterBody(BaseModel):
    collector_id: str
    display_filter: str = ""


class AnalysisBody(BaseModel):
    file_id: int
    collector_id: str
    mode_id: str
    display_filter: str = ""
    extra_fields: list[str] = Field(default_factory=list)


def _jobs(request: Request) -> PacketJobs:
    jobs = getattr(request.app.state, "packet_jobs", None)
    if jobs is None:
        raise HTTPException(status_code=503, detail="Packet Monitor is not running.")
    return jobs


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PacketError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("/hosts")
def list_hosts(request: Request) -> dict:
    return {"hosts": _jobs(request).hosts()}


@router.post("/hosts/{collector_id}/probe")
def probe_host(collector_id: str, request: Request) -> dict:
    jobs = _jobs(request)
    _call(jobs.require_connected, collector_id)
    return _call(jobs.probe, collector_id).public()


@router.delete("/hosts/{collector_id}/staged/{sha256}")
def delete_staged(collector_id: str, sha256: str, request: Request) -> dict:
    return _call(_jobs(request).delete_staged, collector_id, sha256)


@router.get("/modes")
def list_modes() -> dict:
    return {
        "modes": [
            {
                "id": mode.id,
                "label": mode.label,
                "description": mode.description,
                "display_filter": mode.display_filter,
                "group_by": mode.group_by,
                "columns": [
                    {"key": c.key, "label": c.label, "decoder": c.decoder, "hidden": c.hidden}
                    for c in mode.columns
                ],
                "rules": list(mode.rules),
            }
            for mode in pm.MODES
        ]
    }


@router.post("/filters/validate")
def validate_filter(body: FilterBody, request: Request) -> dict:
    return _call(_jobs(request).validate_filter, body.collector_id, body.display_filter)


@router.get("/analyses")
def list_analyses(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    return {"analyses": _jobs(request).recent(limit)}


@router.post("/analyses", status_code=202)
def create_analysis(body: AnalysisBody, request: Request) -> dict:
    user = auth_service.current_user(request)
    analysis_id = _call(
        _jobs(request).create,
        file_id=body.file_id,
        collector_id=body.collector_id,
        mode_id=body.mode_id,
        display_filter=body.display_filter,
        extra_fields=body.extra_fields,
        user_id=user.get("id"),
    )
    return {"id": analysis_id}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: int, request: Request) -> dict:
    return _call(_jobs(request).get, analysis_id)


@router.delete("/analyses/{analysis_id}")
def cancel_analysis(analysis_id: int, request: Request) -> dict:
    return _call(_jobs(request).cancel, analysis_id)


@router.get("/analyses/{analysis_id}/rows")
def analysis_rows(
    analysis_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return _call(_jobs(request).rows, analysis_id, offset, limit)


@router.get("/analyses/{analysis_id}/groups")
def analysis_groups(analysis_id: int, request: Request) -> dict:
    return {"groups": _call(_jobs(request).groups, analysis_id)}


@router.get("/analyses/{analysis_id}/findings")
def analysis_findings(analysis_id: int, request: Request) -> dict:
    return {"findings": _call(_jobs(request).findings, analysis_id)}


@router.get("/analyses/{analysis_id}/frames/{frame_number}")
def analysis_frame(analysis_id: int, frame_number: int, request: Request) -> dict:
    return {"frame": _call(_jobs(request).frame, analysis_id, frame_number)}


@router.get("/analyses/{analysis_id}/export.csv")
def export_analysis(analysis_id: int, request: Request) -> StreamingResponse:
    # export_csv checks the analysis before it returns the stream, so a bad id
    # is a 404/409 here rather than an error half way through a download.
    return StreamingResponse(
        _call(_jobs(request).export_csv, analysis_id),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="packet-analysis-{analysis_id}.csv"'},
    )
