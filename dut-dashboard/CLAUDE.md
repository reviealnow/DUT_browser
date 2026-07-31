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
5. **Never commit runtime data.** Everything under `dut-dashboard/logs/` and
   `dut-dashboard/data/` is gitignored (session logs, `snapshots.jsonl`,
   analyzer output, `workspace.db`, uploads, secrets). Do **not** delete
   `snapshots.jsonl` when testing — it may hold real captured DUT data.
6. **Don't weaken auth.** The app has roles (`guest` / `engineer` / `admin`).
   Gating lives in **one place** — `main.py`'s
   `include_router(..., dependencies=[...])` plus a few `@app.get` decorators —
   so add a new router there rather than sprinkling checks into endpoints.
   Authorship of uploads, posts and comments comes from the **session**, never
   from a client-supplied name; rows without a session id are surfaced as
   *unverified* rather than backfilled into looking trustworthy. Secrets
   (session secret, role passcodes, DUT credentials) are configuration, never
   source, and no endpoint returns a password.

---

## Where code goes (module map)

```
dut-dashboard/
  backend/app/
    parser/       SysMonParser + pydantic models (snapshot / cpu / wifi / console)
    serial/       SerialWorker (background thread; capture_command RPC)
    services/     stateless parsers & stores (wifi_clients.py, console_buffer,
                  auth_service, invite_service, file_service, firmware_service, ...)
    db/           workspace SQLite schema + _ensure_column migrations
    dut/          DUT registry (per-DUT context: worker, parser, buffers)
    websocket/    ws_manager (event fan-out)
    api/          REST routers (serial_api, files_api, bulletin_api, duts_api,
                  auth_api, firmware_api, ...)
    main.py       app wiring, role gating, a few direct @app.get endpoints (incl. /api/wifi/*)
    config.py
  frontend/src/
    pages/        Dashboard, AppShell
    components/   shell/ (Card, Sidebar, Topbar, navigation.ts) · charts/ · *Card.tsx
    monitoring/   contexts + hooks (useDutMonitor, *Context.tsx)
    api/          rest.ts (typed REST client) · websocket.ts · dut.ts
    utils/        shared browser helpers (clipboard.ts)
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

## Search before you write a helper

The cheapest defect in this repo is a second copy of something that already
exists. A real example: a clipboard-copy helper was added to the bulletin that
duplicated `copyToClipboard` in `pages/AppShell.tsx`, down to a near-identical
comment explaining the plain-HTTP fallback. Two copies drift — the next fix for
Safari's selection behaviour would land in only one of them.

This is a context problem, not a skill problem, so make searching a habit:

```bash
rg -n 'clipboard|execCommand' src                      # is this already solved?
rg -n 'export function|export const' src/api/rest.ts   # what shared helpers exist?
```

Worth knowing before you start:

| Where | What |
|---|---|
| `frontend/src/api/rest.ts` | typed REST client, `humanizeApiError`, `imageKind`, `listQuery` |
| `frontend/src/monitoring/authorColor.ts` | author colour tags, shared by `TagChip` / `FilesSection` / `AuthorTag` |
| `frontend/src/pages/AppShell.tsx` | `copyToClipboard`, `downloadTextFile` |
| `backend/app/db/workspace.py` | `_ensure_column` — the migration idiom; do not hand-write `ALTER TABLE` |

If what you find is buried somewhere awkward to import from, **extract it into a
shared module and change both call sites** rather than copying it. The
extraction is itself worthwhile work; say so in the PR.

**When you extract, sweep the whole tree for callers that should now use it** —
not just the two you already knew about. Search by the underlying API, not by
your own helper's name, or you will only find yourself:

```bash
rg -n 'navigator.clipboard|execCommand' src      # every caller, however written
```

A real case: extracting the clipboard helper left
`SettingsSection.tsx`'s invite-link button still calling
`navigator.clipboard?.writeText(...)` directly. On a plain-HTTP LAN origin —
this project's main deployment — `navigator.clipboard` is `undefined`, so the
optional chain short-circuits and the button silently does nothing. A one-line
switch to the shared helper fixes a user-visible bug that had been there since
the feature shipped. Extractions are the cheapest moment to find these, because
you are already looking at every way the thing is done.

Searching catches duplicates. It will not tell you the existing code is wrong —
copying a flawed helper faithfully is still a flaw — so this habit reduces
review load, it does not replace review.

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

## Shared hardware — one bench, several sessions

Sessions (increasingly several agents at once) share **one** physical test bench.
Check before you take anything:

- **The serial port admits exactly one process.** `lsof /dev/cu.PL2303G-*`
  before opening it. If the backend holds it, a `minicom` will fail with
  `Resource busy` — and vice versa, which silently costs the app its console.
- **Hand the port back for console logins.** The DUT requires a login on its
  serial console after every reboot, and only a human can do it. Release the
  port, confirm it is free, and wait for confirmation that the prompt reads
  `AP6_840E#` before reattaching. Output captured at the bootloader's `cmd>`
  prompt is empty and must not be read as data.
- **The DUT is one device.** Firmware upgrades, site surveys and serial captures
  interfere. After any web-UI submit the DUT answers `/submit.cgi` with
  `301 → /busy.html` for several minutes, before its CGI even runs.
- **Never power-cycle a DUT that may be writing flash.** A flash is confirmed by
  the device's own console output and audit log — not by an HTTP status, and not
  by the UI's "Connected" label, which survives a backend restart that has
  already dropped the SerialWorker.
- **`:8000` / `:5173` belong to whoever started first.** `.claude/launch.json`
  carries a `backend-8001` profile. For parallel work use
  `git worktree add -b <branch> ../DUT_browser-<purpose> CPU_Plots`; a fresh
  worktree inherits none of the gitignored parts (`.venv`, `data/`,
  `node_modules`).

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

> ⚠️ **A green suite does not prove a route is still gated.** Most backend tests
> call endpoint functions directly, which bypasses FastAPI's dependency
> injection entirely — remove a router's `dependencies=[...]` and nothing fails.
> `tests/test_route_protection.py` is the repo's TestClient suite and exists for
> exactly that; extend it when you add a gated endpoint.
>
> Prefer a test that goes **red when the fix is reverted**. Several bugs here
> were shipped with passing tests that asserted truthiness where identity was
> needed (SQLite returns `0`/`1`, not `False`/`True`, so `assertFalse(0)` passes
> while the browser shows the wrong thing).

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
- [ ] New endpoints gated in `main.py`; authorship from the session, not the client.
- [ ] `npm run typecheck` and `pytest` pass, and a reverted fix turns a test red.
- [ ] Nothing under `dut-dashboard/logs/` or `data/` staged; `snapshots.jsonl` intact.
- [ ] Serial port released if a human needs the console; no DUT left mid-flash.
- [ ] Branched from `CPU_Plots`; Conventional Commits; existing features unbroken.
