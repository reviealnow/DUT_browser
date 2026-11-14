"""Packet Monitor analyses: the per-Host queue, the interactive lane, results on disk.

docs/design-packet-monitor.md §B2, §B3 (limits, lifecycle), §B4 (*Deletion*),
§B8 (storage) and §B9 (what the API needs from here).

**One running analysis per Host**, the rest queued in memory, so a Pi serving
DUT consoles never runs two tshark reads of a 50 MB capture at once. Beside the
queue, each Host has an **interactive lane** of exactly one slot for the short
steps a person is waiting on -- a filter check or a frame detail -- so those
never wait minutes behind an analysis.

Results live under ``data/packets/``: ``<id>.jsonl`` (one decoded row per
line), ``<id>.idx`` (the byte offset of every 500th row, so a page is a seek),
``<id>.columns.json``, ``<id>.findings.json``, ``<id>.groups.json`` and
``<id>/frames/<n>.json`` once a frame is opened.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from app.collector.registry import CollectorError
from app.config import PACKETS_DIR
from app.db import workspace
from app.services import file_service
from app.services import packet_decode as pd
from app.services import packet_hosts as ph
from app.services import packet_modes as pm
from app.services import packet_rules as pr
from app.services import tshark_runner as tr

log = logging.getLogger(__name__)

ROWS_INDEX_EVERY = 500
MAX_PAGE = 500
MAX_EXTRA_FIELDS = 32
RETENTION_DAYS = 30
RETENTION_SWEEP_SEC = 24 * 3600
ANALYSIS_TIMEOUT_SEC = tr.DEFAULT_TIMEOUT_SEC
VALIDATE_TIMEOUT_SEC = 10
FRAME_TIMEOUT_SEC = 60
#: How long a filter check or frame detail waits for the Host's one interactive slot.
LANE_WAIT_SEC = 10
#: How long shutdown waits for cancelled jobs: the remote kill's own timeout,
#: plus a margin for the local ssh to close.
STOP_TIMEOUT_SEC = tr.KILL_WAIT_SEC + 20 + 10

HOST_SILENT = "Host did not answer"

_TIMESTAMP_NOW = "strftime('%Y-%m-%d %H:%M:%S', 'now')"


class PacketError(Exception):
    """A request this module refuses, worded for the operator. ``status`` is the HTTP code."""

    status = 400


class NotFound(PacketError):
    status = 404


class Conflict(PacketError):
    status = 409


class HostUnavailable(PacketError):
    """The Host could not be logged into, or answered with an error of its own."""

    status = 502


class _Cancelled(Exception):
    pass


def combined_filter(mode_filter: str, user_filter: str) -> str:
    """The Test Mode's own filter AND the user's, each parenthesised."""
    parts = [f"({part.strip()})" for part in (mode_filter, user_filter) if part and part.strip()]
    return " && ".join(parts)


def extra_columns(extra_fields: list[str]) -> list[pm.Column]:
    """A column per extra field the user asked for, decoded as text."""
    columns = []
    for index, name in enumerate(extra_fields):
        if not isinstance(name, str) or not pm.FIELD_RE.fullmatch(name):
            raise PacketError(f"Not a tshark field name: {name!r}")
        key = f"extra{index}_" + re.sub(r"[^a-z0-9_]", "_", name.lower())
        columns.append(pm.Column(key=key[:64], label=name, fields=[name], decoder="text"))
    return columns


def _mode_with_extras(mode: pm.TestMode, extra_fields: list[str]) -> pm.TestMode:
    if not extra_fields:
        return mode
    return mode.model_copy(update={"columns": [*mode.columns, *extra_columns(extra_fields)]})


class PacketJobs:
    """Owns every Packet Monitor job of this backend process."""

    def __init__(
        self,
        collector_registry,
        *,
        results_dir: Path | None = None,
        fields_dir: Path | None = None,
        ssh_binary: str = "ssh",
        analysis_timeout: float = ANALYSIS_TIMEOUT_SEC,
    ) -> None:
        self.registry = collector_registry
        self.store = ph.ProbeStore()
        self.results_dir = results_dir or PACKETS_DIR
        self.fields_dir = fields_dir
        self.ssh_binary = ssh_binary
        self.analysis_timeout = analysis_timeout
        self._lock = threading.Lock()
        self._queues: dict[str, deque[int]] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._cancel: dict[int, threading.Event] = {}
        self._remote_jobs: dict[str, set[str]] = {}
        self._lanes: dict[str, threading.Semaphore] = {}
        #: Analyses whose Files row went away while they ran: their results are
        #: removed the moment the job ends, never written after.
        self._deleted: set[int] = set()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ hosts

    def _target(self, collector_id: str) -> tr.HostTarget:
        try:
            return tr.target_for(self.registry, collector_id, ssh_binary=self.ssh_binary)
        except KeyError:
            raise NotFound(f"Unknown collector: {collector_id}") from None
        except CollectorError as exc:
            raise HostUnavailable(str(exc)) from exc

    def require_connected(self, collector_id: str) -> None:
        try:
            status = self.registry.status(collector_id)
        except KeyError:
            raise NotFound(f"Unknown collector: {collector_id}") from None
        if not status.get("connected"):
            raise Conflict("This Host is not connected. Ask an admin to connect it.")

    def active_jobs(self, collector_id: str) -> list[str]:
        with self._lock:
            return sorted(self._remote_jobs.get(collector_id, set()))

    def _register(self, collector_id: str, job_id: str) -> None:
        with self._lock:
            self._remote_jobs.setdefault(collector_id, set()).add(job_id)

    def _unregister(self, collector_id: str, job_id: str) -> None:
        with self._lock:
            self._remote_jobs.get(collector_id, set()).discard(job_id)

    def probe(self, collector_id: str) -> ph.HostProbe:
        """Probe one Host now; the result replaces the stored one."""
        target = self._target(collector_id)
        try:
            return ph.probe_host(
                target, self.store,
                active_jobs=self.active_jobs(collector_id),
                fields_dir=self.fields_dir,
            )
        except tr.RemoteError as exc:
            raise HostUnavailable(str(exc)) from exc

    def probe_async(self, collector_id: str) -> threading.Thread:
        """§B5 "on every connect": never blocks or fails the connect itself."""

        def run() -> None:
            try:
                self.probe(collector_id)
            except Exception as exc:  # noqa: BLE001 - a background probe only logs
                log.warning("Packet Monitor probe of %s failed: %s", collector_id, exc)

        thread = threading.Thread(target=run, name=f"packet-probe-{collector_id}", daemon=True)
        thread.start()
        return thread

    def _ready_probe(self, collector_id: str) -> ph.HostProbe:
        probe = self.store.get(collector_id) or self.probe(collector_id)
        if not probe.selectable:
            raise Conflict("This Host is not ready for packet analysis: " + "; ".join(probe.problems))
        return probe

    def _field_types(self, target: tr.HostTarget, probe: ph.HostProbe) -> dict[str, str]:
        try:
            return ph.field_types(target, probe, fields_dir=self.fields_dir)
        except tr.RemoteError as exc:
            raise HostUnavailable(str(exc)) from exc

    def hosts(self) -> list[dict]:
        """Every collector, with its tshark status. Only connected ones are selectable."""
        hosts = []
        for status in self.registry.list_status():
            probe = self.store.get(status["id"])
            hosts.append({
                "id": status["id"],
                "label": status["label"],
                "connected": bool(status.get("connected")),
                "probe": probe.public() if probe else None,
                "selectable": bool(status.get("connected") and probe and probe.selectable),
            })
        return hosts

    def _lane(self, collector_id: str) -> threading.Semaphore:
        with self._lock:
            return self._lanes.setdefault(collector_id, threading.Semaphore(1))

    # ---------------------------------------------------------------- filters

    def validate_filter(self, collector_id: str, display_filter: str) -> dict:
        """``{ok, error}``; ``ok`` is None when the Host could not say (§B9)."""
        try:
            tr.check_display_filter(display_filter)
        except tr.CommandError as exc:
            return {"ok": False, "error": str(exc)}
        if not display_filter.strip():
            return {"ok": True, "error": None}
        try:
            self.require_connected(collector_id)
            target = self._target(collector_id)
            probe = self._ready_probe(collector_id)
        except PacketError as exc:
            if isinstance(exc, NotFound):
                raise
            return {"ok": None, "error": str(exc)}
        lane = self._lane(collector_id)
        if not lane.acquire(timeout=LANE_WAIT_SEC):
            return {"ok": None, "error": HOST_SILENT}
        try:
            result = tr.run_remote(
                target,
                tr.filter_check_script(tshark_path=probe.tshark_path, display_filter=display_filter),
                job_id=tr.new_job_id(),
                base64_flag=probe.base64_flag,
                stdin=tr.EMPTY_PCAP,
                timeout=VALIDATE_TIMEOUT_SEC,
            )
        except tr.RemoteError:
            return {"ok": None, "error": HOST_SILENT}
        finally:
            lane.release()
        if result.timed_out:
            return {"ok": None, "error": HOST_SILENT}
        if result.returncode == 0:
            return {"ok": True, "error": None}
        return {"ok": False, "error": _tshark_message(result.stderr)}

    # -------------------------------------------------------------- analyses

    def create(
        self,
        *,
        file_id: int,
        collector_id: str,
        mode_id: str,
        display_filter: str = "",
        extra_fields: list[str] | None = None,
        user_id: int | None = None,
    ) -> int:
        """Check everything a job would fail on later, then queue it (§B9 `POST /analyses`)."""
        try:
            mode = pm.get_mode(mode_id)
        except ValueError as exc:
            raise PacketError(str(exc)) from None
        extra = list(extra_fields or [])
        if len(extra) > MAX_EXTRA_FIELDS:
            raise PacketError(f"At most {MAX_EXTRA_FIELDS} extra fields.")
        extra_columns(extra)  # names only; the Host's own list is checked below
        try:
            tr.check_display_filter(display_filter)
        except tr.CommandError as exc:
            raise PacketError(str(exc)) from None
        file_row = file_service.get_file_by_id(file_id)
        if file_row is None:
            raise NotFound("File not found")
        try:
            path = file_service.resolve_download_path(file_row)
        except FileNotFoundError as exc:
            raise NotFound(str(exc)) from None

        self.require_connected(collector_id)
        target = self._target(collector_id)
        probe = self._ready_probe(collector_id)
        available = self._field_types(target, probe)
        try:
            tr.check_fields(extra, available) if extra else None
        except tr.CommandError as exc:
            raise PacketError(str(exc)) from None
        full_filter = combined_filter(mode.display_filter, display_filter)
        if display_filter.strip():
            check = self.validate_filter(collector_id, full_filter)
            if check["ok"] is False:
                raise PacketError(check["error"])

        sha = ph.ensure_file_sha256(file_row, path)
        analysis_id = workspace.execute(
            "INSERT INTO packet_analyses (file_id, file_sha256, collector_id, mode_id,"
            " display_filter, extra_fields, status, created_by_user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
            (file_id, sha, collector_id, mode.id, display_filter, json.dumps(extra), user_id),
        )
        self._submit(collector_id, analysis_id)
        return analysis_id

    def _submit(self, collector_id: str, analysis_id: int) -> None:
        with self._lock:
            self._cancel[analysis_id] = threading.Event()
            self._queues.setdefault(collector_id, deque()).append(analysis_id)
            worker = self._workers.get(collector_id)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._drain, args=(collector_id,),
                    name=f"packet-jobs-{collector_id}", daemon=True,
                )
                self._workers[collector_id] = worker
                worker.start()

    def _drain(self, collector_id: str) -> None:
        while True:
            with self._lock:
                queue = self._queues.get(collector_id)
                if not queue:
                    self._workers.pop(collector_id, None)
                    return
                analysis_id = queue.popleft()
            try:
                self._run(analysis_id)
            except Exception:  # noqa: BLE001 - one job must never stop the queue
                log.exception("Packet analysis %s crashed", analysis_id)
                self._finish(analysis_id, "failed", error="Internal error; see the backend log.")

    def wait_idle(self, timeout: float = 30) -> bool:
        """Test helper: True once no Host has a running or queued analysis."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                busy = [w for w in self._workers.values() if w.is_alive()]
            if not busy:
                return True
            time.sleep(0.05)
        return False

    def get(self, analysis_id: int) -> dict:
        row = workspace.query_one("SELECT * FROM packet_analyses WHERE id = ?", (analysis_id,))
        if row is None:
            raise NotFound("Analysis not found")
        data = dict(row)
        data["extra_fields"] = json.loads(data.get("extra_fields") or "[]")
        findings = self._read_json(analysis_id, "findings")
        data["findings_summary"] = _summarise(findings or [])
        data["columns"] = self._read_json(analysis_id, "columns")
        return data

    def recent(self, limit: int = 50) -> list[dict]:
        rows = workspace.query_all(
            "SELECT id, file_id, collector_id, mode_id, display_filter, status, error, warning,"
            " frames_shown, rows_dropped, created_by_user_id, created_at, finished_at"
            " FROM packet_analyses ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        return [dict(row) for row in rows]

    def cancel(self, analysis_id: int) -> dict:
        """Cancel a queued or running analysis; a finished one is left as it is."""
        row = self.get(analysis_id)
        if row["status"] not in ("queued", "running"):
            raise Conflict(f"The analysis is already {row['status']}.")
        with self._lock:
            queue = self._queues.get(row["collector_id"])
            was_queued = queue is not None and analysis_id in queue
            if was_queued:
                queue.remove(analysis_id)
                # It will never reach `_run`, whose `finally` is the only other
                # place these are cleared (review of #230: they leaked).
                event = self._cancel.pop(analysis_id, None)
                self._deleted.discard(analysis_id)
            else:
                event = self._cancel.get(analysis_id)
            if event is not None:
                event.set()
        if was_queued or event is None:
            self._finish(analysis_id, "cancelled", error="Cancelled.")
        return {"ok": True}

    def _finish(self, analysis_id: int, status: str, **fields: Any) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        workspace.execute(
            f"UPDATE packet_analyses SET status = ?, {sets + ', ' if sets else ''}"
            f"finished_at = {_TIMESTAMP_NOW} WHERE id = ? AND status IN ('queued', 'running')",
            (status, *fields.values(), analysis_id),
        )

    def _run(self, analysis_id: int) -> None:
        row = workspace.query_one("SELECT * FROM packet_analyses WHERE id = ?", (analysis_id,))
        if row is None or row["status"] != "queued":
            return
        with self._lock:
            cancel = self._cancel.setdefault(analysis_id, threading.Event())
        workspace.execute(
            "UPDATE packet_analyses SET status = 'running' WHERE id = ? AND status = 'queued'",
            (analysis_id,),
        )
        collector_id = row["collector_id"]
        job_id = tr.new_job_id()
        tsv_path = self.results_dir / f"{analysis_id}.tsv.part"
        try:
            outcome = self._analyse(dict(row), cancel, job_id, tsv_path)
        except _Cancelled:
            self._finish(analysis_id, "cancelled", error="Cancelled.")
        except (PacketError, ph.StageError, tr.CommandError, tr.RemoteError, CollectorError) as exc:
            if cancel.is_set():
                self._finish(analysis_id, "cancelled", error="Cancelled.")
            else:
                self._finish(analysis_id, "failed", error=str(exc))
        else:
            self._finish(analysis_id, "done", **outcome)
        finally:
            self._unregister(collector_id, job_id)
            tsv_path.unlink(missing_ok=True)
            with self._lock:
                self._cancel.pop(analysis_id, None)
                doomed = analysis_id in self._deleted
                self._deleted.discard(analysis_id)
            if doomed:
                self._delete_results(analysis_id)

    def _analyse(self, row: dict, cancel: threading.Event, job_id: str, tsv_path: Path) -> dict:
        collector_id = row["collector_id"]
        mode = _mode_with_extras(pm.get_mode(row["mode_id"]), json.loads(row["extra_fields"] or "[]"))
        file_row = file_service.get_file_by_id(row["file_id"])
        if file_row is None:
            raise PacketError("The file was deleted.")
        source = file_service.resolve_download_path(file_row)
        target = self._target(collector_id)
        probe = self._ready_probe(collector_id)

        ph.stage_capture(target, probe, sha256=row["file_sha256"], source=source, cancel=cancel)
        if cancel.is_set():
            raise _Cancelled()
        available = self._field_types(target, probe)
        columns = pm.resolve_columns(mode, available)
        fields = pm.extraction_fields(columns)

        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._register(collector_id, job_id)
        result = tr.run_remote(
            target,
            tr.analysis_script(
                job_id=job_id,
                sha256=row["file_sha256"],
                tshark_path=probe.tshark_path,
                fields=fields,
                available=available,
                display_filter=combined_filter(mode.display_filter, row["display_filter"]),
            ),
            # The same id: run_remote's kill looks for run/<job_id>.pid, which
            # is what the analysis script writes.
            job_id=job_id,
            base64_flag=probe.base64_flag,
            stdout_path=tsv_path,
            timeout=self.analysis_timeout,
            cancel=cancel,
            kill_on_stop=True,
        )
        if result.cancelled:
            raise _Cancelled()
        if not result.ok:
            raise HostUnavailable(result.error_message())

        try:
            shown, dropped = self._write_results(row["id"], columns, tsv_path, mode)
        except BaseException:
            # Nothing half-written outlives the job.
            self._delete_results(row["id"])
            raise
        return {
            "frames_shown": shown,
            "rows_dropped": dropped,
            "warning": result.warning,
            "tshark_version": probe.tshark_version,
        }

    # ---------------------------------------------------------------- results

    def _path(self, analysis_id: int, suffix: str) -> Path:
        return self.results_dir / f"{int(analysis_id)}.{suffix}"

    def _read_json(self, analysis_id: int, name: str) -> Any:
        path = self._path(analysis_id, f"{name}.json")
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _write_results(self, analysis_id: int, columns, tsv_path: Path, mode: pm.TestMode) -> tuple[int, int]:
        """Decode tshark's TSV into the result files, one row at a time.

        Memory is one row, the findings and the per-group summaries -- never
        the capture: the TSV may be up to the runner's stdout cap, and this
        runs in the process that also owns the serial worker (review of #230).
        Returns ``(frames shown, rows dropped)``.
        """
        evaluator = pr.Evaluator(mode.rules, mode.group_by)
        groups = _GroupSummary(mode.group_by)
        offsets = []
        shown = 0
        with open(tsv_path, "rb") as tsv, open(self._path(analysis_id, "jsonl"), "wb") as handle:
            reader = pd.TsvRows(pd.tsv_file_lines(tsv))
            for raw in reader:
                decoded = pm.decode_row(columns, raw)
                if shown % ROWS_INDEX_EVERY == 0:
                    offsets.append(handle.tell())
                handle.write(json.dumps(decoded, default=str, ensure_ascii=False).encode() + b"\n")
                evaluator.add(decoded)
                groups.add(decoded)
                shown += 1
        findings = evaluator.findings()
        with self._lock:
            if analysis_id in self._deleted:
                raise _Cancelled()
        self._path(analysis_id, "idx").write_text(json.dumps(offsets))
        self._path(analysis_id, "columns.json").write_text(json.dumps([
            {"key": c.key, "label": c.label, "decoder": c.decoder, "hidden": c.hidden}
            for c in columns
        ]))
        self._path(analysis_id, "findings.json").write_text(
            json.dumps([asdict(f) for f in findings], default=str)
        )
        self._path(analysis_id, "groups.json").write_text(json.dumps(groups.result(), default=str))
        return shown, reader.dropped

    def require_done(self, analysis_id: int) -> dict:
        row = self.get(analysis_id)
        if row["status"] != "done":
            raise Conflict(f"The analysis is {row['status']}, not done.")
        return row

    def rows(self, analysis_id: int, offset: int = 0, limit: int = 100) -> dict:
        row = self.require_done(analysis_id)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_PAGE))
        total = row["frames_shown"] or 0
        page: list[dict] = []
        if offset < total:
            offsets = json.loads(self._path(analysis_id, "idx").read_text())
            with open(self._path(analysis_id, "jsonl"), "rb") as handle:
                handle.seek(offsets[offset // ROWS_INDEX_EVERY])
                for _ in range(offset % ROWS_INDEX_EVERY):
                    handle.readline()
                for _ in range(limit):
                    line = handle.readline()
                    if not line:
                        break
                    page.append(json.loads(line))
        return {
            "offset": offset, "limit": limit, "total": total,
            "rows": page, "display": self._display(analysis_id, page),
        }

    def _display(self, analysis_id: int, rows: list[dict]) -> list[dict[str, str]]:
        """Each row as the table shows it -- `[2, 8]` → ``PSK / SAE`` -- beside the raw values.

        Formatted here, by the same `packet_decode.format_value` the CSV export
        uses, so the browser never carries a second copy of the suite names.
        A column the Host's tshark could not resolve is absent, not "—".
        """
        columns = self._read_json(analysis_id, "columns") or []
        return [
            {c["key"]: pd.format_value(c["decoder"], row.get(c["key"])) for c in columns if c["key"] in row}
            for row in rows
        ]

    def groups(self, analysis_id: int) -> list[dict]:
        self.require_done(analysis_id)
        groups = self._read_json(analysis_id, "groups") or []
        shown = self._display(analysis_id, [g["row"] for g in groups])
        return [{**group, "display": text} for group, text in zip(groups, shown)]

    def findings(self, analysis_id: int) -> list[dict]:
        self.require_done(analysis_id)
        return self._read_json(analysis_id, "findings") or []

    def export_csv(self, analysis_id: int) -> Iterator[str]:
        """The CSV, as a stream. Checked now, not on the first read of the stream.

        A plain generator would defer `require_done` until the response body
        is being sent, which is too late to answer 404 or 409.
        """
        self.require_done(analysis_id)
        columns = [c for c in (self._read_json(analysis_id, "columns") or []) if not c["hidden"]]
        handle = open(self._path(analysis_id, "jsonl"), "rb")
        return self._csv_lines(columns, handle)

    @staticmethod
    def _csv_lines(columns: list[dict], handle) -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
            return text

        with handle:
            writer.writerow([c["label"] for c in columns])
            yield flush()
            for line in handle:
                decoded = json.loads(line)
                writer.writerow([
                    pd.format_value(c["decoder"], decoded.get(c["key"])) if c["key"] in decoded else ""
                    for c in columns
                ])
                yield flush()

    def frame(self, analysis_id: int, frame_number: int) -> Any:
        """One frame's tshark dissection tree, cached once fetched (§B9)."""
        row = self.require_done(analysis_id)
        if frame_number < 1:
            raise PacketError("Frame numbers start at 1.")
        cache = self.results_dir / str(int(analysis_id)) / "frames" / f"{int(frame_number)}.json"
        if cache.is_file():
            return json.loads(cache.read_text())
        collector_id = row["collector_id"]
        self.require_connected(collector_id)
        target = self._target(collector_id)
        probe = self._ready_probe(collector_id)
        file_row = file_service.get_file_by_id(row["file_id"])
        if file_row is None:
            raise NotFound("The file was deleted.")
        try:
            # Evicted since the analysis ran: staged again on demand (§B3) --
            # before taking the lane, so a re-upload of a 50 MB capture never
            # holds the Host's one interactive slot and turns every filter
            # check there into "Host did not answer" (review of #230).
            ph.stage_capture(
                target, probe, sha256=row["file_sha256"],
                source=file_service.resolve_download_path(file_row),
            )
        except (ph.StageError, tr.RemoteError) as exc:
            raise HostUnavailable(str(exc)) from exc
        lane = self._lane(collector_id)
        if not lane.acquire(timeout=LANE_WAIT_SEC):
            raise HostUnavailable("The Host is busy with another frame or filter check; try again.")
        job_id = tr.new_job_id()
        try:
            self._register(collector_id, job_id)
            result = tr.run_remote(
                target,
                tr.frame_detail_script(
                    job_id=job_id, sha256=row["file_sha256"],
                    tshark_path=probe.tshark_path, frame_number=int(frame_number),
                ),
                job_id=job_id,
                base64_flag=probe.base64_flag,
                timeout=FRAME_TIMEOUT_SEC,
                kill_on_stop=True,
            )
        except tr.RemoteError as exc:
            raise HostUnavailable(str(exc)) from exc
        finally:
            self._unregister(collector_id, job_id)
            lane.release()
        if not result.ok:
            raise HostUnavailable(result.error_message())
        try:
            packets = json.loads(result.stdout.decode("utf-8", "replace") or "[]")
        except ValueError as exc:
            raise HostUnavailable("tshark returned a frame tree that is not JSON.") from exc
        if not packets:
            raise NotFound(f"Frame {frame_number} is not in this capture.")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(packets[0]))
        return packets[0]

    def _delete_results(self, analysis_id: int) -> None:
        for suffix in ("jsonl", "idx", "columns.json", "findings.json", "groups.json", "tsv.part"):
            self._path(analysis_id, suffix).unlink(missing_ok=True)
        shutil.rmtree(self.results_dir / str(int(analysis_id)), ignore_errors=True)

    # --------------------------------------------------------- staged copies

    def delete_staged(self, collector_id: str, sha256: str) -> dict:
        try:
            tr.check_sha256(sha256)
        except tr.CommandError as exc:
            raise PacketError(str(exc)) from None
        self.require_connected(collector_id)
        target = self._target(collector_id)
        probe = self._ready_probe(collector_id)
        try:
            # Pinned is decided inside the stage lock, not here (review of #230).
            removed = ph.remove_staged(target, probe.base64_flag, [sha256])
        except ph.StageError as exc:
            raise HostUnavailable(str(exc)) from exc
        if not removed:
            raise Conflict("A queued or running analysis still needs this capture.")
        return {"ok": True}

    # --------------------------------------------------------------- lifecycle

    def file_deleted(self, file_id: int, sha256: str | None) -> threading.Thread | None:
        """§B4 *Deletion*, called after a Files row is gone.

        Cancels the file's queued and running analyses (running ones through the
        remote kill), deletes them and their results, and -- only when no other
        Files row has the same content -- removes the staged copies from every
        connected Host in the background. A disconnected Host is swept on its
        next probe.
        """
        rows = workspace.query_all(
            "SELECT id, status FROM packet_analyses WHERE file_id = ?", (file_id,)
        )
        finished = []
        for row in rows:
            if row["status"] in ("queued", "running"):
                with self._lock:
                    self._deleted.add(row["id"])
                try:
                    self.cancel(row["id"])
                except PacketError:
                    pass
            else:
                finished.append(row["id"])
        # Rows first, then files: a request arriving in between then finds no
        # analysis (404) rather than an analysis whose files are gone (500).
        workspace.execute("DELETE FROM packet_analyses WHERE file_id = ?", (file_id,))
        for analysis_id in finished:
            self._delete_results(analysis_id)

        if not sha256 or not tr.SHA256_RE.fullmatch(sha256):
            return None
        if workspace.query_one("SELECT 1 FROM files WHERE sha256 = ?", (sha256,)) is not None:
            return None
        holders = [
            row["collector_id"] for row in workspace.query_all(
                "SELECT collector_id FROM packet_host_stage WHERE file_sha256 = ?", (sha256,)
            )
        ]

        def remove() -> None:
            for collector_id in holders:
                try:
                    self.require_connected(collector_id)
                    probe = self.store.get(collector_id)
                    if probe is None or probe.base64_flag is None:
                        continue
                    ph.remove_staged(self._target(collector_id), probe.base64_flag, [sha256])
                except Exception as exc:  # noqa: BLE001 - the next probe sweeps it
                    log.warning("Staged capture on %s not removed now: %s", collector_id, exc)

        thread = threading.Thread(target=remove, name="packet-unstage", daemon=True)
        thread.start()
        return thread

    def mark_interrupted(self) -> int:
        """At startup: nobody owns a job the last process was running (§B3)."""
        with workspace.connect() as conn:
            cursor = conn.execute(
                "UPDATE packet_analyses SET status = 'failed', error = 'Interrupted by a restart.',"
                f" finished_at = {_TIMESTAMP_NOW} WHERE status IN ('queued', 'running')"
            )
            conn.commit()
            return cursor.rowcount

    def sweep_retention(self, days: int = RETENTION_DAYS) -> int:
        """Delete finished analyses older than ``days``, rows and files (Q4)."""
        rows = workspace.query_all(
            "SELECT id FROM packet_analyses WHERE status NOT IN ('queued', 'running')"
            " AND finished_at IS NOT NULL AND finished_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        for row in rows:
            # Row first, then files, as in `file_deleted`.
            workspace.execute("DELETE FROM packet_analyses WHERE id = ?", (row["id"],))
            self._delete_results(row["id"])
        return len(rows)

    def start_retention_timer(self) -> threading.Thread:
        def loop() -> None:
            while True:
                try:
                    removed = self.sweep_retention()
                    if removed:
                        log.info("Packet Monitor retention removed %d analyses", removed)
                except Exception:  # noqa: BLE001 - try again tomorrow
                    log.exception("Packet Monitor retention sweep failed")
                if self._stop.wait(RETENTION_SWEEP_SEC):
                    return

        thread = threading.Thread(target=loop, name="packet-retention", daemon=True)
        thread.start()
        return thread

    def stop(self, timeout: float = STOP_TIMEOUT_SEC) -> bool:
        """Cancel every job and wait, up to ``timeout``, for the remote kills to run.

        The workers are daemon threads, so returning at once would let the
        process exit before a cancelled job's `kill_remote` reaches the Host,
        and leave its tshark running on a Pi that serves DUT consoles (review
        of #230). True when every worker finished in time.
        """
        self._stop.set()
        with self._lock:
            events = list(self._cancel.values())
            for queue in self._queues.values():
                queue.clear()
            workers = [w for w in self._workers.values() if w.is_alive()]
        for event in events:
            event.set()
        deadline = time.monotonic() + timeout
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return not any(w.is_alive() for w in workers)


def _tshark_message(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "tshark rejected the filter."
    return re.sub(r"^tshark:\s*", "", lines[0])


def _summarise(findings: list[dict]) -> dict:
    counts = {"fail": 0, "warn": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


class _GroupSummary:
    """One row per ``group_by`` value -- the latest frame -- with its frame count (§B2).

    Fed one decoded row at a time; holds one row per group, not every row.
    """

    def __init__(self, group_by: str | None) -> None:
        self.group_by = group_by
        self._latest: dict[Any, dict] = {}
        self._counts: dict[Any, int] = {}

    def add(self, decoded: dict) -> None:
        if not self.group_by:
            return
        key = decoded.get(self.group_by)
        self._latest[key] = decoded
        self._counts[key] = self._counts.get(key, 0) + 1

    def result(self) -> list[dict]:
        return [{"group": key, "frames": self._counts[key], "row": row}
                for key, row in self._latest.items()]
