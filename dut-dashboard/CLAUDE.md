# CLAUDE.md — DUT_browser (AP6_monitor)

What this subproject is and the constraints its code has to hold. Read before
editing anything under `dut-dashboard/`.

> **Read [`../CLAUDE.md`](../CLAUDE.md) first.** It carries the rules that hold
> everywhere in the repository and are not repeated here: the language rules,
> git and PR conventions, the shared test bench, when to stop and ask, searching
> before writing a helper, and verifying rather than asserting. This file holds
> only what is specific to the dashboard.

## What this is

Browser-based DUT monitoring dashboard for AP / network-device QA.
**FastAPI** backend (`:8000`) + **React/Vite/TypeScript** frontend (`:5173`),
served over the LAN. It monitors a QCA/Atheros AP6 DUT over a **serial console**
(or a replay log) and parses console output into structured telemetry.

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
  tools/          analyzer3.py · log_event_detector.py (offline) ·
                  mock_survey_server.py (dev aid: serves a fixed survey payload
                  so the Site Survey UI can be worked on without a DUT) ·
                  wifi_timeseries.py · context_render.py (offline Wi-Fi context
                  tooling — interface, schemas and file ownership are fixed in
                  tools/CONTRACT_wifi_context.md; implemented in parallel PRs)
  scripts/        sysMon.sh (DUT-side telemetry)
  demo/           self-contained HTML pages for showing the product without a
                  DUT (one file per screen, opens by double-click; index.html
                  is the front door) · build_demo_data.py regenerates their
                  baked-in data from a real bundle and anonymises every
                  SSID/MAC/IP on the way in · **read README_demo_kit.md before
                  touching a screen**: it carries the parity bar these pages are
                  held to (match the observable contract, not the component line
                  by line) and the rule that earned it — open the real component
                  and read its JSX first, because a demo can misrepresent the
                  product by showing LESS as easily as more, and only one of
                  those has a chip · demo/verify/ loads the shipped pages in a
                  real DOM and clicks their own controls (`npm test`); run it
                  after touching any page, because reading the source is what
                  let every past finding through
```

When adding a feature, follow the **existing Wi-Fi Clients data flow** as the
template, do not invent a new pattern:
`services/<x>.py` parser → `main.py` endpoint → `api/rest.ts` types+fn →
`monitoring/<X>Context.tsx` cache → `components/<X>Card.tsx` → register in
`components/shell/navigation.ts`.

---

## Shared helpers that already exist here

`../CLAUDE.md` says to search before writing a helper. These are the ones worth
knowing about before you start, because they are the ones that have been
duplicated:

| Where | What |
|---|---|
| `frontend/src/api/rest.ts` | typed REST client, `humanizeApiError`, `imageKind`, `listQuery` |
| `frontend/src/monitoring/authorColor.ts` | author colour tags, shared by `TagChip` / `FilesSection` / `AuthorTag` |
| `frontend/src/utils/clipboard.ts` | `copyToClipboard` — the plain-HTTP fallback lives here, not at the call sites |
| `frontend/src/pages/AppShell.tsx` | `downloadTextFile` |
| `backend/app/db/workspace.py` | `_ensure_column` — the migration idiom; do not hand-write `ALTER TABLE` |

Both halves of that rule have been paid for here. A clipboard-copy helper was
once added to the bulletin that duplicated `copyToClipboard` — then still living
in `pages/AppShell.tsx` — down to a near-identical comment explaining the
plain-HTTP fallback. Two copies, and the next fix for Safari's selection
behaviour would have landed in only one. It has since been extracted to
`utils/clipboard.ts`, which is why the table above points there and this
paragraph does not.

And when that helper was finally extracted, the sweep missed a caller:
`SettingsSection.tsx`'s invite-link button was still calling
`navigator.clipboard?.writeText(...)` directly. On a plain-HTTP LAN origin —
this project's main deployment — `navigator.clipboard` is `undefined`, so the
optional chain short-circuited and the button silently did nothing, from the day
the feature shipped. Search by the underlying API, never by your own helper's
name, or you will only find yourself:

```bash
# from dut-dashboard/ — every caller, however it was written
rg -n 'navigator.clipboard|execCommand' frontend/src
```

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

## Pre-commit checklist

Repo-wide items — English artifacts, Conventional Commits, branched from
`CPU_Plots`, nothing staged by `git add -A`, the serial port handed back — are
in [`../CLAUDE.md`](../CLAUDE.md). These are the dashboard's own:

- [ ] No Tauri/Electron/Rust; no CDN; no chart/UI lib added to render path.
- [ ] No new `/ws`; on-demand work uses REST + `capture_command`.
- [ ] No serial background-polling; captures coalesced + DUT-scoped.
- [ ] Missing parser fields → `None`, no uncaught exceptions.
- [ ] New endpoints gated in `main.py`; authorship from the session, not the client.
- [ ] `npm run typecheck` and `pytest` pass, and a reverted fix turns a test red.
- [ ] Nothing under `dut-dashboard/logs/` or `data/` staged; `snapshots.jsonl` intact.
- [ ] Existing features unbroken: serial console, Critical Crash panel, log
      download and replay mode all still work.
