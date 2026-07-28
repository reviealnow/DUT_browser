"""Dashboard-wide settings API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services import auth_service, settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


class CrashKeywordsBody(BaseModel):
    keywords: list[str]


# GET stays open: the crash keyword list feeds crash detection on guest-visible
# sections (Overview KPI, Logs), and gating it would silently split guests onto
# the built-in defaults while engineers use the custom list. Editing is
# engineer+ (the route-level gate below — the one deviation from the
# router-level policy in main.py).
@router.get("/crash-keywords")
def get_crash_keywords():
    return {"keywords": settings_service.get_crash_keywords()}


@router.put(
    "/crash-keywords",
    dependencies=[Depends(auth_service.require_role("engineer"))],
)
def put_crash_keywords(body: CrashKeywordsBody):
    if len(body.keywords) > 100:
        raise HTTPException(status_code=400, detail="Too many keywords (max 100)")
    for kw in body.keywords:
        if len(kw) > 120:
            raise HTTPException(status_code=400, detail="Keyword too long (max 120 chars)")
    saved = settings_service.set_crash_keywords(body.keywords)
    return {"keywords": saved}
