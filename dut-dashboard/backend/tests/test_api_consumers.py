"""The probe that answers "may this response shape change?".

It is an instrument, so what these check is that it records what it claims,
records it once, cannot be made to write somebody else's log lines, and — the
one that matters — is actually wired into the app rather than merely importable.
"""

from __future__ import annotations

import logging
import unittest

from fastapi.testclient import TestClient

from app.services import api_consumers


class NoteRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        api_consumers.reset()
        self.addCleanup(api_consumers.reset)

    def test_a_watched_path_is_recorded_once_per_distinct_caller(self) -> None:
        """The dashboard polls these endpoints. A line per request would bury
        the one caller that matters under thousands from the browser."""
        first = api_consumers.note_request("/api/duts", "192.168.30.5", "Mozilla/5.0")
        again = api_consumers.note_request("/api/duts", "192.168.30.5", "Mozilla/5.0")

        self.assertTrue(first)
        self.assertFalse(again, "the same caller reported twice")
        self.assertEqual(len(api_consumers.seen_callers()), 1)

    def test_a_different_agent_from_the_same_host_is_a_different_caller(self) -> None:
        """The whole question is whether something *other than the dashboard*
        reads this, and on a bench that something shares an IP with a browser."""
        api_consumers.note_request("/api/duts", "192.168.30.5", "Mozilla/5.0")
        api_consumers.note_request("/api/duts", "192.168.30.5", "curl/8.4.0")

        agents = {agent for _path, _client, agent in api_consumers.seen_callers()}
        self.assertEqual(agents, {"Mozilla/5.0", "curl/8.4.0"})

    def test_an_unwatched_path_is_not_recorded(self) -> None:
        self.assertFalse(api_consumers.note_request("/api/files", "192.168.30.5", "curl/8.4.0"))
        self.assertEqual(api_consumers.seen_callers(), [])

    def test_a_caller_cannot_forge_log_lines_through_its_user_agent(self) -> None:
        """A User-Agent is whatever the caller typed. Newlines in one would let
        it write its own entries into the record of who called."""
        api_consumers.note_request(
            "/api/duts", "192.168.30.5", "curl/8\r\nINFO: api-consumer probe: nobody called",
        )

        recorded = api_consumers.seen_callers()[0][2]
        self.assertNotIn("\n", recorded)
        self.assertNotIn("\r", recorded)

    def test_a_long_agent_cannot_fill_the_log(self) -> None:
        api_consumers.note_request("/api/duts", "192.168.30.5", "x" * 5000)
        self.assertLessEqual(len(api_consumers.seen_callers()[0][2]), api_consumers.MAX_AGENT_LENGTH)

    def test_it_stops_recording_rather_than_growing_without_bound(self) -> None:
        """A client varying its User-Agent must not be able to grow this."""
        for index in range(api_consumers.MAX_DISTINCT_CALLERS + 50):
            api_consumers.note_request("/api/duts", "192.168.30.5", f"agent-{index}")

        self.assertEqual(len(api_consumers.seen_callers()), api_consumers.MAX_DISTINCT_CALLERS)

    def test_a_missing_client_address_is_recorded_rather_than_dropped(self) -> None:
        self.assertTrue(api_consumers.note_request("/api/duts", None, "curl/8.4.0"))
        self.assertEqual(api_consumers.seen_callers()[0][1], "unknown")


class WiredIntoTheAppTests(unittest.TestCase):
    """The probe only answers anything if requests actually reach it.

    Asserted through a real request rather than by calling `note_request`: a
    test of the function alone stays green while the middleware is unwired,
    which is exactly how the useful half of an instrument goes missing.
    """

    def setUp(self) -> None:
        api_consumers.reset()
        self.addCleanup(api_consumers.reset)

    def test_a_real_request_to_a_watched_path_is_recorded(self) -> None:
        from app.main import app

        with TestClient(app) as client:
            client.get("/api/duts", headers={"user-agent": "probe-test/1.0"})

        self.assertIn(
            "probe-test/1.0",
            {agent for _path, _client, agent in api_consumers.seen_callers()},
        )

    def test_a_real_request_elsewhere_is_not_recorded(self) -> None:
        from app.main import app

        with TestClient(app) as client:
            client.get("/api/version", headers={"user-agent": "probe-test/1.0"})

        self.assertEqual(api_consumers.seen_callers(), [])

    def test_the_probe_failing_does_not_fail_the_request(self) -> None:
        """An instrument that can break what it measures is worse than none."""
        from unittest import mock

        from app.main import app

        with mock.patch.object(api_consumers, "note_request", side_effect=RuntimeError("boom")):
            with self.assertLogs("app.main", level=logging.ERROR):
                with TestClient(app) as client:
                    response = client.get("/api/duts")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
