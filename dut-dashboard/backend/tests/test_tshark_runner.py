"""Packet Monitor transport: the §B10 command builder, one-shot exec, remote kill.

docs/design-packet-monitor.md §B3 and §B10. Nothing here needs a real Host or a
real tshark: the "Host" is this machine, reached through the fake `ssh` in
``fixtures/fake_ssh.py`` (which logs in at a real pty and then execs the
command it was given), with HOME pointed at a temp directory, and "tshark" is a
stand-in that prints its argv.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import packet_decode
from app.services import tshark_runner as tr

FAKE_SSH = str(Path(__file__).parent / "fixtures" / "fake_ssh.py")
PASSWORD = "s3cr3t-on-the-bench"
SHA = "ab" * 32

#: Strings a display filter or a field name might carry to break out.
INJECTIONS = [
    "'; rm -rf ~ #",
    "$(id)",
    "`id`",
    "\\",
    "!x",
    "a\nb",
    "a\rb",
    'wlan.ssid == "a\'b" && !x $(id) `id` \\ 中',
    "中文 SSID ✓",
    "%s %n",
    "\t|;&<>*?[]{}~",
]

SHELLS = ["sh", "bash", "zsh", "csh", "tcsh", "fish"]

ARGV_TSHARK = """#!/bin/sh
# Stand-in for tshark: print the argv it was given, one JSON list.
exec "{python}" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"
"""


def _installed_shells() -> list[str]:
    return [path for path in (shutil.which(name) for name in SHELLS) if path]


class _HostSandbox(unittest.TestCase):
    """A temp HOME standing in for the Host's login user, and the fake ssh."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.tshark = self.bin / "tshark"
        self.tshark.write_text(ARGV_TSHARK.format(python=sys.executable))
        self.tshark.chmod(0o755)
        env = {
            "HOME": str(self.home),
            "FAKE_SSH_PASSWORD": PASSWORD,
        }
        self._env = patch.dict(os.environ, env)
        self._env.start()
        for key in ("FAKE_SSH_MODE", "FAKE_SSH_ANSWERED", "FAKE_SSH_DENIAL"):
            os.environ.pop(key, None)
        self.target = tr.HostTarget(
            collector_id="c1", host="10.0.0.9", user="dut",
            password=PASSWORD, ssh_binary=FAKE_SSH,
        )
        self.base64_flag = "-d" if _decodes("-d") else "-D"
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for child in self._children:
            if child.poll() is None:
                child.kill()
                child.wait()
        self._env.stop()
        self._tmp.cleanup()

    @property
    def workspace(self) -> Path:
        return self.home / ".dut-browser"

    def run_outer(self, shell: str, command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shell, "-c", command], capture_output=True, timeout=30,
            env={**os.environ, "HOME": str(self.home)},
        )

    def fake_tshark_process(self, *args: str) -> subprocess.Popen:
        """A long-lived process whose command name is `tshark` (a renamed sleep)."""
        link = self.bin / "sleepers" / f"{len(self._children)}" / "tshark"
        link.parent.mkdir(parents=True)
        link.symlink_to(shutil.which("sleep"))
        child = subprocess.Popen([str(link), *(args or ("60",))])
        self._children.append(child)
        return child


def _decodes(flag: str) -> bool:
    try:
        out = subprocess.run(["base64", flag], input=b"eA==", capture_output=True, timeout=5)
    except OSError:
        return False
    return out.returncode == 0 and out.stdout == b"x"


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


class OuterCommandTest(unittest.TestCase):
    """What the login shell sees: a fixed alphabet, whatever the input."""

    def _command(self, display_filter: str, flag: str | None = "-d") -> str:
        script = tr.filter_check_script(tshark_path="/usr/bin/tshark", display_filter=display_filter)
        return tr.build_remote_command(script, tr.new_job_id(), flag)

    def test_every_injection_stays_inside_the_allowed_characters(self) -> None:
        for flag in ("-d", "-D", None):
            for text in INJECTIONS:
                with self.subTest(flag=flag, text=text):
                    command = self._command(text, flag)
                    self.assertRegex(command, r"\A[A-Za-z0-9 ~/._;|>%=+-]*\Z")
                    for bad in ("'", '"', "\\", "!", "$", "`", "\n", "\r"):
                        self.assertNotIn(bad, command)

    def test_a_long_script_is_not_wrapped_onto_a_second_line(self) -> None:
        # GNU base64 wraps at 76 columns; csh rejects the newline that makes.
        command = self._command("x" * 1000)
        self.assertNotIn("\n", command)
        self.assertGreater(len(command), 1000)

    def test_the_script_size_limit(self) -> None:
        job = tr.new_job_id()
        tr.build_remote_command("#" * tr.MAX_SCRIPT_BYTES, job, "-d")
        with self.assertRaises(tr.CommandError):
            tr.build_remote_command("#" * (tr.MAX_SCRIPT_BYTES + 1), job, "-d")

    def test_job_ids_and_flags_are_checked(self) -> None:
        for job in ("", "ABCDEF12", "../../etc", "abc", "0123456789abcdef0123456789abcdef0"):
            with self.subTest(job=job), self.assertRaises(tr.CommandError):
                tr.build_remote_command("true\n", job, "-d")
        with self.assertRaises(tr.CommandError):
            tr.build_remote_command("true\n", tr.new_job_id(), "--decode")

    def test_the_probe_bootstrap_tries_both_decode_spellings(self) -> None:
        command = self._command("x", None)
        self.assertIn("base64 -d", command)
        self.assertIn("|| printf %s", command)
        self.assertIn("base64 -D", command)


class InnerScriptTest(unittest.TestCase):
    AVAILABLE = {"frame.number": "FT_UINT32", "wlan.ssid": "FT_BYTES", "wlan.bssid": "FT_ETHER"}

    def test_field_names_must_fullmatch_and_be_known(self) -> None:
        for name in ("wlan.ssid\n", "wlan.ssid;id", "-e", "wlan..ssid", "", "$(id)"):
            with self.subTest(name=name), self.assertRaises(tr.CommandError):
                tr.check_fields([name], self.AVAILABLE)
        with self.assertRaises(tr.CommandError):
            tr.check_fields(["wlan.nope"], self.AVAILABLE)
        self.assertEqual(tr.check_fields(["wlan.ssid"], self.AVAILABLE), ["wlan.ssid"])

    def test_display_filter_limits(self) -> None:
        tr.check_display_filter("x" * tr.MAX_FILTER_CHARS)
        with self.assertRaises(tr.CommandError):
            tr.check_display_filter("x" * (tr.MAX_FILTER_CHARS + 1))
        with self.assertRaises(tr.CommandError):
            tr.check_display_filter("a\x00b")

    def test_paths_come_only_from_digests_and_job_ids(self) -> None:
        for sha in ("AB" * 32, "ab" * 31, "../" + "ab" * 32, "ab" * 32 + "\n"):
            with self.subTest(sha=sha), self.assertRaises(tr.CommandError):
                tr.analysis_script(
                    job_id=tr.new_job_id(), sha256=sha, tshark_path="/usr/bin/tshark",
                    fields=["wlan.ssid"], available=self.AVAILABLE,
                )
        with self.assertRaises(tr.CommandError):
            tr.analysis_script(
                job_id=tr.new_job_id(), sha256=SHA, tshark_path="tshark",
                fields=["wlan.ssid"], available=self.AVAILABLE,
            )

    def test_the_script_deletes_itself_first(self) -> None:
        script = tr.stage_check_script(sha256=SHA)
        self.assertEqual(script.splitlines()[0], 'rm -f "$0"')


class ShellMatrixTest(_HostSandbox):
    """The outer string, run by every shell installed here, delivers the same bytes.

    CI installs zsh and csh (tests.yml) so the matrix is at least sh, bash, zsh
    and csh there; fish runs wherever it is installed.
    """

    def _argv_through(self, shell: str, display_filter: str) -> list[str]:
        script = tr.filter_check_script(tshark_path=str(self.tshark), display_filter=display_filter)
        command = tr.build_remote_command(script, tr.new_job_id(), self.base64_flag)
        result = self.run_outer(shell, command)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return json.loads(result.stdout)

    def test_the_filter_arrives_as_one_argument_byte_for_byte(self) -> None:
        shells = _installed_shells()
        self.assertIn(shutil.which("sh"), shells)
        for shell in shells:
            for text in INJECTIONS:
                with self.subTest(shell=shell, text=text):
                    argv = self._argv_through(shell, text)
                    self.assertEqual(argv, ["-n", "-r", "-", "-Y", text])

    def test_the_workspace_is_private_and_the_script_is_gone(self) -> None:
        for shell in _installed_shells():
            with self.subTest(shell=shell):
                self._argv_through(shell, "wlan")
                run_dir = self.workspace / "run"
                self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)
                self.assertEqual(list(run_dir.glob("*.sh")), [])

    def test_a_script_at_the_size_limit_survives_every_shell(self) -> None:
        body = 'printf "%s" ok\n'
        padding = "#" * (tr.MAX_SCRIPT_BYTES - len(body) - 1) + "\n"
        script = padding + body
        self.assertEqual(len(script.encode()), tr.MAX_SCRIPT_BYTES)
        command = tr.build_remote_command(script, tr.new_job_id(), self.base64_flag)
        for shell in _installed_shells():
            with self.subTest(shell=shell):
                result = self.run_outer(shell, command)
                self.assertEqual(result.stdout, b"ok", result.stderr.decode(errors="replace"))


class AnalysisScriptTest(_HostSandbox):
    def test_tshark_gets_the_mode_arguments_and_its_pid_is_recorded(self) -> None:
        job = tr.new_job_id()
        script = tr.analysis_script(
            job_id=job, sha256=SHA, tshark_path=str(self.tshark),
            fields=["frame.number", "wlan.ssid"],
            available=InnerScriptTest.AVAILABLE,
            display_filter='wlan.ssid == "x"',
        )
        result = tr.run_remote(self.target, script, job_id=tr.new_job_id(), base64_flag=self.base64_flag, timeout=20)
        self.assertTrue(result.ok, result.error_message())
        argv = json.loads(result.stdout)
        self.assertEqual(argv[:3], ["-n", "-r", f"captures/{SHA}.pcapng"])
        self.assertEqual(argv[argv.index("-Y") + 1], 'wlan.ssid == "x"')
        self.assertIn(f"aggregator={packet_decode.AGGREGATOR}", argv)
        self.assertEqual(argv[-4:], ["-e", "frame.number", "-e", "wlan.ssid"])
        pid_file = self.workspace / "run" / f"{job}.pid"
        self.assertTrue(pid_file.read_text().strip().isdigit())

    def test_no_filter_means_no_dash_y(self) -> None:
        script = tr.analysis_script(
            job_id=tr.new_job_id(), sha256=SHA, tshark_path=str(self.tshark),
            fields=["wlan.ssid"], available=InnerScriptTest.AVAILABLE,
        )
        result = tr.run_remote(self.target, script, job_id=tr.new_job_id(), base64_flag=self.base64_flag, timeout=20)
        self.assertNotIn("-Y", json.loads(result.stdout))


class RunRemoteTest(_HostSandbox):
    def run_script(self, script: str, **kwargs) -> tr.RemoteResult:
        kwargs.setdefault("timeout", 20)
        return tr.run_remote(self.target, script, job_id=tr.new_job_id(), base64_flag=self.base64_flag, **kwargs)

    def test_large_upload_and_large_reply_at_once_do_not_deadlock(self) -> None:
        payload = os.urandom(3 * 1024 * 1024)
        result = self.run_script("cat\n", stdin=payload)
        self.assertTrue(result.ok, result.error_message())
        self.assertEqual(result.stdout, payload)

    def test_stdin_from_a_file_and_stdout_to_a_file(self) -> None:
        source = self.root / "in.bin"
        source.write_bytes(os.urandom(256 * 1024))
        sink = self.root / "out.bin"
        result = self.run_script("cat\n", stdin=source, stdout_path=sink)
        self.assertTrue(result.ok, result.error_message())
        self.assertEqual(sink.read_bytes(), source.read_bytes())
        self.assertEqual(result.stdout, b"")

    def test_a_timeout_stops_the_step(self) -> None:
        started = time.monotonic()
        result = self.run_script("sleep 30\n", timeout=1)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("did not finish within 1 seconds", result.error_message())

    def test_the_stdout_cap_fails_the_step(self) -> None:
        result = self.run_script("yes x\n", stdout_cap=10_000)
        self.assertTrue(result.stdout_capped)
        self.assertLessEqual(len(result.stdout), 10_000)
        self.assertEqual(result.error_message(), "Too many rows -- narrow the filter.")

    def test_cancel_stops_the_step(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        result = self.run_script("sleep 30\n", cancel=cancel)
        self.assertTrue(result.cancelled)

    def test_tshark_diagnostics_come_back_on_stderr(self) -> None:
        result = self.run_script('echo "\\"wlan.nope\\" is not a valid protocol or protocol field" >&2; exit 2\n')
        self.assertEqual(result.returncode, 2)
        self.assertIn("wlan.nope", result.error_message())

    def test_a_wrong_password_is_reported_and_never_echoed(self) -> None:
        os.environ["FAKE_SSH_MODE"] = "echo"
        result = self.run_script("true\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(PASSWORD, result.stderr)
        self.assertIn("***", result.stderr)

    def test_an_unknown_host_key_is_refused_not_accepted(self) -> None:
        os.environ["FAKE_SSH_MODE"] = "hostkey"
        answered = self.root / "answered"
        os.environ["FAKE_SSH_ANSWERED"] = str(answered)
        with self.assertRaises(tr.RemoteError):
            self.run_script("true\n")
        self.assertFalse(answered.exists())


class RemoteKillTest(_HostSandbox):
    """Run as a key login: see the `key` mode in fixtures/fake_ssh.py for why."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["FAKE_SSH_MODE"] = "key"
        self.target = tr.HostTarget(
            collector_id="c1", host="10.0.0.9", user="dut",
            key_path="/nonexistent/key", ssh_binary=FAKE_SSH,
        )

    def _sleeping_tshark_script(self, job: str) -> str:
        link = self.bin / "tshark-sleep" / "tshark"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(shutil.which("sleep"))
        # What `analysis_script` does, with a tshark that never finishes.
        return tr._script([f"echo $$ > run/{job}.pid", "echo $$ > seen.pid", f"exec {link} 60"])

    def _seen_pid(self) -> int:
        deadline = time.monotonic() + 10
        path = self.workspace / "seen.pid"
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return int(path.read_text())
            time.sleep(0.05)
        self.fail("the remote step never started")

    def test_a_timeout_kills_the_remote_tshark_not_only_the_local_ssh(self) -> None:
        job = tr.new_job_id()
        result = tr.run_remote(
            self.target, self._sleeping_tshark_script(job), job_id=job,
            base64_flag=self.base64_flag, timeout=2, kill_on_stop=True,
        )
        self.assertTrue(result.timed_out)
        pid = self._seen_pid()
        self.assertTrue(_gone(pid), "remote tshark survived the timeout")
        self.assertFalse((self.workspace / "run" / f"{job}.pid").exists())

    def test_the_kill_refuses_a_pid_that_is_no_longer_tshark(self) -> None:
        (self.workspace / "run").mkdir(parents=True)
        bystander = subprocess.Popen([shutil.which("sleep"), "60"])
        self._children.append(bystander)
        job = tr.new_job_id()
        (self.workspace / "run" / f"{job}.pid").write_text(f"{bystander.pid}\n")
        self.assertFalse(tr.kill_remote(self.target, job, self.base64_flag))
        self.assertIsNone(bystander.poll())
        self.assertFalse((self.workspace / "run" / f"{job}.pid").exists())

    def test_the_kill_reaches_a_tshark_by_its_pid_file(self) -> None:
        (self.workspace / "run").mkdir(parents=True)
        victim = self.fake_tshark_process()
        job = tr.new_job_id()
        (self.workspace / "run" / f"{job}.pid").write_text(f"{victim.pid}\n")
        self.assertTrue(tr.kill_remote(self.target, job, self.base64_flag))
        victim.wait(timeout=5)
        self.assertEqual(victim.returncode, -signal.SIGTERM)

    def test_the_kill_waits_for_a_pid_file_that_is_late(self) -> None:
        (self.workspace / "run").mkdir(parents=True)
        victim = self.fake_tshark_process()
        job = tr.new_job_id()
        pid_file = self.workspace / "run" / f"{job}.pid"
        threading.Timer(1.5, lambda: pid_file.write_text(f"{victim.pid}\n")).start()
        self.assertTrue(tr.kill_remote(self.target, job, self.base64_flag))
        victim.wait(timeout=5)


class ParsingTest(unittest.TestCase):
    def test_versions(self) -> None:
        self.assertEqual(tr.parse_tshark_version("TShark (Wireshark) 3.6.2 (Git v3.6.2 packaged as 3.6.2-2)"), "3.6.2")
        self.assertEqual(tr.parse_tshark_version("TShark (Wireshark) 4.4.1."), "4.4.1")
        self.assertIsNone(tr.parse_tshark_version("tshark: command not found"))
        self.assertIsNone(tr.parse_tshark_version(None))

    def test_field_types(self) -> None:
        text = (
            "P\tIEEE 802.11 wireless LAN\twlan\n"
            "F\tSSID\twlan.ssid\tFT_BYTES\twlan\t\tBASE_NONE\t0x0\t\n"
            "F\tBSS Id\twlan.bssid\tFT_ETHER\twlan\t\tBASE_NONE\t0x0\t\n"
            "F\tbroken\n"
        )
        self.assertEqual(tr.parse_field_types(text), {"wlan.ssid": "FT_BYTES", "wlan.bssid": "FT_ETHER"})


if __name__ == "__main__":
    unittest.main()
