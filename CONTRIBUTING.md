# Contributing

This repo is a **browser-based** DUT monitoring dashboard (FastAPI backend +
React/Vite frontend). Please read the ground rules and the commit conventions
before opening a PR.

> Traditional Chinese translation:
> [`CONTRIBUTING.zh-TW.md`](CONTRIBUTING.zh-TW.md). This English document is
> authoritative — if the two disagree, this one is right, and the translation
> should be corrected to match.

## Project ground rules (non-negotiable)

- **Browser-only.** Do **not** introduce Tauri, Rust, `src-tauri`, Electron, or
  any desktop packaging. The app runs as a local web service on the LAN.
- **Offline-first.** No CDN. Charts are hand-rendered **inline SVG** — do not add
  Chart.js / a charting library to the render path. New charts should also emit
  their data as `<script type="application/json">` for a future migration.
- **Don't break existing behavior.** The serial console, Critical Crash panel,
  log download, and replay mode must keep working. Reuse the shared
  `useDutMonitor` WebSocket monitor rather than opening new `/ws` connections.
- **Never commit runtime data.** The known runtime paths under
  `dut-dashboard/logs/` are gitignored (session logs and directories,
  `snapshots.jsonl`, analyzer output, context bundles), as is every `*.log`
  anywhere outside the test fixtures. That is eleven specific patterns plus the
  captures rule, **not** the whole directory: a new kind of artifact dropped in
  there is committable until somebody adds it. `snapshots.jsonl` may hold real
  captured DUT data — **do not delete it** when testing.

## Branching & pull requests

- The default branch is **`CPU_Plots`** (the pre-Tauri line). Branch from it.
- **Open PRs against `CPU_Plots`.** Do **not** target `Tauri` or `main`.
- Name branches by type, e.g. `feat/console-backfill`, `fix/ws-reconnect`.
- Keep a PR focused on one theme; rebase/clean up noisy commits before review.

### How to merge

**Squash by default.** A PR with one theme becomes one commit, and `CPU_Plots`
reads as a changelog — one line per PR, which is how this project's history is
tracked. Rebase merges are disabled: they rewrite SHAs *and* lose the "this was
one PR" grouping, so they combine the drawbacks of both other options.

**Use a merge commit when one branch carries several independent changes.**
The test: *could a single squashed commit message honestly describe everything
this branch did?* If not, merge it:

```bash
gh pr merge <n> --merge --delete-branch
```

This matters because squashing is lossy in a specific way — afterwards there is
no ancestry, so `git branch --contains` and `git branch --no-merged` cannot
answer "did this ship?", and a branch that carried five phases has to be kept
forever as the only record of them. That is not hypothetical: `feat/v2-roles`
carried P71a–P72a and is retained for exactly that reason, and one of those
phases was once squashed away entirely without anyone noticing for two days.

`git log --first-parent CPU_Plots` still reads one line per PR either way.

### Verify the merged content, whichever you used

**No merge strategy substitutes for this.** History can only tell you a commit
landed; it cannot tell you the feature is still there. After merging, check the
merged tree for something the change introduced:

```bash
git grep -c '<a symbol the PR added>' CPU_Plots -- <path>
```

This is the check that would have caught the phase that silently vanished.
Reading a branch's own log instead is what let it go unnoticed.

## Commit conventions

We follow **[Conventional Commits](https://www.conventionalcommits.org/)** with
**atomic commits** — one logical change per commit, and the tree stays green
(compiles / tests pass) at every commit.

### Format

```
type(scope): short imperative subject

Optional body explaining WHY (not what). Wrap at ~72 chars.

Refs #123
Co-Authored-By: Name <email>
```

### Types

| Type | Use for |
|------|---------|
| `feat` | new user-facing capability (endpoint, behavior) |
| `fix` | bug fix |
| `perf` | performance improvement |
| `refactor` | behavior-preserving restructure |
| `docs` | documentation only |
| `test` | tests only |
| `chore` | maintenance not touching app behavior: config, `.gitignore`, deps, scripts |
| `build` / `ci` | build system / pipelines |

Common scopes: `dashboard`, `backend`, `parser`, `serial`.

### Subject style

- Imperative mood: **"add"**, not "added" / "adds".
- ≤ 50 characters, no trailing period.
- Body explains the reasoning; reference issues with `Refs #` / `Closes #`.

### Two rules of thumb

1. **Can you write the subject without "and"?** If joining unrelated things needs
   "and", it is probably two commits. Example: console backfill *and* a
   `.gitignore` change are different categories → split into `feat:` + `chore:`.
2. **Don't over-split.** Group changes that tell one story (e.g. the backend +
   frontend halves of a feature) into one commit. The unit is a *logical change*,
   not a file count — one-commit-per-file is as bad as one-commit-per-five-things.

Splitting this way keeps `git revert`, `git bisect`, code review, `git blame`,
and changelog generation clean and predictable.

### Examples

```
# Good
feat(dashboard): console-line backfill + bounded snapshot history
fix(serial): reconnect WebSocket after backend restart
chore: gitignore analyzer session artifacts (dut-session dirs/zips)

# Avoid
update stuff
feat: add backfill and fix gitignore and tweak README   # mixes 3 categories
```

## Before you commit (local verification)

```bash
# Frontend types — must be 0 errors
cd dut-dashboard/frontend && npx tsc --noEmit

# Backend — syntax + tests
cd dut-dashboard/backend
python3 -m compileall app
python3 -m pytest                # if you touched backend logic
```

For UI/behavior changes, verify against a replay log (see the dashboard README):
start the backend + frontend, `POST /api/serial/open` in replay mode (use an
**absolute** `replay_path`), and confirm the affected panels update.

## Local development

See **[`README.md`](README.md)** and **[`dut-dashboard/README.md`](dut-dashboard/README.md)**.
One-command LAN launcher:

```bash
./scripts/start_lan.sh      # backend :8000 + frontend :5173, both on 0.0.0.0
```
