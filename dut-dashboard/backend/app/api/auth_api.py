"""Self-service registration and session endpoints.

There are no per-user passwords: identity is a claimed username, and privilege
comes from the shared per-role passcode. See services/auth_service for the
threat model.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.services import auth_service, invite_service
from app.services.auth_service import COOKIE_NAME, TOKEN_TTL_SECONDS

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MAX_NAME_LEN = 64
_MAX_LABEL_LEN = 80
# One message for every redemption failure (unknown, expired, revoked,
# exhausted). Distinguishing them would let a holder probe which tokens exist.
_INVITE_REJECTED = "invalid or expired invite"


class RegisterBody(BaseModel):
    username: str
    display_name: str | None = None
    role: str = "guest"
    passcode: str | None = None


class PasscodesBody(BaseModel):
    engineer: str | None = None
    admin: str | None = None


class InviteBody(BaseModel):
    role: str
    label: str | None = None
    expires_in_hours: float | None = invite_service.DEFAULT_EXPIRY_HOURS
    max_uses: int = 1


class RedeemBody(BaseModel):
    token: str
    username: str
    display_name: str | None = None


def _public(user: dict) -> dict:
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
    }


def _clean_names(username: str, display_name: str | None) -> tuple[str, str]:
    """Shared validation for the two endpoints that create a user."""
    name = username.strip()
    if not name or len(name) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="username must be 1-64 characters")
    shown = (display_name or "").strip() or name
    if len(shown) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="display name too long (max 64)")
    return name, shown


def _issue_session(request: Request, response: Response, user: dict) -> dict:
    response.set_cookie(
        key=COOKIE_NAME,
        value=auth_service.create_token(user),
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        # Secure follows the scheme rather than being hardcoded: the launcher
        # serves TLS in --prod but plain HTTP in dev (and with DUT_NO_TLS=1),
        # and a Secure cookie over HTTP is silently dropped -- which would log
        # every dev session out on arrival.
        secure=request.url.scheme == "https",
    )
    return _public(user)


@router.post("/register")
def register(body: RegisterBody, request: Request, response: Response) -> dict:
    username, display_name = _clean_names(body.username, body.display_name)

    if body.role not in auth_service.ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role: {body.role}")

    if not auth_service.check_passcode(body.role, body.passcode):
        # Same status for a wrong passcode and a role with none configured, so
        # the response does not reveal which roles are unlocked.
        raise HTTPException(status_code=403, detail=f"invalid passcode for role '{body.role}'")

    user = auth_service.create_or_update_user(username, display_name, body.role)
    return _issue_session(request, response, user)


@router.get("/me")
def me(user: dict = Depends(auth_service.current_user)) -> dict:
    return _public(user)


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    # delete_cookie must mirror the flags the cookie was set with, or the
    # browser keeps the original and the logout silently fails.
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
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


# --- Invite links (P71c) ---------------------------------------------------


@router.post("/invites")
def create_invite(
    body: InviteBody,
    admin: dict = Depends(auth_service.require_role("admin")),
) -> dict:
    """Mint an invite. The raw token and its QR are in THIS response only —
    nothing else ever returns them, because only the hash is stored."""
    if body.role not in auth_service.ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role: {body.role}")
    if body.max_uses < 1:
        raise HTTPException(status_code=400, detail="max_uses must be at least 1")
    label = (body.label or "").strip() or None
    if label and len(label) > _MAX_LABEL_LEN:
        raise HTTPException(status_code=400, detail="label too long (max 80)")

    invite = invite_service.create_invite(
        role=body.role,
        label=label,
        created_by=admin["username"],
        expires_in_hours=body.expires_in_hours,
        max_uses=body.max_uses,
    )
    try:
        invite["qr_svg"] = invite_service.qr_svg(invite["url_path"])
    except ImportError:
        # Optional dependency missing: the invite itself is still usable, the
        # operator just copies the URL instead of scanning it.
        invite["qr_svg"] = None
    return invite


@router.get("/invites")
def list_invites(_admin: dict = Depends(auth_service.require_role("admin"))) -> dict:
    return {"invites": invite_service.list_invites()}


@router.delete("/invites/{invite_id}")
def revoke_invite(
    invite_id: int,
    _admin: dict = Depends(auth_service.require_role("admin")),
) -> dict:
    if not invite_service.revoke_invite(invite_id):
        raise HTTPException(status_code=404, detail="invite not found")
    return {"ok": True}


@router.post("/redeem")
def redeem(body: RedeemBody, request: Request, response: Response) -> dict:
    """Trade an invite token for a session at the invite's role.

    Deliberately open (the token IS the credential) and deliberately without a
    companion "peek" endpoint — the role is only learned by redeeming, so a
    holder cannot probe an invite without spending a use.
    """
    username, display_name = _clean_names(body.username, body.display_name)
    invite = invite_service.consume_invite(body.token)
    if invite is None:
        raise HTTPException(status_code=403, detail=_INVITE_REJECTED)
    user = auth_service.create_or_update_user(username, display_name, invite["role"])
    return _issue_session(request, response, user)
