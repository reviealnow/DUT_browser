"""Invite links: mint, list, revoke, redeem (P71c).

An invite is a capability URL. The raw token is returned exactly once -- at
creation -- and only its SHA-256 hash is persisted, so a leaked database dump
contains no usable invites. Redemption reuses the P71a session machinery
untouched: a successful redeem just calls create_or_update_user + create_token.

Plain SHA-256 (not a password KDF) is the right tool here: the token is 24
random bytes from `secrets`, so there is no low-entropy secret to brute-force
and no stretching to do.
"""

from __future__ import annotations

import hashlib
import io
import secrets
from datetime import datetime, timedelta, timezone

from app.db import workspace
from app.services.auth_service import ROLES

# SQLite's CURRENT_TIMESTAMP format. Storing expiries in the same shape and in
# UTC keeps the SQL-side comparison a plain lexicographic string compare.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_EXPIRY_HOURS = 24 * 7
_TOKEN_BYTES = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _format_expiry(hours: float | None) -> str | None:
    """Absolute UTC expiry `hours` from now; None means the invite never expires."""
    if hours is None:
        return None
    return (_now() + timedelta(hours=hours)).strftime(_TS_FORMAT)


def create_invite(
    role: str,
    label: str | None = None,
    created_by: str | None = None,
    expires_in_hours: float | None = DEFAULT_EXPIRY_HOURS,
    max_uses: int = 1,
) -> dict:
    """Mint an invite. The returned `token` is the ONLY time the raw value
    exists outside the holder's hands -- it cannot be recovered later."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    if max_uses < 1:
        raise ValueError("max_uses must be at least 1")

    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = _format_expiry(expires_in_hours)
    invite_id = workspace.execute(
        """
        INSERT INTO auth_tokens (token_hash, role, label, created_by, expires_at, max_uses)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (hash_token(raw), role, label, created_by, expires_at, max_uses),
    )
    return {
        "id": invite_id,
        "role": role,
        "label": label,
        "token": raw,
        "url_path": f"/?invite={raw}",
        "expires_at": expires_at,
        "max_uses": max_uses,
    }


def _public(row) -> dict:
    """List shape: never the hash, never the token."""
    return {
        "id": row["id"],
        "role": row["role"],
        "label": row["label"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "max_uses": row["max_uses"],
        "used_count": row["used_count"],
        "revoked": row["revoked_at"] is not None,
        "exhausted": row["used_count"] >= row["max_uses"],
    }


def list_invites() -> list[dict]:
    rows = workspace.query_all(
        """
        SELECT id, role, label, created_by, created_at, expires_at,
               max_uses, used_count, revoked_at
        FROM auth_tokens ORDER BY id DESC
        """
    )
    return [_public(row) for row in rows]


def revoke_invite(invite_id: int) -> bool:
    """Soft-revoke. Returns False if the id is unknown; revoking twice is a no-op
    that still reports success, so a double-click is not an error."""
    row = workspace.query_one("SELECT revoked_at FROM auth_tokens WHERE id = ?", (invite_id,))
    if row is None:
        return False
    if row["revoked_at"] is None:
        workspace.execute(
            "UPDATE auth_tokens SET revoked_at = ? WHERE id = ?",
            (_now().strftime(_TS_FORMAT), invite_id),
        )
    return True


def consume_invite(raw_token: str) -> dict | None:
    """Atomically validate and consume an invite; returns its row, or None.

    Validity and consumption are ONE statement on purpose. A check-then-update
    would let two simultaneous scans of a single-use link both pass the check;
    here SQLite serialises the writes and the second UPDATE matches no row.
    """
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    with workspace.connect() as conn:
        cursor = conn.execute(
            """
            UPDATE auth_tokens SET used_count = used_count + 1
            WHERE token_hash = ?
              AND revoked_at IS NULL
              AND used_count < max_uses
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (token_hash, _now().strftime(_TS_FORMAT)),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT id, role, label FROM auth_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


def qr_svg(payload: str) -> str:
    """QR as an SVG string, for embedding as a data: URL.

    Imported lazily so a deployment that skipped the optional dependency still
    serves every other invite endpoint.
    """
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()
