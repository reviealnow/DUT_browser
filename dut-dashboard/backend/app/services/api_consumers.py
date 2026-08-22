"""Who calls the API shapes we are deciding whether we may change.

Removing a published field is safe exactly when nothing outside this repository
reads it — and that is not a question the code can answer. `GET /api/duts` needs
no session, so any client on the LAN may hold it. uvicorn's access log records a
client address and no User-Agent. `scripts/start_lan.sh` does not keep that log
anyway: the backend's stdout goes to whoever started it. A search of this
machine and of GitHub answers only for this machine (GitHub's code search
returns nothing here even for strings that certainly exist).

So this writes down the callers of a named few paths, once each, where somebody
can read them a week later.

**Once each is the whole design.** The dashboard polls these endpoints itself; a
line per request would bury the one caller that matters under thousands from the
browser already accounted for. What is worth seeing is a *distinct* caller
nobody expected — a handful of lines a week, not a firehose.

Reading it, from the repository root — the backend's stdout is where this goes,
and `start_lan.sh` does not keep it, so keep it:

    scripts/start_lan.sh 2>&1 | tee -a dut-dashboard/logs/lan-$(date +%F).log

    # a week later: every distinct caller, once each
    grep 'api-consumer probe' dut-dashboard/logs/lan-*.log

`dut-dashboard/logs/` is gitignored, which is where a record of who used the
bench belongs. Two callers on one address are two lines: the dashboard's own
browser is one of them, and anything else is the answer this was built for.

This exists for a decision and should go when the decision is made. Emptying
`WATCHED_PATHS` turns it off without unwiring anything.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: The paths whose consumers we cannot otherwise name. Add one while deciding
#: whether its response shape can change; remove it once the answer is in.
WATCHED_PATHS: frozenset[str] = frozenset({"/api/duts"})

#: A distinct caller is at most a few dozen on one bench. The cap is not a
#: tuning knob but a promise that a hostile or broken client cannot grow this
#: without bound by varying its User-Agent — it stops recording, and says so
#: once, rather than consuming the process.
MAX_DISTINCT_CALLERS = 200

#: How much of a User-Agent is worth keeping. Long enough to tell curl from a
#: browser from a Python script, short enough that nobody can push a log file
#: over with one request.
MAX_AGENT_LENGTH = 120

_seen: set[tuple[str, str, str]] = set()
_full_reported = False
_lock = threading.Lock()


def _readable(value: str) -> str:
    """A header, made safe to put in a log line.

    A User-Agent is whatever the caller typed. Carriage returns and newlines in
    one would forge log lines — the record of who called would become the thing
    a caller can write for themselves — so they are stripped rather than
    escaped, and the rest is truncated.
    """
    cleaned = "".join(character for character in value if character.isprintable())
    return cleaned[:MAX_AGENT_LENGTH] or "(none)"


def note_request(path: str, client: str | None, user_agent: str) -> bool:
    """Record one caller of a watched path. True when it had not been seen.

    Thread-safe because requests are served from a threadpool, and the check and
    the insert have to be one operation or two callers arriving together are
    both reported as new.
    """
    global _full_reported
    if path not in WATCHED_PATHS:
        return False
    caller = (path, client or "unknown", _readable(user_agent))
    with _lock:
        if caller in _seen:
            return False
        if len(_seen) >= MAX_DISTINCT_CALLERS:
            if not _full_reported:
                _full_reported = True
                logger.warning(
                    "api-consumer probe: %d distinct callers recorded, no longer recording. "
                    "That is far more than a bench has; look at what is varying its User-Agent.",
                    MAX_DISTINCT_CALLERS,
                )
            return False
        _seen.add(caller)
    logger.info("api-consumer probe: %s called by %s as %r", caller[0], caller[1], caller[2])
    return True


def seen_callers() -> list[tuple[str, str, str]]:
    """Everything recorded since the process started, for a test to inspect."""
    with _lock:
        return sorted(_seen)


def reset() -> None:
    """Forget every recorded caller. For tests, and for a fresh measurement."""
    global _full_reported
    with _lock:
        _seen.clear()
        _full_reported = False
