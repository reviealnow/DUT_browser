from __future__ import annotations

import threading
from collections import deque

CONSOLE_BUFFER_MAX = 500


class ConsoleBuffer:
    """In-memory ring of the most recent console lines for instant backfill.

    Fed from the parser's ``console_line`` / ``console_line_batch`` events so a
    page (re)load can seed the Serial Console immediately instead of starting
    empty. Not persisted — the durable record is the raw session log.

    Thread-safety: ``observe`` runs on the SerialWorker thread; ``recent`` runs
    on the asyncio request thread. Guarded by one lock.
    """

    def __init__(self, maxlen: int = CONSOLE_BUFFER_MAX) -> None:
        self._lock = threading.Lock()
        self._lines: deque[str] = deque(maxlen=maxlen)

    def observe(self, event: dict) -> None:
        try:
            event_type = event.get("type")
            if event_type == "console_line":
                text = event.get("text")
                if isinstance(text, str):
                    with self._lock:
                        self._lines.append(text)
            elif event_type == "console_line_batch":
                lines = event.get("lines")
                if isinstance(lines, list):
                    incoming = [line for line in lines if isinstance(line, str)]
                    if incoming:
                        with self._lock:
                            self._lines.extend(incoming)
        except Exception:
            # Never let buffering break the stream.
            return

    def recent(self, limit: int) -> list[str]:
        with self._lock:
            lines = list(self._lines)
        if limit > 0:
            lines = lines[-limit:]
        return lines
