"""Shared FastAPI dependencies: Firebase auth + per-uid rate limiting.
Replaces the repeated _authenticate_request / check_rate_limit calls at the
top of every handler in api_server.py with a single reusable dependency.
"""
import threading
import time

from fastapi import Header, HTTPException, Request

from app import config
from app.services.firebase import verify_firebase_id_token

_rate_limit_lock = threading.Lock()
_recent_requests_by_uid = {}  # uid -> list[timestamp]


class RateLimitExceededError(Exception):
    pass


def check_rate_limit(uid: str):
    """Raises RateLimitExceededError if `uid` has made too many submissions
    in the current rolling window. Thread-safe: FastAPI/Starlette may run
    sync dependencies in a thread pool, same as ThreadingHTTPServer did."""
    now = time.monotonic()
    cutoff = now - config.RATE_LIMIT_WINDOW_SECONDS

    with _rate_limit_lock:
        timestamps = [t for t in _recent_requests_by_uid.get(uid, []) if t > cutoff]
        if len(timestamps) >= config.RATE_LIMIT_MAX_REQUESTS:
            _recent_requests_by_uid[uid] = timestamps
            raise RateLimitExceededError(
                f"Too many requests — max {config.RATE_LIMIT_MAX_REQUESTS} per "
                f"{config.RATE_LIMIT_WINDOW_SECONDS}s. Try again shortly."
            )
        timestamps.append(now)
        _recent_requests_by_uid[uid] = timestamps


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


async def get_current_uid_rate_limited(authorization: str = Header(default="")) -> str:
    """Combines auth + rate limiting into one dependency, mirroring
    NoiseFilterHandler._authenticate_and_rate_limit(). Use this as the
    dependency on every feature-processing route."""
    decoded_token = _verify_bearer_token(authorization)
    uid = decoded_token.get("uid")

    try:
        check_rate_limit(uid)
    except RateLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    return uid


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
