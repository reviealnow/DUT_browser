"""Which model a DUT is, and the band mapping that follows from it.

Every expectation about the 420E below is a reading taken off the bench DUT
whose prompt says ``AP6_420E#``, not a construction::

    ath0   2.412 GHz    ath8   5.66 GHz     ath16  6.775 GHz
    ath1   2.412 GHz    ath9   5.66 GHz     ath17  6.775 GHz
    ath6   2.412 GHz

`band_for_iface` assumed sixteen VAPs per band, which is the 840 layout. On
that device it is wrong for four of the seven active VAPs, and wrong
plausibly -- "5G" for a 6 GHz interface is not obviously nonsense to a reader.
"""

from __future__ import annotations

import unittest

from app.services.dut_model import (
    DEFAULT_VAPS_PER_BAND,
    detect_model,
    model_number,
    vaps_per_band,
)
from app.services.wifi_clients import band_for_iface

# iface -> the band the 420E itself reported, via the frequency in `iwconfig`.
BENCH_420E = {
    "ath0": "2.4G",
    "ath1": "2.4G",
    "ath6": "2.4G",
    "ath8": "5G",
    "ath9": "5G",
    "ath16": "6G",
    "ath17": "6G",
}


class DetectModelTests(unittest.TestCase):
    def test_reads_the_console_prompt(self) -> None:
        self.assertEqual(detect_model("AP6_420E#"), "AP6_420E")
        self.assertEqual(detect_model("AP6_840E#"), "AP6_840E")

    def test_reads_the_hostname_spelling_too(self) -> None:
        """`hostname` answers without the underscore and with a serial glued on
        -- the same device, so it must resolve to the same model."""
        self.assertEqual(detect_model("AP6420E-PB1005QPCFVFMA8"), "AP6_420E")

    def test_accepts_the_non_E_variants(self) -> None:
        self.assertEqual(detect_model("AP6_420#"), "AP6_420")
        self.assertEqual(detect_model("AP6840"), "AP6_840")

    def test_ignores_a_model_name_quoted_mid_line(self) -> None:
        """A log line *about* a model is not the device saying what it is."""
        self.assertIsNone(detect_model("upgrading firmware for AP6_840E now"))
        self.assertIsNone(detect_model("  # comment mentioning AP6_420E"))

    def test_no_model_in_ordinary_console_noise(self) -> None:
        for line in ("", "   ", "cmd>", "BusyBox v1.31.1", "ath0: link up"):
            with self.subTest(line=line):
                self.assertIsNone(detect_model(line))


class VapsPerBandTests(unittest.TestCase):
    def test_the_two_known_families(self) -> None:
        self.assertEqual(vaps_per_band("AP6_420E"), 8)
        self.assertEqual(vaps_per_band("AP6_420"), 8)
        self.assertEqual(vaps_per_band("AP6_840E"), 16)
        self.assertEqual(vaps_per_band("AP6_840"), 16)

    def test_an_unknown_or_absent_model_keeps_the_old_assumption(self) -> None:
        """Not a guess upgrade: 16 is exactly what the code did for every model
        before this existed, so an unrecognised device is no worse off."""
        self.assertEqual(vaps_per_band(None), DEFAULT_VAPS_PER_BAND)
        self.assertEqual(vaps_per_band("AP6_999"), DEFAULT_VAPS_PER_BAND)
        self.assertEqual(vaps_per_band("something else"), DEFAULT_VAPS_PER_BAND)
        self.assertEqual(DEFAULT_VAPS_PER_BAND, 16)

    def test_model_number_extracts_the_digits(self) -> None:
        self.assertEqual(model_number("AP6_420E"), "420")
        self.assertIsNone(model_number(None))
        self.assertIsNone(model_number("not a model"))


class BandForIfaceTests(unittest.TestCase):
    def test_matches_the_bench_420E_on_every_active_vap(self) -> None:
        """The whole point. Each expectation is what the device said."""
        per_band = vaps_per_band("AP6_420E")
        for iface, band in BENCH_420E.items():
            with self.subTest(iface=iface):
                self.assertEqual(band_for_iface(iface, per_band), band)

    def test_the_old_sixteen_wide_guess_got_four_of_those_wrong(self) -> None:
        """Pins the defect rather than just the fix. If someone re-hardcodes 16
        this stays green and the test above goes red -- which is the pair that
        says the parameter is doing work."""
        wrong = {
            iface: band_for_iface(iface, 16)
            for iface, band in BENCH_420E.items()
            if band_for_iface(iface, 16) != band
        }
        self.assertEqual(wrong, {"ath8": "2.4G", "ath9": "2.4G", "ath16": "5G", "ath17": "5G"})

    def test_the_840_layout_is_unchanged(self) -> None:
        per_band = vaps_per_band("AP6_840E")
        self.assertEqual(band_for_iface("ath3", per_band), "2.4G")
        self.assertEqual(band_for_iface("ath15", per_band), "2.4G")
        self.assertEqual(band_for_iface("ath20", per_band), "5G")
        self.assertEqual(band_for_iface("ath47", per_band), "6G")

    def test_the_default_is_still_the_840_layout(self) -> None:
        """Callers that pass nothing behave exactly as before."""
        self.assertEqual(band_for_iface("ath20"), "5G")

    def test_a_non_ath_interface_is_still_unknown(self) -> None:
        self.assertEqual(band_for_iface("eth0", 8), "?")


if __name__ == "__main__":
    unittest.main()
