"""Unit tests for site_survey (DUT-side neighbor scan + channel recommendation).

No subprocess/serial calls: parse_iw_scan_text reuse is exercised against a
real trimmed `iw dev ath0 scan` capture from an AP6 840E (2026-07); the
recommender is tested as a pure function against constructed inputs.
"""

from __future__ import annotations

import unittest

from app.services.site_survey import (
    _overlap_weight_24g,
    _signal_weight,
    channel_recommendation,
    parse_iw_scan_text,
)

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

    def test_one_row_per_band_when_multiple_vaps_share_a_band(self) -> None:
        # Several SSIDs on one radio => several VAPs on the same band/channel.
        # The recommendation is per band, so this must collapse to a single row.
        own = [
            _own_vap("ath0", "2.4GHz", 6, "aa:aa:aa:aa:aa:01"),
            _own_vap("ath1", "2.4GHz", 6, "aa:aa:aa:aa:aa:02"),
            _own_vap("ath2", "2.4GHz", 6, "aa:aa:aa:aa:aa:03"),
            _own_vap("ath16", "5GHz", 36, "bb:bb:bb:bb:bb:01"),
            _own_vap("ath17", "5GHz", 36, "bb:bb:bb:bb:bb:02"),
        ]
        rows = channel_recommendation([], own)
        self.assertEqual([r["band"] for r in rows], ["2.4GHz", "5GHz"])
        # The kept row is the first VAP seen for that band.
        self.assertEqual(rows[0]["iface"], "ath0")
        self.assertEqual(rows[1]["iface"], "ath16")

    def test_own_bssid_exclusion_spans_all_same_band_vaps(self) -> None:
        # Even though only one row per band is emitted, EVERY same-band VAP's
        # BSSID must still be excluded from the neighbor tally (an AP sees all
        # of its own SSIDs in a scan).
        own = [
            _own_vap("ath0", "2.4GHz", 6, "aa:aa:aa:aa:aa:01"),
            _own_vap("ath1", "2.4GHz", 6, "aa:aa:aa:aa:aa:02"),
        ]
        neighbors = [
            _neighbor("aa:aa:aa:aa:aa:02", 6, -40),  # our own 2nd SSID — must be ignored
            _neighbor("ee:ee:ee:ee:ee:01", 6, -50),  # a genuine neighbor
        ]
        row = channel_recommendation(neighbors, own)[0]
        # Only the genuine neighbor contributes (weight 3 for -50), not our own.
        self.assertEqual(row["occupancy"][6], _signal_weight(-50) * _overlap_weight_24g(0))


class TestAdjacentChannelWeighting(unittest.TestCase):
    """2.4GHz occupancy applies _overlap_weight_24g; 5/6GHz stay co-channel."""

    def test_overlap_weights_pinned(self) -> None:
        # One strong neighbor (weight 3) on ch 6 bleeds linearly: 3.0 at Δ0
        # down to 0 at Δ5 — so the 1/6/11 grid stays mutually clean.
        own = [_own_vap("ath0", "2.4GHz", 1, "aa:aa:aa:aa:aa:01")]
        occupancy = channel_recommendation([_neighbor("11:11:11:11:11:01", 6, -50)], own)[0]["occupancy"]
        self.assertEqual(occupancy[6], 3.0)
        self.assertEqual(occupancy[5], 2.4)
        self.assertEqual(occupancy[7], 2.4)
        self.assertEqual(occupancy[4], 1.8)
        self.assertEqual(occupancy[8], 1.8)
        self.assertEqual(occupancy[1], 0.0)
        self.assertEqual(occupancy[11], 0.0)

    def test_adjacent_bleed_changes_recommendation(self) -> None:
        # No neighbor sits exactly on 1/6/11 — the old co-channel model saw
        # all candidates as equally clear and kept the current channel. With
        # bleed, ch 3 pollutes ch 1 (Δ2) and ch 13 pollutes ch 11 (Δ2), so
        # ch 6 (only Δ3 bleed from ch 3) is now the right call.
        own = [_own_vap("ath0", "2.4GHz", 1, "aa:aa:aa:aa:aa:01")]
        neighbors = [
            _neighbor("11:11:11:11:11:01", 3, -50),
            _neighbor("11:11:11:11:11:02", 13, -50),
        ]
        row = channel_recommendation(neighbors, own)[0]
        self.assertEqual(row["occupancy"][1], 1.8)
        self.assertEqual(row["occupancy"][6], 1.2)
        self.assertEqual(row["occupancy"][11], 1.8)
        self.assertEqual(row["recommended_channel"], 6)

    def test_24g_occupancy_covers_full_grid(self) -> None:
        own = [_own_vap("ath0", "2.4GHz", 6, "aa:aa:aa:aa:aa:01")]
        occupancy = channel_recommendation([], own)[0]["occupancy"]
        self.assertEqual(set(occupancy.keys()), set(range(1, 14)))

    def test_5ghz_stays_co_channel_only(self) -> None:
        own = [_own_vap("ath16", "5GHz", 36, "bb:bb:bb:bb:bb:01")]
        row = channel_recommendation([_neighbor("cc:cc:cc:cc:cc:01", 40, -50, band="5GHz")], own)[0]
        self.assertEqual(row["occupancy"], {40: 3.0})
        self.assertEqual(row["recommended_channel"], 36)
        self.assertEqual(row["score"], 0)


if __name__ == "__main__":
    unittest.main()
