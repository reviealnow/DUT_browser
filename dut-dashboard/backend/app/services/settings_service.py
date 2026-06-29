"""Persistent key/value settings backed by workspace.db."""

from __future__ import annotations

import json

from app.db import workspace

# Default crash keywords mirror the built-in frontend pattern.
_DEFAULT_CRASH_KEYWORDS = ["kernel panic", "q6 crash", "watchdog"]
_CRASH_KW_KEY = "crash_keywords"


def get_crash_keywords() -> list[str]:
    row = workspace.query_one(
        "SELECT value FROM settings WHERE key = ?", (_CRASH_KW_KEY,)
    )
    if row is None:
        return list(_DEFAULT_CRASH_KEYWORDS)
    try:
        parsed = json.loads(row["value"])
        if isinstance(parsed, list):
            return [str(k) for k in parsed if k]
    except (json.JSONDecodeError, KeyError):
        pass
    return list(_DEFAULT_CRASH_KEYWORDS)


def set_crash_keywords(keywords: list[str]) -> list[str]:
    cleaned = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    workspace.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (_CRASH_KW_KEY, json.dumps(cleaned)),
    )
    return cleaned
