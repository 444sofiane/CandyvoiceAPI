from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routers.admin_reports import require_admin
from app.services.api_keys import create_api_key, list_api_keys_for_uid, revoke_api_key

router = APIRouter()


class CreateApiKeyRequest(BaseModel):
    uid: str
    label: str | None = None


@router.post("/api/admin/api-keys")
async def create_key(body: CreateApiKeyRequest, _admin: dict = Depends(require_admin)):
    """Issues a new API key for `uid` (a server-to-server credential their
    own application sends as X-API-Key instead of a Firebase ID token). The
    raw key is only ever returned here — only its hash is stored, so it
    can't be recovered later. Hand it to the user once, then it's gone."""
    if not body.uid.strip():
        raise HTTPException(status_code=400, detail="uid is required")

    raw_key, key_id = create_api_key(body.uid.strip(), body.label)
    return {"ok": True, "key_id": key_id, "api_key": raw_key, "uid": body.uid, "label": body.label}


@router.get("/api/admin/api-keys")
async def list_keys(uid: str, _admin: dict = Depends(require_admin)):
    return {"ok": True, "uid": uid, "keys": list_api_keys_for_uid(uid)}


@router.post("/api/admin/api-keys/{key_id}/revoke")
async def revoke_key(key_id: str, _admin: dict = Depends(require_admin)):
    if not revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "revoked": True}
