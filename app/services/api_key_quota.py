"""Per-API-key, per-calendar-month, per-feature quota — separate from the
website's lifetime usage/{uid} counters (quota.py, used by the
Firebase-session path), since API-key usage is billed against a plan with
its own monthly allowance that has to reset. Reuses the same
reserve/commit/release transaction primitives from quota.py; only the
Firestore document identity and the allowance value differ.
"""
from datetime import datetime, timezone

from app import config
from app.services.quota import read_feature_usage_fields

_COLLECTION = "apiKeyUsage"

_ALL_FEATURE_KEYS = (
    config.FEATURE_KEY_NOISE_FILTER,
    config.FEATURE_KEY_IMITATION,
    config.FEATURE_KEY_DEEPFAKE,
    config.FEATURE_KEY_FRAME_RECOVERY,
)


def current_period() -> str:
    """Calendar month in UTC, e.g. "2026-08" — a key's usage resets to 0
    when this rolls over, simply because a new document id starts empty."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage_doc_ref(firestore_client, key_id: str, period: str | None = None):
    period = period or current_period()
    return firestore_client.collection(_COLLECTION).document(f"{key_id}:{period}")


def plan_file_limit(plan: str) -> int | None:
    """None means unlimited (Enterprise, until a real per-key custom limit
    exists — see the TODO on API_KEY_PLANS)."""
    plan_config = config.API_KEY_PLANS.get(plan, config.API_KEY_PLANS[config.DEFAULT_API_KEY_PLAN])
    return plan_config["files_per_feature_per_month"]


def plan_rate_limit(plan: str) -> tuple[int, int]:
    """Returns (max_requests, window_seconds) for `plan`."""
    plan_config = config.API_KEY_PLANS.get(plan, config.API_KEY_PLANS[config.DEFAULT_API_KEY_PLAN])
    return plan_config["rate_limit_max_requests"], plan_config["rate_limit_window_seconds"]


def read_usage_for_key(firestore_client, key_id: str, plan: str) -> dict:
    """Returns this key's usage for the current billing period, for display
    on a usage dashboard: {"period": "2026-08", "features": {feature_key:
    {"filesUsed": int, "limit": int|None}}}."""
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
            for feature_key in _ALL_FEATURE_KEYS
        },
    }
