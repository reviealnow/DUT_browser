"""Bulletin board API (shared-trust model — no auth, author is free text)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services import bulletin_service

router = APIRouter(prefix="/api/bulletin", tags=["bulletin"])


class PostCreate(BaseModel):
    title: str
    body: str
    author: str | None = None


class CommentCreate(BaseModel):
    body: str
    author: str | None = None
    parent_comment_id: int | None = None


@router.get("/posts")
def list_posts(
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Posts newest first. Without ``limit`` the full list is returned (legacy
    behaviour); ``total`` always counts every post."""
    return {
        "posts": bulletin_service.list_posts(limit, offset),
        "total": bulletin_service.count_posts(),
    }


@router.post("/posts")
def create_post(body: PostCreate) -> dict:
    try:
        post_id = bulletin_service.create_post(body.title, body.body, body.author)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": post_id}


@router.delete("/posts/{post_id}")
def delete_post(post_id: int) -> dict:
    if bulletin_service.get_post(post_id) is None:
        raise HTTPException(status_code=404, detail="Post not found")
    bulletin_service.delete_post(post_id)
    return {"ok": True}


@router.post("/posts/{post_id}/comments")
def create_comment(post_id: int, body: CommentCreate) -> dict:
    try:
        comment_id = bulletin_service.create_comment(
            post_id, body.body, body.author, body.parent_comment_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": comment_id}
