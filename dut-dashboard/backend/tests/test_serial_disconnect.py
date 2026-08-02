"""The serial device vanishing mid-session, as reported from a live deployment.

``POST /api/serial/send`` returned 500 in a loop with
``SerialException: write failed: [Errno 6] Device not configured``. Errno 6
(ENXIO) means the file descriptor is dead -- the adapter was unplugged, the DUT
rebooted, or the port re-enumerated under a different name. That much is an
external event.

What was ours to fix is the app's reaction. ``read_loop`` left on a bare
``break``: no close, no state reset, nobody told. pyserial's ``is_open`` is only
a flag and never probes the device, so the port stayed "open" forever, the UI
kept showing Connected, ``send``'s guard kept letting writes through, and
``SerialException`` -- an ``OSError``, *not* a ``RuntimeError`` -- sailed past
the endpoint's handler as a 500 with a traceback.

By explicit decision the worker does **not** reconnect: a re-enumerated adapter
may come back under another name, and reattaching blind on a bench that can
flash firmware is more dangerous than making a human press Connect.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import serial
from fastapi.testclient import TestClient

from app.db import workspace
from app.main import app
from app.serial.serial_worker import SerialWorker
from app.services import auth_service, file_service

WRITE_ENXIO = "write failed: [Errno 6] Device not configured"
READ_ENXIO = "device reports readiness to read but returned no data"


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
    """In-memory serial double that can be told to die like an unplugged adapter."""

    def __init__(self, *, fail_read: bool = False, fail_write: bool = False) -> None:
        self.is_open = True
        self.closed = False
        self.fail_read = fail_read
        self.fail_write = fail_write
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
        if self.fail_read:
            raise serial.SerialException(READ_ENXIO)
        with self._lock:
            take = bytes(self._buf[:n])
            del self._buf[:n]
        return take

    def readline(self) -> bytes:
        if self.fail_read:
            raise serial.SerialException(READ_ENXIO)
        deadline = time.time() + 0.05
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
        if self.fail_write:
            raise serial.SerialException(WRITE_ENXIO)
        with self._lock:
            self.outputs.extend(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False
        self.closed = True


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class DeviceLostTests(unittest.TestCase):
    def _worker(self, fake: FakeSerial) -> tuple[SerialWorker, list[str]]:
        """An attached worker (no reader thread) plus the disconnect events it emits."""
        worker = SerialWorker(StubParser())
        events: list[str] = []
        worker.set_disconnect_handler(events.append)
        worker._serial = fake  # type: ignore[attr-defined]
        worker._mode = "serial"  # type: ignore[attr-defined]
        worker._stop_event.clear()
        return worker, events

    def _reading_worker(self, fake: FakeSerial) -> tuple[SerialWorker, list[str], threading.Thread]:
        worker, events = self._worker(fake)
        thread = threading.Thread(target=worker.read_loop, daemon=True)
        thread.start()
        return worker, events, thread

    # -- the reader notices first ------------------------------------------

    def test_a_read_failure_closes_the_port_and_announces_it(self) -> None:
        fake = FakeSerial(fail_read=True)
        worker, events, thread = self._reading_worker(fake)
        try:
            self.assertTrue(_wait(lambda: not worker.is_open), "worker still reports the port open")
            self.assertIsNone(worker.mode)
            self.assertTrue(fake.closed, "the underlying port was never closed")
            self.assertTrue(_wait(lambda: len(events) == 1))
            self.assertIn("readiness to read", events[0])
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            thread.join(timeout=1.0)

    def test_a_read_failure_in_terminal_mode_also_disconnects(self) -> None:
        """The terminal read path had its own bare `break` -- same bug, second door."""
        fake = FakeSerial()
        worker, events, thread = self._reading_worker(fake)
        try:
            worker.enter_terminal()
            fake.fail_read = True
            self.assertTrue(_wait(lambda: not worker.is_open))
            self.assertTrue(_wait(lambda: len(events) == 1))
            self.assertFalse(worker.is_terminal)
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            thread.join(timeout=1.0)

    def test_a_send_after_the_device_is_gone_is_a_runtime_error(self) -> None:
        """The endpoint only handles RuntimeError, so the worker must not leak
        SerialException (an OSError) to it -- that leak was the reported 500."""
        fake = FakeSerial(fail_read=True)
        worker, _events, thread = self._reading_worker(fake)
        try:
            self.assertTrue(_wait(lambda: not worker.is_open))
            with self.assertRaises(RuntimeError):
                worker.send("show version\n")
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            thread.join(timeout=1.0)

    def test_a_requested_close_is_not_reported_as_a_disconnect(self) -> None:
        """A user pressing Close must not raise a "device disconnected" alarm."""
        fake = FakeSerial()
        worker, events, thread = self._reading_worker(fake)
        worker.close()
        thread.join(timeout=1.0)
        time.sleep(0.1)
        self.assertEqual(events, [])
        self.assertFalse(worker.is_open)

    # -- a writer can notice first too --------------------------------------

    def test_a_write_failure_disconnects_and_raises_runtime_error(self) -> None:
        """The exact reported call: write hits ENXIO before any read has failed."""
        fake = FakeSerial(fail_write=True)
        worker, events = self._worker(fake)
        with self.assertRaises(RuntimeError):
            worker.send("show version\n")
        self.assertFalse(worker.is_open, "the port was left open after a dead write")
        self.assertTrue(fake.closed)
        self.assertEqual(len(events), 1)
        self.assertIn("Errno 6", events[0])

    def test_a_failed_capture_disconnects_and_clears_capture_state(self) -> None:
        fake = FakeSerial(fail_write=True)
        worker, events = self._worker(fake)
        with self.assertRaises(RuntimeError):
            worker.capture_command("iwconfig", timeout=1.0)
        self.assertFalse(worker.is_open)
        self.assertFalse(worker._capture_active)  # type: ignore[attr-defined]
        self.assertEqual(len(events), 1)

    def test_the_disconnect_is_announced_once(self) -> None:
        """Reader and writer can both discover the loss; the UI hears it once."""
        fake = FakeSerial(fail_read=True, fail_write=True)
        worker, events, thread = self._reading_worker(fake)
        try:
            self.assertTrue(_wait(lambda: len(events) == 1))
            with self.assertRaises(RuntimeError):
                worker.send("x")
            time.sleep(0.1)
            self.assertEqual(len(events), 1)
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            thread.join(timeout=1.0)

    def test_an_in_flight_capture_wakes_when_the_device_dies(self) -> None:
        """A capture waits on a sentinel the dead device will never echo. Without
        the teardown releasing it, a Wi-Fi scan blocks a request thread for the
        full timeout after the adapter is already gone."""
        fake = FakeSerial()
        worker, _events, thread = self._reading_worker(fake)
        try:
            def kill() -> None:
                time.sleep(0.1)
                fake.fail_read = True

            threading.Thread(target=kill, daemon=True).start()
            start = time.time()
            worker.capture_command("iw dev ath0 scan", timeout=5.0)
            self.assertLess(time.time() - start, 2.0, "capture waited out the full timeout")
        finally:
            worker._stop_event.set()  # type: ignore[attr-defined]
            thread.join(timeout=1.0)

    def test_reopening_re_arms_the_disconnect_report(self) -> None:
        """The once-only flag is per session, not per worker: a reconnected DUT
        that dies again must be reported again."""
        fake = FakeSerial(fail_write=True)
        worker, events = self._worker(fake)
        with self.assertRaises(RuntimeError):
            worker.send("x")
        second = FakeSerial(fail_write=True)
        with patch.object(serial, "Serial", return_value=second):
            worker.open(port="/dev/fake", baudrate=115200)
        with self.assertRaises(RuntimeError):
            worker.send("x")
        worker.close()
        self.assertEqual(len(events), 2)


class SendEndpointStatusTests(unittest.TestCase):
    """The bug as the deployment saw it: an HTTP status, not an exception type.

    Goes through TestClient so FastAPI's exception handling is real -- calling
    the endpoint function directly would never have shown the 500.
    """

    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        self._stack.enter_context(patch.object(file_service, "UPLOAD_DIR", self._dir / "uploads"))
        self._stack.enter_context(
            patch.object(auth_service, "SESSION_SECRET_FILE", self._dir / "session_secret")
        )
        workspace.init_db()
        self.client = TestClient(app)

        self.fake = FakeSerial(fail_write=True)
        self.worker = SerialWorker(StubParser())
        self.worker._serial = self.fake  # type: ignore[attr-defined]
        self.worker._mode = "serial"  # type: ignore[attr-defined]

        worker = self.worker

        class _Context:
            serial_worker = worker

        class _Registry:
            def get(self, dut_id: str):
                if dut_id == "default":
                    return _Context()
                raise KeyError(dut_id)

        self._stack.enter_context(
            patch.object(app.state, "dut_registry", _Registry(), create=True)
        )
        user = auth_service.create_or_update_user("qa-engineer", "engineer", "engineer")
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))

    def test_send_to_a_vanished_device_is_400_not_500(self) -> None:
        response = self.client.post("/api/serial/send", json={"text": "show version\n"})
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("disconnect", detail.lower())
        self.assertNotIn("Errno", detail)  # no raw errno string in user-facing copy

    def test_the_second_send_reports_a_closed_port_rather_than_failing_again(self) -> None:
        """After the first failure the port is really closed, so the follow-up is
        the ordinary "not open" refusal -- not another dead write."""
        self.client.post("/api/serial/send", json={"text": "a\n"})
        response = self.client.post("/api/serial/send", json={"text": "b\n"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("not open", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
