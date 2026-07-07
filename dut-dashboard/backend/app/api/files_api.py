"""Shared file workspace API (LAN File Server, shared-trust model — no auth)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.services import file_service

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("")
def list_files(
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: Literal["date", "name", "size", "uploader"] = "date",
    order: Literal["asc", "desc"] = "desc",
) -> dict:
    """File list plus the KPI aggregates the Files view needs, bundled to save
    a round-trip. Without ``limit`` the full list is returned (legacy
    behaviour). ``q`` filters by filename substring; ``total`` counts the rows
    matching ``q`` (all rows when ``q`` is empty) so the client can page, while
    ``stats`` always aggregates the whole workspace."""
    q = q.strip() if q and q.strip() else None
    return {
        "files": file_service.list_files(limit, offset, q, sort, order),
        "stats": file_service.stats(),
        "total": file_service.count_files(q),
    }


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    uploader: str | None = Form(default=None),
) -> dict:
    """Upload a file. `uploader` is the client's free-text display name (optional)."""
    try:
        file_id = file_service.save_uploaded_file(file.filename or "", file.file, uploader)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()

    created = file_service.get_file_by_id(file_id)
    if created is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="File saved but could not be read back.")
    return created


@router.get("/{file_id}/download")
def download_file(file_id: int) -> FileResponse:
    row = file_service.get_file_by_id(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        path = file_service.resolve_download_path(row)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path=path, filename=row["filename"], media_type="application/octet-stream")


@router.delete("/{file_id}")
def delete_file(file_id: int) -> dict:
    row = file_service.get_file_by_id(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    file_service.delete_file(row)
    return {"ok": True}
