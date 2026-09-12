"""Saved fleet-host settings, as the Fleet > Profiles page edits them.

Gated in the router rather than in `main.py`, next to its two siblings
`fleet_api` and `collectors_api`, and gated the same: a profile names an
address and a login on this bench, which is the reach those routers are already
admin-only for. The rows are not credentials -- no route here accepts or
returns a password (see `services/fleet_profile_service.py`) -- but a list of
where the boxes are and who logs into them is not a read this dashboard hands
to a guest.

Visibility is the service's rule, not this module's: every route passes the
calling user down and gets back only what that user may see.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.collector.registry import PORT_MAX, PORT_MIN
from app.services import auth_service, fleet_profile_service as profiles

router = APIRouter(prefix="/api/fleet/profiles", tags=["fleet-profiles"])
_ADMIN = Depends(auth_service.require_role("admin"))


class ProfileBody(BaseModel):
    """One saved host setting, as the form sends it.

    Deliberately without a password field. Adding one here would be enough to
    start storing credentials, which is the promise the whole feature is built
    around -- so the refusal lives in the shape of the request, not in a check
    somewhere that could be forgotten.
    """

    name: str
    #: What to call the box once it is registered -- the collector's label.
    #: Optional, because the address is a serviceable name on its own.
    device_name: str | None = None
    host: str
    port: int = Field(default=profiles.DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)
    username: str
    #: 'shared' (the bench sees it) or 'private' (its owner alone). Defaults to
    #: private: a setting that becomes everyone's should be an explicit choice.
    scope: str = profiles.SCOPE_PRIVATE


def _handled(run):
    """Map the service's three failures onto the three statuses they mean."""
    try:
        return run()
    except profiles.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except profiles.ProfileForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except profiles.ProfileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
def list_profiles(user: dict = _ADMIN) -> dict:
    return {"profiles": profiles.list_profiles(user)}


@router.post("")
def create_profile(body: ProfileBody, user: dict = _ADMIN) -> dict:
    profile = _handled(lambda: profiles.create_profile(user, body.model_dump()))
    return {"ok": True, "profile": profile}


@router.put("/{profile_id}")
def update_profile(profile_id: int, body: ProfileBody, user: dict = _ADMIN) -> dict:
    profile = _handled(lambda: profiles.update_profile(user, profile_id, body.model_dump()))
    return {"ok": True, "profile": profile}


@router.delete("/{profile_id}")
def delete_profile(profile_id: int, user: dict = _ADMIN) -> dict:
    profile = _handled(lambda: profiles.delete_profile(user, profile_id))
    return {"ok": True, "id": profile_id, "name": profile["name"]}
