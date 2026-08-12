"""Per-feature file quota (reserve / commit / release), ported 1:1 from
api_server.py. Each feature (noiseFilter, imitation, deepfake,
frameRecovery) has its own independent MAX_FILES_PER_FEATURE allowance
tracked under usage/{uid}.{feature_key}.

NOTE on concurrency: each public function below wraps its "_impl" function
with `firestore.transactional(...)` freshly, on every call, instead of
decorating the impl at module import time. The google-cloud-firestore
`@transactional` decorator returns a `Transactional` object that stores
retry/rollback bookkeeping (current_id, retry_id) as *instance* attributes.
If that same decorated object is shared and invoked concurrently by
multiple in-flight requests (as happens under FastAPI, where the decorated
function is a single module-level object), one request's retry/reset logic
can clobber another's mid-transaction state. That's what produced:

    "The transaction has no transaction ID, so it cannot be rolled back."

— a rollback attempted on a transaction whose _begin() never actually
completed for that call, because a concurrent call reset the shared
wrapper's state first. Wrapping fresh per-call gives each invocation its
own Transactional instance, so there's no shared mutable state to race on.
"""
from google.cloud import firestore

from app import config


class QuotaExceededError(Exception):
    pass


def read_feature_usage_fields(snapshot, feature_key):
    data = snapshot.to_dict() if snapshot.exists else {}
    feature_data = data.get(feature_key) or {}
    files_used = int(feature_data.get("filesUsed", 0) or 0)
    files_reserved = int(feature_data.get("filesReserved", 0) or 0)
    return files_used, files_reserved


def _reserve_usage_file_impl(transaction, usage_ref, feature_key, max_files):
    snapshot = usage_ref.get(transaction=transaction)
    files_used, files_reserved = read_feature_usage_fields(snapshot, feature_key)

    if max_files is not None and files_used + files_reserved >= max_files:
        raise QuotaExceededError(
            f"You've used all {max_files} files allowed for this feature."
        )

    new_reserved = files_reserved + 1
    transaction.set(
        usage_ref,
        {feature_key: {"filesUsed": files_used, "filesReserved": new_reserved}},
        merge=True,
    )
    return {"filesUsed": files_used, "filesReserved": new_reserved}


def reserve_usage_file(transaction, usage_ref, feature_key, max_files):
    """`max_files` is the caller's responsibility to supply explicitly —
    the website's Firebase-session path passes config.MAX_FILES_PER_FEATURE
    (a flat lifetime cap), while the API-key path (api_key_quota.py) passes
    that key's plan allowance for the current calendar month, or None for
    an unlimited plan."""
    return firestore.transactional(_reserve_usage_file_impl)(transaction, usage_ref, feature_key, max_files)


def _release_reserved_file_impl(transaction, usage_ref, feature_key):
    snapshot = usage_ref.get(transaction=transaction)
    files_used, files_reserved = read_feature_usage_fields(snapshot, feature_key)
    transaction.set(
        usage_ref,
        {feature_key: {"filesUsed": files_used, "filesReserved": max(0, files_reserved - 1)}},
        merge=True,
    )


def release_reserved_file(transaction, usage_ref, feature_key):
    return firestore.transactional(_release_reserved_file_impl)(transaction, usage_ref, feature_key)


def _commit_reserved_file_impl(transaction, usage_ref, feature_key):
    snapshot = usage_ref.get(transaction=transaction)
    files_used, files_reserved = read_feature_usage_fields(snapshot, feature_key)

    if files_reserved < 1:
        raise RuntimeError("Reserved file slot was not available to commit.")

    new_used = files_used + 1
    transaction.set(
        usage_ref,
        {feature_key: {"filesUsed": new_used, "filesReserved": max(0, files_reserved - 1)}},
        merge=True,
    )
    return new_used


def commit_reserved_file(transaction, usage_ref, feature_key):
    return firestore.transactional(_commit_reserved_file_impl)(transaction, usage_ref, feature_key)


def release_quota_safely(firestore_client, usage_ref, feature_key, context):
    """Best-effort release that never raises — mirrors
    NoiseFilterHandler._release_quota_safely."""
    if not (firestore_client and usage_ref):
        return
    try:
        release_reserved_file(firestore_client.transaction(), usage_ref, feature_key)
    except Exception as release_exc:  # noqa: BLE001
        print(f"Failed to release reserved file slot after {context}: {release_exc}")
