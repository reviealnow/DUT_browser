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
# The two image types are not interchangeable: the web UI takes the signed one,
# the management API the encrypted one. Tests name them explicitly so a wrong
# pairing shows up as an intentional case rather than an accident.
SIGNED_NAME = "AP6_v2.sig"
ENCRYPTED_NAME = "ubi_kernel_AP6_840E-encrypt_v110339.bin"
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

    def _upload_image(self, data: bytes = IMAGE_BYTES, name: str = SIGNED_NAME) -> int:
        return file_service.save_uploaded_file(name, io.BytesIO(data), "root")

    def auth_schemes(self) -> list[str]:
        """Which auth scheme the client offered, per request that carried one."""
        return [
            self.requests[i].headers["authorization"].split()[0].lower()
            for i in range(len(self.requests))
            if "authorization" in self.requests[i].headers
        ]

    def _ready_with_csrf(self, token: str | None, status: int = 200) -> None:
        """Like `_ready`, but common.cgi answers with (or without) a CSRF token.

        `token=None` models a build with HTTP_SUPPORT_CSRF off, where the page
        disables the field instead of sending one.
        """
        self.context.mgmt_url = "https://192.0.2.10:443"
        firmware_service.set_credentials("admin", "secret")
        self.context.console_buffer.lines = [
            f"Execute {firmware_service.FLASH_STARTED_MARKER} ..."
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            authed = "authorization" in request.headers
            if "common.cgi" in str(request.url):
                # Faithful to the real AP6_840E: this endpoint NEVER challenges.
                # It answers anonymous callers 200 with a token-less body, which
                # is indistinguishable from "CSRF is disabled" — the trap that
                # made the first real flash fail with 577. Only an authenticated
                # caller is given a token.
                if not authed or token is None:
                    return httpx.Response(200, json={"SET_INFO": {"language": "uk", "": ""}})
                return httpx.Response(200, json={"SET_INFO": {"CSRFToken": token}})
            if not authed:
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Digest qop="auth", realm="localhost", nonce="abc123"'
                    },
                )
            return httpx.Response(status, text="ok")

        self._stack.enter_context(
            patch.object(
                firmware_service,
                "run_upgrade",
                _with_mock_transport(firmware_service.run_upgrade, httpx.MockTransport(handler)),
            )
        )

    def _ready(self, status: int = 200) -> None:
        """A DUT that is addressable, credentialed and answers `status`."""
        self.context.mgmt_url = "https://192.0.2.10:10443"
        firmware_service.set_credentials("admin", "secret")
        self.context.console_buffer.lines = [
            f"Execute {firmware_service.FLASH_STARTED_MARKER} ..."
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            # Mimic the real device: challenge an unauthenticated request with
            # Digest, so the client's auth handshake is actually exercised
            # rather than short-circuited by an immediate 200.
            if "authorization" not in request.headers:
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Digest qop="auth", realm="localhost", nonce="abc123"'
                    },
                )
            return httpx.Response(status, text="ok")

        transport = httpx.MockTransport(handler)
        self._stack.enter_context(
            patch.object(
                firmware_service,
                "run_upgrade",
                _with_mock_transport(firmware_service.run_upgrade, transport),
            )
        )


    def _ready_gui_redirect(self, status: int, location: str) -> None:
        """A gui-transport DUT whose upload answers with a redirect.

        The real device answers `302 -> fwtemp.html` when it takes the image and
        `301 -> /busy.html` when its web UI is locked. Both are 3xx, so only the
        redirect target tells them apart.
        """
        self.context.mgmt_url = "https://192.0.2.10:443"
        firmware_service.set_credentials("admin", "secret")
        self.context.console_buffer.lines = [
            f"Execute {firmware_service.FLASH_STARTED_MARKER} ..."
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if "common.cgi" in str(request.url):
                return httpx.Response(200, json={"SET_INFO": {"CSRFToken": "tok"}})
            if "authorization" not in request.headers:
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Digest qop="auth", realm="localhost", nonce="abc123"'
                    },
                )
            return httpx.Response(status, headers={"Location": location})

        self._stack.enter_context(
            patch.object(
                firmware_service,
                "run_upgrade",
                _with_mock_transport(firmware_service.run_upgrade, httpx.MockTransport(handler)),
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
        self.context.mgmt_url = "https://192.0.2.10:10443"
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
        self._ready(status=401)  # rejects even the authenticated retry
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 502)
        # Wording is shared by both transports: the gui path meets the 401 while
        # fetching a CSRF token, the api path on the upload itself.
        self.assertIn("rejected the credentials", response.json()["detail"])


class RejectionTests(FirmwareTestCase):
    def test_a_malformed_refusal_is_reported_as_a_rejection_not_a_network_error(self) -> None:
        """AP6_840E answers a refused image with a bare error line where an HTTP
        header belongs. That is the DUT saying no, not the network failing."""
        file_id = self._upload_image()
        self.context.mgmt_url = "https://192.0.2.10:10443"
        firmware_service.set_credentials("admin", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if "authorization" not in request.headers:
                return httpx.Response(
                    401,
                    headers={"WWW-Authenticate": 'Digest qop="auth", realm="l", nonce="n"'},
                )
            raise httpx.RemoteProtocolError(
                "illegal header line: bytearray(b\"Can't open FW.signauture.st1 for reading\")"
            )

        transport = httpx.MockTransport(handler)
        self._stack.enter_context(
            patch.object(
                firmware_service,
                "run_upgrade",
                _with_mock_transport(firmware_service.run_upgrade, transport),
            )
        )
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("upgrade handler refused it", detail)
        self.assertIn("FW.signauture.st1", detail, "the DUT's own complaint must survive")


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
        """The api transport, which takes the encrypted image."""
        file_id = self._upload_image(name=ENCRYPTED_NAME)
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "api"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        sent = self.requests[-1]
        self.assertEqual(sent.method, "PUT")
        self.assertEqual(str(sent.url), "https://192.0.2.10:10443/ap/systemctl/sysFwUpgrade")
        self.assertEqual(sent.headers["content-type"], "application/octet-stream")
        self.assertEqual(sent.content, IMAGE_BYTES)
        # Digest, not Basic: httpx probes unauthenticated, is challenged, then
        # retries with the header — so the scheme is asserted on the retry.
        self.assertEqual(set(self.auth_schemes()), {"digest"})

    def test_no_expect_header_is_ever_sent(self) -> None:
        """An empty `Expect:` is a literal empty header, not a removal — the DUT
        answers that with 417 Expectation Failed. httpx never adds one, so the
        header must simply be absent."""
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        self.client.post("/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False})
        for request in self.requests:
            self.assertNotIn("expect", [k.lower() for k in request.headers])

    def test_the_authenticated_retry_still_carries_the_whole_image(self) -> None:
        """Digest sends the request twice. A streaming body would be consumed by
        the probe, handing a device about to flash itself an empty image."""
        file_id = self._upload_image(name=ENCRYPTED_NAME)
        self._ready()
        self._login("admin")
        self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "api"},
        )
        authed = [r for r in self.requests if "authorization" in r.headers]
        self.assertTrue(authed, "the authenticated attempt is the one that flashes")
        self.assertEqual(authed[-1].content, IMAGE_BYTES)

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
    def test_a_bare_ip_is_stored_without_a_port(self) -> None:
        """The two transports listen on different ports (443 vs 10443), so the
        stored address must stay portless — pinning one here sends the other
        transport somewhere that 404s. The port is applied per upload instead."""
        self._login("admin")
        body = self.client.put("/api/firmware/mgmt-url", json={"mgmt_url": "192.0.2.10"}).json()
        self.assertEqual(body["mgmt_url"], "https://192.0.2.10")

    def test_an_explicit_port_is_never_overridden(self) -> None:
        self._login("admin")
        body = self.client.put(
            "/api/firmware/mgmt-url", json={"mgmt_url": "https://192.0.2.10:443/"}
        ).json()
        self.assertEqual(body["mgmt_url"], "https://192.0.2.10:443")

    def test_config_lists_the_duts_and_their_addresses(self) -> None:
        self._login("admin")
        self.client.put("/api/firmware/mgmt-url", json={"mgmt_url": "192.0.2.10"})
        config = self.client.get("/api/firmware/config").json()
        self.assertEqual(config["duts"][0]["mgmt_url"], "https://192.0.2.10")
        self.assertEqual(config["upgrade_path"], "/ap/systemctl/sysFwUpgrade")
        self.assertEqual([t["id"] for t in config["transports"]], ["gui", "api"])
        self.assertEqual(config["default_transport"], "gui")


class GuiTransportTests(FirmwareTestCase):
    """The web-UI upload, whose contract was read off /www/html/fwupdate.html and
    /www/mongoose.config on a real AP6_840E."""

    def _uploads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def test_the_signed_image_is_posted_as_multipart_to_submit_cgi(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["transport"], "gui")

        sent = self._uploads()[-1]
        self.assertEqual(str(sent.url), "https://192.0.2.10:10443/submit.cgi")
        self.assertTrue(sent.headers["content-type"].startswith("multipart/form-data"))
        body = sent.content
        # The device's own field names; getting any of them wrong is a silent
        # no-op at flash time, so they are asserted literally.
        self.assertIn(b'name="binary"', body)
        self.assertIn(b'name="submitpg"', body)
        self.assertIn(b"fwupdate_pc.html", body)
        self.assertIn(b'name="decodepwd"', body)
        self.assertIn(IMAGE_BYTES, body, "the whole image must reach the DUT")
        self.assertEqual(set(self.auth_schemes()), {"digest"})

    def test_the_csrf_token_is_fetched_and_forwarded(self) -> None:
        self._ready_with_csrf("tok-4242")
        file_id = self._upload_image()
        self._login("admin")
        self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )
        fetched = [r for r in self.requests if "common.cgi" in str(r.url)]
        self.assertTrue(fetched, "the page reads its token from common.cgi")
        self.assertIn("csrftoken=1", str(fetched[-1].url))
        self.assertIn(b"tok-4242", self._uploads()[-1].content)

    def test_the_token_fetch_is_authenticated(self) -> None:
        """The bug that made the first real flash fail with 577.

        common.cgi answers anonymous callers 200 with a token-less body, and
        httpx's DigestAuth only attaches credentials after a 401 — so on a fresh
        client the token fetch went out ANONYMOUS, read no token, and the upload
        omitted CSRFToken while the device very much required it. The fetch must
        therefore carry Authorization.
        """
        self._ready_with_csrf("tok-auth")
        file_id = self._upload_image()
        self._login("admin")
        self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )
        fetched = [r for r in self.requests if "common.cgi" in str(r.url)]
        self.assertTrue(fetched, "the token must be fetched at all")
        self.assertTrue(
            any("authorization" in r.headers for r in fetched),
            "the CSRF fetch went out anonymous — this is exactly the 577 bug",
        )

    def test_the_upload_carries_origin_and_referer(self) -> None:
        """Mirrors the captured browser request the DUT accepts."""
        self._ready_with_csrf("tok-hdr")
        file_id = self._upload_image()
        self._login("admin")
        self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )
        sent = self._uploads()[-1]
        self.assertEqual(sent.headers["origin"], "https://192.0.2.10:443")
        self.assertTrue(sent.headers["referer"].endswith("/fwupdate.html"))

    def test_a_build_without_csrf_still_uploads(self) -> None:
        """fwupdate.html disables the field when HTTP_SUPPORT_CSRF is off, so a
        missing token is a valid state and must not block the flash."""
        self._ready_with_csrf(None)
        file_id = self._upload_image()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(b'name="CSRFToken"', self._uploads()[-1].content)

    def test_gui_defaults_to_port_443(self) -> None:
        self.assertEqual(
            firmware_service.normalise_mgmt_url("192.0.2.10", "gui"),
            "https://192.0.2.10:443",
        )
        self.assertEqual(
            firmware_service.normalise_mgmt_url("192.0.2.10", "api"),
            "https://192.0.2.10:10443",
        )

    def test_an_explicit_port_survives_either_transport(self) -> None:
        for transport in ("gui", "api"):
            with self.subTest(transport=transport):
                self.assertEqual(
                    firmware_service.normalise_mgmt_url("192.0.2.10:9999", transport),
                    "https://192.0.2.10:9999",
                )

    def test_the_transport_is_the_default_so_signed_images_just_work(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade", json={"file_id": file_id, "dry_run": False}
        )
        self.assertEqual(response.json()["transport"], "gui")


class ImagePairingTests(FirmwareTestCase):
    """The vendor's rule, enforced before anything reaches the DUT: the API takes
    the encrypted image, the web UI the signed one."""

    def test_a_signed_image_is_refused_on_the_api_transport(self) -> None:
        file_id = self._upload_image(name=SIGNED_NAME)
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "api"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("encrypted", response.json()["detail"])
        self.assertEqual(self.requests, [], "nothing may reach the DUT")

    def test_an_encrypted_image_is_refused_on_the_gui_transport(self) -> None:
        file_id = self._upload_image(name=ENCRYPTED_NAME)
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("signed", response.json()["detail"])
        self.assertEqual(self.requests, [], "nothing may reach the DUT")

    def test_an_unrecognised_name_is_allowed_through(self) -> None:
        """The check is a guard against a known-wrong pairing, not a whitelist —
        it must not block an image the vendor names differently."""
        for transport in ("gui", "api"):
            with self.subTest(transport=transport):
                firmware_service.check_image_for_transport("mystery.img", transport)

    def test_unknown_transport_is_rejected(self) -> None:
        file_id = self._upload_image()
        self._ready()
        self._login("admin")
        response = self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "telepathy"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown transport", response.json()["detail"])

    def test_image_kind_classifies_both_real_names(self) -> None:
        self.assertEqual(firmware_service.image_kind("wifix.tar.gz.sig"), "signed")
        self.assertEqual(
            firmware_service.image_kind("ubi_kernel_AP6_420E-encrypt_v110339.bin"), "encrypted"
        )
        self.assertEqual(firmware_service.image_kind("firmware.bin"), "unknown")
        self.assertEqual(firmware_service.image_kind(""), "unknown")


class BusyLockTests(FirmwareTestCase):
    """The device's web server locks for a few minutes after any web-UI submit.

    Measured on a real AP6_840E: ~3.5 minutes after an ordinary submit, and much
    longer after a flash and reboot. The lock lives in the vendor-patched
    Mongoose, so `/submit.cgi` is answered `301 -> /busy.html` before cgi_box
    ever runs and the image is not received at all.
    """

    def _upgrade(self) -> httpx.Response:
        file_id = self._upload_image()
        self._login("admin")
        return self.client.post(
            "/api/firmware/upgrade",
            json={"file_id": file_id, "dry_run": False, "transport": "gui"},
        )

    def test_a_busy_redirect_is_reported_as_a_failure_not_an_accepted_upgrade(self) -> None:
        """The dangerous case: a busy 301 looks like the accepted 302.

        Both are 3xx and both sail past the >= 400 check, so before this was
        handled the operator was told the upgrade had been accepted while the
        DUT had received nothing.
        """
        self._ready_gui_redirect(301, "/busy.html")
        response = self._upgrade()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("busy", response.json()["detail"].lower())

    def test_the_normal_redirect_to_the_progress_page_is_still_accepted(self) -> None:
        """Guards the fix from over-reaching: only busy.html means refused."""
        self._ready_gui_redirect(302, "/fwtemp.html")
        response = self._upgrade()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    def test_a_query_string_or_absolute_url_does_not_hide_the_busy_page(self) -> None:
        self._ready_gui_redirect(301, "https://192.0.2.10/busy.html?from=fwupdate")
        response = self._upgrade()
        self.assertEqual(response.status_code, 503, response.text)


if __name__ == "__main__":
    unittest.main()
