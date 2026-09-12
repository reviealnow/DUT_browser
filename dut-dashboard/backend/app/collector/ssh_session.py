"""A password-authenticated SSH session to a collector, held open as a shell.

The terminal mechanics live in `serial/pty_ssh.py` -- one implementation, shared
with the console transport, rather than two that drift. What is here is what a
*collector* session is: a login that stays open, announces which box answered,
and can be asked to run a command.

**Why a shell and not `cat`.** The session used to hold itself open on a remote
`cat`, which proved the login and nothing else. Everything this feature is for --
finding out which serial devices are on the box, whether `socat` is installed,
whether a port is already busy -- is a command on that machine, and a second
login per question would mean holding the password to re-type it every few
seconds. So the remote end is `sh`, stdout is a command channel, and the ready
marker goes to **stderr** to keep that channel clean from the first byte.

**The password is not kept here.** It arrives as an argument, is typed once at
the prompt, and is scrubbed out of anything this module can raise. The only copy
lives in the registry's memory for as long as the process does.
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
import uuid

from app.serial import pty_ssh
from app.serial.pty_ssh import PtySshError

#: Re-exported so callers have one exception to catch for "the login failed".
CollectorSshError = PtySshError

CONNECT_TIMEOUT_SEC = 8
LOGIN_TIMEOUT_SEC = 20
#: How long one remote command may take before the session is declared unusable.
COMMAND_TIMEOUT_SEC = 15

#: Printed to stderr by the remote shell once it is running, with the box's own
#: name appended.
_READY = "__DUT_COLLECTOR_READY__"


class CollectorSession:
    """One live ssh child, its shell, and the terminal that answered its prompt.

    Held open rather than run-and-exit because the UI states that a collector is
    connected, and a light that means "a login succeeded at some point" is worse
    than no light. While this says ``alive()``, an ssh process is running and its
    remote shell has not returned.
    """

    def __init__(self, process, master_fd: int | None, reported_hostname: str, drain) -> None:
        self._process = process
        self._master_fd = master_fd
        #: What the box answered when asked its own name, at login.
        self.reported_hostname = reported_hostname
        self.opened_at = time.time()
        self._closed = threading.Event()
        # One command at a time. The remote shell is a single stream with a
        # sentinel on it: two callers interleaving their writes would each read
        # the other's output and neither would notice.
        self._rpc = threading.Lock()
        # Already running, and handed in rather than started here: see
        # pty_ssh.TtyDrain. By the time this object exists the window in which
        # an unread terminal wedges the child has been open for a whole login.
        self._drain = drain

    @property
    def pid(self) -> int:
        return self._process.pid

    def alive(self) -> bool:
        return not self._closed.is_set() and self._process.poll() is None

    def run(self, command: str, timeout: float = COMMAND_TIMEOUT_SEC) -> tuple[str, int]:
        """Run one command on the collector. Returns its merged output and status.

        `2>&1` on purpose: a command that failed is exactly the case somebody is
        reading this output to understand, and its message is on stderr.

        The sentinel carries a fresh random suffix per call rather than a fixed
        string, so a command that happens to print the marker cannot end its own
        capture early -- `ls /dev` on a box with a creatively named device is not
        a hypothetical.
        """
        if not self.alive():
            raise CollectorSshError("The SSH session to this collector is not open.")
        marker = f"__DUT_DONE_{uuid.uuid4().hex}__"
        with self._rpc:
            try:
                self._process.stdin.write(
                    f"{command} 2>&1; printf '\\n{marker}%d\\n' $?\n".encode()
                )
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CollectorSshError(f"Could not reach the collector: {exc}") from exc
            return self._read_until(marker, time.monotonic() + timeout)

    def _read_until(self, marker: str, deadline: float) -> tuple[str, int]:
        stdout_fd = self._process.stdout.fileno()
        pattern = re.compile(re.escape(marker) + r"(\d+)")
        buffer = ""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([stdout_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(stdout_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    raise CollectorSshError("The SSH session ended mid-command.")
                buffer += chunk.decode("utf-8", "replace")
                found = pattern.search(buffer)
                if found:
                    return buffer[: found.start()].strip("\r\n"), int(found.group(1))
            if self._process.poll() is not None:
                raise CollectorSshError("The SSH session ended mid-command.")
        raise CollectorSshError(f"The collector did not answer within {int(deadline)} seconds.")

    def close(self) -> None:
        """Stop the session. Safe to call twice, and on a child already gone."""
        self._closed.set()
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except Exception:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except Exception:
                    pass
        else:
            process.wait()
        # After the drain thread has been told to stop and the child is reaped:
        # closing a descriptor another thread is still selecting on frees the
        # number for reuse, and the next transport opened anywhere in this
        # process can be handed it.
        if self._drain is not None:
            self._drain.stop()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass


def _remote_command() -> str:
    """Announce the box's own name on stderr, then become a shell on stdout.

    The hostname is read here rather than over a second connection because it is
    the one fact that separates "logged in somewhere" from "logged in to the
    machine that was registered", and a separate login could reach a different
    box behind the same address.
    """
    return f"printf '{_READY}%s\\n' \"$(hostname)\" >&2; exec sh"


def open_session(
    *,
    ip: str,
    user: str,
    password: str = "",
    key_path: str | None = None,
    port: int = 22,
    ssh_binary: str = "ssh",
    login_timeout: float = LOGIN_TIMEOUT_SEC,
) -> CollectorSession:
    """Log in and return the held-open session.

    Two ways in, matching `SerialWorker._open_ssh` because they are the same two
    and a second answer to "how does this collector authenticate" is how the two
    halves of one model drift apart:

    * a **key**, with ``BatchMode=yes`` -- no terminal is needed or wanted,
      because BatchMode means ssh will never stop to ask a human anything;
    * a **password**, typed at a controlling terminal that carries the prompt
      and nothing else.

    Raises :class:`CollectorSshError` with a message meant for the operator,
    never with the password in it.
    """
    common = [
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SEC}",
        "-o", "ConnectionAttempts=1",
        "-p", str(port),
        f"{user}@{ip}",
        _remote_command(),
    ]
    drain: pty_ssh.TtyDrain | None = None
    if key_path:
        argv = [ssh_binary, "-o", "BatchMode=yes", "-i", key_path, *common]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            raise CollectorSshError(f"Could not start ssh: {exc}") from exc
        master_fd = None
    else:
        argv = [ssh_binary, *pty_ssh.password_auth_options(), *common]
        process, master_fd = pty_ssh.spawn_with_tty(argv)

    def fail(message: str) -> CollectorSshError:
        """Reap the half-open child, then hand back the error to raise.

        A login that failed and left an ssh process attached to a pty nobody
        reads is the worst of both: the operator is told it did not work, and
        the collector still has a session on it.
        """
        CollectorSession(
            process, master_fd, "",
            drain or (pty_ssh.TtyDrain(master_fd) if master_fd is not None else None),
        ).close()
        return CollectorSshError(message)

    try:
        if master_fd is not None:
            pty_ssh.answer_password_prompt(
                master_fd, password, process, timeout=login_timeout
            )
            # Before anything waits on the child. A terminal nobody reads keeps
            # a session leader from finishing its exit, so without this a login
            # that failed after the prompt reports a timeout instead of ssh's
            # own reason.
            drain = pty_ssh.TtyDrain(master_fd)
        hostname = _await_ready(process, password, time.monotonic() + login_timeout)
    except PtySshError as exc:
        raise fail(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never leak the child
        raise fail(f"SSH session failed to start: {exc}") from exc
    return CollectorSession(process, master_fd, hostname, drain)


def _await_ready(process, password: str, deadline: float) -> str:
    """Read stderr until the shell announces itself, or say why it never did."""
    stderr_fd = process.stderr.fileno()
    seen = ""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stderr_fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(stderr_fd, 4096)
            except OSError:
                chunk = b""
            if chunk:
                seen += chunk.decode("utf-8", "replace")
                if _READY in seen:
                    after = seen.split(_READY, 1)[1]
                    # The name may still be arriving; the newline is what says
                    # it is whole. Waiting costs one more read and avoids
                    # reporting a truncated hostname as a mismatch.
                    if "\n" in after:
                        return after.split("\n", 1)[0].strip()
                    continue
                if pty_ssh.DENIED_RE.search(seen):
                    # ssh's own words, carried rather than replaced: which
                    # refusal it is decides the next move, and this branch used
                    # to answer all three with one sentence. `denial_line`
                    # scrubs the password out of it.
                    said = pty_ssh.denial_line(seen, password)
                    raise PtySshError(
                        "The collector refused these credentials."
                        + (f' ssh said: "{said}"' if said else "")
                    )
                if pty_ssh.HOSTKEY_RE.search(seen):
                    raise PtySshError(pty_ssh.HOSTKEY_MESSAGE)
        if process.poll() is not None:
            detail = pty_ssh.scrub(seen.strip(), password).splitlines()
            raise PtySshError(
                detail[-1] if detail else "SSH exited before the login finished"
            )
    raise PtySshError("The collector did not finish logging in in time.")
