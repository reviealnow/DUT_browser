from copy import deepcopy
import json
import re
import threading
from collections.abc import Callable


class SysMonParser:
    """Milestone 3 parser: console lines + snapshots + CPU + wifi clients."""

    SNAPSHOT_RE = re.compile(r"^= Test Time:\s*(\d+),\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*=*\s*$")
    CPU_RE = re.compile(
        r"^CPU(\d+):\s*([\d.]+)% usr\s+([\d.]+)% sys\s+([\d.]+)% nic\s+([\d.]+)% idle\s+([\d.]+)% io\s+([\d.]+)% irq\s+([\d.]+)%%?\s+sirq\s*$"
    )
    CLIENT_MARKER_RE = re.compile(r"^--- CLIENTS Radio=(2G|5G|6G) ---\s*$")
    # A /proc/meminfo line, e.g. "MemAvailable:     475472 kB". Streamed inside
    # the snapshot block, so it is parsed live like CPU per-core.
    MEMINFO_RE = re.compile(r"^(\w+):\s+(\d+)\s*kB\s*$")
    # Only the keys the dashboard charts need (mirrors tools/analyzer3.py).
    MEM_KEYS = frozenset(
        {"MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "Slab", "SReclaimable", "SUnreclaim"}
    )
    CONSOLE_BATCH_SIZE = 20
    CONSOLE_BATCH_MAX_LATENCY_SEC = 0.05

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self.on_event = on_event
        self._current_snapshot: dict | None = None
        self._last_emitted_snapshot: dict | None = None
        self._pending_clients_radio: str | None = None
        self._pending_console_lines: list[str] = []
        self._console_flush_timer: threading.Timer | None = None
        self._state_lock = threading.Lock()
        self._console_line_count = 0
        self._console_batch_count = 0
        self._snapshot_full_count = 0
        self._snapshot_delta_count = 0

    def reset(self) -> None:
        self._cancel_console_timer()
        self._current_snapshot = None
        self._last_emitted_snapshot = None
        self._pending_clients_radio = None
        self._pending_console_lines = []
        self._console_line_count = 0
        self._console_batch_count = 0
        self._snapshot_full_count = 0
        self._snapshot_delta_count = 0

    def efficiency_report(self) -> dict:
        average_batch_size = (
            self._console_line_count / self._console_batch_count if self._console_batch_count > 0 else 0.0
        )
        delta_full_ratio = (
            self._snapshot_delta_count / self._snapshot_full_count if self._snapshot_full_count > 0 else 0.0
        )
        return {
            "console_line_count": self._console_line_count,
            "console_batch_count": self._console_batch_count,
            "average_batch_size": round(average_batch_size, 3),
            "snapshot_delta_count": self._snapshot_delta_count,
            "snapshot_full_count": self._snapshot_full_count,
            "delta_full_ratio": round(delta_full_ratio, 3),
        }

    def feed(self, line: str) -> None:
        text = line.rstrip("\r\n")

        snap_match = self.SNAPSHOT_RE.match(text)
        if snap_match:
            self._flush_console_lines()
            self._emit_current_snapshot()
            self._current_snapshot = {
                "test_count": int(snap_match.group(1)),
                "device_ts": snap_match.group(2),
                "cpu": {},
                "memory": {},
                "wifi_clients": {},
            }
            self._pending_clients_radio = None
            return

        marker_match = self.CLIENT_MARKER_RE.match(text)
        if marker_match:
            self._pending_clients_radio = marker_match.group(1)
            return

        if self._pending_clients_radio is not None and text.startswith("{"):
            self._flush_console_lines()
            self._consume_clients_json(text)
            return

        if self._current_snapshot is None:
            self._queue_console_line(text)
            return

        cpu_match = self.CPU_RE.match(text)
        if not cpu_match:
            # /proc/meminfo lines stream inside the snapshot block: record the
            # keys the charts need into the snapshot and emit a live update, but
            # still queue the raw line so the Serial Console output is unchanged.
            mem_match = self.MEMINFO_RE.match(text)
            if mem_match and mem_match.group(1) in self.MEM_KEYS:
                self._current_snapshot["memory"][mem_match.group(1)] = int(mem_match.group(2))
                self._emit_snapshot_update()
            self._queue_console_line(text)
            return

        self._flush_console_lines()
        core_id = cpu_match.group(1)
        self._current_snapshot["cpu"][core_id] = {
            "usr": float(cpu_match.group(2)),
            "sys": float(cpu_match.group(3)),
            "nic": float(cpu_match.group(4)),
            "idle": float(cpu_match.group(5)),
            "io": float(cpu_match.group(6)),
            "irq": float(cpu_match.group(7)),
            "sirq": float(cpu_match.group(8)),
        }
        self._emit_snapshot_update()

    def flush(self) -> None:
        self._flush_console_lines()
        self._emit_current_snapshot()

    def _consume_clients_json(self, text: str) -> None:
        radio = self._pending_clients_radio
        self._pending_clients_radio = None
        if radio is None:
            return

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return

        if not isinstance(parsed, dict):
            return

        data = parsed.get("data")
        if not isinstance(data, dict):
            return

        clients_raw = data.get("client_list")
        clients = clients_raw if isinstance(clients_raw, list) else []
        total_size_raw = data.get("total_size")

        try:
            total_size = int(total_size_raw)
        except (TypeError, ValueError):
            total_size = len(clients)

        event = {
            "type": "wifi_clients_update",
            "radio": radio,
            "total_size": total_size,
            "clients": clients,
        }
        self.on_event(event)

        if self._current_snapshot is not None:
            self._current_snapshot["wifi_clients"][radio] = {
                "total_size": total_size,
                "clients": clients,
            }

    def _emit_current_snapshot(self) -> None:
        if self._current_snapshot is None:
            return
        self._emit_snapshot_update()
        self._current_snapshot = None

    def _emit_snapshot_update(self) -> None:
        if self._current_snapshot is None:
            return
        current_snapshot = deepcopy(self._current_snapshot)
        previous_snapshot = self._last_emitted_snapshot
        if previous_snapshot is None or self._is_snapshot_boundary(previous_snapshot, current_snapshot):
            self.on_event({"type": "snapshot_update", "snapshot": current_snapshot})
            self._last_emitted_snapshot = current_snapshot
            self._snapshot_full_count += 1
            return

        delta = self._build_snapshot_delta(previous_snapshot, current_snapshot)
        if delta:
            self.on_event({"type": "snapshot_delta", "delta": delta})
            self._last_emitted_snapshot = current_snapshot
            self._snapshot_delta_count += 1

    def _queue_console_line(self, text: str) -> None:
        should_flush_now = False
        with self._state_lock:
            self._pending_console_lines.append(text)
            if len(self._pending_console_lines) >= self.CONSOLE_BATCH_SIZE:
                should_flush_now = True
                self._cancel_console_timer_locked()
            else:
                self._ensure_console_timer_locked()
        if should_flush_now:
            self._flush_console_lines()

    def _flush_console_lines(self) -> None:
        with self._state_lock:
            if not self._pending_console_lines:
                self._cancel_console_timer_locked()
                return
            lines = self._pending_console_lines
            self._pending_console_lines = []
            self._cancel_console_timer_locked()
        self.on_event({"type": "console_line_batch", "lines": lines})
        self._console_line_count += len(lines)
        self._console_batch_count += 1

    def _flush_console_lines_from_timer(self) -> None:
        self._flush_console_lines()

    def _ensure_console_timer_locked(self) -> None:
        if self._console_flush_timer is not None:
            return
        timer = threading.Timer(self.CONSOLE_BATCH_MAX_LATENCY_SEC, self._flush_console_lines_from_timer)
        timer.daemon = True
        self._console_flush_timer = timer
        timer.start()

    def _cancel_console_timer(self) -> None:
        with self._state_lock:
            self._cancel_console_timer_locked()

    def _cancel_console_timer_locked(self) -> None:
        timer = self._console_flush_timer
        self._console_flush_timer = None
        if timer is not None:
            timer.cancel()

    def _build_snapshot_delta(self, previous: dict, current: dict) -> dict:
        delta: dict = {}
        if previous.get("test_count") != current.get("test_count"):
            delta["test_count"] = current.get("test_count")
        if previous.get("device_ts") != current.get("device_ts"):
            delta["device_ts"] = current.get("device_ts")

        previous_cpu = previous.get("cpu") if isinstance(previous.get("cpu"), dict) else {}
        current_cpu = current.get("cpu") if isinstance(current.get("cpu"), dict) else {}
        changed_cpu: dict = {}
        for core_id, metrics in current_cpu.items():
            if previous_cpu.get(core_id) != metrics:
                changed_cpu[core_id] = metrics
        if changed_cpu:
            delta["cpu"] = changed_cpu
        removed_cpu = sorted(set(previous_cpu.keys()) - set(current_cpu.keys()))
        if removed_cpu:
            delta["cpu_removed"] = removed_cpu

        # Memory keys are a fixed set that only changes value, never disappears,
        # so a "removed" list (like cpu_removed) is unnecessary.
        previous_mem = previous.get("memory") if isinstance(previous.get("memory"), dict) else {}
        current_mem = current.get("memory") if isinstance(current.get("memory"), dict) else {}
        changed_mem: dict = {}
        for key, value in current_mem.items():
            if previous_mem.get(key) != value:
                changed_mem[key] = value
        if changed_mem:
            delta["memory"] = changed_mem

        previous_wifi = previous.get("wifi_clients") if isinstance(previous.get("wifi_clients"), dict) else {}
        current_wifi = current.get("wifi_clients") if isinstance(current.get("wifi_clients"), dict) else {}
        changed_wifi: dict = {}
        for radio, payload in current_wifi.items():
            if previous_wifi.get(radio) != payload:
                changed_wifi[radio] = payload
        if changed_wifi:
            delta["wifi_clients"] = changed_wifi
        removed_wifi = sorted(set(previous_wifi.keys()) - set(current_wifi.keys()))
        if removed_wifi:
            delta["wifi_clients_removed"] = removed_wifi

        return delta

    def _is_snapshot_boundary(self, previous: dict, current: dict) -> bool:
        return (
            previous.get("test_count") != current.get("test_count")
            or previous.get("device_ts") != current.get("device_ts")
        )
