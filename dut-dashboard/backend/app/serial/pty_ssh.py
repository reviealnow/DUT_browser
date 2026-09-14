"""Spawning `ssh` with a controlling terminal, so a password can be typed at it.

`ssh` reads a password from its **controlling terminal**, never from stdin, so a
child holding a pipe on fd 0 cannot answer the prompt however carefully it
writes to it. That is why key authentication with `BatchMode=yes` is the only
thing the console transport could do before this module existed.

What this does **not** do is put the session on the terminal. The child gets a
pty as its controlling tty -- enough for `/dev/tty` to open, which is all the
prompt needs -- while stdin, stdout and stderr stay ordinary pipes. That matters
more here than it looks: a console carries raw bytes, and a pty line discipline
would translate CR/NL and act on control characters in the middle of a firmware
transfer. Measured on this machine before it was written: with this split, a
`\\x00\\x01...\\xff` payload arrives on stdout byte for byte while the prompt
arrives on the pty and ssh's own errors stay on stderr.

Every caller therefore keeps working with a plain `subprocess.Popen`, which is
what `SerialWorker`'s ssh path already handles.

The fork is the risky part of this and is kept as small as it can be: `setsid`
then one `ioctl`, both bare syscalls, in a process that has threads in it.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import subprocess
import termios
import threading
import time

#: How long to wait for the password prompt before giving up on it.
PROMPT_TIMEOUT_SEC = 20

# ssh's wording moves between versions and between password and
# keyboard-interactive, so these match the part that does not.
PROMPT_RE = re.compile(r"(?i)password:\s*$|password for .*:\s*$|passcode.*:\s*$")
HOSTKEY_RE = re.compile(
    r"(?i)are you sure you want to continue connecting|host key verification failed"
)
DENIED_RE = re.compile(r"(?i)permission denied|too many authentication failures")

HOSTKEY_MESSAGE = (
    "The host key is not known to this machine. SSH to it by hand once, check "
    "the fingerprint, then retry."
)


class PtySshError(RuntimeError):
    """A login that did not happen, worded for the person who typed it."""


def scrub(text: str, secret: str) -> str:
    """Remove a password from anything that may be shown, logged or raised.

    ssh turns echo off while it reads, so normally there is nothing here to
    remove. This is for the case where a terminal echoes anyway: an HTTP error
    body is the last place a bench password should turn up.
    """
    return text.replace(secret, "***") if secret else text


def spawn_with_tty(argv: list[str]) -> tuple[subprocess.Popen[bytes], int]:
    """Start `argv` with a pty as its controlling terminal and pipes elsewhere.

    Returns the child and the pty **master** fd, which is where the prompt
    appears and where the answer is written. The caller owns both and must close
    the master fd when it is done with the child.
    """
    master, slave = pty.openpty()

    def _take_controlling_tty() -> None:
        # Runs in the forked child, before exec. Two syscalls and nothing else:
        # a fork of a threaded process inherits locks no surviving thread will
        # release, so anything that might allocate or take one does not belong
        # here.
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_take_controlling_tty,
            # Needed for the ioctl above: the child closes every other
            # descriptor before preexec_fn runs.
            pass_fds=(slave,),
            bufsize=0,
        )
    except (OSError, ValueError) as exc:
        os.close(master)
        os.close(slave)
        raise PtySshError(f"Could not start ssh: {exc}") from exc
    finally:
        # The parent's copy. The child keeps its own, and holding a second one
        # here would keep the pty alive after the child has gone.
        try:
            os.close(slave)
        except OSError:
            pass
    return process, master


def answer_password_prompt(
    master_fd: int,
    password: str,
    process: subprocess.Popen[bytes],
    timeout: float = PROMPT_TIMEOUT_SEC,
) -> None:
    """Wait for the prompt on the terminal and type the password, once.

    Returns quietly when the child exits first: that is a login that failed
    before it asked, and ssh has already said why on stderr -- which is a better
    message than anything this function could invent.
    """
    deadline = time.monotonic() + timeout
    seen = ""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                chunk = b""
            if chunk:
                seen += chunk.decode("utf-8", "replace")
                if HOSTKEY_RE.search(seen):
                    # Never answered for the operator. Saying "yes" here would
                    # be this dashboard deciding to trust a machine nobody has
                    # identified.
                    raise PtySshError(HOSTKEY_MESSAGE)
                if PROMPT_RE.search(seen.rstrip("\r\n\0 ")):
                    os.write(master_fd, password.encode() + b"\n")
                    return
        if process.poll() is not None:
            return
    raise PtySshError(
        f"No password prompt arrived within {int(timeout)} seconds; check the "
        "address, and that the machine allows password logins."
    )


def denial_line(seen: str, secret: str) -> str | None:
    """ssh's own last word about a refusal, safe to show to an operator.

    The three refusals ssh writes here mean three different next moves --
    "Permission denied, please try again." is a password to re-type,
    "Permission denied (publickey)." is a server that does not offer password
    logins at all, and "Too many authentication failures" is a key agent
    spending the attempts before the password is ever tried. Collapsing them
    into one sentence of our own, which is what this module used to do, leaves
    the operator with nothing to act on; the bench lost an afternoon to exactly
    that on 2026-09-12.

    Scrubbed before it is returned, because the buffer it comes from is the
    terminal the password was typed on: a remote that echoes puts the password
    one line above the refusal.
    """
    for line in reversed(scrub(seen, secret).splitlines()):
        if DENIED_RE.search(line):
            return line.strip()
    return None


def password_auth_options() -> list[str]:
    """The options that make a password login mean what it says.

    Without `PubkeyAuthentication=no`, an agent key already loaded on this
    machine can satisfy the login while the operator types the wrong password --
    a green light for a credential that was never checked. Host-key checking is
    deliberately absent from this list: it stays at its default, and an unknown
    host is reported rather than accepted.
    """
    return [
        "-o", "BatchMode=no",
        "-o", "PubkeyAuthentication=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        # One prompt. The default three turns a typo into three timeouts.
        "-o", "NumberOfPasswordPrompts=1",
    ]


def _drain_loop(master_fd: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            ready, _, _ = select.select([master_fd], [], [], 0.3)
            if ready and not os.read(master_fd, 4096):
                return
        except OSError:
            return


class TtyDrain:
    """Keeps the pty master read for as long as the child is alive.

    Not tidiness -- a correctness fix, and one that only shows up as a hang.
    A child that took a pty as its controlling terminal **cannot finish
    exiting** while that terminal's buffer is unread: measured on this machine,
    a login that failed after the prompt sat in `ps` state `?Es` indefinitely,
    `poll()` answered None forever, and the caller reported a timeout instead of
    the reason ssh had already printed on stderr.

    So this starts the moment the password has been typed, before anything waits
    on the child -- not when the session object is finally built, which is after
    the window where it is needed.
    """

    def __init__(self, master_fd: int) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_drain_loop, args=(master_fd, self._stop), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
