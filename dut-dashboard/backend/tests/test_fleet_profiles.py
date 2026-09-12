"""Saved fleet-host settings: who sees a row, who may change it, what is stored.

The interesting assertions here are the negative ones. A profile is a small
record, and the three things that would make it dangerous are all absences: a
password column, a shared row somebody else can rewrite, and a private row that
answers "forbidden" (which is itself the confirmation that it exists).
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import fleet_profiles_api as api
from app.api.fleet_profiles_api import ProfileBody
from app.db import workspace
from app.services import auth_service
from app.services import fleet_profile_service as profiles


def body(**overrides) -> ProfileBody:
    fields = {
        "name": "gavin",
        "device_name": "Sniffer Host 1",
        "host": "192.168.68.63",
        "port": 22,
        "username": "gavin",
        "scope": profiles.SCOPE_SHARED,
    }
    fields.update(overrides)
    return ProfileBody(**fields)


class FleetProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(
            patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db")
        )
        workspace.init_db()
        self.gavin = auth_service.create_or_update_user("gavin", "Gavin", "admin")
        self.amy = auth_service.create_or_update_user("amy", "Amy", "admin")

    def tearDown(self) -> None:
        self._stack.close()

    # -- what a row holds ------------------------------------------------

    def test_created_profile_reads_back_with_its_owner(self) -> None:
        created = api.create_profile(body(), user=self.gavin)["profile"]

        self.assertEqual(created["name"], "gavin")
        self.assertEqual(created["device_name"], "Sniffer Host 1")
        self.assertEqual(created["host"], "192.168.68.63")
        self.assertEqual(created["port"], 22)
        self.assertEqual(created["username"], "gavin")
        self.assertEqual(created["scope"], "shared")
        # The display name, through the join -- not a copy taken at write time.
        self.assertEqual(created["owner"], "Gavin")
        self.assertTrue(created["can_edit"])
        self.assertTrue(created["updated_at"])

    def test_no_route_and_no_row_carries_a_password(self) -> None:
        """The one guarantee the whole feature rests on.

        Both halves are asserted because either alone would pass while the
        other leaked: the request model has no password field to fill, and the
        table has no column to put one in.
        """
        self.assertNotIn("password", ProfileBody.model_fields)

        api.create_profile(body(), user=self.gavin)
        columns = {
            row["name"]
            for row in workspace.query_all("PRAGMA table_info(fleet_profiles)")
        }
        self.assertNotIn("password", columns)
        self.assertFalse({c for c in columns if "pass" in c.lower() or "secret" in c.lower()})

    def test_owner_renaming_themselves_renames_the_owner_column(self) -> None:
        api.create_profile(body(), user=self.gavin)
        auth_service.create_or_update_user("gavin", "Gavin C.", "admin")

        listed = api.list_profiles(user=self.gavin)["profiles"]
        self.assertEqual(listed[0]["owner"], "Gavin C.")

    # -- visibility ------------------------------------------------------

    def test_shared_is_everyones_and_private_is_nobody_elses(self) -> None:
        shared = api.create_profile(body(name="bench-pi"), user=self.gavin)["profile"]
        private = api.create_profile(
            body(name="gavins-desk", scope=profiles.SCOPE_PRIVATE), user=self.gavin
        )["profile"]

        mine = {p["name"] for p in api.list_profiles(user=self.gavin)["profiles"]}
        theirs = api.list_profiles(user=self.amy)["profiles"]

        self.assertEqual(mine, {"bench-pi", "gavins-desk"})
        self.assertEqual([p["name"] for p in theirs], ["bench-pi"])
        # Visible, but not hers to change -- that is what the pencil reads.
        self.assertFalse(theirs[0]["can_edit"])
        self.assertEqual(theirs[0]["owner"], "Gavin")
        self.assertEqual(theirs[0]["id"], shared["id"])
        self.assertNotEqual(private["id"], shared["id"])

    def test_someone_elses_private_profile_is_reported_missing_not_forbidden(self) -> None:
        private = api.create_profile(
            body(scope=profiles.SCOPE_PRIVATE), user=self.gavin
        )["profile"]

        with self.assertRaises(HTTPException) as caught:
            api.update_profile(private["id"], body(host="10.0.0.1"), user=self.amy)
        # 404, not 403: a 403 would confirm that Gavin has a profile by that id.
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_shared_profile_is_an_offer_not_a_wiki(self) -> None:
        shared = api.create_profile(body(), user=self.gavin)["profile"]

        for call in (
            lambda: api.update_profile(shared["id"], body(host="10.0.0.1"), user=self.amy),
            lambda: api.delete_profile(shared["id"], user=self.amy),
        ):
            with self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 403)

        # And it is still there, unchanged, for its owner.
        self.assertEqual(
            api.list_profiles(user=self.gavin)["profiles"][0]["host"], "192.168.68.63"
        )

    # -- editing ---------------------------------------------------------

    def test_update_replaces_every_field_for_the_owner(self) -> None:
        created = api.create_profile(body(), user=self.gavin)["profile"]

        updated = api.update_profile(
            created["id"],
            body(
                name="Gavin-Sniffer",
                device_name="192.168.50.10y94",
                host="192.168.50.10",
                username="gavin",
                scope=profiles.SCOPE_PRIVATE,
            ),
            user=self.gavin,
        )["profile"]

        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["name"], "Gavin-Sniffer")
        self.assertEqual(updated["host"], "192.168.50.10")
        self.assertEqual(updated["scope"], "private")
        self.assertEqual(api.list_profiles(user=self.amy)["profiles"], [])

    def test_delete_removes_only_that_row(self) -> None:
        first = api.create_profile(body(name="one"), user=self.gavin)["profile"]
        api.create_profile(body(name="two"), user=self.gavin)

        answer = api.delete_profile(first["id"], user=self.gavin)

        self.assertEqual(answer["name"], "one")
        self.assertEqual(
            [p["name"] for p in api.list_profiles(user=self.gavin)["profiles"]], ["two"]
        )

    def test_a_name_is_taken_per_owner_not_per_bench(self) -> None:
        api.create_profile(body(name="bench-pi"), user=self.gavin)

        with self.assertRaises(HTTPException) as caught:
            api.create_profile(body(name="bench-pi"), user=self.gavin)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("bench-pi", caught.exception.detail)

        # Amy is a different person with her own drawer.
        self.assertTrue(api.create_profile(body(name="bench-pi"), user=self.amy)["ok"])

    # -- validation ------------------------------------------------------

    def test_a_profile_must_be_applicable_to_a_collector(self) -> None:
        """Held to the collector registry's own expressions, not looser ones."""
        for field, value in (
            ("name", "   "),
            ("host", "-lab.example"),
            ("host", "192.168.1.1 ; reboot"),
            ("username", "-oProxyCommand=x"),
            ("scope", "public"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(HTTPException) as caught:
                    api.create_profile(body(**{field: value}), user=self.gavin)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIn(field, caught.exception.detail)

        self.assertEqual(api.list_profiles(user=self.gavin)["profiles"], [])

    def test_port_is_rejected_before_it_reaches_the_service(self) -> None:
        """The request model's range, so a bad port never becomes a row."""
        from pydantic import ValidationError

        for port in (0, 65536, -1):
            with self.subTest(port=port):
                with self.assertRaises(ValidationError):
                    body(port=port)


if __name__ == "__main__":
    unittest.main()
