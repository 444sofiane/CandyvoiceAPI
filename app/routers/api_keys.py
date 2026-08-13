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
    """Création de clé en self-service : l'appelant s'authentifie de façon
    normale (token d'ID Firebase de la session connectée du site) et
    reçoit en retour une clé rattachée à son propre uid — il n'y a aucun
    moyen de demander une clé pour quelqu'un d'autre via cet endpoint. La
    clé brute n'est retournée qu'ici ; seul son hash est stocké, donc
    montre-la à l'utilisateur une fois et dis-lui de la sauvegarder.

    `plan` est fourni par le client et pris tel quel pour l'instant, à
    l'image de la maquette actuelle de la page tarifs (choisir une offre,
    obtenir une clé) — il n'y a pas encore de vérification de paiement
    derrière. Avant que ce soit un vrai produit payant, l'attribution de
    l'offre doit passer derrière un événement vérifié côté serveur (ex. un
    webhook Stripe après le paiement), sinon rien n'empêche un appelant de
    simplement demander "enterprise"."""
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
    """Usage/limite de la période de facturation en cours par
    fonctionnalité, plus la limite de débit de l'offre — tout ce dont une
    page de paramètres de compte a besoin pour afficher un tableau de bord
    d'usage pour une clé."""
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
