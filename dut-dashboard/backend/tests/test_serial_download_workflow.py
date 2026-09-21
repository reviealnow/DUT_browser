from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
import subprocess
from unittest.mock import patch

from fastapi import HTTPException

from app.api import serial_api


class DownloadLogWorkflowTests(unittest.TestCase):
    VALID_LOG_CONTENT = (
        "= Test Time: 1, 2026-03-23 10:00:00\n"
        "CPU0: 1.0% usr 2.0% sys 0.0% nic 90.0% idle 0.0% io 0.0% irq 7.0%% sirq\n"
        "MemAvailable: 1000 kB\n"
        "Slab: 200 kB\n"
        "SUnreclaim: 100 kB\n"
        "= Test Time: 2, 2026-03-23 10:00:01\n"
        "CPU0: 2.0% usr 2.0% sys 0.0% nic 89.0% idle 0.0% io 0.0% irq 7.0%% sirq\n"
        "MemAvailable: 990 kB\n"
        "Slab: 210 kB\n"
        "SUnreclaim: 110 kB\n"
    )
    LONG_VALID_LOG_CONTENT = VALID_LOG_CONTENT + "".join(f"filler line {i}\n" for i in range(1, 121))

    def _write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_download_log_returns_zip_with_log_and_analysis_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            log_dir = base / "logs"
            analyzer_script = base / "tools" / "analyzer3.py"

            self._write_file(log_dir / "dut.log", self.LONG_VALID_LOG_CONTENT)
            self._write_file(analyzer_script, "print('stub analyzer')\n")

            def mock_run(*_args, **kwargs):
                cwd = Path(kwargs["cwd"])
                (cwd / "cpu_usage.csv").write_text("cpu,data\n0,1\n", encoding="utf-8")
                (cwd / "memory.csv").write_text("mem,data\n0,2\n", encoding="utf-8")
                (cwd / "cpu_spike_report.txt").write_text("ok\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=[str(serial_api.sys.executable), str(analyzer_script)],
                    returncode=0,
                    stdout="ok",
                    stderr="",
                )

            with (
                patch.object(serial_api, "LOG_DIR", log_dir),
                patch.object(serial_api, "ANALYZER_SCRIPT", analyzer_script),
                patch.object(serial_api.subprocess, "run", side_effect=mock_run),
            ):
                response = serial_api.download_log("dut.log")

            self.assertEqual(response.media_type, "application/zip")
            self.assertTrue(response.filename.endswith(".zip"))

            zip_path = Path(response.path)
            self.assertTrue(zip_path.exists(), f"zip not found: {zip_path}")

            # The zip is the only thing kept. The bundle it was made from is a
            # second copy of the log plus the analyzer output -- 42 MB on a
            # 39 MB session -- and nothing ever reads it back, so it is
            # assembled in scratch and gone by the time the response exists.
            self.assertEqual(zip_path.parent, log_dir)
            session_dir = log_dir / zip_path.stem
            self.assertFalse(
                session_dir.exists(), f"bundle directory left behind: {session_dir}"
            )
            # Scoped to bundle directories: .mplconfig is the analyzer's own
            # matplotlib cache, deliberately kept and reused across runs.
            leftover = sorted(
                entry.name
                for entry in log_dir.iterdir()
                if entry.is_dir() and entry.name.startswith("dut-session-")
            )
            self.assertEqual(leftover, [], f"bundle directories left in LOG_DIR: {leftover}")

            with zipfile.ZipFile(zip_path, "r") as zf:
                names = set(zf.namelist())

            expected_prefix = f"{zip_path.stem}/"
            self.assertIn(expected_prefix + "dut.log", names)
            self.assertIn(expected_prefix + "cpu_usage.csv", names)
            self.assertIn(expected_prefix + "memory.csv", names)
            self.assertIn(expected_prefix + "cpu_spike_report.txt", names)

    def test_download_log_too_short_returns_422(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            log_dir = base / "logs"
            analyzer_script = base / "tools" / "analyzer3.py"

            # No marker at all. One used to be short enough to refuse; the
            # threshold matches analyzer3 now, so one is analysable and only a
            # log with none is not. TOP keeps should_bypass_analyzer off it.
            self._write_file(log_dir / "dut.log", "TOP\nconsole output, no telemetry\n")
            self._write_file(analyzer_script, "print('stub analyzer')\n")

            with (
                patch.object(serial_api, "LOG_DIR", log_dir),
                patch.object(serial_api, "ANALYZER_SCRIPT", analyzer_script),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    serial_api.download_log("dut.log")

            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("no sysMon snapshots in this log", str(ctx.exception.detail))

    def test_download_log_short_without_top_bypasses_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            log_dir = base / "logs"
            analyzer_script = base / "tools" / "analyzer3.py"
            log_path = log_dir / "dut.log"

            self._write_file(log_path, "line1\nline2\nline3\n")
            self._write_file(analyzer_script, "print('stub analyzer')\n")

            with (
                patch.object(serial_api, "LOG_DIR", log_dir),
                patch.object(serial_api, "ANALYZER_SCRIPT", analyzer_script),
                patch.object(serial_api.subprocess, "run") as mocked_run,
            ):
                response = serial_api.download_log("dut.log")

            self.assertEqual(response.media_type, "text/plain")
            self.assertEqual(Path(response.path), log_path)
            self.assertEqual(response.filename, "dut.log")
            mocked_run.assert_not_called()

    def test_download_log_invalid_file_name_returns_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            serial_api.download_log("../bad.log")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("invalid file name", str(ctx.exception.detail).lower())

    def test_download_log_analyzer_failure_returns_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            log_dir = base / "logs"
            analyzer_script = base / "tools" / "analyzer3.py"

            self._write_file(log_dir / "dut.log", self.LONG_VALID_LOG_CONTENT)
            self._write_file(analyzer_script, "print('stub analyzer')\n")

            def mock_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=["python", str(analyzer_script)],
                    returncode=2,
                    stdout="",
                    stderr="simulated analyzer failure",
                )

            with (
                patch.object(serial_api, "LOG_DIR", log_dir),
                patch.object(serial_api, "ANALYZER_SCRIPT", analyzer_script),
                patch.object(serial_api.subprocess, "run", side_effect=mock_run),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    serial_api.download_log("dut.log")

            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn("analyzer3.py execution failed", str(ctx.exception.detail))

    def test_download_log_matplotlib_font_cache_error_maps_to_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            log_dir = base / "logs"
            analyzer_script = base / "tools" / "analyzer3.py"

            self._write_file(log_dir / "dut.log", self.LONG_VALID_LOG_CONTENT)
            self._write_file(analyzer_script, "print('stub analyzer')\n")

            def mock_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=[str(serial_api.sys.executable), str(analyzer_script)],
                    returncode=1,
                    stdout="",
                    stderr="Matplotlib is building the font cache; this may take a moment.",
                )

            with (
                patch.object(serial_api, "LOG_DIR", log_dir),
                patch.object(serial_api, "ANALYZER_SCRIPT", analyzer_script),
                patch.object(serial_api.subprocess, "run", side_effect=mock_run),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    serial_api.download_log("dut.log")

            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn("matplotlib font cache", str(ctx.exception.detail))

    def test_session_name_is_reserved_against_the_zip_not_the_scratch_dir(self) -> None:
        """A name already taken by a zip must not be handed out again.

        The bundle directory used to be created in LOG_DIR beside its own zip,
        so mkdir refusing an existing name protected the archive too. It is
        assembled in scratch now -- empty on every call -- so the collision can
        only be caught by looking at the destination. Without that check a
        second Download in the same second overwrites the first bundle.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            log_dir = base / "logs"
            scratch = base / "scratch"
            log_dir.mkdir()
            scratch.mkdir()

            taken = f"dut-session-{serial_api.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            reserved_zip = log_dir / f"{taken}.zip"
            reserved_zip.write_bytes(b"an earlier bundle")

            session_dir = serial_api.create_dut_session_dir(root=scratch, zip_dir=log_dir)

            self.assertNotEqual(session_dir.name, taken)
            self.assertEqual(reserved_zip.read_bytes(), b"an earlier bundle")


if __name__ == "__main__":
    unittest.main()
