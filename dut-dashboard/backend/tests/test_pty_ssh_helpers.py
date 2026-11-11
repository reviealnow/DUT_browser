"""The shared ssh pieces in `serial/pty_ssh.py`: argv and login (§B3, Q5).

Only Packet Monitor's runner uses them so far; `open_session` and the serial
worker still build their own (docs/design-packet-monitor.md §B14 Q5).
"""

from __future__ import annotations

import unittest

from app.serial import pty_ssh


class PtySshHelpersTest(unittest.TestCase):
    def test_a_key_login_never_asks_anyone(self) -> None:
        argv = pty_ssh.build_ssh_argv(user="u", host="h", remote_command="cmd", key_path="/k", port=2222)
        self.assertEqual(argv[:5], ["ssh", "-o", "BatchMode=yes", "-i", "/k"])
        self.assertEqual(argv[-2:], ["u@h", "cmd"])
        self.assertIn("2222", argv)

    def test_a_password_login_cannot_be_satisfied_by_an_agent_key(self) -> None:
        argv = pty_ssh.build_ssh_argv(user="u", host="h", remote_command="cmd")
        self.assertIn("PubkeyAuthentication=no", argv)
        self.assertNotIn("BatchMode=yes", argv)
        self.assertNotIn("StrictHostKeyChecking=no", " ".join(argv))

    def test_key_path_starts_on_plain_pipes(self) -> None:
        child = pty_ssh.spawn_and_authenticate(["/bin/sh", "-c", "cat"])
        try:
            out, _ = child.process.communicate(b"bytes\x00\xff", timeout=10)
            self.assertEqual(out, b"bytes\x00\xff")
        finally:
            child.close()

    def test_a_missing_binary_is_a_login_error(self) -> None:
        with self.assertRaises(pty_ssh.PtySshError):
            pty_ssh.spawn_and_authenticate(["/nonexistent/ssh"])


if __name__ == "__main__":
    unittest.main()
