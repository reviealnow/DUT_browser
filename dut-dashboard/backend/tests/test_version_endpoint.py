from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app import main


class VersionEndpointTests(unittest.TestCase):
    def test_endpoint_shape(self) -> None:
        result = main.get_version()
        self.assertIn("version", result)
        self.assertIn("built_at", result)
        self.assertIsInstance(result["version"], str)
        self.assertTrue(result["version"])  # non-empty
        self.assertIsInstance(result["built_at"], str)
        self.assertTrue(result["built_at"])

    def test_env_override_wins(self) -> None:
        with patch.dict(os.environ, {"DUT_APP_VERSION": "phase-99"}):
            self.assertEqual(main._resolve_version(), "phase-99")

    def test_falls_back_to_dev_without_env_or_git(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DUT_APP_VERSION", None)
            with patch("app.main.subprocess.run", side_effect=Exception("no git")):
                self.assertEqual(main._resolve_version(), "dev")


if __name__ == "__main__":
    unittest.main()
