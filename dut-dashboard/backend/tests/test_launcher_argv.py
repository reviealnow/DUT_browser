"""What `scripts/start_lan.sh` actually execs, for dev and for --prod.

The launcher could not start the backend in dev mode for a month (fixed in
PR #94). `"${UVICORN_TLS_ARGS[@]:-}"` expands to one EMPTY word when the array
is unset, not to zero words, and uvicorn answers that with
`Got unexpected extra argument ()`. It survived because the TLS array is only
assigned on the --prod path, acceptance was always done with --prod, and the
default path is dev -- so the broken one was the one nobody ran.

Nothing here binds a port or builds a frontend. The script is copied into a
throwaway tree and run with a PATH of recording shims, so every `exec` is
captured as its exact argv, empty arguments included -- which is the only way
to see the bug at all: the difference between right and wrong is one "".

Deliberately plain subprocess + shell, no bats or other new dependency.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "start_lan.sh"

# The shim records "<name>\0<arg>\0<arg>\0<RS>" so an empty argument survives
# the round trip; \0 cannot appear inside an argv element.
_SHIM = """#!/bin/sh
{ printf '%s\\0' "$(basename "$0")" "$@"; printf '\\036'; } >> "$ARGV_LOG"
exit 0
"""

# Commands the launcher shells out to. `python3` covers both the uvicorn exec
# and the QR/invite helpers; `openssl`, `git` and the network probes are shimmed
# so the run is hermetic and produces no cert, no version lookup and no LAN IP
# (an empty IP skips the QR block, which is not what this test is about).
SHIMMED = ("python3", "npm", "pip", "openssl", "git", "ipconfig", "hostname", "networksetup")


class LauncherArgvTests(unittest.TestCase):
    # A launcher whose children have all exited is a dead launcher, and it now
    # says so and exits 1 rather than sitting in `wait`. Every shim exits
    # immediately, so that is the normal outcome of a *successful* start here --
    # the argv assertions below are made on what got recorded before it.
    def _run(self, *args: str, returncode: int = 1, **env_extra: str) -> list[list[str]]:
        """Run the launcher in a fake tree; return every recorded argv, in order."""
        self.last_result: subprocess.CompletedProcess[str]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
            for name in SHIMMED:
                shim = shim_dir / name
                shim.write_text(_SHIM)
                shim.chmod(0o755)

            log = root / "argv.log"
            env = {
                **os.environ,
                "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
                "ARGV_LOG": str(log),
                # Supplied so the launcher neither runs `git describe` nor
                # generates a passcode file.
                "DUT_APP_VERSION": "test",
                "DUT_ENGINEER_PASSCODE": "eng",
                "DUT_ADMIN_PASSCODE": "adm",
                **env_extra,
            }
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

    # -- the supervisor ------------------------------------------------------

    def test_a_dead_child_brings_the_launcher_down_instead_of_hanging(self) -> None:
        """Every shim exits at once, so this is the backend dying a moment after
        it started. The launcher must notice, name it, and exit non-zero rather
        than sit in `wait` with a live frontend proxying to a backend it does
        not own."""
        self._run(returncode=1)
        self.assertIn("backend", self.last_result.stderr)
        self.assertIn("exited", self.last_result.stderr)


if __name__ == "__main__":
    unittest.main()
