"""Where Packet Monitor reaches into existing routes.

Two hooks, each a few lines in a router that predates Packet Monitor
(docs/design-packet-monitor.md): a collector connect starts a background Host
probe (§B5), and a Files delete hands the deleted row to Packet Monitor's
*Deletion* rule (§B4). Both are skipped when Packet Monitor is not running.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api import collectors_api, files_api
from app.collector.registry import CollectorError


class ConnectHookTest(unittest.TestCase):
    """§B5: a Host is probed on every connect, without holding the connect up."""

    def _request(self, registry, jobs):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            collector_registry=registry, packet_jobs=jobs,
        )))

    def test_a_successful_connect_starts_a_background_probe(self) -> None:
        registry = MagicMock()
        registry.connect.return_value = {}
        registry.status.return_value = {"id": "pi"}
        jobs = MagicMock()
        collectors_api.connect_collector("pi", self._request(registry, jobs), {"role": "admin"})
        jobs.probe_async.assert_called_once_with("pi")
        jobs.probe.assert_not_called()

    def test_a_failed_connect_does_not_probe(self) -> None:
        registry = MagicMock()
        registry.connect.side_effect = CollectorError("no password held")
        jobs = MagicMock()
        with self.assertRaises(HTTPException):
            collectors_api.connect_collector("pi", self._request(registry, jobs), {"role": "admin"})
        jobs.probe_async.assert_not_called()


class FileDeleteHookTest(unittest.TestCase):
    """§B4: the Files delete route passes the deleted row on, after deleting it."""

    def test_the_deleted_row_reaches_packet_monitor(self) -> None:
        row = {"id": 5, "sha256": "a" * 64, "filepath": "/x"}
        order = []
        jobs = MagicMock()
        jobs.file_deleted.side_effect = lambda *args: order.append(("packets", args))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(packet_jobs=jobs)))
        with patch.object(files_api.file_service, "get_file_by_id", return_value=row), \
                patch.object(files_api.file_service, "delete_file",
                             side_effect=lambda r: order.append(("files", r["id"]))):
            self.assertEqual(files_api.delete_file(5, request), {"ok": True})
        self.assertEqual(order, [("files", 5), ("packets", (5, "a" * 64))])

    def test_without_packet_monitor_the_delete_is_unchanged(self) -> None:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        with patch.object(files_api.file_service, "get_file_by_id", return_value={"id": 5}), \
                patch.object(files_api.file_service, "delete_file"):
            self.assertEqual(files_api.delete_file(5, request), {"ok": True})


if __name__ == "__main__":
    unittest.main()
