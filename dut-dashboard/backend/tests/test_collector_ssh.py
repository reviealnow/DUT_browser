"""The password login itself, over a real pty, against a fake `ssh`.

Every other test in this file's neighbourhood can stub a transport. This one
must not: the whole reason `collector/ssh_session.py` exists is that `ssh` reads
a password from its controlling terminal and a piped stdin cannot answer it, so
a test that pipes bytes at a mock would pass against the very implementation
this module was written to replace. `tests/fixtures/fake_ssh.py` opens /dev/tty
exactly as ssh does, which is what makes these assertions mean anything.

What is deliberately NOT covered here: reaching a real collector. That needs the
bench, and the bench is shared.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from app.collector.ssh_session import CollectorSshError, open_session

FAKE_SSH = str(Path(__file__).parent / "fixtures" / "fake_ssh.py")
PASSWORD = "s3cr3t-on-the-bench"


class PtyPasswordLoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in ("FAKE_SSH_MODE", "FAKE_SSH_PASSWORD", "FAKE_SSH_ANSWERED")
        }
        os.environ["FAKE_SSH_PASSWORD"] = PASSWORD
        os.environ.pop("FAKE_SSH_MODE", None)
        os.environ.pop("FAKE_SSH_ANSWERED", None)
        self.sessions = []

    def tearDown(self) -> None:
        for session in self.sessions:
            session.close()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def login(self, password: str = PASSWORD):
        session = open_session(
            ip="10.0.0.9", user="dut", password=password,
            ssh_binary=FAKE_SSH, login_timeout=8,
        )
        self.sessions.append(session)
        return session

    def test_a_correct_password_is_typed_at_the_terminal(self) -> None:
        """The mechanism, end to end: a prompt on a tty, answered, then a shell."""
        session = self.login()
        self.assertTrue(session.alive())
        # Read off the far end at login rather than assumed from configuration:
        # the fake execs the real remote command, so this name came back through
        # `hostname` on the other side exactly as it will on a Pi.
        self.assertEqual(session.reported_hostname, os.uname().nodename)

    def test_the_session_is_held_open_rather_than_run_and_exited(self) -> None:
        """What the green light on the card actually claims.

        A login that succeeded and exited would leave `alive()` true for a
        moment and false forever after, which is a card that goes dark for no
        reason the operator can see.
        """
        session = self.login()
        time.sleep(0.4)
        self.assertTrue(session.alive())

    def test_closing_reaps_the_child(self) -> None:
        session = self.login()
        pid = session.pid
        session.close()
        self.assertFalse(session.alive())
        with self.assertRaises(OSError):
            # Reaped: the pid is gone from this process's children entirely.
            os.waitpid(pid, os.WNOHANG)

    def test_close_is_safe_to_call_twice(self) -> None:
        session = self.login()
        session.close()
        session.close()
        self.assertFalse(session.alive())

    def test_a_wrong_password_is_reported_as_a_refusal(self) -> None:
        session = None
        with self.assertRaises(CollectorSshError) as caught:
            session = self.login(password="not-the-one")
        self.assertIsNone(session)
        self.assertIn("refused", str(caught.exception).lower())

    def test_an_unknown_host_key_is_never_answered_for_the_operator(self) -> None:
        """The prompt is reported, not accepted.

        Answering "yes" here would be this dashboard deciding to trust a machine
        nobody has identified -- on the one feature whose whole job is collecting
        logs off it. The fake leaves a file behind if it is ever answered.
        """
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "answered"
            os.environ["FAKE_SSH_MODE"] = "hostkey"
            os.environ["FAKE_SSH_ANSWERED"] = str(marker)
            with self.assertRaises(CollectorSshError) as caught:
                self.login()
            self.assertFalse(marker.exists(), "the host-key question was answered")
        self.assertIn("host key", str(caught.exception).lower())

    def test_the_password_never_reaches_the_error_message(self) -> None:
        """The abnormal remote: one that repeats what it was sent.

        Real ssh turns echo off while it reads, so in practice there is nothing
        to leak. This is the case where there is -- and an HTTP error body is
        the last place a bench password should turn up.
        """
        os.environ["FAKE_SSH_MODE"] = "echo"
        with self.assertRaises(CollectorSshError) as caught:
            self.login()
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_a_silent_box_times_out_rather_than_hanging(self) -> None:
        """Reachable, alive, and never speaks. The operator gets an answer.

        The connection is up, so ssh does not exit and there is nothing to read
        -- exactly the shape that leaves a UI spinning forever if the caller
        waits for a prompt with no deadline of its own.
        """
        os.environ["FAKE_SSH_MODE"] = "silent"
        with self.assertRaises(CollectorSshError) as caught:
            open_session(
                ip="10.0.0.9", user="dut", password=PASSWORD,
                ssh_binary=FAKE_SSH, login_timeout=1,
            )
        self.assertIn("no password prompt", str(caught.exception).lower())

    def test_ssh_exiting_early_is_reported_in_its_own_words(self) -> None:
        """Whatever the transport said last, rather than a generic failure.

        The messages that matter here come from ssh and not from this codebase
        -- "Host key verification failed", "No route to host" -- so the last
        line it printed is the most useful thing to show.
        """
        with self.assertRaises(CollectorSshError) as caught:
            open_session(
                ip="10.0.0.9", user="dut", password=PASSWORD,
                ssh_binary="/bin/cat", login_timeout=3,
            )
        self.assertIn("usage", str(caught.exception).lower())

    def test_a_login_that_fails_after_the_prompt_does_not_hang(self) -> None:
        """Regression, and the failure mode was a hang rather than a wrong answer.

        A child that took a pty as its controlling terminal cannot finish
        exiting while that terminal is unread. Measured here before the drain
        was moved earlier: `ps` state `?Es`, `poll()` answering None forever,
        and the caller reporting its own timeout instead of the reason ssh had
        already printed on stderr. The assertion is on the clock because that is
        what actually broke.
        """
        os.environ["FAKE_SSH_MODE"] = "echo"
        started = time.monotonic()
        with self.assertRaises(CollectorSshError) as caught:
            self.login()  # login_timeout is 8s; a hang would spend all of it
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertIn("remote said", str(caught.exception))

    def test_a_missing_ssh_binary_fails_instead_of_waiting(self) -> None:
        with self.assertRaises(CollectorSshError):
            open_session(
                ip="10.0.0.9", user="dut", password=PASSWORD,
                ssh_binary="/nonexistent/ssh", login_timeout=3,
            )


class TheCommandChannelTest(unittest.TestCase):
    """Running commands on the collector over the session that is already open.

    The fake `ssh` execs the remote command it was handed, so `exec sh` on the
    far end is a real shell and these run for real -- which is the only way the
    sentinel protocol is worth testing at all.
    """

    def setUp(self) -> None:
        os.environ["FAKE_SSH_PASSWORD"] = PASSWORD
        os.environ.pop("FAKE_SSH_MODE", None)
        self.session = open_session(
            ip="10.0.0.9", user="dut", password=PASSWORD,
            ssh_binary=FAKE_SSH, login_timeout=8,
        )
        self.addCleanup(self.session.close)

    def test_it_returns_what_the_command_printed(self) -> None:
        output, status = self.session.run("echo hello-from-the-collector")
        self.assertEqual(output, "hello-from-the-collector")
        self.assertEqual(status, 0)

    def test_a_failing_command_reports_its_status_and_its_message(self) -> None:
        # `2>&1` on the remote side: the message somebody needs is on stderr,
        # and a status with no words next to it explains nothing.
        output, status = self.session.run("ls /definitely/not/here")
        self.assertNotEqual(status, 0)
        self.assertTrue(output, "a failing command said nothing at all")

    def test_two_commands_in_a_row_do_not_read_each_other(self) -> None:
        first, _ = self.session.run("echo one")
        second, _ = self.session.run("echo two")
        self.assertEqual((first, second), ("one", "two"))

    def test_output_containing_a_sentinel_shaped_string_is_not_truncated(self) -> None:
        """The marker is random per call for exactly this.

        A fixed sentinel would let a device named after it -- or any command
        printing it -- end its own capture early, and the caller would read a
        partial answer as a complete one.
        """
        output, status = self.session.run("echo __DUT_DONE_0__; echo after")
        self.assertIn("after", output)
        self.assertEqual(status, 0)

    def test_running_on_a_closed_session_is_refused(self) -> None:
        self.session.close()
        with self.assertRaises(CollectorSshError):
            self.session.run("echo nope")


if __name__ == "__main__":
    unittest.main()
