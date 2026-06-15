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
        # Synchronous command-capture (Phase 14): when active, the reader collects
        # lines into a buffer (NOT the parser) until a sentinel line is seen.
        self._capture_active = False
        self._capture_lines: list[str] = []
        self._capture_sentinel = ""
        self._capture_done = threading.Event()

    def set_terminal_output(self, callback) -> None:
        """Register a callback(bytes) for raw serial output in terminal mode."""
        self._terminal_output = callback

    def open(
        self,
        port: str,
        baudrate: int,
        mode: str = "serial",
        replay_path: str | None = None,
        replay_interval_ms: int = 100,
    ) -> None:
        self.close()
        self.parser.reset()

        with self._lock:
            self._stop_event.clear()
            if mode == "replay":
                if not replay_path:
                    raise RuntimeError("replay_path is required when mode is replay")
                replay_file = Path(replay_path)
                if not replay_file.exists() or not replay_file.is_file():
                    raise RuntimeError(f"Replay file not found: {replay_path}")
                self._start_log_session(mode=mode, port=port, replay_path=str(replay_file))
                self._mode = "replay"
                self._thread = threading.Thread(
                    target=self._replay_loop,
                    args=(replay_file, replay_interval_ms),
                    daemon=True,
                )
                self._thread.start()
                return

            self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=1)
            self._start_log_session(mode=mode, port=port, replay_path=replay_path)
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

    def send(self, text: str) -> None:
        with self._lock:
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                raise RuntimeError("Serial port is not open")
            self._serial.write(text.encode("utf-8", errors="ignore"))
            self._serial.flush()

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    def enter_terminal(self) -> None:
        """Switch the reader to raw passthrough (sysmon parsing pauses)."""
        with self._lock:
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                raise RuntimeError("Serial port is not open")
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
        with self._lock:
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                raise RuntimeError("Serial port is not open")
            self._serial.write(data)
            self._serial.flush()

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
        with self._lock:
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                raise RuntimeError("Serial port is not open")
            if not self._terminal:
                raise RuntimeError("Not in terminal mode")
            self._serial.write(commands.encode("utf-8", errors="ignore"))
            self._serial.flush()

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        """Run a shell command on the DUT and return its stdout (serial mode).

        DUT-agnostic: appends `; echo <sentinel>` and reads monitor lines into a
        buffer (not the sysmon parser) until the sentinel line appears, or timeout.
        Mutually exclusive with terminal mode and with another in-flight capture.
        """
        sentinel = f"__DUTCAP_{int(time.time() * 1000) % 1_000_000:06d}__"
        with self._lock:
            if self._mode != "serial" or self._serial is None or not self._serial.is_open:
                raise RuntimeError("Serial port is not open")
            if self._terminal:
                raise RuntimeError("Cannot capture while in terminal mode")
            if self._capture_active:
                raise RuntimeError("A capture is already in progress")
            self._capture_lines = []
            self._capture_sentinel = sentinel
            self._capture_done.clear()
            self._capture_active = True
            self._serial.write(f"{cmd}; echo {sentinel}\n".encode("utf-8", errors="ignore"))
            self._serial.flush()

        self._capture_done.wait(timeout=timeout)

        with self._lock:
            self._capture_active = False
            lines = list(self._capture_lines)
            self._capture_lines = []
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
        while not self._stop_event.is_set():
            ser = self._serial
            if ser is None or not ser.is_open:
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
                except Exception:
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
            except Exception:
                break
            if not line:
                continue
            decoded = line.decode("utf-8", errors="ignore")
            self._write_log_line(decoded)
            if self._capture_active:
                # Divert to the capture buffer instead of the parser (avoid
                # polluting CPU/crash data with the captured command's output).
                self._capture_lines.append(decoded)
                # Match the sentinel only when it is echoed on its own line — not
                # in the echoed command itself ("<cmd>; echo <sentinel>").
                if self._capture_sentinel and decoded.strip() == self._capture_sentinel:
                    self._capture_done.set()
                continue
            self.parser.feed(decoded)

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

    def _start_log_session(self, mode: str, port: str, replay_path: str | None) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = f"{self._name}-" if self._name else ""
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
