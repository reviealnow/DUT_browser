"""Shared file workspace API (LAN File Server, shared-trust model — no auth)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app.services import file_service

router = APIRouter(prefix="/api/files", tags=["files"])

# In-row preview support (Files view row expand). Images render in an <img>;
# text files show their head, capped so a huge log can't flood the response.
PREVIEW_IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}
PREVIEW_TEXT_EXTS = {"log", "txt", "csv", "json"}
PREVIEW_TEXT_BYTES = 64 * 1024


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


@router.get("/{file_id}/preview")
def preview_file(file_id: int):
    """Row-expand preview: images stream with their real content type so an
    <img> can render them; text files return their first PREVIEW_TEXT_BYTES as
    text/plain with X-Preview-Truncated signalling a cut. Other types are 415
    (the client offers download instead)."""
    row = file_service.get_file_by_id(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        path = file_service.resolve_download_path(row)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    name = row["filename"]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in PREVIEW_IMAGE_TYPES:
        return FileResponse(path=path, media_type=PREVIEW_IMAGE_TYPES[ext])
    if ext in PREVIEW_TEXT_EXTS:
        with path.open("rb") as fh:
            head = fh.read(PREVIEW_TEXT_BYTES + 1)
        truncated = len(head) > PREVIEW_TEXT_BYTES
        text = head[:PREVIEW_TEXT_BYTES].decode("utf-8", errors="replace")
        return PlainTextResponse(text, headers={"X-Preview-Truncated": "1" if truncated else "0"})
    raise HTTPException(status_code=415, detail="No preview for this file type.")


@router.delete("/{file_id}")
def delete_file(file_id: int) -> dict:
    row = file_service.get_file_by_id(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    file_service.delete_file(row)
    return {"ok": True}
