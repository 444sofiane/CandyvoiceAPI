"""API-key issuance/verification, for server-to-server callers who bring
their own application instead of going through the Firebase-authenticated
web UI. A key's raw value is only ever shown once, at creation time — only
its SHA-256 hash is persisted (as the Firestore document id, for an O(1)
lookup on every request), mirroring how a password would be stored.
"""
import hashlib
import secrets

from firebase_admin import firestore as admin_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.services.firebase import get_firestore_client

_COLLECTION = "apiKeys"


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(uid: str, label: str | None = None) -> tuple[str, str]:
    """Generates a new key for `uid`, stores its hash, and returns
    (raw_key, key_id). `raw_key` is never persisted — capture it now, it
    cannot be recovered later."""
    raw_key = f"cvk_{secrets.token_urlsafe(32)}"
    key_id = _hash_key(raw_key)

    client = get_firestore_client()
    client.collection(_COLLECTION).document(key_id).set({
        "uid": uid,
        "label": label,
        "createdAt": admin_firestore.SERVER_TIMESTAMP,
        "revoked": False,
        "revokedAt": None,
        "lastUsedAt": None,
    })
    return raw_key, key_id


def resolve_api_key(raw_key: str) -> str | None:
    """Returns the owning uid for a valid, non-revoked key, or None.
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

    return uid


def list_api_keys_for_uid(uid: str) -> list[dict]:
    """Returns metadata (never the raw key, which was never stored) for
    every key issued to `uid`, for display in an admin tool."""
    client = get_firestore_client()
    query = client.collection(_COLLECTION).where(filter=FieldFilter("uid", "==", uid))
    return [
        {
            "key_id": doc.id,
            "label": doc.to_dict().get("label"),
            "createdAt": doc.to_dict().get("createdAt"),
            "lastUsedAt": doc.to_dict().get("lastUsedAt"),
            "revoked": bool(doc.to_dict().get("revoked")),
        }
        for doc in query.stream()
    ]


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
