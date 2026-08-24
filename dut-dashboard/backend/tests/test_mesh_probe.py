"""The console transport: asking a DUT about its own mesh over the cable.

The HTTP transport (test_mesh_topology.py) needs a management address and
credentials an admin has to have set. This one needs neither -- it has the DUT
curl its own API over loopback -- so it works on a device somebody has just
cabled up, which is when the question is worth asking.

Both transports share `parse_mesh_payload`, which is already covered there. What
is new here, and what these tests are about, is everything between the console
and that parser: the DUT does not terminate its JSON with a newline, so the next
shell prompt arrives glued to the closing brace.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi import HTTPException

from app.services import mesh_topology
from tests.test_mesh_topology import REAL_PAYLOAD


# Exactly what the console showed on 2026-08-24, prompt and all. Note there is
# no newline between `}` and the prompt -- that is the point of this fixture.
CONSOLE_OUTPUT = json.dumps(REAL_PAYLOAD, separators=(",", ":")) + "AP6_420E# "


class ExtractJsonObjectTests(unittest.TestCase):
    def test_it_stops_at_the_closing_brace_not_at_the_prompt(self) -> None:
        body = mesh_topology.extract_json_object(CONSOLE_OUTPUT)
        self.assertIsNotNone(body)
        self.assertTrue(body.endswith("}"))
        self.assertNotIn("AP6_420E", body)
        self.assertEqual(json.loads(body)["error_code"], 0)

    def test_it_survives_a_command_typed_after_the_reply(self) -> None:
        """The operator's own capture ended `...""}AP6_420E# pwd`. Taking
        everything up to the last brace would swallow whatever came next."""
        body = mesh_topology.extract_json_object(CONSOLE_OUTPUT + "pwd\n/root\n")
        self.assertEqual(json.loads(body)["data"]["total_size"], 2)

    def test_a_brace_inside_a_string_does_not_cut_the_object_short(self) -> None:
        """An SSID or an error message may contain a brace. Counting braces
        without knowing about strings would truncate a perfectly good reply."""
        payload = {"data": {"mesh_info_list": []}, "error_code": 0, "error_msg": "no {mesh} here"}
        body = mesh_topology.extract_json_object(json.dumps(payload) + "AP6# ")
        self.assertEqual(json.loads(body)["error_msg"], "no {mesh} here")

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        payload = {"data": {"mesh_info_list": []}, "error_code": 0, "error_msg": 'say \\"}\\" ok'}
        body = mesh_topology.extract_json_object(json.dumps(payload) + "AP6# ")
        self.assertEqual(json.loads(body), payload)

    def test_a_truncated_body_is_none_rather_than_a_guess(self) -> None:
        """A capture whose window closed mid-reply. Returning the fragment would
        hand the parser something that cannot be JSON."""
        self.assertIsNone(mesh_topology.extract_json_object(CONSOLE_OUTPUT[:80]))

    def test_output_with_no_json_at_all_is_none(self) -> None:
        self.assertIsNone(mesh_topology.extract_json_object("sh: curl: not found\nAP6# "))
        self.assertIsNone(mesh_topology.extract_json_object(""))


class _Worker:
    """A serial worker that answers one capture with canned text."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.commands: list[tuple[str, float]] = []

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        self.commands.append((cmd, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class ProbeMeshOverConsoleTests(unittest.TestCase):
    def test_a_meshed_dut_reports_its_members(self) -> None:
        worker = _Worker(CONSOLE_OUTPUT)
        result = mesh_topology.probe_mesh_over_console(worker)
        self.assertTrue(result["probed"])
        self.assertIs(result["mesh"], True)
        self.assertEqual([m["ip"] for m in result["members"]],
                         ["192.168.30.121", "192.168.30.176"])
        # The shared parser did the work, so the root's 0 is nulled here too --
        # the two transports cannot disagree about what that field means.
        self.assertIsNone(result["members"][0]["rssi"])

    def test_it_asks_over_loopback_with_no_credentials(self) -> None:
        """The whole reason this transport exists: it works on a DUT nobody has
        configured. A probe that needed -u would need the settings page first."""
        worker = _Worker(CONSOLE_OUTPUT)
        mesh_topology.probe_mesh_over_console(worker)
        cmd, timeout = worker.commands[0]
        self.assertIn("127.0.0.1:10443/ap/info/wireless/mesh", cmd)
        self.assertNotIn("-u ", cmd)
        # Without -s, curl draws a progress meter into the middle of the body.
        self.assertIn("-s", cmd)
        # Longer than capture_command's 6s default: a TLS handshake on the DUT's
        # own CPU that times out would be read as "this DUT has no mesh".
        self.assertGreater(timeout, 6.0)

    def test_an_empty_list_is_the_one_branch_allowed_to_say_no_mesh(self) -> None:
        worker = _Worker('{"data":{"mesh_info_list":[],"total_size":0},"error_code":0}AP6# ')
        result = mesh_topology.probe_mesh_over_console(worker)
        self.assertIs(result["mesh"], False)
        self.assertEqual(result["members"], [])
        self.assertIn("empty", result["detail"])

    def test_a_device_error_is_could_not_tell_and_not_no_mesh(self) -> None:
        """The distinction this module is built around. Nobody has yet captured
        this endpoint on a DUT with mesh disabled, so an error_code might mean
        "no mesh" or might mean "something broke" -- and printing "no mesh" over
        a device that merely failed to answer is the confident wrong answer."""
        worker = _Worker('{"data":null,"error_code":7,"error_msg":"mesh not enabled"}AP6# ')
        result = mesh_topology.probe_mesh_over_console(worker)
        self.assertIsNone(result["mesh"])
        self.assertIn("mesh not enabled", result["detail"])

    def test_no_json_is_could_not_tell(self) -> None:
        worker = _Worker("sh: curl: not found\nAP6# ")
        result = mesh_topology.probe_mesh_over_console(worker)
        self.assertIsNone(result["mesh"])
        self.assertEqual(result["members"], [])
        self.assertTrue(result["detail"])

    def test_malformed_json_is_could_not_tell(self) -> None:
        worker = _Worker('{"data":{"mesh_info_list":[}}AP6# ')
        result = mesh_topology.probe_mesh_over_console(worker)
        self.assertIsNone(result["mesh"])

    def test_a_dead_console_raises_rather_than_reading_as_no_mesh(self) -> None:
        """A closed or busy console is the caller's problem to report. Folding it
        into `mesh: false` would put "no mesh" on a DUT nobody managed to ask."""
        worker = _Worker(RuntimeError("Serial capture is busy; try again"))
        with self.assertRaises(mesh_topology.MeshError) as caught:
            mesh_topology.probe_mesh_over_console(worker)
        self.assertIn("busy", str(caught.exception))


class _Context:
    def __init__(self, worker) -> None:
        self.serial_worker = worker


class ProbeEndpointTests(unittest.TestCase):
    """The endpoint's own job. The gate is asserted in test_route_protection."""

    def _call(self, worker):
        from app import main as main_mod

        registry = mock.Mock()
        with mock.patch.object(main_mod, "resolve_dut", return_value=_Context(worker)), \
             mock.patch.object(main_mod.app.state, "dut_registry", registry, create=True):
            return main_mod.probe_dut_mesh("default"), registry

    def test_it_answers_and_persists_what_the_dut_said(self) -> None:
        result, registry = self._call(_Worker(CONSOLE_OUTPUT))
        self.assertEqual(result["dut"], "default")
        self.assertIs(result["mesh"], True)
        self.assertTrue(result["captured_at"])
        stored = registry.store_mesh_probe.call_args[0][1]
        self.assertIs(stored["mesh"], True)
        self.assertTrue(stored["captured_at"])

    def test_could_not_tell_is_a_200_not_an_error(self) -> None:
        """`mesh: null` is an answer -- "we asked and could not tell" -- and the
        client renders it differently from both true and false. A 4xx here would
        make it indistinguishable from a console that never ran the command."""
        result, _ = self._call(_Worker("sh: curl: not found\nAP6# "))
        self.assertIsNone(result["mesh"])

    def test_an_unusable_console_is_a_400(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._call(_Worker(RuntimeError("Serial port is closed")))
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
