"""Firmware upgrade: admin gating, the single-use image token, dry run, and the
refusal to flash without a configured command.

Nothing here touches a serial worker for real — the DUT is stubbed — because
the one thing this feature must never do by accident is send a flash command.
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
from app.services import auth_service, file_service, firmware_service


class _StubWorker:
    """Records what would have been sent to the DUT, and sends nothing."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        self.commands.append(cmd)
        return "Upgrade complete"


class _StubContext:
    def __init__(self) -> None:
        self.serial_worker = _StubWorker()


class _StubRegistry:
    def __init__(self, context: _StubContext) -> None:
        self._context = context

    def get(self, dut_id: str) -> _StubContext:
        if dut_id == "default":
            return self._context
        raise KeyError(dut_id)


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
        self._stack.enter_context(patch.dict(firmware_service._image_tokens, {}, clear=True))
        workspace.init_db()

        self.context = _StubContext()
        self.ws = _StubWsManager()
        self._stack.enter_context(
            patch.object(app.state, "dut_registry", _StubRegistry(self.context), create=True)
        )
        self._stack.enter_context(patch.object(app.state, "ws_manager", self.ws, create=True))
        self.client = TestClient(app)

    def _login(self, role: str) -> None:
        user = auth_service.create_or_update_user(f"user-{role}", role, role)
        self.client.cookies.set(auth_service.COOKIE_NAME, auth_service.create_token(user))

    def _upload_image(self) -> int:
        import io

        return file_service.save_uploaded_file("ap6-v2.log", io.BytesIO(b"firmware-bytes"), "root")


class GatingTests(FirmwareTestCase):
    def test_upgrade_and_config_are_admin_only(self) -> None:
        for method, url, body in (
            ("post", "/api/firmware/upgrade", {"file_id": 1}),
            ("get", "/api/firmware/config", None),
            ("put", "/api/firmware/config", {"template": "x {url}"}),
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

    def test_engineer_cannot_flash_even_with_a_valid_file(self) -> None:
        file_id = self._upload_image()
        self._login("engineer")
        response = self.client.post("/api/firmware/upgrade", json={"file_id": file_id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.context.serial_worker.commands, [])


class ImageTokenTests(FirmwareTestCase):
    def test_token_serves_the_image_exactly_once(self) -> None:
        path = self._dir / "fw.bin"
        path.write_bytes(b"image")
        token = firmware_service.publish_image(str(path))
        first = self.client.get(f"/api/firmware/image/{token}")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"image")
        self.assertEqual(self.client.get(f"/api/firmware/image/{token}").status_code, 404)

    def test_image_endpoint_needs_no_session_but_rejects_a_bad_token(self) -> None:
        """The DUT's curl has no cookie, so the token is the only credential."""
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/firmware/image/made-up").status_code, 404)

    def test_expired_token_is_refused(self) -> None:
        path = self._dir / "fw.bin"
        path.write_bytes(b"image")
        token = firmware_service.publish_image(str(path), ttl=-1)
        self.assertEqual(self.client.get(f"/api/firmware/image/{token}").status_code, 404)


class UpgradeTests(FirmwareTestCase):
    def test_flash_is_refused_when_no_command_is_configured(self) -> None:
        """The safety property: an unconfigured endpoint must never be guessed."""
        file_id = self._upload_image()
        self._login("admin")
        with patch.object(firmware_service, "is_dry_run", return_value=False):
            response = self.client.post("/api/firmware/upgrade", json={"file_id": file_id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("No upgrade command configured", response.json()["detail"])
        self.assertEqual(self.context.serial_worker.commands, [])

    def test_dry_run_streams_every_stage_and_sends_nothing(self) -> None:
        file_id = self._upload_image()
        self._login("admin")
        with patch.object(firmware_service, "is_dry_run", return_value=True):
            response = self.client.post("/api/firmware/upgrade", json={"file_id": file_id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dry_run"])
        stages = [e["stage"] for e in self.ws.events if e["type"] == "firmware_progress"]
        self.assertEqual(stages, list(firmware_service.STAGES))
        self.assertEqual(self.context.serial_worker.commands, [])

    def test_a_configured_command_is_sent_with_the_image_url_substituted(self) -> None:
        file_id = self._upload_image()
        firmware_service.set_upgrade_template("sysupgrade -n {url}")
        self._login("admin")
        with patch.object(firmware_service, "is_dry_run", return_value=False):
            response = self.client.post("/api/firmware/upgrade", json={"file_id": file_id})
        self.assertEqual(response.status_code, 200, response.text)
        [command] = self.context.serial_worker.commands
        self.assertTrue(command.startswith("sysupgrade -n http://"))
        self.assertIn("/api/firmware/image/", command)
        self.assertNotIn("{url}", command)

    def test_progress_events_are_tagged_with_the_dut(self) -> None:
        file_id = self._upload_image()
        self._login("admin")
        with patch.object(firmware_service, "is_dry_run", return_value=True):
            self.client.post("/api/firmware/upgrade", json={"file_id": file_id})
        self.assertTrue(all(e["dut_id"] == "default" for e in self.ws.events))

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

    def test_the_image_token_is_revoked_after_the_attempt(self) -> None:
        file_id = self._upload_image()
        self._login("admin")
        with patch.object(firmware_service, "is_dry_run", return_value=True):
            body = self.client.post("/api/firmware/upgrade", json={"file_id": file_id}).json()
        token = body["url"].rsplit("/", 1)[-1]
        self.assertEqual(self.client.get(f"/api/firmware/image/{token}").status_code, 404)


class ConfigTests(FirmwareTestCase):
    def test_template_must_contain_the_url_placeholder(self) -> None:
        self._login("admin")
        self.assertEqual(
            self.client.put("/api/firmware/config", json={"template": "sysupgrade -n"}).status_code,
            400,
        )

    def test_config_roundtrip_and_clearing(self) -> None:
        self._login("admin")
        self.client.put("/api/firmware/config", json={"template": "sysupgrade -n {url}"})
        self.assertTrue(self.client.get("/api/firmware/config").json()["configured"])
        self.client.put("/api/firmware/config", json={"template": ""})
        self.assertFalse(self.client.get("/api/firmware/config").json()["configured"])


if __name__ == "__main__":
    unittest.main()
