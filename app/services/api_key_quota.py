"""Quota par clé API, par mois calendaire, par fonctionnalité — distinct
des compteurs à vie usage/{uid} du site (quota.py, utilisé par le chemin
session Firebase), puisque l'usage par clé API est facturé selon une offre
avec sa propre allocation mensuelle qui doit se réinitialiser. Réutilise
les mêmes primitives de transaction reserve/commit/release de quota.py ;
seuls l'identité du document Firestore et la valeur d'allocation diffèrent.
"""
from datetime import datetime, timezone

from app import config
from app.services.quota import read_feature_usage_fields

_COLLECTION = "apiKeyUsage"

ALL_FEATURE_KEYS = (
    config.FEATURE_KEY_NOISE_FILTER,
    config.FEATURE_KEY_IMITATION,
    config.FEATURE_KEY_DEEPFAKE,
    config.FEATURE_KEY_FRAME_RECOVERY,
)


def current_period() -> str:
    """Mois calendaire en UTC, ex. "2026-08" — l'usage d'une clé revient à
    0 quand ça bascule, simplement parce qu'un nouvel id de document
    démarre vide."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage_doc_ref(firestore_client, key_id: str, period: str | None = None):
    period = period or current_period()
    return firestore_client.collection(_COLLECTION).document(f"{key_id}:{period}")


def plan_file_limit(plan: str) -> int | None:
    """None signifie illimité (Enterprise, tant qu'il n'existe pas de vraie
    limite personnalisée par clé — voir le TODO sur API_KEY_PLANS)."""
    plan_config = config.API_KEY_PLANS.get(plan, config.API_KEY_PLANS[config.DEFAULT_API_KEY_PLAN])
    return plan_config["files_per_feature_per_month"]


def plan_rate_limit(plan: str) -> tuple[int, int]:
    """Retourne (max_requests, window_seconds) pour `plan`."""
    plan_config = config.API_KEY_PLANS.get(plan, config.API_KEY_PLANS[config.DEFAULT_API_KEY_PLAN])
    return plan_config["rate_limit_max_requests"], plan_config["rate_limit_window_seconds"]


def read_usage_for_key(firestore_client, key_id: str, plan: str) -> dict:
    """Retourne l'usage de cette clé pour la période de facturation en
    cours, pour affichage sur un tableau de bord d'usage : {"period":
    "2026-08", "features": {feature_key: {"filesUsed": int,
    "limit": int|None}}}."""
    period = current_period()
    snapshot = usage_doc_ref(firestore_client, key_id, period).get()
    limit = plan_file_limit(plan)
    return {
        "period": period,
        "features": {
            feature_key: {
                "filesUsed": read_feature_usage_fields(snapshot, feature_key)[0],
                "limit": limit,
            }
            for feature_key in ALL_FEATURE_KEYS
        },
    }
