from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import bulletin_api
from app.api.bulletin_api import CommentCreate, PostCreate
from app.db import workspace


class BulletinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        workspace.init_db()

    def tearDown(self) -> None:
        self._stack.close()

    def test_create_post_and_nested_replies(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="Maintenance", body="Down at 6pm", author="nelson"))
        post_id = post["id"]

        top = bulletin_api.create_comment(post_id, CommentCreate(body="Noted", author="amy"))
        bulletin_api.create_comment(
            post_id, CommentCreate(body="Thanks", parent_comment_id=top["id"])
        )

        posts = bulletin_api.list_posts()["posts"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["title"], "Maintenance")
        self.assertEqual(posts[0]["author"], "nelson")
        self.assertEqual(len(posts[0]["comments"]), 1)
        self.assertEqual(posts[0]["comments"][0]["body"], "Noted")
        self.assertEqual(len(posts[0]["comments"][0]["replies"]), 1)
        self.assertEqual(posts[0]["comments"][0]["replies"][0]["body"], "Thanks")

    def test_blank_author_is_stored_as_null(self) -> None:
        bulletin_api.create_post(PostCreate(title="t", body="b", author="  "))
        self.assertIsNone(bulletin_api.list_posts()["posts"][0]["author"])

    def test_empty_title_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.create_post(PostCreate(title="   ", body="b"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_overlong_body_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.create_post(PostCreate(title="ok", body="x" * 1001))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_comment_on_missing_post_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.create_comment(999, CommentCreate(body="hi"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_reply_to_comment_from_other_post_is_rejected(self) -> None:
        p1 = bulletin_api.create_post(PostCreate(title="p1", body="b"))
        p2 = bulletin_api.create_post(PostCreate(title="p2", body="b"))
        c1 = bulletin_api.create_comment(p1["id"], CommentCreate(body="on p1"))
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.create_comment(
                p2["id"], CommentCreate(body="bad", parent_comment_id=c1["id"])
            )
        self.assertEqual(ctx.exception.status_code, 400)


    def test_delete_post_removes_it_from_list(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="temp", body="to be removed"))
        bulletin_api.delete_post(post["id"])
        self.assertEqual(bulletin_api.list_posts()["posts"], [])

    def test_delete_missing_post_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.delete_post(999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_post_cascades_comments(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="p", body="b"))
        post_id = post["id"]
        top = bulletin_api.create_comment(post_id, CommentCreate(body="top"))
        bulletin_api.create_comment(
            post_id, CommentCreate(body="reply", parent_comment_id=top["id"])
        )

        bulletin_api.delete_post(post_id)

        remaining = workspace.query_all(
            "SELECT id FROM bulletin_comments WHERE post_id = ?", (post_id,)
        )
        self.assertEqual(remaining, [])

    def test_pagination_pages_newest_first_and_reports_total(self) -> None:
        ids = [
            bulletin_api.create_post(PostCreate(title=f"note {i}", body="b"))["id"]
            for i in range(3)
        ]
        # Comments must ride along on a paginated page, not just the full list.
        bulletin_api.create_comment(ids[2], CommentCreate(body="on newest"))

        page = bulletin_api.list_posts(limit=2, offset=0)
        self.assertEqual([p["title"] for p in page["posts"]], ["note 2", "note 1"])
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["posts"][0]["comments"][0]["body"], "on newest")

        rest = bulletin_api.list_posts(limit=2, offset=2)
        self.assertEqual([p["title"] for p in rest["posts"]], ["note 0"])
        self.assertEqual(rest["total"], 3)

        # No limit keeps the legacy full-list behaviour.
        legacy = bulletin_api.list_posts()
        self.assertEqual(len(legacy["posts"]), 3)
        self.assertEqual(legacy["total"], 3)


if __name__ == "__main__":
    unittest.main()
