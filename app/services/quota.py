"""Quota de fichiers par fonctionnalité (réserver / valider / libérer),
porté 1:1 depuis api_server.py. Chaque fonctionnalité (noiseFilter,
imitation, deepfake, frameRecovery) a sa propre allocation
MAX_FILES_PER_FEATURE indépendante, suivie sous usage/{uid}.{feature_key}.

NOTE sur la concurrence : chaque fonction publique ci-dessous enveloppe sa
fonction "_impl" avec `firestore.transactional(...)` à neuf, à chaque
appel, plutôt que de décorer l'impl au moment de l'import du module. Le
décorateur `@transactional` de google-cloud-firestore retourne un objet
`Transactional` qui stocke le suivi retry/rollback (current_id, retry_id)
comme attributs *d'instance*. Si ce même objet décoré est partagé et
invoqué en parallèle par plusieurs requêtes en vol (ce qui arrive sous
FastAPI, où la fonction décorée est un seul objet au niveau module), la
logique de retry/reset d'une requête peut écraser l'état en cours de
transaction d'une autre. C'est ce qui a produit :

    "The transaction has no transaction ID, so it cannot be rolled back."

— un rollback tenté sur une transaction dont le _begin() n'a en réalité
jamais abouti pour cet appel, parce qu'un appel concurrent avait déjà
réinitialisé l'état du wrapper partagé. Envelopper à neuf à chaque appel
donne à chaque invocation sa propre instance Transactional, donc il n'y a
pas d'état mutable partagé sur lequel il pourrait y avoir une course.
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
    """`max_files` est de la responsabilité de l'appelant, qui doit le
    fournir explicitement — le chemin session Firebase du site passe
    config.MAX_FILES_PER_FEATURE (un plafond fixe à vie), tandis que le
    chemin par clé API (api_key_quota.py) passe l'allocation de l'offre de
    cette clé pour le mois calendaire en cours, ou None pour une offre
    illimitée."""
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
    """Libération au mieux, sans garantie, qui ne lève jamais — reflète
    NoiseFilterHandler._release_quota_safely."""
    if not (firestore_client and usage_ref):
        return
    try:
        release_reserved_file(firestore_client.transaction(), usage_ref, feature_key)
    except Exception as release_exc:  # noqa: BLE001
        print(f"Failed to release reserved file slot after {context}: {release_exc}")
