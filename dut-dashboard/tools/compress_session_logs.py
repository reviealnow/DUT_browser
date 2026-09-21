#!/usr/bin/env python3
"""Compress finished DUT session logs in place.

sysMon output is about as compressible as text gets -- the same 45 interfaces
and the same process table, every cycle. Measured on a real 36-hour AP6 420E
run (40,980,035 bytes, 1381 cycles):

    gzip -6    1,723,696 bytes   23.8x   (0.15 s for the whole file)
    gzip -9    1,555,349 bytes   26.3x
    xz -6      1,003,372 bytes   40.8x
    zstd -19     882,810 bytes   46.4x

gzip -6 is what this uses: it is stdlib, it is fast enough that the ratio is
free, and a `.gz` opens with `zcat`/`zless` on any box on the bench. Eight DUTs
streaming at a 30 s step produce roughly 0.4 GB a day, which this turns into
about 17 MB a day.

**Nothing in the dashboard reads a `.gz`.** The Downloads listing globs
`dut-session-*.log`, and the tail, analyzer and context-matching paths all
require that exact suffix, so a compressed log leaves the UI. That is the
trade this tool makes: it is for logs that are finished and being kept, not for
logs anybody still wants to open in the app. Decompress one with `gunzip` to
bring it back.

**This tool is the operator's, not the app's.** It runs nowhere automatically,
it refuses to do anything without `--apply`, and it verifies every archive
round-trips to the original bytes before it removes anything.

Usage:

    # look, change nothing (the default)
    python3 tools/compress_session_logs.py --logs ~/Documents/DUT_browser-prod/dut-dashboard/logs

    # act, on logs untouched for a week
    python3 tools/compress_session_logs.py --logs <dir> --older-than 7 --apply
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: Only these. Never snapshots*.jsonl (a bounded ring the backend rewrites in
#: place), never duts.json / collectors.json (live registry state), never a
#: directory.
SESSION_GLOB = "dut-session-*.log"
#: Read/write chunk. Large enough that a 39 MB log is a handful of iterations,
#: small enough that memory does not scale with the log.
CHUNK = 1 << 20
GZIP_LEVEL = 6


@dataclass
class Candidate:
    path: Path
    size: int
    age_days: float
    skip_reason: str = ""

    @property
    def eligible(self) -> bool:
        return not self.skip_reason


def _held_by_a_process(paths: list[Path]) -> set[Path]:
    """Which of these files some process currently has open.

    A session log the backend is still writing must not be compressed out from
    under it: the worker holds the descriptor, so it would keep appending to a
    file that no longer has a name, and the bytes would be lost on close.

    `lsof` is how the rest of this bench answers "who holds this" (see the
    serial-port discipline in CLAUDE.md). If it is missing, this returns nothing
    and the mtime guard below is the only protection -- which is why that guard
    is not optional.
    """
    if not paths or shutil.which("lsof") is None:
        return set()
    try:
        result = subprocess.run(
            ["lsof", "-Fn", "--", *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # lsof exits 1 when nothing is open, which is not an error here.
    held: set[Path] = set()
    known = {str(p): p for p in paths}
    for line in result.stdout.splitlines():
        if line.startswith("n") and line[1:] in known:
            held.add(known[line[1:]])
    return held


def find_candidates(log_dir: Path, older_than_days: float, min_idle_min: float) -> list[Candidate]:
    now = time.time()
    paths = sorted(p for p in log_dir.glob(SESSION_GLOB) if p.is_file())
    held = _held_by_a_process(paths)

    candidates: list[Candidate] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError as exc:
            candidates.append(Candidate(path, 0, 0.0, f"cannot stat: {exc}"))
            continue
        age_days = (now - stat.st_mtime) / 86400.0
        reason = ""
        if path in held:
            reason = "a process has it open"
        elif (now - stat.st_mtime) < min_idle_min * 60:
            reason = f"written within the last {min_idle_min:g} min"
        elif age_days < older_than_days:
            reason = f"newer than {older_than_days:g} days"
        elif path.with_suffix(".log.gz").exists():
            reason = "a .gz of this name already exists"
        elif stat.st_size == 0:
            reason = "empty"
        candidates.append(Candidate(path, stat.st_size, age_days, reason))
    return candidates


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for block in iter(lambda: fp.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_of_gzip(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as fp:
        for block in iter(lambda: fp.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def compress(candidate: Candidate, remove_original: bool) -> tuple[bool, str]:
    """Compress one log, verify the archive, then optionally drop the original.

    The verification is the point: the original is only removed after the
    archive has been read back and hashed to the same bytes. A gzip that was
    truncated by a full disk, or a file that changed while it was being read,
    fails here and the original stays exactly where it was.
    """
    source = candidate.path
    target = source.with_suffix(".log.gz")
    partial = source.with_suffix(".log.gz.partial")

    try:
        before = _sha256_of_file(source)
        with source.open("rb") as src, gzip.open(partial, "wb", compresslevel=GZIP_LEVEL) as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        after = _sha256_of_gzip(partial)
    except OSError as exc:
        _discard_partial(partial)
        return False, f"failed: {exc}"

    if before != after:
        _discard_partial(partial)
        return False, "failed: the archive did not read back identical; original untouched"

    try:
        os.replace(partial, target)
    except OSError as exc:
        _discard_partial(partial)
        return False, f"failed to place the archive: {exc}"

    if not remove_original:
        return True, f"kept {source.name}; wrote {target.name}"
    try:
        source.unlink()
    except OSError as exc:
        return True, f"wrote {target.name} but could not remove the original: {exc}"
    return True, f"wrote {target.name}"


def _discard_partial(partial: Path) -> None:
    try:
        partial.unlink()
    except OSError:
        pass


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024
    return f"{n:.1f} GB"


def report(candidates: Iterable[Candidate], log_dir: Path) -> tuple[list[Candidate], int]:
    eligible = [c for c in candidates if c.eligible]
    skipped = [c for c in candidates if not c.eligible]

    print(f"logs directory : {log_dir}")
    print(f"session logs   : {len(eligible) + len(skipped)}")
    print()
    if eligible:
        print("eligible:")
        for c in sorted(eligible, key=lambda c: -c.size):
            print(f"  {c.path.name:<62} {_human(c.size):>12}  {c.age_days:5.1f} d")
    else:
        print("eligible: none")
    if skipped:
        print("\nskipped:")
        for c in sorted(skipped, key=lambda c: c.path.name):
            print(f"  {c.path.name:<62} {c.skip_reason}")
    total = sum(c.size for c in eligible)
    print(f"\neligible total : {_human(total)}")
    return eligible, total


def main() -> int:
    default_logs = Path(__file__).resolve().parents[1] / "logs"
    parser = argparse.ArgumentParser(
        description="Compress finished DUT session logs (operator-run; dry run unless --apply)."
    )
    parser.add_argument(
        "--logs",
        type=Path,
        default=default_logs,
        help=f"logs directory to work on (default: {default_logs}). The running "
        "backend may serve from a different tree -- point this at that one.",
    )
    parser.add_argument(
        "--older-than",
        type=float,
        default=7.0,
        metavar="DAYS",
        help="only compress logs not modified for this many days (default: 7)",
    )
    parser.add_argument(
        "--min-idle",
        type=float,
        default=15.0,
        metavar="MIN",
        help="never touch a log written within this many minutes, whatever its "
        "age says (default: 15). The last line of defence if lsof is absent.",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="write the .gz but leave the .log in place (compresses nothing away; "
        "useful for a first run you want to check by hand)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually compress. Without this nothing is written.",
    )
    args = parser.parse_args()

    log_dir: Path = args.logs
    if not log_dir.is_dir():
        print(f"not a directory: {log_dir}")
        return 2

    candidates = find_candidates(log_dir, args.older_than, args.min_idle)
    eligible, total = report(candidates, log_dir)

    if not args.apply:
        print("\nDry run -- nothing was written. Re-run with --apply to compress.")
        return 0
    if not eligible:
        return 0

    print("\napplying:")
    freed = 0
    failures = 0
    for c in eligible:
        ok, message = compress(c, remove_original=not args.keep_original)
        if ok:
            try:
                freed += c.size - c.path.with_suffix(".log.gz").stat().st_size
            except OSError:
                pass
        else:
            failures += 1
        print(f"  {c.path.name:<62} {message}")

    print(f"\ncompressed {len(eligible) - failures}/{len(eligible)} of {_human(total)}")
    if not args.keep_original:
        print(f"reclaimed  {_human(freed)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
