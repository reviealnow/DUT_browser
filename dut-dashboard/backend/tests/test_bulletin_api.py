from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import bulletin_api
from app.api.bulletin_api import CommentCreate, CommentUpdate, PostCreate, PostUpdate
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

    def test_post_tags_create_update_and_retag(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="t", body="b", tags=["UI", "perf"]))
        listed = bulletin_api.list_posts()["posts"][0]
        self.assertEqual(listed["tags"], ["perf", "UI"])

        # Update with tags=None leaves them unchanged.
        bulletin_api.update_post(post["id"], PostUpdate(title="t2", body="b2"))
        self.assertEqual(bulletin_api.list_posts()["posts"][0]["tags"], ["perf", "UI"])

        # Update with an explicit list replaces them.
        bulletin_api.update_post(post["id"], PostUpdate(title="t3", body="b3", tags=["UI"]))
        self.assertEqual(bulletin_api.list_posts()["posts"][0]["tags"], ["UI"])

        updated = bulletin_api.set_post_tags(post["id"], bulletin_api.TagsUpdate(tags=[]))
        self.assertEqual(updated["tags"], [])
        self.assertEqual(bulletin_api.list_posts()["posts"][0]["tags"], [])

    def test_retag_missing_post_is_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.set_post_tags(999, bulletin_api.TagsUpdate(tags=["a"]))
        self.assertEqual(ctx.exception.status_code, 404)

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

    def test_update_post_persists_and_sets_edited_at(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="old title", body="old body"))
        self.assertIsNone(bulletin_api.list_posts()["posts"][0]["edited_at"])

        bulletin_api.update_post(post["id"], PostUpdate(title="new title", body="new body"))

        updated = bulletin_api.list_posts()["posts"][0]
        self.assertEqual(updated["title"], "new title")
        self.assertEqual(updated["body"], "new body")
        self.assertIsNotNone(updated["edited_at"])

    def test_update_post_keeps_author_and_created_at(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="t", body="b", author="nelson"))
        before = bulletin_api.list_posts()["posts"][0]

        bulletin_api.update_post(post["id"], PostUpdate(title="t2", body="b2"))

        after = bulletin_api.list_posts()["posts"][0]
        self.assertEqual(after["author"], "nelson")
        self.assertEqual(after["created_at"], before["created_at"])

    def test_update_missing_post_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.update_post(999, PostUpdate(title="t", body="b"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_update_post_validation_still_applies(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="t", body="b"))
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.update_post(post["id"], PostUpdate(title="   ", body="b"))
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.update_post(post["id"], PostUpdate(title="t", body="x" * 1001))
        self.assertEqual(ctx.exception.status_code, 400)
        # The failed updates must not have touched the stored post.
        kept = bulletin_api.list_posts()["posts"][0]
        self.assertEqual((kept["title"], kept["body"]), ("t", "b"))
        self.assertIsNone(kept["edited_at"])

    def test_update_comment_and_nested_reply(self) -> None:
        post = bulletin_api.create_post(PostCreate(title="p", body="b"))
        top = bulletin_api.create_comment(post["id"], CommentCreate(body="top"))
        reply = bulletin_api.create_comment(
            post["id"], CommentCreate(body="reply", parent_comment_id=top["id"])
        )

        bulletin_api.update_comment(top["id"], CommentUpdate(body="top edited"))
        bulletin_api.update_comment(reply["id"], CommentUpdate(body="reply edited"))

        comment = bulletin_api.list_posts()["posts"][0]["comments"][0]
        self.assertEqual(comment["body"], "top edited")
        self.assertIsNotNone(comment["edited_at"])
        self.assertEqual(comment["replies"][0]["body"], "reply edited")
        self.assertIsNotNone(comment["replies"][0]["edited_at"])

    def test_update_missing_comment_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bulletin_api.update_comment(999, CommentUpdate(body="x"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_init_db_adds_edited_at_to_pre_p65_database(self) -> None:
        # Rebuild the tables without edited_at to simulate a database created
        # before this migration, then re-run init_db against it.
        with workspace.connect() as conn:
            conn.execute("DROP TABLE bulletin_comments")
            conn.execute("DROP TABLE bulletin_posts")
            conn.execute(
                "CREATE TABLE bulletin_posts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " title TEXT NOT NULL, body TEXT NOT NULL, author TEXT,"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "CREATE TABLE bulletin_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " post_id INTEGER NOT NULL, parent_comment_id INTEGER,"
                " body TEXT NOT NULL, author TEXT,"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                " FOREIGN KEY (post_id) REFERENCES bulletin_posts(id) ON DELETE CASCADE)"
            )
            conn.execute("INSERT INTO bulletin_posts (title, body) VALUES ('legacy', 'row')")
            conn.commit()

        workspace.init_db()

        post = bulletin_api.list_posts()["posts"][0]
        self.assertEqual(post["title"], "legacy")
        self.assertIsNone(post["edited_at"])
        bulletin_api.update_post(post["id"], PostUpdate(title="legacy", body="edited"))
        self.assertIsNotNone(bulletin_api.list_posts()["posts"][0]["edited_at"])

    def test_search_matches_title_or_body_and_total_follows(self) -> None:
        bulletin_api.create_post(PostCreate(title="Router down", body="lab A"))
        bulletin_api.create_post(PostCreate(title="Lunch", body="the router place"))
        bulletin_api.create_post(PostCreate(title="Unrelated", body="nothing"))

        hit = bulletin_api.list_posts(q="ROUTER")  # case-insensitive
        self.assertEqual(
            sorted(p["title"] for p in hit["posts"]),
            ["Lunch", "Router down"],
        )
        self.assertEqual(hit["total"], 2)

        # q combines with pagination: total is the match count, page is capped.
        page = bulletin_api.list_posts(limit=1, offset=0, q="router")
        self.assertEqual(len(page["posts"]), 1)
        self.assertEqual(page["total"], 2)

        # LIKE wildcards in the query match literally, not as wildcards.
        self.assertEqual(bulletin_api.list_posts(q="rout%r")["total"], 0)

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
