"""Tests for persisted site-survey snapshots (write/list/latest/restore) and the
download endpoint's traversal guard.

The module is exercised with SURVEY_SNAPSHOT_DIR pointed at a tempdir; the
endpoint is called directly (like test_survey_cache) so no TestClient is needed.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import main
from app.services import survey_cache, survey_snapshot

_RECS = [{"band": "2.4GHz", "current_channel": 11, "recommended_channel": 1}]
_NEIGHBORS = [
    {"band": "2.4GHz", "channel": 11, "ssid": "HomeNet", "bssid": "aa:bb:cc:dd:ee:ff",
     "signal_dbm": -55.0, "security": "WPA2-Personal", "iface": "ath0"},
    {"band": "5GHz", "channel": 36, "ssid": None, "bssid": "11:22:33:44:55:66",
     "signal_dbm": -70.0, "security": "Open", "iface": "ath1"},
]
_VAPS = [{"iface": "ath0", "ssid": "MyAP", "band": "2.4GHz", "channel": 11}]


class SurveySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._tmp = Path(self._dir.name)
        p = mock.patch.object(survey_snapshot, "SURVEY_SNAPSHOT_DIR", self._tmp)
        p.start()
        self.addCleanup(p.stop)
        survey_cache.clear()

    def _write(self, dut="lab2") -> list[Path]:
        return survey_snapshot.write_snapshot(dut, _RECS, _NEIGHBORS, _VAPS, "2026-07-09T10:00:00")

    def test_write_produces_json_and_csv(self) -> None:
        json_path, csv_path = self._write()
        self.assertTrue(json_path.name.startswith("site-survey-lab2-") and json_path.suffix == ".json")
        self.assertTrue(csv_path.suffix == ".csv")
        data = json.loads(json_path.read_text())
        self.assertEqual(data["recommendations"], _RECS)
        self.assertEqual(data["neighbors"], _NEIGHBORS)
        self.assertEqual(data["captured_at"], "2026-07-09T10:00:00")
        rows = list(csv.reader(csv_path.read_text().splitlines()))
        self.assertEqual(rows[0], ["band", "channel", "ssid", "bssid", "signal_dbm", "security"])
        self.assertEqual(rows[1], ["2.4GHz", "11", "HomeNet", "aa:bb:cc:dd:ee:ff", "-55.0", "WPA2-Personal"])
        # A None ssid serializes to an empty CSV cell, not the string "None".
        self.assertEqual(rows[2][2], "")

    def test_list_snapshots_newest_first(self) -> None:
        import os
        # Two distinct snapshots with explicit, ordered mtimes (a same-second
        # write would reuse the filename and overwrite — surveys take minutes).
        old = self._tmp / "site-survey-lab2-20260101-000000.json"
        new = self._tmp / "site-survey-lab2-20260102-000000.json"
        old.write_text("{}", encoding="utf-8")
        new.write_text("{}", encoding="utf-8")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        names = [s["name"] for s in survey_snapshot.list_snapshots()]
        self.assertLess(names.index(new.name), names.index(old.name))

    def test_latest_for_returns_newest_pair(self) -> None:
        import os
        first_json = self._write()[0]
        os.utime(first_json, (1_000_000, 1_000_000))
        os.utime(first_json.with_suffix(".csv"), (1_000_000, 1_000_000))
        # Rename the first pair to an older timestamp so sorting is deterministic.
        older = "site-survey-lab2-20260101-000000"
        first_json.rename(self._tmp / f"{older}.json")
        first_json.with_suffix(".csv").rename(self._tmp / f"{older}.csv")
        newer_json = self._write()[0]
        latest = survey_snapshot.latest_for("lab2")
        self.assertEqual({p.suffix for p in latest}, {".json", ".csv"})
        self.assertIn(newer_json.name, {p.name for p in latest})

    def test_latest_for_unknown_dut_is_empty(self) -> None:
        self._write("lab2")
        self.assertEqual(survey_snapshot.latest_for("ghost"), [])

    def test_latest_for_is_scoped_to_one_hyphenated_dut(self) -> None:
        self._write("lab2")
        self._write("ap6-420e")  # hyphenated id must still parse
        paths = survey_snapshot.latest_for("ap6-420e")
        self.assertEqual(len(paths), 2)  # json + csv
        self.assertTrue(all("ap6-420e" in p.name for p in paths))

    def test_restore_cache_repopulates_recommendations(self) -> None:
        self._write("lab2")
        survey_cache.clear()
        self.assertIsNone(survey_cache.last_recommendation("lab2"))
        survey_snapshot.restore_cache()
        cached = survey_cache.last_recommendation("lab2")
        assert cached is not None
        self.assertEqual(cached["recommendations"], _RECS)
        self.assertEqual(cached["captured_at"], "2026-07-09T10:00:00")

    def test_restore_cache_skips_corrupt_file(self) -> None:
        (self._tmp / "site-survey-bad-20260101-000000.json").write_text("{not json", encoding="utf-8")
        survey_snapshot.restore_cache()  # must not raise
        self.assertIsNone(survey_cache.last_recommendation("bad"))

    def test_stray_files_ignored(self) -> None:
        (self._tmp / "notes.txt").write_text("x", encoding="utf-8")
        (self._tmp / "site-survey-lab2-badstamp.json").write_text("{}", encoding="utf-8")
        self._write("lab2")
        names = [s["name"] for s in survey_snapshot.list_snapshots()]
        self.assertNotIn("notes.txt", names)
        self.assertNotIn("site-survey-lab2-badstamp.json", names)


class SurveyDownloadEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._tmp = Path(self._dir.name)
        p = mock.patch.object(main, "SURVEY_SNAPSHOT_DIR", self._tmp)
        p.start()
        self.addCleanup(p.stop)

    def test_serves_existing_json(self) -> None:
        (self._tmp / "site-survey-lab2-20260101-000000.json").write_text("{}", encoding="utf-8")
        resp = main.download_survey("site-survey-lab2-20260101-000000.json")
        self.assertEqual(Path(resp.path).name, "site-survey-lab2-20260101-000000.json")

    def test_traversal_rejected(self) -> None:
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_survey("../../etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_allowed_extension_rejected(self) -> None:
        (self._tmp / "site-survey-lab2-20260101-000000.log").write_text("x", encoding="utf-8")
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_survey("site-survey-lab2-20260101-000000.log")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_file_404(self) -> None:
        with self.assertRaises(main.HTTPException) as ctx:
            main.download_survey("site-survey-lab2-20260101-000000.csv")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
