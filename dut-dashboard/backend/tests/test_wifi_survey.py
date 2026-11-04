"""Unit tests for wifi_survey parsers. No subprocess or network calls."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import wifi_survey
from app.services.wifi_survey import _parse_iw_scan, _parse_nmcli, _scannable_bands, get_wifi_survey

_IW_SCAN_SAMPLE = """\
BSS ca:4f:86:25:6a:58(on wlan0)
\tlast seen: 10 ms ago
\tfreq: 5955
\tsignal: -65.00 dBm
\tSSID: AP6_6GHzWPA3KEY
\tHE capabilities:
\t\t...
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: SAE
\t\t * Capabilities: 1-PTKSA-RC 1-GTKSA-RC MFP-required
BSS c8:4f:86:91:47:e2(on wlan0)
\tfreq: 5180
\tsignal: -55.00 dBm
\tSSID: !9018_usageInsight
\tVHT capabilities:
\tHT capabilities:
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP TKIP
\t\t * Authentication suites: WPA-EAP
\t\t * Capabilities: 1-PTKSA-RC MFP-capable
BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tfreq: 2437
\tsignal: -70.00 dBm
\tSSID: OpenNet
"""

_NMCLI_SAMPLE = (
    "AA\\:BB\\:CC\\:DD\\:EE\\:FF:MySSID:6:5180 MHz:WPA2\n"
    "11\\:22\\:33\\:44\\:55\\:66::1:2412 MHz:--\n"
)


class TestParseIwScan(unittest.TestCase):
    def test_three_bss_parsed(self) -> None:
        result = _parse_iw_scan(_IW_SCAN_SAMPLE)
        self.assertEqual(len(result), 3)

    def test_6ghz_wpa3_personal(self) -> None:
        bss = {b["bssid"]: b for b in _parse_iw_scan(_IW_SCAN_SAMPLE)}
        b = bss["ca:4f:86:25:6a:58"]
        self.assertEqual(b["ssid"], "AP6_6GHzWPA3KEY")
        self.assertEqual(b["freq_mhz"], 5955)
        self.assertEqual(b["band"], "6GHz")
        self.assertEqual(b["signal_dbm"], -65.0)
        self.assertEqual(b["security"], "WPA3-Personal")
        self.assertEqual(b["pmf"], "required")
        self.assertEqual(b["generation"], "Wi-Fi 6E")

    def test_5ghz_wpa2_enterprise(self) -> None:
        bss = {b["bssid"]: b for b in _parse_iw_scan(_IW_SCAN_SAMPLE)}
        b = bss["c8:4f:86:91:47:e2"]
        self.assertEqual(b["security"], "WPA2-Enterprise")
        self.assertEqual(b["pmf"], "optional")
        # VHT + HT present → Wi-Fi 5 (highest)
        self.assertEqual(b["generation"], "Wi-Fi 5")

    def test_open_network(self) -> None:
        bss = {b["bssid"]: b for b in _parse_iw_scan(_IW_SCAN_SAMPLE)}
        b = bss["aa:bb:cc:dd:ee:ff"]
        self.assertEqual(b["security"], "Open")
        self.assertIsNone(b["category"])

    def test_empty_input(self) -> None:
        self.assertEqual(_parse_iw_scan(""), [])


class TestFreqToChannel(unittest.TestCase):
    """Centre frequency -> channel, pinned at each band's edges."""

    def test_channels(self) -> None:
        cases = {
            2412: 1, 2472: 13, 2484: 14,
            5180: 36, 5885: 177,
            # 6 GHz: channel n is 5950 + 5n MHz; channel 2 is the odd one at 5935.
            5935: 2, 5955: 1, 5975: 5, 6115: 33, 7115: 233,
        }
        for freq, channel in cases.items():
            with self.subTest(freq=freq):
                self.assertEqual(wifi_survey._freq_to_channel(freq), channel)

    def test_off_plan_frequencies(self) -> None:
        for freq in (None, 2400, 2490, 5000, 5945, 7130):
            with self.subTest(freq=freq):
                self.assertIsNone(wifi_survey._freq_to_channel(freq))

    def test_channel_2_is_6ghz(self) -> None:
        self.assertEqual(wifi_survey._freq_to_band(5935), "6GHz")
        self.assertEqual(wifi_survey._freq_to_band(5885), "5GHz")

    def test_parsed_6ghz_bss_gets_its_channel(self) -> None:
        b = _parse_iw_scan(_IW_SCAN_SAMPLE)[0]
        self.assertEqual((b["freq_mhz"], b["channel"]), (5955, 1))


class TestParseNmcli(unittest.TestCase):
    def test_parses_two_rows(self) -> None:
        result = _parse_nmcli(_NMCLI_SAMPLE)
        self.assertEqual(len(result), 2)

    def test_bssid_unescaped(self) -> None:
        result = _parse_nmcli(_NMCLI_SAMPLE)
        bssids = [b["bssid"] for b in result]
        self.assertIn("aa:bb:cc:dd:ee:ff", bssids)

    def test_open_network(self) -> None:
        result = _parse_nmcli(_NMCLI_SAMPLE)
        open_bss = next(b for b in result if b["bssid"] == "11:22:33:44:55:66")
        self.assertEqual(open_bss["security"], "Open")
        self.assertEqual(open_bss["source"], "nmcli")

    def test_dot11_fields_none(self) -> None:
        result = _parse_nmcli(_NMCLI_SAMPLE)
        # A parser regression that returns nothing would satisfy the loop below
        # without ever looking at a field.
        self.assertEqual(len(result), 2)
        for bss in result:
            self.assertIsNone(bss["dot11k"])


class TestScannableBands(unittest.TestCase):
    def test_band_order(self) -> None:
        bss = [
            {"band": "5GHz"},
            {"band": "2.4GHz"},
            {"band": "6GHz"},
        ]
        self.assertEqual(_scannable_bands(bss), ["2.4GHz", "5GHz", "6GHz"])


class TestGetWifiSurveyNoIface(unittest.TestCase):
    def test_unavailable_when_no_iface(self) -> None:
        with patch.object(wifi_survey, "SURVEY_WIFI_IFACE", None):
            r = get_wifi_survey()
        self.assertFalse(r["available"])
        self.assertIn("SURVEY_WIFI_IFACE", r["reason"])

    def test_unavailable_when_iw_missing(self) -> None:
        with patch.object(wifi_survey, "SURVEY_WIFI_IFACE", "wlan0"), \
             patch("shutil.which", return_value=None):
            r = get_wifi_survey()
        self.assertFalse(r["available"])
