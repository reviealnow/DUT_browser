"""Registration, session cookie and passcode administration over HTTP.

Driven through TestClient because the cookie round-trip is the behaviour under
test. The client is built without a context manager on purpose so FastAPI's
startup hook never runs — these tests must not touch the real DUT registry, and
the DB/secret are redirected into a temp directory.
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
from app.services import auth_service


class AuthApiTests(unittest.TestCase):
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

    def test_guest_registers_without_a_passcode(self) -> None:
        response = self.client.post("/api/auth/register", json={"username": "amy"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "guest")
        self.assertIn(auth_service.COOKIE_NAME, response.cookies)

    def test_session_cookie_is_httponly_and_lax(self) -> None:
        response = self.client.post("/api/auth/register", json={"username": "amy"})
        header = response.headers["set-cookie"].lower()
        self.assertIn("httponly", header)
        self.assertIn("samesite=lax", header)

    def test_cookie_is_not_secure_over_plain_http(self) -> None:
        """A Secure cookie is silently dropped over HTTP, so dev (and
        DUT_NO_TLS=1) must not get one — it would log every session out."""
        response = self.client.post("/api/auth/register", json={"username": "amy"})
        self.assertNotIn("secure", response.headers["set-cookie"].lower())

    def test_cookie_is_secure_over_https(self) -> None:
        secure_client = TestClient(app, base_url="https://testserver")
        response = secure_client.post("/api/auth/register", json={"username": "amy"})
        self.assertIn("secure", response.headers["set-cookie"].lower())

    def test_logout_clears_the_cookie_with_matching_flags(self) -> None:
        """delete_cookie must mirror the set flags or the browser keeps the
        original — verified over https, where Secure is in play."""
        secure_client = TestClient(app, base_url="https://testserver")
        secure_client.post("/api/auth/register", json={"username": "amy"})
        header = secure_client.post("/api/auth/logout").headers["set-cookie"].lower()
        self.assertIn("secure", header)
        self.assertIn("httponly", header)
        self.assertIn("samesite=lax", header)
        self.assertEqual(secure_client.get("/api/auth/me").status_code, 401)

    def test_display_name_defaults_to_the_username(self) -> None:
        body = self.client.post("/api/auth/register", json={"username": "amy"}).json()
        self.assertEqual(body["display_name"], "amy")

    def test_engineer_without_passcode_is_refused(self) -> None:
        response = self.client.post(
            "/api/auth/register", json={"username": "amy", "role": "engineer"}
        )
        self.assertEqual(response.status_code, 403)

    def test_engineer_with_wrong_passcode_is_refused(self) -> None:
        auth_service.set_passcode("engineer", "right")
        response = self.client.post(
            "/api/auth/register",
            json={"username": "amy", "role": "engineer", "passcode": "wrong"},
        )
        self.assertEqual(response.status_code, 403)

    def test_engineer_with_correct_passcode_is_admitted(self) -> None:
        auth_service.set_passcode("engineer", "right")
        response = self.client.post(
            "/api/auth/register",
            json={"username": "amy", "role": "engineer", "passcode": "right"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "engineer")

    def test_unknown_role_is_rejected(self) -> None:
        response = self.client.post(
            "/api/auth/register", json={"username": "amy", "role": "root"}
        )
        self.assertEqual(response.status_code, 400)

    def test_blank_username_is_rejected(self) -> None:
        response = self.client.post("/api/auth/register", json={"username": "   "})
        self.assertEqual(response.status_code, 400)

    def test_me_requires_a_session(self) -> None:
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_me_returns_the_registered_identity(self) -> None:
        self.client.post(
            "/api/auth/register", json={"username": "amy", "display_name": "Amy L"}
        )
        body = self.client.get("/api/auth/me").json()
        self.assertEqual(body, {"username": "amy", "display_name": "Amy L", "role": "guest"})

    def test_logout_ends_the_session(self) -> None:
        self.client.post("/api/auth/register", json={"username": "amy"})
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_a_forged_cookie_does_not_authenticate(self) -> None:
        self.client.cookies.set(auth_service.COOKIE_NAME, "forged.token")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_passcodes_endpoint_is_admin_only(self) -> None:
        auth_service.set_passcode("engineer", "eng")
        self.client.post(
            "/api/auth/register",
            json={"username": "amy", "role": "engineer", "passcode": "eng"},
        )
        response = self.client.post("/api/auth/passcodes", json={"engineer": "new"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(auth_service.get_passcode("engineer"), "eng")

    def test_admin_can_rotate_passcodes(self) -> None:
        auth_service.set_passcode("admin", "adm")
        self.client.post(
            "/api/auth/register",
            json={"username": "root", "role": "admin", "passcode": "adm"},
        )
        response = self.client.post("/api/auth/passcodes", json={"engineer": "new-eng"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(auth_service.get_passcode("engineer"), "new-eng")
        self.assertFalse(response.json()["engineer_locked"])

    def test_omitted_passcode_field_is_left_untouched(self) -> None:
        auth_service.set_passcode("admin", "adm")
        auth_service.set_passcode("engineer", "keep-me")
        self.client.post(
            "/api/auth/register",
            json={"username": "root", "role": "admin", "passcode": "adm"},
        )
        self.client.post("/api/auth/passcodes", json={"admin": "rotated"})
        self.assertEqual(auth_service.get_passcode("engineer"), "keep-me")
        self.assertEqual(auth_service.get_passcode("admin"), "rotated")


if __name__ == "__main__":
    unittest.main()
