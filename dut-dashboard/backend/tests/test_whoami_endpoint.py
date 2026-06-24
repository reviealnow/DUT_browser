from __future__ import annotations

import types
import unittest

from app import main


def _request(host: str | None):
    client = types.SimpleNamespace(host=host) if host is not None else None
    return types.SimpleNamespace(client=client)


class SuggestedNameTests(unittest.TestCase):
    def test_ipv4_uses_last_octet(self) -> None:
        self.assertEqual(main._suggested_name("192.168.30.164"), "Guest-164")

    def test_non_ipv4_falls_back_to_host(self) -> None:
        self.assertEqual(main._suggested_name("fe80::1"), "Guest-fe80::1")

    def test_empty_host(self) -> None:
        self.assertEqual(main._suggested_name(""), "Guest")


class WhoAmIEndpointTests(unittest.TestCase):
    def test_shape_and_derive(self) -> None:
        result = main.whoami(_request("192.168.30.164"))
        self.assertEqual(result["ip"], "192.168.30.164")
        self.assertEqual(result["name"], "Guest-164")

    def test_missing_client(self) -> None:
        result = main.whoami(_request(None))
        self.assertEqual(result["ip"], "")
        self.assertEqual(result["name"], "Guest")


if __name__ == "__main__":
    unittest.main()
