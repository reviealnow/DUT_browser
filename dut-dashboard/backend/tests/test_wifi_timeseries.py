from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from app.api import serial_api
from app.services import analyzer_service


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "wifi_timeseries.py"
FIXTURES = Path(__file__).parent / "fixtures" / "wifi_timeseries"


@pytest.fixture(scope="session")
def mpl_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("wifi-timeseries-mpl")


def run_tool(tmp_path: Path, mpl_config_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MPLBACKEND"] = "Agg"
    env["MPLCONFIGDIR"] = str(mpl_config_dir)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )


def artifact(tmp_path: Path, suffix: str) -> Path:
    matches = list(tmp_path.glob(f"*{suffix}"))
    assert len(matches) == 1
    return matches[0]


def test_extracts_complete_cycles_and_exact_schemas(tmp_path: Path, mpl_config_dir: Path) -> None:
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path)
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert "snapshots 2" in result.stdout
    assert "client rows 3" in result.stdout
    assert "2.4G 2 / 5G 1 / 6G 0" in result.stdout
    assert "channel consistency 3/3" in result.stdout

    summary_path = artifact(tmp_path, "wifi_summary.csv")
    assert re.fullmatch(r"\d{8}_101900_19300_wifi_summary\.csv", summary_path.name)

    with summary_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert list(rows[0]) == [
        "ts", "cycle", "connected_clients", "cpu_load_pct", "mem_load_pct",
        "cpu_temp_c", "radio_temp_c", "util_2g4_pct", "util_5g_pct", "util_6g_pct",
        "chan_2g4", "chan_5g", "chan_6g", "tx_bytes", "rx_bytes",
    ]
    assert len(rows) == 2
    assert rows[0]["tx_bytes"] == str(int(20.3 * 1024**3))
    assert rows[0]["cpu_temp_c"] == "64"

    with artifact(tmp_path, "wifi_clients.csv").open(newline="") as stream:
        clients = list(csv.DictReader(stream))
    assert list(clients[0]) == [
        "ts", "cycle", "radio", "ssid_name", "mac_address", "vendor", "ip_address",
        "chan", "chan_width_mhz", "rssi_dbm", "snr_db", "signal_ratio_pct",
        "tx_bytes", "rx_bytes", "connected_secs", "idle_secs", "vlan_id",
    ]
    assert [row["radio"] for row in clients] == ["2.4G", "5G", "2.4G"]
    assert clients[0]["connected_secs"] == str(86400 + 7200 + 180 + 4)
    assert clients[0]["chan_width_mhz"] == "20"
    assert clients[0]["signal_ratio_pct"] == "78"
    assert len(list(tmp_path.glob("*wifi_*plot.png"))) == 4


def test_no_curl_section_writes_nothing(tmp_path: Path, mpl_config_dir: Path) -> None:
    (tmp_path / "empty.log").write_text("= Test Time: 1, 2026-08-03 00:00:00\n")
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0
    assert "no Wi-Fi output written" in result.stdout
    assert not list(tmp_path.glob("*.csv"))
    assert not list(tmp_path.glob("*.png"))


def test_cycle_without_recognizable_values_omits_empty_plots(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    (tmp_path / "summary.log").write_text(
        "= Test Time: 1, 2026-08-03 00:00:00\n"
        "=== CURL Hooks ===============\n"
        "{\"connected_clients\":0}\n"
        "=== Process Status ===========\n"
    )
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0
    artifact(tmp_path, "wifi_summary.csv")
    assert not list(tmp_path.glob("*wifi_clients.csv"))
    assert not list(tmp_path.glob("*.png"))


def test_radio_count_mismatch_retains_client_without_guessing(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    (tmp_path / "mismatch.log").write_text(
        "= Test Time: 1, 2026-08-03 00:00:00\n"
        "=== CURL Hooks ===============\n"
        "{\"data\":{\"workload\":{\"connected_clients\":1}}}\n"
        "{\"data\":{\"radio_name\":\"Wireless 2.4G\",\"channel\":1}}\n"
        "{\"data\":{\"radio_name\":\"Wireless 5G\",\"channel\":36}}\n"
        "{\"data\":{\"client_list\":[{\"mac_address\":\"02:00:00:00:00:04\",\"chan\":1}]}}\n"
        "=== Process Status ===========\n"
    )
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0
    with artifact(tmp_path, "wifi_clients.csv").open(newline="") as stream:
        clients = list(csv.DictReader(stream))
    assert len(clients) == 1
    assert clients[0]["radio"] == ""


def test_malformed_timestamp_is_kept_in_csv_and_skipped_by_plots(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    (tmp_path / "malformed.log").write_text(
        "= Test Time: 1, 0000-00-00 00:00:00\n"
        "=== CURL Hooks ===============\n"
        "{\"data\":{\"workload\":{\"connected_clients\":1}}}\n"
        "=== Process Status ===========\n"
    )
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    with artifact(tmp_path, "wifi_summary.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["ts"] == "0000-00-00 00:00:00"
    assert not list(tmp_path.glob("*.png"))


def test_optional_tools_are_best_effort_at_both_invocation_points(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    analyzer = tools_dir / "analyzer3.py"
    wifi = tools_dir / "wifi_timeseries.py"
    analyzer.write_text("# analyzer\n")
    wifi.write_text("# wifi\n")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log = session_dir / "dut.log"
    log.write_text("log\n")
    output_dir = tmp_path / "outputs"
    log_dir = tmp_path / "logs"

    def fake_run(command, **kwargs):
        tool_name = Path(command[1]).name
        cwd = Path(kwargs["cwd"])
        if tool_name == "analyzer3.py":
            (cwd / "cpu_usage.csv").write_text("cpu\n")
            (cwd / "memory.csv").write_text("mem\n")
            return subprocess.CompletedProcess(command, 0, "analyzer ok\n", "")
        return subprocess.CompletedProcess(command, 2, "", "optional failed")

    with (
        mock.patch.object(serial_api, "ANALYZER_SCRIPT", analyzer),
        mock.patch.object(serial_api, "LOG_DIR", log_dir),
        mock.patch.object(serial_api.subprocess, "run", side_effect=fake_run) as serial_run,
    ):
        serial_api.run_analyzer_for_session(session_dir)
    assert [Path(call.args[0][1]).name for call in serial_run.call_args_list] == [
        "analyzer3.py", "wifi_timeseries.py"
    ]

    with (
        mock.patch.object(analyzer_service, "ANALYZER_SCRIPT", analyzer),
        mock.patch.object(analyzer_service, "ANALYZER_OUTPUT_DIR", output_dir),
        mock.patch.object(analyzer_service, "LOG_DIR", log_dir),
        mock.patch.object(analyzer_service, "_bundle_context", return_value={"dir": None, "files": []}),
        mock.patch.object(analyzer_service.subprocess, "run", side_effect=fake_run),
    ):
        result = analyzer_service.AnalyzerService().run(str(log))
    assert result["ok"] is True


def test_analyzer3s_stamp_is_adopted_so_one_bundle_has_one_prefix(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    """analyzer3 runs first and owns the stamp for the whole bundle.

    Each tool reading its own clock gave a run that crossed a minute boundary
    two different prefixes (seen on a real download: 08041541_ for analyzer3,
    08041542_ for this tool).
    """
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path)
    (tmp_path / "08031129_101900_19300_cpu_usage.csv").write_text("x", encoding="utf-8")

    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr

    for suffix in ("wifi_summary.csv", "wifi_clients.csv"):
        assert artifact(tmp_path, suffix).name.startswith("08031129_101900_19300_")


def test_the_newest_analyzer3_output_wins(tmp_path: Path, mpl_config_dir: Path) -> None:
    """A directory still holding older analyzer3 output must not pin a stale stamp."""
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path)
    stale = tmp_path / "08010900_101900_19300_cpu_usage.csv"
    fresh = tmp_path / "08031129_101900_19300_cpu_usage.csv"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    os.utime(stale, (1_000_000, 1_000_000))

    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert artifact(tmp_path, "wifi_summary.csv").name.startswith("08031129_")


def test_without_analyzer3_output_the_tool_uses_its_own_clock(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path)
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(
        r"\d{8}_101900_19300_wifi_summary\.csv", artifact(tmp_path, "wifi_summary.csv").name
    )


def test_a_mixed_case_log_is_ignored_exactly_as_analyzer3_ignores_it(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    """Log selection must match analyzer3's, case included.

    Selecting case-insensitively made this tool read a log analyzer3 skipped,
    so the two collapsed different log sets into different tags (`MULTI` here
    against analyzer3's concrete pair) — the prefixes split again even with a
    shared stamp, and the CSV described a data set its own name denied.
    """
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path / "a_101900_v1.9.300.log")
    (tmp_path / "b_222222_v2.0.2.LOG").write_text("not a log analyzer3 would read\n", encoding="utf-8")

    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(
        r"\d{8}_101900_19300_wifi_summary\.csv", artifact(tmp_path, "wifi_summary.csv").name
    )


def test_the_whole_prefix_is_adopted_not_only_the_stamp(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    """Tags come from the anchor too, so no per-tool tag logic can split them."""
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path / "a_101900_v1.9.300.log")
    (tmp_path / "08010900_111111_19001_cpu_usage.csv").write_text("x", encoding="utf-8")

    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert artifact(tmp_path, "wifi_summary.csv").name == "08010900_111111_19001_wifi_summary.csv"


def test_a_file_analyzer3_could_not_have_written_is_not_authoritative(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    """Suffix-matching alone must not let a foreign file name the bundle."""
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path / "a_101900_v1.9.300.log")
    (tmp_path / "12345678_NOT-ANALYZER_PREFIX_cpu_usage.csv").write_text("x", encoding="utf-8")
    (tmp_path / "cpu_usage.csv").write_text("x", encoding="utf-8")

    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(
        r"\d{8}_101900_19300_wifi_summary\.csv", artifact(tmp_path, "wifi_summary.csv").name
    )


def test_a_malformed_newest_anchor_does_not_shadow_a_real_one(
    tmp_path: Path, mpl_config_dir: Path
) -> None:
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path / "a_101900_v1.9.300.log")
    real = tmp_path / "08031129_101900_19300_cpu_usage.csv"
    junk = tmp_path / "12345678_NOT-ANALYZER_PREFIX_cpu_usage.csv"
    real.write_text("x", encoding="utf-8")
    junk.write_text("x", encoding="utf-8")
    os.utime(real, (1_000_000, 1_000_000))          # junk is newer
    result = run_tool(tmp_path, mpl_config_dir)
    assert result.returncode == 0, result.stderr
    assert artifact(tmp_path, "wifi_summary.csv").name == "08031129_101900_19300_wifi_summary.csv"
