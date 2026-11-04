"""`iw scan` parsing against real output, element order included.

`fixtures/iw_scan/pi4_iw-6.9_excerpt.txt` is an excerpt of a real
`sudo iw dev wlan0 scan` from a bench Raspberry Pi 4 (iw 6.9), posted on #214.
SSIDs and BSSIDs are replaced and some sub-lines trimmed; element order and
every RSN, HT and capability line are as captured. `iw` prints elements in the
order the beacon carries them, so RSN comes *before* HT here -- the case the
older hand-written fixtures in test_wifi_survey.py never exercised.

The synthetic blocks below cover what that excerpt cannot show: a WPA (v1)
block whose values differ from the RSN block, and WPA-only, OWE and
unrecognised-AKM networks.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.wifi_survey import _parse_iw_scan

PI4 = (Path(__file__).parent / "fixtures" / "iw_scan" / "pi4_iw-6.9_excerpt.txt").read_text()


def by_ssid(text: str) -> dict[str, dict]:
    return {b["ssid"]: b for b in _parse_iw_scan(text)}


def bss(ssid: str, *element_lines: str, capability: str = "ESS Privacy (0x0011)") -> str:
    """One BSS block in iw's layout: one tab per element, two per sub-line."""
    lines = [
        "BSS 02:00:00:00:00:99(on wlan0)",
        "\tfreq: 2437.0",
        f"\tcapability: {capability}",
        "\tsignal: -50.00 dBm",
        f"\tSSID: {ssid}",
        *element_lines,
    ]
    return "\n".join(lines) + "\n"


RSN_MIXED = [
    "\tRSN:\t * Version: 1",
    "\t\t * Group cipher: CCMP",
    "\t\t * Pairwise ciphers: CCMP",
    "\t\t * Authentication suites: PSK SAE",
    "\t\t * Capabilities: 1-PTKSA-RC 1-GTKSA-RC MFP-capable (0x0080)",
]


class RealPi4ScanTests(unittest.TestCase):
    """Each row here was mis-parsed before this fix."""

    @classmethod
    def setUpClass(cls):
        cls.rows = by_ssid(PI4)

    def test_three_bss(self):
        self.assertEqual(len(_parse_iw_scan(PI4)), 3)

    def test_ht_capabilities_do_not_overwrite_pmf(self):
        # RSN says MFP-capable; "HT capabilities: / Capabilities: 0x9ef" follows.
        a = self.rows["AP_A"]
        self.assertEqual(a["pmf"], "optional")
        self.assertEqual(a["security"], "WPA2/WPA3-Personal")
        self.assertEqual(a["akm"], ["PSK", "PSK/SHA-256", "SAE"])
        self.assertEqual((a["group_cipher"], a["pairwise_cipher"]), ("CCMP", ["CCMP"]))

    def test_other_fields_of_the_real_block(self):
        a = self.rows["AP_A"]
        self.assertEqual((a["freq_mhz"], a["channel"], a["band"]), (5220, 44, "5GHz"))
        self.assertEqual(a["signal_dbm"], -72.0)
        self.assertTrue(a["dot11v"])  # Extended capabilities: BSS Transition
        self.assertEqual(a["generation"], "Wi-Fi 4")

    def test_privacy_without_rsn_or_wpa_is_wep(self):
        # Not a trimming artefact: the scan's author describes AP_B as
        # "capability: ... Privacy, no RSN, no WPA element, i.e. WEP" (#214).
        b = self.rows["AP_B"]
        self.assertEqual((b["security"], b["category"]), ("WEP", "personal"))

    def test_escaped_utf8_ssid_is_decoded(self):
        self.assertIn("測試", self.rows)
        self.assertEqual(self.rows["測試"]["security"], "WPA2-Personal")
        self.assertEqual(self.rows["測試"]["pmf"], "disabled")


class BlockBoundaryTests(unittest.TestCase):
    def test_wpa_block_does_not_overwrite_rsn(self):
        text = bss("mixed", *RSN_MIXED,
                   "\tWPA:\t * Version: 1",
                   "\t\t * Group cipher: TKIP",
                   "\t\t * Pairwise ciphers: TKIP",
                   "\t\t * Authentication suites: PSK")
        row = by_ssid(text)["mixed"]
        self.assertEqual(row["akm"], ["PSK", "SAE"])
        self.assertEqual((row["group_cipher"], row["pairwise_cipher"]), ("CCMP", ["CCMP"]))
        self.assertEqual(row["security"], "WPA2/WPA3-Personal")
        self.assertEqual(row["pmf"], "optional")

    def test_rm_capabilities_do_not_overwrite_pmf(self):
        text = bss("rm", *RSN_MIXED,
                   "\tRM enabled capabilities:",
                   "\t\tCapabilities: 0x73 0xd0 0x00 0x00 0x0c",
                   "\t\t\tLink Measurement")
        row = by_ssid(text)["rm"]
        self.assertEqual(row["pmf"], "optional")
        self.assertTrue(row["dot11k"])

    def test_extended_capabilities_end_at_the_next_element(self):
        text = bss("ext",
                   "\tExtended capabilities:",
                   "\t\t * Extended Channel Switching",
                   "\tVendor specific: OUI 00:11:22, data: 01",
                   "\t\t * BSS Transition")  # not inside Extended capabilities
        self.assertFalse(by_ssid(text)["ext"]["dot11v"])


class SecurityClassificationTests(unittest.TestCase):
    def test_wpa_only_is_not_open(self):
        text = bss("wpa1",
                   "\tWPA:\t * Version: 1",
                   "\t\t * Group cipher: TKIP",
                   "\t\t * Pairwise ciphers: TKIP",
                   "\t\t * Authentication suites: PSK")
        row = by_ssid(text)["wpa1"]
        self.assertEqual((row["security"], row["category"]), ("WPA-Personal", "personal"))

    def test_wpa_only_enterprise(self):
        text = bss("wpa1e",
                   "\tWPA:\t * Version: 1",
                   "\t\t * Group cipher: TKIP",
                   "\t\t * Pairwise ciphers: TKIP",
                   "\t\t * Authentication suites: IEEE 802.1X")
        row = by_ssid(text)["wpa1e"]
        self.assertEqual((row["security"], row["category"]), ("WPA-Enterprise", "enterprise"))

    def test_owe_is_neither_wep_nor_open(self):
        # OWE sets the Privacy bit; reading Privacy alone would call it WEP.
        for akm in ("OWE", "00-0f-ac:18"):
            with self.subTest(akm=akm):
                text = bss("owe",
                           "\tRSN:\t * Version: 1",
                           "\t\t * Group cipher: CCMP",
                           "\t\t * Pairwise ciphers: CCMP",
                           f"\t\t * Authentication suites: {akm}",
                           "\t\t * Capabilities: MFP-required MFP-capable (0x00c0)")
                row = by_ssid(text)["owe"]
                self.assertEqual(row["security"], "OWE")
                self.assertEqual(row["pmf"], "required")

    def test_unrecognised_rsn_akm_is_not_wep_or_open(self):
        text = bss("odd",
                   "\tRSN:\t * Version: 1",
                   "\t\t * Group cipher: CCMP",
                   "\t\t * Pairwise ciphers: CCMP",
                   "\t\t * Authentication suites: 00-0f-ac:21")
        row = by_ssid(text)["odd"]
        self.assertNotIn(row["security"], ("WEP", "Open"))
        self.assertIn("00-0f-ac:21", row["security"])

    def test_rsn_without_akm_line_is_rsn(self):
        text = bss("noakm", "\tRSN:\t * Version: 1", "\t\t * Group cipher: CCMP")
        self.assertEqual(by_ssid(text)["noakm"]["security"], "RSN")

    def test_no_privacy_no_rsn_is_open(self):
        text = bss("open", capability="ESS ShortSlotTime (0x0401)")
        self.assertEqual(by_ssid(text)["open"]["security"], "Open")

    def test_no_capability_line_stays_open(self):
        # Existing behaviour: without a capability line Privacy is unknown.
        text = "BSS 02:00:00:00:00:98(on wlan0)\n\tfreq: 2437\n\tSSID: bare\n"
        self.assertEqual(by_ssid(text)["bare"]["security"], "Open")


class SsidEscapeTests(unittest.TestCase):
    def test_backslash_is_escaped_by_iw_and_restored(self):
        # The lab SSID from the scan: iw prints "\" as \x5c.
        text = bss(r"AP6_420E_!!\x5c>5</??")
        self.assertIn("AP6_420E_!!\\>5</??", by_ssid(text))

    def test_edge_spaces_are_escaped_by_iw_and_restored(self):
        text = bss(r"\x20lead and trail\x20")
        self.assertIn(" lead and trail ", by_ssid(text))

    def test_non_utf8_bytes_keep_their_value(self):
        # Big5 "中文", which is not valid UTF-8.
        text = bss(r"\xa4\xa4\xa4\xe5")
        self.assertIn("0xa4a4a4e5", by_ssid(text))

    def test_plain_ascii_unchanged(self):
        self.assertIn("AP_A", by_ssid(bss("AP_A")))

    def test_empty_ssid_is_empty_string(self):
        self.assertIn("", by_ssid(bss("")))

    def test_all_nul_ssid_is_hidden(self):
        # Many hidden APs send NUL bytes; iw prints them as \x00.
        rows = by_ssid(bss(r"\x00\x00\x00\x00"))
        self.assertEqual(list(rows), [""])

    def test_nul_inside_a_name_is_kept(self):
        self.assertIn("a\x00b", by_ssid(bss(r"a\x00b")))

    def test_nested_ssid_line_does_not_replace_the_bss_own(self):
        text = bss("outer", "\tSome container:", "\t\tSSID: inner")
        self.assertEqual(list(by_ssid(text)), ["outer"])


class RawElementFieldTests(unittest.TestCase):
    """The raw values kept beside the labels, for the rule adapter (scan_rows)."""

    @classmethod
    def setUpClass(cls):
        cls.rows = by_ssid(PI4)

    def test_real_block(self):
        a = self.rows["AP_A"]
        self.assertIs(a["privacy"], True)
        self.assertIs(a["rsn_present"], True)
        self.assertEqual((a["mfpr"], a["mfpc"]), (False, True))
        self.assertIsNone(a["wpa1"])
        self.assertEqual(a["country"], "AU")
        self.assertEqual(a["advertised_channel"], 44)  # HT operation
        self.assertEqual(a["beacon_interval_tu"], 100)

    def test_no_rsn_element(self):
        b = self.rows["AP_B"]
        self.assertIs(b["rsn_present"], False)
        self.assertEqual((b["mfpr"], b["mfpc"]), (None, None))
        self.assertEqual((b["country"], b["advertised_channel"]), ("US", 1))  # DS only

    def test_absent_elements_are_none(self):
        c = self.rows["測試"]
        self.assertEqual(c["capability_names"], ["ESS", "Privacy", "SpectrumMgmt", "APSD"])
        for key in ("wpa1", "country", "advertised_channel", "beacon_interval_tu",
                    "dtim_period", "last_seen_ms"):
            self.assertIn(key, c)
            self.assertIsNone(c[key], key)

    def test_wpa1_is_kept_apart_from_rsn(self):
        text = bss("mixed", *RSN_MIXED,
                   "\tWPA:\t * Version: 1",
                   "\t\t * Group cipher: TKIP",
                   "\t\t * Pairwise ciphers: CCMP TKIP",
                   "\t\t * Authentication suites: PSK")
        row = by_ssid(text)["mixed"]
        self.assertEqual(row["wpa1"], {"version": 1, "group_cipher": "TKIP",
                                       "pairwise_cipher": ["CCMP", "TKIP"], "akm": ["PSK"]})
        self.assertEqual(row["akm"], ["PSK", "SAE"])

    def test_heard_on_one_channel_advertising_another(self):
        # 2.4 GHz channels overlap: heard at 2432 MHz (ch 5), the AP says ch 6.
        text = ("BSS 02:00:00:00:00:97(on wlan0)\n\tfreq: 2432.0\n\tSSID: near\n"
                "\tDS Parameter set: channel 6\n")
        row = by_ssid(text)["near"]
        self.assertEqual((row["channel"], row["advertised_channel"]), (5, 6))

    def test_tim_last_seen_and_odd_country(self):
        text = bss("t",
                   "\tlast seen: 1060638.125s [boottime]",
                   "\tlast seen: 640 ms ago",
                   "\tTIM: DTIM Count 0 DTIM Period 3 Bitmap Control 0x0 Bitmap[0] 0x0",
                   "\tCountry: \x00\x00\tEnvironment: bogus")
        row = by_ssid(text)["t"]
        self.assertEqual((row["last_seen_ms"], row["dtim_period"]), (640, 3))
        self.assertIsNone(row["country"])

    def test_privacy_from_the_hex_when_iw_names_only_ess(self):
        # The DUT's busybox iw prints "capability: ESS (0x0431)": 0x0431 has
        # the Privacy bit (0x0010) set although the word is missing.
        # That iw prints no WPA element either, so the label cannot tell WEP
        # from WPA1 there and stays as it was; the raw bit is still exact.
        for cap, privacy, names, security in (
                ("ESS (0x0431)", True, ["ESS"], "Open"),
                ("ESS (0x0421)", False, ["ESS"], "Open"),
                ("ESS Privacy ShortSlotTime (0x0411)", True, ["ESS", "Privacy", "ShortSlotTime"], "WEP"),
                ("ESS Privacy", True, ["ESS", "Privacy"], "WEP")):  # no hex at all
            with self.subTest(cap=cap):
                row = by_ssid(bss("dut-shape", capability=cap))["dut-shape"]
                self.assertIs(row["privacy"], privacy)
                self.assertEqual(row["capability_names"], names)
                self.assertEqual(row["security"], security)

    def test_mfp_bits_from_the_hex(self):
        cases = (("1-PTKSA-RC 1-GTKSA-RC MFP-required MFP-capable (0x00c0)", True, True),
                 ("16-PTKSA-RC 1-GTKSA-RC MFP-capable (0x008c)", False, True),
                 ("1-PTKSA-RC 1-GTKSA-RC (0x0000)", False, False),
                 # Cut short on the serial console: no hex, so unknown.
                 ("16-PTKSA-RC 1-GTKSA-RC MFP-re", None, None))
        for caps, mfpr, mfpc in cases:
            with self.subTest(caps=caps):
                text = bss("m", "\tRSN:\t * Version: 1",
                           "\t\t * Authentication suites: SAE",
                           f"\t\t * Capabilities: {caps}")
                row = by_ssid(text)["m"]
                self.assertEqual((row["mfpr"], row["mfpc"]), (mfpr, mfpc))

    def test_dut_crlf_output(self):
        # The DUT's busybox iw, read over the serial console, ends lines in CRLF.
        text = PI4.replace("\n", "\r\n")
        self.assertEqual(_parse_iw_scan(text), _parse_iw_scan(PI4))


if __name__ == "__main__":
    unittest.main()
