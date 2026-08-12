"""API-key issuance/verification, for server-to-server callers who bring
their own application instead of going through the Firebase-authenticated
web UI. A key's raw value is only ever shown once, at creation time — only
its SHA-256 hash is persisted (as the Firestore document id, for an O(1)
lookup on every request), mirroring how a password would be stored.
"""
import hashlib
import secrets
from dataclasses import dataclass

from firebase_admin import firestore as admin_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app import config
from app.services.firebase import get_firestore_client

_COLLECTION = "apiKeys"


@dataclass
class ApiKeyRecord:
    key_id: str
    uid: str
    plan: str


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(uid: str, label: str | None = None, plan: str = config.DEFAULT_API_KEY_PLAN) -> tuple[str, str]:
    """Generates a new key for `uid` on `plan`, stores its hash, and
    returns (raw_key, key_id). `raw_key` is never persisted — capture it
    now, it cannot be recovered later.

    Raises ValueError if `plan` isn't a known tier — callers should turn
    that into a 400, not let it become an opaque 500."""
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
    """Returns the ApiKeyRecord for a valid, non-revoked key, or None.
    Raises FirestoreUnavailableError if Firestore itself isn't reachable —
    that's a server config problem, not "invalid key", so the caller should
    surface it as a 503 rather than a 401."""
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
    except Exception as exc:  # noqa: BLE001 - best-effort bookkeeping only
        print(f"Could not update lastUsedAt for api key {doc.id}: {exc}")

    plan = data.get("plan") or config.DEFAULT_API_KEY_PLAN
    return ApiKeyRecord(key_id=doc.id, uid=uid, plan=plan)


def list_api_keys_for_uid(uid: str) -> list[dict]:
    """Returns metadata (never the raw key, which was never stored) for
    every key issued to `uid`, for display on an account/keys page."""
    client = get_firestore_client()
    query = client.collection(_COLLECTION).where(filter=FieldFilter("uid", "==", uid))
    return [
        {
            "key_id": doc.id,
            "label": doc.to_dict().get("label"),
            "plan": doc.to_dict().get("plan") or config.DEFAULT_API_KEY_PLAN,
            "createdAt": doc.to_dict().get("createdAt"),
            "lastUsedAt": doc.to_dict().get("lastUsedAt"),
            "revoked": bool(doc.to_dict().get("revoked")),
        }
        for doc in query.stream()
    ]


def list_all_api_keys(limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Returns (keys, next_cursor): up to `limit` keys across *all* users,
    newest first — for an admin "every key" dashboard, as opposed to
    list_api_keys_for_uid's self-service, single-user view. Each row is
    the bare `uid`, not an email — resolving that to something
    human-readable is left to the caller (e.g. only for the rows
    currently visible), since doing it here would mean one Firebase Auth
    lookup per key and get slow as the list grows.

    Pass the returned `next_cursor` back as `cursor` to fetch the next
    page; `next_cursor` is None once there are no more pages."""
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
    keys = [
        {
            "key_id": doc.id,
            "uid": doc.to_dict().get("uid"),
            "label": doc.to_dict().get("label"),
            "plan": doc.to_dict().get("plan") or config.DEFAULT_API_KEY_PLAN,
            "createdAt": doc.to_dict().get("createdAt"),
            "lastUsedAt": doc.to_dict().get("lastUsedAt"),
            "revoked": bool(doc.to_dict().get("revoked")),
        }
        for doc in docs
    ]
    next_cursor = docs[-1].id if len(docs) == limit else None
    return keys, next_cursor


def get_api_key_for_owner(key_id: str, owner_uid: str) -> dict | None:
    """Fetches one key's metadata, scoped to its owner — returns None if
    the key doesn't exist or belongs to someone else (never distinguishes
    the two, same reasoning as revoke_api_key's owner_uid check)."""
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
    """Marks a key revoked by its key_id (the hash returned from
    create_api_key/list_api_keys_for_uid). Returns False if no such key
    exists.

    `owner_uid`, when given, scopes this to self-service revocation: the
    key must belong to that uid or this returns False (not found) rather
    than revoking it — deliberately indistinguishable from "no such key" so
    a user can't use this to probe which key_ids exist for other
    accounts. Admin callers pass owner_uid=None to revoke any key."""
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
    """Admin-only: changes a key's plan (e.g. after a manual Enterprise
    negotiation, or a support-handled upgrade/downgrade). Returns False if
    no such key exists. Raises ValueError for an unknown plan."""
    if plan not in config.API_KEY_PLANS:
        raise ValueError(f"Unknown plan {plan!r} — expected one of {sorted(config.API_KEY_PLANS)}")

    client = get_firestore_client()
    ref = client.collection(_COLLECTION).document(key_id)
    if not ref.get().exists:
        return False
    ref.update({"plan": plan})
    return True
