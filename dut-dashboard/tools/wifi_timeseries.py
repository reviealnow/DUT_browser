#!/usr/bin/env python3
"""Extract Wi-Fi time-series data from sysMon CURL-hook JSON in session logs."""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
import pandas as pd


LOG_EXT = (".log", ".txt")
SUMMARY_COLUMNS = [
    "ts", "cycle", "connected_clients", "cpu_load_pct", "mem_load_pct",
    "cpu_temp_c", "radio_temp_c", "util_2g4_pct", "util_5g_pct", "util_6g_pct",
    "chan_2g4", "chan_5g", "chan_6g", "tx_bytes", "rx_bytes",
]
CLIENT_COLUMNS = [
    "ts", "cycle", "radio", "ssid_name", "mac_address", "vendor", "ip_address",
    "chan", "chan_width_mhz", "rssi_dbm", "snr_db", "signal_ratio_pct",
    "tx_bytes", "rx_bytes", "connected_secs", "idle_secs", "vlan_id",
]
SECTION_RE = re.compile(r"^===\s+(.+?)\s*=+\s*$")
TEST_TIME_RE = re.compile(r"= Test Time:\s*(\d+),\s*([\d-]+\s+[\d:]+)")
CLIENT_MARKER_RE = re.compile(r"---\s+CLIENTS\s+Radio=(2G|5G|6G)\s+---", re.I)


def extract_time_tag(filename: str) -> str:
    match = re.search(r"_(\d{6})(?:_|\.|$)", filename)
    return match.group(1) if match else "notime"


def fw_triplet_to_tag(major: str, minor: str, patch: str) -> str:
    try:
        return f"{int(major)}{int(minor)}{int(patch):03d}"
    except Exception:
        return f"{major}{minor}{patch}"


def extract_fw_tag(filename: str) -> str:
    match = re.search(r"(?:^|[^0-9])v?(\d+)\.(\d+)\.(\d+)(?:[^0-9]|$)", filename, re.I)
    if match:
        return fw_triplet_to_tag(*match.groups())
    match = re.search(r"(?<!\d)(\d{5})(?!\d)", filename)
    return match.group(1) if match else "nofw"


def output_prefix(log_files: list[Path]) -> str:
    run_prefix = datetime.now().strftime("%m%d%H%M")
    time_tags = {extract_time_tag(path.name) for path in log_files}
    fw_tags = {extract_fw_tag(path.name) for path in log_files}
    time_tag = next(iter(time_tags)) if len(time_tags) == 1 else "MULTI"
    fw_tag = next(iter(fw_tags)) if len(fw_tags) == 1 else "MULTI"
    return f"{run_prefix}_{time_tag}_{fw_tag}_".replace("_nofw_", "_")


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower(), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _find(value: Any, *keys: str) -> Any:
    wanted = {key.lower() for key in keys}
    for key, child in _walk(value):
        if key in wanted and not isinstance(child, (dict, list)):
            return child
    return None


def _find_lists(value: Any, *keys: str) -> list[list[Any]]:
    wanted = {key.lower() for key in keys}
    return [child for key, child in _walk(value) if key in wanted and isinstance(child, list)]


def _find_dict(value: Any, key: str) -> dict[str, Any] | None:
    wanted = key.lower()
    for candidate, child in _walk(value):
        if candidate == wanted and isinstance(child, dict):
            return child
    return None


def _numbers(value: Any) -> list[int]:
    if isinstance(value, list):
        return [number for child in value for number in _numbers(child)]
    if isinstance(value, dict):
        return [number for child in value.values() for number in _numbers(child)]
    number = _integer(value)
    return [] if number is None else [number]


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return int(float(match.group())) if match else None


def _bytes(value: Any) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([KMGTPE]?)\s*(?:i?Bytes?|B)?", text, re.I)
    if not match:
        return None
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4,
                  "P": 1024**5, "E": 1024**6}[match.group(2).upper()]
    return int(float(match.group(1)) * multiplier)


def _duration(value: Any) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).lower()
    if text.strip().isdigit():
        return int(text)
    total = 0
    found = False
    for pattern, multiplier in ((r"(\d+)\s*days?", 86400), (r"(\d+)\s*hours?", 3600),
                                (r"(\d+)\s*mins?", 60), (r"(\d+)\s*secs?", 1)):
        match = re.search(pattern, text)
        if match:
            total += int(match.group(1)) * multiplier
            found = True
    return total if found else None


def _band(value: Any) -> str | None:
    text = str(value or "").upper().replace("GHZ", "").replace(" ", "")
    if "2.4G" in text or text.endswith("2G"):
        return "2.4G"
    if "5G" in text:
        return "5G"
    if "6G" in text:
        return "6G"
    return None


def _radio_object(payload: Any) -> bool:
    return _find(payload, "radio_name") is not None


def _client_lists(payload: Any) -> list[list[Any]]:
    return _find_lists(payload, "client_list")


def _parse_cycles(log_files: list[Path]) -> list[dict[str, Any]]:
    cycles: list[dict[str, Any]] = []
    current_cycle: int | None = None
    current_ts: str | None = None
    for path in log_files:
        in_curl = False
        payloads: list[tuple[dict[str, Any], str | None]] = []
        marker: str | None = None
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            for raw in stream:
                line = raw.rstrip("\r\n")
                time_match = TEST_TIME_RE.search(line)
                if time_match and not in_curl:
                    current_cycle, current_ts = int(time_match.group(1)), time_match.group(2)
                section = SECTION_RE.match(line.strip())
                if section:
                    name = section.group(1).strip().lower()
                    if in_curl:
                        cycles.append({"cycle": current_cycle, "ts": current_ts, "payloads": payloads})
                        payloads, marker = [], None
                    in_curl = name == "curl hooks"
                    continue
                if not in_curl:
                    continue
                marker_match = CLIENT_MARKER_RE.search(line)
                if marker_match:
                    marker = _band(marker_match.group(1))
                    continue
                try:
                    decoded = json.loads(line.strip())
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(decoded, dict):
                    payloads.append((decoded, marker))
        # Deliberately do not flush here: a CURL section truncated by EOF is invalid.
    return [cycle for cycle in cycles if cycle["cycle"] is not None and cycle["ts"] and cycle["payloads"]]


def _summary_row(cycle: dict[str, Any]) -> dict[str, Any]:
    payloads = [payload for payload, _ in cycle["payloads"]]
    row = {column: None for column in SUMMARY_COLUMNS}
    row.update(ts=cycle["ts"], cycle=cycle["cycle"])
    for payload in payloads:
        workload = _find_dict(payload, "workload")
        if workload:
            for column, keys, converter in (
                ("connected_clients", ("connected_clients", "connected_client"), _integer),
                ("cpu_load_pct", ("cpu_load", "cpu_usage"), _integer),
                ("mem_load_pct", ("memory_load", "mem_load", "memory_usage"), _integer),
                ("tx_bytes", ("tx_bytes", "total_tx_bytes", "tx"), _bytes),
                ("rx_bytes", ("rx_bytes", "total_rx_bytes", "rx"), _bytes),
            ):
                value = converter(_find(workload, *keys))
                if value is not None and row[column] is None:
                    row[column] = value
        cpu_temps = [number for key, value in _walk(payload) if "cpu" in key and "temp" in key for number in _numbers(value)]
        radio_temps = [number for key, value in _walk(payload) if "radio" in key and "temp" in key for number in _numbers(value)]
        temperatures = _find_dict(payload, "temperatures")
        if temperatures:
            cpu_temps.extend(_numbers(temperatures.get("cpu")))
            radio_temps.extend(_numbers(temperatures.get("radio")))
        if cpu_temps:
            row["cpu_temp_c"] = max([row["cpu_temp_c"] or -999, *cpu_temps])
        if radio_temps:
            row["radio_temp_c"] = max([row["radio_temp_c"] or -999, *radio_temps])
        band = _band(_find(payload, "radio_name", "radio"))
        if band:
            suffix = {"2.4G": "2g4", "5G": "5g", "6G": "6g"}[band]
            row[f"util_{suffix}_pct"] = _integer(_find(payload, "channel_utilization", "channel_util", "utilization"))
            row[f"chan_{suffix}"] = _integer(_find(payload, "channel", "chan"))
    return row


def _client_rows(cycle: dict[str, Any]) -> list[dict[str, Any]]:
    radio_order = [_band(_find(payload, "radio_name")) for payload, _ in cycle["payloads"] if _radio_object(payload)]
    client_groups: list[tuple[list[Any], str | None]] = []
    for payload, marker in cycle["payloads"]:
        for clients in _client_lists(payload):
            client_groups.append((clients, marker))
    fallback = radio_order if len(radio_order) == len(client_groups) else [None] * len(client_groups)
    rows: list[dict[str, Any]] = []
    for index, (clients, marker) in enumerate(client_groups):
        radio = marker or fallback[index]
        for client in clients:
            if not isinstance(client, dict):
                continue
            row = {column: None for column in CLIENT_COLUMNS}
            row.update(
                ts=cycle["ts"], cycle=cycle["cycle"], radio=radio,
                ssid_name=_find(client, "ssid_name", "ssid"),
                mac_address=_find(client, "mac_address", "mac"),
                vendor=_find(client, "vendor"), ip_address=_find(client, "ip_address", "ip"),
                chan=_integer(_find(client, "channel", "chan")),
                chan_width_mhz=_integer(_find(client, "channel_width", "chan_width", "bandwidth")),
                rssi_dbm=_integer(_find(client, "rssi")), snr_db=_integer(_find(client, "snr")),
                signal_ratio_pct=_integer(_find(client, "signale_ratio")),
                tx_bytes=_bytes(_find(client, "tx_bytes", "tx")),
                rx_bytes=_bytes(_find(client, "rx_bytes", "rx")),
                connected_secs=_duration(_find(client, "connected_time", "connected", "connection_time")),
                idle_secs=_duration(_find(client, "idle_time", "idle")),
                vlan_id=_integer(_find(client, "vlan_id", "vlan")),
            )
            rows.append(row)
    return rows


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: "" if value is None else value for key, value in row.items()} for row in rows)


def _plot(path: Path, title: str, ylabel: str, frames: list[tuple[pd.DataFrame, str, str]]) -> None:
    fig, axis = plt.subplots(figsize=(12, 5))
    for frame, column, label in frames:
        if column in frame and frame[column].notna().any():
            axis.plot(pd.to_datetime(frame["ts"]), frame[column], label=label, linewidth=1)
    axis.set_title(title)
    axis.set_xlabel("DUT time")
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.3)
    if axis.lines:
        axis.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> int:
    log_files = sorted(path for path in Path.cwd().iterdir() if path.is_file() and path.suffix.lower() in LOG_EXT)
    if not log_files:
        print("No log files found; no Wi-Fi output written.")
        return 0
    cycles = _parse_cycles(log_files)
    if not cycles:
        print("No complete CURL Hooks sections found; no Wi-Fi output written.")
        return 0
    summaries = [_summary_row(cycle) for cycle in cycles]
    clients = [row for cycle in cycles for row in _client_rows(cycle)]
    prefix = output_prefix(log_files)
    summary_frame = pd.DataFrame(summaries, columns=SUMMARY_COLUMNS)
    _write_csv(Path(f"{prefix}wifi_summary.csv"), SUMMARY_COLUMNS, summaries)
    _plot(Path(f"{prefix}wifi_clients_plot.png"), "Connected clients", "clients", [(summary_frame, "connected_clients", "clients")])
    _plot(Path(f"{prefix}wifi_util_plot.png"), "Channel utilization", "%", [
        (summary_frame, "util_2g4_pct", "2.4G"), (summary_frame, "util_5g_pct", "5G"),
        (summary_frame, "util_6g_pct", "6G")])
    _plot(Path(f"{prefix}wifi_temps_plot.png"), "Temperatures", "°C", [
        (summary_frame, "cpu_temp_c", "CPU"), (summary_frame, "radio_temp_c", "radio")])
    if clients:
        _write_csv(Path(f"{prefix}wifi_clients.csv"), CLIENT_COLUMNS, clients)
        client_frame = pd.DataFrame(clients, columns=CLIENT_COLUMNS)
        _plot(Path(f"{prefix}wifi_rssi_plot.png"), "Client RSSI", "dBm", [(client_frame, "rssi_dbm", "RSSI")])
    by_radio = {band: sum(row["radio"] == band for row in clients) for band in ("2.4G", "5G", "6G")}
    consistent = sum(
        (row["radio"] == "2.4G" and row["chan"] is not None and row["chan"] <= 14)
        or (row["radio"] in {"5G", "6G"} and row["chan"] is not None and row["chan"] > 14)
        for row in clients
    )
    workload = sum(row["tx_bytes"] is not None or row["rx_bytes"] is not None for row in summaries)
    print(f"snapshots {len(summaries)} · with workload {workload} · client rows {len(clients)} · "
          f"2.4G {by_radio['2.4G']} / 5G {by_radio['5G']} / 6G {by_radio['6G']} · "
          f"channel consistency {consistent}/{len(clients)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
