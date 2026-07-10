"""Cross-workspace API: tag directory + fuzzy tag search over files and
bulletin posts (shared-trust model — no auth)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.services import tag_service

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("/tags")
def list_tags() -> dict:
    """All tags with usage counts (feeds the tag-input suggestion datalist)."""
    return {"tags": tag_service.list_tags()}


@router.get("/search")
def search(q: Annotated[str, Query(max_length=200)] = "") -> dict:
    """Fuzzy tag search; a blank query returns empty result lists."""
    return tag_service.search(q.strip())
