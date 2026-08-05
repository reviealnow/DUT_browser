"""Tests for tools/context_render.py (Track B of the Wi-Fi context contract).

The tool is a data-prep layer plus a thin matplotlib layer, and these tests
assert the data prep — bar heights, channel identification, table rows — never
pixels. The render layer is exercised end to end only for "did the right set of
files appear, and is each one a real PNG rather than a stub".

The committed fixtures live under ``fixtures/context_render/context/`` laid out
exactly as a downloaded bundle's ``context/`` directory, so a reviewer can copy
that tree into a temp dir and run the tool on it by hand.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

# The tool lives in dut-dashboard/tools/, outside the backend package. It has a
# __main__ guard (unlike analyzer3.py), so importing it is safe.
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import context_render as cr  # noqa: E402

FIXTURE_CONTEXT = Path(__file__).resolve().parent / "fixtures" / "context_render" / "context"

# The one instant every expected filename in this module is derived from.
FIXED_NOW = datetime(2026, 8, 3, 11, 30, 0)


def load_fixture(kind: str, name: str) -> dict:
    return json.loads((FIXTURE_CONTEXT / kind / name).read_text(encoding="utf-8"))


def survey_payload() -> dict:
    return load_fixture(cr.SITE_SURVEY, "site-survey-lab-ap6-20260803-101500.json")


def render(session_dir: Path) -> tuple[list[Path], str]:
    """Run the tool, returning (paths written, stdout)."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        written = cr.render_session(session_dir, now=FIXED_NOW)
    return written, buffer.getvalue()


class OutputPrefixTests(unittest.TestCase):
    """The prefix must stay byte-identical to analyzer3.py's (contract §5)."""

    def test_prefix_from_a_session_log_name(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            self.assertEqual(cr.output_prefix(base, FIXED_NOW), "08031130_101500_19300_")

    def test_disagreeing_logs_collapse_to_multi(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a_101500_v1.9.300.log").write_text("x", encoding="utf-8")
            (base / "b_220000_v1.8.241.log").write_text("x", encoding="utf-8")
            self.assertEqual(cr.output_prefix(base, FIXED_NOW), "08031130_MULTI_MULTI_")

    def test_no_log_falls_back_and_drops_the_fw_tag(self) -> None:
        # analyzer3 exits here; context JSONs are renderable without a log.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cr.output_prefix(Path(d), FIXED_NOW), "08031130_notime_")

    def test_analyzer3s_stamp_is_adopted_so_one_bundle_has_one_prefix(self) -> None:
        # analyzer3 runs first and owns the stamp; a run that crosses a minute
        # boundary must not give the bundle a second prefix.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            (base / "08031129_101500_19300_cpu_usage.csv").write_text("x", encoding="utf-8")
            self.assertEqual(cr.output_prefix(base), "08031129_101500_19300_")

    def test_the_newest_analyzer3_output_wins(self) -> None:
        # A dirty directory must not pin a stale stamp.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            old = base / "08010900_101500_19300_cpu_usage.csv"
            new = base / "08031129_101500_19300_cpu_usage.csv"
            old.write_text("x", encoding="utf-8")
            new.write_text("x", encoding="utf-8")
            os.utime(old, (1_000_000, 1_000_000))
            self.assertEqual(cr.output_prefix(base), "08031129_101500_19300_")

    def test_an_anchor_wins_over_an_explicit_now(self) -> None:
        """`now` pins the fallback stamp only; it cannot switch adoption off.

        The reverse precedence is how adoption got silently disabled once
        already (render_session pre-resolved `now`), so a caller passing one
        must not be able to reopen that path.
        """
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            (base / "08031129_101500_19300_cpu_usage.csv").write_text("x", encoding="utf-8")
            self.assertEqual(cr.output_prefix(base, FIXED_NOW), "08031129_101500_19300_")

    def test_a_file_analyzer3_could_not_have_written_is_not_authoritative(self) -> None:
        """Suffix-matching alone must not let a foreign file name the bundle."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            (base / "12345678_NOT-ANALYZER_PREFIX_cpu_usage.csv").write_text("x", encoding="utf-8")
            (base / "my_cpu_usage.csv").write_text("x", encoding="utf-8")
            (base / "cpu_usage.csv").write_text("x", encoding="utf-8")
            self.assertEqual(cr.output_prefix(base, FIXED_NOW), "08031130_101500_19300_")

    def test_a_malformed_newest_anchor_does_not_shadow_a_real_one(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            real = base / "08031129_101500_19300_cpu_usage.csv"
            junk = base / "12345678_NOT-ANALYZER_PREFIX_cpu_usage.csv"
            real.write_text("x", encoding="utf-8")
            junk.write_text("x", encoding="utf-8")
            os.utime(real, (1_000_000, 1_000_000))          # junk is newer
            self.assertEqual(cr.output_prefix(base), "08031129_101500_19300_")

    def test_every_shape_analyzer3_can_emit_is_accepted(self) -> None:
        for prefix in ("08041541_notime_", "08041541_101900_19300_",
                       "08041541_MULTI_MULTI_", "08041541_notime_MULTI_",
                       "08041541_101900_101005_"):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                (base / f"{prefix}{cr.ANALYZER_ANCHOR}").write_text("x", encoding="utf-8")
                self.assertEqual(cr.adopted_prefix(base), prefix)

    def test_without_analyzer3_output_the_tool_uses_its_own_clock(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            self.assertRegex(cr.output_prefix(base), r"^\d{8}_101500_19300_$")

    def test_the_whole_prefix_is_adopted_not_only_the_stamp(self) -> None:
        """Tags come from the anchor too.

        Adopting only the stamp left every tool recomputing the tags, so any
        difference in how a tool selects its logs — a mixed-case `.LOG` was the
        real case — split the bundle's prefixes again. wifi_timeseries pins the
        same expected value, and the pair of assertions is the agreement.
        """
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a_111111_v1.9.1.log").write_text("x", encoding="utf-8")
            (base / "b_222222_v2.0.2.LOG").write_text("x", encoding="utf-8")
            (base / "08010900_111111_19001_cpu_usage.csv").write_text("x", encoding="utf-8")
            self.assertEqual(cr.output_prefix(base), "08010900_111111_19001_")

    def test_render_session_adopts_the_stamp_on_the_real_path(self) -> None:
        """The default path must adopt too, not just a direct output_prefix call.

        render_session resolves `now` for its caption; forwarding the resolved
        value to output_prefix silently defeated adoption, and every unit test
        here passes `now` explicitly so none of them saw it — only a run against
        a real session directory did.
        """
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
            (base / "08031129_101500_19300_cpu_usage.csv").write_text("x", encoding="utf-8")
            kind_dir = base / cr.CONTEXT_DIR_NAME / cr.SSID_CAPABILITY
            kind_dir.mkdir(parents=True)
            shutil.copy2(
                FIXTURE_CONTEXT / cr.SSID_CAPABILITY / "ssid-capability-lab-ap6-20260803-101500.json",
                kind_dir,
            )
            with redirect_stdout(io.StringIO()):
                written = cr.render_session(base)
            self.assertTrue(written)
            for path in written:
                self.assertTrue(path.name.startswith("08031129_"), path.name)


class LatestSnapshotTests(unittest.TestCase):
    def test_picks_the_newest_timestamp(self) -> None:
        path = cr.latest_snapshot(FIXTURE_CONTEXT, cr.SITE_SURVEY)
        assert path is not None
        self.assertEqual(path.name, "site-survey-lab-ap6-20260803-101500.json")

    def test_ignores_capture_report_skip_markers_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            context_dir = Path(d) / "context"
            kind_dir = context_dir / cr.WIFI_CLIENTS
            kind_dir.mkdir(parents=True)
            (context_dir / "capture-report.txt").write_text("wifi-clients: ok\n", encoding="utf-8")
            (kind_dir / "wifi-clients-lab-ap6-20260803-101500.csv").write_text("a,b\n", encoding="utf-8")
            (kind_dir / "wifi-clients-lab-ap6-20260804-101500.skip.json").write_text("{}", encoding="utf-8")
            (kind_dir / "notes.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(cr.latest_snapshot(context_dir, cr.WIFI_CLIENTS))

            real = kind_dir / "wifi-clients-lab-ap6-20260803-101500.json"
            real.write_text("{}", encoding="utf-8")
            self.assertEqual(cr.latest_snapshot(context_dir, cr.WIFI_CLIENTS), real)

    def test_missing_kind_directory_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cr.latest_snapshot(Path(d), cr.SITE_SURVEY))

    def test_corrupt_json_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "site-survey-lab-ap6-20260803-101500.json"
            path.write_text("{not json", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cr.load_snapshot(path), {})


class SurveyPrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.charts = {c["band"]: c for c in cr.prepare_survey_bands(survey_payload())}

    def test_one_chart_per_recommended_band_with_contract_slugs(self) -> None:
        self.assertEqual(
            {band: chart["slug"] for band, chart in self.charts.items()},
            {"2.4GHz": "2g4", "5GHz": "5g", "6GHz": "6g"},
        )

    def test_bar_heights_are_the_raw_neighbour_counts(self) -> None:
        # 2.4 GHz is always drawn on the full 1-13 grid; the fixture has two
        # neighbours on ch 1 and four on ch 6, and nothing anywhere else.
        chart = self.charts["2.4GHz"]
        self.assertEqual(chart["channels"], list(range(1, 14)))
        self.assertEqual(chart["counts"], [2, 0, 0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(len(chart["counts"]), len(chart["channels"]))

    def test_five_ghz_grid_is_observed_plus_current_plus_recommended(self) -> None:
        chart = self.charts["5GHz"]
        self.assertEqual(chart["channels"], [36, 44, 149])
        self.assertEqual(chart["counts"], [1, 3, 1])

    def test_recommended_channel_is_drawn_even_with_no_neighbours_on_it(self) -> None:
        # ch 11 has zero observed neighbours and is exactly what got
        # recommended; a chart that dropped it would hide the answer.
        chart = self.charts["2.4GHz"]
        self.assertIn(11, chart["channels"])
        self.assertEqual(chart["counts"][chart["channels"].index(11)], 0)

    def test_identifies_current_recommended_and_busiest(self) -> None:
        self.assertEqual(
            [
                (c["current_channel"], c["recommended_channel"], c["busiest_channel"])
                for c in (self.charts["2.4GHz"], self.charts["5GHz"], self.charts["6GHz"])
            ],
            [(6, 11, 6), (36, 149, 44), (69, 37, 69)],
        )

    def test_bar_roles_colour_recommended_and_busiest_only(self) -> None:
        chart = self.charts["5GHz"]
        self.assertEqual(
            dict(zip(chart["channels"], cr.bar_roles(chart))),
            {36: cr.ROLE_PLAIN, 44: cr.ROLE_BUSIEST, 149: cr.ROLE_RECOMMENDED},
        )

    def test_recommended_wins_when_it_is_also_the_busiest(self) -> None:
        chart = {
            "channels": [1, 6],
            "counts": [1, 3],
            "recommended_channel": 6,
            "busiest_channel": 6,
        }
        self.assertEqual(cr.bar_roles(chart), [cr.ROLE_PLAIN, cr.ROLE_RECOMMENDED])

    def test_busiest_is_none_when_the_band_is_empty(self) -> None:
        payload = {
            "recommendations": [
                {"band": "5GHz", "iface": "ath16", "current_channel": 36,
                 "recommended_channel": 36, "score": 0.0, "occupancy": {}},
            ],
            "neighbors": [],
        }
        chart = cr.prepare_survey_bands(payload)[0]
        self.assertEqual((chart["channels"], chart["counts"]), ([36], [0]))
        self.assertIsNone(chart["busiest_channel"])
        self.assertEqual(cr.bar_roles(chart), [cr.ROLE_RECOMMENDED])

    def test_busiest_ties_resolve_to_the_lowest_channel(self) -> None:
        payload = {
            "recommendations": [
                {"band": "5GHz", "iface": "ath16", "current_channel": 36,
                 "recommended_channel": 36, "occupancy": {}},
            ],
            "neighbors": [
                {"band": "5GHz", "channel": 149},
                {"band": "5GHz", "channel": 44},
            ],
        }
        self.assertEqual(cr.prepare_survey_bands(payload)[0]["busiest_channel"], 44)

    def test_occupancy_keys_are_reparsed_as_ints(self) -> None:
        # JSON object keys are strings; the score lookup must not silently miss.
        occupancy = self.charts["2.4GHz"]["occupancy"]
        self.assertEqual(occupancy[6], 8.0)
        self.assertEqual(occupancy[11], 3.0)
        self.assertTrue(all(isinstance(k, int) for k in occupancy))

    def test_neighbours_of_other_bands_never_leak_into_a_chart(self) -> None:
        self.assertEqual(sum(self.charts["6GHz"]["counts"]), 3)  # ch 37 x1 + ch 69 x2

    def test_unknown_band_is_skipped_rather_than_guessed_a_filename(self) -> None:
        payload = {"recommendations": [{"band": "60GHz", "current_channel": 1}], "neighbors": []}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cr.prepare_survey_bands(payload), [])

    def test_empty_payload_produces_no_chart(self) -> None:
        for payload in ({}, {"recommendations": [], "neighbors": []}, {"neighbors": [{"band": "5GHz"}]}):
            self.assertEqual(cr.prepare_survey_bands(payload), [])


class CapabilityTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = cr.prepare_capability_table(
            load_fixture(cr.SSID_CAPABILITY, "ssid-capability-lab-ap6-20260803-101500.json")
        )

    def test_columns_and_row_count(self) -> None:
        self.assertEqual(self.table["columns"], ["iface", "SSID", "band", "channel", "security", "PMF", "gen"])
        self.assertEqual(len(self.table["rows"]), 9)
        self.assertTrue(all(len(row) == 7 for row in self.table["rows"]))

    def test_row_content(self) -> None:
        self.assertEqual(
            self.table["rows"][0],
            ["ath0", "Fixture-Lab-24", "2.4GHz", "6", "WPA2/WPA3-Personal", "optional", "Wi-Fi 6"],
        )

    def test_absent_fields_render_as_absent_not_as_a_plausible_value(self) -> None:
        hidden = self.table["rows"][5]  # ath18: no SSID broadcast, generation unknown
        self.assertEqual(hidden[1], cr.MISSING)
        self.assertEqual(hidden[6], cr.MISSING)

    def test_empty_payload_produces_no_rows(self) -> None:
        for payload in ({}, {"ssids": []}, {"ssids": None}):
            self.assertEqual(cr.prepare_capability_table(payload)["rows"], [])


class ClientsTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = cr.prepare_clients_table(
            load_fixture(cr.WIFI_CLIENTS, "wifi-clients-lab-ap6-20260803-101500.json")
        )

    def test_columns_and_row_count(self) -> None:
        self.assertEqual(
            self.table["columns"],
            ["MAC", "SSID", "band", "ch", "RSSI", "SNR", "tx/rx rate", "connected", "vendor"],
        )
        self.assertEqual(len(self.table["rows"]), 6)

    def test_row_content(self) -> None:
        self.assertEqual(
            self.table["rows"][0],
            [
                "02:1a:2b:aa:00:01",
                "Fixture-Lab-24",
                "2.4G",
                "6",
                "-47",
                "48",
                "144M / 144M",
                "23:14:07",
                "Private (randomized)",
            ],
        )

    def test_half_measured_client_keeps_what_it_has(self) -> None:
        # ath17's client reported no rx rate and no SNR; neither becomes a zero.
        row = self.table["rows"][4]
        self.assertEqual(row[5], cr.MISSING)
        self.assertEqual(row[6], f"433M / {cr.MISSING}")
        self.assertEqual(row[4], "-74")

    def test_vaps_without_clients_produce_no_rows(self) -> None:
        # The JSON legitimately exists (VAPs were seen), but zero associations
        # is a table with nothing in it, so no PNG gets written for it.
        payload = {"clients": [], "vaps": [{"iface": "ath0"}]}
        self.assertEqual(cr.prepare_clients_table(payload)["rows"], [])


class RenderSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def session(self, kinds: tuple[str, ...] = cr.KINDS, log_name: str | None = None) -> Path:
        session_dir = Path(self._tmp.name) / f"s{len(list(Path(self._tmp.name).iterdir()))}"
        session_dir.mkdir()
        for kind in kinds:
            shutil.copytree(FIXTURE_CONTEXT / kind, session_dir / "context" / kind)
        if log_name:
            (session_dir / log_name).write_text("stub session log\n", encoding="utf-8")
        return session_dir

    def test_full_fixture_set_writes_exactly_the_five_contract_pngs(self) -> None:
        session_dir = self.session(log_name="dut-session-1.9.300_101500_20260803.log")
        written, _ = render(session_dir)
        prefix = "08031130_101500_19300_"
        self.assertEqual(
            sorted(p.name for p in written),
            sorted(
                prefix + name
                for name in (
                    "survey_channels_2g4.png",
                    "survey_channels_5g.png",
                    "survey_channels_6g.png",
                    "ssid_capability.png",
                    "wifi_clients_table.png",
                )
            ),
        )
        self.assertEqual(sorted(p.name for p in session_dir.glob("*.png")), sorted(p.name for p in written))

    def test_every_png_is_a_real_image_not_a_stub(self) -> None:
        written, _ = render(self.session())
        for path in written:
            with self.subTest(png=path.name):
                self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                self.assertGreater(path.stat().st_size, 5_000)

    def test_missing_context_directory_writes_nothing(self) -> None:
        session_dir = Path(self._tmp.name) / "bare"
        session_dir.mkdir()
        (session_dir / "dut-session-1.9.300_101500_20260803.log").write_text("x", encoding="utf-8")
        written, out = render(session_dir)
        self.assertEqual(written, [])
        self.assertEqual(list(session_dir.glob("*.png")), [])
        self.assertIn("no context/ directory", out)

    def test_absent_kind_yields_no_placeholder_for_that_kind(self) -> None:
        session_dir = self.session(kinds=(cr.SITE_SURVEY,))
        written, out = render(session_dir)
        self.assertEqual(len(written), 3)  # the three survey bands only
        self.assertFalse(any("ssid_capability" in p.name or "clients_table" in p.name for p in written))
        self.assertIn("ssid-capability: no snapshot JSON", out)
        self.assertIn("wifi-clients: no snapshot JSON", out)

    def test_empty_payload_json_writes_no_png(self) -> None:
        # #108 stopped these being written at all; a stale bundle may still
        # carry one, and it must not become a chart of nothing.
        session_dir = Path(self._tmp.name) / "empty"
        for kind, payload in (
            (cr.SITE_SURVEY, {"recommendations": [], "neighbors": [], "vaps": []}),
            (cr.SSID_CAPABILITY, {"ssids": []}),
            (cr.WIFI_CLIENTS, {"clients": [], "vaps": []}),
        ):
            kind_dir = session_dir / "context" / kind
            kind_dir.mkdir(parents=True)
            (kind_dir / f"{kind}-lab-ap6-20260803-101500.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
        written, out = render(session_dir)
        self.assertEqual(written, [])
        self.assertEqual(list(session_dir.glob("*.png")), [])
        self.assertEqual(list(session_dir.glob("*")), [session_dir / "context"])
        self.assertIn("no PNG written", out)

    def test_corrupt_snapshot_degrades_to_no_png(self) -> None:
        session_dir = self.session(kinds=(cr.WIFI_CLIENTS,))
        for path in (session_dir / "context" / cr.WIFI_CLIENTS).glob("*.json"):
            path.write_text("{ truncated", encoding="utf-8")
        written, _ = render(session_dir)
        self.assertEqual(written, [])

    def test_newest_snapshot_wins_over_the_stale_one(self) -> None:
        # Both site-survey fixtures are staged; the stale one recommends ch 1 on
        # 2.4 GHz and carries no 5/6 GHz row, so picking it would show up as a
        # single band here.
        written, _ = render(self.session(kinds=(cr.SITE_SURVEY,)))
        self.assertEqual(len(written), 3)

    def test_rerunning_overwrites_rather_than_accumulating(self) -> None:
        session_dir = self.session()
        render(session_dir)
        render(session_dir)
        self.assertEqual(len(list(session_dir.glob("*.png"))), 5)

    def test_main_exits_zero_on_no_usable_input(self) -> None:
        session_dir = Path(self._tmp.name) / "nothing"
        session_dir.mkdir()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cr.main([str(session_dir)]), 0)


if __name__ == "__main__":
    unittest.main()
