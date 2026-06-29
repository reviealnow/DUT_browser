"""Dashboard-wide settings API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


class CrashKeywordsBody(BaseModel):
    keywords: list[str]


@router.get("/crash-keywords")
def get_crash_keywords():
    return {"keywords": settings_service.get_crash_keywords()}


@router.put("/crash-keywords")
def put_crash_keywords(body: CrashKeywordsBody):
    if len(body.keywords) > 100:
        raise HTTPException(status_code=400, detail="Too many keywords (max 100)")
    for kw in body.keywords:
        if len(kw) > 120:
            raise HTTPException(status_code=400, detail="Keyword too long (max 120 chars)")
    saved = settings_service.set_crash_keywords(body.keywords)
    return {"keywords": saved}
