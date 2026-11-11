"""Hosts that can run tshark: the preflight probe and the staged-capture store.

docs/design-packet-monitor.md §B4 (remote workspace, staging, eviction) and
§B5 (probe). Every remote step goes through `tshark_runner`, the only place a
command for a Host is built.

**Probe results are memory-only.** A Host is probed on every connect, so a
result outliving the backend process would only ever be a stale one.

**Two floors protect the Host**, because a Pi that runs out of SD card takes
the DUT consoles it serves down with it: a Host with less than
`MIN_FREE_BYTES` free is not selectable, and the captures staged on one Host
never total more than `STAGE_BUDGET_BYTES`, least recently used evicted first
-- except captures a queued or running analysis still needs ("pinned").
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import PACKETS_DIR
from app.db import workspace
from app.services import tshark_runner as tr

#: Q3: older tshark is refused, with the version that was found.
TSHARK_FLOOR = (3, 6, 0)
MIN_FREE_BYTES = 200 * 1024 * 1024
STAGE_BUDGET_BYTES = 200 * 1024 * 1024
PROBE_TIMEOUT_SEC = 60
FIELDS_TIMEOUT_SEC = 120
STAGE_CHECK_TIMEOUT_SEC = 30

INSTALL_HINTS = {
    "Linux": "sudo apt install tshark",
    "Darwin": "brew install wireshark",
}


class StageError(RuntimeError):
    """A capture that could not be put on the Host. The message is for the operator."""


@dataclass
class HostProbe:
    collector_id: str
    probed_at: float
    os_name: str | None = None
    tshark_path: str | None = None
    tshark_version: str | None = None
    checksum_tool: str | None = None
    base64_flag: str | None = None
    free_bytes: int | None = None
    #: sha256 → size in bytes, as listed on the Host after the sweeps.
    staged: dict[str, int] = field(default_factory=dict)
    swept: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    #: Why the Host is not selectable; empty when it is.
    problems: list[str] = field(default_factory=list)
    install_hint: str | None = None
    #: Pinned captures alone exceed the budget (§B4); reported, not an error.
    over_budget: bool = False

    @property
    def selectable(self) -> bool:
        return not self.problems

    def public(self) -> dict:
        data = asdict(self)
        data["selectable"] = self.selectable
        return data


class ProbeStore:
    """The latest probe per collector, in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probes: dict[str, HostProbe] = {}

    def get(self, collector_id: str) -> HostProbe | None:
        with self._lock:
            return self._probes.get(collector_id)

    def put(self, probe: HostProbe) -> None:
        with self._lock:
            self._probes[probe.collector_id] = probe

    def forget(self, collector_id: str) -> None:
        with self._lock:
            self._probes.pop(collector_id, None)


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


def parse_probe_output(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text.split("\n"):
        key, sep, value = line.rstrip("\r").partition("=")
        if sep and key.isidentifier():
            values.setdefault(key, []).append(value.strip())
    return values


def _first(values: dict[str, list[str]], key: str) -> str | None:
    found = values.get(key)
    return found[0] if found and found[0] else None


def evaluate_probe(collector_id: str, values: dict[str, list[str]]) -> HostProbe:
    """Turn the probe script's ``key=value`` lines into a verdict. No I/O."""
    probe = HostProbe(collector_id=collector_id, probed_at=time.time())
    probe.os_name = _first(values, "os")
    probe.tshark_path = _first(values, "tshark_path")
    probe.tshark_version = tr.parse_tshark_version(_first(values, "tshark_version"))
    probe.checksum_tool = _first(values, "checksum")
    probe.base64_flag = _first(values, "base64")
    free_kb = _first(values, "free_kb")
    probe.free_bytes = int(free_kb) * 1024 if free_kb and free_kb.isdigit() else None
    for entry in values.get("staged", []):
        sha, _, size = entry.partition(" ")
        if tr.SHA256_RE.fullmatch(sha) and size.isdigit():
            probe.staged[sha] = int(size)
    probe.swept = [sha for sha in values.get("swept", []) if tr.SHA256_RE.fullmatch(sha)]
    probe.killed = [job for job in values.get("killed", []) if tr.JOB_RE.fullmatch(job)]

    floor = ".".join(str(part) for part in TSHARK_FLOOR[:2])
    if not probe.tshark_path or not probe.tshark_path.startswith("/"):
        probe.tshark_path = None
        probe.problems.append("tshark not installed")
        probe.install_hint = INSTALL_HINTS.get(probe.os_name or "")
    elif probe.tshark_version is None:
        probe.problems.append("tshark did not report a version")
    elif tr.version_tuple(probe.tshark_version) < TSHARK_FLOOR:
        probe.problems.append(
            f"tshark {probe.tshark_version} is older than {floor}, the oldest supported"
        )
    if probe.checksum_tool not in tr.CHECKSUM_COMMANDS:
        probe.checksum_tool = None
        probe.problems.append("neither sha256sum nor shasum is installed")
    if probe.base64_flag not in tr.BASE64_FLAGS:
        probe.base64_flag = None
        probe.problems.append("base64 cannot decode on this Host")
    if probe.free_bytes is None:
        probe.problems.append("free space could not be read")
    elif probe.free_bytes < MIN_FREE_BYTES:
        probe.problems.append(
            f"only {probe.free_bytes // (1024 * 1024)} MB free; "
            f"at least {MIN_FREE_BYTES // (1024 * 1024)} MB is required"
        )
    return probe


def known_sha256s() -> list[str]:
    """Every digest that still has a Files row -- what the probe sweep keeps."""
    rows = workspace.query_all("SELECT DISTINCT sha256 FROM files WHERE sha256 IS NOT NULL")
    return sorted(row["sha256"] for row in rows if tr.SHA256_RE.fullmatch(row["sha256"] or ""))


def probe_host(
    target: tr.HostTarget,
    store: ProbeStore,
    *,
    active_jobs: list[str] = (),
    keep_sha256s: list[str] | None = None,
    fields_dir: Path | None = None,
) -> HostProbe:
    """Probe one Host, sweep it, record the result, and evict beyond the budget.

    ``active_jobs`` are the remote jobs this backend owns right now; their pid
    files and processes survive the sweep. Raises `tr.RemoteError` when the
    login itself fails.
    """
    keep = known_sha256s() if keep_sha256s is None else keep_sha256s
    result = tr.run_remote(
        target,
        tr.probe_script(job_id=tr.new_job_id(), active_jobs=active_jobs),
        job_id=tr.new_job_id(),
        base64_flag=None,
        stdin=("".join(f"{sha}\n" for sha in keep)).encode("ascii"),
        timeout=PROBE_TIMEOUT_SEC,
    )
    if not result.ok:
        raise tr.RemoteError(f"Probe failed: {result.error_message()}")
    probe = evaluate_probe(target.collector_id, parse_probe_output(result.stdout.decode("utf-8", "replace")))
    _reconcile_stage_rows(target.collector_id, probe.staged)
    if probe.selectable:
        try:
            field_types(target, probe, fields_dir=fields_dir)
        except tr.RemoteError as exc:
            probe.problems.append(str(exc))
    if probe.base64_flag is not None:
        probe.over_budget = evict_over_budget(target, probe.base64_flag)
    store.put(probe)
    return probe


def _fields_path(version: str, fields_dir: Path | None) -> Path:
    tr.version_tuple(version)  # only a dotted version ever becomes a filename
    return (fields_dir or PACKETS_DIR) / f"fields-{version}.json"


def field_types(target: tr.HostTarget, probe: HostProbe, *, fields_dir: Path | None = None) -> dict[str, str]:
    """``tshark -G fields`` for this Host's version, cached per version (§B5)."""
    if not probe.tshark_version or not probe.tshark_path:
        raise tr.RemoteError("This Host has no usable tshark.")
    path = _fields_path(probe.tshark_version, fields_dir)
    if path.is_file():
        return json.loads(path.read_text())
    result = tr.run_remote(
        target,
        tr.fields_script(tshark_path=probe.tshark_path),
        job_id=tr.new_job_id(),
        base64_flag=probe.base64_flag,
        timeout=FIELDS_TIMEOUT_SEC,
    )
    if not result.ok:
        raise tr.RemoteError(f"tshark -G fields failed: {result.error_message()}")
    types = tr.parse_field_types(result.stdout.decode("utf-8", "replace"))
    if not types:
        raise tr.RemoteError("tshark -G fields listed no fields.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(types, sort_keys=True))
    tmp.replace(path)
    return types


# --------------------------------------------------------------------------
# Staged captures
# --------------------------------------------------------------------------

_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


def _remote_path(sha256: str) -> str:
    return f"{tr.REMOTE_DIR}/captures/{tr.check_sha256(sha256)}.pcapng"


def _upsert_stage_row(collector_id: str, sha256: str, size_bytes: int) -> None:
    workspace.execute(
        "INSERT INTO packet_host_stage"
        " (collector_id, file_sha256, remote_path, size_bytes, staged_at, last_used_at)"
        f" VALUES (?, ?, ?, ?, {_NOW}, {_NOW})"
        " ON CONFLICT(collector_id, file_sha256) DO UPDATE SET"
        f" size_bytes = excluded.size_bytes, last_used_at = {_NOW}",
        (collector_id, sha256, _remote_path(sha256), int(size_bytes)),
    )


def _reconcile_stage_rows(collector_id: str, staged: dict[str, int]) -> None:
    """Make the table say what the Host actually holds after the sweep."""
    with workspace.connect() as conn:
        rows = conn.execute(
            "SELECT file_sha256 FROM packet_host_stage WHERE collector_id = ?", (collector_id,)
        ).fetchall()
        for row in rows:
            if row["file_sha256"] not in staged:
                conn.execute(
                    "DELETE FROM packet_host_stage WHERE collector_id = ? AND file_sha256 = ?",
                    (collector_id, row["file_sha256"]),
                )
        conn.commit()
    known = {row["file_sha256"] for row in rows}
    for sha, size in staged.items():
        if sha not in known:
            _upsert_stage_row(collector_id, sha, size)


def pinned_sha256s(collector_id: str) -> set[str]:
    """Captures a queued or running analysis on this Host still needs (§B4)."""
    rows = workspace.query_all(
        "SELECT DISTINCT file_sha256 FROM packet_analyses"
        " WHERE collector_id = ? AND status IN ('queued', 'running')",
        (collector_id,),
    )
    return {row["file_sha256"] for row in rows}


def plan_eviction(
    rows: list[tuple[str, int]], pinned: set[str], budget: int
) -> tuple[list[str], bool]:
    """Which captures to evict so the total fits ``budget``. No I/O.

    ``rows`` are ``(sha256, size_bytes)``, least recently used first. Returns
    the digests to evict and whether the budget is still exceeded afterwards,
    which happens only when pinned captures alone exceed it.
    """
    total = sum(size for _, size in rows)
    evict = []
    for sha, size in rows:
        if total <= budget:
            break
        if sha in pinned:
            continue
        evict.append(sha)
        total -= size
    return evict, total > budget


def evict_over_budget(
    target: tr.HostTarget, base64_flag: str, *, protect: set[str] = frozenset()
) -> bool:
    """Bring one Host back within `STAGE_BUDGET_BYTES`. True if still over it."""
    rows = workspace.query_all(
        "SELECT file_sha256, size_bytes FROM packet_host_stage WHERE collector_id = ?"
        " ORDER BY last_used_at, staged_at",
        (target.collector_id,),
    )
    evict, over = plan_eviction(
        [(row["file_sha256"], int(row["size_bytes"])) for row in rows],
        pinned_sha256s(target.collector_id) | set(protect),
        STAGE_BUDGET_BYTES,
    )
    if evict:
        result = tr.run_remote(
            target,
            tr.delete_staged_script(sha256s=evict),
            job_id=tr.new_job_id(),
            base64_flag=base64_flag,
            timeout=STAGE_CHECK_TIMEOUT_SEC,
        )
        if not result.ok:
            # Rows stay: the Host still holds the files, and the next probe
            # lists what is really there.
            return True
        with workspace.connect() as conn:
            conn.executemany(
                "DELETE FROM packet_host_stage WHERE collector_id = ? AND file_sha256 = ?",
                [(target.collector_id, sha) for sha in evict],
            )
            conn.commit()
    return over


_locks_guard = threading.Lock()
_stage_locks: dict[tuple[str, str], threading.Lock] = {}


def _stage_lock(collector_id: str, sha256: str) -> threading.Lock:
    with _locks_guard:
        return _stage_locks.setdefault((collector_id, sha256), threading.Lock())


def stage_capture(
    target: tr.HostTarget,
    probe: HostProbe,
    *,
    sha256: str,
    source: Path,
    cancel: threading.Event | None = None,
    timeout: float = tr.DEFAULT_TIMEOUT_SEC,
) -> str:
    """Make sure the Host holds ``source`` as ``captures/<sha256>.pcapng``.

    Returns the remote path. The per-(Host, sha) lock makes "already staged?"
    and the upload one step, so the same capture is not sent twice at once; the
    `test -f` on the Host is an optimisation inside it, not the guard. Every
    job uploads to its own temp name and only `mv` commits (§B4).
    """
    tr.check_sha256(sha256)
    if not probe.selectable or probe.checksum_tool is None or probe.base64_flag is None:
        raise StageError("This Host is not ready for packet analysis: " + "; ".join(probe.problems))
    size = source.stat().st_size
    with _stage_lock(target.collector_id, sha256):
        check = tr.run_remote(
            target,
            tr.stage_check_script(sha256=sha256),
            job_id=tr.new_job_id(),
            base64_flag=probe.base64_flag,
            timeout=STAGE_CHECK_TIMEOUT_SEC,
            cancel=cancel,
        )
        if not check.ok:
            raise StageError(check.error_message())
        if check.stdout.strip() != b"staged":
            result = tr.run_remote(
                target,
                tr.stage_script(
                    job_id=tr.new_job_id(),
                    sha256=sha256,
                    size_bytes=size,
                    checksum_tool=probe.checksum_tool,
                    min_free_bytes=MIN_FREE_BYTES,
                ),
                job_id=tr.new_job_id(),
                base64_flag=probe.base64_flag,
                stdin=source,
                timeout=timeout,
                cancel=cancel,
            )
            if not result.ok:
                raise StageError(result.error_message())
        _upsert_stage_row(target.collector_id, sha256, size)
    evict_over_budget(target, probe.base64_flag, protect={sha256})
    return _remote_path(sha256)


def ensure_file_sha256(file_row: dict, path: Path) -> str:
    """The Files row's digest, computed and stored on first use for legacy rows.

    ``files.sha256`` is nullable for rows uploaded before it existed (§B4). It
    is stored only where it is still NULL, so a concurrent first use cannot
    overwrite a value another caller just wrote.
    """
    current = file_row.get("sha256")
    if isinstance(current, str) and tr.SHA256_RE.fullmatch(current):
        return current
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    workspace.execute(
        "UPDATE files SET sha256 = ? WHERE id = ? AND sha256 IS NULL", (sha, file_row["id"])
    )
    return sha
