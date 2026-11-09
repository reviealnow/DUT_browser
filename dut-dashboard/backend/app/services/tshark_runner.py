"""Running tshark on a Host: one ssh per step, and the one place commands are built.

Packet Monitor (docs/design-packet-monitor.md, Part B) never runs tshark on the
dashboard server. Every remote step -- probe, stage, analyse, frame detail,
filter check, kill -- is its own one-shot ``ssh user@host <command>`` (§B3),
not a command on the collector's held-open session, which has a 15 s timeout
and is shared with the console probe.

**Command construction (§B10).** The login shell on the Host is not ours: it
may be csh or fish, and it parses the command string before anything else
does. So the string it sees is a fixed template whose only variable parts are a
hex job id and a base64 blob, written in pieces of at most `MAX_OUTER_WORD`
characters because BSD csh refuses longer words::

    umask 077; mkdir -p ~/.dut-browser/run;
    printf %s <B64 part 1> > ~/.dut-browser/run/<job>.b64; printf %s <part 2> >> ...;
    cat ~/.dut-browser/run/<job>.b64 | base64 -d > ~/.dut-browser/run/<job>.sh;
    rm -f ~/.dut-browser/run/<job>.b64; sh ~/.dut-browser/run/<job>.sh

(one line in reality). Every character in it is in ``[A-Za-z0-9 ~/._;|>%=+-]``, which means the same
thing in sh, bash, zsh, csh and fish. The blob is the real script, always run
by ``sh``; user data (display filter, field names) lives only inside it, quoted
with ``shlex.quote``, and never reaches the login shell. The script deletes
itself first.

**Remote kill (§B3).** Killing the local ssh does not stop a remote tshark: it
has no pty and dies only on SIGPIPE at its next write, which a selective filter
may not reach before the end of the file. So a run step writes its pid to
``run/<job>.pid`` and ``exec``s tshark; stopping it is a second one-shot ssh
that kills that pid **only if** the process is still a tshark.
"""

from __future__ import annotations

import base64
import os
import posixpath
import re
import shlex
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.serial import pty_ssh
from app.services import packet_decode, packet_modes

#: The workspace on the Host, relative to the login user's home (§B4).
REMOTE_DIR = "~/.dut-browser"

#: The whole script travels in one ssh argument. `ARG_MAX` and older shells are
#: not ours to know, so it is kept small (§B10).
MAX_SCRIPT_BYTES = 16 * 1024
#: The longest single word the login shell is handed. BSD csh rejects a word
#: past its buffer with "Word too long" (seen on the Ubuntu CI runner), so the
#: base64 blob is written in pieces no longer than this.
MAX_OUTER_WORD = 1000
MAX_FILTER_CHARS = 1024

DEFAULT_TIMEOUT_SEC = 180
DEFAULT_STDOUT_CAP = 200 * 1024 * 1024
#: tshark's diagnostics are kept for the error message; nobody needs megabytes.
STDERR_CAP = 64 * 1024
#: How long the kill script waits for a pid file that is not there yet (§B3).
KILL_WAIT_SEC = 10
CONNECT_TIMEOUT_SEC = 8
LOGIN_TIMEOUT_SEC = 20

#: What the login shell may see. No quote, backslash, `!`, `$`, backtick or
#: newline, so no shell gives any of it a second meaning.
OUTER_RE = re.compile(r"[A-Za-z0-9 ~/._;|>%=+-]*")
JOB_RE = re.compile(r"[0-9a-f]{8,32}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
BASE64_FLAGS = ("-d", "-D")
#: `checksum tool name` as the probe reports it → how to run it.
CHECKSUM_COMMANDS = {"sha256sum": "sha256sum", "shasum": "shasum -a 256"}

#: An empty pcap: 24-byte global header, radiotap link type. tshark compiles
#: the display filter before it reads input, so this checks a filter for the
#: cost of a login (§B9).
EMPTY_PCAP = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127)

#: Exit codes the step scripts use for their own refusals, so the caller can
#: tell "the Host said no" from "tshark failed".
EXIT_NO_WORKSPACE = 70
EXIT_UPLOAD_FAILED = 74
EXIT_LOW_SPACE = 75
EXIT_CHECKSUM = 76

#: Raised for a login that did not happen; the message is meant for the operator.
RemoteError = pty_ssh.PtySshError


class CommandError(ValueError):
    """A remote command this module refuses to build. Never sent anywhere."""


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def _check_job(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id):
        raise CommandError(f"Not a job id: {job_id!r}")
    return job_id


def check_sha256(sha256: str) -> str:
    """A digest is used in a remote path only once it is exactly 64 hex digits."""
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise CommandError(f"Not a sha256 digest: {sha256!r}")
    return sha256


# --------------------------------------------------------------------------
# §B10 outer layer: the string the login shell parses
# --------------------------------------------------------------------------


def build_remote_command(script: str, job_id: str, base64_flag: str | None) -> str:
    """The one function that builds what the Host's login shell sees.

    ``base64_flag`` is what the probe found decodes on this Host: ``-d``, or
    ``-D`` on older macOS. ``None`` is the probe's own bootstrap, which cannot
    know yet and tries both in one line -- ``||`` means the same in every shell
    this has to survive.
    """
    _check_job(job_id)
    raw = script.encode("utf-8")
    if len(raw) > MAX_SCRIPT_BYTES:
        raise CommandError(
            f"Remote script is {len(raw)} bytes; the limit is {MAX_SCRIPT_BYTES}."
        )
    # b64encode never wraps lines. GNU `base64` does, at 76 columns, and a
    # newline here is exactly what csh rejects.
    blob = base64.b64encode(raw).decode("ascii")
    target = f"{REMOTE_DIR}/run/{job_id}.sh"
    encoded = f"{REMOTE_DIR}/run/{job_id}.b64"
    if base64_flag is None:
        decode = (
            f"cat {encoded} | base64 -d > {target} || "
            f"cat {encoded} | base64 -D > {target}"
        )
    elif base64_flag in BASE64_FLAGS:
        decode = f"cat {encoded} | base64 {base64_flag} > {target}"
    else:
        raise CommandError(f"Unknown base64 decode flag: {base64_flag!r}")
    # BSD csh -- `csh` on Debian and Ubuntu, unlike macOS where it is tcsh --
    # refuses any single word longer than its buffer ("Word too long"), so the
    # blob goes up in pieces appended to a file rather than as one word.
    chunks = [blob[i:i + MAX_OUTER_WORD] for i in range(0, len(blob), MAX_OUTER_WORD)]
    writes = "; ".join(
        f"printf %s {chunk} {'>' if index == 0 else '>>'} {encoded}"
        for index, chunk in enumerate(chunks)
    )
    command = (
        f"umask 077; mkdir -p {REMOTE_DIR}/run; {writes}; {decode}; "
        f"rm -f {encoded}; sh {target}"
    )
    # Holds by construction; checked anyway, because the day it stops holding
    # is the day a filter reaches a csh.
    if not OUTER_RE.fullmatch(command):
        raise CommandError("Remote command left the allowed character set.")
    return command


# --------------------------------------------------------------------------
# §B10 inner layer: the scripts, always run by `sh`
# --------------------------------------------------------------------------


def _script(body: Iterable[str], tshark_path: str | None = None) -> str:
    lines = [
        'rm -f "$0"',
        "umask 077",
        f'cd "$HOME/.dut-browser" || exit {EXIT_NO_WORKSPACE}',
    ]
    if tshark_path:
        # A non-interactive ssh does not load the user's PATH (§B3), and
        # tshark may need its own directory for dumpcap and plugins.
        lines.append(
            f"PATH={shlex.quote(posixpath.dirname(tshark_path))}:$PATH; export PATH"
        )
    lines += [
        # `$low` prefixes everything that reads or writes captures. `nice` only
        # lowers CPU priority; on a Pi the shared resource is more often the SD
        # card, so the idle I/O class is added where `ionice` exists and works
        # (Linux). Tried rather than looked up: an ionice that refuses the idle
        # class must not fail the step.
        'low="nice -n 19"',
        'if ionice -c3 true >/dev/null 2>&1; then low="ionice -c3 nice -n 19"; fi',
    ]
    lines.extend(body)
    return "\n".join(lines) + "\n"


def _argv(words: Iterable[str]) -> str:
    return " ".join(shlex.quote(word) for word in words)


def _check_tshark_path(tshark_path: str) -> str:
    if not isinstance(tshark_path, str) or not tshark_path.startswith("/") or "\n" in tshark_path:
        raise CommandError("tshark path must be the absolute path the probe found.")
    return tshark_path


def check_display_filter(display_filter: str) -> str:
    """Length and the one byte argv cannot carry. Meaning is tshark's to judge (§B9)."""
    if not isinstance(display_filter, str):
        raise CommandError("Display filter must be text.")
    if len(display_filter) > MAX_FILTER_CHARS:
        raise CommandError(f"Display filter is longer than {MAX_FILTER_CHARS} characters.")
    if "\x00" in display_filter:
        raise CommandError("Display filter contains a NUL byte.")
    return display_filter


def check_fields(fields: Iterable[str], available: Mapping[str, str]) -> list[str]:
    """Field names must be names, and names this Host's tshark knows (§B10)."""
    checked = []
    for name in fields:
        if not isinstance(name, str) or not packet_modes.FIELD_RE.fullmatch(name):
            raise CommandError(f"Not a tshark field name: {name!r}")
        if name not in available:
            raise CommandError(f"This Host's tshark does not know the field {name}.")
        checked.append(name)
    if not checked:
        raise CommandError("No fields to extract.")
    return checked


def _exec_tshark(job_id: str | None, tshark_path: str, args: list[str]) -> list[str]:
    lines = []
    if job_id is not None:
        # `$$` is this shell; after `exec` it is tshark's pid, so the file names
        # the process a kill must reach (§B3).
        lines.append(f"echo $$ > run/{job_id}.pid")
    lines.append(f"exec $low {_argv([tshark_path, *args])}")
    return lines


def analysis_script(
    *,
    job_id: str,
    sha256: str,
    tshark_path: str,
    fields: Iterable[str],
    available: Mapping[str, str],
    display_filter: str = "",
) -> str:
    """Extract ``fields`` from a staged capture as TSV (§B2 step 2)."""
    _check_job(job_id)
    check_sha256(sha256)
    _check_tshark_path(tshark_path)
    names = check_fields(fields, available)
    check_display_filter(display_filter)
    args = ["-n", "-r", f"captures/{sha256}.pcapng"]
    if display_filter:
        args += ["-Y", display_filter]
    args += [
        "-T", "fields",
        "-E", "header=y",
        "-E", "separator=/t",
        "-E", "occurrence=a",
        "-E", f"aggregator={packet_decode.AGGREGATOR}",
    ]
    for name in names:
        args += ["-e", name]
    return _script(_exec_tshark(job_id, tshark_path, args), tshark_path)


def filter_check_script(*, tshark_path: str, display_filter: str) -> str:
    """Compile a display filter against an empty capture on stdin (§B9)."""
    _check_tshark_path(tshark_path)
    check_display_filter(display_filter)
    args = ["-n", "-r", "-", "-Y", display_filter]
    return _script(_exec_tshark(None, tshark_path, args), tshark_path)


def frame_detail_script(*, job_id: str, sha256: str, tshark_path: str, frame_number: int) -> str:
    """One frame's dissection tree. ``-c`` stops tshark at frame n (§B9)."""
    _check_job(job_id)
    check_sha256(sha256)
    _check_tshark_path(tshark_path)
    if isinstance(frame_number, bool) or not isinstance(frame_number, int) or frame_number < 1:
        raise CommandError(f"Not a frame number: {frame_number!r}")
    args = [
        "-n", "-r", f"captures/{sha256}.pcapng",
        "-c", str(frame_number), "-Y", f"frame.number=={frame_number}", "-T", "json",
    ]
    return _script(_exec_tshark(job_id, tshark_path, args), tshark_path)


def fields_script(*, tshark_path: str) -> str:
    """``tshark -G fields``: every field this tshark knows and its FT_* type."""
    _check_tshark_path(tshark_path)
    return _script(_exec_tshark(None, tshark_path, ["-G", "fields"]), tshark_path)


def stage_check_script(*, sha256: str) -> str:
    check_sha256(sha256)
    return _script([
        f"if [ -f captures/{sha256}.pcapng ]; then echo staged; else echo missing; fi",
    ])


def stage_script(*, job_id: str, sha256: str, size_bytes: int, checksum_tool: str, min_free_bytes: int) -> str:
    """Upload on stdin to a per-job temp name, verify, then `mv` into place (§B4).

    The `mv` is the only commit point: atomic within one directory, and two jobs
    that both reach it install identical content. A Host that would be left with
    less than ``min_free_bytes`` once this capture is on it refuses before a
    byte is written -- a full SD card on a Pi takes its consoles down with it.
    """
    _check_job(job_id)
    check_sha256(sha256)
    if checksum_tool not in CHECKSUM_COMMANDS:
        raise CommandError(f"Unknown checksum tool: {checksum_tool!r}")
    need_kb = -(-(int(size_bytes) + int(min_free_bytes)) // 1024)
    tmp = f"captures/{sha256}.{job_id}.tmp"
    return _script([
        "mkdir -p captures",
        "free=$(df -Pk . | awk 'NR==2 {print $4}')",
        "case \"$free\" in ''|*[!0-9]*) free=0;; esac",
        f'if [ "$free" -lt {need_kb} ]; then',
        f'  echo "dut-browser: only $free KB free on this Host; staging needs {need_kb} KB" >&2',
        f"  exit {EXIT_LOW_SPACE}",
        "fi",
        f'$low cat > {tmp} || {{ rm -f {tmp}; exit {EXIT_UPLOAD_FAILED}; }}',
        f"got=$($low {CHECKSUM_COMMANDS[checksum_tool]} {tmp} | awk '{{print $1}}')",
        f'if [ "$got" != "{sha256}" ]; then',
        f"  rm -f {tmp}",
        '  echo "dut-browser: checksum mismatch after upload" >&2',
        f"  exit {EXIT_CHECKSUM}",
        "fi",
        f"mv -f {tmp} captures/{sha256}.pcapng",
        "echo staged",
    ])


def delete_staged_script(*, sha256s: Iterable[str]) -> str:
    targets = [f"captures/{check_sha256(sha)}.pcapng" for sha in sha256s]
    return _script([f"rm -f {path}" for path in targets])


#: Shell fragment: sets `c` to the command name of pid `$pid`. Basename, because
#: macOS prints the path the process was exec'd with. Comparing it to `tshark`
#: is the guard against a pid that was recycled after our tshark ended.
_IS_TSHARK = (
    'c=$(ps -p "$pid" -o comm= 2>/dev/null | awk \'{print $1}\'); c=${c##*/}'
)


def kill_script(*, job_id: str, wait_sec: int = KILL_WAIT_SEC) -> str:
    """Stop the tshark a run step started, if it is still that tshark (§B3)."""
    _check_job(job_id)
    pid_file = f"run/{job_id}.pid"
    return _script([
        "i=0",
        f'while [ ! -f {pid_file} ] && [ "$i" -lt {int(wait_sec)} ]; do sleep 1; i=$((i+1)); done',
        f"[ -f {pid_file} ] || exit 0",
        f'pid=$(cat {pid_file} 2>/dev/null)',
        'case "$pid" in',
        "  ''|*[!0-9]*) ;;",
        f"  *) {_IS_TSHARK}",
        '     if [ "$c" = tshark ]; then kill "$pid" 2>/dev/null && echo killed; fi;;',
        "esac",
        f"rm -f {pid_file}",
    ])


#: Where tshark lives when `command -v` cannot see it from a non-interactive
#: login (§B5).
TSHARK_CANDIDATES = (
    "/usr/bin/tshark",
    "/usr/local/bin/tshark",
    "/opt/homebrew/bin/tshark",
    "/Applications/Wireshark.app/Contents/MacOS/tshark",
)


def probe_script(*, job_id: str, active_jobs: Iterable[str] = ()) -> str:
    """Find tshark and the tools, report space and staged captures, sweep (§B5).

    Reads on stdin the sha256 digests that still have a Files row, one per
    line -- stdin rather than the script, so the 16 KB limit does not bound how
    many files the workspace may hold. Prints ``key=value`` lines.

    The sweeps: staged captures no Files row knows; ``*.tmp`` older than an
    hour, which a stage that lost its connection could not remove itself; and
    pid files of jobs nobody owns any more (after a backend restart), whose
    tshark is killed under the same guard as `kill_script`. Jobs in
    ``active_jobs`` -- and any process one of them owns -- are left alone, so an
    on-demand probe never kills a running analysis.
    """
    _check_job(job_id)
    active = [_check_job(job) for job in active_jobs]
    keep = f"run/{job_id}.keep"
    candidates = " ".join(TSHARK_CANDIDATES)
    return _script([
        "mkdir -p captures run",
        'echo "os=$(uname -s)"',
        "t=$(command -v tshark 2>/dev/null)",
        'case "$t" in /*) ;; *) t=;; esac',
        f'if [ -z "$t" ]; then for c in {candidates}; do if [ -x "$c" ]; then t=$c; break; fi; done; fi',
        'echo "tshark_path=$t"',
        'if [ -n "$t" ]; then echo "tshark_version=$("$t" --version 2>/dev/null | head -n 1)"; fi',
        "if command -v sha256sum >/dev/null 2>&1; then echo checksum=sha256sum;",
        "elif command -v shasum >/dev/null 2>&1; then echo checksum=shasum;",
        "else echo checksum=; fi",
        "if printf %s eA== | base64 -d >/dev/null 2>&1; then echo base64=-d;",
        "elif printf %s eA== | base64 -D >/dev/null 2>&1; then echo base64=-D;",
        "else echo base64=; fi",
        "echo \"free_kb=$(df -Pk . | awk 'NR==2 {print $4}')\"",
        f"cat > {keep}",
        "$low find captures -type f -name '*.tmp' -mmin +60 -exec rm -f {} + 2>/dev/null",
        "for f in captures/*.pcapng; do",
        '  [ -f "$f" ] || continue',
        '  s=${f#captures/}; s=${s%.pcapng}',
        f'  if grep -qx "$s" {keep}; then',
        "    echo \"staged=$s $(wc -c < \"$f\" | tr -d ' ')\"",
        '  else rm -f "$f"; echo "swept=$s"; fi',
        "done",
        f"rm -f {keep}",
        f'active=" {" ".join(active)} "',
        'apids=" "',
        f"for j in {' '.join(active)}; do if [ -f run/$j.pid ]; then apids=\"$apids$(cat run/$j.pid) \"; fi; done"
        if active else ":",
        "for p in run/*.pid; do",
        '  [ -f "$p" ] || continue',
        '  j=${p#run/}; j=${j%.pid}',
        '  case "$active" in *" $j "*) continue;; esac',
        '  pid=$(cat "$p" 2>/dev/null)',
        '  case "$pid" in',
        "    ''|*[!0-9]*) ;;",
        f'    *) case "$apids" in *" $pid "*) ;; *) {_IS_TSHARK}',
        '         if [ "$c" = tshark ]; then kill "$pid" 2>/dev/null && echo "killed=$j"; fi;; esac;;',
        "  esac",
        '  rm -f "$p"',
        "done",
        "find run -type f -mmin +1440 -exec rm -f {} + 2>/dev/null",
        "exit 0",
    ])


# --------------------------------------------------------------------------
# §B3 one-shot exec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HostTarget:
    """How to log into one Host. The password is memory-only and never printed."""

    collector_id: str
    host: str
    user: str
    port: int = 22
    key_path: str | None = None
    password: str = field(default="", repr=False)
    ssh_binary: str = "ssh"


def target_for(registry, collector_id: str, ssh_binary: str = "ssh") -> HostTarget:
    """Build a target from the collector registry.

    `credentials_for` raises after a backend restart, when the password is no
    longer held; that error is the one to show ("enter it again") and is not
    retried here (§B3 *Job lifecycle*).
    """
    collector = registry.get(collector_id)
    credentials = registry.credentials_for(collector_id)
    return HostTarget(
        collector_id=collector_id,
        host=collector.ip,
        user=collector.user,
        port=int(collector.port),
        key_path=credentials.get("key_path") or None,
        password=credentials.get("password") or "",
        ssh_binary=ssh_binary,
    )


@dataclass
class RemoteResult:
    returncode: int | None
    stdout: bytes
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    stdout_capped: bool = False
    timeout_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not (self.timed_out or self.cancelled or self.stdout_capped)

    def error_message(self) -> str:
        if self.cancelled:
            return "Cancelled."
        if self.timed_out:
            return f"The Host did not finish within {int(self.timeout_sec)} seconds."
        if self.stdout_capped:
            return "Too many rows -- narrow the filter."
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        if lines:
            return lines[-1]
        return f"The remote step exited with status {self.returncode}."


class _Pump:
    """The three pipes, each on its own thread (§B3).

    A 50 MB upload on stdin and a large reply on stdout in flight at once
    deadlock on full pipe buffers if one thread serves both.
    """

    def __init__(self, process: subprocess.Popen[bytes], stdin_data, stdout_path: Path | None, stdout_cap: int) -> None:
        self.process = process
        self.stop = threading.Event()
        self.stdout_cap = stdout_cap
        self.stdout_bytes = 0
        self.capped = False
        self._out = bytearray()
        self._err = bytearray()
        self._sink = open(stdout_path, "wb") if stdout_path is not None else None
        self._threads = [
            threading.Thread(target=self._write, args=(stdin_data,), daemon=True),
            threading.Thread(target=self._read_out, daemon=True),
            threading.Thread(target=self._read_err, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _write(self, data) -> None:
        stream = self.process.stdin
        try:
            if data is None:
                return
            if isinstance(data, (bytes, bytearray)):
                chunks: Iterable[bytes] = (bytes(data[i:i + 65536]) for i in range(0, len(data), 65536))
                self._write_all(stream, chunks)
            else:
                with open(data, "rb") as handle:
                    self._write_all(stream, iter(lambda: handle.read(65536), b""))
        except (BrokenPipeError, OSError, ValueError):
            # The remote end stopped reading -- a refusal (low space) or a
            # failure it will explain on stderr.
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _write_all(self, stream, chunks: Iterable[bytes]) -> None:
        for chunk in chunks:
            view = memoryview(chunk)
            while view and not self.stop.is_set():
                written = stream.write(view)
                view = view[written or 0:]
            if self.stop.is_set():
                return

    def _read_out(self) -> None:
        fd = self.process.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            if self.capped:
                continue  # keep draining so the child is not blocked on a full pipe
            if self.stdout_bytes + len(chunk) > self.stdout_cap:
                self.capped = True
                continue
            self.stdout_bytes += len(chunk)
            if self._sink is not None:
                self._sink.write(chunk)
            else:
                self._out += chunk

    def _read_err(self) -> None:
        fd = self.process.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            room = STDERR_CAP - len(self._err)
            if room > 0:
                self._err += chunk[:room]

    def finish(self) -> tuple[bytes, bytes]:
        self.stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        if self._sink is not None:
            self._sink.close()
        return bytes(self._out), bytes(self._err)


def run_remote(
    target: HostTarget,
    script: str,
    *,
    job_id: str,
    base64_flag: str | None,
    stdin: bytes | Path | None = None,
    stdout_path: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    stdout_cap: int = DEFAULT_STDOUT_CAP,
    cancel: threading.Event | None = None,
    kill_on_stop: bool = False,
    login_timeout: float = LOGIN_TIMEOUT_SEC,
) -> RemoteResult:
    """Run one step on the Host and wait for it, within its limits.

    stdin is written only after the login, so a password prompt never receives
    pcap bytes. On timeout, cancel or the stdout cap the local ssh is stopped,
    and with ``kill_on_stop`` the remote tshark is too (`kill_remote`).

    Raises `RemoteError` when the login itself does not happen.
    """
    command = build_remote_command(script, job_id, base64_flag)
    argv = pty_ssh.build_ssh_argv(
        user=target.user,
        host=target.host,
        port=target.port,
        key_path=target.key_path,
        remote_command=command,
        connect_timeout=CONNECT_TIMEOUT_SEC,
        ssh_binary=target.ssh_binary,
    )
    child = pty_ssh.spawn_and_authenticate(argv, password=target.password, timeout=login_timeout)
    deadline = time.monotonic() + timeout
    pump = _Pump(child.process, stdin, stdout_path, stdout_cap)
    timed_out = cancelled = False
    try:
        while child.process.poll() is None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if pump.capped:
                break
            try:
                child.process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        stopped = timed_out or cancelled or pump.capped
        if stopped:
            child.close()
        returncode = child.process.poll()
        stdout, stderr = pump.finish()
    finally:
        child.close()
    if stopped and kill_on_stop:
        kill_remote(target, job_id, base64_flag)
    return RemoteResult(
        returncode=returncode,
        stdout=stdout,
        stderr=pty_ssh.scrub(stderr.decode("utf-8", "replace"), target.password),
        timed_out=timed_out,
        cancelled=cancelled,
        stdout_capped=pump.capped,
        timeout_sec=timeout,
    )


def kill_remote(target: HostTarget, job_id: str, base64_flag: str | None) -> bool:
    """Stop the tshark that `job_id` started on the Host. True if one was killed.

    Best effort by design: a Host that cannot be reached now is swept on its
    next probe (§B5), so a failure here is reported as False, never raised.
    """
    try:
        result = run_remote(
            target,
            kill_script(job_id=job_id),
            job_id=new_job_id(),
            base64_flag=base64_flag,
            timeout=KILL_WAIT_SEC + 20,
        )
    except (RemoteError, CommandError):
        return False
    return result.ok and b"killed" in result.stdout


# --------------------------------------------------------------------------
# Parsing what the Host prints
# --------------------------------------------------------------------------

_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")


def parse_tshark_version(line: str | None) -> str | None:
    """``TShark (Wireshark) 3.6.2 (Git v3.6.2 ...)`` → ``"3.6.2"``."""
    if not line:
        return None
    match = _VERSION_RE.search(line)
    return ".".join(match.groups()) if match else None


def version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def parse_field_types(text: str) -> dict[str, str]:
    """``tshark -G fields`` → field name → FT_* type.

    Field lines are ``F<TAB>name<TAB>abbrev<TAB>type<TAB>...``; protocol lines
    (``P``) carry no type and are skipped.
    """
    types: dict[str, str] = {}
    for line in text.split("\n"):
        parts = line.rstrip("\r").split("\t")
        if len(parts) >= 4 and parts[0] == "F" and parts[3].startswith("FT_"):
            types[parts[2]] = parts[3]
    return types
