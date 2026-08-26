"""The role map, asserted at the HTTP layer.

The rest of the suite calls endpoint functions directly, which bypasses FastAPI
dependencies entirely -- so a route losing its gate would go unnoticed. These
tests go through TestClient precisely so the gates are exercised.

TestClient is built without a context manager so the startup hook never runs
(no real DUT registry, no real data/ writes); the DB and session secret are
redirected into a temp directory.
"""

from __future__ import annotations

import re
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

# Admin surfaces: passcodes, the user list, invites, the audit trail and the
# firmware transport. GET-only here for the same reason as ENGINEER_ROUTES --
# these are the ones safe to actually execute.
ADMIN_ROUTES = [
    "/api/auth/users",
    "/api/auth/invites",
    "/api/auth/role-changes",
    "/api/firmware/config",
    # Safe to execute only because the stub context carries no mgmt_url, so the
    # handler refuses before any socket is opened. Give the stub an address and
    # this sweep starts dialling a device from a unit test.
    "/api/fleet/nodes/default/mesh",
]

FLEET_ADMIN_ROUTES = [
    ("POST", "/api/fleet/nodes"),
    ("POST", "/api/fleet/nodes/default/connect"),
    ("POST", "/api/fleet/nodes/default/disconnect"),
    ("POST", "/api/fleet/nodes/default/rssi"),
    ("GET", "/api/fleet/nodes/default/mesh"),
]

# ---------------------------------------------------------------------------
# The whole role map, method by method.
#
# The lists above cover one representative route per surface, which catches a
# router losing its blanket gate but not a single route that never had one.
# ROLE_MAP is the exhaustive version: every path+method the app serves, and the
# minimum role it must demand.
#
# It is written out by hand ON PURPOSE. Deriving it from the app's own
# dependency tree would make the test agree with whatever the app currently
# does -- including a gate somebody deleted. This table is the specification;
# the app is the thing being checked against it.
#
# "guest" here means "any signed-in user" (rank 0), i.e. a session is required
# but no particular role. None means deliberately open to anonymous callers.
# ---------------------------------------------------------------------------
ROLE_MAP: dict[tuple[str, str], str | None] = {
    # -- auth -----------------------------------------------------------
    ("POST", "/api/auth/register"): None,
    ("POST", "/api/auth/redeem"): None,
    ("POST", "/api/auth/logout"): None,
    ("GET", "/api/auth/me"): "guest",
    ("POST", "/api/auth/passcodes"): "admin",
    ("GET", "/api/auth/users"): "admin",
    ("GET", "/api/auth/role-changes"): "admin",
    ("GET", "/api/auth/invites"): "admin",
    ("POST", "/api/auth/invites"): "admin",
    ("DELETE", "/api/auth/invites/{invite_id}"): "admin",
    # -- firmware: admin everywhere; see firmware_api for the one exception,
    #    the DUT's cookieless image fetch, which carries a single-use token.
    ("GET", "/api/firmware/config"): "admin",
    ("PUT", "/api/firmware/credentials"): "admin",
    ("PUT", "/api/firmware/mgmt-url"): "admin",
    ("POST", "/api/firmware/upgrade"): "admin",
    # -- remote fleet: every route drives or configures an SSH console ------
    ("POST", "/api/fleet/nodes"): "admin",
    ("POST", "/api/fleet/nodes/{dut_id}/connect"): "admin",
    ("POST", "/api/fleet/nodes/{dut_id}/disconnect"): "admin",
    ("POST", "/api/fleet/nodes/{dut_id}/rssi"): "admin",
    # The one fleet route that drives no console. Still admin: it reaches the
    # DUT with the management-API credentials, the same reach /api/firmware is
    # gated on.
    ("GET", "/api/fleet/nodes/{dut_id}/mesh"): "admin",
    # -- serial: drives the DUT ------------------------------------------
    ("GET", "/api/serial/ports"): "engineer",
    ("POST", "/api/serial/open"): "engineer",
    ("POST", "/api/serial/close"): "engineer",
    ("POST", "/api/serial/send"): "engineer",
    ("POST", "/api/serial/terminal/enter"): "engineer",
    ("POST", "/api/serial/terminal/exit"): "engineer",
    ("POST", "/api/serial/terminal/resize"): "engineer",
    ("POST", "/api/serial/wifi/kick"): "engineer",
    ("GET", "/api/serial/efficiency-report"): "engineer",
    ("GET", "/api/serial/logs/{file_name}"): "engineer",
    # -- filesystem reach -------------------------------------------------
    ("GET", "/api/logs"): "engineer",
    ("GET", "/api/logs/tail"): "engineer",
    ("GET", "/api/download/{file_name}"): "engineer",
    ("GET", "/api/download/survey/{file_name}"): "engineer",
    ("GET", "/api/download/context/{kind}/{file_name}"): "engineer",
    ("GET", "/api/download/preview/{file_name}"): "engineer",
    ("GET", "/api/analyzer/memory"): "engineer",
    ("POST", "/api/analyzer/run"): "engineer",
    ("POST", "/api/analyzer/run-session"): "engineer",
    # -- workspace content ------------------------------------------------
    ("GET", "/api/files"): "engineer",
    ("POST", "/api/files"): "engineer",
    ("DELETE", "/api/files/{file_id}"): "engineer",
    ("GET", "/api/files/{file_id}/download"): "engineer",
    ("GET", "/api/files/{file_id}/preview"): "engineer",
    ("PUT", "/api/files/{file_id}/tags"): "engineer",
    ("GET", "/api/bulletin/posts"): "engineer",
    ("POST", "/api/bulletin/posts"): "engineer",
    ("PUT", "/api/bulletin/posts/{post_id}"): "engineer",
    ("DELETE", "/api/bulletin/posts/{post_id}"): "engineer",
    ("PUT", "/api/bulletin/posts/{post_id}/tags"): "engineer",
    ("POST", "/api/bulletin/posts/{post_id}/comments"): "engineer",
    ("PUT", "/api/bulletin/comments/{comment_id}"): "engineer",
    ("GET", "/api/workspace/search"): "engineer",
    ("GET", "/api/workspace/tags"): "engineer",
    # -- settings: split gate --------------------------------------------
    ("GET", "/api/settings/crash-keywords"): None,
    ("PUT", "/api/settings/crash-keywords"): "engineer",
    # -- writes a capture to disk ----------------------------------------
    ("POST", "/api/wifi/context-capture"): "engineer",
    # Drives the console, like its neighbour above, and fires on every connect.
    # Deliberately NOT under /api/fleet (all admin): an engineer's connect would
    # then trigger a probe that can only answer 403.
    ("POST", "/api/wifi/mesh-probe"): "engineer",
    # -- read-only telemetry, open by design ------------------------------
    ("GET", "/health"): None,
    ("GET", "/api/version"): None,
    ("GET", "/api/whoami"): None,
    ("GET", "/api/snapshots"): None,
    ("GET", "/api/console/tail"): None,
    ("GET", "/api/wifi/clients"): None,
    ("GET", "/api/wifi/client-stats"): None,
    ("GET", "/api/wifi/capabilities"): None,
    ("GET", "/api/wifi/capability-report"): None,
    ("GET", "/api/wifi/survey"): None,
    ("GET", "/api/wifi/site-survey"): None,
    ("GET", "/api/wifi/channel-recommendation"): None,
    ("GET", "/api/wifi/channel-recommendation/last"): None,
    # -- the DUT registry: split gate -------------------------------------
    # GET is what the switcher reads on every page load, guests included.
    # POST persists a new DUT and DELETE closes that DUT's serial worker, so
    # both are engineer+ -- until 2026-08-02 neither was gated at all, and an
    # anonymous caller on the LAN could end someone else's capture mid-run.
    ("GET", "/api/duts"): None,
    ("POST", "/api/duts"): "engineer",
    ("PATCH", "/api/duts/{dut_id}"): "engineer",
    ("DELETE", "/api/duts/{dut_id}"): "engineer",
}

ROLES_BELOW = {"guest": None, "engineer": "guest", "admin": "engineer"}


def _concrete(path: str) -> str:
    """Fill path params with a placeholder. Any value works: the role check runs
    before the handler, so these requests never reach one."""
    return re.sub(r"\{[^}]+\}", "1", path)


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
        self.label = "stub"
        self.mgmt_url = ""


class _StubRegistry:
    def __init__(self, context: _StubContext) -> None:
        self._context = context

    def get(self, dut_id: str) -> _StubContext:
        if dut_id == "default":
            return self._context
        raise KeyError(dut_id)

    def ids(self) -> list[str]:
        return ["default"]

    def describe(self) -> list[dict]:
        return []


class _ApiCase(unittest.TestCase):
    """Shared fixture: temp DB/uploads/secret, a stub registry, a TestClient.

    The client is built without a context manager so the startup hook never
    runs (no real DUT registry, no real data/ writes); the stub registry stands
    in for the parts of app.state that gated handlers reach for once a request
    is past the gate.
    """

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
        self.context = _StubContext()
        self._stack.enter_context(
            patch.object(app.state, "dut_registry", _StubRegistry(self.context), create=True)
        )
        self.client = TestClient(app)

    def _login(self, role: str) -> None:
        user = auth_service.create_or_update_user(f"user-{role}", role, role)
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))


class RouteProtectionTests(_ApiCase):
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

    # -- admin surfaces -----------------------------------------------------

    def test_admin_routes_reject_an_anonymous_caller(self) -> None:
        for route in ADMIN_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 401)

    def test_admin_routes_reject_an_engineer(self) -> None:
        """The gap that mattered: engineer is the role most sessions run as, so
        an admin route that had slipped to engineer would look fine in use."""
        self._login("engineer")
        for route in ADMIN_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 403)

    def test_admin_passes_the_admin_gate(self) -> None:
        self._login("admin")
        for route in ADMIN_ROUTES:
            with self.subTest(route=route):
                self.assertNotIn(self.client.get(route).status_code, (401, 403))

    def test_every_fleet_route_is_admin_only(self) -> None:
        for method, route in FLEET_ADMIN_ROUTES:
            with self.subTest(method=method, route=route):
                self.assertEqual(self.client.request(method, route).status_code, 401)
        self._login("engineer")
        for method, route in FLEET_ADMIN_ROUTES:
            with self.subTest(method=method, route=route):
                self.assertEqual(self.client.request(method, route).status_code, 403)

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
        self._login("engineer")
        with self.client.websocket_connect("/ws/term?dut=default"):
            pass
        self.assertTrue(self.context.terminal_manager.accepted)


class RoleMapTests(_ApiCase):
    """Every route, not just one per surface.

    The sweeps are all negative (401/403) on purpose: a role gate runs *before*
    the handler, so these requests never execute one -- which is what makes it
    safe to sweep POST /api/firmware/upgrade and DELETE /api/files/{id} in a
    unit test. The positive direction (a sufficient role gets through) stays on
    the GET-only lists above, where executing the handler is harmless.
    """

    def _paths_from_openapi(self) -> set[tuple[str, str]]:
        """The app's own route table, via the public schema rather than
        FastAPI internals (which moved once already: included routers no longer
        flatten into app.routes)."""
        schema = self.client.get("/openapi.json").json()
        return {
            (method.upper(), path)
            for path, operations in schema["paths"].items()
            for method in operations
            if method.upper() not in {"HEAD", "OPTIONS"}
        }

    def test_the_role_map_lists_every_route_the_app_serves(self) -> None:
        """A new route cannot ship without a deliberate entry here -- which is
        the moment someone has to decide whether it needs a gate. WebSocket
        routes are absent from the schema and covered separately below."""
        actual = self._paths_from_openapi()
        declared = set(ROLE_MAP)
        self.assertEqual(
            actual - declared, set(), "route(s) missing from ROLE_MAP -- add them with a role"
        )
        self.assertEqual(
            declared - actual, set(), "ROLE_MAP entr(ies) for routes the app no longer serves"
        )

    def test_every_gated_route_refuses_an_anonymous_caller(self) -> None:
        for (method, path), role in sorted(ROLE_MAP.items()):
            if role is None:
                continue
            with self.subTest(method=method, path=path, role=role):
                response = self.client.request(method, _concrete(path))
                self.assertEqual(response.status_code, 401)

    def test_every_gated_route_refuses_the_role_below_it(self) -> None:
        for (method, path), role in sorted(ROLE_MAP.items()):
            below = ROLES_BELOW.get(role or "")
            if below is None:
                continue  # open, or "any session" -- the anonymous check covers it
            with self.subTest(method=method, path=path, role=role, tried=below):
                self._login(below)
                response = self.client.request(method, _concrete(path))
                self.assertEqual(response.status_code, 403)

    def test_open_routes_are_reachable_without_a_session(self) -> None:
        """Guards the other direction: a gate added to read-only telemetry
        would silently break every unregistered guest browser.

        These GETs do reach their handler, and several then want live runtime
        state this fixture has no business faking (a real serial worker, a
        populated snapshot store), so the client is told to return the handler's
        500 instead of re-raising. A 500 is still proof there is no gate: a gate
        answers 401 without ever calling the handler.
        """
        client = TestClient(app, raise_server_exceptions=False)
        for (method, path), role in sorted(ROLE_MAP.items()):
            if role is not None or method != "GET":
                continue  # a POST/DELETE here would run a real handler
            with self.subTest(path=path):
                self.assertNotIn(client.get(_concrete(path)).status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
