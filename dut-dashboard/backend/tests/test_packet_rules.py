"""Packet Monitor: Test Modes, field decoding and findings rules.

The end-to-end cases read real tshark 4.4.1 output of synthetic beacons
(``fixtures/packets/``), so the decoders are tested against the format tshark
actually prints -- hex SSIDs, ``True``/``False``, ``<MISSING>`` -- not against
what we assumed it prints.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.services import packet_decode as pd
from app.services import packet_modes as pm
from app.services import packet_rules as pr

FIXTURES = Path(__file__).parent / "fixtures" / "packets"
FIELD_TYPES_441 = json.loads((FIXTURES / "field_types_tshark-4.4.1.json").read_text())
TSV_441 = (FIXTURES / "beacons_tshark-4.4.1.tsv").read_text()
US = pd.AGGREGATOR


def decoded_441(mode_id: str) -> tuple[dict[str, dict], list[dict]]:
    """Decode the 4.4.1 fixture with one mode: (rows keyed by SSID, all rows)."""
    columns = pm.resolve_columns(pm.get_mode(mode_id), FIELD_TYPES_441)
    raw_rows, dropped = pd.rows_from_tsv(TSV_441)
    assert dropped == 0, dropped
    rows = [pm.decode_row(columns, raw) for raw in raw_rows]
    return {row["ssid"]: row for row in rows if row["bssid"] != "00:03:7f:00:00:0f"}, rows


def findings_for(row: dict, mode_id: str = "beacon_security") -> list[pr.Finding]:
    mode = pm.get_mode(mode_id)
    return pr.evaluate([row], mode.rules, mode.group_by)


def rules_hit(row: dict, mode_id: str = "beacon_security") -> set[str]:
    return {f.rule for f in findings_for(row, mode_id)}


class DecodeTests(unittest.TestCase):
    def test_ssid_bytes_are_hex_in_4x(self):
        self.assertEqual(pd.decode("text", "6f70656e", "FT_BYTES"), "open")

    def test_non_utf8_ssid_keeps_its_bytes(self):
        # Big5 "中文": no replacement characters, no guessing; exact hex instead.
        self.assertEqual(pd.decode("text", "a4a4a4e5", "FT_BYTES"), "0xa4a4a4e5")

    def test_ssid_string_is_kept_verbatim_in_3x(self):
        # A 3.6 FT_STRING SSID that happens to look like hex must stay text.
        self.assertEqual(pd.decode("text", "cafe", "FT_STRING"), "cafe")

    def test_missing_means_empty_not_absent(self):
        self.assertEqual(pd.decode("text", "<MISSING>", "FT_BYTES"), "")
        self.assertIsNone(pd.decode("text", None, "FT_BYTES"))
        self.assertIsNone(pd.decode("text", "", "FT_BYTES"))

    def test_bool_accepts_both_spellings(self):
        self.assertIs(pd.decode("bool", "True"), True)
        self.assertIs(pd.decode("bool", "False"), False)
        self.assertIs(pd.decode("bool", "1"), True)
        self.assertIs(pd.decode("bool", "0"), False)
        self.assertIsNone(pd.decode("bool", "maybe"))

    def test_lists_and_bad_values(self):
        self.assertEqual(pd.decode("akm_list", f"2{US}8"), [2, 8])
        self.assertIsNone(pd.decode("akm_list", f"2{US}x"))
        self.assertIsNone(pd.decode("akm_list", f"2{US}{US}8"))
        self.assertIsNone(pd.decode("int", "abc"))
        self.assertIsNone(pd.decode("int", "1_000"))
        self.assertEqual(pd.decode("int", "0x10"), 16)
        self.assertIsNone(pd.decode("float", "nan"))
        self.assertIsNone(pd.decode("float", "inf"))
        self.assertIsNone(pd.decode("text", "zz", "FT_BYTES"))

    def test_text_with_comma_is_one_value(self):
        self.assertEqual(pd.decode("text", "a,b", "FT_STRING"), "a,b")
        self.assertEqual(pd.decode("text", "612c62", "FT_BYTES"), "a,b")

    def test_repeated_ssid_takes_the_first(self):
        # Multiple BSSID beacons carry more than one SSID element.
        self.assertEqual(pd.decode("text", f"6f70656e{US}6f70", "FT_BYTES"), "open")
        self.assertEqual(pd.decode("text", f"<MISSING>{US}6f70", "FT_BYTES"), "")

    def test_rates(self):
        self.assertEqual(pd.decode("rates", f"0x82{US}0x0c"), [(1.0, True), (6.0, False)])
        # 0xfb is the SAE H2E membership selector, not a 61.5 Mbps basic rate.
        self.assertEqual(pd.decode("rates", f"0x8c{US}0xfb"), [(6.0, True)])
        self.assertEqual(pd.format_value("rates", [(5.5, True), (6.0, False)]), "5.5(B), 6")

    def test_format(self):
        self.assertEqual(pd.format_value("akm_list", [2, 8]), "PSK / SAE")
        self.assertEqual(pd.format_value("akm_list", [22, 23]), "FT-802.1X-SHA384 / 802.1X-SHA384")
        self.assertEqual(pd.format_value("cipher", 4), "CCMP-128")
        self.assertEqual(pd.format_value("cipher", 99), "Cipher 99")
        self.assertEqual(pd.format_value("bool", False), "No")
        self.assertEqual(pd.format_value("text", ""), "(hidden)")
        self.assertEqual(pd.format_value("text", "\x00\x00"), "(hidden)")
        self.assertEqual(pd.format_value("int", None), "—")

    def test_band(self):
        self.assertEqual(pd.band_of(2437), "2.4")
        self.assertEqual(pd.band_of(5180), "5")
        self.assertEqual(pd.band_of(5975), "6")
        self.assertIsNone(pd.band_of(None))

    def test_rows_from_tsv_drops_and_counts_misaligned_lines(self):
        rows, dropped = pd.rows_from_tsv("a\tb\n1\t\n1\t2\t3\n")
        self.assertEqual(rows, [{"a": "1", "b": None}])
        self.assertEqual(dropped, 1)

    def test_rows_from_tsv_splits_on_newline_only(self):
        rows, dropped = pd.rows_from_tsv("a\tb\r\nx y\tz\x0bw\r\n")
        self.assertEqual(rows, [{"a": "x y", "b": "z\x0bw"}])
        self.assertEqual(dropped, 0)


class ModeTests(unittest.TestCase):
    def test_every_mode_field_name_is_safe(self):
        for mode in pm.MODES:
            for column in mode.columns:
                for name in column.fields:
                    self.assertTrue(pm.FIELD_RE.fullmatch(name), name)

    def test_trailing_newline_is_not_a_field_name(self):
        with self.assertRaises(ValueError):
            pm.Column(key="x", label="x", fields=["wlan.ssid\n"])

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            pm.get_mode("nope")

    def test_bad_definitions_are_refused(self):
        frame = pm.Column(key="frame", label="No.", fields=["frame.number"], decoder="int")
        with self.assertRaises(ValueError):
            pm.Column(key="x", label="x", fields=["wlan.ssid; rm -rf ~"])
        with self.assertRaises(ValueError):
            pm.TestMode(id="m", label="m", description="", display_filter="",
                        columns=[frame], rules=["no_such_rule"])
        with self.assertRaises(ValueError):
            pm.TestMode(id="m", label="m", description="", display_filter="",
                        columns=[frame], group_by="bssid")
        with self.assertRaises(ValueError):  # a group rule with nothing to group by
            pm.TestMode(id="m", label="m", description="", display_filter="",
                        columns=[frame], rules=["bss_config_changed"])
        with self.assertRaises(ValueError):
            pm.TestMode(id="m", label="m", description="", display_filter="",
                        columns=[frame], rules=["hidden_ssid", "hidden_ssid"])

    def test_version_rename_resolves_to_whichever_exists(self):
        manual = pm.get_mode("manual")
        new = {c.key: c for c in pm.resolve_columns(manual, FIELD_TYPES_441 | {
            "_ws.col.protocol": "FT_STRING"})}
        old = {c.key: c for c in pm.resolve_columns(manual, {
            "frame.number": "FT_UINT32", "_ws.col.Protocol": "FT_STRING"})}
        self.assertEqual(new["protocol"].fields, (("_ws.col.protocol", "FT_STRING"),))
        self.assertEqual(old["protocol"].fields, (("_ws.col.Protocol", "FT_STRING"),))

    def test_unresolved_column_is_absent_not_none(self):
        mode = pm.get_mode("beacon_security")
        columns = pm.resolve_columns(mode, {"frame.number": "FT_UINT32", "wlan.bssid": "FT_ETHER"})
        row = pm.decode_row(columns, {"frame.number": "1", "wlan.bssid": "00:03:7f:00:00:01"})
        self.assertEqual(set(row), {"frame", "bssid"})
        self.assertEqual(findings_for(row), [])

    def test_per_frame_fallback_takes_first_value(self):
        mode = pm.get_mode("beacon_security")
        columns = pm.resolve_columns(mode, FIELD_TYPES_441)
        row = pm.decode_row(columns, {"wlan.ht.info.primarychannel": "11",
                                      "wlan.ds.current_channel": "6"})
        self.assertEqual(row["channel"], 11)

    def test_advertised_channel_beats_the_sniffer_channel(self):
        mode = pm.get_mode("beacon_security")
        columns = pm.resolve_columns(mode, FIELD_TYPES_441)
        row = pm.decode_row(columns, {"wlan_radio.channel": "5", "wlan.ds.current_channel": "6"})
        self.assertEqual((row["channel"], row["captured_on"]), (6, 5))
        # Radio header only: it is the last resort, not nothing.
        row = pm.decode_row(columns, {"wlan_radio.channel": "5"})
        self.assertEqual(row["channel"], 5)

    def test_structure_helpers_do_not_leak_into_rows(self):
        mode = pm.get_mode("beacon_security")
        columns = pm.resolve_columns(mode, FIELD_TYPES_441)
        row = pm.decode_row(columns, {"wlan.tag.number": f"0{US}48",
                                      "wlan.rsn.akms.count": "1", "wlan.rsn.akms.type": "8"})
        self.assertFalse({"tag_numbers", "akm_count", "pcs_count"} & set(row))

    def test_counts_that_do_not_match_the_list_are_not_trusted(self):
        mode = pm.get_mode("beacon_security")
        columns = pm.resolve_columns(mode, FIELD_TYPES_441)
        row = pm.decode_row(columns, {"wlan.tag.number": f"0{US}48",
                                      "wlan.rsn.akms.count": "1",
                                      "wlan.rsn.akms.type": f"8{US}2",
                                      "wlan.rsn.capabilities.mfpr": "False"})
        self.assertNotIn("akm", row)
        self.assertNotIn("mfpr", row)
        self.assertIs(row[pm.RSN_UNRESOLVED], True)

    def test_extraction_fields_are_unique(self):
        mode = pm.get_mode("beacon_security")
        fields = pm.extraction_fields(pm.resolve_columns(mode, FIELD_TYPES_441))
        self.assertEqual(len(fields), len(set(fields)))
        self.assertIn("wlan.rsn.akms.type", fields)


class RealTsharkOutputTests(unittest.TestCase):
    """Rules over the tshark 4.4.1 fixture: one beacon per case."""

    @classmethod
    def setUpClass(cls):
        cls.rows, cls.all_rows = decoded_441("beacon_security")
        cls.basic, _ = decoded_441("wifi_basic")

    def test_fixture_decodes(self):
        row = self.rows["wpa3sae_ok"]
        self.assertEqual(row["akm"], [8])
        self.assertIs(row["mfpr"], True)
        self.assertEqual(row["channel"], 6)
        self.assertEqual(row["group_cipher"], 4)
        self.assertEqual(self.rows["transition"]["akm"], [2, 8])
        self.assertEqual(self.rows["sae_tkip"]["pairwise_cipher"], [2, 4])
        self.assertIsNone(self.rows["open"]["akm"])
        self.assertIn("AP7_中文 sp\tc", self.rows)

    def test_clean_configurations_have_no_findings(self):
        for ssid in ("open", "wpa2psk", "wpa3sae_ok", "6g_sae_ok", "AP7_中文 sp\tc",
                     "owe_ok", "6g_enterprise", "comma,ssid", "6g_ent_ft", "0xa4a4a4e5",
                     "mb_sae_only", "mb_6g_two"):
            with self.subTest(ssid=ssid):
                self.assertEqual(findings_for(self.rows[ssid]), [])

    def test_transition_mode_is_not_a_finding(self):
        # PSK + SAE with MFPC=1, MFPR=0 is WPA3 transition mode: valid by spec.
        self.assertEqual(findings_for(self.rows["transition"]), [])

    def test_sae_only_without_mfpr(self):
        self.assertEqual(rules_hit(self.rows["wpa3sae_nomfpr"]), {"wpa3_requires_mfpr"})

    def test_owe_without_mfpr(self):
        self.assertEqual(rules_hit(self.rows["owe_nomfpr"]), {"wpa3_requires_mfpr"})

    def test_transition_without_mfpc(self):
        self.assertEqual(rules_hit(self.rows["trans_nomfpc"]), {"sae_requires_mfpc"})

    def test_six_ghz_enterprise_transition(self):
        findings = findings_for(self.rows["6g_ent_trans"])
        self.assertEqual({f.rule for f in findings}, {"six_ghz_security"})
        self.assertIn("802.1X", findings[0].message)

    # --- Multiple BSSID: nontransmitted profiles must not leak into the row ---

    def test_mbssid_keeps_the_transmitting_bss_rsn(self):
        row = self.rows["mb_sae_only"]
        self.assertEqual(row["akm"], [8])  # not [8, 2] from the PSK profile
        self.assertIs(row["mfpr"], True)
        two = self.rows["mb_6g_two"]
        self.assertEqual((two["akm"], two["pairwise_cipher"]), ([8, 24], [4, 8]))

    def test_mbssid_does_not_hide_a_missing_mfpr(self):
        # Merged with the PSK profile this read as transition mode and passed.
        self.assertEqual(rules_hit(self.rows["mb_sae_nomfpr"]), {"wpa3_requires_mfpr"})

    def test_mbssid_open_transmitting_bss_is_not_judged(self):
        row = self.rows["mb_open_tx"]
        self.assertFalse(set(pm.RSN_KEYS) & set(row))  # not "SAE" from the profile
        self.assertEqual(rules_hit(row), {"rsn_not_evaluated"})
        self.assertEqual(findings_for(row)[0].severity, "info")

    def test_sniffer_channel_change_is_not_a_bss_change(self):
        mode = pm.get_mode("beacon_security")
        same_ap = [r for r in self.all_rows if r["ssid"] == "offchan"]
        self.assertEqual(sorted(r["captured_on"] for r in same_ap), [5, 6])
        findings = pr.evaluate(same_ap, mode.rules, mode.group_by)
        self.assertEqual(findings, [])

    def test_mfpr_without_mfpc(self):
        self.assertEqual(rules_hit(self.rows["mfpr_nomfpc"]), {"mfpr_without_mfpc"})

    def test_sae_with_tkip(self):
        findings = findings_for(self.rows["sae_tkip"])
        self.assertEqual({f.rule for f in findings}, {"no_legacy_cipher_with_wpa3"})
        self.assertIn("TKIP", findings[0].message)

    def test_six_ghz_psk(self):
        findings = findings_for(self.rows["6g_psk"])
        self.assertEqual({f.rule for f in findings}, {"six_ghz_security"})
        self.assertIn("PSK", findings[0].message)
        self.assertIn("MFPR = 0", findings[0].message)

    def test_hidden_ssids(self):
        self.assertEqual(rules_hit(self.basic[""], "wifi_basic"), {"hidden_ssid"})
        self.assertEqual(rules_hit(self.basic["\x00\x00\x00\x00"], "wifi_basic"), {"hidden_ssid"})
        self.assertEqual(rules_hit(self.basic["wpa2psk"], "wifi_basic"), set())

    def test_beacon_timing(self):
        findings = findings_for(self.basic["oddinterval"], "wifi_basic")
        self.assertEqual([f.rule for f in findings], ["beacon_timing"])
        self.assertIn("224 TU", findings[0].message)
        self.assertIn("DTIM period is 24", findings[0].message)

    def test_bss_config_change_across_beacons(self):
        mode = pm.get_mode("beacon_security")
        flappy = [r for r in self.all_rows if r["bssid"] == "00:03:7f:00:00:0f"]
        findings = pr.evaluate(flappy, mode.rules, mode.group_by)
        changed = [f for f in findings if f.rule == "bss_config_changed"]
        self.assertEqual({f.message.split(" ")[0] for f in changed}, {"akm", "mfpr"})
        frames = sorted(r["frame"] for r in flappy)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f.severity == "warn" and f.frames == frames for f in changed))


class EvaluateTests(unittest.TestCase):
    BAD = {"frame": 1, "bssid": "aa:aa:aa:aa:aa:01", "akm": [8], "mfpr": False, "mfpc": True,
           "group_cipher": 4, "pairwise_cipher": [4], "freq": 2437}

    def test_identical_findings_collapse_per_group(self):
        rows = [dict(self.BAD, frame=n) for n in (3, 1, 2)]
        rows.append(dict(self.BAD, frame=9, bssid="aa:aa:aa:aa:aa:02"))
        findings = pr.evaluate(rows, ["wpa3_requires_mfpr"], "bssid")
        self.assertEqual([(f.group, f.frames) for f in findings],
                         [("aa:aa:aa:aa:aa:01", [1, 2, 3]), ("aa:aa:aa:aa:aa:02", [9])])

    def test_severity_order(self):
        row = dict(self.BAD, beacon_interval=200, dtim_period=1)
        severities = [f.severity for f in pr.evaluate([row], ["beacon_timing", "wpa3_requires_mfpr"])]
        self.assertEqual(severities, ["fail", "info"])

    def test_group_rules_need_a_group_key(self):
        rows = [dict(self.BAD, frame=1), dict(self.BAD, frame=2, bssid="x", akm=[2])]
        self.assertEqual(pr.evaluate(rows, ["bss_config_changed"]), [])

    def test_rows_without_a_group_value_are_not_pooled(self):
        rows = [dict(self.BAD, frame=1, bssid=None, ssid="A"),
                dict(self.BAD, frame=2, bssid=None, ssid="B")]
        self.assertEqual(pr.evaluate(rows, ["bss_config_changed"], "bssid"), [])

    def test_reordered_list_is_not_a_change(self):
        rows = [dict(self.BAD, frame=1, akm=[2, 8]), dict(self.BAD, frame=2, akm=[8, 2])]
        self.assertEqual(pr.evaluate(rows, ["bss_config_changed"], "bssid"), [])

    def test_key_missing_from_some_rows_is_skipped(self):
        first = dict(self.BAD, frame=1, ssid="A")
        second = {k: v for k, v in dict(self.BAD, frame=2).items() if k != "ssid"}
        self.assertEqual(pr.evaluate([first, second], ["bss_config_changed"], "bssid"), [])

    def test_non_text_group_values_sort(self):
        rows = [dict(self.BAD, frame=1, channel=6), dict(self.BAD, frame=2, channel=None),
                dict(self.BAD, frame=3, channel=11)]
        findings = pr.evaluate(rows, ["wpa3_requires_mfpr"], "channel")
        self.assertEqual([f.group for f in findings], [None, 11, 6])

    def test_duplicate_rule_ids_do_not_double_frames(self):
        findings = pr.evaluate([self.BAD], ["wpa3_requires_mfpr", "wpa3_requires_mfpr"])
        self.assertEqual(findings[0].frames, [1])

    def test_unknown_rule(self):
        with self.assertRaises(ValueError):
            pr.evaluate([], ["nope"])

    def test_six_ghz_from_he_operation_without_radio_header(self):
        row = dict(self.BAD, freq=None, six_ghz_primary=37, akm=[2], mfpr=False)
        self.assertIn("six_ghz_security", {f.rule for f in pr.evaluate([row], ["six_ghz_security"])})

    def test_six_ghz_open(self):
        row = dict(self.BAD, freq=5975, akm=None, mfpr=None)
        findings = pr.evaluate([row], ["six_ghz_security"])
        self.assertIn("open network", findings[0].message)

    def test_sae_ext_key_family_counts_as_wpa3(self):
        row = dict(self.BAD, akm=[24, 25])
        self.assertEqual({f.rule for f in pr.evaluate([row], ["wpa3_requires_mfpr"])},
                         {"wpa3_requires_mfpr"})

    def test_to_dict(self):
        finding = pr.evaluate([self.BAD], ["wpa3_requires_mfpr"], "bssid")[0]
        self.assertEqual(set(finding.to_dict()), {"rule", "severity", "message", "group", "frames"})


if __name__ == "__main__":
    unittest.main()
