from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app import main
from app.services.analyzer_service import _clear_analyzer_outputs


class ClearAnalyzerOutputsTests(unittest.TestCase):
    def test_removes_only_analyzer_file_types(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "06170000_cpu_usage_plot.png").write_bytes(b"x")
            (base / "memory.csv").write_text("a", encoding="utf-8")
            (base / "06170000_cpu_spike_report.txt").write_text("r", encoding="utf-8")
            (base / "snapshots.jsonl").write_text("{}", encoding="utf-8")  # must survive
            (base / ".DS_Store").write_bytes(b"x")  # must survive
            sub = base / "keep_dir"
            sub.mkdir()

            _clear_analyzer_outputs(base)

            remaining = sorted(p.name for p in base.iterdir())
            self.assertEqual(remaining, [".DS_Store", "keep_dir", "snapshots.jsonl"])

    def test_missing_dir_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _clear_analyzer_outputs(Path(d) / "does-not-exist")  # must not raise


class PreviewEndpointTests(unittest.TestCase):
    def test_rejects_non_png_and_traversal(self) -> None:
        for bad in ["memory.csv", "report.txt", "../etc/passwd.png", "a/b.png"]:
            with self.assertRaises(HTTPException) as ctx:
                main.preview_file(bad)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_png_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(main, "ANALYZER_OUTPUT_DIR", Path(d)):
                with self.assertRaises(HTTPException) as ctx:
                    main.preview_file("cpu_usage_plot.png")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_valid_png_served_inline(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "cpu_usage_plot.png").write_bytes(b"\x89PNG\r\n")
            with patch.object(main, "ANALYZER_OUTPUT_DIR", Path(d)):
                response = main.preview_file("cpu_usage_plot.png")
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.media_type, "image/png")


if __name__ == "__main__":
    unittest.main()
