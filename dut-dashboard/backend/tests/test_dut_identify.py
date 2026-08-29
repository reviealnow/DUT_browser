"""Asking a DUT which unit it is, over the console it is already on.

The prompt gives the model for free and that is where the free answers stop. Two
AP6_420Es print the same prompt, report the same core count, open on the same
cable and carry the same console token, so every rule this registry already has
for "is what I am holding still about this device" is blind to one being swapped
for the other. `hostname` is what separates them.

The endpoint's own job is tested here; its gate is asserted in
test_route_protection.py, and what happens to a stored capture when the answer
changes is in test_dut_registry.py.
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi import HTTPException

# Captured from the bench 420E on 2026-08-27: the command echo, the answer and
# the prompt that follows it, which is why the detector is not line-anchored.
CONSOLE_OUTPUT = "hostname\r\nAP6420E-PB1005QPCFVFMA8\r\nAP6_420E# "


class _Worker:
    """A serial worker that answers one capture with canned text."""

    mode = "serial"

    def __init__(self, answer: "str | Exception") -> None:
        self.answer = answer
        self.commands: list[tuple[str, float]] = []

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        self.commands.append((cmd, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Context:
    def __init__(self, worker) -> None:
        self.serial_worker = worker


class IdentifyEndpointTests(unittest.TestCase):
    def _call(self, worker):
        from app import main as main_mod

        registry = mock.Mock()
        registry.record_device_id.return_value = None
        with mock.patch.object(main_mod, "resolve_dut", return_value=_Context(worker)), \
             mock.patch.object(main_mod.app.state, "dut_registry", registry, create=True):
            return main_mod.identify_dut("default"), registry

    def test_it_reads_the_unit_and_records_it(self) -> None:
        result, registry = self._call(_Worker(CONSOLE_OUTPUT))
        self.assertEqual(result["device_id"], "AP6420E-PB1005QPCFVFMA8")
        registry.record_device_id.assert_called_once()
        args, kwargs = registry.record_device_id.call_args
        self.assertEqual(args[:2], ("default", "AP6420E-PB1005QPCFVFMA8"))

    def test_it_runs_one_short_command_and_no_more(self) -> None:
        """It fires on every connect, in the quiet stretch before the survey.
        Anything longer would be taking serial time from the captures around it,
        and `capture_command` pauses sysMon parsing while it runs."""
        worker = _Worker(CONSOLE_OUTPUT)
        self._call(worker)
        self.assertEqual([cmd for cmd, _ in worker.commands], ["hostname"])

    def test_it_records_the_transport_the_worker_actually_holds(self) -> None:
        """Not the one the configuration implies. A DUT registered with an SSH
        console can be opened on a cable at this desk, and then the answer came
        from the cable -- an anchor naming the wrong console would revoke the
        identity on the next open of the right one."""
        _, registry = self._call(_Worker(CONSOLE_OUTPUT))
        self.assertEqual(registry.record_device_id.call_args.kwargs["mode"], "serial")

    def test_it_reports_which_unit_it_replaced(self) -> None:
        """So a caller can say "this is a different device" without asking twice
        and racing itself between the two reads."""
        from app import main as main_mod

        registry = mock.Mock()
        registry.record_device_id.return_value = "AP6420E-PA10054DDHWVF2D"
        with mock.patch.object(main_mod, "resolve_dut", return_value=_Context(_Worker(CONSOLE_OUTPUT))), \
             mock.patch.object(main_mod.app.state, "dut_registry", registry, create=True):
            result = main_mod.identify_dut("default")
        self.assertEqual(result["changed_from"], "AP6420E-PA10054DDHWVF2D")

    def test_a_read_that_learned_nothing_records_nothing(self) -> None:
        """sysMon saturates the console for a whole run, so a command fired on
        connect can lose the line and come back with nothing. Silence is not
        evidence the hardware changed: recording it would revoke a good capture
        every time the console was merely busy."""
        result, registry = self._call(_Worker("\r\nAP6_420E# "))
        self.assertIsNone(result["device_id"])
        self.assertIsNone(result["changed_from"])
        registry.record_device_id.assert_not_called()

    def test_a_bare_prompt_is_not_an_identity(self) -> None:
        """The commonest thing a raced capture comes back with. Accepting it
        would give every unit of one model the same identity -- the exact
        failure this endpoint exists to fix, reintroduced from the other end."""
        result, registry = self._call(_Worker("AP6_420E# "))
        self.assertIsNone(result["device_id"])
        registry.record_device_id.assert_not_called()

    def test_an_unusable_console_is_a_400(self) -> None:
        """Closed or busy is a different problem from the DUT answering
        something unreadable, and the caller's to report."""
        with self.assertRaises(HTTPException) as caught:
            self._call(_Worker(RuntimeError("Serial port is closed")))
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
