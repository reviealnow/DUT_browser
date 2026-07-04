"""Tests for the per-DUT last-recommendation cache and its read-only endpoint.

The cache is exercised as a pure module; the endpoint is called directly (like
test_version_endpoint) so no serial worker / TestClient wiring is needed.
"""

from __future__ import annotations

import unittest

from app import main
from app.services import survey_cache

_RECS = [
    {"band": "2.4GHz", "current_channel": 11, "recommended_channel": 1},
    {"band": "5GHz", "current_channel": 60, "recommended_channel": 60},
]


class SurveyCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        survey_cache.clear()

    def test_unknown_dut_returns_none(self) -> None:
        self.assertIsNone(survey_cache.last_recommendation("nope"))

    def test_remember_then_last_roundtrips(self) -> None:
        survey_cache.remember_recommendation("lab2", _RECS, "2026-07-04T10:00:00")
        cached = survey_cache.last_recommendation("lab2")
        assert cached is not None
        self.assertEqual(cached["recommendations"], _RECS)
        self.assertEqual(cached["captured_at"], "2026-07-04T10:00:00")

    def test_per_dut_isolation(self) -> None:
        survey_cache.remember_recommendation("a", _RECS, "t")
        self.assertIsNone(survey_cache.last_recommendation("b"))

    def test_remember_overwrites_previous(self) -> None:
        survey_cache.remember_recommendation("a", _RECS, "t1")
        survey_cache.remember_recommendation("a", [], "t2")
        cached = survey_cache.last_recommendation("a")
        assert cached is not None
        self.assertEqual(cached["recommendations"], [])
        self.assertEqual(cached["captured_at"], "t2")


class LastRecommendationEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        survey_cache.clear()

    def test_never_scanned_returns_empty_uncached(self) -> None:
        result = main.get_wifi_channel_recommendation_last(dut="never")
        self.assertEqual(result, {"recommendations": [], "captured_at": None, "cached": False})

    def test_returns_cached_recommendation(self) -> None:
        survey_cache.remember_recommendation("lab2", _RECS, "2026-07-04T10:00:00")
        result = main.get_wifi_channel_recommendation_last(dut="lab2")
        self.assertTrue(result["cached"])
        self.assertEqual(result["recommendations"], _RECS)
        self.assertEqual(result["captured_at"], "2026-07-04T10:00:00")


if __name__ == "__main__":
    unittest.main()
