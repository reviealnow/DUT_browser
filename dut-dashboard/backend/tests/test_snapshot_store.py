from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.snapshot_store import SnapshotStore


def _update(test_count: int, device_ts: str, cpu: dict, wifi: dict | None = None) -> dict:
    return {
        "type": "snapshot_update",
        "snapshot": {
            "test_count": test_count,
            "device_ts": device_ts,
            "cpu": cpu,
            "wifi_clients": wifi or {},
        },
    }


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _store(self, ring_max: int = 500) -> SnapshotStore:
        return SnapshotStore(Path(self._dir.name) / "snap.jsonl", ring_max=ring_max)

    def test_reconstruct_full_from_update_and_delta(self) -> None:
        store = self._store()
        store.observe(_update(1, "T1", {"0": {"idle": 80.0}}))
        store.observe({"type": "snapshot_delta", "delta": {"device_ts": "T1", "cpu": {"1": {"idle": 90.0}}}})
        snaps = store.recent(10)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(set(snaps[0]["cpu"].keys()), {"0", "1"})  # delta merged onto base

    def test_upsert_dedups_by_device_ts(self) -> None:
        store = self._store()
        store.observe(_update(1, "T1", {"0": {"idle": 80.0}}))
        store.observe(_update(1, "T1", {"0": {"idle": 80.0}}))  # same ts again
        store.observe(_update(2, "T2", {"0": {"idle": 70.0}}))
        self.assertEqual([s["device_ts"] for s in store.recent(10)], ["T1", "T2"])

    def test_recent_limit(self) -> None:
        store = self._store()
        for i in range(10):
            store.observe(_update(i, f"T{i}", {"0": {"idle": 50.0}}))
        self.assertEqual([s["device_ts"] for s in store.recent(3)], ["T7", "T8", "T9"])

    def test_compaction_bounds_file(self) -> None:
        store = self._store(ring_max=5)
        for i in range(30):
            store.observe(_update(i, f"T{i:02d}", {"0": {"idle": 50.0}}))
        line_count = sum(1 for _ in store.file_path.open())
        self.assertLessEqual(line_count, 2 * 5)          # append + threshold compaction
        self.assertEqual(len(store.recent(100)), 5)      # ring bounded

    def test_startup_compaction_dedups_and_bounds(self) -> None:
        path = Path(self._dir.name) / "bloated.jsonl"
        with path.open("w", encoding="utf-8") as fp:
            for i in range(20):
                fp.write(json.dumps({"test_count": i, "device_ts": f"T{i:02d}", "cpu": {}, "wifi_clients": {}}) + "\n")
            fp.write(json.dumps({"test_count": 19, "device_ts": "T19", "cpu": {}, "wifi_clients": {}}) + "\n")  # dup
        SnapshotStore(path, ring_max=5)  # __init__ runs startup compaction
        with path.open() as fp:
            tss = [json.loads(line)["device_ts"] for line in fp if line.strip()]
        self.assertLessEqual(len(tss), 5)
        self.assertEqual(len(tss), len(set(tss)))        # no duplicates after compaction


if __name__ == "__main__":
    unittest.main()
