"""Tests for connect-time DUT context: the writers, the session-window
selection that replaced "newest of everything", and the bundlers that feed the
log ZIP and the Analyze output.

The kind → directory map is patched onto a tempdir so nothing touches the real
logs/. Snapshot timestamps come from the filename, so tests place files at
chosen instants by writing them directly rather than by faking the clock.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.services import context_snapshot

_CLIENTS = [
    {"iface": "ath0", "band": "2.4GHz", "ssid": "MyAP", "mac": "aa:bb:cc:dd:ee:ff",
     "vendor": "Acme", "aid": 1, "channel": 11, "txrate": "72M", "rxrate": "65M",
     "rssi": -48, "phymode": "IEEE80211_MODE_11AXG_HE20", "width": "20MHz",
     "assoc_time": "00:12:33"},
]
_CLIENT_VAPS = [{"iface": "ath0", "ssid": "MyAP", "band": "2.4GHz", "channel": 11}]
_SSIDS = [
    {"iface": "ath0", "ssid": "MyAP", "bssid": "11:22:33:44:55:66", "band": "2.4GHz",
     "freq_mhz": 2462, "channel": 11, "channel_width": "20 MHz", "generation": "Wi-Fi 6",
     "security": "WPA2-Personal", "category": "personal", "pmf": "optional",
     "akm": ["WPA-PSK", "SAE"], "pairwise_cipher": ["CCMP"], "group_mgmt_cipher": None,
     "dot11k": True, "dot11v": None, "dot11r": None},
]


class ContextSnapshotTestCase(unittest.TestCase):
    """Points every kind's directory at a fresh tempdir."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.dirs = {kind: self.tmp / kind for kind in context_snapshot.KINDS}
        p = mock.patch.object(context_snapshot, "_KIND_DIRS", self.dirs)
        p.start()
        self.addCleanup(p.stop)

    def place(self, kind: str, dut: str, ts: str) -> Path:
        """Write a minimal snapshot pair for `kind`/`dut` stamped at `ts`."""
        directory = self.dirs[kind]
        directory.mkdir(parents=True, exist_ok=True)
        base = directory / f"{kind}-{dut}-{ts}"
        json_path = base.with_suffix(".json")
        json_path.write_text(json.dumps({"dut_id": dut, "kind": kind}), encoding="utf-8")
        base.with_suffix(".csv").write_text("a,b\n1,2\n", encoding="utf-8")
        return json_path

    def session_log(self, name: str, mtime: str) -> Path:
        """A session log file whose mtime is set to `mtime` (YYYYmmdd-HHMMSS)."""
        path = self.tmp / name
        path.write_text("# mode=serial\n", encoding="utf-8")
        stamp = datetime.strptime(mtime, "%Y%m%d-%H%M%S").timestamp()
        os.utime(path, (stamp, stamp))
        return path


class WriterTests(ContextSnapshotTestCase):
    def test_clients_capture_writes_json_and_csv(self) -> None:
        json_path, csv_path = context_snapshot.write_clients(
            "lab2", _CLIENTS, _CLIENT_VAPS, "2026-07-29T10:00:00"
        )
        self.assertTrue(json_path.name.startswith("wifi-clients-lab2-"))
        self.assertEqual(json_path.suffix, ".json")
        data = json.loads(json_path.read_text())
        self.assertEqual(data["dut_id"], "lab2")
        self.assertEqual(data["kind"], "wifi-clients")
        self.assertEqual(data["captured_at"], "2026-07-29T10:00:00")
        self.assertEqual(data["clients"], _CLIENTS)
        self.assertEqual(data["vaps"], _CLIENT_VAPS)

        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(rows[0]["rssi"], "-48")

    def test_capability_capture_flattens_list_fields_into_the_csv(self) -> None:
        json_path, csv_path = context_snapshot.write_capability(
            "lab2", _SSIDS, "2026-07-29T10:00:00"
        )
        self.assertEqual(json.loads(json_path.read_text())["ssids"], _SSIDS)
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        self.assertEqual(rows[0]["akm"], "WPA-PSK SAE")
        self.assertEqual(rows[0]["pairwise_cipher"], "CCMP")
        # A None stays empty rather than rendering the word "None".
        self.assertEqual(rows[0]["group_mgmt_cipher"], "")

    def test_capture_of_nothing_still_writes_a_header_only_csv(self) -> None:
        _json_path, csv_path = context_snapshot.write_clients("lab2", [], [], "2026-07-29T10:00:00")
        self.assertEqual(csv_path.read_text(encoding="utf-8").strip().count("\n"), 0)

    def test_hyphenated_dut_id_round_trips_through_the_name(self) -> None:
        context_snapshot.write_clients("ap6-420e", _CLIENTS, _CLIENT_VAPS, "2026-07-29T10:00:00")
        entries = context_snapshot.snapshot_entries()
        self.assertTrue(entries)
        self.assertEqual({e["dut"] for e in entries}, {"ap6-420e"})

    def test_two_captures_a_second_apart_do_not_collide(self) -> None:
        first = context_snapshot.write_clients("lab2", _CLIENTS, [], "2026-07-29T10:00:00")
        time.sleep(1.05)
        second = context_snapshot.write_clients("lab2", _CLIENTS, [], "2026-07-29T10:00:01")
        self.assertNotEqual(first[0].name, second[0].name)
        self.assertEqual(len(context_snapshot.snapshot_entries()), 4)


class SessionWindowTests(ContextSnapshotTestCase):
    def test_window_spans_the_log_name_to_its_mtime(self) -> None:
        log = self.session_log("dut-session-20260729-100000.log", "20260729-110000")
        self.assertEqual(context_snapshot.session_window(log), ("20260729-100000", "20260729-110000"))

    def test_window_parses_a_labelled_log_with_hyphens(self) -> None:
        log = self.session_log("dut-session-ap6-420e-20260729-100000.log", "20260729-110000")
        self.assertEqual(context_snapshot.session_window(log), ("20260729-100000", "20260729-110000"))

    def test_unparseable_name_has_no_window(self) -> None:
        stray = self.tmp / "notes.log"
        stray.write_text("x", encoding="utf-8")
        self.assertIsNone(context_snapshot.session_window(stray))

    def test_mtime_before_the_name_collapses_the_window(self) -> None:
        log = self.session_log("dut-session-20260729-100000.log", "20260728-090000")
        self.assertEqual(context_snapshot.session_window(log), ("20260729-100000", "20260729-100000"))


class SelectForSessionTests(ContextSnapshotTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.session_log("dut-session-20260729-100000.log", "20260729-110000")

    def test_picks_only_captures_inside_the_window(self) -> None:
        inside = self.place("wifi-clients", "lab2", "20260729-100500")
        self.place("wifi-clients", "lab2", "20260729-095959")  # before the session opened
        self.place("wifi-clients", "lab2", "20260729-110001")  # after the last write
        names = {p.name for p in context_snapshot.select_for_session(self.log)}
        self.assertEqual(names, {inside.name, inside.with_suffix(".csv").name})

    def test_window_boundaries_are_inclusive(self) -> None:
        self.place("wifi-clients", "lab2", "20260729-100000")
        self.place("ssid-capability", "lab2", "20260729-110000")
        selected = context_snapshot.select_for_session(self.log)
        self.assertEqual(len(selected), 4)  # two kinds × (json + csv)

    def test_all_three_kinds_are_selected(self) -> None:
        self.place("site-survey", "lab2", "20260729-100500")
        self.place("wifi-clients", "lab2", "20260729-100600")
        self.place("ssid-capability", "lab2", "20260729-100700")
        kinds = {e["kind"] for e in context_snapshot.select_entries_for_session(self.log)}
        self.assertEqual(kinds, set(context_snapshot.KINDS))

    def test_dut_filter_excludes_another_dut_in_the_same_window(self) -> None:
        self.place("wifi-clients", "lab2", "20260729-100500")
        self.place("wifi-clients", "ap6-420e", "20260729-100500")
        selected = context_snapshot.select_entries_for_session(self.log, dut_id="ap6-420e")
        self.assertEqual({e["dut"] for e in selected}, {"ap6-420e"})

    def test_a_log_that_predates_the_feature_selects_nothing(self) -> None:
        old = self.session_log("dut-session-20260101-080000.log", "20260101-090000")
        self.place("wifi-clients", "lab2", "20260729-100500")
        self.assertEqual(context_snapshot.select_for_session(old), [])

    def test_unparseable_log_name_never_falls_back_to_newest(self) -> None:
        stray = self.tmp / "notes.log"
        stray.write_text("x", encoding="utf-8")
        self.place("wifi-clients", "lab2", "20260729-100500")
        self.assertEqual(context_snapshot.select_for_session(stray), [])


class BundleContextTests(ContextSnapshotTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.session_log("dut-session-20260729-100000.log", "20260729-110000")
        self.dest = self.tmp / "session-dir"

    def test_bundle_lays_files_out_by_kind(self) -> None:
        self.place("site-survey", "lab2", "20260729-100500")
        self.place("wifi-clients", "lab2", "20260729-100600")
        written = context_snapshot.bundle_context(self.dest, self.log)
        self.assertEqual(len(written), 4)
        self.assertTrue((self.dest / "context" / "site-survey").is_dir())
        self.assertTrue((self.dest / "context" / "wifi-clients").is_dir())

    def test_bundle_skips_out_of_window_captures(self) -> None:
        self.place("site-survey", "lab2", "20260101-090000")
        written = context_snapshot.bundle_context(self.dest, self.log)
        self.assertEqual(written, [])
        # Nothing captured means no directory at all, not an empty one.
        self.assertFalse(self.dest.exists())

    def test_bundle_swallows_copy_errors(self) -> None:
        self.place("wifi-clients", "lab2", "20260729-100500")
        with mock.patch.object(context_snapshot.shutil, "copy2", side_effect=OSError("disk full")):
            self.assertEqual(context_snapshot.bundle_context(self.dest, self.log), [])

    def test_rebundling_the_same_log_is_idempotent(self) -> None:
        self.place("wifi-clients", "lab2", "20260729-100500")
        first = context_snapshot.bundle_context(self.dest, self.log)
        second = context_snapshot.bundle_context(self.dest, self.log)
        self.assertEqual([p.name for p in first], [p.name for p in second])


class ListSnapshotsTests(ContextSnapshotTestCase):
    def test_listing_excludes_site_surveys_by_default(self) -> None:
        self.place("site-survey", "lab2", "20260729-100500")
        self.place("wifi-clients", "lab2", "20260729-100600")
        kinds = {item["kind"] for item in context_snapshot.list_snapshots()}
        self.assertEqual(kinds, {"wifi-clients"})

    def test_listing_is_newest_first_with_sizes(self) -> None:
        self.place("wifi-clients", "lab2", "20260729-100500")
        items = context_snapshot.list_snapshots()
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["size"] > 0 for item in items))
        self.assertEqual(items, sorted(items, key=lambda i: i["mtime"], reverse=True))

    def test_missing_directories_list_as_empty(self) -> None:
        self.assertEqual(context_snapshot.list_snapshots(), [])
        self.assertEqual(context_snapshot.snapshot_entries(), [])

    def test_unrelated_files_in_the_directory_are_ignored(self) -> None:
        directory = self.dirs["wifi-clients"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "README.txt").write_text("x", encoding="utf-8")
        (directory / "wifi-clients-lab2-bogus.json").write_text("{}", encoding="utf-8")
        self.assertEqual(context_snapshot.snapshot_entries(), [])


if __name__ == "__main__":
    unittest.main()
