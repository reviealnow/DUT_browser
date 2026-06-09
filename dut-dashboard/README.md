# DUT Local Monitoring Dashboard

Browser-based DUT monitoring console (FastAPI + React/Vite). This document covers
the run steps, the frontend/backend module map, the WebSocket event contracts,
the REST API, and the offline analyzer / log-download flows.

> Architecture note: **browser-only** — no Tauri, Electron, or desktop
> packaging. See the repo-root [`README.md`](../README.md) for the high-level
> architecture diagram.

## Run

### Backend (`:8000`)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt
cd backend
python3 -m app.main
```

### Frontend (`:5173`)

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173` (local) or `http://<your-server-ip>:5173` (LAN).
Vite proxies `/api` and `/ws` to `127.0.0.1:8000`.

## Frontend architecture

```text
frontend/src/
├── main.tsx                      mounts <AppShell/>, imports the design system
├── pages/
│   ├── AppShell.tsx              shell: sidebar + toolbar + sections; KPIs + chart cards
│   └── Dashboard.tsx             Serial Console (embedded), Critical Crash panel, log download
├── components/
│   ├── shell/                    Sidebar · Topbar · Card/KpiCard/EmptyState · navigation
│   ├── charts/                   Sparkline (inline SVG) · ChartData (JSON blob)
│   ├── ConsolePanel.tsx          console view + Vim popup command editor
│   └── (legacy, unused) CpuChart.tsx · MemoryChart.tsx · ClientsPanel.tsx
├── monitoring/
│   ├── useDutMonitor.ts          single /ws connection → derived monitor state
│   ├── DutMonitorContext.tsx     shares the one monitor instance app-wide
│   └── crash.ts                  built-in CRITICAL_CRASH_PATTERN (kernel panic / Q6 crash / watchdog)
├── api/  rest.ts · websocket.ts  REST helpers + WS event types / delta application
└── styles/dashboard.css          Luna design tokens + shell/card/chart CSS
```

**Design system.** `styles/dashboard.css` carries the Luna "Spacing –
Dashboards" tokens — spacing scale `--space-1..8`, a single `--accent`,
`--radius`, `--sidebar-w` — and the `.app / .sidebar / .toolbar / .kpi / .card`
chrome. Rebrand by changing only `--accent` / `--accent-weak`.

**Single shared WebSocket monitor.** `useDutMonitor` opens **one** `/ws`
connection (it is the only caller of `connectDashboardWebSocket`) and derives:

- `status` — `offline` (WS down) / `idle` (up, no recent stream) / `streaming`
- `lines` — console stream (capped 1000), shared with the Serial Console
- `cpuBusyPct` / `cpuIdlePct` / `coreCount` / `cpuPerCoreBusy` — from the latest snapshot
- `cpuHistory` — one point per snapshot (capped 120), for the trend chart
- `wifiByRadio` / `wifiClientTotal` — associated clients per radio
- `crashLines` / `crashCount` — console lines matching the built-in crash pattern
- `lastSnapshotTs`

`DutMonitorContext` shares that single instance, so the Overview KPIs, the
charts, **and** the embedded Serial Console all read the same stream — no
duplicate connections.

**KPI row + status pill.** Four KPIs (DUT Status, Latest CPU, Wi-Fi Clients,
Crash Events) plus a toolbar status pill, all from the real stream above.

**Inline-SVG charts (offline-first, no Chart.js / no CDN).**

| Card | Component | Data |
|---|---|---|
| CPU trend | `Sparkline` (SVG area + gridlines) | `cpuHistory` busy% |
| Wi-Fi client summary | CSS bar rows | `wifiByRadio` |
| Critical crash / log events | crash feed list | `crashLines` |

Each card also emits a `<script type="application/json">` blob via `ChartData`
(ids: `cpu-trend-data`, `wifi-summary-data`, `crash-events-data`) so a later
swap to Chart.js needs no change to how the data is produced. Every card has an
**empty** state and an **offline** state.

**Serial Console (preserved).** `Dashboard.tsx` is embedded unchanged under the
"Serial Console" section: realtime console, send / Tab / Ctrl-C, Vim popup
editor, `Download DUT Log`, and the Critical Crash panel with live keyword
detection + user lock-in keywords + new/seen badge.

## Backend module map

```text
backend/app/
├── main.py                 FastAPI app; wires parser/serial/ws on startup; /health, /ws, /api/download
├── config.py               paths (LOG_DIR, ANALYZER_SCRIPT, ANALYZER_OUTPUT_DIR, SNAPSHOT_FILE)
├── api/
│   ├── serial_api.py       /api/serial/* (open, close, send, ports, logs, efficiency-report)
│   └── analyzer_api.py     /api/analyzer/run
├── serial/serial_worker.py SerialWorker: serial + replay threads; writes raw session log
├── parser/sysmon_parser.py SysMonParser: snapshots / CPU / wifi clients / batched console
├── services/
│   ├── analyzer_service.py runs analyzer3.py → cpu_usage.csv / memory.csv
│   └── snapshot_store.py   JSONL store (no-op placeholder — see Roadmap)
└── websocket/ws_manager.py broadcasts events to all /ws clients (thread→loop bridge)
```

**Data path.** `SerialWorker` (background thread) reads serial/replay lines,
writes each to `logs/dut-session-*.log`, and feeds them to `SysMonParser`. The
parser emits events through `WebSocketManager.emit_from_thread`, which bridges
to the asyncio loop and broadcasts to every `/ws` client.

**Snapshots are ephemeral.** `SnapshotStore.append()` is currently a no-op, so
nothing is persisted to `logs/snapshots.jsonl`. The only durable artifact is the
raw session log; structured CPU/memory is re-derived offline by `analyzer3.py`.
(Persistence + on-connect backfill are on the Roadmap.)

## WebSocket event contracts (`/ws`)

`console_line` / `console_line_batch`:

```json
{ "type": "console_line", "text": "..." }
{ "type": "console_line_batch", "lines": ["...", "..."] }
```

`snapshot_update` (full) / `snapshot_delta` (incremental; the frontend applies
deltas onto the last full snapshot):

```json
{
  "type": "snapshot_update",
  "snapshot": {
    "test_count": 1,
    "device_ts": "2026-02-26 09:46:01",
    "cpu": { "0": { "usr": 1.9, "sys": 2.9, "nic": 0.0, "idle": 80.6, "io": 0.0, "irq": 1.9, "sirq": 12.6 } },
    "wifi_clients": { "5G": { "total_size": 1, "clients": [{ "mac": "AA:..." }] } }
  }
}
```

`wifi_clients_update`:

```json
{
  "type": "wifi_clients_update",
  "radio": "5G",
  "total_size": 1,
  "clients": [{ "mac": "AA:...", "ip": "192.168.1.9", "rssi": -42, "snr": 30 }]
}
```

## REST API

| Method | Endpoint | Notes |
|---|---|---|
| `GET` | `/health` | liveness |
| `GET` | `/api/serial/ports` | list serial ports |
| `POST` | `/api/serial/open` | `{mode:"serial"\|"replay", port, baudrate, replay_path, replay_interval_ms}` → `{ok, mode, log_path}` |
| `POST` | `/api/serial/close` | stop the worker |
| `POST` | `/api/serial/send` | `{text}` (serial mode only; `""` = Ctrl-C) |
| `GET` | `/api/serial/logs/{file}` | download log — direct `.log` or analyzer `.zip` (see below) |
| `GET` | `/api/serial/efficiency-report` | parser counters |
| `POST` | `/api/analyzer/run` | `{log_path}` → runs analyzer; returns produced files |
| `GET` | `/api/download/{file}` | download an artifact from `logs/analyzer_output/` |

### Serial vs Replay

```bash
# Serial
curl -X POST http://127.0.0.1:8000/api/serial/open \
  -H 'Content-Type: application/json' \
  -d '{"mode":"serial","port":"/dev/ttyUSB0","baudrate":115200}'

# Replay (replay_path is relative to the backend CWD — use an absolute path)
curl -X POST http://127.0.0.1:8000/api/serial/open \
  -H 'Content-Type: application/json' \
  -d '{"mode":"replay","replay_path":"/abs/path/to/session.log","replay_interval_ms":100}'
```

## Download DUT Log + CPU/Memory plots

`GET /api/serial/logs/{file_name}` chooses one of two paths:

1. **Short log, no `TOP`** (< 100 lines): returns the original `.log`
   (`text/plain`); analyzer skipped. Frontend toast: *"The log file is ready."*
2. **Otherwise**: creates `logs/dut-session-YYYYMMDD-HHMMSS/`, copies the log in,
   runs `tools/analyzer3.py` there, zips the directory, and returns the `.zip`
   (`application/zip`). Frontend toast: *"DUT CPU and Memory usage plots are
   created."*

Typical artifacts: `*cpu_usage.csv`, `*memory.csv`, `*cpu_usage_plot.png`,
`*memavailable_plot.png`, `*slab_plot.png`, `*sunreclaim_plot.png`,
`*cpu_spike_report.txt`.

Error handling: invalid filename → `400`; missing log → `404`; too short for
analysis → `422`; analyzer/zip/runtime failure → `500` with detail.

## Analyzer flow (direct)

```bash
curl -X POST http://127.0.0.1:8000/api/analyzer/run \
  -H 'Content-Type: application/json' \
  -d '{"log_path":"logs/session.log"}'

curl -L -o cpu_usage.csv http://127.0.0.1:8000/api/download/cpu_usage.csv
curl -L -o memory.csv     http://127.0.0.1:8000/api/download/memory.csv
```

Artifacts are stored in `logs/analyzer_output/`.

## Log event detector tool

```bash
python3 tools/log_event_detector.py --root . --output log_events.json
```

Produces `log_events.json` (merged abnormal-event detection) and
`tools/example_output.json` (sample). Rules: `tools/README_log_event_detector.md`.

## Develop / verify

```bash
python3 -m compileall backend/app          # backend syntax
cd frontend && npx tsc --noEmit            # frontend types (expect 0 errors)
```

Visual check: run backend + frontend, drive a replay log in the parser's sysmon
format (snapshot markers `= Test Time: N, YYYY-MM-DD HH:MM:SS =`, `CPUn: …% idle …`
lines, `--- CLIENTS Radio=XG ---` + JSON), and confirm the KPIs/charts populate.

> **Port note.** If something else already holds `:8000`, run the backend on an
> alternate port (`python3 -m uvicorn app.main:app --port 8011`) and temporarily
> point the Vite proxy in `frontend/vite.config.ts` at it, then revert to `8000`.

## Migrating charts to Chart.js later

Each chart card already renders the data and emits it as
`<script type="application/json" id="…">`
(`cpu-trend-data`, `wifi-summary-data`, `crash-events-data`). A Chart.js (or
other) renderer can read those blobs with **zero backend change** — vendor the
library locally, never from a CDN (offline-first LAN).
