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

### One-command LAN launcher

```bash
./scripts/start_lan.sh          # dev: backend :8000 + Vite :5173
./scripts/start_lan.sh --prod   # prod: build the UI, serve UI+API+WS from the backend on :8000
```

It creates `.venv`, installs backend deps, and (on `--prod`) runs `npm run build`. Overrides:
`BIND_HOST`, `BACKEND_PORT`, `FRONTEND_PORT` (changing `BACKEND_PORT` in dev also needs the
Vite proxy target updated).

### DUTs on a remote Raspberry Pi

A DUT that is not cabled to this machine can be reached over SSH to a Pi that
pipes its serial port through `socat`. Setup, the admin-only API, how to read the
Fleet strip's rows and what the failure modes mean are in
[`docs/fleet-remote-nodes.md`](../docs/fleet-remote-nodes.md) — start there, in
particular for the two prerequisites that are easy to get wrong: the SSH key must
have **no passphrase**, and the Pi must already be a known host.

## Upgrading from a previous version

This is a git-based app (no installer), so updating is **pull → re-run the launcher**:

```bash
cd /path/to/DUT_browser
# stop the running instance (Ctrl-C, or kill the uvicorn pid)
git fetch origin
git checkout CPU_Plots && git pull --ff-only   # or `git checkout phase-N` to pin a version
./scripts/start_lan.sh --prod                   # re-installs backend deps + rebuilds the UI
```

- **Browser:** the build emits hash-named assets, so a normal reload picks up the new UI; hard
  refresh (Cmd/Ctrl-Shift-R) if anything looks stale. Open WebSockets auto-reconnect, so a
  backend restart recovers without F5.
- **No data migration:** the `default` DUT keeps using `logs/snapshots.jsonl`; `logs/duts.json`
  and per-DUT `logs/snapshots-<id>.jsonl` are created on demand (absent = just the default DUT,
  exactly like before). Crash keywords + workspace data (files metadata, bulletin) persist in
  `data/workspace.db` (SQLite, created on demand); UI preferences (accent / baud) live in browser
  localStorage. Existing logs/history survive an upgrade.
- **Dependency changes:** `start_lan.sh` re-runs `pip install` every launch but only runs
  `npm install` when `node_modules` is missing. If an upgrade changes `frontend/package.json`,
  refresh once: `rm -rf frontend/node_modules && ./scripts/start_lan.sh --prod`.
- **Rollback:** each milestone is tagged (`phase-N`) as an anchor — `git checkout phase-<prev>`
  to run the older version, or `git revert <merge_commit>` to undo a merged change on `CPU_Plots`
  without rewriting published history.

## Frontend architecture

```text
frontend/src/
├── main.tsx                      mounts <AppShell/>, imports the design system
├── pages/
│   ├── AppShell.tsx              shell: sidebar (12 sections in 3 groups) + toolbar + DUT switcher; Overview KPIs + chart cards
│   └── Dashboard.tsx             Serial Console (embedded), Critical Crash panel, log download
├── components/
│   ├── shell/                    Sidebar · Topbar · Card · navigation (SectionId, nav groups)
│   ├── charts/                   Sparkline (inline SVG) · ChartData (JSON blob)
│   ├── FleetSection.tsx          fleet card grid (one card per registered DUT)
│   ├── WifiClientsCard.tsx · SsidCapabilityCard.tsx · SiteSurveyCard.tsx
│   │                             on-demand Wi-Fi captures: client tables · capability report · site survey
│   ├── RecommendationPill.tsx · BandRecoSummary.tsx   per-band channel recommendation UI
│   ├── ConsolePanel.tsx          console view + Vim popup command editor
│   ├── TerminalView.tsx          xterm.js interactive terminal (bundled, /ws/term)
│   ├── DownloadsSection.tsx      log list + analyzer artifacts with inline PNG preview
│   ├── FilesSection.tsx · BulletinSection.tsx · AuthorTag.tsx   workspace (files · bulletin · author colours)
│   ├── SettingsSection.tsx       crash-keyword editor + UI preferences
│   └── DutSwitcher.tsx           toolbar DUT selector (registry-backed)
├── monitoring/
│   ├── useDutMonitor.ts          per-DUT /ws monitor → derived state (filters by dut_id)
│   ├── useFleetMonitor.ts        fleet aggregate: one /ws demuxed across all DUTs
│   ├── DutMonitorContext.tsx     shares the one monitor instance app-wide
│   ├── useCrashKeywords.ts       shared crash keywords (GET/PUT /api/settings/crash-keywords) + memoized RegExp
│   ├── crash.ts                  buildCrashPattern + built-in defaults (kernel panic / q6 crash / watchdog)
│   ├── siteSurveyStore.ts · useLastRecommendation.ts · WifiScanContext.tsx   survey cache + recommendation polling
│   └── useAppVersion.ts · useSettings.ts   release-banner poll · UI settings
├── api/  rest.ts · websocket.ts · dut.ts   REST helpers + WS event types / delta application + DUT registry
└── styles/dashboard.css          Luna design tokens + shell/card/chart CSS
```

**Design system.** `styles/dashboard.css` carries the Luna "Spacing –
Dashboards" tokens — spacing scale `--space-1..8`, a single `--accent`,
`--radius`, `--sidebar-w` — and the `.app / .sidebar / .toolbar / .kpi / .card`
chrome. Rebrand by changing only `--accent` / `--accent-weak`.

**Single shared WebSocket monitor.** `useDutMonitor` opens **one** `/ws`
connection for the selected DUT (events are tagged `dut_id`; it ignores other
DUTs' events) and derives:

- `status` — `offline` (WS down) / `idle` (up, no recent stream) / `streaming`
- `lines` — console stream (capped 1000), shared with the Serial Console
- `cpuBusyPct` / `cpuIdlePct` / `coreCount` / `cpuPerCoreBusy` — from the latest snapshot
- `cpuHistory` — one point per snapshot (capped 120), for the trend chart
- `memoryLive` / `memoryHistory` — from the snapshot's streamed `/proc/meminfo`
  (`effectiveKb` = MemAvailable − SUnreclaim, matching the offline analyzer)
- `wifiByRadio` / `wifiClientTotal` — associated clients per radio
- `crashLines` / `crashCount` — console lines matching the built-in crash pattern
- `lastSnapshotTs`

`DutMonitorContext` shares that single instance, so the Overview KPIs, the
charts, **and** the embedded Serial Console all read the same stream — no
duplicate connections.

**Fleet monitor.** `useFleetMonitor` (mounted only while the Fleet section is
visible) opens one additional un-filtered `/ws` and demuxes it per `dut_id`,
keeping only lightweight per-DUT state (latest snapshot base, last activity,
crash count) so the whole fleet updates from a single socket. Crash matching
uses the shared keyword pattern from `useCrashKeywords` (memoized — its
identity must stay stable across renders, or the socket effect would reconnect
in a loop).

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
detection + user lock-in keywords + new/seen badge. The keyword list itself is
editable in **Settings** and persisted server-side
(`GET/PUT /api/settings/crash-keywords`), so every client shares the same
patterns.

## Backend module map

```text
backend/app/
├── main.py                 FastAPI app; wires the DUT registry + ws on startup; /health, /api/version,
│                           /api/whoami, /api/snapshots, /api/console/tail, /api/wifi/*, /api/logs*,
│                           /api/download*, /ws, /ws/term; serves the built SPA in prod
├── config.py               paths (LOG_DIR, ANALYZER_SCRIPT, ANALYZER_OUTPUT_DIR, SNAPSHOT_FILE, DATA_DIR)
├── api/
│   ├── serial_api.py       /api/serial/* (open, close, send, ports, terminal, wifi/kick, logs, efficiency-report)
│   ├── analyzer_api.py     /api/analyzer/* (run, run-session, memory)
│   ├── duts_api.py         /api/duts (dynamic DUT registry CRUD)
│   ├── settings_api.py     /api/settings/crash-keywords
│   ├── files_api.py        /api/files (workspace uploads)
│   └── bulletin_api.py     /api/bulletin/posts (+comments)
├── dut/registry.py         per-DUT runtime contexts (serial worker · parser · snapshot store ·
│                           console buffer · terminal manager), keyed by dut_id; persisted to logs/duts.json
├── db/workspace.py         SQLite schema/connection for data/workspace.db (files · bulletin · settings)
├── serial/serial_worker.py SerialWorker: serial + replay threads; writes raw session log; capture_command
│                           gate for on-demand wifi captures
├── parser/sysmon_parser.py SysMonParser: snapshots / CPU / memory (/proc/meminfo) / wifi clients / batched console
├── services/
│   ├── analyzer_service.py runs analyzer3.py → cpu_usage.csv / memory.csv
│   ├── snapshot_store.py   bounded JSONL snapshot ring; persists + backfills on connect
│   ├── console_buffer.py   recent console lines per DUT (seeds the console on load)
│   ├── survey_cache.py     last site-survey result per DUT (feeds /channel-recommendation/last)
│   └── settings_service.py crash-keyword persistence (workspace.db)
└── websocket/
    ├── ws_manager.py       broadcasts dut_id-tagged events to all /ws clients (thread→loop bridge)
    └── terminal_manager.py raw-byte fan-out for /ws/term (interactive terminal)
```

**Data path.** `SerialWorker` (background thread) reads serial/replay lines,
writes each to `logs/dut-session-*.log`, and feeds them to `SysMonParser`. The
parser emits events through `WebSocketManager.emit_from_thread`, which bridges
to the asyncio loop and broadcasts to every `/ws` client.

**Snapshots are persisted.** `SnapshotStore.observe()` reconstructs full
snapshots from the parser's `snapshot_update` / `snapshot_delta` events, keeps a
bounded in-memory ring (latest 500, keyed by `device_ts`), and appends finalized
snapshots to `logs/snapshots.jsonl` (per-DUT: `logs/snapshots-<id>.jsonl`) so
history survives a backend restart. The file is bounded — `_compact_locked()`
rewrites it from the deduped ring at startup and periodically. On startup
`_load_tail()` reloads recent history, so `recent()` can backfill a freshly
connected `/ws` client instantly. The raw session log remains the source for
offline CPU/memory re-derivation by `analyzer3.py`.

## WebSocket event contracts (`/ws`)

One `/ws` carries **every DUT's** events; each event is tagged with the
originating `dut_id`. The single-DUT monitor keeps only its own DUT's events;
the Fleet page demuxes all of them from one socket.

`console_line` / `console_line_batch`:

```json
{ "type": "console_line", "dut_id": "default", "text": "..." }
{ "type": "console_line_batch", "dut_id": "default", "lines": ["...", "..."] }
```

`snapshot_update` (full) / `snapshot_delta` (incremental; the frontend applies
deltas onto the last full snapshot):

```json
{
  "type": "snapshot_update",
  "dut_id": "default",
  "snapshot": {
    "test_count": 1,
    "device_ts": "2026-02-26 09:46:01",
    "cpu": { "0": { "usr": 1.9, "sys": 2.9, "nic": 0.0, "idle": 80.6, "io": 0.0, "irq": 1.9, "sirq": 12.6 } },
    "memory": { "MemTotal": 1907104, "MemAvailable": 475472, "SUnreclaim": 84212 },
    "wifi_clients": { "5G": { "total_size": 1, "clients": [{ "mac": "AA:..." }] } }
  }
}
```

`wifi_clients_update`:

```json
{
  "type": "wifi_clients_update",
  "dut_id": "default",
  "radio": "5G",
  "total_size": 1,
  "clients": [{ "mac": "AA:...", "ip": "192.168.1.9", "rssi": -42, "snr": 30 }]
}
```

## REST API

Per-DUT endpoints (serial, snapshots, console, wifi, terminal) accept
`?dut=<id>`; omitting it targets the `default` DUT.

| Method | Endpoint | Notes |
|---|---|---|
| `GET` | `/health` | liveness |
| `GET` | `/api/version` | `{version, built_at}` — SPA polls this for the "new release" banner |
| `GET` | `/api/whoami` | caller IP + suggested display name (workspace identity prefill) |
| `GET` | `/api/duts` | list registered DUTs (id, label, mode, serial_open, …) |
| `POST` | `/api/duts` | `{id, label?}` → register a DUT with its own serial/parser/snapshot context |
| `DELETE` | `/api/duts/{dut_id}` | remove a DUT (the `default` DUT is not removable) |
| `POST` | `/api/fleet/nodes` | admin — `{id, label?, host, user, key_path, port?, device?, baudrate?, is_mesh?, backhaul_iface?}` → register a DUT whose console is an SSH + `socat` pipe to a Pi |
| `POST` | `/api/fleet/nodes/{dut_id}/connect` | admin — open that console |
| `POST` | `/api/fleet/nodes/{dut_id}/disconnect` | admin — close it and reap the ssh child |
| `POST` | `/api/fleet/nodes/{dut_id}/rssi` | admin — capture both directions of the mesh backhaul → `{role, uplink, downlink}` |
| `GET` | `/api/snapshots` | recent full snapshots — chart backfill on (re)connect |
| `GET` | `/api/console/tail` | recent console lines — console seeds instantly on load |
| `GET` | `/api/serial/ports` | list serial ports |
| `POST` | `/api/serial/open` | `{mode:"serial"\|"replay", port, baudrate, replay_path, replay_interval_ms}` → `{ok, mode, log_path}` |
| `POST` | `/api/serial/close` | stop the worker |
| `POST` | `/api/serial/send` | `{text}` (serial mode only; `""` = Ctrl-C) |
| `GET` | `/api/serial/logs/{file}` | download log — direct `.log` or analyzer `.zip` (see below) |
| `POST` | `/api/serial/terminal/enter` | switch to raw interactive terminal mode (monitoring pauses); 400 if serial not open |
| `POST` | `/api/serial/terminal/exit` | resume monitoring |
| `POST` | `/api/serial/terminal/resize` | `{rows, cols, term?}` → sets DUT terminal size/type (see below); 400 if not in terminal mode |
| `POST` | `/api/serial/wifi/kick` | kick an associated client off a VAP |
| `GET` | `/api/serial/efficiency-report` | parser counters |
| `GET` | `/api/wifi/clients` · `/api/wifi/client-stats` | on-demand per-client tables (`wlanconfig … list`); serial mode only, briefly pauses sysmon parsing |
| `GET` | `/api/wifi/capabilities` · `/api/wifi/capability-report` | SSID/VAP capability capture + parsed report |
| `GET` | `/api/wifi/site-survey` | site-survey scan: per-band neighbors + channel occupancy |
| `GET` | `/api/wifi/channel-recommendation` | least-occupied channel per band (uses/produces a survey) |
| `GET` | `/api/wifi/channel-recommendation/last` | cached last recommendation — read-only, never scans |
| `POST` | `/api/analyzer/run` · `/api/analyzer/run-session` | `{log_path}` → runs analyzer; returns produced files |
| `GET` | `/api/analyzer/memory` | parsed memory series from the latest analyzer run (post-analysis only) |
| `GET` | `/api/logs` · `/api/logs/tail` | list session logs · tail one |
| `GET` | `/api/download/{file}` | download an artifact from `logs/analyzer_output/` |
| `GET` | `/api/download/preview/{file}` | inline preview (e.g. analyzer PNGs in Downloads) |
| `GET/PUT` | `/api/settings/crash-keywords` | shared crash-keyword list (persisted in `data/workspace.db`) |
| `GET/POST` | `/api/files` · `GET /api/files/{id}/download` · `DELETE /api/files/{id}` | workspace file sharing (uploads in `data/uploads/`) |
| `GET/POST` | `/api/bulletin/posts` · `POST /api/bulletin/posts/{id}/comments` · `DELETE /api/bulletin/posts/{id}` | bulletin board with comments |

### Interactive terminal (xterm.js)

The Serial Console has a **Monitor / Terminal** toggle. "Terminal" drives the DUT
serial console with a bundled xterm.js (no CDN) over a separate **`/ws/term`** raw
byte channel, so `vi` / `nano` work. Entering terminal mode is explicit and
**pauses sysmon monitoring** (CPU/Wi-Fi/crash KPIs freeze on their last values;
raw bytes are still written to the session log) so terminal escape sequences never
reach the parser. Leaving the terminal (button or navigating away) resumes
monitoring. Serial mode only — not replay. Assumes a single controller.

On open and on browser resize, the client sends the xterm grid size to the DUT
via `/api/serial/terminal/resize`, which runs `export TERM=xterm` and
`stty rows R cols C` at the remote prompt so `vi` / `nano` render at the right
size. `stty` errors are suppressed (`2>/dev/null`). **Note:** resizing requires
the DUT shell to have `stty`; some busybox images don't, and their kernel tty
winsize is fixed (e.g. 80×24) — there `TERM=xterm` still applies (correct escape
sequences) but the editor size can't be changed from the dashboard.

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
cd backend && python3 -m pytest -q         # backend test suite
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
