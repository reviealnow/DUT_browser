"""Self-service registration and session endpoints.

There are no per-user passwords: identity is a claimed username, and privilege
comes from the shared per-role passcode. See services/auth_service for the
threat model.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.services import auth_service
from app.services.auth_service import COOKIE_NAME, TOKEN_TTL_SECONDS

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MAX_NAME_LEN = 64


class RegisterBody(BaseModel):
    username: str
    display_name: str | None = None
    role: str = "guest"
    passcode: str | None = None


class PasscodesBody(BaseModel):
    engineer: str | None = None
    admin: str | None = None


def _public(user: dict) -> dict:
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
    }


@router.post("/register")
def register(body: RegisterBody, response: Response) -> dict:
    username = body.username.strip()
    if not username or len(username) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="username must be 1-64 characters")

    display_name = (body.display_name or "").strip() or username
    if len(display_name) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="display name too long (max 64)")

    if body.role not in auth_service.ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role: {body.role}")

    if not auth_service.check_passcode(body.role, body.passcode):
        # Same status for a wrong passcode and a role with none configured, so
        # the response does not reveal which roles are unlocked.
        raise HTTPException(status_code=403, detail=f"invalid passcode for role '{body.role}'")

    user = auth_service.create_or_update_user(username, display_name, body.role)
    response.set_cookie(
        key=COOKIE_NAME,
        value=auth_service.create_token(user),
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return _public(user)


@router.get("/me")
def me(user: dict = Depends(auth_service.current_user)) -> dict:
    return _public(user)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/passcodes")
def set_passcodes(
    body: PasscodesBody,
    _admin: dict = Depends(auth_service.require_role("admin")),
) -> dict:
    """Update the shared role passcodes. An omitted field is left unchanged; an
    empty string locks that role."""
    for role in ("engineer", "admin"):
        value = getattr(body, role)
        if value is not None:
            auth_service.set_passcode(role, value)
    return {
        "engineer_locked": not auth_service.get_passcode("engineer"),
        "admin_locked": not auth_service.get_passcode("admin"),
    }
