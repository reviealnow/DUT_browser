import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.analyzer_api import router as analyzer_router
from app.api.bulletin_api import router as bulletin_router
from app.api.duts_api import router as duts_router
from app.api.files_api import router as files_router
from app.api.serial_api import router as serial_router
from app.api.settings_api import router as settings_router
from app.api.workspace_api import router as workspace_router
from app.config import ANALYZER_OUTPUT_DIR, FRONTEND_DIST, LOG_DIR, SURVEY_SNAPSHOT_DIR, UPLOAD_DIR
from app.db.workspace import init_db
from app.dut.registry import DEFAULT_DUT_ID, DutContext, DutRegistry, build_default_registry
from app.services.analyzer_service import AnalyzerService
from app.services.capability_report import build_capability_report
from app.services.site_survey import channel_recommendation, get_site_survey
from app.services.survey_cache import last_recommendation, remember_recommendation
from app.services import survey_snapshot
from app.services.wifi_clients import discover_vaps, get_ssid_capabilities, parse_apstats, parse_wlanconfig_list
from app.services.wifi_survey import get_wifi_survey
from app.websocket.terminal_manager import TerminalManager
from app.websocket.ws_manager import WebSocketManager

app = FastAPI(title="DUT Local Monitoring Dashboard")
app.include_router(serial_router)
app.include_router(analyzer_router)
app.include_router(duts_router)
app.include_router(files_router)
app.include_router(bulletin_router)
app.include_router(settings_router)
app.include_router(workspace_router)


@app.on_event("startup")
async def on_startup() -> None:
    # Workspace module (file sharing + bulletin): create the SQLite schema and
    # the upload directory up front so the first request never races them.
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    ws_manager = WebSocketManager()
    ws_manager.bind_loop(loop)

    # Shared, cross-DUT services.
    app.state.ws_manager = ws_manager
    app.state.analyzer_service = AnalyzerService()

    # Per-DUT runtime; A0 registers the single default DUT (behaviour unchanged).
    app.state.dut_registry = build_default_registry(ws_manager=ws_manager, loop=loop)

    # Rebuild the in-memory recommendation cache from persisted survey snapshots
    # so Overview / Fleet band badges survive a restart with no new scan.
    survey_snapshot.restore_cache()


def resolve_dut(app_, dut_id: str) -> DutContext:
    """Look up a DUT context or raise 404 for an unknown id."""
    try:
        return app_.state.dut_registry.get(dut_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown DUT: {dut_id}") from exc


@app.get("/health")
def health() -> dict:
    return {"ok": True, "phase": "milestone-4"}


def _resolve_version() -> str:
    """Resolve the running build version, preferring an explicit deploy stamp.

    `DUT_APP_VERSION` (set by start_lan.sh at deploy) wins; otherwise fall back to
    `git describe` so a dev checkout still reports something useful; "dev" if even
    that is unavailable. Called once at import — never per request."""
    env = os.environ.get("DUT_APP_VERSION", "").strip()
    if env:
        return env
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=Path(__file__).resolve().parent,
        )
        described = result.stdout.strip()
        if result.returncode == 0 and described:
            return described
    except Exception:
        return "dev"
    return "dev"


# Resolved once at module import (no per-request shell-out). The frontend records
# this on first load and polls /api/version to detect a redeploy.
APP_VERSION = _resolve_version()
BUILT_AT = os.environ.get("DUT_BUILT_AT", "").strip() or datetime.now().isoformat(timespec="seconds")


@app.get("/api/version")
def get_version() -> dict:
    """Current build version + build time, so an open SPA can detect a redeploy."""
    return {"version": APP_VERSION, "built_at": BUILT_AT}


def _suggested_name(ip: str) -> str:
    """A friendly default display name derived from the caller's LAN IP.

    `Guest-<last-octet>` for a dotted IPv4 (e.g. 192.168.30.164 -> Guest-164),
    else the raw host (covers IPv6 / unknown). Lets the Workspace pre-fill an
    identity nobody has to type, while staying overridable.
    """
    octets = ip.split(".")
    if len(octets) == 4 and octets[-1].isdigit():
        return f"Guest-{octets[-1]}"
    return f"Guest-{ip}" if ip else "Guest"


@app.get("/api/whoami")
def whoami(request: Request) -> dict:
    """The caller's IP + a suggested display name, so the Workspace can pre-fill an
    identity. Behind the dev vite proxy this is 127.0.0.1; on the LAN/prod single
    port it is the real client IP."""
    ip = request.client.host if request.client else ""
    return {"ip": ip, "name": _suggested_name(ip)}


@app.get("/api/snapshots")
def get_snapshots(limit: int = 120, dut: str = DEFAULT_DUT_ID) -> dict:
    """Recent full snapshots for instant frontend chart backfill on (re)connect."""
    limit = max(1, min(limit, 500))
    snapshots = resolve_dut(app, dut).snapshot_store.recent(limit)
    return {"snapshots": snapshots}


@app.get("/api/console/tail")
def get_console_tail(limit: int = 500, dut: str = DEFAULT_DUT_ID) -> dict:
    """Recent console lines so the Serial Console seeds instantly on (re)load."""
    limit = max(1, min(limit, 500))
    lines = resolve_dut(app, dut).console_buffer.recent(limit)
    return {"lines": lines}


@app.get("/api/wifi/clients")
def get_wifi_clients(dut: str = DEFAULT_DUT_ID) -> dict:
    """On-demand per-client Wi-Fi detail: discover active VAPs (iwconfig) then run
    `wlanconfig <vap> list` for each and parse the association tables. Serial mode
    only; briefly pauses sysmon parsing during the captures."""
    worker = resolve_dut(app, dut).serial_worker
    try:
        iwconfig_text = worker.capture_command("iwconfig", timeout=6.0)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    vaps = discover_vaps(iwconfig_text)
    clients: list[dict] = []
    for vap in vaps:
        try:
            out = worker.capture_command(f"wlanconfig {vap['iface']} list", timeout=6.0)
        except RuntimeError:
            continue
        for client in parse_wlanconfig_list(out, vap["iface"]):
            client["ssid"] = vap["ssid"]
            clients.append(client)

    return {
        "clients": clients,
        "vaps": vaps,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


@app.get("/api/wifi/client-stats")
def get_wifi_client_stats(mac: str, dut: str = DEFAULT_DUT_ID) -> dict:
    """On-demand per-client deep stats via `apstats -s -m <mac>` (Tx/Rx bytes,
    throughput, channel width, NSS, Rx RSSI, PER). Serial mode only."""
    if not _MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="Invalid MAC")
    worker = resolve_dut(app, dut).serial_worker
    try:
        out = worker.capture_command(f"apstats -s -m {mac}", timeout=6.0)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "mac": mac,
        "stats": parse_apstats(out),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/wifi/survey")
def wifi_survey() -> dict:
    """Host-side on-demand Wi-Fi scan (iw / nmcli). Requires SURVEY_WIFI_IFACE env var.
    Returns available:false when the host has no suitable interface or permission."""
    return get_wifi_survey()


@app.get("/api/wifi/capabilities")
def get_wifi_capabilities(dut: str = DEFAULT_DUT_ID) -> dict:
    """On-demand per-VAP SSID capability: iw dev (BSSID/freq) + iwconfig (generation)
    + /etc/hostapd*.conf (security/PMF/k/v/r). Serial mode only."""
    worker = resolve_dut(app, dut).serial_worker
    try:
        caps = get_ssid_capabilities(worker)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ssids": caps,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/wifi/capability-report")
def get_wifi_capability_report(dut: str = DEFAULT_DUT_ID) -> dict:
    """Reconcile DUT SSID config (Source A: serial) vs host-side scan (Source B: iw/nmcli).

    Source A requires an open serial connection; Source B requires SURVEY_WIFI_IFACE.
    Both failures are surfaced in the response without raising (available_b=false or
    an HTTP 400 for Source A serial errors).
    """
    worker = resolve_dut(app, dut).serial_worker
    try:
        ssids = get_ssid_capabilities(worker)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    survey = get_wifi_survey()
    captured_at_a = datetime.now().isoformat(timespec="seconds")
    report = build_capability_report(ssids, survey)
    report["captured_at_a"] = captured_at_a
    return report


def _survey_progress_emitter(dut: str):
    """Bridge get_site_survey's on_progress callback onto the shared /ws.

    The survey runs inside a threadpool-executed sync endpoint, so events go out
    via emit_from_thread (same path the parser uses), tagged with dut_id like
    every other /ws event. Frontends that don't know "survey_progress" ignore
    unknown event types, so this is additive."""
    ws_manager: WebSocketManager = app.state.ws_manager

    def emit(progress: dict) -> None:
        ws_manager.emit_from_thread({"type": "survey_progress", "dut_id": dut, **progress})

    return emit


@app.get("/api/wifi/site-survey")
def get_wifi_site_survey(dut: str = DEFAULT_DUT_ID) -> dict:
    """On-demand DUT-side neighbor scan: `iw dev <vap> scan` per active VAP.
    Serial mode only; off-channel scans are slower than other captures.
    Progress is broadcast as survey_progress events on /ws while it runs."""
    worker = resolve_dut(app, dut).serial_worker
    try:
        return get_site_survey(worker, on_progress=_survey_progress_emitter(dut))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/wifi/channel-recommendation")
def get_wifi_channel_recommendation(dut: str = DEFAULT_DUT_ID) -> dict:
    """Recommend the least-occupied channel per band from the DUT's own site
    survey, reconciled against its own SSID config (current channel per band).
    Also echoes the raw survey (neighbors/vaps) so the frontend can render both
    the recommendation and the underlying table from a single scan — a second
    call to /api/wifi/site-survey would re-run the (slow, off-channel) scan.
    Serial mode only; both captures happen on this request. The result is cached
    per DUT so read-only surfaces (Overview / Fleet) can show it without a new
    scan — see /api/wifi/channel-recommendation/last.
    Progress is broadcast as survey_progress events on /ws while it runs
    (a "capabilities" stage for the config capture, then the per-VAP scan)."""
    worker = resolve_dut(app, dut).serial_worker
    notify = _survey_progress_emitter(dut)
    notify({"stage": "capabilities", "iface": None, "index": 0, "total": 0})
    try:
        own_vaps = get_ssid_capabilities(worker)
        survey = get_site_survey(worker, on_progress=notify)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    recommendations = channel_recommendation(survey["neighbors"], own_vaps)
    remember_recommendation(dut, recommendations, survey["captured_at"])
    # Persist the survey to disk (json+csv) for Downloads / log-ZIP bundling and
    # restart restore. Best-effort: a write failure must never fail the scan.
    try:
        survey_snapshot.write_snapshot(
            dut, recommendations, survey["neighbors"], survey["vaps"], survey["captured_at"]
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logging.getLogger(__name__).exception("failed to persist survey snapshot for %s", dut)
    return {
        "recommendations": recommendations,
        "neighbors": survey["neighbors"],
        "survey_vaps": survey["vaps"],
        "captured_at": survey["captured_at"],
    }


@app.get("/api/wifi/channel-recommendation/last")
def get_wifi_channel_recommendation_last(dut: str = DEFAULT_DUT_ID) -> dict:
    """Return the last cached channel recommendation for a DUT — no scan, no
    serial gate. Populated by /api/wifi/channel-recommendation (the connect-time
    prescan or a manual Re-scan). Lets the Overview mini-card and Fleet grid show
    the latest per-band recommendation without triggering the slow off-channel
    scan. Returns {recommendations: [], captured_at: null, cached: false} when the
    DUT has never been surveyed."""
    cached = last_recommendation(dut)
    if cached is None:
        return {"recommendations": [], "captured_at": None, "cached": False}
    return {**cached, "cached": True}


@app.get("/api/logs")
def list_logs() -> dict:
    """Browse saved artifacts: DUT session logs and analyzer outputs (read-only,
    newest first). Download session logs via /api/serial/logs/{name} and analyzer
    outputs via /api/download/{name}."""

    def entries(paths) -> list[dict]:
        items: list[dict] = []
        for path in paths:
            try:
                if not path.is_file() or path.name.startswith("."):
                    continue
                stat = path.stat()
                items.append(
                    {
                        "name": path.name,
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    }
                )
            except OSError:
                continue
        return sorted(items, key=lambda item: item["mtime"], reverse=True)

    sessions = entries(LOG_DIR.glob("dut-session-*.log")) if LOG_DIR.is_dir() else []
    artifacts = entries(ANALYZER_OUTPUT_DIR.glob("*")) if ANALYZER_OUTPUT_DIR.is_dir() else []
    surveys = survey_snapshot.list_snapshots()
    return {"sessions": sessions, "artifacts": artifacts, "surveys": surveys}


@app.get("/api/download/{file_name}")
def download_file(file_name: str) -> FileResponse:
    safe_name = Path(file_name).name
    if safe_name != file_name:
        raise HTTPException(status_code=400, detail="Invalid file name")

    file_path = ANALYZER_OUTPUT_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=file_path, filename=safe_name, media_type="application/octet-stream")


@app.get("/api/download/survey/{file_name}")
def download_survey(file_name: str) -> FileResponse:
    """Serve a persisted site-survey snapshot (json or csv) as a download.
    Name validated against traversal; only .json/.csv under SURVEY_SNAPSHOT_DIR."""
    safe_name = Path(file_name).name
    if safe_name != file_name or not safe_name.lower().endswith((".json", ".csv")):
        raise HTTPException(status_code=400, detail="Invalid file name")

    file_path = SURVEY_SNAPSHOT_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=file_path, filename=safe_name, media_type="application/octet-stream")


@app.get("/api/download/preview/{file_name}")
def preview_file(file_name: str) -> FileResponse:
    """Serve an analyzer PNG plot inline (image/png, no attachment) so it can
    render in an <img> for the Downloads preview. PNG-only, under
    ANALYZER_OUTPUT_DIR, name validated against traversal."""
    safe_name = Path(file_name).name
    if safe_name != file_name or not safe_name.lower().endswith(".png"):
        raise HTTPException(status_code=400, detail="Invalid preview name")

    file_path = ANALYZER_OUTPUT_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=file_path, media_type="image/png")


_TAIL_CAP_BYTES = 256 * 1024


def _tail_lines(path: Path, lines: int, cap: int = _TAIL_CAP_BYTES) -> tuple[list[str], bool]:
    """Read at most the last `cap` bytes of `path` and return its last `lines`
    lines plus whether the file was truncated (we did not start at offset 0)."""
    size = path.stat().st_size
    start = max(0, size - cap)
    with path.open("rb") as handle:
        handle.seek(start)
        chunk = handle.read()
    text = chunk.decode("utf-8", errors="replace")
    parts = text.splitlines()
    # Drop a partial first line when we did not read from the start.
    if start > 0 and parts:
        parts = parts[1:]
    truncated = start > 0 or len(parts) > lines
    return parts[-lines:], truncated


@app.get("/api/logs/tail")
def tail_log(name: str, lines: int = 200) -> dict:
    """Read the last `lines` lines of a session log (dut-session-*.log) for an
    in-place peek in the Downloads view. Read-only, bounded, session logs only."""
    safe_name = Path(name).name
    if safe_name != name:
        raise HTTPException(status_code=400, detail="Invalid file name")
    if not (safe_name.startswith("dut-session-") and safe_name.endswith(".log")):
        raise HTTPException(status_code=400, detail="Not a session log")

    log_path = LOG_DIR / safe_name
    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    lines = max(1, min(lines, 2000))
    tail, truncated = _tail_lines(log_path, lines)
    return {"name": safe_name, "lines": tail, "truncated": truncated}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    manager: WebSocketManager = app.state.ws_manager
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


@app.websocket("/ws/term")
async def terminal_endpoint(ws: WebSocket) -> None:
    """Raw interactive serial terminal. Output is broadcast via TerminalManager;
    input (keystrokes) is written straight to the serial port. Mode switching is
    explicit (POST /api/serial/terminal/enter|exit), so this only carries bytes."""
    dut_id = ws.query_params.get("dut", DEFAULT_DUT_ID)
    try:
        context = app.state.dut_registry.get(dut_id)
    except KeyError:
        await ws.close(code=1008)
        return
    terminal: TerminalManager = context.terminal_manager
    worker = context.serial_worker
    await terminal.connect(ws)
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is None and message.get("text") is not None:
                data = message["text"].encode("utf-8", errors="ignore")
            if not data:
                continue
            try:
                worker.write_raw(data)
            except Exception:
                # Serial not open / not in terminal mode: ignore the keystroke.
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        terminal.disconnect(ws)


# Serve the built frontend (single-port production). Mounted LAST and only when
# the build exists, so /api/* and /ws (registered above) keep priority and dev
# mode (no dist/ -> Vite serves the UI) is unaffected.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
