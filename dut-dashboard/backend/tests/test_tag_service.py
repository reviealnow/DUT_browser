from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.db import workspace
from app.services import tag_service


class NormalizeAndScoreTests(unittest.TestCase):
    def test_normalize(self) -> None:
        cases = [
            ("Usage_Insight", "usageinsight"),
            ("usage insight", "usageinsight"),
            ("usage-insight", "usageinsight"),
            ("UI", "ui"),
            ("  ", ""),
            ("__--", ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(tag_service.normalize(raw), expected)

    def test_match_score_tiers(self) -> None:
        n = tag_service.normalize
        cases = [
            # (query, tag, expected tier)
            ("UI", "Usageinsight", 1),        # subsequence (abbreviation)
            ("ui", "usage_insight", 1),       # roadmap acceptance case
            ("UI", "ui", 3),                  # equal after normalization
            ("usage", "usage_insight", 2),    # substring
            ("usage_insight", "usage", 2),    # substring, reversed lengths
            ("wifi", "usage_insight", 0),     # no match
            ("", "usage_insight", 0),         # empty query
            ("ui", "", 0),                    # empty tag
        ]
        for query, tag, expected in cases:
            with self.subTest(query=query, tag=tag):
                self.assertEqual(tag_service.match_score(n(query), n(tag)), expected)


class TagCrudTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        workspace.init_db()

    def tearDown(self) -> None:
        self._stack.close()

    def _insert_file(self, name: str = "a.log") -> int:
        return workspace.execute(
            "INSERT INTO files (filename, filepath, size, uploader) VALUES (?, ?, ?, ?)",
            (name, f"/x/{name}", 1, None),
        )

    def _insert_post(self, title: str = "t") -> int:
        return workspace.execute(
            "INSERT INTO bulletin_posts (title, body, author) VALUES (?, ?, ?)",
            (title, "b", None),
        )

    def test_get_or_create_dedupes_by_norm_name(self) -> None:
        first = tag_service.get_or_create(["Usage_Insight"])
        again = tag_service.get_or_create(["usage insight", "USAGE-INSIGHT"])
        self.assertEqual(again, [first[0]] )
        # First spelling wins as the display name.
        self.assertEqual([t["name"] for t in tag_service.list_tags()], ["Usage_Insight"])

    def test_get_or_create_skips_blank_and_overlong(self) -> None:
        ids = tag_service.get_or_create(["  ", "_-_", "x" * 41, "ok"])
        self.assertEqual(len(ids), 1)
        self.assertEqual([t["name"] for t in tag_service.list_tags()], ["ok"])

    def test_set_and_clear_tags(self) -> None:
        file_id = self._insert_file()
        tag_service.set_file_tags(file_id, ["beta", "alpha"])
        self.assertEqual(tag_service.tags_for_files([file_id])[file_id], ["alpha", "beta"])

        tag_service.set_file_tags(file_id, ["alpha"])
        self.assertEqual(tag_service.tags_for_files([file_id])[file_id], ["alpha"])

        tag_service.set_file_tags(file_id, [])
        self.assertEqual(tag_service.tags_for_files([file_id]), {})

    def test_list_tags_counts_files_and_posts(self) -> None:
        file_id = self._insert_file()
        post_id = self._insert_post()
        tag_service.set_file_tags(file_id, ["shared", "files-only"])
        tag_service.set_post_tags(post_id, ["shared"])

        tags = {t["name"]: t for t in tag_service.list_tags()}
        self.assertEqual(tags["shared"]["file_count"], 1)
        self.assertEqual(tags["shared"]["post_count"], 1)
        self.assertEqual(tags["files-only"]["file_count"], 1)
        self.assertEqual(tags["files-only"]["post_count"], 0)

    def test_delete_cascades_remove_link_rows(self) -> None:
        file_id = self._insert_file()
        post_id = self._insert_post()
        tag_service.set_file_tags(file_id, ["keepme"])
        tag_service.set_post_tags(post_id, ["keepme"])

        workspace.execute("DELETE FROM files WHERE id = ?", (file_id,))
        workspace.execute("DELETE FROM bulletin_posts WHERE id = ?", (post_id,))

        self.assertEqual(workspace.query_one("SELECT COUNT(*) AS n FROM file_tags")["n"], 0)
        self.assertEqual(workspace.query_one("SELECT COUNT(*) AS n FROM post_tags")["n"], 0)
        # The tag itself survives for reuse.
        self.assertEqual([t["name"] for t in tag_service.list_tags()], ["keepme"])

    def test_search_matches_fuzzy_and_decorates_rows(self) -> None:
        file_id = self._insert_file("usage.log")
        post_id = self._insert_post("UI thoughts")
        tag_service.set_file_tags(file_id, ["usage_insight"])
        tag_service.set_post_tags(post_id, ["UI"])

        result = tag_service.search("ui")
        # "UI" is an exact match (tier 3), "usage_insight" a subsequence (tier 1).
        self.assertEqual(
            [(t["name"], t["score"]) for t in result["matched_tags"]],
            [("UI", 3), ("usage_insight", 1)],
        )
        self.assertEqual([f["id"] for f in result["files"]], [file_id])
        self.assertEqual(result["files"][0]["tags"], ["usage_insight"])
        self.assertEqual([p["id"] for p in result["posts"]], [post_id])
        self.assertEqual(result["posts"][0]["tags"], ["UI"])

    def test_search_blank_query_is_empty(self) -> None:
        result = tag_service.search("")
        self.assertEqual(result["matched_tags"], [])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["posts"], [])


if __name__ == "__main__":
    unittest.main()
