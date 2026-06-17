import csv
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import ANALYZER_OUTPUT_DIR, LOG_DIR

router = APIRouter(prefix="/api/analyzer", tags=["analyzer"])


class AnalyzerRunRequest(BaseModel):
    log_path: str


class AnalyzerRunSessionRequest(BaseModel):
    name: str


@router.post("/run")
def run_analyzer(body: AnalyzerRunRequest, request: Request) -> dict:
    try:
        return request.app.state.analyzer_service.run(body.log_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run-session")
def run_analyzer_for_session_log(body: AnalyzerRunSessionRequest, request: Request) -> dict:
    """Run the offline analyzer on a saved session log, referenced by name, and
    publish its CSV/PNG outputs to logs/analyzer_output/ (browsable in Downloads
    → Analyzer outputs). Name-based + validated so no absolute path or traversal
    reaches the filesystem."""
    name = body.name
    if name != Path(name).name or not (name.startswith("dut-session-") and name.endswith(".log")):
        raise HTTPException(status_code=400, detail="Invalid session log name")
    path = LOG_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Session log not found")
    try:
        return request.app.state.analyzer_service.run(str(path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _to_int(value: str | None) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


@router.get("/memory")
def get_memory(limit: int = 500) -> dict:
    """Parsed memory series from the latest analyzer run (logs/analyzer_output/
    memory.csv). Post-analysis only — empty until `POST /api/analyzer/run` (or a
    log download) produces it."""
    limit = max(1, min(limit, 2000))
    path = ANALYZER_OUTPUT_DIR / "memory.csv"
    if not path.exists() or not path.is_file():
        return {"available": False, "points": []}

    points: list[dict] = []
    generated_at: str | None = None
    version: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fp:
            for row in csv.DictReader(fp):
                generated_at = row.get("Generated_At") or generated_at
                version = row.get("Version") or version
                points.append(
                    {
                        "ts": row.get("Timestamp_MMDD_HHMMSS") or row.get("Timestamp") or "",
                        "memAvailableKb": _to_int(row.get("MemAvailable_kB")),
                        "slabKb": _to_int(row.get("Slab_kB")),
                        "sunreclaimKb": _to_int(row.get("SUnreclaim_kB")),
                        "effectiveKb": _to_int(row.get("EffectiveAvailable_kB")),
                    }
                )
    except Exception:
        return {"available": False, "points": []}

    points = points[-limit:]
    return {
        "available": len(points) > 0,
        "generated_at": generated_at,
        "version": version,
        "points": points,
    }
