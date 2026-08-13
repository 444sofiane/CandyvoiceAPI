"""Shared FastAPI dependencies: Firebase auth + API-key auth + rate
limiting. Replaces the repeated _authenticate_request / check_rate_limit
calls at the top of every handler in api_server.py with a single reusable
dependency.
"""
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from app import config
from app.services.api_key_quota import plan_rate_limit
from app.services.api_keys import resolve_api_key
from app.services.firebase import FirestoreUnavailableError, verify_firebase_id_token


@dataclass
class AuthContext:
    uid: str
    auth_method: str  # "firebase" | "api_key"
    key_id: str | None = None
    plan: str | None = None


_rate_limit_lock = threading.Lock()
_recent_requests_by_bucket = {}  # bucket key -> list[timestamp]


class RateLimitExceededError(Exception):
    pass


def check_rate_limit(bucket_key: str, max_requests: int | None = None, window_seconds: int | None = None):
    """Raises RateLimitExceededError if `bucket_key` has made too many
    submissions in the current rolling window. `bucket_key` is a plain uid
    for the Firebase-session path, or "apikey:<key_id>" for API-key
    callers — each key gets its own independent budget sized to its plan,
    rather than sharing one bucket with that user's browser session (which
    might be on a different limit entirely). Thread-safe: FastAPI/Starlette
    may run sync dependencies in a thread pool, same as
    ThreadingHTTPServer did."""
    max_requests = config.RATE_LIMIT_MAX_REQUESTS if max_requests is None else max_requests
    window_seconds = config.RATE_LIMIT_WINDOW_SECONDS if window_seconds is None else window_seconds

    now = time.monotonic()
    cutoff = now - window_seconds

    with _rate_limit_lock:
        timestamps = [t for t in _recent_requests_by_bucket.get(bucket_key, []) if t > cutoff]
        if len(timestamps) >= max_requests:
            _recent_requests_by_bucket[bucket_key] = timestamps
            raise RateLimitExceededError(
                f"Too many requests — max {max_requests} per {window_seconds}s. Try again shortly."
            )
        timestamps.append(now)
        _recent_requests_by_bucket[bucket_key] = timestamps


def _verify_bearer_token(authorization: str) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        decoded_token = verify_firebase_id_token(token)
    except ValueError as exc:
        message = str(exc).lower()
        if "expired" in message:
            print("Firebase ID token verification failed: token expired")
            raise HTTPException(status_code=401, detail="Session expired, please sign in again")
        print(f"Firebase ID token verification failed: invalid token ({exc})")
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface unexpected verification errors as 401s, not 500s
        print(f"Firebase ID token verification failed: {exc}")
        raise HTTPException(status_code=401, detail="Could not verify authentication token")

    if not isinstance(decoded_token, dict):
        print("Firebase ID token verification failed: unexpected decoded token payload")
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    return decoded_token


async def get_current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency: verifies the Firebase ID token on the
    Authorization header and returns the decoded token dict. Raises
    HTTPException(401) on any failure — FastAPI turns that into the JSON
    error response automatically."""
    return _verify_bearer_token(authorization)


async def get_current_auth(
    authorization: str = Header(default=""),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthContext:
    """Accepts either an API key (X-API-Key header — for server-to-server
    callers with their own application, issued via /api/keys) or a Firebase
    ID token (Authorization: Bearer ...). The API key is tried first when
    present, since a caller using one wouldn't normally send the other —
    but an invalid/revoked key doesn't shadow an otherwise-valid bearer
    token if one was also sent (e.g. a stale key left over from testing
    alongside a real browser session): only 401 on the key specifically
    when there's no bearer token to fall back to."""
    if x_api_key:
        try:
            record = resolve_api_key(x_api_key)
        except FirestoreUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if record:
            return AuthContext(uid=record.uid, auth_method="api_key", key_id=record.key_id, plan=record.plan)
        if not authorization:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    decoded_token = _verify_bearer_token(authorization)
    return AuthContext(uid=decoded_token.get("uid"), auth_method="firebase")


async def get_current_uid_rate_limited(auth: AuthContext = Depends(get_current_auth)) -> AuthContext:
    """Combines auth + rate limiting into one dependency, mirroring
    NoiseFilterHandler._authenticate_and_rate_limit(). Use this as the
    dependency on every feature-processing route. API-key callers are
    rate-limited per key, at their plan's budget; Firebase-session callers
    keep the flat global budget, per uid."""
    if auth.auth_method == "api_key":
        max_requests, window_seconds = plan_rate_limit(auth.plan)
        bucket_key = f"apikey:{auth.key_id}"
    else:
        max_requests, window_seconds = None, None
        bucket_key = auth.uid

    try:
        check_rate_limit(bucket_key, max_requests, window_seconds)
    except RateLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    return auth


async def verify_websocket_token(token: str) -> dict:
    """Same token verification as get_current_user, but for use inside a
    WebSocket handler after receiving the token as the first message
    (WebSocket handshakes can't reliably carry custom Authorization headers
    from a browser)."""
    if not token:
        raise ValueError("Missing auth token")
    try:
        decoded_token = verify_firebase_id_token(token)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid authentication token: {exc}")
    if not isinstance(decoded_token, dict):
        raise ValueError("Invalid authentication token")
    return decoded_token
