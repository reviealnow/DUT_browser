"""The role map, asserted at the HTTP layer.

The rest of the suite calls endpoint functions directly, which bypasses FastAPI
dependencies entirely -- so a route losing its gate would go unnoticed. These
tests go through TestClient precisely so the gates are exercised.

TestClient is built without a context manager so the startup hook never runs
(no real DUT registry, no real data/ writes); the DB and session secret are
redirected into a temp directory.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.db import workspace
from app.main import app
from app.services import auth_service, file_service

# One representative route per gated surface. GET-only so a passing request is
# side-effect free.
ENGINEER_ROUTES = [
    "/api/files",
    "/api/serial/ports",
    "/api/logs",
    "/api/logs/tail?name=nope.log",
    "/api/download/nope.csv",
    "/api/download/survey/nope.json",
    "/api/download/context/wifi-clients/nope.json",
    "/api/download/preview/nope.png",
    "/api/analyzer/memory",
    "/api/bulletin/posts",
    "/api/workspace/tags",
]

# Read-only telemetry stays open so an unregistered guest browser keeps
# working. crash-keywords GET is deliberately open (guest crash detection must
# use the same keyword list as engineers) while its PUT is engineer.
OPEN_ROUTES = [
    "/health",
    "/api/version",
    "/api/whoami",
    "/api/settings/crash-keywords",
]


class _StubTerminal:
    def __init__(self) -> None:
        self.accepted = False

    async def connect(self, ws) -> None:
        self.accepted = True
        await ws.accept()

    def disconnect(self, ws) -> None:
        pass


class _StubContext:
    def __init__(self) -> None:
        self.terminal_manager = _StubTerminal()
        self.serial_worker = object()


class _StubRegistry:
    def __init__(self, context: _StubContext) -> None:
        self._context = context

    def get(self, dut_id: str) -> _StubContext:
        if dut_id == "default":
            return self._context
        raise KeyError(dut_id)


class RouteProtectionTests(unittest.TestCase):
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
        self.client = TestClient(app)

    def _login(self, role: str) -> None:
        user = auth_service.create_or_update_user(f"user-{role}", role, role)
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))

    # -- REST ---------------------------------------------------------------

    def test_engineer_routes_reject_an_anonymous_caller(self) -> None:
        for route in ENGINEER_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 401)

    def test_engineer_routes_reject_a_guest(self) -> None:
        self._login("guest")
        for route in ENGINEER_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 403)

    def test_engineer_passes_the_gate(self) -> None:
        """A 404/422 from the handler still proves the gate let the request in;
        what must never appear is 401 or 403."""
        self._login("engineer")
        for route in ENGINEER_ROUTES:
            with self.subTest(route=route):
                self.assertNotIn(self.client.get(route).status_code, (401, 403))

    def test_admin_inherits_engineer_access(self) -> None:
        self._login("admin")
        self.assertEqual(self.client.get("/api/files").status_code, 200)

    def test_crash_keywords_get_is_open_but_put_is_engineer(self) -> None:
        """The split-gated settings route: everyone reads the same keyword list,
        only engineers edit it."""
        body = {"keywords": ["kernel panic"]}
        self.assertEqual(self.client.get("/api/settings/crash-keywords").status_code, 200)
        self.assertEqual(self.client.put("/api/settings/crash-keywords", json=body).status_code, 401)
        self._login("guest")
        self.assertEqual(self.client.put("/api/settings/crash-keywords", json=body).status_code, 403)
        self._login("engineer")
        self.assertEqual(self.client.put("/api/settings/crash-keywords", json=body).status_code, 200)

    def test_context_capture_is_engineer_only(self) -> None:
        """The one POST on /api/wifi: its siblings are open read-only telemetry,
        but this one writes captures to disk, so it takes the engineer gate."""
        route = "/api/wifi/context-capture"
        self.assertEqual(self.client.post(route).status_code, 401)
        self._login("guest")
        self.assertEqual(self.client.post(route).status_code, 403)

    def test_open_routes_need_no_session(self) -> None:
        for route in OPEN_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_a_deleted_user_loses_access_immediately(self) -> None:
        self._login("engineer")
        self.assertEqual(self.client.get("/api/files").status_code, 200)
        workspace.execute("DELETE FROM users WHERE username = 'user-engineer'")
        self.assertEqual(self.client.get("/api/files").status_code, 401)

    def test_a_demoted_user_loses_access_immediately(self) -> None:
        self._login("engineer")
        self.assertEqual(self.client.get("/api/files").status_code, 200)
        auth_service.create_or_update_user("user-engineer", "engineer", "guest")
        self.assertEqual(self.client.get("/api/files").status_code, 403)

    # -- WebSockets ---------------------------------------------------------

    def test_ws_telemetry_stays_open(self) -> None:
        """/ws is read-only telemetry and must not require a session."""
        route = next(r for r in app.routes if getattr(r, "path", None) == "/ws")
        self.assertEqual(getattr(route.dependant, "dependencies", []), [])

    def test_ws_term_refuses_an_anonymous_handshake(self) -> None:
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/term"):
                pass

    def test_ws_term_refuses_a_guest(self) -> None:
        self._login("guest")
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/term"):
                pass

    def test_ws_term_admits_an_engineer(self) -> None:
        context = _StubContext()
        self._stack.enter_context(
            patch.object(app.state, "dut_registry", _StubRegistry(context), create=True)
        )
        self._login("engineer")
        with self.client.websocket_connect("/ws/term?dut=default"):
            pass
        self.assertTrue(context.terminal_manager.accepted)


if __name__ == "__main__":
    unittest.main()
