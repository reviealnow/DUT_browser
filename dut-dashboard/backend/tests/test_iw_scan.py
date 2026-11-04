"""``iw scan`` rows for the findings rules: wifi_survey's parser, scan_rows' map.

The fixture is shaped like real ``iw dev wlan0 scan`` output from a Raspberry
Pi 4 on the bench (iw 6.x), with SSIDs and BSSIDs replaced: tab-indented
sections, ``RSN:`` carrying its first item on the header line, unknown suites
printed as ``00-0f-ac:<n>``, and SSID bytes iw would not print as ``\\xNN``.
#217's real Pi capture in ``fixtures/iw_scan`` is checked here too.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from app.services import packet_rules as pr
from app.services import scan_rows
from app.services.wifi_survey import _parse_iw_scan

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import host_scan_report  # noqa: E402

SCAN = """\
BSS 3a:00:00:00:00:01(on wlan0)
\tlast seen: 1029505.429s [boottime]
\tfreq: 2412.0
\tbeacon interval: 100 TUs
\tcapability: ESS Privacy ShortSlotTime (0x1431)
\tsignal: -56.00 dBm
\tlast seen: 120 ms ago
\tSSID: dut\\x20lab\\x5c\\xe4\\xb8\\xad
\tDS Parameter set: channel 1
\tCountry: US\tEnvironment: bogus
\t\tChannels [1 - 11] @ 30 dBm
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: 00-0f-ac:9 CCMP
\t\t * Authentication suites: SAE 00-0f-ac:24
\t\t * Capabilities: 16-PTKSA-RC 1-GTKSA-RC MFP-required MFP-capable (0x00cc)
\tHT operation:
\t\t * primary channel: 1
BSS 00:00:00:00:00:02(on wlan0)
\tfreq: 2432.0
\tcapability: ESS Privacy (0x0411)
\tsignal: -48.00 dBm
\tlast seen: 900 ms ago
\tSSID: mixed
\tDS Parameter set: channel 6
\tCountry: JP\tEnvironment: Indoor/Outdoor
\tRSN:\t * Version: 1
\t\t * Group cipher: TKIP
\t\t * Pairwise ciphers: CCMP TKIP
\t\t * Authentication suites: IEEE 802.1X/SHA-256 PSK
\t\t * Capabilities: 1-PTKSA-RC 1-GTKSA-RC (0x0000)
\tWPA:\t * Version: 1
\t\t * Group cipher: TKIP
\t\t * Pairwise ciphers: CCMP TKIP
\t\t * Authentication suites: PSK
BSS 00:00:00:00:00:03(on wlan0)
\tfreq: 5200.0
\tcapability: ESS Privacy (0x0011)
\tsignal: -69.00 dBm
\tSSID: wep
\tTIM: DTIM Count 0 DTIM Period 4 Bitmap Control 0x0 Bitmap[0] 0x0
BSS 00:00:00:00:00:04(on wlan0) -- associated
\tfreq: 5200.0
\tcapability: ESS (0x0001)
\tsignal: -61.00 dBm
\tSSID: guest
\tHT operation:
\t\t * primary channel: 40
BSS 00:00:00:00:00:05(on wlan0)
\tfreq: 2412.0
\tcapability: ESS Privacy (0x0011)
\tsignal: -70.00 dBm
\tSSID: \\x00\\x00\\x00
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
\t\t * Capabilities: 1-PTKSA-RC 1-GTKSA-RC (0x0000)
"""

PI4 = (Path(__file__).parent / "fixtures" / "iw_scan" / "pi4_iw-6.9_excerpt.txt").read_text()

# The DUT's busybox iw names only ESS on the capability line and never prints a
# WPA element, so Privacy without RSN may be WEP or WPA1 there.
DUT_SHAPED = ("BSS 00:00:00:00:00:0a(on ath0)\r\n\tfreq: 2437\r\n"
              "\tcapability: ESS (0x0431)\r\n\tSSID: wpa1-or-wep\r\n")

# An RSN element whose only AKM is another vendor's suite.
VENDOR_AKM = ("BSS 00:00:00:00:00:09(on wlan0)\n\tfreq: 2412\n"
              "\tcapability: ESS Privacy (0x0011)\n\tSSID: vendor\n"
              "\tRSN:\t * Version: 1\n\t\t * Authentication suites: 00-11-22:1\n")

RULE_KEYS = ("akm", "group_cipher", "pairwise_cipher", "mfpr", "mfpc", "privacy",
             "wpa_version", "wpa_group_cipher", "wpa_pairwise_cipher", "country",
             "freq", "channel", "ssid", "beacon_interval", "dtim_period")


def parse(text):
    return scan_rows.rule_rows(_parse_iw_scan(text))


def by_ssid(rows):
    return {r["ssid"]: r for r in rows}


class RowTests(unittest.TestCase):
    def setUp(self):
        self.rows = parse(SCAN)
        self.by = by_ssid(self.rows)

    def test_every_bss_is_a_row(self):
        self.assertEqual([r["bssid"] for r in self.rows],
                         [f"{p}:00:00:00:00:0{i}" for p, i in
                          (("3a", 1), ("00", 2), ("00", 3), ("00", 4), ("00", 5))])
        self.assertEqual([r["frame"] for r in self.rows], [1, 2, 3, 4, 5])

    def test_ssid_escapes_are_undone(self):
        # Space, backslash and UTF-8 bytes all escaped by iw; all-NUL is hidden.
        self.assertIn("dut lab\\中", self.by)
        self.assertIn("", self.by)

    def test_rsn_suites_known_unknown_and_spaced_names(self):
        sae = self.by["dut lab\\中"]
        self.assertEqual(sae["akm"], [8, 24])
        self.assertEqual(sae["pairwise_cipher"], [9, 4])
        self.assertEqual(sae["group_cipher"], 4)
        self.assertIs(sae["mfpr"], True)
        self.assertIs(sae["mfpc"], True)
        # The parser splits "IEEE 802.1X/SHA-256" on its space; the map rejoins it.
        self.assertEqual(self.by["mixed"]["akm"], [5, 2])

    def test_wpa1_is_its_own_flag_not_merged_into_rsn(self):
        mixed = self.by["mixed"]
        self.assertEqual(mixed["wpa_version"], 1)
        self.assertEqual(mixed["wpa_group_cipher"], 2)
        self.assertEqual(mixed["wpa_pairwise_cipher"], [4, 2])
        self.assertEqual(mixed["akm"], [5, 2])  # RSN only; WPA1's PSK not appended
        self.assertIs(mixed["mfpr"], False)

    def test_absent_elements_are_none_not_missing(self):
        wep = self.by["wep"]
        for key in ("akm", "group_cipher", "pairwise_cipher", "mfpr", "mfpc", "country",
                    "wpa_version", "wpa_group_cipher", "wpa_pairwise_cipher"):
            self.assertIn(key, wep)
            self.assertIsNone(wep[key], key)
        self.assertIs(wep["privacy"], True)

    def test_every_rule_key_on_a_real_scan(self):
        for row in parse(PI4):
            for key in RULE_KEYS:
                self.assertIn(key, row, (row["ssid"], key))

    def test_channel_is_advertised_not_heard(self):
        # Heard at 2432 MHz (ch5), advertises ch6: the AP's own element wins.
        self.assertEqual(self.by["mixed"]["channel"], 6)
        self.assertEqual(self.by["mixed"]["freq"], 2432)
        # No element at all: fall back to the frequency.
        self.assertEqual(self.by["wep"]["channel"], 40)

    def test_country_and_timing(self):
        self.assertEqual(self.by["dut lab\\中"]["country"], "US")
        self.assertEqual(self.by["dut lab\\中"]["beacon_interval"], 100)
        self.assertEqual(self.by["wep"]["dtim_period"], 4)
        self.assertEqual(self.by["dut lab\\中"]["last_seen_ms"], 120)

    def test_rsn_with_unmapped_akms_leaves_akm_out(self):
        # None would read as "no RSN" and, with Privacy set, as WEP.
        row, = parse(VENDOR_AKM)
        self.assertNotIn("akm", row)

    def test_privacy_iw_does_not_name_leaves_wpa_out(self):
        row, = parse(DUT_SHAPED)
        self.assertIs(row["privacy"], True)
        self.assertIsNone(row["akm"])
        for key in ("wpa_version", "wpa_group_cipher", "wpa_pairwise_cipher"):
            self.assertNotIn(key, row)

    def test_garbage_does_not_raise(self):
        self.assertEqual(parse(""), [])
        self.assertEqual(parse("command failed: Network is down (-100)\n"), [])
        rows = parse("BSS 00:11:22:33:44:55(on wlan0)\n\tfreq: banana\n\tsignal:\n")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["channel"])

    def test_interface_name_guard(self):
        self.assertTrue(host_scan_report.IFACE_RE.fullmatch("wlan0"))
        for bad in ("wlan0;id", "$(id)", "wlan0\n", "", "a" * 16):
            self.assertIsNone(host_scan_report.IFACE_RE.fullmatch(bad), bad)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.by = by_ssid(parse(SCAN))

    def hit(self, row, rule):
        return [(f.severity, f.message) for f in pr.evaluate([row], [rule])]

    def test_wep_is_a_fail(self):
        (severity, message), = self.hit(self.by["wep"], "legacy_security")
        self.assertEqual(severity, "fail")
        self.assertIn("WEP", message)

    def test_wpa1_and_tkip_are_a_warning(self):
        (severity, message), = self.hit(self.by["mixed"], "legacy_security")
        self.assertEqual(severity, "warn")
        self.assertIn("WPA (v1)", message)
        self.assertIn("TKIP", message)

    def test_modern_security_is_clean(self):
        self.assertEqual(self.hit(self.by["dut lab\\中"], "legacy_security"), [])
        self.assertEqual(self.hit(self.by[""], "legacy_security"), [])

    def test_real_pi_scan(self):
        by = by_ssid(parse(PI4))
        (severity, message), = self.hit(by["AP_B"], "legacy_security")
        self.assertEqual(severity, "fail")
        self.assertIn("WEP", message)
        self.assertEqual(self.hit(by["AP_A"], "legacy_security"), [])
        self.assertEqual(self.hit(by["測試"], "legacy_security"), [])

    def test_no_wep_fail_where_iw_cannot_show_wpa(self):
        self.assertEqual(self.hit(parse(DUT_SHAPED)[0], "legacy_security"), [])
        # Privacy clear is unambiguous even there.
        open_row = parse(DUT_SHAPED.replace("0x0431", "0x0421"))[0]
        self.assertEqual([sev for sev, _ in self.hit(open_row, "open_network")], ["info"])

    def test_rsn_with_unmapped_akms_is_not_wep(self):
        self.assertEqual(self.hit(parse(VENDOR_AKM)[0], "legacy_security"), [])

    def test_open_network(self):
        self.assertEqual([sev for sev, _ in self.hit(self.by["guest"], "open_network")], ["info"])
        self.assertEqual(self.hit(self.by["wep"], "open_network"), [])
        six = dict(self.by["guest"], freq=5975)
        self.assertEqual(self.hit(six, "open_network"), [])  # six_ghz_security's job

    def test_open_6ghz_fails(self):
        text = ("BSS 00:00:00:00:00:0b(on wlan0)\n\tfreq: 5975\n"
                "\tcapability: ESS (0x0001)\n\tSSID: open6\n")
        (severity, message), = self.hit(parse(text)[0], "six_ghz_security")
        self.assertEqual(severity, "fail")
        self.assertIn("open network", message)

    def test_country_checked_only_where_expected(self):
        dut = dict(self.by["dut lab\\中"])
        self.assertEqual(self.hit(dut, "country_mismatch"), [])  # neighbour: context only
        self.assertEqual(self.hit(dict(dut, expected_country="US"), "country_mismatch"), [])
        self.assertEqual(self.hit(dict(dut, expected_country="us"), "country_mismatch"), [])
        (severity, message), = self.hit(dict(dut, expected_country="TW"), "country_mismatch")
        self.assertEqual(severity, "fail")
        self.assertIn("US", message)
        self.assertIn("TW", message)
        (severity, _), = self.hit(dict(self.by["wep"], expected_country="TW"), "country_mismatch")
        self.assertEqual(severity, "warn")  # no Country element at all

    def test_hidden_ssid_from_iw(self):
        self.assertEqual([f.rule for f in pr.evaluate([self.by[""]], ["hidden_ssid"])],
                         ["hidden_ssid"])


class OccupancyTests(unittest.TestCase):
    def test_counts_and_neighbours_on_dut_channel(self):
        rows = [
            {"bssid": "aa:00:00:00:00:01", "freq": 5660, "channel": 132, "signal": -37.0, "ssid": "dut"},
            {"bssid": "aa:00:00:00:00:02", "freq": 5660, "channel": 132, "signal": -65.0, "ssid": "root"},
            {"bssid": "aa:00:00:00:00:03", "freq": 5660, "channel": 132, "signal": -50.0, "ssid": "near"},
            {"bssid": "aa:00:00:00:00:04", "freq": 5180, "channel": 36, "signal": -40.0, "ssid": "x"},
            # Channel numbers repeat across bands: 6 GHz ch5 is not 2.4 GHz ch5.
            {"bssid": "aa:00:00:00:00:05", "freq": 5975, "channel": 5, "signal": None, "ssid": "6g"},
            {"bssid": "aa:00:00:00:00:06", "freq": 2432, "channel": 5, "signal": -70.0, "ssid": "2g"},
        ]
        out = pr.channel_occupancy(rows, ["AA:00:00:00:00:01"])
        chans = {(c["band"], c["channel"]): c for c in out["channels"]}
        self.assertEqual(chans[("5", 132)]["bss_count"], 3)
        self.assertIs(chans[("5", 132)]["dut_present"], True)
        self.assertEqual(chans[("5", 132)]["strongest_neighbour_dbm"], -50.0)  # not the DUT's -37
        self.assertIn(("6", 5), chans)
        self.assertIn(("2.4", 5), chans)
        self.assertEqual([n["ssid"] for n in out["dut_channel_neighbours"]], ["near", "root"])

    def test_scanned_6ghz_channel(self):
        text = ("BSS 00:00:00:00:00:0c(on wlan0)\n\tfreq: 5955\n"
                "\tcapability: ESS Privacy (0x0011)\n\tSSID: six\n")
        self.assertEqual(parse(text)[0]["channel"], 1)


class ReportToolTests(unittest.TestCase):
    def test_dut_matches_vap_bssids_by_first_octet_only(self):
        is_dut = host_scan_report.dut_matcher(["02:ab:cd:ef:44:55"])
        self.assertTrue(is_dut("06:ab:cd:ef:44:55"))
        self.assertTrue(is_dut("02:AB:CD:EF:44:55"))
        self.assertFalse(is_dut("02:ab:cd:ef:44:66"))
        self.assertFalse(is_dut(None))

    def test_report_flags_the_duts_country_and_only_the_dut(self):
        report = host_scan_report.build_report(
            parse(SCAN), host_scan_report.dut_matcher(["00:00:00:00:00:01"]), "TW")
        country = [f for f in report["findings"] if f["rule"] == "country_mismatch"]
        self.assertEqual([(f["group"], f["dut"]) for f in country], [("3a:00:00:00:00:01", True)])
        self.assertEqual(report["oldest_result_ms"], 900)

    def test_security_labels(self):
        label = host_scan_report.security_label
        by = by_ssid(parse(SCAN))
        self.assertEqual(label(by["wep"]), "WEP")
        self.assertEqual(label(by["guest"]), "Open")
        self.assertEqual(label(parse(DUT_SHAPED)[0]), "WEP or WPA1")

    def test_ssh_target_guard(self):
        for bad in ("host", "a@b;id", "-oProxyCommand=x@h", "-oX@h", "a@b c"):
            with self.assertRaises(SystemExit):
                host_scan_report.fetch_over_ssh(bad, "wlan0", False)
        with self.assertRaises(SystemExit):
            host_scan_report.fetch_over_ssh("a@b", "wlan0;id", False)


if __name__ == "__main__":
    unittest.main()
