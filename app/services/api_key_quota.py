"""Quota par clé API, par fonctionnalité — distinct des compteurs à vie
usage/{uid} du site (quota.py, utilisé par le chemin session Firebase).

L'usage par clé API n'est pas compté au fichier mais au budget de
secondes d'une "session" — un appel `/ws/deepfake` en direct (le budget se
cumule sur tous les chunks envoyés pendant cette connexion — c'est la
seule fonctionnalité multi-chunks), ou un seul clip pour les trois autres
endpoints en one-shot (noiseFilter, imitation, frameRecovery, ainsi que
`/api/deepfake-detect`). Ce budget est vérifié en temps réel par
process_flow.py/deepfake_prepare.py avant même que le traitement ne
démarre ; il n'y a rien à réserver dans Firestore au préalable comme pour
le chemin Firebase, une session étant éphémère.

Le compteur `secondsUsed` stocké ici par log_session_usage() sert
uniquement au reporting mensuel (tableau de bord d'usage, rapport admin)
— il n'est jamais relu pour bloquer une requête.
"""
from datetime import datetime, timezone

from firebase_admin import firestore as admin_firestore

from app import config

_COLLECTION = "apiKeyUsage"

ALL_FEATURE_KEYS = (
    config.FEATURE_KEY_NOISE_FILTER,
    config.FEATURE_KEY_IMITATION,
    config.FEATURE_KEY_DEEPFAKE,
    config.FEATURE_KEY_FRAME_RECOVERY,
)


def current_period() -> str:
    """Mois calendaire en UTC, ex. "2026-08" — l'usage affiché d'une clé
    revient à 0 quand ça bascule, simplement parce qu'un nouvel id de
    document démarre vide. N'affecte jamais l'enforcement, purement le
    reporting — voir le docstring du module."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage_doc_ref(firestore_client, key_id: str, period: str | None = None):
    period = period or current_period()
    return firestore_client.collection(_COLLECTION).document(f"{key_id}:{period}")


def plan_session_seconds(plan: str) -> int:
    """Budget de secondes d'audio traité par session et par
    fonctionnalité (chaque fonctionnalité a sa propre allocation
    indépendante, comme le fait aujourd'hui MAX_FILES_PER_FEATURE pour le
    chemin Firebase). Vérifié en temps réel avant traitement, pas via une
    réservation Firestore : une session est éphémère, il n'y a rien à
    réserver au-delà de "est-ce que ce clip/chunk dépasse ce qu'il reste
    au budget de cette session"."""
    plan_config = config.API_KEY_PLANS.get(plan, config.API_KEY_PLANS[config.DEFAULT_API_KEY_PLAN])
    return plan_config["max_session_seconds"]


def plan_rate_limit(plan: str) -> tuple[int, int]:
    """Retourne (max_requests, window_seconds) pour `plan`."""
    plan_config = config.API_KEY_PLANS.get(plan, config.API_KEY_PLANS[config.DEFAULT_API_KEY_PLAN])
    return plan_config["rate_limit_max_requests"], plan_config["rate_limit_window_seconds"]


def log_session_usage(firestore_client, key_id: str, feature_key: str, seconds_used: float, period: str | None = None) -> None:
    """Ajoute `seconds_used` au total cumulé de `feature_key` pour cette
    clé sur la période de facturation en cours — purement pour le
    reporting (tableau de bord d'usage, rapport admin mensuel), jamais
    pour bloquer une requête (ça, c'est le rôle du budget par session,
    déjà vérifié en amont avant que le traitement ne démarre). Au mieux,
    sans garantie — ne lève jamais, un pépin ici ne doit pas faire échouer
    un traitement par ailleurs réussi."""
    try:
        usage_doc_ref(firestore_client, key_id, period).set(
            {feature_key: {"secondsUsed": admin_firestore.Increment(seconds_used)}},
            merge=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to log session usage for key {key_id}/{feature_key}: {exc}")


def read_usage_for_key(firestore_client, key_id: str, plan: str) -> dict:
    """Retourne l'usage de cette clé pour la période de facturation en
    cours, pour affichage sur un tableau de bord d'usage :
    {"period": "2026-08", "features": {feature_key: {"secondsUsed": int,
    "maxSessionSeconds": int}}}.

    `secondsUsed` est un cumul purement indicatif (voir le docstring du
    module) — `maxSessionSeconds` est le budget appliqué à chaque
    session, pas un plafond mensuel."""
    period = current_period()
    snapshot = usage_doc_ref(firestore_client, key_id, period).get()
    data = snapshot.to_dict() if snapshot.exists else {}
    max_session_seconds = plan_session_seconds(plan)

    return {
        "period": period,
        "features": {
            feature_key: {
                "secondsUsed": (data.get(feature_key) or {}).get("secondsUsed", 0),
                "maxSessionSeconds": max_session_seconds,
            }
            for feature_key in ALL_FEATURE_KEYS
        },
    }
