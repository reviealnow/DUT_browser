import unittest

from app.services import dut_regdomain as rd
from app.services import packet_rules as pr


class DomainTableTests(unittest.TestCase):
    def test_table_matches_the_sku_list(self):
        # Counts from the SKU country list: a transcription slip shows up here.
        sizes = {n: len(d.countries) for n, d in rd.DOMAINS.items()}
        self.assertEqual(sizes, {0: 1, 1: 1, 2: 38, 3: 2, 4: 1, 5: 191})
        self.assertEqual(len(rd._ROW.split()), 191)  # no duplicate code collapsed
        self.assertEqual(len(rd._EU.split()), 38)

    def test_domains_are_disjoint_iso_codes(self):
        seen: set[str] = set()
        for d in rd.DOMAINS.values():
            for code in d.countries:
                self.assertRegex(code, r"^[A-Z]{2}$")
            self.assertFalse(seen & d.countries, d.name)
            seen |= d.countries

    def test_spot_checks(self):
        self.assertIn("TW", rd.DOMAINS[5].countries)
        self.assertIn("DE", rd.DOMAINS[2].countries)
        self.assertIn("GB", rd.DOMAINS[2].countries)
        self.assertEqual(rd.DOMAINS[0].countries, {"US"})
        self.assertEqual(rd.DOMAINS[1].countries, {"CA"})


class CommandAndParseTests(unittest.TestCase):
    def test_command_per_family(self):
        self.assertEqual(rd.command_for("AP6_420E"), "board_config get domain")
        self.assertEqual(rd.command_for("ap6_840"), "board_config get domain")
        self.assertIsNone(rd.command_for("AP7_430"))  # command not recorded yet
        self.assertIsNone(rd.command_for(None))

    def test_parse_captured_ap6_output(self):
        # Verbatim from an AP6_420E EU SKU's console, 2026-11-03.
        captured = "AP6_420E# board_config get domain\n2\nAP6_420E#"
        self.assertEqual(rd.parse_domain(captured), rd.DOMAINS[2])
        self.assertEqual(rd.parse_domain(captured.replace("\n", "\r\n")), rd.DOMAINS[2])

    def test_parse_captured_us_sku_output(self):
        # Verbatim from an AP6_420 US SKU's console, 2026-11-04: 0 is the US.
        captured = "board_config get domain\n0 \nAP6_420# "
        self.assertEqual(rd.parse_domain(captured), rd.DOMAINS[0])

    def test_parse(self):
        self.assertEqual(rd.parse_domain("board_config get domain\n2\nAP6_420E# ").number, 2)
        self.assertEqual(rd.parse_domain("domain=5\n").number, 5)
        self.assertEqual(rd.parse_domain("Domain: 1").number, 1)
        self.assertIsNone(rd.parse_domain("board_config get domain\nAP6_420E# "))
        self.assertIsNone(rd.parse_domain("9\n"))  # unknown SKU: not checked
        self.assertIsNone(rd.parse_domain(""))


class DomainRuleTests(unittest.TestCase):
    def hit(self, row):
        return [(f.severity, f.message) for f in pr.evaluate([row], ["country_mismatch"])]

    def row(self, country, number):
        d = rd.DOMAINS[number]
        return {"frame": 1, "bssid": "aa:bb:cc:dd:ee:ff", "country": country,
                "expected_countries": d.countries, "expected_region": d.label}

    def test_any_country_of_the_domain_passes(self):
        self.assertEqual(self.hit(self.row("DE", 2)), [])
        self.assertEqual(self.hit(self.row("fr", 2)), [])
        self.assertEqual(self.hit(self.row("CA", 1)), [])
        self.assertEqual(self.hit(self.row("US", 0)), [])

    def test_us_and_canada_are_separate_skus(self):
        (severity, message), = self.hit(self.row("US", 1))
        self.assertEqual(severity, "fail")
        self.assertIn("domain 1 (CA)", message)
        (severity, _), = self.hit(self.row("CA", 0))
        self.assertEqual(severity, "fail")

    def test_country_outside_the_domain_fails(self):
        (severity, message), = self.hit(self.row("US", 2))
        self.assertEqual(severity, "fail")
        self.assertIn("US", message)
        self.assertIn("domain 2 (EU)", message)

    def test_missing_country_element_warns(self):
        (severity, _), = self.hit(self.row(None, 2))
        self.assertEqual(severity, "warn")


if __name__ == "__main__":
    unittest.main()
