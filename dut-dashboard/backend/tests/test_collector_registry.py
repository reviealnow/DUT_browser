"""What the collector registry accepts, and what it refuses to remember.

The load-bearing claim is the second one. A password typed into this dashboard
is a real login on a real box on this bench, and the decision was that it lives
in memory and nowhere else -- so "the state file never contains it" and "a
restart forgets it" are not implementation details, they are the feature. Both
are asserted here against the bytes actually written.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app import config
from app.api import collectors_api
from app.collector.registry import MAX_COLLECTORS, CollectorError, CollectorRegistry

PASSWORD = "bench-password-not-in-git"
VALID = {
    "id": "edge1",
    "label": "Edge collector (lab)",
    "ip": "10.0.0.9",
    "hostname": "edge-collector",
    "user": "dut",
    "port": 22,
}


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "collectors.json"
        self.registry = CollectorRegistry(state_file=self.state)


class WhatIsAcceptedTest(RegistryTestCase):
    def test_a_complete_collector_registers(self) -> None:
        collector = self.registry.configure(dict(VALID), password=PASSWORD)
        self.assertEqual(collector.id, "edge1")
        self.assertEqual(collector.ip, "10.0.0.9")
        self.assertTrue(self.registry.status("edge1")["has_password"])

    def test_a_name_is_an_acceptable_address(self) -> None:
        """Deliberately looser than this field started out.

        It began as "an IP address, so DNS is never between the operator and the
        box they picked", which is a real benefit and was given up on purpose: a
        remote node's host has always been allowed to be a name, the fleet guide
        writes it as `<pi-host>`, and a Pi's default name on a bench is
        `raspberrypi.local`. A merged model that cannot express what the old one
        did would strand exactly those configurations.

        `ip` and `hostname` stay different fields because they have different
        jobs, not different shapes: one is dialled, the other is checked against
        what the box answers.
        """
        collector = self.registry.configure({**VALID, "ip": "raspberrypi.local"})
        self.assertEqual(collector.ip, "raspberrypi.local")

    def test_an_address_cannot_start_a_command_line_option(self) -> None:
        # The part that was never about DNS: a value beginning with "-" reaches
        # ssh as an option rather than a name.
        for bad in ("-oProxyCommand=id", "", "  "):
            with self.subTest(ip=bad), self.assertRaises(CollectorError):
                self.registry.configure({**VALID, "ip": bad})

    def test_a_login_name_cannot_start_a_command_line_option(self) -> None:
        # `-oProxyCommand=...` as a user name reaches ssh as an option. The DUT
        # registry refuses the same shape next door for the same reason.
        with self.assertRaises(CollectorError):
            self.registry.configure({**VALID, "user": "-oProxyCommand=touch /tmp/x"})

    def test_the_port_must_be_a_port(self) -> None:
        for bad in (0, 65536, True, "22"):
            with self.subTest(port=bad), self.assertRaises(CollectorError):
                self.registry.configure({**VALID, "port": bad})

    def test_the_label_falls_back_to_the_hostname(self) -> None:
        collector = self.registry.configure({**VALID, "label": "  "})
        self.assertEqual(collector.label, "edge-collector")

    def test_the_table_is_bounded(self) -> None:
        for index in range(MAX_COLLECTORS):
            self.registry.configure({**VALID, "id": f"edge{index}", "ip": f"10.0.0.{index + 1}"})
        with self.assertRaises(CollectorError):
            self.registry.configure({**VALID, "id": "one-too-many"})

    def test_re_configuring_an_existing_id_stays_allowed_at_the_limit(self) -> None:
        for index in range(MAX_COLLECTORS):
            self.registry.configure({**VALID, "id": f"edge{index}", "ip": f"10.0.0.{index + 1}"})
        # An edit is not a new row, and refusing it would strand a full table
        # with a typo in it.
        self.registry.configure({**VALID, "id": "edge0", "ip": "10.0.0.99"})
        self.assertEqual(self.registry.get("edge0").ip, "10.0.0.99")


class ThePasswordIsMemoryOnlyTest(RegistryTestCase):
    def test_the_state_file_never_contains_the_password(self) -> None:
        self.registry.configure(dict(VALID), password=PASSWORD)
        written = self.state.read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, written)
        entry = json.loads(written)[0]
        # Not just absent as a value -- absent as a field, so a later reader
        # cannot start populating it. Checked as a key rather than a substring:
        # `auth` legitimately holds the string "password", which is the mode and
        # not a credential.
        self.assertNotIn("password", entry)
        self.assertEqual(entry["auth"], "password")
        self.assertEqual(entry["ip"], "10.0.0.9")

    def test_a_restart_keeps_the_collector_and_forgets_the_login(self) -> None:
        """The rule, from the operator's side.

        Losing the registration on every restart would make the feature
        useless; keeping the password would make it a stored credential. This is
        the split, and `has_password` is what lets the UI ask for it again
        rather than offering a Connect that can only fail.
        """
        self.registry.configure(dict(VALID), password=PASSWORD)
        restarted = CollectorRegistry(state_file=self.state)
        restarted.load_persisted()
        status = restarted.status("edge1")
        self.assertEqual(status["hostname"], "edge-collector")
        self.assertFalse(status["has_password"])
        self.assertFalse(status["connected"])

    def test_an_empty_password_is_not_a_password(self) -> None:
        # Otherwise `has_password` promises a login that ssh will reject.
        self.registry.configure(dict(VALID), password="")
        self.assertFalse(self.registry.status("edge1")["has_password"])

    def test_re_pointing_a_collector_drops_the_credentials_it_held(self) -> None:
        """A new address is a different machine, and the password was not for it."""
        self.registry.configure(dict(VALID), password=PASSWORD)
        self.registry.configure({**VALID, "ip": "10.0.0.77"})
        self.assertFalse(self.registry.status("edge1")["has_password"])

    def test_editing_only_the_label_keeps_the_login(self) -> None:
        # The counterpart: a rename is not a re-point, and making somebody retype
        # a bench password to fix a typo in a label teaches them to write it down.
        self.registry.configure(dict(VALID), password=PASSWORD)
        self.registry.configure({**VALID, "label": "Edge collector (rack 2)"})
        self.assertTrue(self.registry.status("edge1")["has_password"])

    def test_removing_a_collector_forgets_everything_about_it(self) -> None:
        self.registry.configure(dict(VALID), password=PASSWORD)
        self.registry.remove("edge1")
        self.assertEqual(self.registry.ids(), [])
        self.assertNotIn("edge1", json.dumps(json.loads(self.state.read_text())))


class AKeyAuthenticatedCollectorTest(RegistryTestCase):
    """What an existing remote node becomes when it is folded in here."""

    KEYED = {**VALID, "id": "node1", "auth": "key", "key_path": "/home/you/.ssh/dut_fleet"}

    def test_it_needs_no_password_and_is_ready_immediately(self) -> None:
        # The point of keeping both: a key file outlives this process, so a
        # migrated node is connectable straight after a restart, exactly as it
        # was before the merge.
        self.registry.configure(dict(self.KEYED))
        status = self.registry.status("node1")
        self.assertTrue(status["ready"])
        self.assertFalse(status["has_password"])
        self.assertEqual(status["key_path"], "/home/you/.ssh/dut_fleet")

    def test_key_authentication_requires_a_key(self) -> None:
        with self.assertRaises(CollectorError):
            self.registry.configure({**VALID, "id": "node1", "auth": "key"})

    def test_a_password_collector_does_not_keep_a_key_path(self) -> None:
        # Not merely ignored: a stored key path on a password collector would
        # make `ssh -i` reachable from a row whose UI never mentions a key.
        collector = self.registry.configure({**VALID, "key_path": "/tmp/k", "auth": "password"})
        self.assertIsNone(collector.key_path)

    def test_the_transport_is_told_which_one_to_use(self) -> None:
        self.registry.configure(dict(self.KEYED))
        self.registry.configure(dict(VALID), password=PASSWORD)
        self.assertEqual(
            self.registry.credentials_for("node1"),
            {"key_path": "/home/you/.ssh/dut_fleet", "password": ""},
        )
        self.assertEqual(
            self.registry.credentials_for("edge1"), {"key_path": "", "password": PASSWORD}
        )

    def test_a_key_survives_a_restart_where_a_password_does_not(self) -> None:
        self.registry.configure(dict(self.KEYED))
        self.registry.configure(dict(VALID), password=PASSWORD)
        restarted = CollectorRegistry(state_file=self.state)
        restarted.load_persisted()
        self.assertTrue(restarted.status("node1")["ready"])
        self.assertFalse(restarted.status("edge1")["ready"])


class AnOptionalExpectedHostnameTest(RegistryTestCase):
    def test_a_collector_may_have_no_expected_name(self) -> None:
        """Absent is a state, not a gap to fill in.

        A collector derived from an existing remote node carries nobody's
        expectation of what the box calls itself. Inventing one from the address
        would either manufacture a mismatch or hide a real one.
        """
        collector = self.registry.configure({**VALID, "hostname": ""})
        self.assertIsNone(collector.hostname)
        # And the label falls back to the address rather than to nothing.
        self.assertEqual(collector.label, "Edge collector (lab)")

    def test_a_nameless_collector_labels_itself_by_address(self) -> None:
        collector = self.registry.configure({**VALID, "hostname": "", "label": ""})
        self.assertEqual(collector.label, "10.0.0.9")


class WhereStateIsWrittenTest(unittest.TestCase):
    def test_the_configured_path_is_read_when_it_is_needed(self) -> None:
        """Regression, and it was found by writing into the real bench file.

        `state_file=COLLECTORS_FILE` as a default argument binds the path at
        import time, so patching the module attribute redirects nothing and the
        registry goes on writing wherever it was pointed when it was first
        imported. A boot-path check written that way put a synthetic collector
        into this machine's `logs/collectors.json`. The DUT registry looks its
        own path up at call time; so does this one now.
        """
        with tempfile.TemporaryDirectory() as directory:
            redirected = Path(directory) / "collectors.json"
            with mock.patch.object(config, "COLLECTORS_FILE", redirected):
                registry = CollectorRegistry()
                registry.configure(dict(VALID), password=PASSWORD)
            self.assertTrue(redirected.exists(), "wrote somewhere other than the patched path")
            self.assertIn("edge1", redirected.read_text(encoding="utf-8"))


class ConnectingWithoutAPasswordTest(RegistryTestCase):
    def test_connect_refuses_before_it_opens_anything(self) -> None:
        self.registry.configure(dict(VALID))
        with mock.patch("app.collector.ssh_session.open_session") as opened:
            with self.assertRaises(CollectorError) as caught:
                self.registry.connect("edge1")
        opened.assert_not_called()
        self.assertIn("memory", str(caught.exception))


class NothingLeaksThroughTheApiTest(RegistryTestCase):
    """The HTTP surface, checked for the one thing it must never return."""

    def request(self):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            collector_registry=self.registry
        )))

    def test_no_response_body_carries_the_password(self) -> None:
        body = collectors_api.CollectorBody(**VALID, password=PASSWORD)
        created = collectors_api.configure_collector(body, self.request())
        listed = collectors_api.list_collectors(self.request())
        for payload in (created, listed):
            self.assertNotIn(PASSWORD, json.dumps(payload))
        self.assertTrue(created["has_password"])

    def test_a_key_collector_survives_the_request_body(self) -> None:
        """Regression, and the kind only running the page could find.

        `auth` and `key_path` have to be declared on the pydantic body or they
        are dropped before the registry sees them. They were not, so the form
        offered a key, sent one, and got back a row asking for a password --
        while every test passed, because they all called the registry directly.
        """
        body = collectors_api.CollectorBody(
            **{**VALID, "hostname": None},
            auth="key",
            key_path="/home/you/.ssh/dut_fleet",
        )
        created = collectors_api.configure_collector(body, self.request())
        self.assertEqual(created["auth"], "key")
        self.assertEqual(created["key_path"], "/home/you/.ssh/dut_fleet")
        self.assertTrue(created["ready"])

    def test_a_password_sent_alongside_a_key_is_not_held(self) -> None:
        # Nothing would ever use it, and `has_password` would claim a
        # credential this collector does not authenticate with.
        body = collectors_api.CollectorBody(
            **VALID, auth="key", key_path="/home/you/.ssh/dut_fleet", password=PASSWORD
        )
        created = collectors_api.configure_collector(body, self.request())
        self.assertFalse(created["has_password"])

    def test_a_collector_may_be_registered_with_no_expected_hostname(self) -> None:
        body = collectors_api.CollectorBody(**{**VALID, "hostname": None}, password=PASSWORD)
        created = collectors_api.configure_collector(body, self.request())
        self.assertIsNone(created["hostname"])

    def test_an_unknown_collector_is_a_404_rather_than_a_crash(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            collectors_api.connect_collector("nope", self.request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_collector_with_no_password_held_is_a_400_not_a_502(self) -> None:
        """This side is not ready; nothing was sent to the box.

        The distinction is the operator's next step: 400 means finish setting
        this up here, 502 means go and look at the collector.
        """
        collectors_api.configure_collector(
            collectors_api.CollectorBody(**VALID), self.request()
        )
        with self.assertRaises(HTTPException) as caught:
            collectors_api.connect_collector("edge1", self.request())
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
