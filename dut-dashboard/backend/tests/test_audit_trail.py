"""The audit trail: role history, last-seen throttling, and the guarantee that
a session's identity overrides whatever the client claims.

The spoofing tests go through TestClient because a session is only resolved by
FastAPI's dependency machinery -- calling the endpoint functions directly (as
the older service tests do) means "no session" by design.
"""

from __future__ import annotations

import io
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import workspace
from app.main import app
from app.services import auth_service, file_service, invite_service


class AuditTestCase(unittest.TestCase):
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
        auth_service._last_seen_writes.clear()
        self.client = TestClient(app)

    def _login(self, role: str, username: str | None = None) -> dict:
        user = auth_service.create_or_update_user(username or f"user-{role}", role, role)
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))
        return user


class RoleHistoryTests(AuditTestCase):
    def test_first_registration_is_logged_with_no_previous_role(self) -> None:
        auth_service.create_or_update_user("amy", "Amy", "guest")
        [change] = auth_service.list_role_changes()
        self.assertIsNone(change["from_role"])
        self.assertEqual(change["to_role"], "guest")
        self.assertEqual(change["via"], "register")

    def test_an_upgrade_records_both_roles(self) -> None:
        auth_service.create_or_update_user("amy", "Amy", "guest")
        auth_service.create_or_update_user("amy", "Amy", "engineer")
        change = auth_service.list_role_changes()[0]
        self.assertEqual((change["from_role"], change["to_role"]), ("guest", "engineer"))

    def test_a_same_role_relogin_writes_nothing(self) -> None:
        """Otherwise routine re-logins would bury the real privilege changes."""
        auth_service.create_or_update_user("amy", "Amy", "engineer")
        auth_service.create_or_update_user("amy", "Amy Longer", "engineer")
        self.assertEqual(len(auth_service.list_role_changes()), 1)

    def test_updated_at_advances_even_without_a_role_change(self) -> None:
        auth_service.create_or_update_user("amy", "Amy", "engineer")
        self.assertIsNotNone(auth_service.list_users()[0]["updated_at"])

    def test_redeeming_an_invite_records_via_invite_and_the_id(self) -> None:
        invite = invite_service.create_invite("engineer", created_by="root")
        response = self.client.post(
            "/api/auth/redeem", json={"token": invite["token"], "username": "scanner"}
        )
        self.assertEqual(response.status_code, 200)
        change = auth_service.list_role_changes()[0]
        self.assertEqual(change["via"], "invite")
        self.assertEqual(change["invite_id"], invite["id"])
        self.assertEqual(change["to_role"], "engineer")

    def test_history_survives_a_later_demotion(self) -> None:
        """The whole point: users.role is overwritten, the log is not."""
        auth_service.create_or_update_user("amy", "Amy", "admin")
        auth_service.create_or_update_user("amy", "Amy", "guest")
        roles = [(c["from_role"], c["to_role"]) for c in auth_service.list_role_changes()]
        self.assertEqual(roles, [("admin", "guest"), (None, "admin")])
        self.assertEqual(auth_service.list_users()[0]["role"], "guest")


class LastSeenTests(AuditTestCase):
    def test_touch_is_throttled_per_user(self) -> None:
        user = auth_service.create_or_update_user("amy", "Amy", "guest")
        self.assertTrue(auth_service.touch_last_seen(user["id"]))
        self.assertFalse(auth_service.touch_last_seen(user["id"]))
        later = time.time() + auth_service.LAST_SEEN_THROTTLE_SECONDS + 1
        self.assertTrue(auth_service.touch_last_seen(user["id"], now=later))

    def test_a_request_records_last_seen(self) -> None:
        self._login("engineer")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        [row] = auth_service.list_users()
        self.assertIsNotNone(row["last_seen_at"])


class AuthorshipTests(AuditTestCase):
    def _upload(self, claimed: str | None) -> dict:
        data = {"uploader": claimed} if claimed is not None else {}
        response = self.client.post(
            "/api/files",
            files={"file": ("report.log", io.BytesIO(b"hello"), "text/plain")},
            data=data,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_session_overrides_a_spoofed_uploader(self) -> None:
        self._login("engineer", "nelson")
        auth_service.create_or_update_user("nelson", "Nelson C", "engineer")
        self._upload(claimed="somebody-else")
        [row] = self.client.get("/api/files").json()["files"]
        self.assertEqual(row["uploader"], "Nelson C")
        self.assertTrue(row["uploader_verified"])

    def test_a_session_overrides_a_spoofed_post_author(self) -> None:
        self._login("engineer", "nelson")
        auth_service.create_or_update_user("nelson", "Nelson C", "engineer")
        response = self.client.post(
            "/api/bulletin/posts",
            json={"title": "t", "body": "b", "author": "somebody-else"},
        )
        self.assertEqual(response.status_code, 200)
        [post] = self.client.get("/api/bulletin/posts").json()["posts"]
        self.assertEqual(post["author"], "Nelson C")
        self.assertTrue(post["author_verified"])

    def test_a_comment_is_attributed_to_the_session(self) -> None:
        self._login("engineer", "nelson")
        auth_service.create_or_update_user("nelson", "Nelson C", "engineer")
        post_id = self.client.post(
            "/api/bulletin/posts", json={"title": "t", "body": "b"}
        ).json()["id"]
        self.client.post(
            f"/api/bulletin/posts/{post_id}/comments",
            json={"body": "reply", "author": "impostor"},
        )
        [post] = self.client.get("/api/bulletin/posts").json()["posts"]
        [comment] = post["comments"]
        self.assertEqual(comment["author"], "Nelson C")
        self.assertTrue(comment["author_verified"])

    def test_rows_without_a_session_stay_unverified(self) -> None:
        """Pre-P71d rows and sessionless writes keep their free-text name and
        must NOT be presented as verified."""
        file_id = file_service.save_uploaded_file("legacy.log", io.BytesIO(b"x"), "amy")
        self.assertIsNotNone(file_id)
        self._login("admin")
        [row] = self.client.get("/api/files").json()["files"]
        self.assertEqual(row["uploader"], "amy")
        self.assertFalse(row["uploader_verified"])


class AuditEndpointTests(AuditTestCase):
    def test_endpoints_are_admin_only(self) -> None:
        for url in ("/api/auth/users", "/api/auth/role-changes"):
            with self.subTest(url=url, actor="anonymous"):
                self.client.cookies.clear()
                self.assertEqual(self.client.get(url).status_code, 401)
            with self.subTest(url=url, actor="engineer"):
                self._login("engineer")
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_admin_sees_users_and_history(self) -> None:
        auth_service.create_or_update_user("amy", "Amy", "guest")
        auth_service.create_or_update_user("amy", "Amy", "engineer")
        self._login("admin")
        users = self.client.get("/api/auth/users").json()["users"]
        self.assertIn("amy", [u["username"] for u in users])
        changes = self.client.get("/api/auth/role-changes").json()["changes"]
        self.assertIn(("guest", "engineer"), [(c["from_role"], c["to_role"]) for c in changes])

    def test_user_listing_exposes_no_secrets(self) -> None:
        self._login("admin")
        body = self.client.get("/api/auth/users").text
        for leaked in ("passcode", "token", "secret"):
            self.assertNotIn(leaked, body.lower())


if __name__ == "__main__":
    unittest.main()
