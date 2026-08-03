from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "wifi_timeseries.py"
FIXTURES = Path(__file__).parent / "fixtures" / "wifi_timeseries"


def run_tool(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MPLBACKEND"] = "Agg"
    env["MPLCONFIGDIR"] = str(tmp_path / ".mpl")
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )


def artifact(tmp_path: Path, suffix: str) -> Path:
    matches = list(tmp_path.glob(f"*{suffix}"))
    assert len(matches) == 1
    return matches[0]


def test_extracts_complete_cycles_and_exact_schemas(tmp_path: Path) -> None:
    shutil.copy2(next(FIXTURES.glob("*.log")), tmp_path)
    result = run_tool(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "snapshots 2" in result.stdout
    assert "client rows 3" in result.stdout
    assert "2.4G 2 / 5G 1 / 6G 0" in result.stdout
    assert "channel consistency 3/3" in result.stdout

    with artifact(tmp_path, "wifi_summary.csv").open(newline="") as stream:
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


def test_no_curl_section_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "empty.log").write_text("= Test Time: 1, 2026-08-03 00:00:00\n")
    result = run_tool(tmp_path)
    assert result.returncode == 0
    assert "no Wi-Fi output written" in result.stdout
    assert not list(tmp_path.glob("*.csv"))
    assert not list(tmp_path.glob("*.png"))


def test_cycle_without_clients_omits_client_artifacts(tmp_path: Path) -> None:
    (tmp_path / "summary.log").write_text(
        "= Test Time: 1, 2026-08-03 00:00:00\n"
        "=== CURL Hooks ===============\n"
        "{\"connected_clients\":0}\n"
        "=== Process Status ===========\n"
    )
    result = run_tool(tmp_path)
    assert result.returncode == 0
    artifact(tmp_path, "wifi_summary.csv")
    assert not list(tmp_path.glob("*wifi_clients.csv"))
    assert not list(tmp_path.glob("*wifi_rssi_plot.png"))


def test_radio_count_mismatch_retains_client_without_guessing(tmp_path: Path) -> None:
    (tmp_path / "mismatch.log").write_text(
        "= Test Time: 1, 2026-08-03 00:00:00\n"
        "=== CURL Hooks ===============\n"
        "{\"data\":{\"workload\":{\"connected_clients\":1}}}\n"
        "{\"data\":{\"radio_name\":\"Wireless 2.4G\",\"channel\":1}}\n"
        "{\"data\":{\"radio_name\":\"Wireless 5G\",\"channel\":36}}\n"
        "{\"data\":{\"client_list\":[{\"mac_address\":\"02:00:00:00:00:04\",\"chan\":1}]}}\n"
        "=== Process Status ===========\n"
    )
    result = run_tool(tmp_path)
    assert result.returncode == 0
    with artifact(tmp_path, "wifi_clients.csv").open(newline="") as stream:
        clients = list(csv.DictReader(stream))
    assert len(clients) == 1
    assert clients[0]["radio"] == ""
