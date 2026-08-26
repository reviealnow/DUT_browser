"""What `scripts/start_lan.sh` actually execs, for dev and for --prod, and what
it refuses to exec at all.

The launcher could not start the backend in dev mode for a month (fixed in
PR #94). `"${UVICORN_TLS_ARGS[@]:-}"` expands to one EMPTY word when the array
is unset, not to zero words, and uvicorn answers that with
`Got unexpected extra argument ()`. It survived because the TLS array is only
assigned on the --prod path, acceptance was always done with --prod, and the
default path is dev -- so the broken one was the one nobody ran.

The second launcher defect is the reason for the port tests below: started
against ports another instance already held, it left a NEW Vite (bumped to the
next free port, still running) proxying to the OLD backend, because the proxy
target in frontend/vite.config.ts is hard-coded. Nothing looked wrong; the code
under test was simply not the code being served.

The third is why anything here signals a real process: a deploy host was found
with both halves STOPPED, holding :8000 and :5173 while serving nothing, and a
`kill` against them doing nothing visible because a stopped process never runs
its SIGTERM handler. `kill -0` reports such a child as perfectly alive, so those
tests start a launcher whose children stay up and then stop them for real.

Most of this file binds no port and builds no frontend. The script is copied
into a throwaway tree and run with a PATH of recording shims, so every `exec` is
captured as its exact argv, empty arguments included -- which is the only way to
see the first bug at all: the difference between right and wrong is one "".
`lsof` is shimmed too, so a normal run's port check always sees a free port
regardless of what the developer's machine happens to be running. The tests that
are *about* the port check pass `real_lsof=True` and bind a real socket, because
a guard verified against its own shim would be verifying nothing.

Deliberately plain subprocess + shell, no bats or other new dependency.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "start_lan.sh"

# The shim records "<name>\0<arg>\0<arg>\0<RS>" so an empty argument survives
# the round trip; \0 cannot appear inside an argv element.
_SHIM = """#!/bin/sh
{ printf '%s\\0' "$(basename "$0")" "$@"; printf '\\036'; } >> "$ARGV_LOG"
exit 0
"""

# The same recording shim, but it stays alive -- needed to watch what the
# supervisor does to a child that is running, or stopped, rather than gone. It
# publishes its own PID under $PID_DIR so a test can signal exactly the child it
# means: `pgrep -P` cannot, because the supervisor's own `sleep 1` is a child of
# the launcher too.
#
# The `trap` is load-bearing, not decoration. A stopped process CAN still be
# killed by a signal whose disposition is the default action -- the kernel
# terminates it without ever resuming it. uvicorn is not that: it installs a
# SIGTERM handler, and a process with a handler must run user code, so it cannot
# act on the signal until something resumes it. That is the whole reason a
# `kill` against a stopped backend looks like it did nothing. A shim of plain
# `exec sleep 300` has the default disposition, dies on SIGTERM whether stopped
# or not, and would quietly make every assertion here about stopped children
# untrue while still passing.
_SHIM_ALIVE = """#!/bin/sh
{ printf '%s\\0' "$(basename "$0")" "$@"; printf '\\036'; } >> "$ARGV_LOG"
echo $$ > "$PID_DIR/$(basename "$0")"
trap 'exit 0' TERM
while :; do sleep 1; done
"""

# Only the two commands that BECOME the long-running services get the
# stay-alive shim. `pip install` and the rest must still return, or the
# launcher never reaches the point of starting anything. In dev mode `python3`
# is reached only by the uvicorn exec (the venv already exists, and the QR and
# invite helpers need a LAN IP that the shimmed probes never produce), and
# `npm` only by `npm run dev` (node_modules already exists).
LONG_LIVED = ("python3", "npm")

# Commands the launcher shells out to. `python3` covers both the uvicorn exec
# and the QR/invite helpers; `openssl`, `git` and the network probes are shimmed
# so the run is hermetic and produces no cert, no version lookup and no LAN IP
# (an empty IP skips the QR block, which is not what this test is about).
SHIMMED = (
    "python3",
    "npm",
    "pip",
    "openssl",
    "git",
    "ipconfig",
    "hostname",
    "networksetup",
    # Shimmed so the port guard sees "free" deterministically. Dropped from the
    # PATH by real_lsof=True in the tests that exercise the guard itself.
    "lsof",
)


@contextlib.contextmanager
def taken_port():
    """Hold a real listening socket, and yield the port it took."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]
    finally:
        sock.close()


def free_port() -> int:
    """A port nothing was listening on a moment ago. Good enough for a launcher
    test: the assertions below name the busy port, never this one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def stopped_port_holder():
    """A real process, holding a real listening port, in state T.

    The kernel keeps the socket listening while its owner is stopped -- which is
    the whole trap: the port looks taken and the holder answers nothing.
    """
    code = (
        "import socket, time;"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1);"
        "print(s.getsockname()[1], flush=True);"
        "time.sleep(300)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline())
        proc.send_signal(signal.SIGSTOP)
        yield port, proc.pid
    finally:
        # CONT before KILL: SIGKILL reaches a stopped process, but nothing else
        # would, and leaving one behind would hold the port for the next test.
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGCONT)
        proc.kill()
        proc.wait(timeout=10)


def wait_gone(pid: int, timeout: float = 8.0) -> bool:
    """True once `pid` no longer exists. These are grandchildren of the test
    process, so they are reparented rather than reaped by us; `kill -0` is the
    honest check."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


class Launched:
    """A live launcher plus the PIDs of the children it started."""

    def __init__(self, proc: subprocess.Popen, pids: dict[str, int], out_path: Path):
        self.proc = proc
        self.backend = pids["python3"]
        self.frontend = pids["npm"]
        # start_new_session=True makes the launcher its own group leader, so its
        # PID is the group id -- which is what Ctrl-Z signals.
        self.pgid = proc.pid
        self._out_path = out_path

    def output(self) -> str:
        return self._out_path.read_text(errors="replace")


def build_fake_tree(
    root: Path, *, real_lsof: bool = False, long_lived: bool = False, **env_extra: str
) -> tuple[Path, dict[str, str], Path]:
    """Lay out a throwaway tree with a PATH of shims. Returns (script, env, log)."""
    (root / "scripts").mkdir()
    script = root / "scripts" / "start_lan.sh"
    script.write_text(SCRIPT.read_text())

    backend = root / "dut-dashboard" / "backend"
    frontend = root / "dut-dashboard" / "frontend"
    backend.mkdir(parents=True)
    # Present => the launcher skips `npm install`.
    (frontend / "node_modules").mkdir(parents=True)
    (root / "requirements.txt").write_text("")
    # Present => the launcher skips `python3 -m venv`; sourcing an empty
    # activate script is a no-op, which is what we want.
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("")
    # Present => --prod arms TLS without ever calling openssl.
    certs = root / "dut-dashboard" / "data" / "certs"
    certs.mkdir(parents=True)
    (certs / "dev.crt").write_text("cert")
    (certs / "dev.key").write_text("key")

    shim_dir = root / "shims"
    shim_dir.mkdir()
    pid_dir = root / "pids"
    pid_dir.mkdir()
    for name in SHIMMED:
        if name == "lsof" and real_lsof:
            continue
        shim = shim_dir / name
        shim.write_text(_SHIM_ALIVE if long_lived and name in LONG_LIVED else _SHIM)
        shim.chmod(0o755)

    log = root / "argv.log"
    env = {
        **os.environ,
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
        "ARGV_LOG": str(log),
        "PID_DIR": str(pid_dir),
        # Supplied so the launcher neither runs `git describe` nor
        # generates a passcode file.
        "DUT_APP_VERSION": "test",
        "DUT_ENGINEER_PASSCODE": "eng",
        "DUT_ADMIN_PASSCODE": "adm",
        **env_extra,
    }
    return script, env, log


class LauncherArgvTests(unittest.TestCase):
    # A launcher whose children have all exited is a dead launcher, and it now
    # says so and exits 1 rather than sitting in `wait`. Every shim exits
    # immediately, so that is the normal outcome of a *successful* start here --
    # the argv assertions below are made on what got recorded before it.
    def _run(
        self,
        *args: str,
        returncode: int = 1,
        real_lsof: bool = False,
        **env_extra: str,
    ) -> list[list[str]]:
        """Run the launcher in a fake tree; return every recorded argv, in order."""
        self.last_result: subprocess.CompletedProcess[str]
        with tempfile.TemporaryDirectory() as tmp:
            script, env, log = build_fake_tree(Path(tmp), real_lsof=real_lsof, **env_extra)
            result = subprocess.run(
                ["bash", str(script), *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.last_result = result
            self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)

            if not log.exists():
                return []
            # Each record is "<field>\0" repeated, so the split leaves one
            # trailing "" from the final separator -- drop that one only, and
            # every genuinely empty argument survives.
            return [
                record.split("\0")[:-1]
                for record in log.read_bytes().decode().split("\036")
                if record
            ]

    @staticmethod
    def _find(records: list[list[str]], *needles: str) -> list[list[str]]:
        return [r for r in records if all(n in r for n in needles)]

    @contextlib.contextmanager
    def _launch(self, *args: str, **env_extra: str):
        """Start the launcher with children that STAY UP, and yield a `Launched`.

        Two things here are load-bearing rather than stylistic:

        * `start_new_session=True` puts the launcher in its own process group,
          so a test can signal the GROUP exactly the way Ctrl-Z signals a
          foreground job. Without it SIGTSTP would hit the test runner.
        * output goes to a FILE, never a pipe. The children inherit the write
          end, so `.communicate()` blocks until the grandchildren close it --
          which, for a child that stays up, is never. That hung the prototype.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, env, _log = build_fake_tree(root, long_lived=True, **env_extra)
            out_path = root / "launcher.out"
            with open(out_path, "wb") as out:
                proc = subprocess.Popen(
                    ["bash", str(script), *args],
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            pids: dict[str, int] = {}
            try:
                pids = self._await_children(root / "pids", proc, out_path)
                yield Launched(proc, pids, out_path)
            finally:
                # Reap by PID as well as by group: a child left stopped would
                # hold its port into the next test. CONT before KILL so a
                # stopped member can actually die.
                for pid in pids.values():
                    for sig in (signal.SIGCONT, signal.SIGKILL):
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.kill(pid, sig)
                for sig in (signal.SIGCONT, signal.SIGKILL):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, sig)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)

    def _await_children(self, pid_dir: Path, proc: subprocess.Popen, out: Path) -> dict[str, int]:
        """Wait for both stay-alive shims to publish their PID."""
        deadline = time.time() + 30
        while time.time() < deadline:
            found = {p.name: int(p.read_text().strip()) for p in pid_dir.iterdir() if p.is_file()}
            if set(found) >= set(LONG_LIVED):
                return found
            if proc.poll() is not None:
                self.fail(f"launcher exited before starting its children:\n{out.read_text()}")
            time.sleep(0.2)
        self.fail(f"children never appeared:\n{out.read_text()}")

    # -- dev (the path that was broken) -------------------------------------

    def test_dev_execs_uvicorn_with_no_empty_argument(self) -> None:
        """The regression itself: with the TLS array unset the backend command
        must be exactly these words -- a trailing "" is what uvicorn rejected."""
        records = self._run()
        [uvicorn] = self._find(records, "uvicorn")
        self.assertEqual(
            uvicorn,
            ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        )

    def test_dev_also_starts_vite_on_the_frontend_port(self) -> None:
        """--strictPort is the point: without it Vite answers a port clash by
        moving to the next free port and staying up, so the URL the launcher
        just printed is not the URL that works."""
        records = self._run()
        [vite] = self._find(records, "npm", "dev")
        self.assertEqual(
            vite,
            ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173", "--strictPort"],
        )

    def test_dev_never_builds_the_frontend(self) -> None:
        self.assertEqual(self._find(self._run(), "npm", "build"), [])

    def test_dev_honours_the_port_overrides(self) -> None:
        records = self._run(BACKEND_PORT="8011", FRONTEND_PORT="5199", BIND_HOST="127.0.0.1")
        [uvicorn] = self._find(records, "uvicorn")
        self.assertEqual(uvicorn[-4:], ["--host", "127.0.0.1", "--port", "8011"])
        [vite] = self._find(records, "npm", "dev")
        self.assertEqual(
            vite,
            ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5199", "--strictPort"],
        )

    # -- prod ---------------------------------------------------------------

    def test_prod_builds_the_frontend_and_serves_one_port(self) -> None:
        records = self._run("--prod")
        self.assertEqual(len(self._find(records, "npm", "build")), 1)
        self.assertEqual(self._find(records, "npm", "dev"), [], "prod must not start Vite")

    def test_prod_passes_the_tls_pair_to_uvicorn(self) -> None:
        records = self._run("--prod")
        [uvicorn] = self._find(records, "uvicorn")
        self.assertIn("--ssl-keyfile", uvicorn)
        self.assertIn("--ssl-certfile", uvicorn)
        self.assertTrue(uvicorn[uvicorn.index("--ssl-keyfile") + 1].endswith("dev.key"))
        self.assertTrue(uvicorn[uvicorn.index("--ssl-certfile") + 1].endswith("dev.crt"))

    def test_prod_without_tls_is_the_same_command_minus_the_tls_pair(self) -> None:
        """DUT_NO_TLS leaves the array unset on the --prod path too, so it is the
        second way into the empty-argument bug."""
        records = self._run("--prod", DUT_NO_TLS="1")
        [uvicorn] = self._find(records, "uvicorn")
        self.assertEqual(
            uvicorn,
            ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        )

    def test_an_unknown_argument_is_refused_before_anything_starts(self) -> None:
        """A typo'd flag must not quietly launch the dev pair instead."""
        self.assertEqual(self._run("--produciton", returncode=2), [])

    # -- the port guard (real sockets, real lsof) ----------------------------

    def test_a_taken_backend_port_stops_the_launch(self) -> None:
        with taken_port() as port:
            records = self._run(
                returncode=1,
                real_lsof=True,
                BACKEND_PORT=str(port),
                FRONTEND_PORT=str(free_port()),
            )
        self.assertEqual(records, [], "nothing may be started once a port is refused")
        err = self.last_result.stderr
        self.assertIn(str(port), err)
        self.assertIn(str(os.getpid()), err, "the operator needs the PID holding it")
        self.assertIn("BACKEND_PORT=", err, "and a way out that does not need this knowledge")

    def test_a_taken_frontend_port_stops_the_launch_too(self) -> None:
        """The orphan case. Both ports are checked before anything starts, so a
        busy 5173 must not leave a backend running on a free 8000 behind it."""
        with taken_port() as port:
            records = self._run(
                returncode=1,
                real_lsof=True,
                BACKEND_PORT=str(free_port()),
                FRONTEND_PORT=str(port),
            )
        self.assertEqual(records, [], "a busy frontend port must not orphan a backend")
        self.assertIn(str(port), self.last_result.stderr)

    def test_prod_ignores_a_taken_frontend_port(self) -> None:
        """--prod runs no Vite, so that port is not ours to claim and refusing
        over it would block a legitimate launch."""
        with taken_port() as port:
            records = self._run(
                "--prod",
                returncode=1,
                real_lsof=True,
                BACKEND_PORT=str(free_port()),
                FRONTEND_PORT=str(port),
            )
        self.assertEqual(len(self._find(records, "uvicorn")), 1)

    # -- the supervisor ------------------------------------------------------

    def test_a_dead_child_brings_the_launcher_down_instead_of_hanging(self) -> None:
        """Every shim exits at once, so this is the backend dying a moment after
        it started -- the case the port guard cannot see. The launcher must
        notice, name it, and exit non-zero rather than sit in `wait` with a live
        frontend proxying to a backend it does not own."""
        self._run(returncode=1)
        self.assertIn("backend", self.last_result.stderr)
        self.assertIn("exited", self.last_result.stderr)

    # -- stopped children (a live launcher, signalled for real) --------------

    def test_a_child_that_stays_stopped_counts_as_dead(self) -> None:
        """`kill -0` succeeds for a process in state T, so the liveness check
        alone reads a STOPPED backend as healthy. It is not: it holds :8000 and
        serves nothing, and SIGTERM sits pending against it until it resumes.
        Found that way on a deploy host, both halves stopped for hours."""
        with self._launch() as job:
            os.kill(job.backend, signal.SIGSTOP)
            returncode = job.proc.wait(timeout=30)
            out = job.output()
            # Exiting is only half the job, and the weaker half. cleanup() sends
            # SIGTERM, which a stopped process with a handler cannot act on, so
            # without a SIGCONT ahead of it the launcher announces the problem
            # and then walks away leaving the child stopped and still holding
            # its port -- the exact state it just diagnosed. Asserted inside the
            # `with`, because leaving it tears the group down regardless.
            backend_gone = wait_gone(job.backend)
            frontend_gone = wait_gone(job.frontend)
        self.assertEqual(returncode, 1, out)
        self.assertIn("STOPPED", out)
        self.assertIn("backend", out)
        self.assertIn("kill -CONT", out, "the operator needs the way out, not just the diagnosis")
        self.assertTrue(backend_gone, f"the STOPPED child was left running:\n{out}")
        self.assertTrue(frontend_gone, f"the other half was left running:\n{out}")

    def test_a_brief_stop_does_not_bring_the_stack_down(self) -> None:
        """The debounce has to be a real one, and this has to be able to prove
        it. 1.5s is chosen, not arbitrary: polls are >=1s apart, so a 1.5s stop
        is guaranteed to be seen by at least one poll and cannot be seen by more
        than two. It therefore fails with a strike limit of 1 and passes with 3
        -- every time, not most of the time. (0.5s does not: it can fall between
        two polls and be missed entirely, which makes it a coin toss that proves
        nothing.)"""
        with self._launch() as job:
            os.kill(job.backend, signal.SIGSTOP)
            time.sleep(1.5)
            # Suppressed so a too-eager supervisor fails this test on the
            # assertion below -- which says what went wrong -- rather than on a
            # ProcessLookupError raised while resuming a child it already killed.
            with contextlib.suppress(ProcessLookupError):
                os.kill(job.backend, signal.SIGCONT)
            time.sleep(5)
            alive = job.proc.poll() is None
            out = job.output()
        self.assertTrue(alive, f"a 0.5s stop must not be fatal:\n{out}")

    def test_ctrl_z_then_fg_does_not_bring_the_stack_down(self) -> None:
        """The regression this guard could most easily cause. Ctrl-Z sends
        SIGTSTP to the whole foreground process group, so the launcher and both
        children stop together -- and on `fg` a child can still read as T for an
        instant. Suspending a launcher and resuming it is legitimate and must
        stay that way.

        Signalling the group is what Ctrl-Z *is*, so this needs no pty.
        """
        with self._launch() as job:
            os.killpg(job.pgid, signal.SIGTSTP)
            time.sleep(4)  # longer than the strike limit, while nothing polls
            suspended_alive = job.proc.poll() is None
            os.killpg(job.pgid, signal.SIGCONT)
            time.sleep(5)  # several polls after the resume
            alive = job.proc.poll() is None
            out = job.output()
        self.assertTrue(suspended_alive, f"a suspended launcher is not a dead one:\n{out}")
        self.assertTrue(alive, f"Ctrl-Z followed by fg must leave the stack running:\n{out}")


    def test_the_port_guard_says_when_the_holder_is_stopped(self) -> None:
        """What the incident actually needed. The guard named the PID and the
        command, so `kill` looked like the answer -- and did nothing, because
        the holder was stopped. The state belongs in that message."""
        with stopped_port_holder() as (port, holder_pid):
            self._run(
                returncode=1,
                real_lsof=True,
                BACKEND_PORT=str(port),
                FRONTEND_PORT=str(free_port()),
            )
            err = self.last_result.stderr
        self.assertIn("[STOPPED]", err)
        self.assertIn(str(holder_pid), err)
        self.assertIn("kill -CONT", err)


if __name__ == "__main__":
    unittest.main()
