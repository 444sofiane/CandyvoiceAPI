from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import get_current_user
from app.services.api_keys import create_api_key, list_api_keys_for_uid, revoke_api_key

router = APIRouter()


class CreateApiKeyRequest(BaseModel):
    label: str | None = None


@router.post("/api/keys")
async def create_my_key(
    decoded_token: dict = Depends(get_current_user),
    body: CreateApiKeyRequest | None = None,
):
    """Self-service key creation: the caller authenticates the normal way
    (Firebase ID token from the website's logged-in session) and gets back
    a key scoped to their own uid — there's no way to request a key for
    anyone else through this endpoint. The raw key is only ever returned
    here; only its hash is stored, so show it to the user once and tell
    them to save it."""
    uid = decoded_token.get("uid")
    label = body.label if body else None
    raw_key, key_id = create_api_key(uid, label)
    return {"ok": True, "key_id": key_id, "api_key": raw_key, "label": label}


@router.get("/api/keys")
async def list_my_keys(decoded_token: dict = Depends(get_current_user)):
    uid = decoded_token.get("uid")
    return {"ok": True, "keys": list_api_keys_for_uid(uid)}


@router.post("/api/keys/{key_id}/revoke")
async def revoke_my_key(key_id: str, decoded_token: dict = Depends(get_current_user)):
    uid = decoded_token.get("uid")
    if not revoke_api_key(key_id, owner_uid=uid):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "revoked": True}
