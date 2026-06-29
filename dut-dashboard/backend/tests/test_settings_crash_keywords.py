from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import settings_api
from app.api.settings_api import CrashKeywordsBody
from app.db import workspace
from app.services import settings_service


class CrashKeywordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        db_path = self._dir / "workspace.db"
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", db_path))
        workspace.init_db()

    def tearDown(self) -> None:
        self._stack.close()

    def test_get_returns_defaults(self) -> None:
        result = settings_api.get_crash_keywords()
        self.assertIn("keywords", result)
        self.assertIn("kernel panic", result["keywords"])

    def test_put_persists_and_returns(self) -> None:
        result = settings_api.put_crash_keywords(CrashKeywordsBody(keywords=["oops", "kernel panic"]))
        self.assertEqual(result["keywords"], ["oops", "kernel panic"])

        result2 = settings_api.get_crash_keywords()
        self.assertEqual(result2["keywords"], ["oops", "kernel panic"])

    def test_put_strips_blank_keywords(self) -> None:
        result = settings_api.put_crash_keywords(CrashKeywordsBody(keywords=["  ", "watchdog", ""]))
        self.assertEqual(result["keywords"], ["watchdog"])

    def test_put_empty_list_disables(self) -> None:
        result = settings_api.put_crash_keywords(CrashKeywordsBody(keywords=[]))
        self.assertEqual(result["keywords"], [])

    def test_put_too_many_keywords_rejected(self) -> None:
        body = CrashKeywordsBody(keywords=[f"kw{i}" for i in range(101)])
        with self.assertRaises(HTTPException) as ctx:
            settings_api.put_crash_keywords(body)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_service_round_trip(self) -> None:
        settings_service.set_crash_keywords(["q6 crash", "watchdog"])
        result = settings_service.get_crash_keywords()
        self.assertEqual(result, ["q6 crash", "watchdog"])
