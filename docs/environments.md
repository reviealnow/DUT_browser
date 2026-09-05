# Environments — running a deployment beside the checkout you develop in

Two people, or one person and an agent, need the dashboard running from
different code at the same time: one stable instance somebody is using, one
being changed. This is what separates cleanly, what cannot be separated at all,
and what collides unless you configure it.

Nothing here is new machinery. It is the shape the project already has, written
down, plus `.env.example` as an inventory of a configuration surface that was
previously spread across five files.

## What separates by itself

Every piece of runtime state lives **inside the checkout**, because
`backend/app/config.py` derives it from the source file's own location:

```python
BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
```

There is no environment variable for this. The consequence is worth stating
plainly, because it does the work for you: **a second checkout is automatically
a second environment.** Separate captured snapshots, separate `duts.json`,
separate session logs, separate `workspace.db`, separate session secret,
separate uploads. Nothing to configure and nothing to keep apart by hand.

The same fact is why none of it is inherited when you make one. All of it is
gitignored, so a fresh worktree starts with empty `logs/` and no `data/` at all,
and needs its own `.venv` and `node_modules`.

```bash
git worktree add -b env/stable ../DUT_browser-stable <commit>
cd ../DUT_browser-stable && python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Pin it to a commit rather than tracking a branch, and the stable instance stops
moving when the branch does. `scripts/start_lan.sh` creates the venv and
installs both sets of dependencies on first launch, so in practice you can skip
straight to running it.

## What cannot be separated

**The DUT and its serial console.** One cable, one device, one process. The
second instance to ask for the port gets `Resource busy`, and no amount of
configuration changes that. Before taking it:

```bash
lsof /dev/cu.PL2303G-* /dev/tty.PL2303G-*
```

Check **both** node families. One USB adapter presents its console under two,
and a `minicom` on the `tty` twin blocks the `cu` node while a `cu`-only listing
comes back empty — see `docs/rca-console-hang-2026-09-04.md` for the day that
cost.

So two instances can both *run*, and only one of them can hold the DUT. The
other still serves everything that does not need it: backfilled charts from its
own snapshots, the offline analyzer, the demo kit.

## What collides unless you configure it

**Ports.** `scripts/start_lan.sh` reads `BACKEND_PORT` and `FRONTEND_PORT`, and
refuses to start when either is taken rather than half-starting:

```bash
BACKEND_PORT=8100 FRONTEND_PORT=5273 ./scripts/start_lan.sh
```

**The Vite proxy, in dev only.** The proxy target is hard-coded at
`127.0.0.1:8000` in `frontend/vite.config.ts`. `BACKEND_PORT` does not move it,
so a second dev stack keeps talking to whatever is on `:8000` — on a shared
machine, somebody else's backend, looking perfectly healthy while serving
different code. Either edit that target in your own checkout, or run Vite with
your own config:

```bash
npx vite --config ../../vite.stable.config.mjs
```

`--prod` sidesteps this entirely: it builds the frontend and serves the UI, the
API and the WebSocket from the backend on one port, with no Vite at all. For
anything that is meant to keep running, prefer it.

```bash
BACKEND_PORT=8100 ./scripts/start_lan.sh --prod
```

## Configuration: `.env`

Copy `.env.example` to `.env` in the checkout that needs it. Fifteen variables,
all optional, each documented where it sits.

**Nothing loads `.env` automatically yet.** Load it before launching:

```bash
set -a; . ./.env; set +a
```

Two things about it are worth knowing before you spend time debugging.

**The database wins.** Role passcodes and the DUT's management credentials are
read from `data/workspace.db` first and from the environment only as a fallback.
A value saved through the UI silently outranks the one in your `.env`. This is
the usual reason an edit here appears to do nothing.

**Passcodes are generated, not required.** `start_lan.sh` writes a stable pair
into `dut-dashboard/data/role-passcodes.env` on first launch and prints them,
so a fresh deployment is never locked out of its own engineer and admin roles.
Set them in `.env` only to pin known values; an explicit variable wins over the
generated file.

`.env` is gitignored and `.env.example` is not. Keep it that way: `.env` holds
role passcodes and `DUT_API_PASSWORD`, which is a real login to a real device.

## Open questions

Neither is decided, and both are recorded here rather than in somebody's head.

**Should the launcher load `.env` itself?** Today `.env.example` is an inventory
and the operator sources it. Wiring it into `start_lan.sh` would make it real
configuration, and needs one decision first: precedence. The launcher is
deliberate about letting an explicit environment variable win over its own
defaults, and a plain `set -a; . .env` inverts that — the file would override
what you exported on the command line.

**Should runtime state move out of the checkout?** `LOG_DIR` and `DATA_DIR`
being source-relative is what makes a second checkout a second environment for
free, and it also means updating the code and keeping the data are operations on
one directory. A real deployment usually wants `DUT_DATA_DIR` pointing somewhere
a redeploy cannot touch. That is a new architectural shape and it moves
`snapshots.jsonl`, which holds real captured DUT data, so it is a decision for a
person and a migration, not a refactor.
