"""Émission/vérification des clés API, pour les appelants serveur-à-serveur
qui apportent leur propre application au lieu de passer par l'UI web
authentifiée en Firebase. La valeur brute d'une clé n'est montrée qu'une
seule fois, à la création — seul son hash SHA-256 est persisté (comme id
de document Firestore, pour une recherche en O(1) à chaque requête), à
l'image de la façon dont un mot de passe serait stocké.
"""
import hashlib
import secrets
from dataclasses import dataclass

from firebase_admin import auth as firebase_auth
from firebase_admin import firestore as admin_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app import config
from app.services.firebase import get_firestore_client

_COLLECTION = "apiKeys"
_GET_USERS_MAX_BATCH = 100  # plafond documenté par appel de firebase_admin.auth.get_users


@dataclass
class ApiKeyRecord:
    key_id: str
    uid: str
    plan: str


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(uid: str, label: str | None = None, plan: str = config.DEFAULT_API_KEY_PLAN) -> tuple[str, str]:
    """Génère une nouvelle clé pour `uid` sur `plan`, stocke son hash, et
    retourne (raw_key, key_id). `raw_key` n'est jamais persistée —
    capture-la maintenant, elle ne pourra pas être récupérée plus tard.

    Lève ValueError si `plan` n'est pas un palier connu — les appelants
    doivent transformer ça en 400, pas laisser ça devenir un 500 opaque."""
    if plan not in config.API_KEY_PLANS:
        raise ValueError(f"Unknown plan {plan!r} — expected one of {sorted(config.API_KEY_PLANS)}")

    raw_key = f"cvk_{secrets.token_urlsafe(32)}"
    key_id = _hash_key(raw_key)

    client = get_firestore_client()
    client.collection(_COLLECTION).document(key_id).set({
        "uid": uid,
        "label": label,
        "plan": plan,
        "createdAt": admin_firestore.SERVER_TIMESTAMP,
        "revoked": False,
        "revokedAt": None,
        "lastUsedAt": None,
    })
    return raw_key, key_id


def resolve_api_key(raw_key: str) -> ApiKeyRecord | None:
    """Retourne l'ApiKeyRecord pour une clé valide et non révoquée, ou
    None. Lève FirestoreUnavailableError si Firestore lui-même n'est pas
    joignable — c'est un problème de config serveur, pas une "clé
    invalide", donc l'appelant doit remonter ça en 503 plutôt qu'en 401."""
    if not raw_key:
        return None

    client = get_firestore_client()
    doc = client.collection(_COLLECTION).document(_hash_key(raw_key)).get()
    if not doc.exists:
        return None

    data = doc.to_dict()
    if data.get("revoked"):
        return None

    uid = data.get("uid")
    if not uid:
        return None

    try:
        doc.reference.update({"lastUsedAt": admin_firestore.SERVER_TIMESTAMP})
    except Exception as exc:  # noqa: BLE001 - suivi au mieux, sans garantie
        print(f"Could not update lastUsedAt for api key {doc.id}: {exc}")

    plan = data.get("plan") or config.DEFAULT_API_KEY_PLAN
    return ApiKeyRecord(key_id=doc.id, uid=uid, plan=plan)


def list_api_keys_for_uid(uid: str) -> list[dict]:
    """Retourne les métadonnées (jamais la clé brute, qui n'a jamais été
    stockée) de chaque clé émise pour `uid`, pour affichage sur une page
    compte/clés."""
    client = get_firestore_client()
    query = client.collection(_COLLECTION).where(filter=FieldFilter("uid", "==", uid))
    keys = []
    for doc in query.stream():
        data = doc.to_dict()
        keys.append({
            "key_id": doc.id,
            "label": data.get("label"),
            "plan": data.get("plan") or config.DEFAULT_API_KEY_PLAN,
            "createdAt": data.get("createdAt"),
            "lastUsedAt": data.get("lastUsedAt"),
            "revoked": bool(data.get("revoked")),
        })
    return keys


def resolve_emails(uids: list[str]) -> dict[str, str | None]:
    """Résout par lot les e-mails Firebase Auth pour `uids` — découpé au
    plafond documenté de 100 identifiants par appel de get_users(), donc
    ça reste une poignée d'allers-retours même pour une page complète, pas
    une recherche par uid. Un uid que Firebase Auth ne reconnaît pas (ex.
    un compte supprimé) ou un pépin Firestore/Auth signifie simplement que
    cet uid est absent du dict retourné — les appelants doivent utiliser
    .get(uid), pas [uid]."""
    unique_uids = list(dict.fromkeys(u for u in uids if u))
    emails: dict[str, str | None] = {}

    for i in range(0, len(unique_uids), _GET_USERS_MAX_BATCH):
        chunk = unique_uids[i:i + _GET_USERS_MAX_BATCH]
        identifiers = [firebase_auth.UidIdentifier(uid) for uid in chunk]
        try:
            result = firebase_auth.get_users(identifiers)
        except Exception as exc:  # noqa: BLE001 - l'e-mail est un plus, jamais une raison de faire échouer la liste
            print(f"Batch email resolution failed for {len(chunk)} uid(s): {exc}")
            continue
        for user in result.users:
            emails[user.uid] = user.email

    return emails


def list_all_api_keys(limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Retourne (keys, next_cursor) : jusqu'à `limit` clés de *tous* les
    utilisateurs, les plus récentes d'abord, chacune annotée avec l'e-mail
    de son propriétaire (via une recherche Firebase Auth par lot — voir
    resolve_emails) — pour un tableau de bord admin "toutes les clés", par
    opposition à la vue self-service mono-utilisateur de
    list_api_keys_for_uid.

    Renvoie le `next_cursor` retourné en `cursor` pour récupérer la page
    suivante ; `next_cursor` vaut None une fois qu'il n'y a plus de page."""
    client = get_firestore_client()
    query = (
        client.collection(_COLLECTION)
        .order_by("createdAt", direction=admin_firestore.Query.DESCENDING)
        .limit(limit)
    )

    if cursor:
        cursor_doc = client.collection(_COLLECTION).document(cursor).get()
        if cursor_doc.exists:
            query = query.start_after(cursor_doc)

    docs = list(query.stream())
    rows = [(doc.id, doc.to_dict()) for doc in docs]
    emails = resolve_emails([data.get("uid") for _, data in rows])

    keys = [
        {
            "key_id": key_id,
            "uid": data.get("uid"),
            "email": emails.get(data.get("uid")),
            "label": data.get("label"),
            "plan": data.get("plan") or config.DEFAULT_API_KEY_PLAN,
            "createdAt": data.get("createdAt"),
            "lastUsedAt": data.get("lastUsedAt"),
            "revoked": bool(data.get("revoked")),
        }
        for key_id, data in rows
    ]
    next_cursor = docs[-1].id if len(docs) == limit else None
    return keys, next_cursor


def list_all_key_records() -> list[dict]:
    """(key_id, uid, plan, revoked) de chaque clé — sans pagination, sans
    résolution d'e-mail. Pour les tâches par lot internes (ex. le rapport
    mensuel d'usage API — voir api_usage_report.py) qui ont besoin de
    parcourir toutes les clés, par opposition à la page paginée du
    tableau de bord admin de list_all_api_keys."""
    client = get_firestore_client()
    return [
        {
            "key_id": doc.id,
            "uid": doc.to_dict().get("uid"),
            "plan": doc.to_dict().get("plan") or config.DEFAULT_API_KEY_PLAN,
            "revoked": bool(doc.to_dict().get("revoked")),
        }
        for doc in client.collection(_COLLECTION).stream()
    ]


def get_api_key_for_owner(key_id: str, owner_uid: str) -> dict | None:
    """Récupère les métadonnées d'une clé, restreint à son propriétaire —
    retourne None si la clé n'existe pas ou appartient à quelqu'un d'autre
    (ne fait jamais la distinction entre les deux, même raisonnement que
    la vérification owner_uid de revoke_api_key)."""
    client = get_firestore_client()
    doc = client.collection(_COLLECTION).document(key_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if data.get("uid") != owner_uid:
        return None
    return {
        "key_id": doc.id,
        "label": data.get("label"),
        "plan": data.get("plan") or config.DEFAULT_API_KEY_PLAN,
        "revoked": bool(data.get("revoked")),
    }


def revoke_api_key(key_id: str, owner_uid: str | None = None) -> bool:
    """Marque une clé comme révoquée via son key_id (le hash retourné par
    create_api_key/list_api_keys_for_uid). Retourne False si aucune telle
    clé n'existe.

    `owner_uid`, quand il est fourni, restreint ceci à la révocation en
    self-service : la clé doit appartenir à cet uid, sinon ça retourne
    False (introuvable) plutôt que de la révoquer — délibérément
    indiscernable de "cette clé n'existe pas" pour qu'un utilisateur ne
    puisse pas s'en servir pour sonder quels key_id existent chez d'autres
    comptes. Les appelants admin passent owner_uid=None pour révoquer
    n'importe quelle clé."""
    client = get_firestore_client()
    ref = client.collection(_COLLECTION).document(key_id)
    snapshot = ref.get()
    if not snapshot.exists:
        return False
    if owner_uid is not None and snapshot.to_dict().get("uid") != owner_uid:
        return False
    ref.update({"revoked": True, "revokedAt": admin_firestore.SERVER_TIMESTAMP})
    return True


def set_api_key_plan(key_id: str, plan: str) -> bool:
    """Réservé aux admins : change l'offre d'une clé (ex. après une
    négociation Enterprise manuelle, ou un changement d'offre géré par le
    support). Retourne False si aucune telle clé n'existe. Lève ValueError
    pour une offre inconnue."""
    if plan not in config.API_KEY_PLANS:
        raise ValueError(f"Unknown plan {plan!r} — expected one of {sorted(config.API_KEY_PLANS)}")

    client = get_firestore_client()
    ref = client.collection(_COLLECTION).document(key_id)
    if not ref.get().exists:
        return False
    ref.update({"plan": plan})
    return True
