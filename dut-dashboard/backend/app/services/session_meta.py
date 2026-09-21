"""Where a session log came from, recorded in the log itself.

A session log's filename carries the label typed at Open and a timestamp, and
nothing else. That is enough to tell two sessions apart and not enough to say
which unit, which model or which host one came from -- and on this bench one
cable (`/dev/cu.PL2303G-USBtoUART1130`) carried an AP6_420E through July and an
AP6_840E from the 28th, under names that differ only in the time. The bundle a
Download produced dropped even the label, naming itself after the moment the
button was pressed.

The identity goes into the log as comment lines rather than into its name
because the part that matters is not known when the file is created. The unit's
own name arrives from `/api/dut/identify`, seconds after the console opened;
whatever the registry remembered from an earlier session may belong to a device
since swapped for another of the same model on the same cable, which is exactly
the case `device_id` exists to catch. So the log records what was known, when:

    # session-meta {"dut_id":"lab2","kind":"start","label":"lab2",...}
    # session-meta {"device_id":"AP6420E-PB1005QPCFVFMA8","kind":"identity",...}

Comment lines, like the `# mode=` header above them, which the parser and
analyzer3 already pass over -- and a log copied off this machine keeps its
provenance with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services import dut_model

META_PREFIX = "# session-meta "

# How much of a log is read to find its provenance. Both records are written
# within seconds of the console opening, and the prompt a legacy log is read
# for appears at its first command; a quarter of a megabyte is minutes of sysMon
# output. Bounded because /api/logs reads every session log on each listing,
# and a long run is tens of megabytes.
HEAD_SCAN_BYTES = 256 * 1024

# The transport and the port are not repeated in the start record: the worker's
# own `# mode=` header, the log's first line, already states them, and is the
# one place they come from for every log ever written.
HEADER_FIELDS = ("mode", "source")
START_FIELDS = ("dut_id", "label", "host", "collector_id")
IDENTITY_FIELDS = ("device_id", "model")
FIELDS = HEADER_FIELDS + START_FIELDS + IDENTITY_FIELDS

# The header every session log has always opened with.
_HEADER_RE = re.compile(r"^# mode=(?P<mode>\S+) source=(?P<source>.*)$")
# The DUT's own shell prompt, `AP6_420E#`, at the start of a line. Deliberately
# narrower than `dut_model.detect_model`: that pattern also accepts a hostname,
# and a log carries other units' hostnames -- a mesh probe lists every member --
# so only the prompt is evidence of the device the console is on.
_PROMPT_RE = re.compile(r"(?m)^(AP6_\d{3}[EX]?)#")


def format_meta_line(record: dict) -> str:
    """One provenance record as the comment line a session log carries."""
    return META_PREFIX + json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"


def read_session_meta(path: Path, limit: int = HEAD_SCAN_BYTES) -> dict:
    """What a session log says about where it came from.

    Every field in `FIELDS` is present and None when the log does not say.
    The first `start` record is this session's own; the first `identity` after
    it is what the unit answered when asked. A replayed log also carries the
    records of the log being replayed, which come after its own start -- so a
    replay reports the unit its data was recorded on, which is the truth about
    that data.

    The transport and port come from the `# mode=` header, so logs written
    before these records existed still say that much, and the prompt names
    their model. Never raises; an unreadable log describes nothing.
    """
    meta: dict = dict.fromkeys(FIELDS)
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fp:
            head = fp.read(limit)
    except OSError:
        return meta

    seen_start = False
    seen_identity = False
    for line in head.splitlines():
        if line.startswith(META_PREFIX):
            try:
                record = json.loads(line[len(META_PREFIX):])
            except ValueError:
                # The last line of the head may be cut mid-record.
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("kind")
            if kind == "start" and not seen_start:
                seen_start = True
                meta.update(_pick(record, START_FIELDS))
            elif kind == "identity" and seen_start and not seen_identity:
                seen_identity = True
                meta.update(_pick(record, IDENTITY_FIELDS))
            continue
        if meta["mode"] is None:
            # Only the first: a replayed log repeats the header of the log it
            # replays, and this session's transport is the replay.
            header = _HEADER_RE.match(line)
            if header:
                meta["mode"] = header.group("mode")
                meta["source"] = header.group("source").strip() or None

    if meta["model"] is None:
        prompt = _PROMPT_RE.search(head)
        if prompt:
            meta["model"] = dut_model.detect_model(prompt.group(1))
    return meta


def _pick(record: dict, fields: tuple[str, ...]) -> dict:
    return {name: record[name] for name in fields if isinstance(record.get(name), str) and record[name]}
