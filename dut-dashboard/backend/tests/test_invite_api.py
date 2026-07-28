"""Invite endpoints over HTTP: admin gating, the redeem flow, and what the
responses are allowed to reveal.

TestClient (not direct calls) because the role gates and the session cookie are
the behaviour under test; built without a context manager so FastAPI's startup
hook never runs.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import workspace
from app.main import app
from app.services import auth_service, invite_service


class InviteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        self._stack.enter_context(
            patch.object(auth_service, "SESSION_SECRET_FILE", self._dir / "session_secret")
        )
        workspace.init_db()
        self.client = TestClient(app)

    def _login(self, role: str) -> None:
        user = auth_service.create_or_update_user(f"user-{role}", role, role)
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))

    def _logout(self) -> None:
        self.client.cookies.clear()

    def _mint(self, role: str = "engineer", **kwargs) -> dict:
        self._login("admin")
        response = self.client.post("/api/auth/invites", json={"role": role, **kwargs})
        self.assertEqual(response.status_code, 200)
        self._logout()
        return response.json()

    # -- gating -------------------------------------------------------------

    def test_invite_management_is_admin_only(self) -> None:
        for method, url in (
            ("post", "/api/auth/invites"),
            ("get", "/api/auth/invites"),
            ("delete", "/api/auth/invites/1"),
        ):
            with self.subTest(url=url, actor="anonymous"):
                self._logout()
                call = getattr(self.client, method)
                response = call(url, json={"role": "guest"}) if method == "post" else call(url)
                self.assertEqual(response.status_code, 401)
            with self.subTest(url=url, actor="engineer"):
                self._login("engineer")
                call = getattr(self.client, method)
                response = call(url, json={"role": "guest"}) if method == "post" else call(url)
                self.assertEqual(response.status_code, 403)

    def test_redeem_is_open(self) -> None:
        invite = self._mint("engineer")
        response = self.client.post(
            "/api/auth/redeem", json={"token": invite["token"], "username": "amy"}
        )
        self.assertEqual(response.status_code, 200)

    # -- create -------------------------------------------------------------

    def test_create_returns_the_token_and_a_qr_once(self) -> None:
        invite = self._mint("engineer", label="lab bench")
        self.assertTrue(invite["token"])
        self.assertIn("<svg", invite["qr_svg"])
        self.assertEqual(invite["url_path"], f"/?invite={invite['token']}")

    def test_created_by_records_the_issuing_admin(self) -> None:
        self._mint("guest")
        self._login("admin")
        [row] = self.client.get("/api/auth/invites").json()["invites"]
        self.assertEqual(row["created_by"], "user-admin")

    def test_create_rejects_a_bad_role_or_max_uses(self) -> None:
        self._login("admin")
        self.assertEqual(
            self.client.post("/api/auth/invites", json={"role": "root"}).status_code, 400
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/invites", json={"role": "guest", "max_uses": 0}
            ).status_code,
            400,
        )

    def test_listing_hides_tokens_and_hashes(self) -> None:
        invite = self._mint("engineer")
        self._login("admin")
        body = self.client.get("/api/auth/invites").text
        self.assertNotIn(invite["token"], body)
        self.assertNotIn(invite_service.hash_token(invite["token"]), body)

    # -- redeem -------------------------------------------------------------

    def test_redeem_grants_a_session_at_the_invite_role(self) -> None:
        invite = self._mint("engineer")
        response = self.client.post(
            "/api/auth/redeem",
            json={"token": invite["token"], "username": "amy", "display_name": "Amy L"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"username": "amy", "display_name": "Amy L", "role": "engineer"}
        )
        self.assertEqual(self.client.get("/api/auth/me").json()["role"], "engineer")
        # The session is a real one: it opens the engineer surface.
        self.assertEqual(self.client.get("/api/files").status_code, 200)

    def test_redeemed_session_cookie_matches_the_registered_one(self) -> None:
        invite = self._mint("engineer")
        response = self.client.post(
            "/api/auth/redeem", json={"token": invite["token"], "username": "amy"}
        )
        header = response.headers["set-cookie"].lower()
        self.assertIn("httponly", header)
        self.assertIn("samesite=lax", header)

    def test_every_rejection_looks_identical(self) -> None:
        """Unknown, expired, revoked and exhausted must not be distinguishable."""
        expired = self._mint("guest", expires_in_hours=-1)
        revoked = self._mint("guest")
        self._login("admin")
        self.client.delete(f"/api/auth/invites/{revoked['id']}")
        self._logout()
        exhausted = self._mint("guest")
        self.client.post(
            "/api/auth/redeem", json={"token": exhausted["token"], "username": "first"}
        )
        self._logout()

        cases = {
            "unknown": "no-such-token",
            "expired": expired["token"],
            "revoked": revoked["token"],
            "exhausted": exhausted["token"],
        }
        seen = set()
        for name, token in cases.items():
            with self.subTest(case=name):
                response = self.client.post(
                    "/api/auth/redeem", json={"token": token, "username": "amy"}
                )
                self.assertEqual(response.status_code, 403)
                seen.add(response.json()["detail"])
        self.assertEqual(len(seen), 1, f"rejection reasons leaked: {seen}")

    def test_redeem_validates_the_username(self) -> None:
        invite = self._mint("guest")
        response = self.client.post(
            "/api/auth/redeem", json={"token": invite["token"], "username": "   "}
        )
        self.assertEqual(response.status_code, 400)

    def test_a_rejected_redeem_leaves_no_session(self) -> None:
        response = self.client.post(
            "/api/auth/redeem", json={"token": "bogus", "username": "amy"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_redeeming_replaces_an_existing_session(self) -> None:
        invite = self._mint("engineer")
        self._login("guest")
        self.client.post(
            "/api/auth/redeem", json={"token": invite["token"], "username": "user-guest"}
        )
        self.assertEqual(self.client.get("/api/auth/me").json()["role"], "engineer")

    def test_revoke_reports_unknown_ids(self) -> None:
        self._login("admin")
        self.assertEqual(self.client.delete("/api/auth/invites/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
