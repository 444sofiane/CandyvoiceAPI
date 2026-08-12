from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import config
from app.routers.admin_reports import require_admin
from app.services.api_keys import (
    create_api_key,
    list_all_api_keys,
    list_api_keys_for_uid,
    revoke_api_key,
    set_api_key_plan,
)

router = APIRouter()


class CreateApiKeyRequest(BaseModel):
    uid: str
    label: str | None = None
    plan: str = config.DEFAULT_API_KEY_PLAN


class SetPlanRequest(BaseModel):
    plan: str


@router.post("/api/admin/api-keys")
async def create_key(body: CreateApiKeyRequest, _admin: dict = Depends(require_admin)):
    """Issues a new API key for `uid` (a server-to-server credential their
    own application sends as X-API-Key instead of a Firebase ID token). The
    raw key is only ever returned here — only its hash is stored, so it
    can't be recovered later. Hand it to the user once, then it's gone."""
    if not body.uid.strip():
        raise HTTPException(status_code=400, detail="uid is required")

    try:
        raw_key, key_id = create_api_key(body.uid.strip(), body.label, body.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True, "key_id": key_id, "api_key": raw_key, "uid": body.uid, "label": body.label, "plan": body.plan}


@router.get("/api/admin/api-keys")
async def list_keys(uid: str, _admin: dict = Depends(require_admin)):
    return {"ok": True, "uid": uid, "keys": list_api_keys_for_uid(uid)}


@router.get("/api/admin/api-keys/all")
async def list_every_key(limit: int = 50, cursor: str | None = None, _admin: dict = Depends(require_admin)):
    """Every API key across every user, newest first — for an admin
    dashboard listing keys + plans + owning uid. Each row has a bare
    `uid`, not an email; resolve that separately (e.g. Firebase Auth
    lookup, only for rows currently on screen) rather than expect it here.

    Paginated: pass the response's `next_cursor` back as `?cursor=` to
    fetch the next page. `next_cursor: null` means there are no more."""
    limit = max(1, min(limit, 200))
    keys, next_cursor = list_all_api_keys(limit=limit, cursor=cursor)
    return {"ok": True, "keys": keys, "next_cursor": next_cursor}


@router.post("/api/admin/api-keys/{key_id}/revoke")
async def revoke_key(key_id: str, _admin: dict = Depends(require_admin)):
    """Unscoped revoke — an admin can revoke any user's key (e.g. a support
    request for a lost/compromised key). Compare to the self-service
    /api/keys/{key_id}/revoke in api_keys.py, which only lets a user revoke
    their own."""
    if not revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "revoked": True}


@router.post("/api/admin/api-keys/{key_id}/plan")
async def change_key_plan(key_id: str, body: SetPlanRequest, _admin: dict = Depends(require_admin)):
    """Staff-only plan change — for a manual Enterprise ("sur mesure")
    negotiation, or a support-handled upgrade/downgrade outside the
    self-service flow."""
    try:
        changed = set_api_key_plan(key_id, body.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not changed:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "plan": body.plan}
