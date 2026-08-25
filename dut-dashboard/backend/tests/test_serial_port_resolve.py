"""Repairing a remembered port name, without opening another DUT's console.

A USB adapter renumbers on every replug -- measured on this bench,
`/dev/cu.PL2303G-USBtoUART11130` became `...11120` inside one session -- so
Fleet's one-click Connect failed on a DUT sitting right there.

The dangerous fix is the obvious one. "The remembered port is gone, open
whichever serial port is present" attaches a DUT to whatever cable happens to
be there, and this bench has several: the telemetry, crash counts and backhaul
captures would then be filed under the wrong DUT id, which nothing downstream
can detect. So most of these tests are about what the resolver REFUSES.
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi import HTTPException

from app.serial import ports as serial_ports


def _ports(*devices: str) -> list[dict]:
    return [{"device": d, "description": "", "hwid": ""} for d in devices]


REMEMBERED = "/dev/cu.PL2303G-USBtoUART11130"
RENUMBERED = "/dev/cu.PL2303G-USBtoUART11120"


class AdapterFamilyTests(unittest.TestCase):
    def test_it_drops_the_instance_number_and_nothing_else(self) -> None:
        self.assertEqual(
            serial_ports.adapter_family(REMEMBERED), "/dev/cu.PL2303G-USBtoUART"
        )

    def test_a_name_with_no_digits_is_unchanged(self) -> None:
        self.assertEqual(serial_ports.adapter_family("/dev/ttyUSB"), "/dev/ttyUSB")

    def test_an_all_digit_name_does_not_collapse_to_nothing(self) -> None:
        """An empty family would match every port on the machine, which is the
        one value this function must never return."""
        self.assertEqual(serial_ports.adapter_family("1130"), "1130")


class ResolvePortTests(unittest.TestCase):
    def test_a_port_that_exists_is_used_untouched(self) -> None:
        port, note = serial_ports.resolve_port(REMEMBERED, _ports(REMEMBERED, RENUMBERED))
        self.assertEqual(port, REMEMBERED)
        self.assertIsNone(note)

    def test_a_renumbered_adapter_is_followed(self) -> None:
        with mock.patch("os.path.exists", return_value=False):
            port, note = serial_ports.resolve_port(REMEMBERED, _ports(RENUMBERED))
        self.assertEqual(port, RENUMBERED)
        self.assertIn(RENUMBERED, note)

    def test_two_identical_adapters_are_refused_rather_than_guessed(self) -> None:
        """The case that would put one DUT's console under another's id."""
        other = "/dev/cu.PL2303G-USBtoUART22220"
        with mock.patch("os.path.exists", return_value=False):
            with self.assertRaises(serial_ports.PortUnresolved) as caught:
                serial_ports.resolve_port(REMEMBERED, _ports(RENUMBERED, other))
        message = str(caught.exception)
        self.assertIn(RENUMBERED, message)
        self.assertIn(other, message)

    def test_a_different_adapter_is_not_a_candidate(self) -> None:
        """A DUT remembered on a PL2303 must not be opened on an FTDI cable that
        happens to be the only thing plugged in. Not substituted, and not
        refused either -- the request goes through untouched and fails on the
        device that really is missing."""
        with mock.patch("os.path.exists", return_value=False):
            port, note = serial_ports.resolve_port(REMEMBERED, _ports("/dev/cu.usbserial-FTDI1"))
        self.assertEqual(port, REMEMBERED)
        self.assertIsNone(note)

    def test_nothing_to_substitute_gets_out_of_the_way(self) -> None:
        """No candidate means no repair to make. The name goes back unchanged so
        the open fails with the OS's own account of the missing device -- a fact,
        where a message from here would be a guess about an unseen cable. It also
        keeps callers working whose port this enumeration simply does not list."""
        with mock.patch("os.path.exists", return_value=False):
            port, note = serial_ports.resolve_port(REMEMBERED, _ports())
        self.assertEqual(port, REMEMBERED)
        self.assertIsNone(note)

    def test_a_node_the_enumerator_missed_is_still_used(self) -> None:
        """`comports()` does not always list a tty that opens perfectly well.
        Substituting for a device that is right there would be the resolver
        causing the very mislabelling it exists to prevent."""
        with mock.patch("os.path.exists", return_value=True):
            port, note = serial_ports.resolve_port(REMEMBERED, _ports(RENUMBERED))
        self.assertEqual(port, REMEMBERED)
        self.assertIsNone(note)

    def test_a_blank_port_is_handed_back_for_the_caller_to_reject(self) -> None:
        self.assertEqual(serial_ports.resolve_port("", _ports(RENUMBERED)), ("", None))


class _Worker:
    def __init__(self) -> None:
        self.opened: dict | None = None
        self.current_log_path = "/tmp/session.log"

    def open(self, **kwargs) -> None:
        self.opened = kwargs


class _Registry:
    def __init__(self) -> None:
        self.recorded: tuple | None = None

    def note_console_open(self, dut_id: str, mode: str) -> None:
        pass

    def record_serial_params(self, dut_id: str, port: str, baudrate: int) -> None:
        self.recorded = (dut_id, port, baudrate)


class OpenSerialResolutionTests(unittest.TestCase):
    """The endpoint's half: open the resolved port, and remember THAT one."""

    def _call(self, requested: str, devices: list[str], mode: str = "serial"):
        from app.api.serial_api import SerialOpenRequest, open_serial

        worker = _Worker()
        registry = _Registry()
        context = mock.Mock(serial_worker=worker)
        request = mock.Mock()
        request.app.state.dut_registry = registry
        body = SerialOpenRequest(port=requested, baudrate=115200, mode=mode)
        with mock.patch.object(serial_ports, "available_ports", return_value=_ports(*devices)), \
             mock.patch("app.api.serial_api._dut", return_value=context), \
             mock.patch("os.path.exists", return_value=False):
            return open_serial(body, request), worker, registry

    def test_it_opens_the_renumbered_port_and_says_so(self) -> None:
        result, worker, _ = self._call(REMEMBERED, [RENUMBERED])
        self.assertEqual(worker.opened["port"], RENUMBERED)
        self.assertEqual(result["port"], RENUMBERED)
        self.assertIn("renumbered", result["port_note"])

    def test_it_remembers_the_port_it_opened_not_the_one_it_was_given(self) -> None:
        """Otherwise the stale entry survives and the next Connect resolves the
        same dead node again -- the entry is supposed to heal itself."""
        _, _, registry = self._call(REMEMBERED, [RENUMBERED])
        self.assertEqual(registry.recorded, ("default", RENUMBERED, 115200))

    def test_an_ambiguous_port_is_a_400_and_opens_nothing(self) -> None:
        """Two identical adapters: refuse, rather than file one DUT's console
        under another DUT's id."""
        other = "/dev/cu.PL2303G-USBtoUART22220"
        with self.assertRaises(HTTPException) as caught:
            self._call(REMEMBERED, [RENUMBERED, other])
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_missing_port_with_no_candidate_still_reaches_the_worker(self) -> None:
        """Unchanged behaviour for every caller whose port this enumeration does
        not list: the open is attempted and fails on its own terms."""
        _, worker, _ = self._call(REMEMBERED, [])
        self.assertEqual(worker.opened["port"], REMEMBERED)

    def test_a_replay_open_is_left_alone(self) -> None:
        """Replay has no port to resolve, and an SSH console's device belongs to
        the machine at the far end -- neither is this resolver's business."""
        from app.api.serial_api import SerialOpenRequest, open_serial

        worker = _Worker()
        registry = _Registry()
        request = mock.Mock()
        request.app.state.dut_registry = registry
        body = SerialOpenRequest(
            port="", baudrate=115200, mode="replay", replay_path="/tmp/x.log"
        )
        with mock.patch("app.api.serial_api._dut", return_value=mock.Mock(serial_worker=worker)):
            open_serial(body, request)
        self.assertEqual(worker.opened["mode"], "replay")
        self.assertIsNone(registry.recorded)


if __name__ == "__main__":
    unittest.main()
