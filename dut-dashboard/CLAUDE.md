# CLAUDE.md — DUT_browser (AP6_monitor)

Guidance every agent session must follow in this repo. Read before editing.

## What this is

Browser-based DUT monitoring dashboard for AP / network-device QA.
**FastAPI** backend (`:8000`) + **React/Vite/TypeScript** frontend (`:5173`),
served over the LAN. It monitors a QCA/Atheros AP6 DUT over a **serial console**
(or a replay log) and parses console output into structured telemetry.

---

## Language rules (strict)

Two separate channels — do not mix them:

- **Chat with the user:** reply in **Traditional Chinese (繁體中文, Taiwan
  conventions)**. Never Simplified characters, never Mainland phrasing.
- **Anything committed to the repo:** **English only.** This covers code,
  identifiers, file/dir paths, comments, docstrings, log/console strings,
  commit subjects + bodies, branch names, and PR descriptions.

Rationale: the existing codebase, CONTRIBUTING.md, and Conventional Commits are
all English, and this is a shared repo (colleagues read it). Keep repo artifacts
monolingual-English for consistency; speak 繁中 to the user.

- Identifiers and paths stay verbatim English even when explaining them in 繁中
  chat (e.g. say「在 `services/wifi_clients.py` 新增 parser」, do not translate the
  path or symbol names).
- Do **not** add Chinese comments or Chinese commit messages to this repo.

---

## Non-negotiable constraints (from CONTRIBUTING.md)

1. **Browser-only.** No Tauri, Rust, `src-tauri`, Electron, or desktop
   packaging. The app is a local web service on the LAN.
2. **Offline-first.** No CDN at runtime. Do **not** add a charting/UI library to
   the render path (no Chart.js, etc.). Charts are hand-rendered **inline SVG**;
   new charts must also emit their source data as
   `<script type="application/json">` for a future migration. Bundled npm deps
   (React, xterm, codemirror) are fine — the ban is on CDN + chart/UI libs.
3. **One WebSocket.** Realtime telemetry reuses the shared `useDutMonitor`
   monitor. Do **not** open new `/ws` connections. On-demand features use REST.
4. **Don't break existing behavior.** Serial console, Critical Crash panel, log
   download, and replay mode must keep working.
5. **Never commit runtime data.** Everything under `dut-dashboard/logs/` is
   gitignored (session logs, `snapshots.jsonl`, analyzer output). Do **not**
   delete `snapshots.jsonl` when testing — it may hold real captured DUT data.

---

## Where code goes (module map)

```
dut-dashboard/
  backend/app/
    parser/       SysMonParser + pydantic models (snapshot / cpu / wifi / console)
    serial/       SerialWorker (background thread; capture_command RPC)
    services/     stateless parsers & stores (wifi_clients.py, console_buffer, ...)
    websocket/    ws_manager (event fan-out)
    api/          REST routers (serial_api, files_api, bulletin_api, duts_api, ...)
    main.py       app wiring + a few direct @app.get endpoints (incl. /api/wifi/*)
    config.py
  frontend/src/
    pages/        Dashboard, AppShell
    components/   shell/ (Card, Sidebar, Topbar, navigation.ts) · charts/ · *Card.tsx
    monitoring/   contexts + hooks (useDutMonitor, *Context.tsx)
    api/          rest.ts (typed REST client) · websocket.ts · dut.ts
    styles/
  tools/          analyzer3.py · log_event_detector.py (offline)
  scripts/        sysMon.sh (DUT-side telemetry)
```

When adding a feature, follow the **existing Wi-Fi Clients data flow** as the
template, do not invent a new pattern:
`services/<x>.py` parser → `main.py` endpoint → `api/rest.ts` types+fn →
`monitoring/<X>Context.tsx` cache → `components/<X>Card.tsx` → register in
`components/shell/navigation.ts`.

---

## On-demand serial RPC discipline (critical)

`SerialWorker.capture_command(cmd, timeout)` is a **synchronous** serial RPC: it
occupies the read loop and **briefly pauses sysmon parsing**. Therefore:

- **Never background-poll** a capture. Trigger only on section entry (once) or an
  explicit user action (button).
- **Coalesce** concurrent captures for the same DUT into one in-flight promise
  (see `WifiScanContext.tsx`).
- **Scope results to a `dutId`** so a scan never leaks across the DUT switcher.
- Wrap each capture in `try/except RuntimeError` (backend) — one VAP/command
  failing must not abort the whole batch. Missing fields parse to `None`, never
  raise.

---

## Build / run / verify

Backend (`dut-dashboard/backend`):
```bash
python3 -m app.main          # run dev server on :8000
pytest                       # run backend tests (backend/tests/test_*.py)
```
Frontend (`dut-dashboard/frontend`):
```bash
npm run dev                  # vite dev server :5173 (proxies /api, /ws to :8000)
npm run typecheck            # tsc --noEmit — MUST pass before committing
npm run build                # production build
```
There is no wired-up linter; **`npm run typecheck` + `pytest` are the gates.**
Keep the tree green (typecheck passes, tests pass) at **every** commit.

---

## Git / commits / PRs

- Branch from **`CPU_Plots`** (the pre-Tauri line). Open PRs against `CPU_Plots`.
  **Never** target `main` or `Tauri`.
- Branch names by type: `feat/...`, `fix/...`, `docs/...`.
- **Conventional Commits**, **atomic** (one logical change per commit), English:
  ```
  type(scope): short imperative subject

  Optional body explaining WHY (not what). Wrap ~72 chars.
  ```
  Types: `feat` `fix` `docs` `refactor` `test` `chore` (see CONTRIBUTING.md).

---

## Pre-commit checklist

- [ ] Repo artifacts are **English** (code, comments, commits, paths).
- [ ] No Tauri/Electron/Rust; no CDN; no chart/UI lib added to render path.
- [ ] No new `/ws`; on-demand work uses REST + `capture_command`.
- [ ] No serial background-polling; captures coalesced + DUT-scoped.
- [ ] Missing parser fields → `None`, no uncaught exceptions.
- [ ] `npm run typecheck` and `pytest` pass.
- [ ] Nothing under `dut-dashboard/logs/` staged; `snapshots.jsonl` intact.
- [ ] Branched from `CPU_Plots`; Conventional Commits; existing features unbroken.
