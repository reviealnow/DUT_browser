import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.analyzer_api import router as analyzer_router
from app.api.duts_api import router as duts_router
from app.api.serial_api import router as serial_router
from app.config import ANALYZER_OUTPUT_DIR, FRONTEND_DIST, LOG_DIR
from app.dut.registry import DEFAULT_DUT_ID, DutContext, DutRegistry, build_default_registry
from app.services.analyzer_service import AnalyzerService
from app.services.wifi_clients import discover_vaps, parse_wlanconfig_list
from app.websocket.terminal_manager import TerminalManager
from app.websocket.ws_manager import WebSocketManager

app = FastAPI(title="DUT Local Monitoring Dashboard")
app.include_router(serial_router)
app.include_router(analyzer_router)
app.include_router(duts_router)


@app.on_event("startup")
async def on_startup() -> None:
    loop = asyncio.get_running_loop()
    ws_manager = WebSocketManager()
    ws_manager.bind_loop(loop)

    # Shared, cross-DUT services.
    app.state.ws_manager = ws_manager
    app.state.analyzer_service = AnalyzerService()

    # Per-DUT runtime; A0 registers the single default DUT (behaviour unchanged).
    app.state.dut_registry = build_default_registry(ws_manager=ws_manager, loop=loop)


def resolve_dut(app_, dut_id: str) -> DutContext:
    """Look up a DUT context or raise 404 for an unknown id."""
    try:
        return app_.state.dut_registry.get(dut_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown DUT: {dut_id}") from exc


@app.get("/health")
def health() -> dict:
    return {"ok": True, "phase": "milestone-4"}


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
    return {"sessions": sessions, "artifacts": artifacts}


@app.get("/api/download/{file_name}")
def download_file(file_name: str) -> FileResponse:
    safe_name = Path(file_name).name
    if safe_name != file_name:
        raise HTTPException(status_code=400, detail="Invalid file name")

    file_path = ANALYZER_OUTPUT_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=file_path, filename=safe_name, media_type="application/octet-stream")


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
