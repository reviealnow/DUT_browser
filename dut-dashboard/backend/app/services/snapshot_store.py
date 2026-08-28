from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

RING_MAX = 500


class SnapshotStore:
    """Server-side snapshot history for instant frontend backfill.

    Reconstructs full snapshots from the parser's ``snapshot_update`` /
    ``snapshot_delta`` events (mirroring the frontend's ``applySnapshotDelta``),
    keeps a bounded in-memory ring keyed by ``device_ts``, and appends finalized
    snapshots to a JSONL file so history survives a backend restart.

    Thread-safety: ``observe`` runs on the SerialWorker thread; ``recent`` runs
    on the asyncio request thread. All shared state is guarded by one lock.
    """

    def __init__(self, file_path: Path, ring_max: int = RING_MAX) -> None:
        self.file_path = file_path
        self.ring_max = ring_max
        self._lock = threading.Lock()
        self._ring: "OrderedDict[str, dict]" = OrderedDict()
        self._latest: dict | None = None
        self._appends_since_compact = 0

        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.touch(exist_ok=True)
        self._load_tail()
        # Rewrite the file from the (deduped, bounded) ring so an existing
        # append-only file is compacted at startup — fixes accumulated bloat
        # and duplicate lines without changing recent()/_load_tail behavior.
        with self._lock:
            self._compact_locked()

    # ---- ingest ---------------------------------------------------------

    def observe(self, event: dict) -> None:
        """Feed a WebSocket event; only snapshot events mutate state."""
        try:
            event_type = event.get("type")
            if event_type == "snapshot_update":
                snapshot = event.get("snapshot")
                if isinstance(snapshot, dict):
                    self._apply_full(deepcopy(snapshot))
            elif event_type == "snapshot_delta":
                delta = event.get("delta")
                if isinstance(delta, dict):
                    self._apply_delta(delta)
        except Exception:
            # Never let history bookkeeping break the stream.
            return

    def _apply_full(self, snapshot: dict) -> None:
        device_ts = snapshot.get("device_ts")
        if not isinstance(device_ts, str):
            return
        with self._lock:
            self._finalize_if_boundary_locked(device_ts)
            self._latest = snapshot
            self._upsert_locked(snapshot)

    def _apply_delta(self, delta: dict) -> None:
        with self._lock:
            if self._latest is None:
                return
            new_ts = delta.get("device_ts", self._latest.get("device_ts"))
            if isinstance(new_ts, str):
                self._finalize_if_boundary_locked(new_ts)
            self._latest = _reconstruct(self._latest, delta)
            self._upsert_locked(self._latest)

    # ---- query ----------------------------------------------------------

    def recent(self, limit: int) -> list[dict]:
        with self._lock:
            snapshots = list(self._ring.values())
        if limit > 0:
            snapshots = snapshots[-limit:]
        return deepcopy(snapshots)

    # ---- internals ------------------------------------------------------

    def _upsert_locked(self, snapshot: dict) -> None:
        device_ts = snapshot.get("device_ts")
        if not isinstance(device_ts, str):
            return
        # Move-to-end on re-insert so cores streaming in keep one ordered point.
        self._ring[device_ts] = deepcopy(snapshot)
        self._ring.move_to_end(device_ts)
        while len(self._ring) > self.ring_max:
            self._ring.popitem(last=False)

    def _finalize_if_boundary_locked(self, new_ts: str) -> None:
        # When device_ts changes the previous snapshot is complete: persist it.
        if self._latest is not None and self._latest.get("device_ts") != new_ts:
            self._persist_locked(self._latest)

    def _persist_locked(self, snapshot: dict) -> None:
        try:
            line = json.dumps(snapshot, separators=(",", ":"))
            with self.file_path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            self._appends_since_compact += 1
            # Bound the append-only file: after ~ring_max appends, rewrite it
            # from the ring (keeps the file within roughly [ring_max, 2*ring_max]).
            if self._appends_since_compact >= self.ring_max:
                self._compact_locked()
        except Exception:
            return

    def _compact_locked(self) -> None:
        # Atomically rewrite the file from the current ring (deduped by
        # device_ts, bounded to ring_max) via a temp file + os.replace.
        try:
            tmp_path = self.file_path.with_name(self.file_path.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as fp:
                for snapshot in self._ring.values():
                    fp.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
            os.replace(tmp_path, self.file_path)
            self._appends_since_compact = 0
        except Exception:
            return

    def _load_tail(self) -> None:
        # Load up to ring_max most-recent persisted snapshots; do NOT set
        # _latest, so the first new boundary won't re-persist a loaded entry.
        try:
            with self.file_path.open("r", encoding="utf-8", errors="ignore") as fp:
                from collections import deque

                tail = deque(fp, maxlen=self.ring_max)
        except Exception:
            return
        for raw in tail:
            raw = raw.strip()
            if not raw:
                continue
            try:
                snapshot = json.loads(raw)
            except Exception:
                continue
            if isinstance(snapshot, dict) and isinstance(snapshot.get("device_ts"), str):
                self._ring[snapshot["device_ts"]] = snapshot
                self._ring.move_to_end(snapshot["device_ts"])
        while len(self._ring) > self.ring_max:
            self._ring.popitem(last=False)


def _reconstruct(base: dict, delta: dict) -> dict:
    """Mirror of the frontend applySnapshotDelta (websocket.ts)."""
    next_cpu = dict(base.get("cpu") or {})
    for core_id in delta.get("cpu_removed") or []:
        next_cpu.pop(core_id, None)
    for core_id, metrics in (delta.get("cpu") or {}).items():
        next_cpu[core_id] = metrics

    next_memory = dict(base.get("memory") or {})
    for key, value in (delta.get("memory") or {}).items():
        next_memory[key] = value

    next_wifi = dict(base.get("wifi_clients") or {})
    for radio in delta.get("wifi_clients_removed") or []:
        next_wifi.pop(radio, None)
    for radio, payload in (delta.get("wifi_clients") or {}).items():
        next_wifi[radio] = payload

    return {
        "test_count": delta.get("test_count", base.get("test_count")),
        "device_ts": delta.get("device_ts", base.get("device_ts")),
        # Carried from the base rather than dropped. A delta never names the
        # device -- the update it is applied onto did -- so rebuilding without
        # this line would strip the stamp off every reading that arrived as a
        # delta, and the whole persisted ring would read as unknown provenance.
        "device_id": base.get("device_id"),
        "cpu": next_cpu,
        "memory": next_memory,
        "wifi_clients": next_wifi,
    }
