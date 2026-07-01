"""Unit tests for site_survey (DUT-side neighbor scan + channel recommendation).

No subprocess/serial calls: parse_iw_scan_text reuse is exercised against a
real trimmed `iw dev ath0 scan` capture from an AP6 840E (2026-07); the
recommender is tested as a pure function against constructed inputs.
"""

from __future__ import annotations

import unittest

from app.services.site_survey import channel_recommendation, parse_iw_scan_text

# Trimmed real capture (3 of ~14 BSS blocks) from `iw dev ath0 scan` on a live
# AP6 840E — proves the DUT's busybox `iw` output matches the host-side format
# wifi_survey._parse_iw_scan already parses, reused here rather than
# duplicated.
_ATH0_SCAN_SAMPLE = """\
BSS 74:da:38:42:a9:d8(on ath0)
\tfreq: 2457
\tsignal: -51.00 dBm
\tSSID: WAP1750-42A9D8_G
\tHT capabilities:
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
BSS c8:4f:86:89:f1:68(on ath0)
\tfreq: 2462
\tsignal: -51.00 dBm
\tSSID: Lng01_20260623T104114_hf7q
\tHT capabilities:
BSS 00:aa:bb:cc:dd:fe(on ath0)
\tfreq: 2437
\tsignal: -74.00 dBm
\tSSID: Sophos AP6 420E CCDDFE_2
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
"""


class TestParseIwScanTextReuse(unittest.TestCase):
    """parse_iw_scan_text is wifi_survey._parse_iw_scan, imported not copied."""

    def test_real_dut_capture_parses(self) -> None:
        neighbors = parse_iw_scan_text(_ATH0_SCAN_SAMPLE)
        self.assertEqual(len(neighbors), 3)

    def test_channels_and_band_derived_from_freq(self) -> None:
        by_bssid = {n["bssid"]: n for n in parse_iw_scan_text(_ATH0_SCAN_SAMPLE)}
        self.assertEqual(by_bssid["74:da:38:42:a9:d8"]["channel"], 10)
        self.assertEqual(by_bssid["c8:4f:86:89:f1:68"]["channel"], 11)
        self.assertEqual(by_bssid["00:aa:bb:cc:dd:fe"]["channel"], 6)
        for n in by_bssid.values():
            self.assertEqual(n["band"], "2.4GHz")


def _neighbor(bssid: str, channel: int, signal_dbm: float, band: str = "2.4GHz") -> dict:
    return {"bssid": bssid, "channel": channel, "signal_dbm": signal_dbm, "band": band, "iface": "ath0"}


def _own_vap(iface: str, band: str, channel: int, bssid: str) -> dict:
    return {"iface": iface, "band": band, "channel": channel, "bssid": bssid}


class TestChannelRecommendation(unittest.TestCase):
    def test_recommends_least_occupied_24g_channel(self) -> None:
        own = [_own_vap("ath0", "2.4GHz", 6, "aa:aa:aa:aa:aa:01")]
        neighbors = [
            _neighbor("11:11:11:11:11:01", 6, -50),
            _neighbor("11:11:11:11:11:02", 6, -55),
            _neighbor("11:11:11:11:11:03", 6, -60),
        ]
        rows = channel_recommendation(neighbors, own)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["current_channel"], 6)
        # Channel 1 and 11 have zero neighbors -> tie broken by lowest channel number.
        self.assertEqual(row["recommended_channel"], 1)
        self.assertEqual(row["score"], 0)
        self.assertEqual(row["occupancy"].get(1, 0), 0)

    def test_excludes_own_bssid_from_occupancy(self) -> None:
        own_bssid = "aa:aa:aa:aa:aa:01"
        own = [_own_vap("ath0", "2.4GHz", 6, own_bssid)]
        # Only neighbor on channel 6 is the DUT's own VAP (self-detected in its
        # own scan) — must not count against itself.
        neighbors = [_neighbor(own_bssid, 6, -40)]
        rows = channel_recommendation(neighbors, own)
        row = rows[0]
        self.assertEqual(row["recommended_channel"], row["current_channel"])
        self.assertEqual(row["occupancy"].get(6, 0), 0)

    def test_no_neighbors_keeps_current_channel(self) -> None:
        own = [_own_vap("ath0", "2.4GHz", 6, "aa:aa:aa:aa:aa:01")]
        rows = channel_recommendation([], own)
        row = rows[0]
        self.assertEqual(row["recommended_channel"], 6)
        self.assertIn("clear", row["reasoning"])

    def test_5ghz_candidates_limited_to_observed_channels(self) -> None:
        own = [_own_vap("ath16", "5GHz", 36, "bb:bb:bb:bb:bb:01")]
        neighbors = [
            _neighbor("cc:cc:cc:cc:cc:01", 36, -40, band="5GHz"),
            _neighbor("cc:cc:cc:cc:cc:02", 36, -45, band="5GHz"),
            _neighbor("cc:cc:cc:cc:cc:03", 149, -70, band="5GHz"),
        ]
        rows = channel_recommendation(neighbors, own)
        row = rows[0]
        # Only channels actually observed (+ current) are candidates — not the
        # fixed 1/6/11 set, which is 2.4GHz-only.
        self.assertEqual(set(row["occupancy"].keys()) | {row["current_channel"]}, {36, 149})
        self.assertEqual(row["recommended_channel"], 149)

    def test_stronger_signal_scores_higher_than_weak(self) -> None:
        own = [_own_vap("ath0", "2.4GHz", 1, "aa:aa:aa:aa:aa:01")]
        neighbors = [_neighbor("dd:dd:dd:dd:dd:01", 1, -50)]  # strong -> weight 3
        weak_neighbors = [_neighbor("dd:dd:dd:dd:dd:02", 1, -85)]  # weak -> weight 1
        strong_score = channel_recommendation(neighbors, own)[0]["occupancy"][1]
        weak_score = channel_recommendation(weak_neighbors, own)[0]["occupancy"][1]
        self.assertGreater(strong_score, weak_score)

    def test_skips_own_vap_missing_band_or_channel(self) -> None:
        own = [{"iface": "ath0", "band": None, "channel": None, "bssid": "aa:aa:aa:aa:aa:01"}]
        self.assertEqual(channel_recommendation([], own), [])


if __name__ == "__main__":
    unittest.main()
