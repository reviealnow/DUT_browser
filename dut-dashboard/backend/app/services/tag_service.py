"""Shared tags for workspace files and bulletin posts, with fuzzy search.

Tags are matched on `norm_name` (lowercased, separator-stripped) so "UI",
"ui" and "u_i" are the same tag. Fuzzy search is tiered rather than scored:
exact > substring > subsequence (abbreviation), which is predictable at the
LAN scale this runs at (all tags fit in memory).
"""

from __future__ import annotations

from app.db.workspace import connect, query_all


TAG_NAME_LIMIT = 40

# Match tiers for match_score(); higher wins.
TIER_EQUAL = 3
TIER_SUBSTRING = 2
TIER_SUBSEQUENCE = 1


def normalize(s: str) -> str:
    """Canonical form used for lookups and matching: lowercase with spaces,
    underscores and dashes removed ("Usage_Insight" -> "usageinsight")."""
    return "".join(ch for ch in s.lower() if ch not in " _-\t")


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def match_score(query_norm: str, tag_norm: str) -> int:
    """Tiered fuzzy match between two normalize()d strings: 3 = equal,
    2 = one is a substring of the other, 1 = the shorter is a subsequence of
    the longer (abbreviations: "ui" matches "usageinsight"), 0 = no match."""
    if not query_norm or not tag_norm:
        return 0
    if query_norm == tag_norm:
        return TIER_EQUAL
    if query_norm in tag_norm or tag_norm in query_norm:
        return TIER_SUBSTRING
    shorter, longer = sorted((query_norm, tag_norm), key=len)
    if _is_subsequence(shorter, longer):
        return TIER_SUBSEQUENCE
    return 0


def _clean_names(names: list[str]) -> list[tuple[str, str]]:
    """Trim, drop empties/overlong names, and dedupe by normalized form
    (first spelling wins). Returns (display_name, norm_name) pairs."""
    seen: set[str] = set()
    cleaned: list[tuple[str, str]] = []
    for raw in names:
        name = raw.strip()
        norm = normalize(name)
        if not norm or len(name) > TAG_NAME_LIMIT or norm in seen:
            continue
        seen.add(norm)
        cleaned.append((name, norm))
    return cleaned


def get_or_create(names: list[str]) -> list[int]:
    """Resolve tag names to ids, creating missing tags. Lookup is by
    norm_name, so the first spelling ever stored keeps naming the tag."""
    ids: list[int] = []
    with connect() as conn:
        for name, norm in _clean_names(names):
            row = conn.execute(
                "SELECT id FROM tags WHERE norm_name = ?", (norm,)
            ).fetchone()
            if row is not None:
                ids.append(row["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO tags (name, norm_name) VALUES (?, ?)", (name, norm)
                )
                ids.append(cursor.lastrowid)
        conn.commit()
    return ids


def _set_links(link_table: str, item_column: str, item_id: int, names: list[str]) -> None:
    tag_ids = get_or_create(names)
    with connect() as conn:
        conn.execute(f"DELETE FROM {link_table} WHERE {item_column} = ?", (item_id,))
        for tag_id in tag_ids:
            conn.execute(
                f"INSERT OR IGNORE INTO {link_table} ({item_column}, tag_id) VALUES (?, ?)",
                (item_id, tag_id),
            )
        conn.commit()


def set_file_tags(file_id: int, names: list[str]) -> None:
    _set_links("file_tags", "file_id", file_id, names)


def set_post_tags(post_id: int, names: list[str]) -> None:
    _set_links("post_tags", "post_id", post_id, names)


def _tags_for(link_table: str, item_column: str, ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = query_all(
        f"""
        SELECT lt.{item_column} AS item_id, t.name
        FROM {link_table} lt JOIN tags t ON t.id = lt.tag_id
        WHERE lt.{item_column} IN ({placeholders})
        ORDER BY t.name COLLATE NOCASE
        """,
        tuple(ids),
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["item_id"], []).append(row["name"])
    return result


def tags_for_files(ids: list[int]) -> dict[int, list[str]]:
    return _tags_for("file_tags", "file_id", ids)


def tags_for_posts(ids: list[int]) -> dict[int, list[str]]:
    return _tags_for("post_tags", "post_id", ids)


def list_tags() -> list[dict]:
    """All tags with usage counts, for the suggestion datalist."""
    rows = query_all(
        """
        SELECT t.name,
               (SELECT COUNT(*) FROM file_tags ft WHERE ft.tag_id = t.id) AS file_count,
               (SELECT COUNT(*) FROM post_tags pt WHERE pt.tag_id = t.id) AS post_count
        FROM tags t
        ORDER BY t.name COLLATE NOCASE
        """
    )
    return [dict(r) for r in rows]


def search(q: str) -> dict:
    """Fuzzy tag search: score every tag against ``q``, then return the files
    and bulletin posts linked to any matched tag. Rows carry their tags so the
    client can render chips without a second request."""
    query_norm = normalize(q or "")
    matched: list[dict] = []
    if query_norm:
        for row in query_all("SELECT id, name, norm_name FROM tags"):
            score = match_score(query_norm, row["norm_name"])
            if score > 0:
                matched.append({"id": row["id"], "name": row["name"], "score": score})
    matched.sort(key=lambda t: (-t["score"], t["name"].lower()))

    tag_ids = [t["id"] for t in matched]
    files: list[dict] = []
    posts: list[dict] = []
    if tag_ids:
        placeholders = ", ".join("?" for _ in tag_ids)
        file_rows = query_all(
            f"""
            SELECT DISTINCT f.id, f.filename, f.size, f.uploader, f.uploaded_at
            FROM files f JOIN file_tags ft ON ft.file_id = f.id
            WHERE ft.tag_id IN ({placeholders})
            ORDER BY f.uploaded_at DESC, f.id DESC
            """,
            tuple(tag_ids),
        )
        files = [dict(r) for r in file_rows]
        file_tag_map = tags_for_files([f["id"] for f in files])
        for f in files:
            f["tags"] = file_tag_map.get(f["id"], [])

        post_rows = query_all(
            f"""
            SELECT DISTINCT p.id, p.title, p.body, p.author, p.created_at, p.edited_at
            FROM bulletin_posts p JOIN post_tags pt ON pt.post_id = p.id
            WHERE pt.tag_id IN ({placeholders})
            ORDER BY p.created_at DESC, p.id DESC
            """,
            tuple(tag_ids),
        )
        posts = [dict(r) for r in post_rows]
        post_tag_map = tags_for_posts([p["id"] for p in posts])
        for p in posts:
            p["tags"] = post_tag_map.get(p["id"], [])

    return {
        "query": q,
        "matched_tags": [{"name": t["name"], "score": t["score"]} for t in matched],
        "files": files,
        "posts": posts,
    }
