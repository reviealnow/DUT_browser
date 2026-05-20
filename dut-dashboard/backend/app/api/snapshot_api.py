from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import LOG_DIR
from app.websocket.ws_manager import WebSocketManager

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])

_replay_lock = asyncio.Lock()


class ReplayStartRequest(BaseModel):
    file: str
    speed_ms: int = 500


def _safe_name(file_name: str) -> str:
    safe = Path(file_name).name
    if safe != file_name or not safe.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="Invalid file name")
    return safe


@router.get("/list")
def list_snapshots() -> dict:
    files = sorted(LOG_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        with f.open(encoding="utf-8") as fp:
            frames = sum(1 for line in fp if line.strip())
        result.append(
            {
                "name": f.name,
                "size_bytes": f.stat().st_size,
                "frames": frames,
                "mtime": f.stat().st_mtime,
            }
        )
    return {"files": result}


@router.post("/replay/start")
async def start_replay(body: ReplayStartRequest, request: Request) -> dict:
    safe = _safe_name(body.file)
    path = LOG_DIR / safe
    if not path.resolve().is_relative_to(LOG_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot file not found")

    ws_manager: WebSocketManager = request.app.state.ws_manager

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    total = len(lines)
    speed_ms = max(50, min(body.speed_ms, 30_000))

    async def do_replay() -> None:
        try:
            for i, line in enumerate(lines):
                try:
                    snapshot = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await ws_manager.broadcast({"type": "snapshot_update", "snapshot": snapshot})
                if "memory" in snapshot and isinstance(snapshot["memory"], dict):
                    mem = snapshot["memory"]
                    used_kb = mem.get("used_kb", 0)
                    free_kb = mem.get("free_kb", 0)
                    await ws_manager.broadcast(
                        {
                            "type": "memory_update",
                            "used_kb": used_kb,
                            "free_kb": free_kb,
                            "total_kb": used_kb + free_kb,
                        }
                    )
                await ws_manager.broadcast(
                    {"type": "replay_progress", "frame": i + 1, "total": total}
                )
                await asyncio.sleep(speed_ms / 1000)
            await ws_manager.broadcast({"type": "replay_done", "total": total})
        except asyncio.CancelledError:
            await ws_manager.broadcast({"type": "replay_stopped"})

    async with _replay_lock:
        existing: asyncio.Task | None = getattr(request.app.state, "replay_task", None)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.create_task(do_replay())
        request.app.state.replay_task = task
    return {"ok": True, "total": total, "file": safe}


@router.post("/replay/stop")
async def stop_replay(request: Request) -> dict:
    task: asyncio.Task | None = getattr(request.app.state, "replay_task", None)
    if task and not task.done():
        task.cancel()
    return {"ok": True}


@router.get("/{file_name}/download")
def download_snapshot(file_name: str) -> FileResponse:
    safe = _safe_name(file_name)
    path = LOG_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot file not found")
    return FileResponse(path=path, filename=safe, media_type="application/octet-stream")
