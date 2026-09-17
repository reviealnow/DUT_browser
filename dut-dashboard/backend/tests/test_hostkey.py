"""Trusting a host key from the card: what it writes, and what it refuses.

The refusals are the feature. Accepting whatever a host presents is one ssh
option (`StrictHostKeyChecking=accept-new`) and needs no code; what this module
adds is that a **named** key is written, so the operator's answer is about the
fingerprint they were looking at rather than about whatever answers next.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.collector import hostkey


ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIExample"
KEY_LINE = f"192.168.30.145 ssh-ed25519 {ED25519}"
FINGERPRINT = "SHA256:wMVcMplM4ziqK26b2UzKWLQWYN2VlwR//TP7r6scKIc"
OTHER = "SHA256:someoneElsesKeyEntirely0000000000000000000"


def keygen_listing(fingerprint: str = FINGERPRINT) -> str:
    return f"256 {fingerprint} 192.168.30.145 (ED25519)\n"


class HostKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.known_hosts = Path(self._dir.name) / ".ssh" / "known_hosts"
        patch = mock.patch.object(hostkey, "KNOWN_HOSTS", self.known_hosts)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _runner(*, scan_out: str = KEY_LINE, known_out: str = "",
                listing: str = None, scan_err: str = ""):
        """Stand in for ssh-keyscan and ssh-keygen, by which one is asked."""
        listing = listing if listing is not None else keygen_listing()

        def run(argv, stdin=None):
            if argv[0] == hostkey.KEYSCAN:
                return scan_out, scan_err
            if "-lf" in argv:
                return (listing if stdin and stdin.strip() else ""), ""
            if "-F" in argv:
                return known_out, ""
            if "-H" in argv:
                return f"|1|hashed|hashed= ssh-ed25519 {ED25519}\n", ""
            return "", ""

        return mock.patch.object(hostkey, "_run", side_effect=run)

    # -- reading -------------------------------------------------------

    def test_status_reports_what_is_presented_and_what_is_on_record(self) -> None:
        with self._runner(known_out=KEY_LINE):
            answer = hostkey.status("192.168.30.145", 22)
        self.assertTrue(answer["known"])
        self.assertTrue(answer["matches"])
        self.assertEqual(answer["presented_keys"][0]["fingerprint"], FINGERPRINT)

    def test_an_unreachable_host_is_not_reported_as_a_changed_key(self) -> None:
        """`matches` is None when nobody could look.

        False would raise the alarm that must not cry wolf: a box that is off
        and a box whose key changed are not the same finding, and only one of
        them means stop.
        """
        with self._runner(scan_out="", scan_err="connect: No route to host", known_out=KEY_LINE):
            answer = hostkey.status("192.168.30.145", 22)
        self.assertIsNone(answer["matches"])
        self.assertTrue(answer["known"])
        self.assertIn("No route to host", answer["scan_error"])

    def test_a_failed_scan_carries_the_tool_s_own_words(self) -> None:
        with self._runner(scan_out="", scan_err="connect: Connection refused"):
            with self.assertRaises(hostkey.HostKeyError) as caught:
                hostkey.scan("192.168.30.145", 22)
        self.assertIn("Connection refused", str(caught.exception))

    # -- writing -------------------------------------------------------

    def test_trusting_the_confirmed_key_writes_it_hashed(self) -> None:
        with self._runner():
            answer = hostkey.trust("192.168.30.145", 22, FINGERPRINT)
        self.assertEqual(answer["trusted"], FINGERPRINT)
        written = self.known_hosts.read_text()
        self.assertIn(ED25519, written)
        # Hashed, as `ssh-keyscan -H` writes them: this file should not double
        # as a readable inventory of the bench.
        self.assertTrue(written.startswith("|1|"), written[:20])
        self.assertEqual(self.known_hosts.stat().st_mode & 0o777, 0o600)

    def test_a_key_that_changed_between_reading_and_pressing_is_refused(self) -> None:
        """The race the whole design exists to close.

        The operator confirmed one fingerprint; the host is presenting another.
        Writing it anyway would make the button mean "accept whatever answers",
        which is the option this one was chosen over.
        """
        with self._runner(listing=keygen_listing(OTHER)):
            with self.assertRaises(hostkey.HostKeyError) as caught:
                hostkey.trust("192.168.30.145", 22, FINGERPRINT)
        self.assertIn("not presenting that key", str(caught.exception))
        self.assertFalse(self.known_hosts.exists())

    def test_a_host_that_already_has_a_key_is_never_overwritten(self) -> None:
        """A reimaged box and a replaced one look identical from here."""
        with self._runner(known_out=KEY_LINE):
            with self.assertRaises(hostkey.HostKeyError) as caught:
                hostkey.trust("192.168.30.145", 22, FINGERPRINT)
        self.assertIn("ssh-keygen -R", str(caught.exception))
        self.assertFalse(self.known_hosts.exists())

    def test_an_empty_fingerprint_writes_nothing(self) -> None:
        with self._runner():
            with self.assertRaises(hostkey.HostKeyError):
                hostkey.trust("192.168.30.145", 22, "   ")
        self.assertFalse(self.known_hosts.exists())

    def test_a_non_default_port_is_named_the_way_openssh_names_it(self) -> None:
        # `[host]:port`, or every entry for a box on 2222 is looked up under the
        # address alone and answers about the wrong daemon.
        self.assertEqual(hostkey._target("10.0.0.9", 22), "10.0.0.9")
        self.assertEqual(hostkey._target("10.0.0.9", 2222), "[10.0.0.9]:2222")


if __name__ == "__main__":
    unittest.main()
