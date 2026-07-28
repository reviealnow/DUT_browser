"""SQLite layer for the Workspace module (file sharing + bulletin) and auth.

Workspace rows stay shared-trust: `uploaded_by`/`created_by` from the original
LAN File Server schema are free-text `uploader`/`author` columns (nullable),
populated from the client's Settings display name or left NULL. The `users`
table added for role-based auth is deliberately NOT wired to them by foreign
key, so existing rows keep their free-text authorship.

One short-lived connection per call keeps this dependency-free and safe for the
app's low write volume; `PRAGMA foreign_keys=ON` enables the bulletin cascade.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import WORKSPACE_DB


FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  filepath TEXT NOT NULL,
  size INTEGER NOT NULL,
  uploader TEXT,
  uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

BULLETIN_POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bulletin_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  author TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  edited_at TIMESTAMP
);
"""

BULLETIN_COMMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bulletin_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL,
  parent_comment_id INTEGER,
  body TEXT NOT NULL,
  author TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  edited_at TIMESTAMP,
  FOREIGN KEY (post_id) REFERENCES bulletin_posts(id) ON DELETE CASCADE,
  FOREIGN KEY (parent_comment_id) REFERENCES bulletin_comments(id) ON DELETE CASCADE
);
"""


SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Tags are shared between files and bulletin posts; `norm_name` (lowercased,
# separator-stripped) is what lookups and fuzzy search match against, while
# `name` keeps the first spelling the user typed for display.
TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  norm_name TEXT NOT NULL
);
"""

FILE_TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_tags (
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  UNIQUE(file_id, tag_id)
);
"""

POST_TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS post_tags (
  post_id INTEGER NOT NULL REFERENCES bulletin_posts(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  UNIQUE(post_id, tag_id)
);
"""


# Role-based auth (P71a). `role` is the only privilege source; the CHECK keeps
# an unknown role out of the DB so the role ladder never sees a value it cannot
# rank. Registration is self-service, gated by a shared per-role passcode.
USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT,
  role TEXT NOT NULL CHECK(role IN ('guest','engineer','admin')),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# Invite links (P71c). A row is a capability: whoever holds the raw token can
# claim `role` once (or `max_uses` times) without knowing the shared passcode.
# Only the SHA-256 hash is stored -- reading this table must never yield a
# working invite -- and `used_count`/`revoked_at`/`expires_at` are what a
# redemption checks, all in one conditional UPDATE so concurrent scans of a
# single-use link cannot both win.
AUTH_TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('guest','engineer','admin')),
  label TEXT,
  created_by TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP,
  max_uses INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  revoked_at TIMESTAMP
);
"""


def _db_path() -> Path:
    return WORKSPACE_DB


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """CREATE TABLE IF NOT EXISTS never alters an existing table, so databases
    created before a column was added to the schema need an ALTER TABLE."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TIMESTAMP")


def init_db() -> None:
    with connect() as conn:
        conn.execute(FILES_SCHEMA)
        conn.execute(BULLETIN_POSTS_SCHEMA)
        conn.execute(BULLETIN_COMMENTS_SCHEMA)
        conn.execute(SETTINGS_SCHEMA)
        conn.execute(TAGS_SCHEMA)
        conn.execute(FILE_TAGS_SCHEMA)
        conn.execute(POST_TAGS_SCHEMA)
        conn.execute(USERS_SCHEMA)
        conn.execute(AUTH_TOKENS_SCHEMA)
        _ensure_column(conn, "bulletin_posts", "edited_at")
        _ensure_column(conn, "bulletin_comments", "edited_at")
        conn.commit()


def query_one(query: str, params: tuple = ()):
    with connect() as conn:
        return conn.execute(query, params).fetchone()


def query_all(query: str, params: tuple = ()):
    with connect() as conn:
        return conn.execute(query, params).fetchall()


def execute(query: str, params: tuple = ()) -> int:
    with connect() as conn:
        cursor = conn.execute(query, params)
        conn.commit()
        return cursor.lastrowid
