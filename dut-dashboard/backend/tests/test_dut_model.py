"""Which model a DUT is, and the band mapping that follows from it.

Every expectation about the 420E below is a reading taken off the bench DUT
whose prompt says ``AP6_420E#``, not a construction::

    ath0   2.412 GHz    ath8   5.66 GHz     ath16  6.775 GHz
    ath1   2.412 GHz    ath9   5.66 GHz     ath17  6.775 GHz
    ath6   2.412 GHz

`iw dev` also lists one ``wifiN`` radio per band -- wifi0 at 2412 MHz, wifi1 at
5660, wifi2 at 6775 -- an independent witness that this model has three.

`band_for_iface` assumed sixteen VAPs per band AND three bands, which is the
840E row of the spec and nothing else. On this device it is wrong for four of
the seven active VAPs, and wrong plausibly: "5G" for a 6 GHz interface is not
obviously nonsense to a reader. On a model with no 6GHz radio it was wrong a
second way, answering "6G" for an interface number that model cannot produce.
"""

from __future__ import annotations

import unittest

from app.services.dut_model import (
    DEFAULT_SPEC,
    bands_for,
    cores_for,
    detect_model,
    spec_for,
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

    def test_accepts_every_suffix_in_the_spec(self) -> None:
        self.assertEqual(detect_model("AP6_420#"), "AP6_420")
        self.assertEqual(detect_model("AP6_420X#"), "AP6_420X")
        self.assertEqual(detect_model("AP6420x-SERIAL"), "AP6_420X")
        self.assertEqual(detect_model("AP6840"), "AP6_840")

    def test_ignores_a_model_name_quoted_mid_line(self) -> None:
        """A log line *about* a model is not the device saying what it is."""
        self.assertIsNone(detect_model("upgrading firmware for AP6_840E now"))
        self.assertIsNone(detect_model("  # comment mentioning AP6_420E"))

    def test_no_model_in_ordinary_console_noise(self) -> None:
        for line in ("", "   ", "cmd>", "BusyBox v1.31.1", "ath0: link up"):
            with self.subTest(line=line):
                self.assertIsNone(detect_model(line))


# The full spec, as given. Width AND which bands exist -- they vary
# independently, and only the E models have a 6GHz radio.
SPEC = {
    "AP6_420": (8, ("2.4G", "5G"), 2),
    "AP6_420E": (8, ("2.4G", "5G", "6G"), 2),
    "AP6_420X": (8, ("2.4G", "5G"), 2),
    "AP6_840": (16, ("2.4G", "5G"), 4),
    "AP6_840E": (16, ("2.4G", "5G", "6G"), 4),
}


class ModelSpecTests(unittest.TestCase):
    def test_every_model_in_the_spec(self) -> None:
        for model, (per_band, bands, cores) in SPEC.items():
            with self.subTest(model=model):
                self.assertEqual(vaps_per_band(model), per_band)
                self.assertEqual(bands_for(model), bands)
                self.assertEqual(cores_for(model), cores)

    def test_only_the_E_models_have_a_6ghz_radio(self) -> None:
        """The suffix is part of the identity, not decoration: 420 and 420X are
        the same width as 420E and a different set of bands."""
        for model, (_, bands, _cores) in SPEC.items():
            with self.subTest(model=model):
                self.assertEqual("6G" in bands, model.endswith("E"))

    def test_an_unknown_or_absent_model_keeps_the_old_assumption(self) -> None:
        """Not a guess upgrade: sixteen-wide and three-band is exactly what the
        code did for every model before this existed."""
        for unknown in (None, "AP6_999", "something else", ""):
            with self.subTest(model=unknown):
                self.assertIs(spec_for(unknown), DEFAULT_SPEC)
        self.assertEqual(DEFAULT_SPEC.per_band, 16)
        self.assertEqual(DEFAULT_SPEC.bands, ("2.4G", "5G", "6G"))

    def test_an_unknown_model_has_no_core_count_rather_than_a_default(self) -> None:
        """A guessed core count would manufacture the very mismatch the caller
        is looking for: it compares this against a snapshot's core count to spot
        a reading taken on other hardware."""
        for unknown in (None, "AP6_999", "something else", ""):
            with self.subTest(model=unknown):
                self.assertIsNone(cores_for(unknown))

    def test_the_bench_420E_really_has_two_cores(self) -> None:
        """Measured, not taken from the spec sheet:

            grep -c ^processor /proc/cpuinfo  ->  2

        on the DUT whose prompt reads AP6_420E#. The dashboard was showing
        4 cores for it, from a snapshot recorded when this registry entry was
        cabled to an 840E."""
        self.assertEqual(cores_for("AP6_420E"), 2)


class BandForIfaceTests(unittest.TestCase):
    def test_matches_the_bench_420E_on_every_active_vap(self) -> None:
        """The whole point. Each expectation is what the device said."""
        plan = spec_for("AP6_420E")
        for iface, band in BENCH_420E.items():
            with self.subTest(iface=iface):
                self.assertEqual(band_for_iface(iface, plan), band)

    def test_the_old_sixteen_wide_guess_got_four_of_those_wrong(self) -> None:
        """Pins the defect rather than just the fix. If someone re-hardcodes 16
        this stays green and the test above goes red -- which is the pair that
        says the parameter is doing work."""
        wrong = {
            iface: band_for_iface(iface, DEFAULT_SPEC)
            for iface, band in BENCH_420E.items()
            if band_for_iface(iface, DEFAULT_SPEC) != band
        }
        self.assertEqual(wrong, {"ath8": "2.4G", "ath9": "2.4G", "ath16": "5G", "ath17": "5G"})

    def test_the_840E_layout_is_unchanged(self) -> None:
        plan = spec_for("AP6_840E")
        self.assertEqual(band_for_iface("ath3", plan), "2.4G")
        self.assertEqual(band_for_iface("ath15", plan), "2.4G")
        self.assertEqual(band_for_iface("ath20", plan), "5G")
        self.assertEqual(band_for_iface("ath47", plan), "6G")

    def test_every_block_boundary_in_the_spec(self) -> None:
        """First and last interface of each block, for all five models. The 420
        starts are measured (ath0/ath8/ath16 on the bench); the ends follow from
        the next block's start."""
        expected = {
            "AP6_420": {"ath0": "2.4G", "ath7": "2.4G", "ath8": "5G", "ath15": "5G"},
            "AP6_420X": {"ath0": "2.4G", "ath7": "2.4G", "ath8": "5G", "ath15": "5G"},
            "AP6_420E": {"ath0": "2.4G", "ath7": "2.4G", "ath8": "5G", "ath15": "5G",
                         "ath16": "6G", "ath23": "6G"},
            "AP6_840": {"ath0": "2.4G", "ath15": "2.4G", "ath16": "5G", "ath31": "5G"},
            "AP6_840E": {"ath0": "2.4G", "ath15": "2.4G", "ath16": "5G", "ath31": "5G",
                         "ath32": "6G", "ath47": "6G"},
        }
        for model, ifaces in expected.items():
            plan = spec_for(model)
            for iface, band in ifaces.items():
                with self.subTest(model=model, iface=iface):
                    self.assertEqual(band_for_iface(iface, plan), band)

    def test_a_model_without_6ghz_refuses_to_invent_one(self) -> None:
        """This is the half a width alone cannot express. On a 420 there is no
        third block, so ath16 is a number that model cannot produce -- and the
        old code answered "6G" for it, which reads exactly like a measurement.

        It matters where the guess is actually used: a mesh backhaul can sit on
        any band, and `wlanconfig` names its VAP by number with no frequency.
        """
        for model in ("AP6_420", "AP6_420X"):
            with self.subTest(model=model):
                self.assertEqual(band_for_iface("ath16", spec_for(model)), "?")
                self.assertEqual(band_for_iface("ath23", spec_for(model)), "?")
        self.assertEqual(band_for_iface("ath32", spec_for("AP6_840")), "?")
        # ...while the E models of the same width do have it.
        self.assertEqual(band_for_iface("ath16", spec_for("AP6_420E")), "6G")
        self.assertEqual(band_for_iface("ath32", spec_for("AP6_840E")), "6G")

    def test_the_default_is_still_the_840E_layout(self) -> None:
        """Callers that pass nothing behave exactly as before."""
        self.assertEqual(band_for_iface("ath20"), "5G")
        self.assertEqual(band_for_iface("ath40"), "6G")

    def test_a_non_ath_interface_is_still_unknown(self) -> None:
        self.assertEqual(band_for_iface("eth0", spec_for("AP6_420E")), "?")


if __name__ == "__main__":
    unittest.main()
