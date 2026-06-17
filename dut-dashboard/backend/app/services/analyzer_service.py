from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import ANALYZER_OUTPUT_DIR, ANALYZER_SCRIPT, LOG_DIR


def _concise_error(stderr: str, stdout: str) -> str:
    """Surface a short, actionable reason from a failed analyzer run instead of
    dumping its whole multi-line stdout. analyzer3.py prints '[ERROR] ...' lines
    on failure (e.g. records=0 for a log with no sysmon snapshots)."""
    combined = f"{stderr}\n{stdout}"
    errors = [line.strip() for line in combined.splitlines() if "[ERROR]" in line]
    if errors:
        return " ".join(errors)
    return stderr.strip() or stdout.strip() or "analyzer3.py failed"


class AnalyzerService:
    def __init__(self) -> None:
        ANALYZER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def run(self, log_path: str) -> dict:
        log_file = Path(log_path)
        if not log_file.exists() or not log_file.is_file():
            raise FileNotFoundError(f"Log file not found: {log_path}")
        if not ANALYZER_SCRIPT.exists() or not ANALYZER_SCRIPT.is_file():
            raise FileNotFoundError(f"Analyzer script not found: {ANALYZER_SCRIPT}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staged_log = tmp_path / log_file.name
            shutil.copy2(log_file, staged_log)
            shutil.copy2(ANALYZER_SCRIPT, tmp_path / "analyzer3.py")

            # Force a headless matplotlib backend + a writable config dir so the
            # plot generation works on a server with no display and survives the
            # first-run font-cache build (mirrors run_analyzer_for_session).
            env = os.environ.copy()
            mpl_config_dir = LOG_DIR / ".mplconfig"
            mpl_config_dir.mkdir(parents=True, exist_ok=True)
            env["MPLCONFIGDIR"] = str(mpl_config_dir)
            env["MPLBACKEND"] = "Agg"

            completed = subprocess.run(
                [sys.executable, "analyzer3.py"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                env=env,
            )
            if completed.returncode != 0:
                raise RuntimeError(_concise_error(completed.stderr, completed.stdout))

            generated = [p for p in tmp_path.iterdir() if p.is_file()]
            cpu_candidates = sorted([p for p in generated if p.name.endswith("cpu_usage.csv")])
            mem_candidates = sorted([p for p in generated if p.name.endswith("memory.csv")])

            if not cpu_candidates or not mem_candidates:
                raise RuntimeError("Analyzer did not produce cpu_usage.csv and memory.csv outputs")

            cpu_src = cpu_candidates[-1]
            mem_src = mem_candidates[-1]

            cpu_dst = ANALYZER_OUTPUT_DIR / "cpu_usage.csv"
            mem_dst = ANALYZER_OUTPUT_DIR / "memory.csv"
            shutil.copy2(cpu_src, cpu_dst)
            shutil.copy2(mem_src, mem_dst)

            copied_files = {"cpu_usage.csv", "memory.csv"}
            for item in generated:
                if item.suffix.lower() in {".csv", ".png", ".txt"}:
                    dst = ANALYZER_OUTPUT_DIR / item.name
                    shutil.copy2(item, dst)
                    copied_files.add(item.name)

            return {
                "ok": True,
                "log_path": str(log_file),
                "files": sorted(copied_files),
                "stdout": completed.stdout,
            }
