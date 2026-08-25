from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import serial

from app.config import LOG_DIR
from app.parser.sysmon_parser import SysMonParser

# Allowlist for the TERM value written to the DUT shell (shell-injection guard).
_TERM_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")

# Characters NOT allowed in a user-supplied session label (it becomes a filename).
_LABEL_STRIP = re.compile(r"[^A-Za-z0-9._-]")
_LABEL_MAX = 40

# Some captures (e.g. site_survey's multi-VAP off-channel scan) legitimately
# hold the gate for up to ~70s. A short caller queued behind one must have
# enough patience to wait through it rather than giving up on "busy" well
# before the long capture finishes — so the gate-wait floor is decoupled from
# this call's own (usually much shorter) read timeout. A caller that explicitly
# asks for a longer read timeout than the floor still gets timeout + 4.0.
_GATE_WAIT_FLOOR_SEC = 90.0

PORT_CLOSED_MESSAGE = "Serial port is not open"
# Shown when the device vanished under us (adapter unplugged, DUT rebooted, port
# re-enumerated). Deliberately tells the operator to reconnect by hand — see
# _handle_device_lost for why this worker never reconnects on its own.
PORT_LOST_MESSAGE = "Serial device disconnected; reconnect it and press Connect again"
SSH_CONNECT_TIMEOUT_SEC = 8
SSH_STARTUP_GRACE_SEC = 2
# Network transit and SSH scheduling add latency beyond a local UART capture.
SSH_CAPTURE_TIMEOUT_SEC = 15.0
SSH_LOST_MESSAGE = "Remote console disconnected; check Pi reachability and socat, then reconnect"
_SSH_READY = b"__DUT_FLEET_READY__"
# Ctrl-U, written once to a console we have just taken over.
#
# A DUT shell holds an unterminated line in its input buffer until a newline
# arrives, and it keeps holding it while nobody is attached. Serial line noise
# — measured on the bench as a few bytes when a port is opened or released —
# lands in that buffer, so the first command we write is appended to the junk:
# the DUT runs "<junk>iwconfig", answers "/bin/sh: <junk>iwconfig: not found",
# and a healthy console reads back as one with no wireless interfaces. The junk
# survives our disconnects because it lives on the DUT, which is also why
# draining our own read buffer or waiting before the first command cannot help.
#
# Ctrl-U is the line kill in both busybox's line editor and the tty's canonical
# mode, and it is a no-op on an already-empty line. Deliberately not "\n":
# that would submit the noise as a command on a bench that can flash firmware.
_LINE_KILL = b"\x15"


def _gate_wait_seconds(timeout: float) -> float:
    return max(timeout + 4.0, _GATE_WAIT_FLOOR_SEC)


def sanitize_session_label(label: str | None) -> str:
    """Reduce a free-text DUT label to a filename-safe token (or "").

    The label is woven into the session-log filename, so the backend is the
    trust boundary: keep only ``[A-Za-z0-9._-]`` and cap the length. Returns ""
    when nothing usable remains (caller falls back to the per-DUT name).
    """
    if not label:
        return ""
    return _LABEL_STRIP.sub("", label)[:_LABEL_MAX]


class SerialWorker:
    _FSYNC_INTERVAL_SEC = 180

    def __init__(self, parser: SysMonParser, name: str = "") -> None:
        self.parser = parser
        # Optional per-DUT label woven into the session-log filename so concurrent
        # DUTs don't collide on the timestamp ("" keeps the original naming).
        self._name = name
        self._serial: serial.Serial | None = None
        self._ssh: subprocess.Popen[bytes] | None = None
        self._ssh_stderr: list[bytes] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._mode: str | None = None
        self._log_fp = None
        self._log_path: Path | None = None
        self._last_fsync_monotonic: float = 0.0
        # Interactive raw-terminal mode (Phase 8): when on, the reader forwards
        # raw bytes to _terminal_output instead of feeding the sysmon parser.
        self._terminal = False
        self._terminal_output = None  # type: ignore[assignment]
        # Set once per session when the device is found to be gone, so the
        # reader thread and a failing writer don't both announce it.
        self._disconnected = False
        self._on_disconnect = None  # type: ignore[assignment]
        # Synchronous command-capture (Phase 14): when active, the reader collects
        # lines into a buffer (NOT the parser) until a sentinel line is seen.
        self._capture_active = False
        self._capture_lines: list[str] = []
        self._capture_sentinel = ""
        self._capture_done = threading.Event()
        # Serial is a single channel, so concurrent captures (e.g. a Wi-Fi scan
        # overlapping a per-client apstats, each a sync route on its own thread)
        # must run one-at-a-time. Callers queue on this gate instead of failing.
        self._capture_gate = threading.Lock()

    def set_terminal_output(self, callback) -> None:
        """Register a callback(bytes) for raw serial output in terminal mode."""
        self._terminal_output = callback

    def set_disconnect_handler(self, callback) -> None:
        """Register a callback(detail: str) fired when the device vanishes mid-session."""
        self._on_disconnect = callback

    def open(
        self,
        port: str,
        baudrate: int,
        mode: str = "serial",
        replay_path: str | None = None,
        replay_interval_ms: int = 100,
        session_label: str | None = None,
        ssh: dict | None = None,
    ) -> None:
        self.close()
        self.parser.reset()
        label = sanitize_session_label(session_label)

        with self._lock:
            self._stop_event.clear()
            self._disconnected = False
            if mode == "replay":
                if not replay_path:
                    raise RuntimeError("replay_path is required when mode is replay")
                replay_file = Path(replay_path)
                if not replay_file.exists() or not replay_file.is_file():
                    raise RuntimeError(f"Replay file not found: {replay_path}")
                self._start_log_session(mode=mode, port=port, replay_path=str(replay_file), label=label)
                self._mode = "replay"
                self._thread = threading.Thread(
                    target=self._replay_loop,
                    args=(replay_file, replay_interval_ms),
                    daemon=True,
                )
                self._thread.start()
                return

            if mode == "ssh":
                if not isinstance(ssh, dict):
                    raise RuntimeError("ssh configuration is required when mode is ssh")
                self._open_ssh(ssh, baudrate)
                try:
                    self._start_log_session(mode=mode, port=port, replay_path=None, label=label)
                except Exception:
                    assert self._ssh is not None
                    # No reader thread exists yet, so the pipes can go now.
                    self._terminate_ssh(self._ssh)
                    self._close_ssh_pipes(self._ssh)
                    self._ssh = None
                    raise
                self._mode = "ssh"
                self._thread = threading.Thread(target=self._ssh_read_loop, daemon=True)
                self._thread.start()
                self._discard_stale_input_line()
                return

            self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=1)
            self._start_log_session(mode=mode, port=port, replay_path=replay_path, label=label)
            self._mode = "serial"
            self._thread = threading.Thread(target=self.read_loop, daemon=True)
            self._thread.start()
            self._discard_stale_input_line()

    def close(self) -> None:
        old_thread: threading.Thread | None = None
        with self._lock:
            self._stop_event.set()
            if self._serial is not None:
                try:
                    if self._serial.is_open:
                        self._serial.close()
                finally:
                    self._serial = None
            ssh = self._ssh
            self._ssh = None
            if ssh is not None:
                self._terminate_ssh(ssh)
            self._mode = None
            self._terminal = False
            old_thread = self._thread
            self._thread = None

        if old_thread is not None and old_thread.is_alive() and old_thread is not threading.current_thread():
            old_thread.join(timeout=1.5)

        if ssh is not None:
            # Deliberately after the join: see _close_ssh_pipes. A join that
            # timed out leaves the original window open, but holding the
            # descriptors forever would be worse than a narrow race.
            self._close_ssh_pipes(ssh)

        self.parser.flush()
        self._close_log_session()

    @property
    def current_log_path(self) -> str | None:
        return str(self._log_path) if self._log_path is not None else None

    @property
    def mode(self) -> str | None:
        """Current source mode: 'serial', 'replay', 'ssh', or None when idle."""
        return self._mode

    @property
    def is_open(self) -> bool:
        if self._mode == "ssh":
            return self._ssh is not None and self._ssh.poll() is None
        return self._serial is not None and self._serial.is_open

    def _open_ssh(self, config: dict, baudrate: int) -> None:
        """Start system ssh with a bidirectional socat serial pipe."""
        host = config["host"]
        user = config["user"]
        key_path = config["key_path"]
        ssh_port = int(config.get("port", 22))
        device = config["device"]
        command = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SEC}",
            "-o", "ConnectionAttempts=1",
            "-i", key_path,
            "-p", str(ssh_port),
            f"{user}@{host}",
            "command -v socat >/dev/null 2>&1 || { echo 'socat: command not found' >&2; exit 127; }; "
            f"echo {_SSH_READY.decode()} >&2; exec socat - {device},b{baudrate},raw,echo=0",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not start system ssh: {exc}") from exc
        self._ssh = process
        self._ssh_stderr = []
        assert process.stderr is not None
        try:
            deadline = time.monotonic() + SSH_CONNECT_TIMEOUT_SEC + SSH_STARTUP_GRACE_SEC
            stderr_fd = process.stderr.fileno()
            pending = b""
            while time.monotonic() < deadline:
                ready, _, _ = select.select([stderr_fd], [], [], 0.2)
                if stderr_fd in ready:
                    chunk = os.read(stderr_fd, 4096)
                    if chunk:
                        pending += chunk
                        if _SSH_READY in pending:
                            before, after = pending.split(_SSH_READY, 1)
                            if before:
                                self._ssh_stderr.append(before)
                            if after.strip():
                                self._ssh_stderr.append(after)
                            return
                if process.poll() is not None:
                    trailing = process.stderr.read()
                    self._ssh_stderr.append(pending + trailing)
                    raise RuntimeError(self._ssh_error_detail())
            raise RuntimeError(
                f"SSH connection timed out after {SSH_CONNECT_TIMEOUT_SEC} seconds; check Pi reachability"
            )
        except Exception:
            if self._ssh is not None:
                # Same as the caller's rollback: nothing is reading these yet.
                self._terminate_ssh(process)
                self._close_ssh_pipes(process)
                self._ssh = None
            raise

    @staticmethod
    def _terminate_ssh(process: subprocess.Popen[bytes]) -> None:
        """Always reap the SSH child. Its pipes are closed separately, later."""
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        else:
            process.wait()

    @staticmethod
    def _close_ssh_pipes(process: subprocess.Popen[bytes]) -> None:
        """Release the child's pipes.

        Separate from :meth:`_terminate_ssh`, and called only once the reader
        thread has stopped: closing a descriptor another thread is still
        selecting on frees the number for reuse, and the next transport opened
        anywhere in this process can be handed it. The reader would then be
        reading a different DUT's console into this one's parser and log.
        """
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    def _ssh_error_detail(self) -> str:
        detail = b"".join(self._ssh_stderr).decode("utf-8", errors="ignore").strip()
        if "socat" in detail and ("not found" in detail or "command not found" in detail):
            return "Remote Pi is missing socat; install socat on the Pi and reconnect"
        return detail.splitlines()[-1] if detail else SSH_LOST_MESSAGE

    def _handle_device_lost(self, detail: str) -> None:
        """Tear down after the serial device vanished mid-session.

        The disappearance is an external event (adapter unplugged, DUT rebooted,
        port re-enumerated under another name), but the reaction is ours to get
        right: pyserial's ``is_open`` is only a flag and never probes the device,
        so without this the port stays "open" forever, the UI keeps saying
        Connected, and every later write dies with ENXIO.

        Deliberately does **not** reconnect. A re-enumerated adapter can come back
        under a different device name, and silently reattaching to whatever now
        answers to that name — on a bench that can flash firmware — is worse than
        making a human press Connect.
        """
        with self._lock:
            already_handled = self._disconnected
            self._disconnected = True
        if already_handled:
            return
        # Release anyone blocked on a capture sentinel that can no longer arrive.
        self._capture_done.set()
        self.close()
        callback = self._on_disconnect
        if callback is not None:
            try:
                callback(detail)
            except Exception:
                pass  # notification is best-effort; never mask the disconnect

    def _discard_stale_input_line(self) -> None:
        """Drop the half-typed line the DUT may still be holding (see _LINE_KILL).

        Called from :meth:`open` with the lock already held, so it writes to the
        transport directly rather than through :meth:`_guarded_write`, whose own
        lock is not reentrant. Best-effort on purpose: a transport that cannot
        take one byte here is gone, and the reader thread — already running —
        reports that through the ordinary disconnect path, with a message about
        the console rather than about this write.

        Ordering is what makes this work on the SSH path: socat can only write
        this byte after it has opened the serial device, so a line dirtied by
        that very open is still cleared.
        """
        target = self._ssh.stdin if self._mode == "ssh" and self._ssh else self._serial
        if target is None:
            return
        try:
            target.write(_LINE_KILL)
            target.flush()
        except (OSError, ValueError):
            pass

    def _guarded_write(self, data: bytes, *, terminal: bool | None = None) -> None:
        """Write to the open port, turning a dead device into a clean disconnect.

        ``terminal`` optionally asserts the required terminal-mode state. The
        teardown runs outside the lock because :meth:`close` takes it too.
        """
        lost: Exception | None = None
        with self._lock:
            if self._mode not in {"serial", "ssh"} or not self.is_open:
                raise RuntimeError(PORT_CLOSED_MESSAGE)
            if terminal is True and not self._terminal:
                raise RuntimeError("Not in terminal mode")
            try:
                if self._mode == "ssh":
                    assert self._ssh is not None and self._ssh.stdin is not None
                    self._ssh.stdin.write(data)
                    self._ssh.stdin.flush()
                else:
                    assert self._serial is not None
                    self._serial.write(data)
                    self._serial.flush()
            except (OSError, ValueError) as exc:
                # SerialException subclasses OSError, so an unplugged adapter's
                # ENXIO lands here instead of escaping as a 500.
                lost = exc
        if lost is not None:
            self._handle_device_lost(str(lost) or type(lost).__name__)
            raise RuntimeError(PORT_LOST_MESSAGE) from lost

    def send(self, text: str) -> None:
        self._guarded_write(text.encode("utf-8", errors="ignore"))

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    def enter_terminal(self) -> None:
        """Switch the reader to raw passthrough (sysmon parsing pauses)."""
        with self._lock:
            if self._mode not in {"serial", "ssh"} or not self.is_open:
                raise RuntimeError(PORT_CLOSED_MESSAGE)
            self._terminal = True
        self._write_log_line("\n--- terminal session start ---\n")

    def exit_terminal(self) -> None:
        """Resume sysmon monitoring."""
        with self._lock:
            was_terminal = self._terminal
            self._terminal = False
        if was_terminal:
            self._write_log_line("\n--- terminal session end ---\n")

    def write_raw(self, data: bytes) -> None:
        """Write raw bytes to the serial port (terminal keystrokes)."""
        self._guarded_write(data)

    def resize_terminal(self, rows: int, cols: int, term: str | None = None) -> None:
        """Tell the DUT shell the terminal size (and optionally TERM) so full-screen
        apps (vi/nano) render correctly. Runs `export TERM=<term>` and
        `stty rows R cols C` at the remote prompt over the raw serial line.

        Only valid in interactive terminal mode; raises otherwise so we never inject
        commands into the sysmon monitor stream.
        """
        rows = max(1, min(int(rows), 1000))
        cols = max(1, min(int(cols), 1000))
        commands = ""
        if term is not None and _TERM_PATTERN.match(term):
            commands += f"export TERM={term}\n"
        # Best-effort: stderr suppressed so DUTs whose busybox lacks `stty` don't
        # print a "not found" error. Apps like vi also self-detect size via the
        # cursor-position (DSR) query, which xterm.js answers automatically.
        commands += f"stty rows {rows} cols {cols} 2>/dev/null\n"
        self._guarded_write(commands.encode("utf-8", errors="ignore"), terminal=True)

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        """Run a shell command on the DUT and return its stdout (serial mode).

        DUT-agnostic: appends `; echo <sentinel>` and reads monitor lines into a
        buffer (not the sysmon parser) until the sentinel line appears, or timeout.
        Mutually exclusive with terminal mode and with another in-flight capture.
        """
        if self._mode == "ssh":
            timeout = max(timeout, SSH_CAPTURE_TIMEOUT_SEC)
        sentinel = f"__DUTCAP_{int(time.time() * 1000) % 1_000_000:06d}__"
        # Queue behind any in-flight capture rather than rejecting it: serial is a
        # single channel and these calls arrive on independent request threads.
        if not self._capture_gate.acquire(timeout=_gate_wait_seconds(timeout)):
            raise RuntimeError("Serial capture is busy; try again")
        try:
            lost: Exception | None = None
            with self._lock:
                if self._mode not in {"serial", "ssh"} or not self.is_open:
                    raise RuntimeError(PORT_CLOSED_MESSAGE)
                if self._terminal:
                    raise RuntimeError("Cannot capture while in terminal mode")
                self._capture_lines = []
                self._capture_sentinel = sentinel
                self._capture_done.clear()
                self._capture_active = True
                try:
                    target = self._ssh.stdin if self._mode == "ssh" and self._ssh else self._serial
                    assert target is not None
                    target.write(f"{cmd}; echo {sentinel}\n".encode("utf-8", errors="ignore"))
                    target.flush()
                except (OSError, ValueError) as exc:
                    self._capture_active = False
                    lost = exc
            if lost is not None:
                self._handle_device_lost(str(lost) or type(lost).__name__)
                raise RuntimeError(PORT_LOST_MESSAGE) from lost

            self._capture_done.wait(timeout=timeout)

            with self._lock:
                self._capture_active = False
                self._capture_sentinel = ""
                lines = list(self._capture_lines)
                self._capture_lines = []
        finally:
            self._capture_gate.release()
        # Drop the echoed command line and the sentinel marker; keep stdout only.
        #
        # The sentinel does NOT always arrive on a line of its own. A command
        # whose output has no trailing newline shares its last line with it --
        # `{"error_msg":""}__DUTCAP_123456__` -- and dropping that whole line
        # threw the reply away. Measured on AP6840E-PD1005VMG3KJH9C, 2026-08-25:
        # the DUT's mesh API answered 200 with the body intact, and the capture
        # came back empty, so the caller reported "could not tell" about a device
        # that had just answered. Every other command captured here happens to be
        # line-oriented, which is the only reason this went unnoticed.
        #
        # So the marker is split off rather than the line discarded. The echoed
        # command line still goes whole: it carries `; echo <sentinel>` and is
        # input, not output -- the same discriminator `_consume_line` uses to
        # decide the capture has ended.
        out: list[str] = []
        for line in lines:
            if f"echo {sentinel}" in line or line.strip() == cmd:
                continue
            if sentinel in line:
                head = line.split(sentinel, 1)[0]
                if head:
                    # Restore the terminator the command never wrote, so the
                    # caller gets whole lines rather than a fragment fused to
                    # whatever a future change appends after it.
                    out.append(head if head.endswith("\n") else head + "\n")
                continue
            out.append(line)
        return "".join(out)

    def _consume_line(self, decoded: str) -> None:
        """Log one line, then either divert it to an in-flight capture or parse it.

        Both readers land here, so the sentinel rule below has one definition.
        """
        self._write_log_line(decoded)
        if self._capture_active:
            # Divert to the capture buffer instead of the parser (avoid
            # polluting CPU/crash data with the captured command's output).
            self._capture_lines.append(decoded)
            # Done when the echoed sentinel appears — tolerate a shell prompt
            # prefix ("root@AP:/# __DUTCAP__") so a prefixed marker still ends
            # the capture instead of waiting out the full timeout. Exclude the
            # echoed command line itself ("<cmd>; echo <sentinel>").
            sentinel = self._capture_sentinel
            if sentinel and sentinel in decoded and f"echo {sentinel}" not in decoded:
                self._capture_done.set()
            return
        self.parser.feed(decoded)

    def _ssh_read_loop(self) -> None:
        process = self._ssh
        if process is None or process.stdout is None or process.stderr is None:
            return
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        pending = b""
        lost: str | None = None
        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([stdout_fd, stderr_fd], [], [], 0.5)
                if stderr_fd in ready:
                    chunk = os.read(stderr_fd, 4096)
                    if chunk:
                        self._ssh_stderr.append(chunk)
                if stdout_fd in ready:
                    chunk = os.read(stdout_fd, 4096)
                    if not chunk:
                        lost = self._ssh_error_detail()
                        break
                    if self._terminal:
                        self._write_log_raw(chunk.decode("utf-8", errors="ignore"))
                        callback = self._terminal_output
                        if callback is not None:
                            callback(chunk)
                        continue
                    pending += chunk
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        self._consume_line(raw.decode("utf-8", errors="ignore") + "\n")
                if process.poll() is not None:
                    trailing = process.stderr.read()
                    if trailing:
                        self._ssh_stderr.append(trailing)
                    lost = self._ssh_error_detail()
                    break
        except Exception as exc:
            if not self._stop_event.is_set():
                lost = str(exc) or type(exc).__name__
        finally:
            if pending:
                self._consume_line(pending.decode("utf-8", errors="ignore"))
            if lost is not None and not self._stop_event.is_set():
                self._handle_device_lost(lost)

    def read_loop(self) -> None:
        # Why the reader owns disconnect detection: it is the only thread that
        # touches the device continuously, so it notices a vanished adapter
        # first. Leaving on a bare `break` (as this did) left the port "open"
        # with nobody reading it — see _handle_device_lost.
        lost: str | None = None
        try:
            while not self._stop_event.is_set():
                ser = self._serial
                if ser is None or not ser.is_open:
                    if not self._stop_event.is_set():
                        lost = "serial port closed unexpectedly"
                    break

                if self._terminal:
                    # Raw passthrough: forward bytes to the terminal, log verbatim,
                    # do NOT feed the sysmon parser (avoid polluting CPU/crash data).
                    try:
                        waiting = ser.in_waiting
                    except Exception:
                        waiting = 0
                    try:
                        data = ser.read(waiting or 1)
                    except Exception as exc:
                        lost = str(exc) or type(exc).__name__
                        break
                    if not data:
                        continue
                    self._write_log_raw(data.decode("utf-8", errors="ignore"))
                    callback = self._terminal_output
                    if callback is not None:
                        callback(data)
                    continue

                try:
                    line = ser.readline()
                except Exception as exc:
                    lost = str(exc) or type(exc).__name__
                    break
                if not line:
                    continue
                self._consume_line(line.decode("utf-8", errors="ignore"))
        finally:
            # A requested close() sets _stop_event first, so only an unrequested
            # exit counts as the device going away.
            if lost is not None and not self._stop_event.is_set():
                self._handle_device_lost(lost)

    def _replay_loop(self, replay_file: Path, replay_interval_ms: int) -> None:
        delay_sec = max(1, replay_interval_ms) / 1000.0
        try:
            with replay_file.open("r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    if self._stop_event.is_set():
                        break
                    self._write_log_line(line)
                    self.parser.feed(line)
                    time.sleep(delay_sec)
        finally:
            self.parser.flush()
            with self._lock:
                self._mode = None
                self._thread = None
            self._close_log_session()

    def _start_log_session(
        self, mode: str, port: str, replay_path: str | None, label: str = ""
    ) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # A user-supplied (sanitized) label names the log for this DUT; otherwise
        # keep the per-DUT name prefix ("" for the default DUT → original naming).
        # The "dut-session-" prefix is preserved so /api/logs + /api/logs/tail match.
        token = label or self._name
        prefix = f"{token}-" if token else ""
        self._log_path = LOG_DIR / f"dut-session-{prefix}{timestamp}.log"
        self._log_fp = self._log_path.open("a", encoding="utf-8")
        source = replay_path if mode == "replay" else port
        self._log_fp.write(f"# mode={mode} source={source}\n")
        self._log_fp.flush()
        self._last_fsync_monotonic = time.monotonic()
        os.fsync(self._log_fp.fileno())

    def _write_log_line(self, line: str) -> None:
        with self._lock:
            if self._log_fp is None:
                return
            self._log_fp.write(line)
            if not line.endswith("\n"):
                self._log_fp.write("\n")
            self._log_fp.flush()
        self._maybe_force_sync()

    def _write_log_raw(self, text: str) -> None:
        # Verbatim write for raw terminal bytes — no newline insertion.
        with self._lock:
            if self._log_fp is None:
                return
            self._log_fp.write(text)
            self._log_fp.flush()
        self._maybe_force_sync()

    def _maybe_force_sync(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._log_fp is None:
                return
            if now - self._last_fsync_monotonic < self._FSYNC_INTERVAL_SEC:
                return
            self._log_fp.flush()
            os.fsync(self._log_fp.fileno())
            self._last_fsync_monotonic = now

    def _close_log_session(self) -> None:
        with self._lock:
            if self._log_fp is not None:
                self._log_fp.flush()
                os.fsync(self._log_fp.fileno())
                self._log_fp.close()
                self._log_fp = None
