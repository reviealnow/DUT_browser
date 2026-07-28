"""Shared file workspace: upload, list, download metadata, delete, aggregates.

Ported from the LAN File Server. Differences in the shared-trust model:
  - no user_id / owner column — `uploader` is free text (nullable);
  - delete has no owner check (anyone in the trusted LAN may delete);
  - aggregates group by `uploader` instead of a users join.

Security preserved from the original: extension whitelist, 50 MB size cap,
sanitised filenames, and unique on-disk names. Path-traversal on download is
guarded by `resolve_download_path`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from app.config import ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES, UPLOAD_DIR
from app.db.workspace import execute, query_all, query_one
from app.services import tag_service


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_CHUNK = 1024 * 1024


def secure_filename(filename: str) -> str:
    """Reduce a client filename to a safe basename: strip any directory parts,
    replace whitespace with underscores, drop characters outside [A-Za-z0-9._-],
    and trim leading dots/dashes. Mirrors werkzeug.secure_filename closely
    enough for our whitelist without adding a Flask dependency."""
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = _SAFE_CHARS.sub("_", filename.strip().replace(" ", "_"))
    filename = filename.lstrip("._-")
    return filename


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _unique_filename(original_name: str) -> str:
    safe_name = secure_filename(original_name)
    if not safe_name:
        raise ValueError("Invalid filename.")

    candidate = UPLOAD_DIR / safe_name
    stem = candidate.stem
    suffix = candidate.suffix
    idx = 1
    while candidate.exists():
        candidate = UPLOAD_DIR / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate.name


def save_uploaded_file(
    filename: str,
    fileobj: BinaryIO,
    uploader: str | None,
    uploader_user_id: int | None = None,
) -> int:
    """Validate, store the upload under UPLOAD_DIR, and record it. `fileobj` is a
    binary file-like (e.g. Starlette UploadFile.file). Enforces the size cap while
    streaming so an oversized file is rejected without being fully buffered.

    `uploader_user_id` is set when a session backed the upload; a NULL id means
    the name is unverified client-supplied text (pre-P71d rows, or an
    unauthenticated caller)."""
    if not filename:
        raise ValueError("No file selected.")
    if not allowed_file(filename):
        allowed = ", ".join(f".{ext}" for ext in sorted(ALLOWED_EXTENSIONS))
        raise ValueError(f"File type is not allowed. Accepted formats: {allowed}.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = _unique_filename(filename)
    save_path = UPLOAD_DIR / stored_name

    size = 0
    try:
        with save_path.open("wb") as out:
            while True:
                chunk = fileobj.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
                out.write(chunk)
    except ValueError:
        save_path.unlink(missing_ok=True)
        raise

    clean_uploader = uploader.strip() if uploader and uploader.strip() else None
    return execute(
        "INSERT INTO files (filename, filepath, size, uploader, uploader_user_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (stored_name, str(save_path), size, clean_uploader, uploader_user_id),
    )


# Whitelisted sort keys -> ORDER BY fragments (id tiebreak keeps pages stable).
# "date" is the default and matches the pre-sort behaviour (newest first).
FILE_SORTS = {
    "date": "uploaded_at {o}, id {o}",
    "name": "filename COLLATE NOCASE {o}, id {o}",
    "size": "size {o}, id {o}",
    "uploader": "uploader IS NULL, uploader COLLATE NOCASE {o}, id {o}",
}


def _like_pattern(q: str) -> str:
    """Substring LIKE pattern with %/_ escaped so user input matches literally."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_files(
    limit: int | None = None,
    offset: int = 0,
    q: str | None = None,
    sort: str = "date",
    order: str = "desc",
) -> list[dict]:
    """``limit=None`` returns everything (legacy no-param behaviour); otherwise a
    page of ``limit`` rows starting at ``offset``. ``q`` filters by filename
    substring (case-insensitive). ``sort``/``order`` must be pre-validated
    against FILE_SORTS / asc|desc (the API layer does this)."""
    order_by = FILE_SORTS[sort].format(o="ASC" if order == "asc" else "DESC")
    sql = f"""
        SELECT id, filename, size, uploader, uploaded_at,
               uploader_user_id IS NOT NULL AS uploader_verified
        FROM files
        {"WHERE filename LIKE ? ESCAPE '\\'" if q else ""}
        ORDER BY {order_by}
        """
    params: tuple = (_like_pattern(q),) if q else ()
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += (limit, offset)
    rows = query_all(sql, params)
    files = [dict(r) for r in rows]
    tag_map = tag_service.tags_for_files([f["id"] for f in files])
    for f in files:
        f["tags"] = tag_map.get(f["id"], [])
    return files


def count_files(q: str | None = None) -> int:
    if q:
        row = query_one(
            "SELECT COUNT(*) AS n FROM files WHERE filename LIKE ? ESCAPE '\\'",
            (_like_pattern(q),),
        )
    else:
        row = query_one("SELECT COUNT(*) AS n FROM files")
    return int(row["n"])


def get_file_by_id(file_id: int) -> dict | None:
    row = query_one(
        "SELECT id, filename, filepath, size, uploader, uploaded_at FROM files WHERE id = ?",
        (file_id,),
    )
    if row is None:
        return None
    result = dict(row)
    result["tags"] = tag_service.tags_for_files([file_id]).get(file_id, [])
    return result


def resolve_download_path(file_row: dict) -> Path:
    """Return the on-disk path for a file row, guaranteed to live inside
    UPLOAD_DIR. Raises FileNotFoundError otherwise (traversal / missing)."""
    path = Path(file_row["filepath"]).resolve()
    base = UPLOAD_DIR.resolve()
    if base != path and base not in path.parents:
        raise FileNotFoundError("File is outside the upload directory.")
    if not path.is_file():
        raise FileNotFoundError("File not found on disk.")
    return path


def delete_file(file_row: dict) -> None:
    """Delete the file from disk and DB. No owner check — shared-trust model."""
    path = Path(file_row["filepath"])
    if path.exists():
        path.unlink()
    execute("DELETE FROM files WHERE id = ?", (file_row["id"],))


# ---------------------------------------------------------------------------
# Dashboard aggregates (KPI row + charts). Plain JSON-serialisable dicts.
# ---------------------------------------------------------------------------

def uploads_per_day(days: int = 14) -> list[dict]:
    """Files uploaded per calendar day for the last `days` days, zero-filled."""
    rows = query_all("SELECT date(uploaded_at) AS d, COUNT(*) AS c FROM files GROUP BY d")
    counts = {r["d"]: r["c"] for r in rows}
    today = date.today()
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        series.append({
            "date": day.isoformat(),
            "label": f"{day.month}/{day.day}",
            "count": counts.get(day.isoformat(), 0),
        })
    return series


def files_by_type() -> list[dict]:
    """Aggregate file count and total size per extension, largest first."""
    rows = query_all("SELECT filename, size FROM files")
    agg: dict[str, dict] = {}
    for row in rows:
        name = row["filename"]
        ext = name.rsplit(".", 1)[1].lower() if "." in name else "other"
        entry = agg.setdefault(ext, {"ext": ext, "count": 0, "size": 0})
        entry["count"] += 1
        entry["size"] += row["size"]
    return sorted(agg.values(), key=lambda e: e["size"], reverse=True)


def top_uploaders(limit: int = 5) -> list[dict]:
    """Uploaders ranked by number of files. NULL uploader is labelled "—"."""
    rows = query_all(
        """
        SELECT COALESCE(uploader, '—') AS uploader, COUNT(*) AS count
        FROM files
        GROUP BY COALESCE(uploader, '—')
        ORDER BY count DESC, uploader ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def stats() -> dict:
    """KPI summary returned alongside the file list (saves a round-trip)."""
    rows = query_all("SELECT size, uploader, uploaded_at FROM files")
    total = len(rows)
    total_size = sum(r["size"] for r in rows)
    contributors = len({r["uploader"] for r in rows if r["uploader"]})
    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    this_week = sum(1 for r in rows if (r["uploaded_at"] or "") >= week_ago)
    return {
        "total": total,
        "total_size": total_size,
        "contributors": contributors,
        "this_week": this_week,
        "uploads_per_day": uploads_per_day(),
        "files_by_type": files_by_type(),
        "top_uploaders": top_uploaders(),
    }
