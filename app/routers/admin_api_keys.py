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
    """Émet une nouvelle clé API pour `uid` (un identifiant serveur-à-serveur
    que leur propre application envoie en X-API-Key au lieu d'un token d'ID
    Firebase). La clé brute n'est retournée qu'ici — seul son hash est
    stocké, donc elle ne peut pas être récupérée ensuite. Remets-la à
    l'utilisateur une fois, puis elle a disparu."""
    uid = body.uid.strip()
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")

    try:
        raw_key, key_id = create_api_key(uid, body.label, body.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True, "key_id": key_id, "api_key": raw_key, "uid": uid, "label": body.label, "plan": body.plan}


@router.get("/api/admin/api-keys")
async def list_keys(uid: str, _admin: dict = Depends(require_admin)):
    return {"ok": True, "uid": uid, "keys": list_api_keys_for_uid(uid)}


@router.get("/api/admin/api-keys/all")
async def list_every_key(limit: int = 50, cursor: str | None = None, _admin: dict = Depends(require_admin)):
    """Toutes les clés API de tous les utilisateurs, les plus récentes
    d'abord — pour un tableau de bord admin listant clés + offres + uid/
    e-mail du propriétaire. `email` est résolu par lot depuis Firebase Auth
    (voir api_keys.resolve_emails) et vaut `null` si cet uid n'a plus de
    compte Firebase Auth.

    Paginé : renvoie le `next_cursor` de la réponse en `?cursor=` pour
    récupérer la page suivante. `next_cursor: null` signifie qu'il n'y en
    a plus."""
    limit = max(1, min(limit, 200))
    keys, next_cursor = list_all_api_keys(limit=limit, cursor=cursor)
    return {"ok": True, "keys": keys, "next_cursor": next_cursor}


@router.post("/api/admin/api-keys/{key_id}/revoke")
async def revoke_key(key_id: str, _admin: dict = Depends(require_admin)):
    """Révocation non restreinte — un admin peut révoquer la clé de
    n'importe quel utilisateur (ex. une demande de support pour une clé
    perdue/compromise). À comparer avec le /api/keys/{key_id}/revoke en
    self-service dans api_keys.py, qui ne laisse un utilisateur révoquer
    que les siennes."""
    if not revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "revoked": True}


@router.post("/api/admin/api-keys/{key_id}/plan")
async def change_key_plan(key_id: str, body: SetPlanRequest, _admin: dict = Depends(require_admin)):
    """Changement d'offre réservé au staff — pour une négociation Enterprise
    ("sur mesure") manuelle, ou un changement d'offre géré par le support
    en dehors du parcours self-service."""
    try:
        changed = set_api_key_plan(key_id, body.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not changed:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "key_id": key_id, "plan": body.plan}
