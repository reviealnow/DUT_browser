from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from serial.tools import list_ports

from app.config import ANALYZER_SCRIPT, LOG_DIR
from app.dut.registry import DEFAULT_DUT_ID, DutContext
from app.services import survey_snapshot

router = APIRouter(prefix="/api/serial", tags=["serial"])


def _dut(request: Request, dut: str) -> DutContext:
    """Resolve a DUT context or raise 404 for an unknown id."""
    try:
        return request.app.state.dut_registry.get(dut)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown DUT: {dut}") from exc


class SerialOpenRequest(BaseModel):
    port: str = ""
    baudrate: int = 115200
    mode: Literal["serial", "replay"] = "serial"
    replay_path: str | None = None
    replay_interval_ms: int = 100
    # Free-text DUT label woven into the session-log filename (sanitized backend-side).
    session_label: str | None = None


class SerialSendRequest(BaseModel):
    text: str


class SerialResizeRequest(BaseModel):
    rows: int
    cols: int
    term: str | None = None


class WifiKickRequest(BaseModel):
    iface: str
    mac: str


_IFACE_RE = re.compile(r"^ath\d+$")
_KICK_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


class DownloadWorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


MIN_SNAPSHOT_MARKERS = 2
DIRECT_DOWNLOAD_MAX_LINES = 100
TOP_COMMAND_PATTERN = re.compile(r"\btop\b", re.IGNORECASE)


def create_dut_session_dir() -> Path:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to create logs root directory: {exc}", status_code=500) from exc

    for _ in range(3):
        session_name = f"dut-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        session_dir = LOG_DIR / session_name
        try:
            session_dir.mkdir(parents=False, exist_ok=False)
            return session_dir
        except FileExistsError:
            time.sleep(1)
        except Exception as exc:
            raise DownloadWorkflowError(f"failed to create directory: {exc}", status_code=500) from exc

    raise DownloadWorkflowError("failed to create directory: session name collision", status_code=500)


def save_downloaded_log_to_session(file_name: str, session_dir: Path) -> Path:
    safe_name = Path(file_name).name
    if safe_name != file_name:
        raise DownloadWorkflowError("failed to download DUT log: invalid file name", status_code=400)

    src = LOG_DIR / safe_name
    if not src.exists() or not src.is_file():
        raise DownloadWorkflowError("log file not found", status_code=404)

    dst = session_dir / safe_name
    try:
        shutil.copy2(src, dst)
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to write downloaded DUT log: {exc}", status_code=500) from exc
    return dst


def should_bypass_analyzer(log_path: Path) -> bool:
    line_count = 0
    has_top_command = False
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                line_count += 1
                if TOP_COMMAND_PATTERN.search(line):
                    has_top_command = True
                if line_count >= DIRECT_DOWNLOAD_MAX_LINES:
                    return False
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to read downloaded DUT log: {exc}", status_code=500) from exc
    return line_count < DIRECT_DOWNLOAD_MAX_LINES and not has_top_command


def ensure_log_has_minimum_snapshots(log_path: Path, minimum_markers: int = MIN_SNAPSHOT_MARKERS) -> None:
    marker = "= Test Time:"
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as fp:
            count = 0
            for line in fp:
                if marker in line:
                    count += 1
                    if count >= minimum_markers:
                        return
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to read downloaded DUT log: {exc}", status_code=500) from exc
    raise DownloadWorkflowError(
        f"log too short for analysis; need at least {minimum_markers} snapshots ('{marker}')",
        status_code=422,
    )


def run_analyzer_for_session(session_dir: Path) -> None:
    if not ANALYZER_SCRIPT.exists() or not ANALYZER_SCRIPT.is_file():
        raise DownloadWorkflowError("analyzer3.py not found", status_code=500)

    mpl_config_dir = LOG_DIR / ".mplconfig"
    try:
        mpl_config_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to prepare analyzer runtime directory: {exc}", status_code=500) from exc

    env = os.environ.copy()
    env["MPLCONFIGDIR"] = str(mpl_config_dir)
    env["MPLBACKEND"] = "Agg"

    try:
        completed = subprocess.run(
            [sys.executable, str(ANALYZER_SCRIPT)],
            cwd=session_dir,
            capture_output=True,
            text=True,
            env=env,
        )
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to execute analyzer3.py: {exc}", status_code=500) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        combined = f"{stderr}\n{stdout}".strip()
        if "Matplotlib is building the font cache" in combined:
            raise DownloadWorkflowError(
                "analyzer runtime setup issue (matplotlib font cache), not log content issue",
                status_code=500,
            )
        message = stderr or stdout or "analyzer3.py execution failed"
        raise DownloadWorkflowError(f"analyzer3.py execution failed: {message}", status_code=500)


def bundle_survey_snapshots(session_dir: Path) -> None:
    """Copy the newest persisted site-survey pair of every DUT into
    ``session_dir/site-survey/`` so the log ZIP carries the survey context.

    Best-effort: a missing snapshot or copy error must never fail the download.
    """
    try:
        paths = survey_snapshot.latest_all()
        if not paths:
            return
        dest_dir = session_dir / "site-survey"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in paths:
            shutil.copy2(path, dest_dir / path.name)
    except Exception:  # noqa: BLE001 — bundling is best-effort
        logging.getLogger(__name__).exception("failed to bundle survey snapshots")


def zip_session_dir(session_dir: Path) -> Path:
    if not session_dir.exists() or not session_dir.is_dir():
        raise DownloadWorkflowError("failed to create zip: session directory not found", status_code=500)

    zip_path = session_dir.with_suffix(".zip")
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(session_dir.rglob("*")):
                if item.is_file():
                    zf.write(item, arcname=item.relative_to(session_dir.parent))
    except Exception as exc:
        raise DownloadWorkflowError(f"failed to create zip: {exc}", status_code=500) from exc
    return zip_path


@router.get("/ports")
def list_serial_ports() -> dict:
    ports = []
    for info in list_ports.comports():
        ports.append(
            {
                "device": info.device,
                "description": info.description or "",
                "hwid": info.hwid or "",
            }
        )
    return {"ports": ports}


@router.post("/open")
def open_serial(body: SerialOpenRequest, request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    serial_worker = _dut(request, dut).serial_worker
    try:
        serial_worker.open(
            port=body.port,
            baudrate=body.baudrate,
            mode=body.mode,
            replay_path=body.replay_path,
            replay_interval_ms=body.replay_interval_ms,
            session_label=body.session_label,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Remember the params of a successful serial-mode open so the Fleet view can
    # offer one-click Connect. Replay opens have no reusable port — skip them.
    if body.mode == "serial" and body.port:
        request.app.state.dut_registry.record_serial_params(dut, body.port, body.baudrate)
    return {"ok": True, "mode": body.mode, "log_path": serial_worker.current_log_path}


@router.post("/close")
def close_serial(request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    _dut(request, dut).serial_worker.close()
    return {"ok": True}


@router.post("/send")
def send_serial(body: SerialSendRequest, request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    try:
        _dut(request, dut).serial_worker.send(body.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/terminal/enter")
def enter_terminal(request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    """Switch to interactive raw-terminal mode (sysmon monitoring pauses)."""
    try:
        _dut(request, dut).serial_worker.enter_terminal()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "terminal": True}


@router.post("/terminal/exit")
def exit_terminal(request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    """Resume sysmon monitoring."""
    _dut(request, dut).serial_worker.exit_terminal()
    return {"ok": True, "terminal": False}


@router.post("/terminal/resize")
def resize_terminal(body: SerialResizeRequest, request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    """Tell the DUT shell the terminal size (and optionally TERM) so vi/nano render
    at the right dimensions."""
    try:
        _dut(request, dut).serial_worker.resize_terminal(body.rows, body.cols, body.term)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "rows": body.rows, "cols": body.cols}


@router.post("/wifi/kick")
def kick_wifi_client(body: WifiKickRequest, request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    """Disassociate a Wi-Fi client via `wlanconfig <iface> kickmac <mac>`."""
    if not _IFACE_RE.match(body.iface) or not _KICK_MAC_RE.match(body.mac):
        raise HTTPException(status_code=400, detail="Invalid interface or MAC")
    try:
        _dut(request, dut).serial_worker.capture_command(
            f"wlanconfig {body.iface} kickmac {body.mac}", timeout=5.0
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/efficiency-report")
def get_efficiency_report(request: Request, dut: str = DEFAULT_DUT_ID) -> dict:
    return _dut(request, dut).parser.efficiency_report()


@router.get("/logs/{file_name}")
def download_log(file_name: str) -> FileResponse:
    try:
        safe_name = Path(file_name).name
        if safe_name != file_name:
            raise DownloadWorkflowError("failed to download DUT log: invalid file name", status_code=400)

        source_log_path = LOG_DIR / safe_name
        if not source_log_path.exists() or not source_log_path.is_file():
            raise DownloadWorkflowError("log file not found", status_code=404)

        if should_bypass_analyzer(source_log_path):
            return FileResponse(path=source_log_path, filename=safe_name, media_type="text/plain")

        session_dir = create_dut_session_dir()
        log_path = save_downloaded_log_to_session(file_name=safe_name, session_dir=session_dir)
        ensure_log_has_minimum_snapshots(log_path=log_path)
        bundle_survey_snapshots(session_dir)
        run_analyzer_for_session(session_dir=session_dir)
        zip_path = zip_session_dir(session_dir=session_dir)
    except DownloadWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unexpected error while preparing DUT log bundle: {exc}") from exc

    return FileResponse(
        path=zip_path,
        filename=zip_path.name,
        media_type="application/zip",
    )
