"""Invite minting, revocation and the atomicity of redemption.

Every test redirects the workspace DB into a temp directory so a run never
touches real state under data/.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.db import workspace
from app.services import invite_service


class InviteServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        workspace.init_db()


class MintTests(InviteServiceTestCase):
    def test_mint_returns_a_token_and_a_url_path(self) -> None:
        invite = invite_service.create_invite("engineer", label="lab", created_by="root")
        self.assertTrue(invite["token"])
        self.assertEqual(invite["url_path"], f"/?invite={invite['token']}")
        self.assertEqual(invite["role"], "engineer")
        self.assertEqual(invite["max_uses"], 1)

    def test_only_the_hash_is_persisted(self) -> None:
        """The point of the whole design: a DB dump must contain no usable invite."""
        invite = invite_service.create_invite("admin")
        dump = str([dict(row) for row in workspace.query_all("SELECT * FROM auth_tokens")])
        self.assertNotIn(invite["token"], dump)
        self.assertIn(invite_service.hash_token(invite["token"]), dump)

    def test_two_invites_never_share_a_token(self) -> None:
        tokens = {invite_service.create_invite("guest")["token"] for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    def test_unknown_role_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            invite_service.create_invite("superuser")

    def test_zero_max_uses_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            invite_service.create_invite("guest", max_uses=0)

    def test_no_expiry_when_hours_is_none(self) -> None:
        invite = invite_service.create_invite("guest", expires_in_hours=None)
        self.assertIsNone(invite["expires_at"])
        self.assertIsNotNone(invite_service.consume_invite(invite["token"]))


class ConsumeTests(InviteServiceTestCase):
    def test_single_use_invite_works_exactly_once(self) -> None:
        invite = invite_service.create_invite("engineer")
        first = invite_service.consume_invite(invite["token"])
        assert first is not None
        self.assertEqual(first["role"], "engineer")
        self.assertIsNone(invite_service.consume_invite(invite["token"]))

    def test_multi_use_invite_stops_at_max_uses(self) -> None:
        invite = invite_service.create_invite("guest", max_uses=3)
        results = [invite_service.consume_invite(invite["token"]) is not None for _ in range(4)]
        self.assertEqual(results, [True, True, True, False])

    def test_expired_invite_is_refused(self) -> None:
        invite = invite_service.create_invite("engineer", expires_in_hours=-1)
        self.assertIsNone(invite_service.consume_invite(invite["token"]))

    def test_revoked_invite_is_refused(self) -> None:
        invite = invite_service.create_invite("admin", max_uses=5)
        self.assertTrue(invite_service.revoke_invite(invite["id"]))
        self.assertIsNone(invite_service.consume_invite(invite["token"]))

    def test_unknown_and_empty_tokens_are_refused(self) -> None:
        for bad in ("", "not-a-token", "x" * 40):
            with self.subTest(token=bad):
                self.assertIsNone(invite_service.consume_invite(bad))

    def test_a_failed_redemption_does_not_burn_a_use(self) -> None:
        invite = invite_service.create_invite("guest", max_uses=2)
        invite_service.consume_invite("wrong-token")
        rows = invite_service.list_invites()
        self.assertEqual(rows[0]["used_count"], 0)

    def test_concurrent_redemptions_of_a_single_use_invite_yield_one_winner(self) -> None:
        """The reason validity and consumption are one UPDATE: a check-then-write
        would let both threads through."""
        invite = invite_service.create_invite("engineer")
        token = invite["token"]
        results: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def attempt() -> None:
            start.wait()
            outcome = invite_service.consume_invite(token) is not None
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(results), 1, f"expected exactly one winner, got {results}")
        self.assertEqual(invite_service.list_invites()[0]["used_count"], 1)


class ListAndRevokeTests(InviteServiceTestCase):
    def test_listing_never_exposes_hashes_or_tokens(self) -> None:
        invite = invite_service.create_invite("engineer", label="lab")
        [row] = invite_service.list_invites()
        self.assertNotIn("token", row)
        self.assertNotIn("token_hash", row)
        self.assertEqual(row["label"], "lab")
        self.assertNotIn(invite["token"], str(row))

    def test_derived_flags(self) -> None:
        used = invite_service.create_invite("guest")
        invite_service.consume_invite(used["token"])
        revoked = invite_service.create_invite("guest")
        invite_service.revoke_invite(revoked["id"])
        by_id = {row["id"]: row for row in invite_service.list_invites()}
        self.assertTrue(by_id[used["id"]]["exhausted"])
        self.assertFalse(by_id[used["id"]]["revoked"])
        self.assertTrue(by_id[revoked["id"]]["revoked"])
        self.assertFalse(by_id[revoked["id"]]["exhausted"])

    def test_revoking_an_unknown_invite_reports_failure(self) -> None:
        self.assertFalse(invite_service.revoke_invite(4242))

    def test_revoking_twice_is_idempotent(self) -> None:
        invite = invite_service.create_invite("guest")
        self.assertTrue(invite_service.revoke_invite(invite["id"]))
        self.assertTrue(invite_service.revoke_invite(invite["id"]))


class QrTests(unittest.TestCase):
    def test_qr_svg_encodes_the_payload(self) -> None:
        svg = invite_service.qr_svg("/?invite=abc123")
        self.assertIn("<svg", svg)
        self.assertIn("path", svg)


class CliTests(InviteServiceTestCase):
    def test_cli_mints_a_redeemable_token(self) -> None:
        from app import invite_cli

        with patch("sys.stdout") as stdout:
            code = invite_cli.main(["mint", "--role", "engineer", "--label", "launcher"])
        self.assertEqual(code, 0)
        printed = "".join(call.args[0] for call in stdout.write.call_args_list).strip()
        consumed = invite_service.consume_invite(printed)
        assert consumed is not None
        self.assertEqual(consumed["role"], "engineer")
        self.assertEqual(invite_service.list_invites()[0]["created_by"], "launcher")

    def test_cli_zero_expiry_means_never(self) -> None:
        from app import invite_cli

        with patch("sys.stdout"):
            invite_cli.main(["mint", "--role", "guest", "--expires-hours", "0"])
        self.assertIsNone(invite_service.list_invites()[0]["expires_at"])


if __name__ == "__main__":
    unittest.main()
