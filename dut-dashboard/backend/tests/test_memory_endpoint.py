from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import analyzer_api

HEADER = (
    "Timestamp,Timestamp_MMDD_HHMMSS,MemAvailable_kB,Slab_kB,SUnreclaim_kB,"
    "EffectiveAvailable_kB,Generated_At,Version,Output_Prefix\n"
)


class MemoryEndpointTests(unittest.TestCase):
    def test_missing_file_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(analyzer_api, "ANALYZER_OUTPUT_DIR", Path(d)):
                result = analyzer_api.get_memory()
        self.assertFalse(result["available"])
        self.assertEqual(result["points"], [])

    def test_parses_memory_csv(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "memory.csv").write_text(
                HEADER
                + "2026,0609_01,512000,40000,12000,500000,gen,v1,run_\n"
                + "2026,0609_02,508000,41000,12500,495500,gen,v1,run_\n",
                encoding="utf-8",
            )
            with patch.object(analyzer_api, "ANALYZER_OUTPUT_DIR", Path(d)):
                result = analyzer_api.get_memory()
        self.assertTrue(result["available"])
        self.assertEqual(len(result["points"]), 2)
        self.assertEqual(result["points"][0]["effectiveKb"], 500000)
        self.assertEqual(result["points"][1]["memAvailableKb"], 508000)
        self.assertEqual(result["generated_at"], "gen")
        self.assertEqual(result["version"], "v1")


if __name__ == "__main__":
    unittest.main()
