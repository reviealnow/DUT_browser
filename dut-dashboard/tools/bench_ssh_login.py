#!/usr/bin/env python3
"""Watch an SSH password login happen, byte by byte, on the terminal ssh uses.

The question this answers is the one a refused login cannot: *did the password
ever leave this machine?* On 2026-09-14 a Raspberry Pi rejected every login
from the dashboard while accepting the same credentials typed by hand, and the
card could only say the collector refused them. The server's log said otherwise
-- `Connection closed by authenticating user [preauth]` and never a `Failed
password` -- because ssh had no controlling terminal to ask on and gave up
without sending an authentication request at all. That is invisible from both
ends and obvious here.

It drives the app's own transport: `pty_ssh.spawn_with_tty` with
`password_auth_options()`, the same pty and the same options
`collector/ssh_session.py` and `SerialWorker`'s ssh path use. What it adds is a
timestamp on every read and write, and `ssh -v` alongside.

    python3 tools/bench_ssh_login.py 192.168.30.124 nelson

**The password is read at a prompt, never taken as an argument.** An argument is
visible in `ps` to every account on the machine, and this is a bench password.
It is scrubbed out of everything printed, including whatever a remote that
echoes may send back.

Read-only on the far end: it logs in, runs `echo`, and closes. Nothing about the
DUT is touched -- this is about the *login*, not the device behind it. Exits 0
when the shell answered, 2 when ssh refused, 3 when no prompt ever arrived,
which is the class of failure the tool was written for.

This is a diagnostic aid, not a test. The transport it drives is covered by
`backend/tests/test_collector_ssh.py`.
"""

from __future__ import annotations

import argparse
import getpass
import os
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.serial import pty_ssh  # noqa: E402

READY = "__BENCH_SSH_OK__"
#: What a child does to its descriptors before it asks for anything. Real ssh
#: runs closefrom(); a probe that does not is holding the last reference to its
#: own terminal and will report success no matter what.
PROBE = (
    "import os\n"
    "os.closerange(3, 256)\n"
    "try:\n"
    "    fd = os.open('/dev/tty', os.O_RDWR); os.close(fd); print('TTY-OK')\n"
    "except OSError as exc:\n"
    "    print(f'TTY-LOST errno={exc.errno} {exc.strerror}')\n"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host")
    parser.add_argument("user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=float, default=25.0,
                        help="seconds to wait for the login to finish (default: 25)")
    parser.add_argument("--quiet", action="store_true",
                        help="drop ssh's -v chatter; keep the terminal conversation")
    args = parser.parse_args()

    password = getpass.getpass(f"password for {args.user}@{args.host}: ")
    start = time.monotonic()

    def log(line: str) -> None:
        print(f"[{time.monotonic() - start:6.2f}s] {pty_ssh.scrub(line, password)}", flush=True)

    # Before anything else: can a child of this spawn still open /dev/tty after
    # doing what ssh does to its descriptors? A TTY-LOST here is the whole
    # answer, and it is answered in milliseconds.
    probe, probe_master, probe_slave = pty_ssh.spawn_with_tty([sys.executable, "-c", PROBE])
    probe.wait(timeout=10)
    probe_said = (probe.stdout.read() or b"").decode().strip()
    log(f"controlling-terminal probe: {probe_said}")
    pty_ssh.close_tty(probe_master, probe_slave)

    argv = [
        "ssh", *([] if args.quiet else ["-v"]), *pty_ssh.password_auth_options(),
        "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
        "-p", str(args.port), f"{args.user}@{args.host}", f"echo {READY}",
    ]
    log(f"spawning: {' '.join(argv)}")
    process, master, slave = pty_ssh.spawn_with_tty(argv)

    seen, chatter, wrote, answered = "", "", False, False
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [master, process.stdout.fileno(), process.stderr.fileno()], [], [], 0.2)
            for fd in ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    continue
                text = chunk.decode("utf-8", "replace")
                where = ("tty" if fd == master
                         else "stdout" if fd == process.stdout.fileno() else "stderr")
                log(f"{where} <- {text!r}")
                if READY in text:
                    answered = True
                if fd != master:
                    chatter += text
                    continue
                seen += text
                if not wrote and pty_ssh.PROMPT_RE.search(seen.rstrip("\r\n\0 ")):
                    os.write(master, password.encode() + b"\n")
                    wrote = True
                    log("tty -> wrote the password and a newline")
            if answered or process.poll() is not None:
                break
    finally:
        try:
            process.kill()
        except OSError:
            pass
        pty_ssh.close_tty(master, slave)

    if answered:
        log("logged in: the remote shell answered")
        return 0
    if wrote:
        log("the password was written and the login still did not complete — "
            "ssh's own words above say why")
        return 2
    if "can't open /dev/tty" in chatter or "TTY-LOST" in probe_said:
        # The failure this tool exists for, and the only one worth naming
        # outright. ssh asks on /dev/tty; with none to open it sends no
        # authentication request at all, and the server logs a connection
        # closed at preauth with no failed password beside it.
        log("no prompt: ssh had no controlling terminal to ask on, so nothing "
            "was ever sent to the server")
        return 3
    # Everything else that ends before a prompt -- refused, unreachable, an
    # unknown host key. ssh has already said which, and inventing a diagnosis
    # on top of its sentence is how a tool starts lying.
    log("no prompt arrived; ssh ended first for the reason it printed above")
    return 2


if __name__ == "__main__":
    sys.exit(main())
