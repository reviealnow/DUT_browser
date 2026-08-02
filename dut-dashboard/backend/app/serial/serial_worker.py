from __future__ import annotations

import os
import re
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

            self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=1)
            self._start_log_session(mode=mode, port=port, replay_path=replay_path, label=label)
            self._mode = "serial"
            self._thread = threading.Thread(target=self.read_loop, daemon=True)
            self._thread.start()

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
            self._mode = None
            self._terminal = False
            old_thread = self._thread
            self._thread = None

        if old_thread is not None and old_thread.is_alive() and old_thread is not threading.current_thread():
            old_thread.join(timeout=1.5)

        self.parser.flush()
        self._close_log_session()

    @property
    def current_log_path(self) -> str | None:
        return str(self._log_path) if self._log_path is not None else None

    @property
    def mode(self) -> str | None:
        """Current source mode: 'serial', 'replay', or None when idle."""
        return self._mode

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

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

    def _guarded_write(self, data: bytes, *, terminal: bool | None = None) -> None:
        """Write to the open port, turning a dead device into a clean disconnect.

        ``terminal`` optionally asserts the required terminal-mode state. The
        teardown runs outside the lock because :meth:`close` takes it too.
        """
        lost: Exception | None = None
        with self._lock:
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                raise RuntimeError(PORT_CLOSED_MESSAGE)
            if terminal is True and not self._terminal:
                raise RuntimeError("Not in terminal mode")
            try:
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
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
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
        sentinel = f"__DUTCAP_{int(time.time() * 1000) % 1_000_000:06d}__"
        # Queue behind any in-flight capture rather than rejecting it: serial is a
        # single channel and these calls arrive on independent request threads.
        if not self._capture_gate.acquire(timeout=_gate_wait_seconds(timeout)):
            raise RuntimeError("Serial capture is busy; try again")
        try:
            lost: Exception | None = None
            with self._lock:
                if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                    raise RuntimeError(PORT_CLOSED_MESSAGE)
                if self._terminal:
                    raise RuntimeError("Cannot capture while in terminal mode")
                self._capture_lines = []
                self._capture_sentinel = sentinel
                self._capture_done.clear()
                self._capture_active = True
                try:
                    self._serial.write(f"{cmd}; echo {sentinel}\n".encode("utf-8", errors="ignore"))
                    self._serial.flush()
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
        # Drop the echoed command line and the sentinel line; keep stdout only.
        out: list[str] = []
        for line in lines:
            if sentinel in line:
                continue
            if line.strip() == cmd or line.strip().endswith(f"; echo {sentinel}"):
                continue
            out.append(line)
        return "".join(out)

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
                decoded = line.decode("utf-8", errors="ignore")
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
                    continue
                self.parser.feed(decoded)
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
