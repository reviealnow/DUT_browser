# CLAUDE.md — repository principles

The rules that hold **everywhere in this repository**, for every session and
every agent, whatever directory the work happens in. Read this first.

Anything specific to a subproject — its architecture, its module map, its build
commands, the constraints of the code it contains — lives with that subproject
and does not belong here. Today there is one:

| Where | What it holds |
|---|---|
| **`CLAUDE.md`** (this file) | Principles: language, git, the shared bench, when to stop and ask, searching before writing |
| `CONTRIBUTING.md` | The human-facing contribution guide |
| `dut-dashboard/CLAUDE.md` | The dashboard's architecture, module map, non-negotiable code constraints, serial RPC discipline, build/verify commands |
| `dut-dashboard/demo/README_demo_kit.md` | The demo kit's parity bar — read before touching a demo screen |

If a rule here and a rule in a subproject file ever conflict, the subproject
file wins for that subproject: it is the more specific one. Fix the conflict
rather than leaving both standing.

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

**One exception: translated documentation.** A `*.zh-TW.md` companion to an
English document may be written in Traditional Chinese, under three conditions
that keep it from becoming a second source of truth:

1. **The English document is authoritative.** Where the two disagree, English
   wins, and the translation says so in its own opening lines.
2. **It translates, it does not extend.** A rule that exists only in the
   translation is a rule nobody else in the repository can read. Add it to the
   English original first.
3. **Everything around it stays English** — the filename, the commit subject and
   body, the branch name, the PR description, and any code or path it quotes.

Today that means `CONTRIBUTING.zh-TW.md`. The exception exists because the
contribution guide is the one document a new colleague reads *before* they can
be expected to work in English all day; it does not generalise to design notes,
`CLAUDE.md`, or anything an agent reads as instructions.

---

## Git / commits / PRs

- Branch from **`CPU_Plots`** (the pre-Tauri line). Open PRs against `CPU_Plots`.
  **Never** target `main` or `Tauri`.
- Branch names by type: `feat/...`, `fix/...`, `docs/...`, `chore/...`, `test/...`.
- **Conventional Commits**, **atomic** (one logical change per commit), English:
  ```
  type(scope): short imperative subject

  Optional body explaining WHY (not what). Wrap ~72 chars.
  ```
  Types: `feat` `fix` `docs` `refactor` `test` `chore` (see CONTRIBUTING.md).
- **Never `git add -A` in a working tree that has been used for testing.** A
  phase in flight leaves screenshots and serial captures in the repo root;
  eleven screenshots reached `CPU_Plots` this way in #103. Stage files by name
  and re-read `git status --porcelain` afterwards.
- A stacked branch whose base gets **squash-merged** stops having that base as
  an ancestor, so its next PR double-counts everything already merged. Rebase
  with `git rebase --onto origin/<base> <old-base> <branch>` — onto
  `origin/<base>`, not the local ref, which may be stale or checked out in
  another worktree.

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

## Stop and ask

Most decisions are yours to make. These are not — surface them and wait,
because the cost of being wrong is paid by someone else, later:

- **A new dependency.** Including a test runner or a linter. Write the case for
  it (what it buys, what it costs, what it changes about CI) and let a human
  decide; do not add it as a side effect of another change.
- **An API shape other code consumes** — a response field, an exported type, a
  route's status codes. Callers you cannot see may rely on it.
- **A subsystem whose failure modes are asymmetric.** In the dashboard that is
  auth/roles, the serial worker and the firmware transport: a weakened gate, a
  stolen serial port or a half-flashed device costs far more than the change was
  worth.
- **Anything that needs the DUT or the serial port.** They are single-owner and
  shared with other sessions — see *Shared hardware* above.
- **A new architectural shape**: a new top-level directory, a second way of
  doing something the codebase already does one way.
- **Publishing anything outward** — a public page, a released artifact, a
  message on somebody's behalf. "The repository is already public" is not the
  same decision as "this is rendered and indexed".
- **Existing code that looks wrong.** Do not copy it, and do not quietly fix it
  inside an unrelated change either. Say what you found; a drive-by fix with no
  explanation is indistinguishable from a mistake at review time. (If it *is*
  in scope — an extraction that reveals a broken caller — fix it and list it
  **separately** in the PR, because a pre-existing user-visible bug is
  changelog news, not a footnote to a feature.)

Asking is cheap. The expensive version is discovering the answer in review,
after the work is built on top of it.

---

## Search before you write a helper

The cheapest defect in this repository is a second copy of something that
already exists. Two copies drift, and the next fix lands in only one of them.

This is a context problem, not a skill problem, so make searching a habit:

Paths are from the repository root, where this file is, because a command that
fails silently in the one section meant to prevent duplicate helpers is worse
than no command at all:

```bash
# is this already solved?
rg -n 'clipboard|execCommand' dut-dashboard/frontend/src
# what shared helpers exist?
rg -n 'export function|export const' dut-dashboard/frontend/src/api/rest.ts
```

If what you find is buried somewhere awkward to import from, **extract it into a
shared module and change both call sites** rather than copying it. The
extraction is itself worthwhile work; say so in the PR.

**When you extract, sweep the whole tree for callers that should now use it** —
not just the two you already knew about, and search by the underlying API rather
than by your own helper's name, or you will only find yourself. A real case in
this repo: extracting a clipboard helper left one button still calling
`navigator.clipboard?.writeText(...)` directly, which is `undefined` on the
plain-HTTP LAN origin this project actually deploys to, so it silently did
nothing. Extractions are the cheapest moment to find these.

Searching catches duplicates. It will not tell you the existing code is wrong —
copying a flawed helper faithfully is still a flaw — so this habit reduces
review load, it does not replace review.

`dut-dashboard/CLAUDE.md` lists the shared helpers that already exist there.

---

## Verify, don't assert

- **Check the claim, don't restate it.** A rule that says a path is ignored, a
  test that says a control works, a comment that says a file is safe: run the
  check. `git check-ignore`, the actual click, the actual grep. A confident
  sentence with nothing behind it is how this repository has shipped most of its
  defects.
- **A test that cannot fail is worse than none** — it converts "nobody checked"
  into "it passed". Break the thing on purpose once and watch the test go red.
- **Every gate you report must have been run.** If a check was skipped, say
  which and why; do not infer a green from a related green.
