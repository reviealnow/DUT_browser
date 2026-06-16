from __future__ import annotations

import unittest

from app.services.wifi_clients import (
    band_for_iface,
    discover_vaps,
    parse_apstats,
    parse_wlanconfig_list,
    signal_pct,
    vendor_for_mac,
    width_from_phymode,
)

# Real `apstats -s -m <MAC>` dump (AP6 840E, trimmed to the fields we surface).
APSTATS = """Node Level Stats: d6:0d:0f:42:6f:c7 (under VAP ath32)
Tx Data Bytes                   = 27196107
Average Tx Rate (kbps)          = 864700
Average Rx Rate (kbps)          = 1020800
Last Packet Error Rate (PER)    = 0
Rx Data Bytes                   = 1473977
Rx RSSI                         = 38
Band Width                      = 80
chainmask (NSS)                 tx(2) rx(2)
Tx bytes for last one second    = 4163
Rx bytes for last one second    = 4494
"""

# Real capture from an AP6 840E (phone on 6 GHz), trimmed verbose tail.
WLANCONFIG_ATH32 = """wlanconfig ath32 list
ADDR               AID CHAN TXRATE RXRATE RSSI  ANT_RSSI MINRSSI MAXRSSI IDLE  TXSEQ  RXSEQ  CAPS XCAPS ACAPS     ERP    STATE MAXRATE(DOT11) HTCAPS   VHTCAPS ASSOCTIME    IEs   MODE RXNSS TXNSS                   PSMODE
d6:0d:0f:42:6f:c7    4   93 864M   1080M  -55  40/40/40     -61     -54    0      0   65535   EPR   EBO NULL    0          3        1201000               Q              00 00:01:40     RSN WME IEEE80211_MODE_11AXA_HE80  2 2   0
      LM NR BRT
 RSSI is combined over chains in dBm
 SNR\t\t\t\t: 40
 Operating band\t\t\t: 6GHz
 Max STA phymode\t\t: IEEE80211_MODE_11AXA_HE80
"""

WLANCONFIG_EMPTY = """wlanconfig ath0 list
ADDR               AID CHAN TXRATE RXRATE RSSI  ANT_RSSI MINRSSI MAXRSSI IDLE  TXSEQ  RXSEQ  CAPS XCAPS ACAPS     ERP    STATE MAXRATE(DOT11) HTCAPS   VHTCAPS ASSOCTIME    IEs   MODE RXNSS TXNSS                   PSMODE
"""

IWCONFIG = """ath0      IEEE 802.11axg  ESSID:"!!3290-1"
          Mode:Master  Frequency:2.437 GHz (Channel 6)  Access Point: C8:4F:86:95:CE:E4
          Bit Rate:573.5 Mb/s   Tx-Power:29 dBm
ath16     IEEE 802.11axa  ESSID:"!!3290-1"
          Mode:Master  Frequency:5.18 GHz (Channel 36)  Access Point: C8:4F:86:95:CE:E5
ath32     IEEE 802.11axa  ESSID:"!!3290-1"
          Mode:Master  Frequency:6.415 GHz (Channel 93)  Access Point: CA:4F:86:66:2F:A0
lo        no wireless extensions.
"""


class WifiParseTests(unittest.TestCase):
    def test_parse_real_client_row(self) -> None:
        clients = parse_wlanconfig_list(WLANCONFIG_ATH32, "ath32")
        self.assertEqual(len(clients), 1)
        c = clients[0]
        self.assertEqual(c["mac"], "d6:0d:0f:42:6f:c7")
        self.assertEqual(c["aid"], 4)
        self.assertEqual(c["channel"], 93)
        self.assertEqual(c["txrate"], "864M")
        self.assertEqual(c["rxrate"], "1080M")
        self.assertEqual(c["rssi"], -55)
        self.assertEqual(c["signal_pct"], 90)
        self.assertEqual(c["assoc_time"], "00:01:40")
        self.assertEqual(c["phymode"], "11AXA_HE80")
        self.assertEqual(c["width"], "80MHz")
        self.assertEqual(c["rxnss"], 2)
        self.assertEqual(c["txnss"], 2)
        self.assertEqual(c["snr"], 40)          # from verbose tail
        self.assertEqual(c["band"], "6G")       # verbose '6GHz' normalised to iface form
        self.assertEqual(c["vendor"], "Private (randomized)")  # d6 = locally administered

    def test_empty_list_is_no_clients(self) -> None:
        self.assertEqual(parse_wlanconfig_list(WLANCONFIG_EMPTY, "ath0"), [])

    def test_discover_vaps(self) -> None:
        vaps = discover_vaps(IWCONFIG)
        ifaces = {v["iface"]: v for v in vaps}
        self.assertEqual(set(ifaces), {"ath0", "ath16", "ath32"})
        self.assertEqual(ifaces["ath0"]["band"], "2.4G")
        self.assertEqual(ifaces["ath16"]["band"], "5G")
        self.assertEqual(ifaces["ath32"]["band"], "6G")
        self.assertEqual(ifaces["ath16"]["channel"], 36)
        self.assertEqual(ifaces["ath0"]["ssid"], "!!3290-1")

    def test_band_for_iface(self) -> None:
        self.assertEqual(band_for_iface("ath3"), "2.4G")
        self.assertEqual(band_for_iface("ath20"), "5G")
        self.assertEqual(band_for_iface("ath47"), "6G")

    def test_vendor_randomized_vs_oui(self) -> None:
        self.assertEqual(vendor_for_mac("d6:0d:0f:42:6f:c7"), "Private (randomized)")
        # c8 = 11001000, bit 0x2 clear -> universally administered -> OUI prefix
        self.assertEqual(vendor_for_mac("c8:4f:86:95:ce:e2"), "C8:4F:86")

    def test_signal_pct_clamps(self) -> None:
        self.assertEqual(signal_pct(-55), 90)
        self.assertEqual(signal_pct(-30), 100)
        self.assertEqual(signal_pct(-100), 0)
        self.assertIsNone(signal_pct(None))

    def test_parse_apstats_real_dump(self) -> None:
        s = parse_apstats(APSTATS)
        self.assertEqual(s["tx_bytes"], 27196107)
        self.assertEqual(s["rx_bytes"], 1473977)
        self.assertEqual(s["avg_tx_kbps"], 864700)
        self.assertEqual(s["avg_rx_kbps"], 1020800)
        self.assertEqual(s["tx_bytes_1s"], 4163)
        self.assertEqual(s["rx_bytes_1s"], 4494)
        self.assertEqual(s["band_width"], 80)
        self.assertEqual(s["rx_rssi"], 38)
        self.assertEqual(s["per"], 0)
        self.assertEqual((s["tx_nss"], s["rx_nss"]), (2, 2))

    def test_parse_apstats_empty_is_all_none(self) -> None:
        s = parse_apstats("garbage\nno fields here\n")
        self.assertTrue(all(v is None for v in s.values()))

    def test_width_from_phymode(self) -> None:
        self.assertEqual(width_from_phymode("11AXA_HE80"), "80MHz")
        self.assertEqual(width_from_phymode("11AC_VHT160"), "160MHz")
        self.assertIsNone(width_from_phymode("11B"))


if __name__ == "__main__":
    unittest.main()
