"""Firmware upgrade: admin gating, checksum verification, credential handling,
and the console confirmation that the DUT actually started flashing.

No test ever reaches a real device: httpx is driven through a MockTransport, so
"did we send the right bytes to the right URL" is asserted without a DUT, and an
accidental real PUT would fail rather than flash something.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.db import workspace
from app.main import app
from app.services import auth_service, file_service, firmware_service

IMAGE_BYTES = b"customer-signed firmware image"
IMAGE_SHA = hashlib.sha256(IMAGE_BYTES).hexdigest()


class _StubConsole:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines or []

    def recent(self, limit: int = 200) -> list[str]:
        return self.lines


class _StubContext:
    def __init__(self) -> None:
        self.dut_id = "default"
        self.label = "Default"
        self.mgmt_url = ""
        self.console_buffer = _StubConsole()


class _StubRegistry:
    def __init__(self, context: _StubContext) -> None:
        self._context = context

    def get(self, dut_id: str) -> _StubContext:
        if dut_id == "default":
            return self._context
        raise KeyError(dut_id)

    def ids(self) -> list[str]:
        return ["default"]

    def record_mgmt_url(self, dut_id: str, mgmt_url: str) -> None:
        self._context.mgmt_url = mgmt_url


class _StubWsManager:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit_from_thread(self, event: dict) -> None:
        self.events.append(event)


class FirmwareTestCase(unittest.TestCase):
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
        self.ws = _StubWsManager()
        self._stack.enter_context(
            patch.object(app.state, "dut_registry", _StubRegistry(self.context), create=True)
        )
        self._stack.enter_context(patch.object(app.state, "ws_manager", self.ws, create=True))
        self.client = TestClient(app)
        self.requests: list[httpx.Request] = []

    def _login(self, role: str) -> None:
        user = auth_service.create_or_update_user(f"user-{role}", role, role)
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))

    def _upload_image(self, data: bytes = IMAGE_BYTES) -> int:
        return file_service.save_uploaded_file("AP6_v2.sig", io.BytesIO(data), "root")

    def _ready(self, status: int = 200) -> None:
        """A DUT that is addressable, credentialed and answers `status`."""
        self.context.mgmt_url = "https://192.0.2.10"
        firmware_service.set_credentials("admin", "secret")
        self.context.console_buffer.lines = [
            f"Execute {firmware_service.FLASH_STARTED_MARKER} ..."
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status, text="ok")

        transport = httpx.MockTransport(handler)
        self._stack.enter_context(
            patch.object(
                firmware_service,
                "run_upgrade",
                _with_mock_transport(firmware_service.run_upgrade, transport),
            )
        )


def _with_mock_transport(original, transport):
    """Force run_upgrade onto a MockTransport so no test can reach a network."""

    def wrapper(*args, **kwargs):
        kwargs.setdefault("client_factory", lambda: httpx.Client(transport=transport))
        return original(*args, **kwargs)

    return wrapper


class GatingTests(FirmwareTestCase):
    def test_every_endpoint_is_admin_only(self) -> None:
        for method, url, body in (
            ("post", "/api/firmware/upgrade", {"file_id": 1}),
            ("get", "/api/firmware/config", None),
            ("put", "/api/firmware/credentials", {"user": "a", "password": "b"}),
            ("put", "/api/firmware/mgmt-url", {"mgmt_url": "https://x"}),
        ):
            with self.subTest(url=url, actor="anonymous"):
                self.client.cookies.clear()
                call = getattr(self.client, method)
                response = call(url, json=body) if body else call(url)
                self.assertEqual(response.status_code, 401)
            with self.subTest(url=url, actor="engineer"):
                self._login("engineer")
                call = getattr(self.client, method)
                response = call(url, json=body) if body else call(url)
                self.assertEqual(response.status_code, 403)


class CredentialTests(FirmwareTestCase):
    def test_the_password_is_never_returned(self) -> None:
        """Not even to an admin: a UI that can display it is one that can leak it."""
        self._login("admin")
        self.client.put("/api/firmware/credentials", json={"user": "admin", "password": "s3cret"})
        body = self.client.get("/api/firmware/config").text
        self.assertNotIn("s3cret", body)
        self.assertTrue(self.client.get("/api/firmware/config").json()["has_credentials"])

    def test_credentials_are_required_for_a_real_upgrade(self) -> None:
        file_id = self._upload_image()
        self.context.mgmt_url = "https://192.0.2.10"
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("credentials", response.json()["detail"].lower())

    def test_a_rejected_credential_surfaces_as_a_distinct_error(self) -> None:
        """Per the operator: a 401 means the device is off its expected defaults,
        which is a finding to report, not something to retry around."""
        file_id = self._upload_image()
        self._ready(status=401)
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("rejected the API credentials", response.json()["detail"])


class ChecksumTests(FirmwareTestCase):
    def test_a_wrong_expected_checksum_blocks_the_upload(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "expected_sha256": "0" * 64, "dry_run": False},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.requests, [], "nothing may be sent when the checksum fails")

    def test_a_matching_expected_checksum_proceeds(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "expected_sha256": IMAGE_SHA.upper(), "dry_run": False},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_bytes_changed_on_disk_are_caught(self) -> None:
        """The stored digest proves what was uploaded, not what is there now."""
        file_id = self._upload_image()
        row = file_service.get_file_by_id(file_id)
        Path(row["filepath"]).write_bytes(b"tampered")
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.requests, [])


class UpgradeTests(FirmwareTestCase):
    def test_the_image_is_put_to_the_documented_endpoint(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        [sent] = self.requests
        self.assertEqual(sent.method, "PUT")
        self.assertEqual(str(sent.url), "https://192.0.2.10/ap/systemctl/sysFwUpgrade")
        self.assertEqual(sent.headers["content-type"], "application/octet-stream")
        self.assertEqual(sent.content, IMAGE_BYTES)
        self.assertIn("authorization", sent.headers)

    def test_a_missing_management_address_refuses_before_anything_is_sent(self) -> None:
        file_id = self._upload_image()
        firmware_service.set_credentials("admin", "secret")
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("management address", response.json()["detail"])

    def test_dry_run_streams_every_stage_and_sends_nothing(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dry_run"])
        stages = [e["stage"] for e in self.ws.events if e["type"] == "firmware_progress"]
        self.assertEqual(stages, list(firmware_service.STAGES))
        self.assertEqual(self.requests, [])

    def test_a_request_cannot_switch_off_a_deployment_dry_run(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        with patch.object(firmware_service, "is_dry_run", return_value=True):
            body = self.client.post(
                "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
            ).json()
        self.assertTrue(body["dry_run"], "DUT_FIRMWARE_DRY_RUN must not be overridable")
        self.assertEqual(self.requests, [])

    def test_unknown_file_and_unknown_dut_are_404(self) -> None:
        self._login("admin")
        self.assertEqual(
            self.client.post("/api/firmware/upgrade", json={"file_id": 4242}).status_code, 404
        )
        file_id = self._upload_image()
        self.assertEqual(
            self.client.post(
                "/api/firmware/upgrade", json={"file_id": file_id, "dut": "nope"}
            ).status_code,
            404,
        )


class FlashConfirmationTests(FirmwareTestCase):
    """A 200 means the request was accepted; the console says it actually began."""

    def test_the_start_marker_is_reported_when_seen(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        body = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        ).json()
        self.assertIs(body["flash_started"], True)
        self.assertIn(firmware_service.FLASH_STARTED_MARKER, body["detail"])

    def test_a_silent_console_is_reported_honestly_not_as_success(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self.context.console_buffer.lines = ["nothing interesting here"]
        self._login("admin")
        with patch.object(firmware_service, "FLASH_START_WAIT_SECONDS", 0.0):
            body = self.client.post(
                "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
            ).json()
        self.assertIs(body["flash_started"], False)
        self.assertIn("was not seen", body["detail"])

    def test_wait_returns_as_soon_as_the_marker_appears(self) -> None:
        seen: list[float] = []
        lines: list[str] = []

        def console() -> list[str]:
            seen.append(1.0)
            if len(seen) >= 3:
                lines.append("Execute wifix_downloader.sh ...")
            return lines

        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        started = firmware_service.wait_for_flash_start(
            console, timeout=10.0, now=lambda: next(clock), sleep=lambda _s: None
        )
        self.assertTrue(started)
        self.assertEqual(len(seen), 3)


class MgmtUrlTests(FirmwareTestCase):
    def test_a_bare_ip_is_normalised_to_https(self) -> None:
        self._login("admin")
        body = self.client.put("/api/firmware/mgmt-url", json={"mgmt_url": "192.0.2.10"}).json()
        self.assertEqual(body["mgmt_url"], "https://192.0.2.10")

    def test_an_explicit_scheme_and_port_are_kept(self) -> None:
        self._login("admin")
        body = self.client.put(
            "/api/firmware/mgmt-url", json={"mgmt_url": "https://192.0.2.10:10443/"}
        ).json()
        self.assertEqual(body["mgmt_url"], "https://192.0.2.10:10443")

    def test_config_lists_the_duts_and_their_addresses(self) -> None:
        self._login("admin")
        self.client.put("/api/firmware/mgmt-url", json={"mgmt_url": "192.0.2.10"})
        config = self.client.get("/api/firmware/config").json()
        self.assertEqual(config["duts"][0]["mgmt_url"], "https://192.0.2.10")
        self.assertEqual(config["upgrade_path"], "/ap/systemctl/sysFwUpgrade")


if __name__ == "__main__":
    unittest.main()
