"""Bulletin board: posts with one level of nested replies.

Ported from the LAN File Server. Shared-trust model: no user_id — `author` is
free text (nullable). Validation limits and the nested-reply assembly are kept.
"""

from __future__ import annotations

from collections import defaultdict

from app.db.workspace import execute, query_all, query_one
from app.services import tag_service


POST_TITLE_LIMIT = 120
POST_BODY_LIMIT = 1000
COMMENT_BODY_LIMIT = 600


def _validate_text(value: str, field_name: str, limit: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required.")
    if len(cleaned) > limit:
        raise ValueError(f"{field_name} must be {limit} characters or fewer.")
    return cleaned


def _with_bool_flags(row: dict) -> dict:
    """SQLite returns 0/1 for a boolean expression; JSON consumers checking
    `=== false` would silently miss a 0, so coerce before the row leaves."""
    if "author_verified" in row:
        row["author_verified"] = bool(row["author_verified"])
    return row


def _clean_author(author: str | None) -> str | None:
    return author.strip() if author and author.strip() else None


def create_post(
    title: str,
    body: str,
    author: str | None = None,
    author_user_id: int | None = None,
) -> int:
    clean_title = _validate_text(title, "Post title", POST_TITLE_LIMIT)
    clean_body = _validate_text(body, "Post content", POST_BODY_LIMIT)
    return execute(
        "INSERT INTO bulletin_posts (title, body, author, author_user_id)"
        " VALUES (?, ?, ?, ?)",
        (clean_title, clean_body, _clean_author(author), author_user_id),
    )


def create_comment(
    post_id: int,
    body: str,
    author: str | None = None,
    parent_comment_id: int | None = None,
    author_user_id: int | None = None,
) -> int:
    post = query_one("SELECT id FROM bulletin_posts WHERE id = ?", (post_id,))
    if post is None:
        raise ValueError("Post not found.")

    clean_body = _validate_text(body, "Reply", COMMENT_BODY_LIMIT)

    if parent_comment_id is not None:
        parent = query_one(
            "SELECT id, post_id FROM bulletin_comments WHERE id = ?",
            (parent_comment_id,),
        )
        if parent is None or parent["post_id"] != post_id:
            raise ValueError("Reply target not found.")

    return execute(
        """
        INSERT INTO bulletin_comments (post_id, parent_comment_id, body, author, author_user_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_id, parent_comment_id, clean_body, _clean_author(author), author_user_id),
    )


def update_post(post_id: int, title: str, body: str) -> bool:
    """Shared-trust model: anyone may edit any post (matches ownerless delete).
    Returns False when the post does not exist."""
    clean_title = _validate_text(title, "Post title", POST_TITLE_LIMIT)
    clean_body = _validate_text(body, "Post content", POST_BODY_LIMIT)
    if query_one("SELECT id FROM bulletin_posts WHERE id = ?", (post_id,)) is None:
        return False
    execute(
        "UPDATE bulletin_posts SET title = ?, body = ?, edited_at = CURRENT_TIMESTAMP"
        " WHERE id = ?",
        (clean_title, clean_body, post_id),
    )
    return True


def update_comment(comment_id: int, body: str) -> bool:
    """Shared-trust edit of a comment or nested reply. Returns False when the
    comment does not exist."""
    clean_body = _validate_text(body, "Reply", COMMENT_BODY_LIMIT)
    if query_one("SELECT id FROM bulletin_comments WHERE id = ?", (comment_id,)) is None:
        return False
    execute(
        "UPDATE bulletin_comments SET body = ?, edited_at = CURRENT_TIMESTAMP WHERE id = ?",
        (clean_body, comment_id),
    )
    return True


def get_post(post_id: int) -> dict | None:
    row = query_one(
        "SELECT id, title, body, author, created_at, edited_at,"
        " author_user_id IS NOT NULL AS author_verified"
        " FROM bulletin_posts WHERE id = ?",
        (post_id,),
    )
    return _with_bool_flags(dict(row)) if row is not None else None


def delete_post(post_id: int) -> None:
    """Delete a post; its comments and nested replies cascade via the schema's
    ``ON DELETE CASCADE`` foreign keys (foreign_keys pragma is enabled per
    connection)."""
    execute("DELETE FROM bulletin_posts WHERE id = ?", (post_id,))


def _like_pattern(q: str) -> str:
    """Substring LIKE pattern with %/_ escaped so user input matches literally."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_POSTS_MATCH = "(title LIKE :pat ESCAPE '\\' OR body LIKE :pat ESCAPE '\\')"


def count_posts(q: str | None = None) -> int:
    if q:
        row = query_one(
            f"SELECT COUNT(*) AS n FROM bulletin_posts WHERE {_POSTS_MATCH}",
            {"pat": _like_pattern(q)},
        )
    else:
        row = query_one("SELECT COUNT(*) AS n FROM bulletin_posts")
    return int(row["n"])


def list_posts(limit: int | None = None, offset: int = 0, q: str | None = None) -> list[dict]:
    """Newest first. ``limit=None`` returns everything (legacy no-param behaviour);
    otherwise a page of ``limit`` posts starting at ``offset``. ``q`` filters by
    title/body substring (case-insensitive). Comments are only fetched for the
    returned page."""
    posts_sql = f"""
        SELECT id, title, body, author, created_at, edited_at,
               author_user_id IS NOT NULL AS author_verified
        FROM bulletin_posts
        {f"WHERE {_POSTS_MATCH}" if q else ""}
        ORDER BY created_at DESC, id DESC
        """
    params: dict = {"pat": _like_pattern(q)} if q else {}
    if limit is not None:
        posts_sql += " LIMIT :limit OFFSET :offset"
        params.update({"limit": limit, "offset": offset})
    posts = query_all(posts_sql, params)

    post_ids = [row["id"] for row in posts]
    if post_ids:
        placeholders = ", ".join("?" for _ in post_ids)
        comments = query_all(
            f"""
            SELECT id, post_id, parent_comment_id, body, author, created_at, edited_at,
                   author_user_id IS NOT NULL AS author_verified
            FROM bulletin_comments
            WHERE post_id IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            """,
            tuple(post_ids),
        )
    else:
        comments = []

    replies_by_parent: dict[int | None, list[dict]] = defaultdict(list)
    comments_by_post: dict[int, list[dict]] = defaultdict(list)

    for row in comments:
        comment = _with_bool_flags(dict(row))
        comment["replies"] = []
        replies_by_parent[comment["parent_comment_id"]].append(comment)

    for comment in replies_by_parent[None]:
        comment["replies"] = replies_by_parent.get(comment["id"], [])
        comments_by_post[comment["post_id"]].append(comment)

    tag_map = tag_service.tags_for_posts(post_ids)

    result = []
    for row in posts:
        post = _with_bool_flags(dict(row))
        post["comments"] = comments_by_post.get(post["id"], [])
        post["tags"] = tag_map.get(post["id"], [])
        result.append(post)
    return result
