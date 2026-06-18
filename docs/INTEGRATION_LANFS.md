# Integration plan — LAN File Server → DUT_browser

Bring the **file-sharing + bulletin** features from
[LAN_filesever2](https://github.com/reviealnow/LAN_filesever2) into this project.

## Key finding
Both apps already use the **same design system** (Luna "Spacing – Dashboards"):
this repo's `dut-dashboard/frontend/src/styles/dashboard.css` and LAN_filesever2's
`static/styles.css` share the `:root` tokens and the `.app/.sidebar/.card/.kpis/
.filetable/.feed/.btn/.pill` classes. **Integration is additive (add features), not a
re-skin.**

## Reference artifacts
- **Pixel-accurate mockup** (uses this repo's real CSS):
  `dut-dashboard/frontend/mockup_lanfs_integration.html`
  → `cd dut-dashboard/frontend && python3 -m http.server 8895`,
    open `http://127.0.0.1:8895/mockup_lanfs_integration.html`
- LAN_filesever2 `docs/ARCHITECTURE.md` (Part 2 = integration analysis)
- LAN_filesever2 `docs/DESIGN_SYSTEM.md` (tokens + component patterns)
- Portable backend logic to reuse: LAN_filesever2 `file_service.py`,
  `bulletin_service.py`, `db.py` (4-table SQLite schema)
- This repo's shell to extend: `dut-dashboard/frontend/src/pages/AppShell.tsx`,
  `components/shell/{navigation.ts, Sidebar.tsx, Topbar.tsx, Card.tsx}`

## Scope
**Frontend** — two new sections inside the existing Luna shell:
1. **Files** — KPI row (Total Files / Storage Used / Contributors / This Week) +
   Upload card + Shared Files table (reuse `.filetable`; add `.icon-btn`
   download/delete buttons + file-type chips).
2. **Bulletin** — posts/replies as sticky-note cards.

**Backend** — FastAPI routers in `dut-dashboard/backend/app`:
- `/api/files` (list / upload via `UploadFile` / download via `FileResponse` / delete)
  plus aggregates (`uploads_per_day`, `files_by_type`, `top_uploaders`).
- `/api/bulletin` (posts / comments).
- Reuse LAN_filesever2's SQLite schema + service logic (drop `flask.current_app`;
  read FastAPI settings instead).

**Storage** — upload directory under this repo's `data/`; keep the extension
allow-list + size cap + `secure_filename` safeguards.

## Open decision (resolve before backend work)
DUT_browser has **no user/auth model**. Decide: files **per-user** (needs auth) or
**shared within DUT_browser's existing trust boundary**? This drives `uploaded_by` /
ownership / delete-permission.

## Recommended order
1. Read the references + mockup; propose a concrete landing plan (files to add/edit,
   API shapes, data flow) before coding.
2. Backend first: `files` + `bulletin` routers returning JSON; cover with `pytest`
   (`dut-dashboard/backend/tests/`).
3. Frontend: add 2 `NAV_ITEMS` + `FilesSection.tsx` / `BulletinSection.tsx` from the
   existing `Card`/`KpiCard`/`EmptyState` + `.filetable`; ~30 lines new CSS
   (`.icon-btn`, `.ftype`, `.note`).
4. Run `vite` dev, screenshot the real screen (not just the mockup); `npm run typecheck`
   must pass.

## Discipline
Branch off `CPU_Plots`; show a plan before coding; verify on real data/server and
screenshot; no hardcoded secrets; don't commit until approved; keep one spacing-token
scale + single accent; KPIs only from real data.

---

## Copy-paste kickoff prompt (paste into a fresh session in this repo)

```
我要把 LAN_filesever2 的「檔案分享 + 佈告欄(Bulletin)」功能整合進 DUT_browser。
先讀 docs/INTEGRATION_LANFS.md(本檔)與 dut-dashboard/frontend/mockup_lanfs_integration.html,
以及 LAN_filesever2 的 docs/ARCHITECTURE.md、docs/DESIGN_SYSTEM.md、file_service.py、
bulletin_service.py、db.py。

關鍵:兩邊用同一套設計系統(dashboard.css),整合是「加功能」不是改外觀。

範圍:
- 前端:在現有 Luna shell 新增 Files 與 Bulletin 兩個 section(沿用 Card/KpiCard/.filetable,
  只補 .icon-btn/.ftype/.note ~30 行 CSS)。
- 後端:dut-dashboard/backend/app 新增 FastAPI router /api/files、/api/bulletin,
  重用 LAN_filesever2 的 SQLite schema 與 service 邏輯;上傳目錄放 data/,保留副檔名白名單+大小上限+secure_filename。

先跟我確認:DUT_browser 沒有使用者登入模型 — 檔案要分使用者(需加 auth)還是共用?(決定 uploaded_by/刪除權限)

順序:後端 router 先(pytest 驗證)→ 前端 2 個 NAV_ITEMS + FilesSection/BulletinSection
→ vite 跑起來截圖驗證、npm run typecheck 要過。

紀律:開新分支(off CPU_Plots);動工前先給計畫;真實資料驗證+截圖;我核可前不要 commit;
feature 與 docs/mockup 分開 commit。

先讀參考檔與 mockup,然後(a)問我 auth 決策,(b)提出後端優先的落地計畫。
```
