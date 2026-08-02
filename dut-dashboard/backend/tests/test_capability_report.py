"""Unit tests for capability_report reconciler. Pure function — no I/O."""

from __future__ import annotations

import unittest

from app.services.capability_report import build_capability_report

# Minimal Source A entries.
_SSID_A_6GHZ = {
    "iface": "ath16",
    "bssid": "ca:4f:86:25:6a:58",
    "ssid": "AP6_6GHzWPA3KEY",
    "band": "6GHz",
    "freq_mhz": 5955,
    "channel": 1,
    "channel_width": "80 MHz",
    "generation": "Wi-Fi 6E",
    "security": "WPA3-Personal",
    "pmf": "required",
    "dot11k": True,
    "dot11v": True,
    "dot11r": False,
}

_SSID_A_5GHZ = {
    "iface": "ath8",
    "bssid": "c8:4f:86:91:47:e2",
    "ssid": "TestNet5",
    "band": "5GHz",
    "freq_mhz": 5180,
    "channel": 36,
    "channel_width": "80 MHz",
    "generation": "Wi-Fi 5",
    "security": "WPA2-Personal",
    "pmf": "optional",
    "dot11k": False,
    "dot11v": False,
    "dot11r": False,
}

# Matching Source B BSS (same as Source A — no diffs).
_BSS_6GHZ_MATCH = {
    "bssid": "ca:4f:86:25:6a:58",
    "ssid": "AP6_6GHzWPA3KEY",
    "freq_mhz": 5955,
    "band": "6GHz",
    "channel": 1,
    "signal_dbm": -65.0,
    "generation": "Wi-Fi 6E",
    "security": "WPA3-Personal",
    "pmf": "required",
    "dot11k": True,
    "dot11v": True,
    "dot11r": False,
    "source": "iw",
}

# Source B BSS with security mismatch (observed = WPA2 vs config = WPA3).
_BSS_6GHZ_MISMATCH = {
    **_BSS_6GHZ_MATCH,
    "security": "WPA2-Personal",
    "pmf": "optional",
}

_SURVEY_AVAILABLE_BOTH = {
    "available": True,
    "iface": "wlan0",
    "bss": [_BSS_6GHZ_MATCH],
    "scannable_bands": ["6GHz"],
    "captured_at": "2026-06-29T10:00:00",
    "source": "iw",
}

_SURVEY_UNAVAILABLE = {
    "available": False,
    "reason": "SURVEY_WIFI_IFACE not set",
    "bss": [],
    "captured_at": "2026-06-29T10:00:00",
}


class TestBuildCapabilityReport(unittest.TestCase):
    def test_single_match_no_diffs(self) -> None:
        report = build_capability_report([_SSID_A_6GHZ], _SURVEY_AVAILABLE_BOTH)
        self.assertEqual(len(report["rows"]), 1)
        row = report["rows"][0]
        self.assertTrue(row["match"])
        self.assertEqual(row["diffs"], [])
        self.assertIsNone(row["caveat"])

    def test_match_with_security_diff(self) -> None:
        survey = {**_SURVEY_AVAILABLE_BOTH, "bss": [_BSS_6GHZ_MISMATCH]}
        report = build_capability_report([_SSID_A_6GHZ], survey)
        row = report["rows"][0]
        self.assertTrue(row["match"])
        diff_fields = {d["field"] for d in row["diffs"]}
        self.assertIn("security", diff_fields)
        self.assertIn("pmf", diff_fields)

    def test_no_match_with_caveat_unscanned_band(self) -> None:
        # 5GHz SSID, but survey only saw 6GHz → caveat expected.
        survey = {**_SURVEY_AVAILABLE_BOTH, "scannable_bands": ["6GHz"]}
        report = build_capability_report([_SSID_A_5GHZ], survey)
        row = report["rows"][0]
        self.assertFalse(row["match"])
        self.assertIsNotNone(row["caveat"])
        self.assertIn("5GHz", row["caveat"])

    def test_no_match_no_caveat_when_band_was_scanned(self) -> None:
        # 5GHz was scanned but BSSID simply wasn't seen — real miss, no caveat.
        survey = {**_SURVEY_AVAILABLE_BOTH, "bss": [], "scannable_bands": ["5GHz", "6GHz"]}
        report = build_capability_report([_SSID_A_5GHZ], survey)
        row = report["rows"][0]
        self.assertFalse(row["match"])
        self.assertIsNone(row["caveat"])

    def test_survey_unavailable_all_unmatched(self) -> None:
        report = build_capability_report([_SSID_A_6GHZ, _SSID_A_5GHZ], _SURVEY_UNAVAILABLE)
        self.assertFalse(report["available_b"])
        # One row per SSID passed in; a report that dropped them all would pass
        # the loop below without comparing anything.
        self.assertEqual(len(report["rows"]), 2)
        for row in report["rows"]:
            self.assertFalse(row["match"])
            self.assertIsNone(row["caveat"])  # no caveat when survey unavailable
            self.assertEqual(row["diffs"], [])

    def test_bssid_case_insensitive_join(self) -> None:
        # Source A uses uppercase, Source B uses lowercase — must still match.
        a = {**_SSID_A_6GHZ, "bssid": "CA:4F:86:25:6A:58"}
        b = {**_BSS_6GHZ_MATCH, "bssid": "ca:4f:86:25:6a:58"}
        survey = {**_SURVEY_AVAILABLE_BOTH, "bss": [b]}
        report = build_capability_report([a], survey)
        self.assertTrue(report["rows"][0]["match"])

    def test_empty_ssids(self) -> None:
        report = build_capability_report([], _SURVEY_AVAILABLE_BOTH)
        self.assertEqual(report["rows"], [])

    def test_scannable_bands_propagated(self) -> None:
        report = build_capability_report([], _SURVEY_AVAILABLE_BOTH)
        self.assertIn("6GHz", report["scannable_bands"])
