"""The mesh table as the DUT reports it: parsing, and the endpoint around it.

The payload below is the real one, captured from AP6_420E's own console on
2026-08-24:

    curl -k -X GET 'https://127.0.0.1:10443/ap/info/wireless/mesh' \
         -H 'accept: application/octet-stream'

It is the reason this feature exists -- two members were listed there while the
Fleet view showed only the one the dashboard had a console for -- so it is the
fixture rather than an invented shape. Note `signal: 0` on the root: that is the
single most misleading value in the payload, and it has its own test.

Two more payloads follow it, captured 2026-08-28 from **both ends of one mesh**
minutes apart. One payload cannot show what those two do: `node` and `hop` are
relative to whichever DUT was asked, and `node_number` tracks neither. A single
capture reads as an absolute topology, which is how the comment in `_member`
came to say the two labels agree.
"""

from __future__ import annotations

import unittest
from unittest import mock

import httpx
from fastapi import HTTPException

from app.api.fleet_api import read_mesh
from app.services import firmware_service, mesh_topology


# Verbatim, field order included, from the console session that prompted this.
REAL_PAYLOAD = {
    "data": {
        "mesh_info_list": [
            {
                "mac_address": "C8:4F:86:91:47:E1",
                "node": "0",
                "hop": 0,
                "mesh_type": "Root",
                "ip_address": "192.168.30.121",
                "signal": 0,
                "node_number": 0,
            },
            {
                "mac_address": "C8:4F:86:89:F1:68",
                "node": "1",
                "hop": 1,
                "mesh_type": "Node",
                "ip_address": "192.168.30.176",
                "signal": -26,
                "node_number": 1,
            },
        ],
        "total_size": 2,
    },
    "error_code": 0,
    "error_msg": "",
}


# The same two-device mesh, asked from each end on 2026-08-28. Root is the
# AP6420E cabled to the desk (192.168.30.121); node is the AP6420 on the Pi
# (192.168.30.176). Verbatim, field order included.
ASKED_THE_ROOT = {
    "data": {
        "mesh_info_list": [
            {
                "mac_address": "C8:4F:86:91:47:E1",
                "node": "0",
                "hop": 0,
                "mesh_type": "Root",
                "ip_address": "192.168.30.121",
                "signal": 0,
                "node_number": 0,
            },
            {
                "mac_address": "C8:4F:86:89:F1:68",
                "node": "1",
                "hop": 1,
                "mesh_type": "Node",
                "ip_address": "192.168.30.176",
                "signal": -32,
                "node_number": 1,
            },
        ],
        "total_size": 2,
    },
    "error_code": 0,
    "error_msg": "",
}

ASKED_THE_NODE = {
    "data": {
        "mesh_info_list": [
            {
                "mac_address": "C8:4F:86:91:47:E1",
                "node": "1",
                "hop": 1,
                "mesh_type": "Root",
                "ip_address": "192.168.30.121",
                "signal": 0,
                "node_number": 1,
            },
            {
                "mac_address": "C8:4F:86:89:F1:68",
                "node": "0",
                "hop": 0,
                "mesh_type": "Node",
                "ip_address": "192.168.30.176",
                "signal": -31,
                "node_number": 1,
            },
        ],
        "total_size": 2,
    },
    "error_code": 0,
    "error_msg": "",
}


def _by_ip(payload: dict) -> dict:
    return {m["ip"]: m for m in mesh_topology.parse_mesh_payload(payload)}


class RelativeToWhoeverWasAskedTests(unittest.TestCase):
    """`node` and `hop` describe the answering device's view, not the mesh.

    Both payloads are the same two devices, the same link, minutes apart. Only
    the DUT asked differs, and the labels swap -- so a hop count here cannot be
    read as distance from the root, and a caller showing one has to say which
    DUT it asked.
    """

    ROOT_IP = "192.168.30.121"
    NODE_IP = "192.168.30.176"

    def test_the_device_asked_always_calls_itself_zero(self) -> None:
        asked_root = _by_ip(ASKED_THE_ROOT)
        asked_node = _by_ip(ASKED_THE_NODE)
        self.assertEqual(asked_root[self.ROOT_IP]["node"], "0")
        self.assertEqual(asked_root[self.ROOT_IP]["hop"], 0)
        self.assertEqual(asked_node[self.NODE_IP]["node"], "0")
        self.assertEqual(asked_node[self.NODE_IP]["hop"], 0)

    def test_the_same_device_gets_a_different_hop_from_the_other_end(self) -> None:
        # The root is hop 0 to itself and hop 1 to its node. Re-basing either
        # onto the other would be inventing a topology from one account of it.
        self.assertEqual(_by_ip(ASKED_THE_ROOT)[self.ROOT_IP]["hop"], 0)
        self.assertEqual(_by_ip(ASKED_THE_NODE)[self.ROOT_IP]["hop"], 1)

    def test_the_role_does_not_move_with_the_viewpoint(self) -> None:
        """`mesh_type` is a fact about the device; `node` and `hop` are not.
        Nothing here should conflate them."""
        for payload in (ASKED_THE_ROOT, ASKED_THE_NODE):
            with self.subTest(payload=payload["data"]["mesh_info_list"][0]["node"]):
                members = _by_ip(payload)
                self.assertEqual(members[self.ROOT_IP]["role"], "root")
                self.assertEqual(members[self.NODE_IP]["role"], "node")

    def test_node_number_tracks_neither_the_label_nor_the_device(self) -> None:
        """Why both are published rather than one derived from the other.

        Asked the node, BOTH members come back with `node_number: 1` while
        `node` is "1" and "0" -- so it is not the label, and not an identity
        for the device either, since the root is 0 in the other payload.
        """
        asked_node = _by_ip(ASKED_THE_NODE)
        self.assertEqual(
            [asked_node[self.ROOT_IP]["node_number"], asked_node[self.NODE_IP]["node_number"]],
            [1, 1],
        )
        self.assertEqual(asked_node[self.NODE_IP]["node"], "0")
        self.assertEqual(_by_ip(ASKED_THE_ROOT)[self.ROOT_IP]["node_number"], 0)

    def test_both_roots_still_report_no_reading_rather_than_zero_dbm(self) -> None:
        for payload in (ASKED_THE_ROOT, ASKED_THE_NODE):
            with self.subTest(payload=payload["data"]["mesh_info_list"][0]["node"]):
                self.assertIsNone(_by_ip(payload)[self.ROOT_IP]["rssi"])
                self.assertIsNone(_by_ip(payload)[self.ROOT_IP]["rssi_band"])


class ParseMeshPayloadTests(unittest.TestCase):
    def test_it_reports_every_member_the_device_listed(self) -> None:
        members = mesh_topology.parse_mesh_payload(REAL_PAYLOAD)
        self.assertEqual(len(members), 2)
        self.assertEqual(
            [m["ip"] for m in members], ["192.168.30.121", "192.168.30.176"]
        )
        self.assertEqual([m["role"] for m in members], ["root", "node"])
        self.assertEqual([m["hop"] for m in members], [0, 1])

    def test_the_node_keeps_its_measured_signal(self) -> None:
        node = mesh_topology.parse_mesh_payload(REAL_PAYLOAD)[1]
        self.assertEqual(node["rssi"], -26)
        # From the same helper the backhaul card uses, so one link cannot be
        # "near" in one view and something else in the other.
        self.assertEqual(node["rssi_band"], "near")
        self.assertEqual(node["mac"], "C8:4F:86:89:F1:68")
        self.assertEqual(node["node"], "1")
        self.assertEqual(node["node_number"], 1)

    def test_the_roots_zero_signal_is_no_reading_and_not_zero_dbm(self) -> None:
        """The defect this guards: a root has no parent to hear, and the device
        fills the field with 0. Passed through, the UI shows `0 dBm` -- which
        reads as the strongest link on the bench instead of no link at all."""
        root = mesh_topology.parse_mesh_payload(REAL_PAYLOAD)[0]
        self.assertIsNone(root["rssi"])
        self.assertIsNone(root["rssi_band"])

    def test_an_empty_mesh_is_an_answer(self) -> None:
        members = mesh_topology.parse_mesh_payload(
            {"data": {"mesh_info_list": [], "total_size": 0}, "error_code": 0}
        )
        self.assertEqual(members, [])

    def test_a_device_error_is_raised_with_the_devices_own_words(self) -> None:
        with self.assertRaises(mesh_topology.MeshError) as caught:
            mesh_topology.parse_mesh_payload(
                {"data": None, "error_code": 7, "error_msg": "mesh not enabled"}
            )
        self.assertIn("mesh not enabled", str(caught.exception))

    def test_a_reply_without_the_list_is_not_an_empty_mesh(self) -> None:
        """Reporting this as "no members" would be a confident wrong answer:
        nothing in it says the device is standalone."""
        for payload in ({"error_code": 0}, {"data": {}, "error_code": 0}, [], "nope"):
            with self.subTest(payload=payload):
                with self.assertRaises(mesh_topology.MeshError):
                    mesh_topology.parse_mesh_payload(payload)

    def test_missing_and_junk_fields_become_none_rather_than_raising(self) -> None:
        members = mesh_topology.parse_mesh_payload(
            {"data": {"mesh_info_list": [{}, {"hop": "x", "signal": "y"}]}, "error_code": 0}
        )
        self.assertEqual(len(members), 2)
        for member in members:
            self.assertIsNone(member["hop"])
            self.assertIsNone(member["rssi"])
            self.assertIsNone(member["mac"])
            self.assertIsNone(member["role"])

    def test_a_hop_of_true_is_not_a_hop_of_one(self) -> None:
        member = mesh_topology.parse_mesh_payload(
            {"data": {"mesh_info_list": [{"hop": True}]}, "error_code": 0}
        )[0]
        self.assertIsNone(member["hop"])

    def test_an_unrecognised_mesh_type_is_kept_but_not_guessed_at(self) -> None:
        member = mesh_topology.parse_mesh_payload(
            {"data": {"mesh_info_list": [{"mesh_type": "Repeater"}]}, "error_code": 0}
        )[0]
        self.assertIsNone(member["role"])
        self.assertEqual(member["mesh_type"], "Repeater")

    def test_role_matches_the_vocabulary_the_rest_of_the_fleet_uses(self) -> None:
        """`backhaul.role` is lowercase; a card comparing against "Root" would
        silently never match."""
        member = mesh_topology.parse_mesh_payload(
            {"data": {"mesh_info_list": [{"mesh_type": "ROOT"}]}, "error_code": 0}
        )[0]
        self.assertEqual(member["role"], "root")


def _client_factory(handler):
    """An httpx client whose transport is `handler`; no socket is opened."""
    transport = httpx.MockTransport(handler)
    return lambda: httpx.Client(transport=transport)


class FetchMeshTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            firmware_service, "get_credentials", return_value=("admin", "secret")
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        has = mock.patch.object(firmware_service, "has_credentials", return_value=True)
        self.addCleanup(has.stop)
        has.start()

    def test_it_asks_the_management_port_and_returns_the_members(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=REAL_PAYLOAD)

        result = mesh_topology.fetch_mesh("192.168.30.121", _client_factory(handler))

        # The /ap/* family answers on 10443, not https's default 443 -- an
        # address stored without a port must not be sent to the web UI.
        self.assertEqual(
            str(seen[0].url), "https://192.168.30.121:10443/ap/info/wireless/mesh"
        )
        self.assertEqual(result["mgmt_url"], "https://192.168.30.121:10443")
        self.assertEqual(len(result["members"]), 2)

    def test_an_explicit_port_is_honoured(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=REAL_PAYLOAD)

        mesh_topology.fetch_mesh("https://10.0.0.5:8443", _client_factory(handler))
        self.assertEqual(seen[0].url.port, 8443)

    def test_no_management_address_refuses_before_touching_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("the DUT must not be contacted")

        with self.assertRaises(mesh_topology.MeshNotConfigured):
            mesh_topology.fetch_mesh("", _client_factory(handler))

    def test_missing_credentials_refuse_before_touching_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("the DUT must not be contacted")

        with mock.patch.object(firmware_service, "has_credentials", return_value=False):
            with self.assertRaises(mesh_topology.MeshNotConfigured):
                mesh_topology.fetch_mesh("192.168.30.121", _client_factory(handler))

    def test_a_rejected_credential_is_its_own_error(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                with self.assertRaises(mesh_topology.MeshAuthError):
                    mesh_topology.fetch_mesh(
                        "192.168.30.121",
                        _client_factory(lambda request: httpx.Response(status)),
                    )

    def test_an_unreachable_device_is_reported_not_swallowed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with self.assertRaises(mesh_topology.MeshError) as caught:
            mesh_topology.fetch_mesh("192.168.30.121", _client_factory(handler))
        self.assertIn("192.168.30.121", str(caught.exception))

    def test_a_non_json_body_is_reported(self) -> None:
        with self.assertRaises(mesh_topology.MeshError):
            mesh_topology.fetch_mesh(
                "192.168.30.121",
                _client_factory(lambda request: httpx.Response(200, text="<html>")),
            )


class _StubContext:
    def __init__(self, mgmt_url: str = "https://192.168.30.121:10443") -> None:
        self.mgmt_url = mgmt_url


class _StubRegistry:
    def __init__(self, context: _StubContext | None) -> None:
        self._context = context

    def get(self, dut_id: str):
        if self._context is None:
            raise KeyError(dut_id)
        return self._context


class _StubRequest:
    def __init__(self, registry: _StubRegistry) -> None:
        self.app = mock.Mock()
        self.app.state.dut_registry = registry


class ReadMeshEndpointTests(unittest.TestCase):
    """The endpoint's own job: turn a service error into the right status.

    The gate itself is asserted in test_route_protection.py -- calling the
    function directly here bypasses FastAPI's dependencies entirely, which is
    exactly why that suite exists.
    """

    def _call(self, *, mgmt_url: str = "https://192.168.30.121:10443", side_effect=None,
              return_value=None):
        request = _StubRequest(_StubRegistry(_StubContext(mgmt_url)))
        with mock.patch.object(
            mesh_topology, "fetch_mesh", side_effect=side_effect, return_value=return_value
        ):
            return read_mesh("default", request)

    def test_it_answers_with_the_members_and_the_address_it_used(self) -> None:
        result = self._call(
            return_value={
                "mgmt_url": "https://192.168.30.121:10443",
                "members": mesh_topology.parse_mesh_payload(REAL_PAYLOAD),
            }
        )
        self.assertEqual(result["dut"], "default")
        self.assertEqual(result["mgmt_url"], "https://192.168.30.121:10443")
        self.assertEqual(len(result["members"]), 2)
        self.assertTrue(result["captured_at"])

    def test_an_unset_management_address_is_a_400_not_a_502(self) -> None:
        """The operator's next step is a settings page, not the bench, and the
        status code is what tells the two apart."""
        with self.assertRaises(HTTPException) as caught:
            self._call(side_effect=mesh_topology.MeshNotConfigured("not set up"))
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_device_that_will_not_answer_is_a_502(self) -> None:
        for error in (
            mesh_topology.MeshError("unreachable"),
            mesh_topology.MeshAuthError("rejected"),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(HTTPException) as caught:
                    self._call(side_effect=error)
                self.assertEqual(caught.exception.status_code, 502)

    def test_an_unknown_dut_is_a_404(self) -> None:
        request = _StubRequest(_StubRegistry(None))
        with self.assertRaises(HTTPException) as caught:
            read_mesh("nope", request)
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
