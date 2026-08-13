# 貢獻指南

> **英文版為準。** 本文是 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的繁體中文翻譯，
> 提供給尚未習慣全英文工作環境的同事。兩份文件若有出入，**一律以英文版為準**；
> 發現不一致時請先修英文版，再回頭更新這一份。
>
> 這份翻譯**不新增任何規則**。只存在於中文版的規則，是 repo 裡其他人讀不到的規則。

本 repo 是一個**瀏覽器端**的 DUT 監控儀表板（FastAPI 後端 + React/Vite 前端）。
開 PR 之前，請先讀過基本原則與 commit 慣例。

## 專案基本原則（不可協商）

- **瀏覽器限定。** **不要**引入 Tauri、Rust、`src-tauri`、Electron 或任何桌面應用
  打包方式。這個應用是在區域網路上跑的本機 web service。
- **離線優先。** 不使用 CDN。圖表是手寫的**行內 SVG** —— 不要把 Chart.js 或任何
  圖表函式庫加進 render path。新增的圖表也應該把資料以
  `<script type="application/json">` 一併輸出，供未來遷移使用。
- **不要弄壞既有行為。** 序列主控台、Critical Crash 面板、日誌下載、replay 模式
  都必須持續可用。請沿用共用的 `useDutMonitor` WebSocket monitor，不要另開新的
  `/ws` 連線。
- **絕不 commit 執行期資料。** `dut-dashboard/logs/` 底下**已知的**執行期路徑已被
  gitignore —— session 日誌與目錄、`snapshots.jsonl`、analyzer 輸出、context
  bundle —— 測試 fixture 以外的所有 `*.log` 也是。那些是**逐一指名的 pattern，
  不是整個目錄**：丟進去的新種類產物，在有人替它補上規則之前都是可以被 commit
  的，所以請用 `git check-ignore <path>` 確認，不要用猜的。`snapshots.jsonl`
  可能保存著真實擷取到的 DUT 資料 —— 測試時**不要刪除它**。

## 分支與 Pull Request

- 預設分支是 **`CPU_Plots`**（Tauri 之前的那條線）。請從它開分支。
- **PR 一律對 `CPU_Plots` 開。** **不要**指向 `Tauri` 或 `main`。
- 分支依類型命名，例如 `feat/console-backfill`、`fix/ws-reconnect`。
- 一個 PR 聚焦一個主題；送審前先 rebase、整理掉雜亂的 commit。

### 怎麼合併

**預設用 squash。** 一個主題的 PR 變成一個 commit，`CPU_Plots` 讀起來就是一份
changelog —— 一個 PR 一行，這也是這個專案追蹤歷史的方式。Rebase merge 已停用：
它同時重寫 SHA **並且**失去「這是同一個 PR」的分組，等於集兩種缺點於一身。

**當一條分支帶著數個各自獨立的變更時，改用 merge commit。** 判準是：*一則
squash 後的 commit 訊息，有辦法誠實描述這條分支做過的每一件事嗎？* 如果不行，就
用 merge：

```bash
gh pr merge <n> --merge --delete-branch
```

這件事之所以重要，是因為 squash 的失真有其特定形狀：squash 之後沒有血緣關係，
`git branch --contains` 和 `git branch --no-merged` 都答不出「這個到底出貨了
沒」，而一條承載了五個 phase 的分支就得永遠留著，因為它是那些 phase 唯一的紀錄。
這不是假設：`feat/v2-roles` 承載了 P71a–P72a，正是為此被保留下來，而其中一個
phase 曾經被 squash 整個吃掉，兩天內沒有人發現。

不論用哪一種，`git log --first-parent CPU_Plots` 讀起來都還是一個 PR 一行。

### 不管用哪一種，都要驗證合併後的內容

**沒有任何合併策略可以取代這一步。** 歷史只能告訴你某個 commit 落地了，它無法
告訴你那個功能還在。合併後，請針對這次變更引入的東西去檢查合併後的樹：

```bash
git grep -c '<a symbol the PR added>' CPU_Plots -- <path>
```

（把 `<a symbol the PR added>` 換成這個 PR 新增的某個符號，`<path>` 換成要檢查的
路徑。）

這就是那個能抓到「悄悄消失的 phase」的檢查。改去讀分支自己的 log，正是當初讓它
沒被發現的原因。

## Commit 慣例

我們遵循 **[Conventional Commits](https://www.conventionalcommits.org/)**，並採用
**atomic commit** —— 一個 commit 一個邏輯變更，而且**每一個** commit 的樹都要是綠
的（能編譯、測試通過）。

### 格式

```
type(scope): short imperative subject

Optional body explaining WHY (not what). Wrap at ~72 chars.

Refs #123
Co-Authored-By: Name <email>
```

> commit 訊息一律使用**英文**，沒有例外 —— 主旨、內文、trailer 都是。見
> [`CLAUDE.md`](CLAUDE.md) 的 language rules。

### 類型

| Type | 用於 |
|------|------|
| `feat` | 對使用者可見的新能力（端點、行為） |
| `fix` | 修正 bug |
| `perf` | 效能改善 |
| `refactor` | 不改變行為的結構調整 |
| `docs` | 只動文件 |
| `test` | 只動測試 |
| `chore` | 不觸及應用行為的維護：設定、`.gitignore`、相依套件、指令稿 |
| `build` / `ci` | 建置系統／CI 管線 |

常見的 scope：`dashboard`、`backend`、`parser`、`serial`。

### 主旨寫法

- 祈使語氣：用 **"add"**，不要用 "added" / "adds"。
- 不超過 50 字元，結尾不加句點。
- 內文說明理由；用 `Refs #` / `Closes #` 連結 issue。

### 兩條經驗法則

1. **主旨能不能不用 "and" 就寫完？** 如果要靠 "and" 才能把不相干的事串起來，那
   多半是兩個 commit。例如：console backfill **和** 一個 `.gitignore` 變更屬於
   不同類別 → 拆成 `feat:` + `chore:`。
2. **也不要拆過頭。** 講同一個故事的變更要放在一起（例如一個功能的後端與前端兩
   半）。單位是**一個邏輯變更**，不是檔案數量 —— 一個檔案一個 commit，跟一個
   commit 塞五件事一樣糟。

照這樣切，`git revert`、`git bisect`、code review、`git blame` 和 changelog 產生
都會保持乾淨且可預期。

### 範例

```
# Good
feat(dashboard): console-line backfill + bounded snapshot history
fix(serial): reconnect WebSocket after backend restart
chore: gitignore analyzer session artifacts (dut-session dirs/zips)

# Avoid
update stuff
feat: add backfill and fix gitignore and tweak README   # mixes 3 categories
```

## Commit 之前（本機驗證）

```bash
# Frontend types — must be 0 errors
cd dut-dashboard/frontend && npx tsc --noEmit

# Backend — syntax + tests
cd dut-dashboard/backend
python3 -m compileall app
python3 -m pytest                # if you touched backend logic
```

依序是：前端型別檢查（必須 0 錯誤）、後端語法檢查，以及後端測試 —— 最後一項在你
動到後端邏輯時才需要跑。

若變更涉及 UI 或行為，請用 replay 日誌驗證（見 dashboard 的 README）：啟動後端與
前端，以 replay 模式 `POST /api/serial/open`（`replay_path` 要用**絕對路徑**），
確認受影響的面板有更新。

## 本機開發

見 **[`README.md`](README.md)** 與 **[`dut-dashboard/README.md`](dut-dashboard/README.md)**。
一行指令啟動區域網路版本：

```bash
./scripts/start_lan.sh      # backend :8000 + frontend :5173, both on 0.0.0.0
```

（後端 :8000、前端 :5173，兩者都綁在 0.0.0.0。）
