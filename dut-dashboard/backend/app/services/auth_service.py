"""Role-based auth: HMAC session tokens, role passcodes, FastAPI role gates.

Shared-LAN threat model, not the public internet. Registration is self-service:
anyone can claim `guest`, while `engineer` and `admin` require the shared
per-role passcode. There are no per-user passwords, so nothing secret is stored
per user and the `users` table is only a record of who claimed what.

The session cookie is a signed (not encrypted) token built from the standard
library alone -- `payload_b64 . hmac_sha256(secret, payload_b64)` -- so no new
backend dependency is needed. The secret lives in `data/session_secret`
(gitignored); losing it only invalidates live sessions.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException, Request

from app.config import SESSION_SECRET_FILE
from app.db import workspace

COOKIE_NAME = "dut_session"
TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# Ordered weakest to strongest; index is the rank used by role comparisons.
ROLES: tuple[str, ...] = ("guest", "engineer", "admin")

_PASSCODE_KEYS = {
    "engineer": "passcode_engineer",
    "admin": "passcode_admin",
}
_PASSCODE_ENV = {
    "engineer": "DUT_ENGINEER_PASSCODE",
    "admin": "DUT_ADMIN_PASSCODE",
}


# --------------------------------------------------------------------------
# Secret


def _secret_path() -> Path:
    return SESSION_SECRET_FILE


def load_secret() -> bytes:
    """Read the HMAC secret, creating it on first use.

    `O_CREAT | O_EXCL` makes creation atomic: if two workers race, the loser
    re-reads the winner's file instead of overwriting it, so tokens minted a
    moment earlier keep verifying.
    """
    path = _secret_path()
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32).hex().encode()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


# --------------------------------------------------------------------------
# Tokens


def _b64encode(raw: bytes) -> str:
    # Padding is stripped so the token stays a clean cookie value.
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str) -> str:
    return hmac.new(load_secret(), payload_b64.encode(), sha256).hexdigest()


def create_token(user: dict, now: float | None = None) -> str:
    """Sign a session token for `user`. `now` is injectable for tests."""
    issued = time.time() if now is None else now
    payload = {
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "exp": int(issued + TOKEN_TTL_SECONDS),
    }
    payload_b64 = _b64encode(json.dumps(payload, sort_keys=True).encode())
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_token(token: str | None, now: float | None = None) -> dict | None:
    """Return the token payload, or None if it is malformed, forged or expired."""
    if not token or "." not in token:
        return None
    payload_b64, _, signature = token.rpartition(".")
    if not hmac.compare_digest(_sign(payload_b64), signature):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expiry = payload.get("exp")
    if not isinstance(expiry, int):
        return None
    if (time.time() if now is None else now) >= expiry:
        return None
    if payload.get("role") not in ROLES:
        return None
    return payload


# --------------------------------------------------------------------------
# Users


def create_or_update_user(
    username: str,
    display_name: str | None,
    role: str,
    via: str = "register",
    invite_id: int | None = None,
) -> dict:
    """Register `username` at `role`, or move an existing name to that role.

    Re-registering is how someone upgrades from guest to engineer, so a taken
    username is an update rather than an error. The caller has already checked
    the passcode (or spent the invite) for the requested role.

    A role that actually changes is appended to `role_changes` — `users.role`
    only ever holds the current value, so this log is the only record of how
    someone got their privileges. `via` distinguishes a passcode registration
    from an invite redemption or a CLI mint.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")

    previous = get_user_by_username(username)
    workspace.execute(
        """
        INSERT INTO users (username, display_name, role, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(username) DO UPDATE SET display_name = excluded.display_name,
                                            role = excluded.role,
                                            updated_at = CURRENT_TIMESTAMP
        """,
        (username, display_name, role),
    )
    # Only an actual privilege change is worth a row; a plain re-login at the
    # same role would otherwise bury the real changes in noise.
    if previous is None or previous["role"] != role:
        workspace.execute(
            """
            INSERT INTO role_changes (username, from_role, to_role, via, invite_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, previous["role"] if previous else None, role, via, invite_id),
        )
    return get_user_by_username(username)  # type: ignore[return-value]


def list_users() -> list[dict]:
    rows = workspace.query_all(
        """
        SELECT id, username, display_name, role, created_at, updated_at, last_seen_at
        FROM users ORDER BY username
        """
    )
    return [dict(row) for row in rows]


def list_role_changes(limit: int = 100) -> list[dict]:
    rows = workspace.query_all(
        """
        SELECT id, username, from_role, to_role, via, invite_id, changed_at
        FROM role_changes ORDER BY id DESC LIMIT ?
        """,
        (max(1, min(limit, 500)),),
    )
    return [dict(row) for row in rows]


def get_user_by_username(username: str) -> dict | None:
    row = workspace.query_one(
        "SELECT id, username, display_name, role FROM users WHERE username = ?",
        (username,),
    )
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    row = workspace.query_one(
        "SELECT id, username, display_name, role FROM users WHERE id = ?",
        (user_id,),
    )
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Role passcodes


def get_passcode(role: str) -> str:
    """Configured passcode for `role`; empty string means the role is locked."""
    key = _PASSCODE_KEYS.get(role)
    if key is None:
        return ""
    row = workspace.query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is not None:
        return str(row["value"])
    return os.getenv(_PASSCODE_ENV[role], "")


def set_passcode(role: str, passcode: str) -> None:
    key = _PASSCODE_KEYS.get(role)
    if key is None:
        raise ValueError(f"role has no passcode: {role}")
    workspace.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, passcode),
    )


def check_passcode(role: str, supplied: str | None) -> bool:
    """Guest needs no passcode; every other role needs a configured, matching one.

    The empty check comes first on purpose: an unconfigured role is locked, and
    without this an empty supplied passcode would compare equal to it.
    """
    if role == "guest":
        return True
    expected = get_passcode(role)
    if not expected:
        return False
    return hmac.compare_digest(expected, supplied or "")


# --------------------------------------------------------------------------
# Role gates


def role_rank(role: str) -> int:
    try:
        return ROLES.index(role)
    except ValueError:
        return -1


def user_from_cookie_header(cookie_header: str | None) -> dict | None:
    """Resolve a user from a raw `Cookie:` header — for the WebSocket handshake,
    which has headers but no `Request`."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return payload_to_user(verify_token(value))
    return None


def payload_to_user(payload: dict | None) -> dict | None:
    """Re-read the user behind a valid token so a role change (or a deleted
    user) takes effect immediately instead of at token expiry."""
    if payload is None:
        return None
    return get_user_by_id(payload["user_id"])


# last_seen_at is a liveness hint, not an access log: throttled per user so a
# busy dashboard does not put a SQLite write in front of every single request.
LAST_SEEN_THROTTLE_SECONDS = 300
_last_seen_writes: dict[int, float] = {}


def touch_last_seen(user_id: int, now: float | None = None) -> bool:
    """Record that `user_id` was active. Returns True if it actually wrote."""
    stamp = time.time() if now is None else now
    previous = _last_seen_writes.get(user_id)
    if previous is not None and stamp - previous < LAST_SEEN_THROTTLE_SECONDS:
        return False
    _last_seen_writes[user_id] = stamp
    workspace.execute(
        "UPDATE users SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,)
    )
    return True


def optional_user(request: Request) -> dict | None:
    user = payload_to_user(verify_token(request.cookies.get(COOKIE_NAME)))
    if user is not None:
        touch_last_seen(user["id"])
    return user


def current_user(request: Request) -> dict:
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_role(min_role: str):
    """FastAPI dependency factory: 401 without a session, 403 below `min_role`."""
    minimum = role_rank(min_role)
    if minimum < 0:
        raise ValueError(f"unknown role: {min_role}")

    def dependency(request: Request) -> dict:
        user = current_user(request)
        if role_rank(user["role"]) < minimum:
            raise HTTPException(
                status_code=403,
                detail=f"role '{min_role}' or higher required",
            )
        return user

    return dependency
