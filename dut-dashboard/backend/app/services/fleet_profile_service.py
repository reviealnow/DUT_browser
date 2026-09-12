"""Saved fleet-host settings: the Hosts form, kept for next time.

A profile is a *description of where a box is* -- host, port, login name, and
the name to call it -- so an operator re-registering a collector picks a row
instead of retyping four fields. It configures nothing on its own: applying one
fills the form, and the form still asks for the password.

Two rules shape everything here:

**No password is stored.** A collector's password lives in the backend's memory
for the life of its process (`collector/registry.py`) and reaches no file. A
profile that carried one would put on disk exactly what that model refuses to,
so this table has no column for it and no route accepts one.

**Scope is the visibility rule, owner is who may change it.** A `shared`
profile is readable by everyone who can reach this surface; a `private` one by
its owner alone. Either way only the owner may edit or delete it -- a shared
profile is an offer, not a thing the bench may rewrite under its author.
"""

from __future__ import annotations

import sqlite3

from app.collector.registry import ADDRESS_RE, MAX_LABEL_LEN, PORT_MAX, PORT_MIN, USER_RE
from app.db import workspace

SCOPE_SHARED = "shared"
SCOPE_PRIVATE = "private"
SCOPES = (SCOPE_SHARED, SCOPE_PRIVATE)
MAX_NAME_LEN = 48
DEFAULT_PORT = 22

#: Everything a client is given, plus the two fields the reader needs to decide
#: what the row offers them: who owns it, and whether they may change it.
_SELECT = """
SELECT p.id, p.name, p.device_name, p.host, p.port, p.username, p.scope,
       p.owner_user_id, p.created_at, p.updated_at,
       COALESCE(u.display_name, u.username) AS owner
  FROM fleet_profiles p
  LEFT JOIN users u ON u.id = p.owner_user_id
"""


class ProfileError(ValueError):
    """A profile this service will not accept, worded for the operator."""


class ProfileNotFound(LookupError):
    """No profile with that id is visible to this user."""


class ProfileForbidden(PermissionError):
    """Visible, but owned by somebody else."""


def _row_to_profile(row: sqlite3.Row, viewer_id: int) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "device_name": row["device_name"],
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "scope": row["scope"],
        # The display name is read through a join rather than copied into the
        # row, so somebody renaming themselves does not leave their old name
        # standing in this table.
        "owner": row["owner"] or "unknown",
        "owner_user_id": row["owner_user_id"],
        # Answered here rather than left to the client to work out from an id
        # comparison: the same rule decides the button and the 403, and it
        # should be written once.
        "can_edit": row["owner_user_id"] == viewer_id,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _text(payload: dict, key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ProfileError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise ProfileError(f"{key} must be text")
    return value.strip()


def _clean(payload: dict) -> dict:
    """Validate one profile, naming the field that is wrong.

    Host, port and username are held to the expressions `collector/registry.py`
    applies, imported rather than restated: a profile whose host the collector
    registry would refuse is a row that can only fail once somebody applies it.
    """
    name = _text(payload, "name")
    if len(name) > MAX_NAME_LEN:
        raise ProfileError(f"name must be at most {MAX_NAME_LEN} characters")

    host = _text(payload, "host")
    if not ADDRESS_RE.fullmatch(host):
        raise ProfileError("host must be an address or a host name")

    username = _text(payload, "username")
    if not USER_RE.fullmatch(username):
        raise ProfileError("username must be an SSH login name")

    port = payload.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise ProfileError(f"port must be between {PORT_MIN} and {PORT_MAX}")

    device_name = _text(payload, "device_name", required=False)
    if device_name is not None:
        device_name = device_name[:MAX_LABEL_LEN]

    scope = payload.get("scope") or SCOPE_PRIVATE
    if scope not in SCOPES:
        raise ProfileError(f"scope must be '{SCOPE_SHARED}' or '{SCOPE_PRIVATE}'")

    return {
        "name": name,
        "device_name": device_name,
        "host": host,
        "port": port,
        "username": username,
        "scope": scope,
    }


def list_profiles(user: dict) -> list[dict]:
    """Every shared profile, plus this user's own private ones."""
    rows = workspace.query_all(
        _SELECT
        + " WHERE p.scope = ? OR p.owner_user_id = ?"
        " ORDER BY LOWER(p.name)",
        (SCOPE_SHARED, user["id"]),
    )
    return [_row_to_profile(row, user["id"]) for row in rows]


def get_profile(user: dict, profile_id: int) -> dict:
    """One profile, if this user is allowed to see it.

    A private profile belonging to somebody else is reported as missing rather
    than as forbidden: "there is no such row" is all a reader is owed, and the
    alternative confirms the existence of another person's saved bench.
    """
    row = workspace.query_one(_SELECT + " WHERE p.id = ?", (profile_id,))
    if row is None or (row["scope"] != SCOPE_SHARED and row["owner_user_id"] != user["id"]):
        raise ProfileNotFound(f"Unknown profile: {profile_id}")
    return _row_to_profile(row, user["id"])


def _owned(user: dict, profile_id: int) -> dict:
    profile = get_profile(user, profile_id)
    if not profile["can_edit"]:
        raise ProfileForbidden(f"{profile['name']} belongs to {profile['owner']}")
    return profile


def create_profile(user: dict, payload: dict) -> dict:
    fields = _clean(payload)
    try:
        profile_id = workspace.execute(
            "INSERT INTO fleet_profiles"
            " (name, device_name, host, port, username, scope, owner_user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fields["name"],
                fields["device_name"],
                fields["host"],
                fields["port"],
                fields["username"],
                fields["scope"],
                user["id"],
            ),
        )
    except sqlite3.IntegrityError as exc:
        # The only UNIQUE on this table is (owner_user_id, name). Named rather
        # than passed through: "UNIQUE constraint failed" is not a sentence for
        # the person who just pressed Save.
        raise ProfileError(f"You already have a profile named \"{fields['name']}\"") from exc
    return get_profile(user, profile_id)


def update_profile(user: dict, profile_id: int, payload: dict) -> dict:
    """Replace a profile's fields. Owner only; the whole record is sent."""
    _owned(user, profile_id)
    fields = _clean(payload)
    try:
        workspace.execute(
            "UPDATE fleet_profiles"
            " SET name = ?, device_name = ?, host = ?, port = ?, username = ?, scope = ?,"
            "     updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ?",
            (
                fields["name"],
                fields["device_name"],
                fields["host"],
                fields["port"],
                fields["username"],
                fields["scope"],
                profile_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ProfileError(f"You already have a profile named \"{fields['name']}\"") from exc
    return get_profile(user, profile_id)


def delete_profile(user: dict, profile_id: int) -> dict:
    """Drop a profile. Nothing registered from it is touched."""
    profile = _owned(user, profile_id)
    workspace.execute("DELETE FROM fleet_profiles WHERE id = ?", (profile_id,))
    return profile
