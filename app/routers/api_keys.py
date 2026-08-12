from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import config
from app.deps import get_current_user
from app.services.api_key_quota import plan_rate_limit, read_usage_for_key
from app.services.api_keys import create_api_key, get_api_key_for_owner, list_api_keys_for_uid, revoke_api_key
from app.services.firebase import get_firestore_client

router = APIRouter()


class CreateApiKeyRequest(BaseModel):
    label: str | None = None
    plan: str = config.DEFAULT_API_KEY_PLAN


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
    them to save it.

    `plan` is client-supplied and trusted as-is for now, matching the
    current pricing-page mockup (choose a plan, get a key) — there's no
    payment verification behind it yet. Before this is a real paid
    product, plan assignment needs to move behind a server-verified event
    (e.g. a Stripe webhook after checkout), otherwise nothing stops a
    caller from just asking for "enterprise"."""
    uid = decoded_token.get("uid")
    label = body.label if body else None
    plan = body.plan if body else config.DEFAULT_API_KEY_PLAN

    try:
        raw_key, key_id = create_api_key(uid, label, plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True, "key_id": key_id, "api_key": raw_key, "label": label, "plan": plan}


@router.get("/api/keys")
async def list_my_keys(decoded_token: dict = Depends(get_current_user)):
    uid = decoded_token.get("uid")
    return {"ok": True, "keys": list_api_keys_for_uid(uid)}


@router.get("/api/keys/{key_id}/usage")
async def get_my_key_usage(key_id: str, decoded_token: dict = Depends(get_current_user)):
    """Current-billing-period usage/limit per feature, plus the plan's
    rate limit — everything an account settings page needs to render a
    usage dashboard for one key."""
    uid = decoded_token.get("uid")
    key = get_api_key_for_owner(key_id, uid)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    firestore_client = get_firestore_client()
    usage = read_usage_for_key(firestore_client, key_id, key["plan"])
    max_requests, window_seconds = plan_rate_limit(key["plan"])

    return {
        "ok": True,
        "key_id": key_id,
        "label": key["label"],
        "plan": key["plan"],
        "revoked": key["revoked"],
        "period": usage["period"],
        "usage": usage["features"],
        "rate_limit": {"max_requests": max_requests, "window_seconds": window_seconds},
    }


@router.post("/api/keys/{key_id}/revoke")
async def revoke_my_key(key_id: str, decoded_token: dict = Depends(get_current_user)):
    uid = decoded_token.get("uid")
    if not revoke_api_key(key_id, owner_uid=uid):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "revoked": True}
