"""Unit tests for SSID capability parsers (wifi_clients.py).

Real AP6 840E sample data used as fixtures — command format verified 2026-06.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.serial.serial_worker import SerialWorker
from app.services.capability_report import build_capability_report
from app.services.wifi_clients import (
    _HOSTAPD_CONF_CMD,
    get_ssid_capabilities,
    _band_from_freq,
    _classify_security,
    _derive_generation,
    _parse_hostapd_confs,
    _parse_iw_dev,
    _parse_iwconfig_modes,
)

_IW_DEV_SAMPLE = """\
phy#2
\tInterface ath16
\t\tifindex 91
\t\twdev 0x200000006
\t\taddr ca:4f:86:25:6a:58
\t\tssid AP6_6GHzWPA3KEY
\t\ttype AP
\t\tchannel 1 (5955 MHz), width: 80 MHz, center1: 5985 MHz
\t\ttxpower 13.00 dBm
\tInterface wifi2
\t\tifindex 10
\t\twdev 0x200000001
\t\taddr c8:4f:86:91:47:e3
\t\ttype AP
\t\tchannel 1 (5955 MHz), width: 80 MHz, center1: 5985 MHz
phy#1
\tInterface ath8
\t\tifindex 87
\t\twdev 0x100000019
\t\taddr c8:4f:86:91:47:e2
\t\tssid !9018_usageInsight
\t\ttype AP
\t\tchannel 36 (5180 MHz), width: 40 MHz, center1: 5190 MHz
\t\ttxpower 25.00 dBm
\tInterface wifi1
\t\tifindex 8
\t\ttype AP
phy#0
\tInterface ath0
\t\tifindex 82
\t\taddr c8:4f:86:91:47:e1
\t\tssid !9018_usageInsight
\t\ttype AP
\t\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
"""

_IWCONFIG_SAMPLE = """\
ath8      IEEE 802.11axa  ESSID:"!9018_usageInsight"
          Mode:Master  Frequency:5.18 GHz (Channel 36)  Access Point: C8:4F:86:91:47:E2
ath16     IEEE 802.11axa  ESSID:"AP6_6GHzWPA3KEY"
          Mode:Master  Frequency:5.955 GHz (Channel 1)  Access Point: CA:4F:86:25:6A:58
ath0      IEEE 802.11axg  ESSID:"!9018_usageInsight"
          Mode:Master  Frequency:2.437 GHz (Channel 6)  Access Point: C8:4F:86:91:47:E1
"""

_HOSTAPD_CONF_DUMP = """\
====CONF====/etc/hostapd21.conf
interface=ath8
wpa=2
wpa_key_mgmt=SAE
wpa_pairwise=CCMP
ieee80211w=2
====CONF====/etc/hostapd23.conf
interface=ath10
wpa=2
wpa_key_mgmt=WPA-EAP-SUITE-B-192
wpa_pairwise=GCMP-256
group_mgmt_cipher=BIP-GMAC-256
ieee80211w=2
====CONF====/etc/hostapd22.conf
interface=ath9
wpa=2
wpa_key_mgmt=WPA-EAP
wpa_pairwise=CCMP
ieee80211w=0
ieee8021x=1
====CONF====/etc/hostapd31.conf
interface=ath16
wpa=2
wpa_key_mgmt=SAE
wpa_pairwise=CCMP
ieee80211w=2
ieee80211ax=1
"""


class TestParseIwDev(unittest.TestCase):
    def test_parses_ap_ifaces_only(self) -> None:
        result = _parse_iw_dev(_IW_DEV_SAMPLE)
        self.assertIn("ath16", result)
        self.assertIn("ath8", result)
        self.assertIn("ath0", result)
        # wifi2 has no ssid — excluded
        self.assertNotIn("wifi2", result)

    def test_bssid_freq_channel(self) -> None:
        r = _parse_iw_dev(_IW_DEV_SAMPLE)
        self.assertEqual(r["ath16"]["bssid"], "ca:4f:86:25:6a:58")
        self.assertEqual(r["ath16"]["freq_mhz"], 5955)
        self.assertEqual(r["ath16"]["channel"], 1)
        self.assertEqual(r["ath16"]["width_mhz"], 80)
        self.assertEqual(r["ath8"]["freq_mhz"], 5180)
        self.assertEqual(r["ath0"]["freq_mhz"], 2437)

    def test_ssid_preserved(self) -> None:
        r = _parse_iw_dev(_IW_DEV_SAMPLE)
        self.assertEqual(r["ath8"]["ssid"], "!9018_usageInsight")
        self.assertEqual(r["ath16"]["ssid"], "AP6_6GHzWPA3KEY")


class TestParseIwconfigModes(unittest.TestCase):
    def test_modes_extracted(self) -> None:
        r = _parse_iwconfig_modes(_IWCONFIG_SAMPLE)
        self.assertEqual(r["ath8"], "802.11axa")
        self.assertEqual(r["ath16"], "802.11axa")
        self.assertEqual(r["ath0"], "802.11axg")

    def test_empty_input(self) -> None:
        self.assertEqual(_parse_iwconfig_modes(""), {})


class TestParseHostapdConfs(unittest.TestCase):
    def test_sae_wpa3_personal(self) -> None:
        r = _parse_hostapd_confs(_HOSTAPD_CONF_DUMP)
        ath8 = r["ath8"]
        self.assertEqual(ath8["wpa_key_mgmt"], ["SAE"])
        self.assertEqual(ath8["wpa_pairwise"], ["CCMP"])
        self.assertEqual(ath8["ieee80211w"], 2)
        # assertIs, not assertFalse: the frontend types this field boolean|null
        # and renders null as "—" (unknown) rather than "✗" (not supported), so
        # a parser that started returning None here must fail this test.
        self.assertIs(ath8["dot11r"], False)

    def test_suite_b_enterprise(self) -> None:
        r = _parse_hostapd_confs(_HOSTAPD_CONF_DUMP)
        ath10 = r["ath10"]
        self.assertIn("WPA-EAP-SUITE-B-192", ath10["wpa_key_mgmt"])
        self.assertEqual(ath10["group_mgmt_cipher"], "BIP-GMAC-256")
        self.assertEqual(ath10["ieee80211w"], 2)

    def test_eap_enterprise_no_pmf(self) -> None:
        r = _parse_hostapd_confs(_HOSTAPD_CONF_DUMP)
        ath9 = r["ath9"]
        self.assertIn("WPA-EAP", ath9["wpa_key_mgmt"])
        self.assertEqual(ath9["ieee80211w"], 0)

    def test_commented_lines_ignored(self) -> None:
        dump = "====CONF====/etc/hostapd1.conf\ninterface=ath0\n#wpa_key_mgmt=WPA-PSK\nwpa_key_mgmt=SAE\n"
        r = _parse_hostapd_confs(dump)
        self.assertEqual(r["ath0"]["wpa_key_mgmt"], ["SAE"])


@unittest.skipIf(shutil.which("sh") is None, "needs a POSIX sh")
class TestHostapdConfCommand(unittest.TestCase):
    """Run the dump command through a real shell. The parser tests above feed
    `_parse_hostapd_confs` its input directly and never see the command, so
    they passed while the DUT shell was asked to cat '"/etc/hostapd1.conf"'."""

    def test_dump_reads_every_conf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "hostapd1.conf").write_text("interface=ath0\nwpa_key_mgmt=SAE\nieee80211w=2\n")
            Path(tmp, "hostapd2.conf").write_text("interface=ath1\nwpa_key_mgmt=WPA-PSK\n")
            cmd = _HOSTAPD_CONF_CMD.replace("/etc/", tmp + "/")
            out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False)
        self.assertEqual(out.stderr, "")
        r = _parse_hostapd_confs(out.stdout)
        self.assertEqual(set(r), {"ath0", "ath1"})
        self.assertEqual(r["ath0"]["wpa_key_mgmt"], ["SAE"])
        self.assertEqual(r["ath0"]["ieee80211w"], 2)


class TestDeriveGeneration(unittest.TestCase):
    def test_6e(self) -> None:
        self.assertEqual(_derive_generation("802.11axa", 5955), "Wi-Fi 6E")

    def test_wifi6_5g(self) -> None:
        self.assertEqual(_derive_generation("802.11axa", 5180), "Wi-Fi 6")

    def test_wifi6_2g(self) -> None:
        self.assertEqual(_derive_generation("802.11axg", 2437), "Wi-Fi 6")

    def test_wifi5(self) -> None:
        self.assertEqual(_derive_generation("802.11ac", 5180), "Wi-Fi 5")

    def test_wifi4(self) -> None:
        self.assertEqual(_derive_generation("802.11n", 2437), "Wi-Fi 4")

    def test_wifi7(self) -> None:
        self.assertEqual(_derive_generation("802.11be", 6000), "Wi-Fi 7")


class TestClassifySecurity(unittest.TestCase):
    def test_wpa3_personal(self) -> None:
        s, cat, pmf = _classify_security(["SAE"], 2)
        self.assertEqual(s, "WPA3-Personal")
        self.assertEqual(cat, "personal")
        self.assertEqual(pmf, "required")

    def test_wpa2_enterprise(self) -> None:
        s, cat, pmf = _classify_security(["WPA-EAP"], 0)
        self.assertEqual(s, "WPA2-Enterprise")
        self.assertEqual(cat, "enterprise")
        self.assertEqual(pmf, "disabled")

    def test_wpa3_enterprise_192(self) -> None:
        s, cat, pmf = _classify_security(["WPA-EAP-SUITE-B-192"], 2)
        self.assertEqual(s, "WPA3-Enterprise-192")
        self.assertEqual(cat, "enterprise")
        self.assertEqual(pmf, "required")

    def test_open(self) -> None:
        s, cat, pmf = _classify_security([], 0)
        self.assertEqual(s, "Open")
        self.assertIsNone(cat)

    def test_wpa2_wpa3_transition(self) -> None:
        s, cat, _ = _classify_security(["WPA-PSK", "SAE"], 1)
        self.assertEqual(s, "WPA2/WPA3-Personal")


class TestBandFromFreq(unittest.TestCase):
    def test_6ghz(self) -> None:
        self.assertEqual(_band_from_freq(5955), "6GHz")

    def test_5ghz(self) -> None:
        self.assertEqual(_band_from_freq(5180), "5GHz")

    def test_24ghz(self) -> None:
        self.assertEqual(_band_from_freq(2437), "2.4GHz")

    def test_none(self) -> None:
        self.assertIsNone(_band_from_freq(None))


class _StubParser:
    def reset(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def feed(self, line: str) -> None:
        pass


class _CannedWorker(SerialWorker):
    """capture_command double: the samples above, with a chosen conf dump."""

    def __init__(self, conf_dump: str) -> None:
        super().__init__(_StubParser())
        self._conf_dump = conf_dump

    def capture_command(self, cmd: str, timeout: float = 6.0) -> str:
        if cmd == "iw dev":
            return _IW_DEV_SAMPLE
        if cmd == "iwconfig":
            return _IWCONFIG_SAMPLE
        if cmd == _HOSTAPD_CONF_CMD:
            return self._conf_dump
        raise AssertionError(f"unexpected command {cmd!r}")


class TestSecurityWithoutConf(unittest.TestCase):
    """A VAP whose hostapd conf was not read is unknown, never Open (#223)."""

    def caps(self, conf_dump: str) -> dict[str, dict]:
        return {c["iface"]: c for c in get_ssid_capabilities(_CannedWorker(conf_dump))}

    def test_vap_without_a_conf_is_unknown(self) -> None:
        # The dump names ath8 and ath16 but not ath0.
        caps = self.caps(_HOSTAPD_CONF_DUMP)
        self.assertEqual((caps["ath8"]["security"], caps["ath8"]["pmf"]), ("WPA3-Personal", "required"))
        self.assertEqual(caps["ath16"]["category"], "personal")
        for key in ("security", "category", "pmf"):
            self.assertIsNone(caps["ath0"][key], key)

    def test_failed_dump_leaves_every_vap_unknown(self) -> None:
        # What the bench AP6_420 returned before #222: every cat failed.
        failed = ('====CONF===="/etc/hostapd1.conf"\n'
                  "cat: can't open '\"/etc/hostapd1.conf\"': No such file or directory\n")
        for c in self.caps(failed).values():
            self.assertIsNone(c["security"], c["iface"])
            self.assertIsNone(c["pmf"], c["iface"])

    def test_a_read_conf_without_wpa_is_still_open(self) -> None:
        caps = self.caps("====CONF====/etc/hostapd1.conf\ninterface=ath0\nssid=guest\n")
        self.assertEqual((caps["ath0"]["security"], caps["ath0"]["pmf"]), ("Open", "disabled"))


class TestUnknownConfigIsNotADiff(unittest.TestCase):
    def row(self, security, pmf) -> dict:
        ssids = [{"iface": "ath0", "bssid": "02:00:00:00:00:01", "band": "5GHz",
                  "security": security, "pmf": pmf, "generation": "Wi-Fi 6"}]
        survey = {"available": True, "scannable_bands": ["5GHz"],
                  "bss": [{"bssid": "02:00:00:00:00:01", "security": "WPA2-Personal",
                           "pmf": "optional", "generation": "Wi-Fi 6"}]}
        return build_capability_report(ssids, survey)["rows"][0]

    def report(self, security, pmf) -> list[dict]:
        return self.row(security, pmf)["diffs"]

    def test_unknown_fields_are_named_on_the_row(self) -> None:
        # The card must not show a clean match for a row whose security was
        # never compared; `unknown` is what lets it tell the two apart.
        row = self.row(None, None)
        self.assertTrue(row["match"])
        self.assertEqual(row["diffs"], [])
        self.assertEqual(row["unknown"], ["security", "pmf"])

    def test_a_known_config_has_nothing_unknown(self) -> None:
        self.assertEqual(self.row("WPA2-Personal", "optional")["unknown"], [])
        self.assertEqual(self.row("Open", "disabled")["unknown"], [])

    def test_unknown_security_and_pmf_are_not_diffs(self) -> None:
        self.assertEqual(self.report(None, None), [])

    def test_a_known_mismatch_is_still_a_diff(self) -> None:
        self.assertEqual([d["field"] for d in self.report("Open", "disabled")], ["security", "pmf"])
