"""Folding the remote nodes that already exist into the collector model.

The claim being tested is not "a collector appears". It is that **nothing is
lost doing it**: the node keeps its own key, its console identity does not move,
every backhaul reading filed under that identity stays valid, and a node the
conversion cannot handle goes on working exactly as it did.

That last one is why the migration is additive rather than a rewrite. A boot
that half-converts a bench is worse than one that converts none of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.dut.registry as registry_mod
from app.api.fleet_api import RemoteNodeBody, configure_node, connect_node
from app.collector.migration import collector_id_for, migrate_remote_nodes
from app.collector.registry import MAX_COLLECTORS, CollectorRegistry
from app.dut.registry import DutRegistry, console_id

NODE = {
    "host": "10.0.0.24", "user": "pi", "key_path": "/home/you/.ssh/dut_fleet",
    "port": 22, "device": "/dev/ttyUSB0", "baudrate": 115200,
    "is_mesh": True, "backhaul_iface": "ath16",
}


class _Ws:
    def emit_from_thread(self, event: dict) -> None:
        pass


@contextlib.contextmanager
def _bench(root: Path):
    loop = asyncio.new_event_loop()
    with (
        mock.patch.object(registry_mod, "DUTS_FILE", root / "duts.json"),
        mock.patch.object(registry_mod, "snapshot_file_for", lambda d: root / f"{d}.jsonl"),
    ):
        try:
            yield DutRegistry(_Ws(), loop), CollectorRegistry(state_file=root / "collectors.json")
        finally:
            loop.close()


class MigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    @contextlib.contextmanager
    def bench(self, nodes: dict[str, dict]):
        with _bench(self.root) as (duts, collectors):
            for dut_id, remote in nodes.items():
                duts.register_dut(dut_id, label=dut_id)
                duts.configure_remote(dut_id, remote)
            yield duts, collectors


class WhatAMigratedNodeBecomesTest(MigrationTestCase):
    def test_a_node_gains_a_collector_and_keeps_everything_it_had(self) -> None:
        with self.bench({"node1": NODE}) as (duts, collectors):
            done = migrate_remote_nodes(duts, collectors)
            remote = duts.get("node1").remote
        self.assertEqual(done, [{"dut": "node1", "collector": "10-0-0-24", "ok": True}])
        self.assertEqual(remote["collector_id"], "10-0-0-24")
        # Additive: the key is still on the node, so the console opens on its own
        # terms even if the derived row is later removed.
        self.assertEqual(remote["key_path"], NODE["key_path"])
        self.assertEqual(remote["device"], NODE["device"])
        self.assertEqual(remote["backhaul_iface"], NODE["backhaul_iface"])

    def test_the_console_identity_does_not_move(self) -> None:
        """The reason this is safe to run on a bench with history.

        Every backhaul reading is filed under `console_id`, and a reading whose
        console identity changed underneath it is silently discarded the next
        time anything asks. `collector_id` is deliberately not one of
        CONSOLE_IDENTITY_FIELDS -- this is the test that says so out loud.
        """
        with self.bench({"node1": NODE}) as (duts, collectors):
            before = console_id(duts.get("node1").remote)
            migrate_remote_nodes(duts, collectors)
            after = console_id(duts.get("node1").remote)
        self.assertEqual(before, after)

    def test_the_collector_is_key_authenticated_and_needs_no_password(self) -> None:
        with self.bench({"node1": NODE}) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            status = collectors.status("10-0-0-24")
        self.assertEqual(status["auth"], "key")
        self.assertEqual(status["key_path"], NODE["key_path"])
        # Ready straight away, and after a restart: a key file outlives this
        # process where a password does not.
        self.assertTrue(status["ready"])
        self.assertFalse(status["has_password"])

    def test_no_expected_hostname_is_invented(self) -> None:
        """Nobody recorded one, and guessing would be worse than not knowing.

        A name derived from the address would either manufacture a mismatch at
        the next login or hide a real one.
        """
        with self.bench({"node1": NODE}) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            self.assertIsNone(collectors.get("10-0-0-24").hostname)

    def test_running_it_again_changes_nothing(self) -> None:
        with self.bench({"node1": NODE}) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            first = dict(duts.get("node1").remote)
            second_pass = migrate_remote_nodes(duts, collectors)
            self.assertEqual(second_pass, [])
            self.assertEqual(duts.get("node1").remote, first)
            self.assertEqual(collectors.ids(), ["10-0-0-24"])


class OneBoxManyConsolesTest(MigrationTestCase):
    def test_two_nodes_on_one_pi_share_a_collector(self) -> None:
        """The whole point of separating the box from the port.

        `docs/fleet-remote-nodes.md` calls two DUTs on one Pi the normal
        arrangement. Under the old model that was two unrelated rows repeating
        the same host, user and key.
        """
        with self.bench({
            "node1": NODE,
            "node2": {**NODE, "device": "/dev/ttyUSB1", "backhaul_iface": "ath15"},
        }) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            self.assertEqual(collectors.ids(), ["10-0-0-24"])
            self.assertEqual(duts.get("node1").remote["collector_id"], "10-0-0-24")
            self.assertEqual(duts.get("node2").remote["collector_id"], "10-0-0-24")

    def test_a_different_login_on_the_same_box_is_a_different_collector(self) -> None:
        # It is a different set of credentials, and one of them may stop working
        # without the other. Sharing a row would hide that.
        with self.bench({
            "node1": NODE,
            "node2": {**NODE, "user": "root", "device": "/dev/ttyUSB1"},
        }) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            self.assertEqual(len(collectors.ids()), 2)

    def test_two_pis_get_two_collectors(self) -> None:
        with self.bench({
            "node1": NODE,
            "node2": {**NODE, "host": "pi-node-2.local", "device": "/dev/ttyUSB0"},
        }) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
        self.assertEqual(sorted(collectors.ids()), ["10-0-0-24", "pi-node-2-local"])

    def test_an_id_is_derived_the_same_way_every_time(self) -> None:
        # Repeatable rather than random, which is what lets this run on every
        # boot instead of being a one-shot script somebody must be careful with.
        self.assertEqual(collector_id_for("10.0.0.24"), "10-0-0-24")
        self.assertEqual(collector_id_for("pi-node-1.local"), "pi-node-1-local")
        self.assertEqual(collector_id_for("PI-NODE-1"), "pi-node-1")


class WhenAConversionCannotHappenTest(MigrationTestCase):
    def test_a_node_that_cannot_be_converted_is_reported_and_left_working(self) -> None:
        """Reported, skipped, and untouched -- in that order.

        The node still has its host, user, key and device, so it connects
        exactly as it did before. A migration that stopped at the first problem,
        or that stripped a node it could not finish, would cost somebody a
        console for a bookkeeping row.
        """
        crowd = {
            f"filler{index}": {**NODE, "host": f"10.0.1.{index}"}
            for index in range(MAX_COLLECTORS)
        }
        with self.bench({**crowd, "node1": NODE}) as (duts, collectors):
            done = migrate_remote_nodes(duts, collectors)
            failed = [entry for entry in done if not entry["ok"]]
            self.assertEqual([entry["dut"] for entry in failed], ["node1"])
            self.assertIn("limit", failed[0]["detail"])
            remote = duts.get("node1").remote
        self.assertNotIn("collector_id", remote)
        self.assertEqual(remote["key_path"], NODE["key_path"])
        self.assertEqual(remote["host"], NODE["host"])

    def test_a_cabled_dut_is_not_a_node_and_is_left_alone(self) -> None:
        with self.bench({}) as (duts, collectors):
            self.assertEqual(migrate_remote_nodes(duts, collectors), [])
            self.assertEqual(collectors.ids(), [])


class RegisteringANodeTodayTest(MigrationTestCase):
    """The other half of the merge: new registrations land in the same model."""

    def request(self, duts, collectors):
        request = mock.Mock()
        request.app.state.dut_registry = duts
        request.app.state.collector_registry = collectors
        return request

    def body(self, **over):
        return RemoteNodeBody(
            id=over.pop("id", "node1"), host=NODE["host"], user=NODE["user"],
            key_path=NODE["key_path"], backhaul_iface=NODE["backhaul_iface"], **over
        )

    def test_registering_a_node_derives_its_collector_immediately(self) -> None:
        with self.bench({}) as (duts, collectors):
            configure_node(self.body(), self.request(duts, collectors), _admin={})
            self.assertEqual(collectors.ids(), ["10-0-0-24"])
            self.assertEqual(duts.get("node1").remote["collector_id"], "10-0-0-24")

    def test_a_second_node_on_the_same_pi_joins_the_first_collector(self) -> None:
        with self.bench({}) as (duts, collectors):
            request = self.request(duts, collectors)
            configure_node(self.body(), request, _admin={})
            configure_node(self.body(id="node2", device="/dev/ttyUSB1"), request, _admin={})
            self.assertEqual(collectors.ids(), ["10-0-0-24"])


class ConnectingAMigratedNodeTest(MigrationTestCase):
    """Where the credentials come from once a node is a console on a collector."""

    def request(self, duts, collectors):
        request = mock.Mock()
        request.app.state.dut_registry = duts
        request.app.state.collector_registry = collectors
        return request

    def test_the_collector_supplies_the_key(self) -> None:
        with self.bench({"node1": NODE}) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            with mock.patch("app.serial.serial_worker.SerialWorker.open") as opened:
                connect_node("node1", self.request(duts, collectors), _admin={})
        ssh = opened.call_args.kwargs["ssh"]
        self.assertEqual(ssh["key_path"], NODE["key_path"])
        self.assertEqual(ssh["password"], "")

    def test_a_node_whose_collector_is_gone_still_connects_on_its_own_key(self) -> None:
        """The fallback, and it is not dead code.

        A node registered before the merge, or one whose collector an admin
        removed, still carries the key it was registered with. Refusing to open
        its console because a derived row is missing would cost a bench a
        console over bookkeeping.
        """
        with self.bench({"node1": NODE}) as (duts, collectors):
            migrate_remote_nodes(duts, collectors)
            collectors.remove("10-0-0-24")
            with mock.patch("app.serial.serial_worker.SerialWorker.open") as opened:
                connect_node("node1", self.request(duts, collectors), _admin={})
        self.assertEqual(opened.call_args.kwargs["ssh"]["key_path"], NODE["key_path"])


if __name__ == "__main__":
    unittest.main()
