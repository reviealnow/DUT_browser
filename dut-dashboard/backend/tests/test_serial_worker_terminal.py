from __future__ import annotations

import re
import threading
import time
import unittest

from app.serial.serial_worker import SerialWorker


class StubParser:
    def __init__(self) -> None:
        self.fed: list[str] = []

    def reset(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def feed(self, line: str) -> None:
        self.fed.append(line)


class FakeSerial:
    """In-memory serial double: feed() simulates DUT output; outputs records writes."""

    def __init__(self) -> None:
        self.is_open = True
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.outputs = bytearray()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, n: int) -> bytes:
        deadline = time.time() + 0.1
        while time.time() < deadline:
            with self._lock:
                if self._buf:
                    take = bytes(self._buf[:n])
                    del self._buf[:n]
                    return take
            time.sleep(0.01)
        return b""

    def readline(self) -> bytes:
        deadline = time.time() + 0.1
        while time.time() < deadline:
            with self._lock:
                nl = self._buf.find(b"\n")
                if nl >= 0:
                    line = bytes(self._buf[: nl + 1])
                    del self._buf[: nl + 1]
                    return line
            time.sleep(0.01)
        return b""

    def write(self, data: bytes) -> None:
        with self._lock:
            self.outputs.extend(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SerialWorkerTerminalTests(unittest.TestCase):
    def test_enter_requires_serial_open(self) -> None:
        worker = SerialWorker(StubParser())
        with self.assertRaises(RuntimeError):
            worker.enter_terminal()

    def test_terminal_routes_raw_and_pauses_parser(self) -> None:
        parser = StubParser()
        worker = SerialWorker(parser)
        captured: list[bytes] = []
        worker.set_terminal_output(captured.append)

        fake = FakeSerial()
        worker._serial = fake  # type: ignore[attr-defined]
        worker._mode = "serial"  # type: ignore[attr-defined]
        worker._stop_event.clear()
        thread = threading.Thread(target=worker.read_loop, daemon=True)
        thread.start()
        try:
            # Terminal mode: raw forwarded, keystrokes written, parser NOT fed.
            worker.enter_terminal()
            self.assertTrue(worker.is_terminal)
            fake.feed(b"\x1b[2Jvi-screen")
            worker.write_raw(b"ihello")
            self.assertTrue(_wait(lambda: b"vi-screen" in b"".join(captured)))
            self.assertIn(b"ihello", bytes(fake.outputs))
            self.assertEqual(parser.fed, [])

            # Monitor mode: lines fed to the parser again. Let any in-flight
            # terminal read settle first (mode switch resolves within one read
            # interval — the documented ≤1-read boundary behavior).
            worker.exit_terminal()
            self.assertFalse(worker.is_terminal)
            time.sleep(0.15)
            fake.feed(b"sysmon-line\n")
            self.assertTrue(_wait(lambda: any("sysmon-line" in x for x in parser.fed)))
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            fake.close()
            thread.join(timeout=1.0)

    def _terminal_worker(self) -> tuple[SerialWorker, "FakeSerial"]:
        worker = SerialWorker(StubParser())
        fake = FakeSerial()
        worker._serial = fake  # type: ignore[attr-defined]
        worker._mode = "serial"  # type: ignore[attr-defined]
        worker.enter_terminal()
        return worker, fake

    def test_resize_writes_stty_and_term(self) -> None:
        worker, fake = self._terminal_worker()
        worker.resize_terminal(40, 100, term="xterm")
        out = bytes(fake.outputs)
        self.assertIn(b"export TERM=xterm\n", out)
        self.assertIn(b"stty rows 40 cols 100 2>/dev/null\n", out)
        # TERM must be exported before stty.
        self.assertLess(out.index(b"export TERM"), out.index(b"stty"))

    def test_resize_without_term_writes_only_stty(self) -> None:
        worker, fake = self._terminal_worker()
        worker.resize_terminal(24, 80)
        out = bytes(fake.outputs)
        self.assertNotIn(b"export TERM", out)
        self.assertIn(b"stty rows 24 cols 80 2>/dev/null\n", out)

    def test_resize_rejects_bad_term(self) -> None:
        worker, fake = self._terminal_worker()
        worker.resize_terminal(24, 80, term="xterm; rm -rf /")
        out = bytes(fake.outputs)
        self.assertNotIn(b"export TERM", out)  # bad term silently dropped
        self.assertIn(b"stty rows 24 cols 80 2>/dev/null\n", out)

    def test_resize_clamps_out_of_range(self) -> None:
        worker, fake = self._terminal_worker()
        worker.resize_terminal(0, 99999)
        self.assertIn(b"stty rows 1 cols 1000 2>/dev/null\n", bytes(fake.outputs))

    def test_resize_requires_terminal_mode(self) -> None:
        worker = SerialWorker(StubParser())
        fake = FakeSerial()
        worker._serial = fake  # type: ignore[attr-defined]
        worker._mode = "serial"  # type: ignore[attr-defined]
        # serial open but not in terminal mode -> refuse (never inject at monitor prompt).
        with self.assertRaises(RuntimeError):
            worker.resize_terminal(24, 80)

    def test_resize_requires_serial_open(self) -> None:
        worker = SerialWorker(StubParser())
        with self.assertRaises(RuntimeError):
            worker.resize_terminal(24, 80)


class _AutoFakeSerial(FakeSerial):
    """When the captured command is written (`cmd; echo <sentinel>`), queue the
    command's fake stdout followed by the sentinel line so read_loop completes."""

    def write(self, data: bytes) -> None:
        super().write(data)
        text = data.decode("utf-8", errors="ignore")
        m = re.search(r"echo (\S+)", text)
        if m:
            self.feed(b"vap-output-line\n" + m.group(1).encode() + b"\n")


class _PromptPrefixFakeSerial(FakeSerial):
    """Like _AutoFakeSerial but echoes the sentinel behind a shell prompt, e.g.
    `root@AP:/# __DUTCAP__`, to exercise prompt-prefixed sentinel detection."""

    def write(self, data: bytes) -> None:
        super().write(data)
        text = data.decode("utf-8", errors="ignore")
        m = re.search(r"echo (\S+)", text)
        if m:
            self.feed(b"stats-line\nroot@AP:/# " + m.group(1).encode() + b"\n")


class CaptureCommandTests(unittest.TestCase):
    def _running_worker(self, fake) -> tuple[SerialWorker, StubParser, threading.Thread]:
        parser = StubParser()
        worker = SerialWorker(parser)
        worker._serial = fake  # type: ignore[attr-defined]
        worker._mode = "serial"  # type: ignore[attr-defined]
        worker._stop_event.clear()
        t = threading.Thread(target=worker.read_loop, daemon=True)
        t.start()
        return worker, parser, t

    def test_capture_returns_stdout_and_skips_parser(self) -> None:
        fake = _AutoFakeSerial()
        worker, parser, t = self._running_worker(fake)
        try:
            out = worker.capture_command("iwconfig", timeout=2.0)
            self.assertIn("vap-output-line", out)
            self.assertNotIn("__DUTCAP", out)           # sentinel stripped
            self.assertFalse(worker._capture_active)      # cleared after return
            self.assertEqual(parser.fed, [])              # parser not fed during capture
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            fake.close()
            t.join(timeout=1.0)

    def test_capture_timeout_returns_and_clears(self) -> None:
        fake = FakeSerial()  # never feeds a sentinel back
        worker, _parser, t = self._running_worker(fake)
        try:
            out = worker.capture_command("iwconfig", timeout=0.3)
            self.assertEqual(out, "")
            self.assertFalse(worker._capture_active)       # state cleared on timeout
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            fake.close()
            t.join(timeout=1.0)

    def test_capture_requires_serial_open(self) -> None:
        with self.assertRaises(RuntimeError):
            SerialWorker(StubParser()).capture_command("iwconfig")

    def test_concurrent_captures_serialize(self) -> None:
        # Two captures on separate threads (mirrors a Wi-Fi scan overlapping a
        # per-client apstats) must both succeed by queueing — never raise
        # "A capture is already in progress".
        fake = _AutoFakeSerial()
        worker, _parser, t = self._running_worker(fake)
        results: dict[str, object] = {}

        def run(key: str, cmd: str) -> None:
            try:
                results[key] = worker.capture_command(cmd, timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - record for assertion
                results[key] = exc

        try:
            a = threading.Thread(target=run, args=("a", "iwconfig"))
            b = threading.Thread(target=run, args=("b", "apstats -s -m x"))
            a.start()
            b.start()
            a.join(timeout=5.0)
            b.join(timeout=5.0)
            self.assertIsInstance(results.get("a"), str)
            self.assertIsInstance(results.get("b"), str)
            self.assertIn("vap-output-line", results["a"])  # type: ignore[arg-type]
            self.assertIn("vap-output-line", results["b"])  # type: ignore[arg-type]
            self.assertFalse(worker._capture_active)
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            fake.close()
            t.join(timeout=1.0)

    def test_capture_completes_with_prompt_prefixed_sentinel(self) -> None:
        # A sentinel echoed behind a shell prompt ("root@AP:/# __DUTCAP__") must
        # still end the capture promptly instead of waiting out the timeout.
        fake = _PromptPrefixFakeSerial()
        worker, _parser, t = self._running_worker(fake)
        try:
            start = time.time()
            out = worker.capture_command("apstats", timeout=2.0)
            elapsed = time.time() - start
            self.assertIn("stats-line", out)
            self.assertNotIn("__DUTCAP", out)         # sentinel stripped
            self.assertLess(elapsed, 1.5)             # completed, did not time out
            self.assertFalse(worker._capture_active)
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            fake.close()
            t.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
