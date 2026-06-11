from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
