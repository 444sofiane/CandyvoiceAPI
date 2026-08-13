"""Dépendances FastAPI partagées : auth Firebase + auth par clé API +
limitation de débit. Remplace les appels répétés à _authenticate_request
/ check_rate_limit en tête de chaque handler dans api_server.py par une
seule dépendance réutilisable.
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
_recent_requests_by_bucket = {}  # clé de bucket -> list[timestamp]


class RateLimitExceededError(Exception):
    pass


def check_rate_limit(bucket_key: str, max_requests: int | None = None, window_seconds: int | None = None):
    """Lève RateLimitExceededError si `bucket_key` a fait trop de
    soumissions dans la fenêtre glissante actuelle. `bucket_key` est un
    simple uid pour le chemin session Firebase, ou "apikey:<key_id>" pour
    les appelants par clé API — chaque clé a son propre budget
    indépendant, dimensionné selon son offre, plutôt que de partager un
    bucket avec la session navigateur de cet utilisateur (qui pourrait
    être sur une limite complètement différente). Thread-safe :
    FastAPI/Starlette peut exécuter des dépendances synchrones dans un
    thread pool, comme le faisait ThreadingHTTPServer."""
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
    except Exception as exc:  # noqa: BLE001 - laisse remonter les erreurs de vérification inattendues en 401, pas en 500
        print(f"Firebase ID token verification failed: {exc}")
        raise HTTPException(status_code=401, detail="Could not verify authentication token")

    if not isinstance(decoded_token, dict):
        print("Firebase ID token verification failed: unexpected decoded token payload")
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    return decoded_token


async def get_current_user(authorization: str = Header(default="")) -> dict:
    """Dépendance FastAPI : vérifie le token d'ID Firebase dans l'en-tête
    Authorization et retourne le dict du token décodé. Lève une
    HTTPException(401) en cas d'échec — FastAPI la transforme
    automatiquement en réponse JSON d'erreur."""
    return _verify_bearer_token(authorization)


async def get_current_auth(
    authorization: str = Header(default=""),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthContext:
    """Accepte soit une clé API (en-tête X-API-Key — pour les appelants
    serveur-à-serveur avec leur propre application, émise via /api/keys),
    soit un token d'ID Firebase (Authorization: Bearer ...). La clé API
    est essayée en premier quand elle est présente, puisqu'un appelant
    utilisant l'une ne devrait normalement pas envoyer l'autre — mais une
    clé invalide/révoquée ne masque pas un bearer token par ailleurs
    valide s'il a aussi été envoyé (ex. une clé de test périmée laissée à
    côté d'une vraie session navigateur) : on ne renvoie 401 sur la clé
    spécifiquement que s'il n'y a pas de bearer token de secours."""
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
    """Combine auth + limitation de débit en une seule dépendance, à
    l'image de NoiseFilterHandler._authenticate_and_rate_limit(). À
    utiliser comme dépendance sur chaque route de traitement de
    fonctionnalité. Les appelants par clé API sont limités en débit par
    clé, au budget de leur offre ; les appelants en session Firebase
    gardent le budget global fixe, par uid."""
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
    """Même vérification de token que get_current_user, mais pour
    utilisation à l'intérieur d'un handler WebSocket après réception du
    token comme premier message (les handshakes WebSocket ne peuvent pas
    porter de manière fiable des en-têtes Authorization personnalisés
    depuis un navigateur)."""
    if not token:
        raise ValueError("Missing auth token")
    try:
        decoded_token = verify_firebase_id_token(token)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid authentication token: {exc}")
    if not isinstance(decoded_token, dict):
        raise ValueError("Invalid authentication token")
    return decoded_token
