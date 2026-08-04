import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.analyzer_api import router as analyzer_router
from app.api.auth_api import router as auth_router
from app.api.bulletin_api import router as bulletin_router
from app.api.duts_api import router as duts_router
from app.api.firmware_api import router as firmware_router
from app.api.files_api import router as files_router
from app.api.serial_api import router as serial_router
from app.api.settings_api import router as settings_router
from app.api.workspace_api import router as workspace_router
from app.config import ANALYZER_OUTPUT_DIR, FRONTEND_DIST, LOG_DIR, SURVEY_SNAPSHOT_DIR, UPLOAD_DIR
from app.db.workspace import init_db
from app.dut.registry import DEFAULT_DUT_ID, DutContext, DutRegistry, build_default_registry
from app.services import auth_service
from app.services.analyzer_service import AnalyzerService
from app.services.capability_report import build_capability_report
from app.services.site_survey import channel_recommendation, get_site_survey
from app.services.survey_cache import last_recommendation, remember_recommendation
from app.services import context_snapshot, survey_snapshot
from app.services.wifi_clients import discover_vaps, get_ssid_capabilities, parse_apstats, parse_wlanconfig_list
from app.services.wifi_survey import get_wifi_survey
from app.websocket.terminal_manager import TerminalManager
from app.websocket.ws_manager import WebSocketManager

app = FastAPI(title="DUT Local Monitoring Dashboard")

# Role map (P71a, workspace split revised for P71b). Everything that drives the
# DUT, reaches the filesystem or exposes workspace content (files, bulletin and
# the tag search that spans both) is engineer+; read-only telemetry stays open
# so the dashboard keeps working for an unregistered guest browser. Gates live
# here rather than in each router so the whole policy reads in one place. Two
# routers split per-route instead, because their GET and their writes serve
# different audiences: settings_api (open GET so guest crash detection uses the
# same keyword list as engineers, engineer PUT) and duts_api (open GET so the
# switcher still lists DUTs for a guest, engineer POST/DELETE).
_ENGINEER = Depends(auth_service.require_role("engineer"))

app.include_router(auth_router)
app.include_router(serial_router, dependencies=[_ENGINEER])
app.include_router(analyzer_router, dependencies=[_ENGINEER])
app.include_router(duts_router)
# firmware_api gates per-route: admin for everything except the image fetch,
# which the DUT's cookieless curl authorises with a single-use token.
app.include_router(firmware_router)
app.include_router(files_router, dependencies=[_ENGINEER])
app.include_router(bulletin_router, dependencies=[_ENGINEER])
app.include_router(settings_router)
app.include_router(workspace_router, dependencies=[_ENGINEER])


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


def _persist_scan(kind: str, dut: str, write) -> None:
    """Persist an on-demand scan's result to disk, invisibly to the caller.

    On-demand scans are the Wi-Fi context that actually succeeds. The
    connect-time capture races sysMon for the serial line and loses — contract
    §1, measured on the 40-hour reference run: 11 retries over 97 s, zero
    round-trips — but a scan the operator triggers does get through. Until now
    its result lived only in the frontend, so a bundle downloaded afterwards
    still carried no Wi-Fi context. Writing it through means one successful scan
    anywhere in a run is enough to put a real snapshot in the ZIP.

    Strictly best-effort and strictly invisible: every response is built from
    the same values whether this writes, fails, or writes nothing, so no caller
    can tell it happened. A write failure is a warning here, never an error the
    operator's scan has to absorb.

    Empty results write nothing — that guard lives in the writers
    (`context_snapshot.write_capture`, `survey_snapshot.write_snapshot`), not
    here. No skip marker is written either: a marker asserts "the connect-time
    capture ran and produced nothing", so minting one from a read-only GET would
    put invented "skipped" lines into a session's capture report.
    """
    try:
        write()
    except Exception:  # noqa: BLE001 — write-through must never fail a scan
        logging.getLogger(__name__).warning(
            "failed to persist %s scan for %s", kind, dut, exc_info=True
        )


@app.get("/api/wifi/clients")
def get_wifi_clients(dut: str = DEFAULT_DUT_ID) -> dict:
    """On-demand per-client Wi-Fi detail: discover active VAPs (iwconfig) then run
    `wlanconfig <vap> list` for each and parse the association tables. Serial mode
    only; briefly pauses sysmon parsing during the captures.

    A non-empty result is also persisted as a wifi-clients snapshot, so a log
    downloaded later carries it (see _persist_scan)."""
    worker = resolve_dut(app, dut).serial_worker
    try:
        clients, vaps = _capture_clients(worker)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    captured_at = datetime.now().isoformat(timespec="seconds")
    _persist_scan(
        context_snapshot.WIFI_CLIENTS,
        dut,
        lambda: context_snapshot.write_clients(dut, clients, vaps, captured_at),
    )
    return {
        "clients": clients,
        "vaps": vaps,
        "captured_at": captured_at,
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
    + /etc/hostapd*.conf (security/PMF/k/v/r). Serial mode only.

    A non-empty result is also persisted as an ssid-capability snapshot (see
    _persist_scan)."""
    worker = resolve_dut(app, dut).serial_worker
    try:
        caps = get_ssid_capabilities(worker)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    captured_at = datetime.now().isoformat(timespec="seconds")
    _persist_scan(
        context_snapshot.SSID_CAPABILITY,
        dut,
        lambda: context_snapshot.write_capability(dut, caps, captured_at),
    )
    return {
        "ssids": caps,
        "captured_at": captured_at,
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
    # Source A is the same capture /api/wifi/capabilities persists, so persist it
    # here too — the operator who reconciles is as likely to be the only one who
    # scanned during a run as the operator who just listed capabilities.
    _persist_scan(
        context_snapshot.SSID_CAPABILITY,
        dut,
        lambda: context_snapshot.write_capability(dut, ssids, captured_at_a),
    )
    report = build_capability_report(ssids, survey)
    report["captured_at_a"] = captured_at_a
    return report


def _capture_clients(worker) -> tuple[list[dict], list[dict]]:
    """Associated clients per active VAP — the same two-step capture the
    /api/wifi/clients endpoint performs, factored out so the connect-time
    context capture does not duplicate it."""
    vaps = discover_vaps(worker.capture_command("iwconfig", timeout=6.0))
    clients: list[dict] = []
    for vap in vaps:
        try:
            out = worker.capture_command(f"wlanconfig {vap['iface']} list", timeout=6.0)
        except RuntimeError:
            continue
        for client in parse_wlanconfig_list(out, vap["iface"]):
            client["ssid"] = vap["ssid"]
            clients.append(client)
    return clients, vaps


@app.post("/api/wifi/context-capture", dependencies=[_ENGINEER])
def capture_dut_context(dut: str = DEFAULT_DUT_ID) -> dict:
    """Persist the DUT's Wi-Fi clients and SSID capability as connect-time context.

    Fired once when a DUT is connected, alongside the site-survey prescan, so a
    log downloaded later carries what the site looked like on arrival. sysMon
    logs both of these every step as a time series — that remains the primary
    source; this is the fixed arrival reference point.

    Engineer-gated rather than open like the sibling read-only /api/wifi routes:
    it writes to disk, and the only thing that triggers it (connecting a DUT) is
    engineer-gated already.

    Never raises: each kind is captured and written independently and reports its
    own outcome, so one failure (or no serial at all) neither blocks the other
    nor surfaces as a failed connect.

    A capture that comes back empty writes no snapshot at all (see
    context_snapshot.write_capture) and is reported as not-ok; its reason is
    persisted as a skip marker so the session's bundle can still explain the
    absence months later.
    """
    captures: list[dict] = []
    try:
        worker = resolve_dut(app, dut).serial_worker
    except HTTPException:
        raise
    captured_at = datetime.now().isoformat(timespec="seconds")
    # sysMon saturates the console for a whole run, so a capture racing it gets
    # no round-trip and comes back empty rather than failing. Stated as a cause,
    # not a guess: it is what the 40-hour reference log shows (contract §1).
    empty_reason = "empty payload (serial line busy or capture timed out)"

    def note_skip(kind: str, reason: str) -> None:
        """Persist why a kind produced no snapshot, for context/capture-report.txt."""
        try:
            context_snapshot.write_skip(kind, dut, reason, captured_at)
        except Exception:  # noqa: BLE001 — explaining a skip must never fail a connect
            logging.getLogger(__name__).exception("failed to record %s skip for %s", kind, dut)

    def record(kind: str, capture) -> None:
        try:
            paths = capture()
        except Exception as exc:  # noqa: BLE001 — context capture is best-effort
            logging.getLogger(__name__).warning("context capture %s failed for %s: %s", kind, dut, exc)
            note_skip(kind, str(exc))
            captures.append({"kind": kind, "ok": False, "error": str(exc), "files": []})
        else:
            if not paths:
                logging.getLogger(__name__).info("context capture %s empty for %s", kind, dut)
                note_skip(kind, empty_reason)
                captures.append({"kind": kind, "ok": False, "error": empty_reason, "files": []})
            else:
                captures.append(
                    {"kind": kind, "ok": True, "error": None, "files": [p.name for p in paths]}
                )

    def clients_capture() -> list[Path]:
        clients, vaps = _capture_clients(worker)
        return context_snapshot.write_clients(dut, clients, vaps, captured_at)

    def capability_capture() -> list[Path]:
        return context_snapshot.write_capability(dut, get_ssid_capabilities(worker), captured_at)

    record(context_snapshot.WIFI_CLIENTS, clients_capture)
    record(context_snapshot.SSID_CAPABILITY, capability_capture)
    return {"dut": dut, "captured_at": captured_at, "captures": captures}


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
    Progress is broadcast as survey_progress events on /ws while it runs.

    A non-empty scan is also persisted as a site-survey snapshot (see
    _persist_scan), the same file shape /api/wifi/channel-recommendation
    writes."""
    worker = resolve_dut(app, dut).serial_worker
    try:
        survey = get_site_survey(worker, on_progress=_survey_progress_emitter(dut))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # No recommendation is computed here: it needs the DUT's own SSID capability
    # (channel_recommendation's own_vaps), and buying that with a second serial
    # round-trip would change what this endpoint costs on the wire. The snapshot
    # records the scan and says so with recommendation_computed=False, which is
    # what keeps its empty list out of the recommendation cache on restart —
    # an *absence*, not the empty answer a real computation can legitimately give.
    _persist_scan(
        context_snapshot.SITE_SURVEY,
        dut,
        lambda: survey_snapshot.write_snapshot(
            dut, [], survey["neighbors"], survey["vaps"], survey["captured_at"],
            recommendation_computed=False,
        ),
    )
    return survey


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
    # recommendation_computed=True even when `recommendations` is empty: this
    # request ran channel_recommendation, so an empty result means "no own VAPs
    # right now", which is a current answer and must beat an older non-empty one
    # on restart rather than being mistaken for a missing computation.
    _persist_scan(
        context_snapshot.SITE_SURVEY,
        dut,
        lambda: survey_snapshot.write_snapshot(
            dut, recommendations, survey["neighbors"], survey["vaps"], survey["captured_at"],
            recommendation_computed=True,
        ),
    )
    # This request also captured the DUT's own SSID capability on its way to the
    # recommendation; it is a real measurement whether or not the caller wanted
    # the survey, so it is persisted like the survey is.
    _persist_scan(
        context_snapshot.SSID_CAPABILITY,
        dut,
        lambda: context_snapshot.write_capability(dut, own_vaps, survey["captured_at"]),
    )
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


@app.get("/api/logs", dependencies=[_ENGINEER])
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

    session_paths = list(LOG_DIR.glob("dut-session-*.log")) if LOG_DIR.is_dir() else []
    sessions = entries(session_paths)
    artifacts = entries(ANALYZER_OUTPUT_DIR.glob("*")) if ANALYZER_OUTPUT_DIR.is_dir() else []
    surveys = survey_snapshot.list_snapshots()

    # Which connect-time captures fall inside each session log's own time window.
    # Listed in full rather than counted: the count alone told an operator that
    # three files existed without saying which, and the flat tables below mix
    # every session and DUT together, so matching them up was manual. Site
    # surveys are included here (unlike list_snapshots) because "why this
    # channel" is exactly what a session's own context has to answer.
    #
    # Inline rather than a per-row fetch: the snapshot index is already built
    # once for the whole listing, so the selection costs nothing extra, and a
    # session carries a handful of files, not a heavy payload.
    snapshot_index = context_snapshot.snapshot_entries()
    by_name = {path.name: path for path in session_paths}
    for session in sessions:
        session["context"] = context_snapshot.describe(
            context_snapshot.select_entries_for_session(
                by_name[session["name"]], entries=snapshot_index
            )
        )

    return {
        "sessions": sessions,
        "artifacts": artifacts,
        "surveys": surveys,
        "context": context_snapshot.list_snapshots(),
    }


@app.get("/api/download/{file_name}", dependencies=[_ENGINEER])
def download_file(file_name: str) -> FileResponse:
    safe_name = Path(file_name).name
    if safe_name != file_name:
        raise HTTPException(status_code=400, detail="Invalid file name")

    file_path = ANALYZER_OUTPUT_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=file_path, filename=safe_name, media_type="application/octet-stream")


@app.get("/api/download/survey/{file_name}", dependencies=[_ENGINEER])
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


@app.get("/api/download/context/{kind}/{file_name}", dependencies=[_ENGINEER])
def download_context(kind: str, file_name: str) -> FileResponse:
    """Serve a persisted connect-time context capture (json or csv) as a download.
    The kind must be a known one (which fixes the directory) and the name is
    validated against traversal, exactly like /api/download/survey."""
    try:
        directory = context_snapshot.dir_for(kind)
    except KeyError:
        raise HTTPException(status_code=400, detail="Invalid context kind") from None

    safe_name = Path(file_name).name
    if safe_name != file_name or not safe_name.lower().endswith((".json", ".csv")):
        raise HTTPException(status_code=400, detail="Invalid file name")

    file_path = directory / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=file_path, filename=safe_name, media_type="application/octet-stream")


@app.get("/api/download/preview/{file_name}", dependencies=[_ENGINEER])
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


@app.get("/api/logs/tail", dependencies=[_ENGINEER])
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
    explicit (POST /api/serial/terminal/enter|exit), so this only carries bytes.

    Keystrokes reach the DUT shell, so this needs the same engineer role as the
    REST serial API. The cookie is checked before accept(), so an unauthorised
    socket is never opened. Note the consequence for the client: refusing a
    handshake surfaces in the browser as a generic failure (close code 1006),
    not as 1008 — ask /api/auth/me for the reason rather than reading the code."""
    user = auth_service.user_from_cookie_header(ws.headers.get("cookie"))
    if user is None or auth_service.role_rank(user["role"]) < auth_service.role_rank("engineer"):
        await ws.close(code=1008)
        return

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
