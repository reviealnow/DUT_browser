"""SQLite layer for the Workspace module (file sharing + bulletin).

Shared-trust model: there is no users table and no auth. The original LAN
File Server schema referenced a users table via foreign keys; here the
`uploaded_by`/`created_by` columns become free-text `uploader`/`author`
(nullable), populated from the client's Settings display name or left NULL.

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
