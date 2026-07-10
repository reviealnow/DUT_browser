from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.api import workspace_api
from app.db import workspace
from app.services import tag_service


class WorkspaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        workspace.init_db()

    def tearDown(self) -> None:
        self._stack.close()

    def test_tags_endpoint_lists_names_with_counts(self) -> None:
        file_id = workspace.execute(
            "INSERT INTO files (filename, filepath, size, uploader) VALUES (?, ?, ?, ?)",
            ("a.log", "/x/a.log", 1, None),
        )
        tag_service.set_file_tags(file_id, ["alpha"])

        tags = workspace_api.list_tags()["tags"]
        self.assertEqual(tags, [{"name": "alpha", "file_count": 1, "post_count": 0}])

    def test_search_endpoint_shape_and_acceptance_case(self) -> None:
        # Roadmap acceptance: file tagged "usage_insight" + post tagged "UI"
        # must both come back for the query "ui".
        file_id = workspace.execute(
            "INSERT INTO files (filename, filepath, size, uploader) VALUES (?, ?, ?, ?)",
            ("usage.log", "/x/usage.log", 1, None),
        )
        post_id = workspace.execute(
            "INSERT INTO bulletin_posts (title, body, author) VALUES (?, ?, ?)",
            ("UI thoughts", "b", None),
        )
        tag_service.set_file_tags(file_id, ["usage_insight"])
        tag_service.set_post_tags(post_id, ["UI"])

        result = workspace_api.search(q="ui")
        self.assertEqual(
            set(result.keys()), {"query", "matched_tags", "files", "posts"}
        )
        self.assertEqual(result["query"], "ui")
        self.assertEqual([f["id"] for f in result["files"]], [file_id])
        self.assertEqual([p["id"] for p in result["posts"]], [post_id])

    def test_search_blank_query_returns_empty_lists(self) -> None:
        result = workspace_api.search(q="   ")
        self.assertEqual(result["matched_tags"], [])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["posts"], [])


if __name__ == "__main__":
    unittest.main()
