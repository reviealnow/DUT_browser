"""Session tokens, the secret file and role passcodes.

Every test redirects both the workspace DB and the session secret into a temp
directory, so a run never touches (or creates) real state under data/.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.db import workspace
from app.services import auth_service


class AuthServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        self._stack.enter_context(
            patch.object(auth_service, "SESSION_SECRET_FILE", self._dir / "session_secret")
        )
        workspace.init_db()


class SecretTests(AuthServiceTestCase):
    def test_secret_is_created_once_and_reused(self) -> None:
        first = auth_service.load_secret()
        self.assertEqual(len(first), 64)  # 32 random bytes, hex encoded
        self.assertEqual(auth_service.load_secret(), first)

    def test_secret_file_is_owner_only(self) -> None:
        auth_service.load_secret()
        mode = (self._dir / "session_secret").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class TokenTests(AuthServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = auth_service.create_or_update_user("nelson", "Nelson", "engineer")

    def test_roundtrip_carries_identity_and_role(self) -> None:
        payload = auth_service.verify_token(auth_service.create_token(self.user))
        assert payload is not None
        self.assertEqual(payload["username"], "nelson")
        self.assertEqual(payload["role"], "engineer")
        self.assertEqual(payload["user_id"], self.user["id"])

    def test_tampered_signature_is_rejected(self) -> None:
        token = auth_service.create_token(self.user)
        body, _, signature = token.rpartition(".")
        flipped = "0" if signature[-1] != "0" else "1"
        self.assertIsNone(auth_service.verify_token(f"{body}.{signature[:-1]}{flipped}"))

    def test_tampered_payload_is_rejected(self) -> None:
        """A forged role must not verify: the signature covers the payload."""
        token = auth_service.create_token(self.user)
        body, _, signature = token.rpartition(".")
        forged = auth_service.create_token({**self.user, "role": "admin"}).split(".")[0]
        self.assertNotEqual(forged, body)
        self.assertIsNone(auth_service.verify_token(f"{forged}.{signature}"))

    def test_token_from_another_secret_is_rejected(self) -> None:
        token = auth_service.create_token(self.user)
        (self._dir / "session_secret").unlink()
        self.assertIsNone(auth_service.verify_token(token))

    def test_expired_token_is_rejected(self) -> None:
        token = auth_service.create_token(self.user)
        just_valid = time.time() + auth_service.TOKEN_TTL_SECONDS - 60
        self.assertIsNotNone(auth_service.verify_token(token, now=just_valid))
        expired = time.time() + auth_service.TOKEN_TTL_SECONDS + 1
        self.assertIsNone(auth_service.verify_token(token, now=expired))

    def test_malformed_tokens_are_rejected(self) -> None:
        for bad in (None, "", "no-dot", "not-base64.deadbeef", ".", "a.b.c"):
            with self.subTest(token=bad):
                self.assertIsNone(auth_service.verify_token(bad))

    def test_user_is_reread_so_role_changes_apply_immediately(self) -> None:
        token = auth_service.create_token(self.user)
        auth_service.create_or_update_user("nelson", "Nelson", "guest")
        resolved = auth_service.payload_to_user(auth_service.verify_token(token))
        assert resolved is not None
        self.assertEqual(resolved["role"], "guest")

    def test_cookie_header_is_parsed_among_other_cookies(self) -> None:
        token = auth_service.create_token(self.user)
        header = f"theme=dark; {auth_service.COOKIE_NAME}={token}; other=1"
        resolved = auth_service.user_from_cookie_header(header)
        assert resolved is not None
        self.assertEqual(resolved["username"], "nelson")

    def test_cookie_header_without_session_resolves_to_none(self) -> None:
        self.assertIsNone(auth_service.user_from_cookie_header("theme=dark"))
        self.assertIsNone(auth_service.user_from_cookie_header(None))


class UserTests(AuthServiceTestCase):
    def test_registering_a_taken_username_updates_the_role(self) -> None:
        auth_service.create_or_update_user("amy", "Amy", "guest")
        upgraded = auth_service.create_or_update_user("amy", "Amy L", "admin")
        self.assertEqual(upgraded["role"], "admin")
        self.assertEqual(upgraded["display_name"], "Amy L")
        rows = workspace.query_all("SELECT id FROM users WHERE username = 'amy'")
        self.assertEqual(len(rows), 1)

    def test_unknown_role_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            auth_service.create_or_update_user("amy", "Amy", "superuser")


class PasscodeTests(AuthServiceTestCase):
    def test_guest_needs_no_passcode(self) -> None:
        self.assertTrue(auth_service.check_passcode("guest", None))

    def test_unconfigured_role_is_locked_even_for_an_empty_passcode(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(auth_service.get_passcode("engineer"), "")
            self.assertFalse(auth_service.check_passcode("engineer", ""))
            self.assertFalse(auth_service.check_passcode("engineer", None))
            self.assertFalse(auth_service.check_passcode("engineer", "guess"))

    def test_env_default_is_used_until_a_value_is_stored(self) -> None:
        with patch.dict("os.environ", {"DUT_ENGINEER_PASSCODE": "from-env"}):
            self.assertTrue(auth_service.check_passcode("engineer", "from-env"))
            auth_service.set_passcode("engineer", "from-db")
            self.assertFalse(auth_service.check_passcode("engineer", "from-env"))
            self.assertTrue(auth_service.check_passcode("engineer", "from-db"))

    def test_stored_empty_passcode_relocks_the_role(self) -> None:
        auth_service.set_passcode("admin", "letmein")
        self.assertTrue(auth_service.check_passcode("admin", "letmein"))
        auth_service.set_passcode("admin", "")
        self.assertFalse(auth_service.check_passcode("admin", ""))
        self.assertFalse(auth_service.check_passcode("admin", "letmein"))


class RoleRankTests(unittest.TestCase):
    def test_ladder_order(self) -> None:
        self.assertLess(auth_service.role_rank("guest"), auth_service.role_rank("engineer"))
        self.assertLess(auth_service.role_rank("engineer"), auth_service.role_rank("admin"))

    def test_unknown_role_ranks_below_guest(self) -> None:
        self.assertEqual(auth_service.role_rank("wat"), -1)

    def test_require_role_rejects_an_unknown_minimum(self) -> None:
        with self.assertRaises(ValueError):
            auth_service.require_role("superuser")


if __name__ == "__main__":
    unittest.main()
