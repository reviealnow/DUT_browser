"""The DUT consoles behind a collector: finding them, and opening one.

Two halves, tested differently on purpose.

The **probe** runs against a real remote shell -- the fake `ssh` execs the
command it is handed, so `exec sh` on the far end is a genuine shell and the
commands in `collector/probe.py` are executed rather than matched against a
string. What that cannot do on this machine is produce a `/dev/ttyUSB0`, so
parsing is driven separately from canned output.

The **attach** path is exercised through the endpoint functions with a real DUT
registry writing a real `duts.json`, because the claim worth asserting is about
what lands in that file: a console behind a collector is authenticated by a
password, and that password must not be in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

import app.dut.registry as registry_mod
from app.api import collectors_api
from app.collector import probe as probe_mod
from app.collector.registry import CollectorRegistry
from app.collector.ssh_session import open_session
from app.dut.registry import DutRegistry

FAKE_SSH = str(Path(__file__).parent / "fixtures" / "fake_ssh.py")
PASSWORD = "bench-password-not-in-git"
COLLECTOR = {
    "id": "edge1", "label": "Edge collector", "ip": "192.168.30.122",
    "hostname": "raspberrypi", "user": "dut", "port": 22,
}


class _Ws:
    def emit_from_thread(self, event: dict) -> None:
        pass


@contextlib.contextmanager
def _registries_under(root: Path):
    loop = asyncio.new_event_loop()
    with (
        mock.patch.object(registry_mod, "DUTS_FILE", root / "duts.json"),
        mock.patch.object(registry_mod, "snapshot_file_for", lambda d: root / f"{d}.jsonl"),
    ):
        try:
            yield lambda: DutRegistry(_Ws(), loop)
        finally:
            loop.close()


class _StubSession:
    """A collector session that answers canned output, in the order asked."""

    def __init__(self, answers: dict[str, tuple[str, int]]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.reported_hostname = "raspberrypi"

    def run(self, command: str, timeout: float = 10) -> tuple[str, int]:
        self.asked.append(command)
        for fragment, answer in self.answers.items():
            if fragment in command:
                return answer
        return ("", 0)

    def alive(self) -> bool:
        return True


READY_PI = {
    "ls -1 /dev/ttyUSB*": ("/dev/ttyUSB0\n/dev/ttyUSB1\n", 0),
    "command -v socat": ("/usr/bin/socat", 0),
    "id -nG": ("dut adm dialout sudo", 0),
    "command -v fuser": ("yes", 0),
    "fuser": ("/dev/ttyUSB0\t\n/dev/ttyUSB1\t 2043\n", 0),
}

#: A device name shaped like a shell injection rather than a /dev path. Harmless
#: on purpose -- it must be dropped for what it looks like, not for what it does.
HOSTILE_DEVICE = "/tmp/../evil; touch /tmp/dut-probe-was-here"


class WhatTheProbeReportsTest(unittest.TestCase):
    def test_it_lists_the_serial_devices_it_found(self) -> None:
        found = probe_mod.probe(_StubSession(READY_PI))
        self.assertEqual(
            [device["device"] for device in found["devices"]],
            ["/dev/ttyUSB0", "/dev/ttyUSB1"],
        )

    def test_free_and_busy_are_different_answers(self) -> None:
        found = probe_mod.probe(_StubSession(READY_PI))
        by_name = {device["device"]: device for device in found["devices"]}
        self.assertIs(by_name["/dev/ttyUSB0"]["busy"], False)
        self.assertIs(by_name["/dev/ttyUSB1"]["busy"], True)
        # Named, because the answer to "who has it" decides the next move: a
        # minicom somebody left running is not this dashboard holding the port.
        self.assertEqual(by_name["/dev/ttyUSB1"]["held_by"], "2043")

    def test_a_box_without_fuser_says_nobody_looked(self) -> None:
        """The distinction this module exists for.

        Reporting an unchecked port as free is how a bench spends half an hour
        on a `socat` that fails against a `minicom` from yesterday.
        """
        found = probe_mod.probe(_StubSession({**READY_PI, "command -v fuser": ("no", 0)}))
        self.assertEqual(found["busy_check"], "unavailable")
        self.assertTrue(all(device["busy"] is None for device in found["devices"]))

    def test_a_device_name_that_is_not_a_dev_path_is_dropped(self) -> None:
        # Every one of these is interpolated into a socat command line on the
        # far end. A box with a creatively named device gets it dropped rather
        # than quoted and hoped for.
        found = probe_mod.probe(
            _StubSession({
                **READY_PI,
                "ls -1 /dev/ttyUSB*": (f"/dev/ttyUSB0\n{HOSTILE_DEVICE}\n", 0),
            })
        )
        self.assertEqual([d["device"] for d in found["devices"]], ["/dev/ttyUSB0"])

    def test_the_three_blockers_are_reported_as_sentences(self) -> None:
        found = probe_mod.probe(
            _StubSession({
                "ls -1 /dev/ttyUSB*": ("", 0),
                "command -v socat": ("", 1),
                "id -nG": ("dut sudo", 0),
                "command -v fuser": ("no", 0),
            })
        )
        blockers = " ".join(probe_mod.readiness(found))
        self.assertIn("socat", blockers)
        self.assertIn("dialout", blockers)
        self.assertIn("No USB serial devices", blockers)

    def test_a_ready_collector_reports_nothing_in_the_way(self) -> None:
        self.assertEqual(probe_mod.readiness(probe_mod.probe(_StubSession(READY_PI))), [])


class TheProbeAgainstARealShellTest(unittest.TestCase):
    """The commands themselves, run by a shell rather than compared to strings."""

    def setUp(self) -> None:
        os.environ["FAKE_SSH_PASSWORD"] = PASSWORD
        os.environ.pop("FAKE_SSH_MODE", None)
        self.session = open_session(
            ip="10.0.0.9", user="dut", password=PASSWORD,
            ssh_binary=FAKE_SSH, login_timeout=8,
        )
        self.addCleanup(self.session.close)

    def test_every_command_runs_and_the_result_is_well_formed(self) -> None:
        found = probe_mod.probe(self.session)
        self.assertEqual(found["hostname"], os.uname().nodename)
        self.assertIn(found["busy_check"], ("fuser", "unavailable"))
        self.assertTrue(found["serial_group"]["groups"], "id -nG returned nothing")
        # This machine is not a Pi and has no USB serial devices; the point is
        # that the listing came back empty rather than erroring.
        self.assertIsInstance(found["devices"], list)


class AttachingAConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.collectors = CollectorRegistry(state_file=self.root / "collectors.json")
        self.collectors.configure(dict(COLLECTOR), password=PASSWORD)
        self.collectors._sessions["edge1"] = _StubSession(READY_PI)  # noqa: SLF001

    @contextlib.contextmanager
    def app(self):
        with _registries_under(self.root) as make_registry:
            duts = make_registry()
            yield SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                collector_registry=self.collectors, dut_registry=duts,
            )))

    def body(self, **over):
        return collectors_api.AttachBody(device="/dev/ttyUSB0", baudrate=115200, **over)

    @staticmethod
    def console_open(is_open: bool = True):
        """A worker whose `open` does nothing and whose `is_open` answers this.

        Both halves are needed now that "attached" means a console is OPEN
        rather than a DUT being registered against the device: patching `open`
        alone leaves a registration whose worker truthfully reports closed,
        which is exactly the state the bench got stuck in.
        """
        return mock.patch.multiple(
            "app.serial.serial_worker.SerialWorker",
            open=mock.DEFAULT,
            is_open=mock.PropertyMock(return_value=is_open),
        )

    def test_the_password_is_handed_to_the_transport_and_never_persisted(self) -> None:
        """The load-bearing claim of the whole feature.

        The console has to be opened with a password, and `duts.json` has to not
        contain one. Both halves are asserted here: what `open` was given, and
        what landed on disk.
        """
        with self.app() as request, mock.patch(
            "app.serial.serial_worker.SerialWorker.open"
        ) as opened:
            collectors_api.attach_console("edge1", self.body(), request)
        self.assertEqual(opened.call_args.kwargs["ssh"]["password"], PASSWORD)
        written = (self.root / "duts.json").read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, written)
        self.assertNotIn("password", written)
        self.assertIn("edge1", written)

    def test_the_persisted_console_names_the_collector_that_opens_it(self) -> None:
        # This is what lets a restart know the console is password-authenticated
        # without storing the password: the collector is named, and the login is
        # fetched from that registry's memory at connect time.
        with self.app() as request, mock.patch("app.serial.serial_worker.SerialWorker.open"):
            collectors_api.attach_console("edge1", self.body(), request)
            remote = request.app.state.dut_registry.get("edge1-ttyusb0").remote
        self.assertEqual(remote["collector_id"], "edge1")
        self.assertEqual(remote["key_path"], "")
        self.assertNotIn("password", remote)

    def test_the_dut_id_is_derived_from_the_port_so_re_attaching_finds_it(self) -> None:
        with self.app() as request, mock.patch("app.serial.serial_worker.SerialWorker.open"):
            result = collectors_api.attach_console("edge1", self.body(), request)
        self.assertEqual(result["dut"], "edge1-ttyusb0")

    def test_a_collector_that_is_not_connected_is_refused_before_anything_opens(self) -> None:
        self.collectors._sessions.clear()  # noqa: SLF001
        with self.app() as request, mock.patch(
            "app.serial.serial_worker.SerialWorker.open"
        ) as opened:
            with self.assertRaises(HTTPException) as caught:
                collectors_api.attach_console("edge1", self.body(), request)
        opened.assert_not_called()
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_collector_whose_password_a_restart_forgot_is_refused(self) -> None:
        self.collectors.forget_password("edge1")
        with self.app() as request, mock.patch(
            "app.serial.serial_worker.SerialWorker.open"
        ) as opened:
            with self.assertRaises(HTTPException) as caught:
                collectors_api.attach_console("edge1", self.body(), request)
        opened.assert_not_called()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("memory", caught.exception.detail)

    def test_a_device_that_is_not_a_dev_path_is_refused_by_the_body(self) -> None:
        for bad in ("/etc/passwd", "/dev/../etc/passwd", HOSTILE_DEVICE):
            with self.subTest(device=bad), self.assertRaises(Exception):
                collectors_api.AttachBody(device=bad)

    def test_a_transport_that_fails_is_a_502_and_leaves_no_half_dut(self) -> None:
        """The dashboard did its part; the box upstream did not answer usably."""
        with self.app() as request, mock.patch(
            "app.serial.serial_worker.SerialWorker.open",
            side_effect=RuntimeError("Remote Pi is missing socat"),
        ):
            with self.assertRaises(HTTPException) as caught:
                collectors_api.attach_console("edge1", self.body(), request)
            self.assertEqual(caught.exception.status_code, 502)
            self.assertNotIn("edge1-ttyusb0", request.app.state.dut_registry.ids())

    def test_the_console_list_says_which_device_this_dashboard_holds(self) -> None:
        # Distinct from `busy`, which is whatever the box reports has the port.
        with self.app() as request, self.console_open():
            collectors_api.attach_console("edge1", self.body(), request)
            listed = collectors_api.list_consoles("edge1", request)
        by_name = {device["device"]: device for device in listed["devices"]}
        self.assertEqual(by_name["/dev/ttyUSB0"]["attached_dut"], "edge1-ttyusb0")
        self.assertIsNone(by_name["/dev/ttyUSB0"]["registered_dut"])
        self.assertIsNone(by_name["/dev/ttyUSB1"]["attached_dut"])

    def test_a_console_that_has_been_closed_stops_reading_as_attached(self) -> None:
        """The state the bench got stuck in on 2026-09-16.

        `attached` was computed from the registration alone, and Detach leaves
        the registration standing on purpose -- so the row claimed a session
        that had ended, the button stayed Detach, and pressing it changed
        nothing anyone could see. There was no way back to Attach at all.
        """
        with self.app() as request:
            with self.console_open():
                collectors_api.attach_console("edge1", self.body(), request)
            # Same registry, same registration; only the console has ended.
            with self.console_open(False):
                listed = collectors_api.list_consoles("edge1", request)
        row = {device["device"]: device for device in listed["devices"]}["/dev/ttyUSB0"]
        self.assertIsNone(row["attached_dut"])
        # Said, not hidden: attaching again lands on that same DUT, with the
        # history and the label it already has.
        self.assertEqual(row["registered_dut"], "edge1-ttyusb0")

    def test_detaching_a_console_that_is_not_open_is_a_404(self) -> None:
        """Rather than closing a worker that is already closed and answering ok,
        which is what made the button look broken instead of unnecessary."""
        with self.app() as request:
            with self.console_open():
                collectors_api.attach_console("edge1", self.body(), request)
            with self.console_open(False):
                with self.assertRaises(HTTPException) as caught:
                    collectors_api.detach_console("edge1", self.body(), request)
        self.assertEqual(caught.exception.status_code, 404)

    def test_detaching_closes_the_console_that_is_actually_attached(self) -> None:
        with self.app() as request, self.console_open(), \
                mock.patch("app.serial.serial_worker.SerialWorker.close") as closed:
            collectors_api.attach_console("edge1", self.body(), request)
            result = collectors_api.detach_console("edge1", self.body(), request)
        self.assertEqual(result["dut"], "edge1-ttyusb0")
        closed.assert_called()

    def test_a_console_can_be_declared_a_mesh_node(self) -> None:
        """The two declarations the retired node-registration form carried.

        Neither is measurable from a console -- an admin either said this DUT is
        in a mesh and named a fallback VAP, or nobody did. Retiring that form
        without bringing them here would have quietly dropped the ability to
        register a mesh node at all.
        """
        with self.app() as request, mock.patch("app.serial.serial_worker.SerialWorker.open"):
            collectors_api.attach_console(
                "edge1", self.body(is_mesh=True, backhaul_iface="ath16"), request
            )
            remote = request.app.state.dut_registry.get("edge1-ttyusb0").remote
        self.assertTrue(remote["is_mesh"])
        self.assertEqual(remote["backhaul_iface"], "ath16")

    def test_a_mesh_node_without_a_fallback_vap_is_refused(self) -> None:
        # Same rule the node route has always applied: a mesh node with no
        # fallback produces a root whose children silently read as an empty list.
        with self.app() as request, mock.patch(
            "app.serial.serial_worker.SerialWorker.open"
        ) as opened:
            with self.assertRaises(HTTPException) as caught:
                collectors_api.attach_console("edge1", self.body(is_mesh=True), request)
        opened.assert_not_called()
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_stale_backhaul_iface_is_not_kept_on_a_standalone_console(self) -> None:
        # Sending one with is_mesh cleared would persist a value the card then
        # reports as configured -- the same trap the old form had to avoid.
        with self.app() as request, mock.patch("app.serial.serial_worker.SerialWorker.open"):
            collectors_api.attach_console(
                "edge1", self.body(is_mesh=False, backhaul_iface="ath16"), request
            )
            remote = request.app.state.dut_registry.get("edge1-ttyusb0").remote
        self.assertFalse(remote["is_mesh"])
        self.assertIsNone(remote["backhaul_iface"])

    def test_an_interface_that_is_not_an_ath_vap_is_refused_by_the_body(self) -> None:
        for bad in ("eth0", "ath", "../ath1"):
            with self.subTest(iface=bad), self.assertRaises(Exception):
                collectors_api.AttachBody(device="/dev/ttyUSB0", backhaul_iface=bad)

    def test_detaching_a_device_nothing_is_attached_to_is_a_404(self) -> None:
        with self.app() as request:
            with self.assertRaises(HTTPException) as caught:
                collectors_api.detach_console("edge1", self.body(), request)
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
