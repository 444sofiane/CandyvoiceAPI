import asyncio
import json
import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app import config
from app.deps import AuthContext, get_current_uid_rate_limited, verify_websocket_token, check_rate_limit, RateLimitExceededError
from app.services import audio
from app.services.api_key_quota import plan_rate_limit, plan_session_seconds
from app.services.api_keys import resolve_api_key
from app.services.deepfake_flow import run_deepfake_stream
from app.services.deepfake_prepare import finalize_deepfake_result, prepare_deepfake_job
from app.services.firebase import FirestoreUnavailableError

router = APIRouter()


# ---------------------------------------------------------------------
# Route HTTP — garde le NDJSON-en-réponse-chunkée pour rester compatible
# avec le frontend actuel (même content-type, mêmes formes d'événements
# que la version originale BaseHTTPRequestHandler).
# ---------------------------------------------------------------------

@router.post("/api/deepfake-detect")
@router.post("/api/deepfake")
@router.post("/api/deepfake-detection")
async def deepfake_detect(
    request: Request,
    auth: AuthContext = Depends(get_current_uid_rate_limited),
    x_file_name: str | None = Header(default=None, alias="X-File-Name"),
):
    raw_body = await audio.read_limited_upload(request)
    file_name = x_file_name or request.query_params.get("file_name")
    if not file_name:
        raise HTTPException(status_code=400, detail="X-File-Name header (or file_name param) is required")

    # Tout ce qui précède peut encore échouer avec un statut HTTP normal.
    # À partir d'ici on s'engage dans la réponse en streaming, comme
    # l'original — tout échec ultérieur doit être signalé comme un
    # événement {"type": "error"} dans le flux plutôt que par un statut HTTP.
    job = await asyncio.to_thread(prepare_deepfake_job, auth, raw_body, file_name)

    async def event_stream():
        cancel_event = threading.Event()
        try:
            async for event in run_deepfake_stream(job["command"], config.SCRIPT_DIR, cancel_event=cancel_event):
                if event.get("type") == "__done__":
                    final_event = await asyncio.to_thread(finalize_deepfake_result, event, job, auth.uid)
                    yield (json.dumps(final_event) + "\n").encode("utf-8")
                    return
                yield (json.dumps(event) + "\n").encode("utf-8")
                # Détecte de manière coopérative un client qui est parti
                # entre deux événements (Starlette n'interrompt pas de
                # lui-même un générateur en cours en cas de déconnexion).
                if await request.is_disconnected():
                    cancel_event.set()
        finally:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------
# Route WebSocket — le nouveau chemin façon SaaS. Protocole :
#   client -> {"type": "auth", "token": "<firebase id token>"}
#             ou {"type": "auth", "api_key": "<clé API>"}
#   serveur -> {"type": "auth_ok"}                    (ou erreur + fermeture)
#   client -> {"type": "start", "file_name": "clip.wav"}
#   client -> <frame binaire : octets bruts du fichier>
#   serveur -> {"type": "warning"|"progress"|"info"} ...  (0 ou plus)
#   serveur -> {"type": "result"|"error"} ...              (exactement un, puis fermeture)
# ---------------------------------------------------------------------

WS_POLICY_VIOLATION = 4401


@router.websocket("/ws/deepfake")
async def deepfake_ws(ws: WebSocket):
    
    origin = ws.headers.get("origin", "")
    if origin and origin not in config.ALLOWED_ORIGINS:
        await ws.close(code=4403)
        return
    await ws.accept()
    

    # --- auth ----------------------------------------------------------
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close(code=WS_POLICY_VIOLATION, reason="Expected an auth message first")
        return

    api_key = first.get("api_key")
    token = first.get("token")
    if first.get("type") != "auth" or not (token or api_key):
        await ws.send_json({
            "type": "error",
            "error": "First message must be {type: 'auth', token: ...} or {type: 'auth', api_key: ...}",
        })
        await ws.close(code=WS_POLICY_VIOLATION)
        return

    if api_key:
        # Appelant serveur-à-serveur par clé API — pas de limitation de
        # navigateur ici (contrairement au bearer token, qui passe par le
        # premier message WS uniquement parce qu'un navigateur ne peut pas
        # joindre d'en-tête personnalisé au handshake). Même précédence
        # clé-avant-bearer et même repli que get_current_auth côté HTTP.
        try:
            record = resolve_api_key(api_key)
        except FirestoreUnavailableError as exc:
            await ws.send_json({"type": "error", "error": str(exc)})
            await ws.close(code=1013)
            return
        if not record:
            await ws.send_json({"type": "error", "error": "Invalid or revoked API key"})
            await ws.close(code=WS_POLICY_VIOLATION)
            return
        auth = AuthContext(uid=record.uid, auth_method="api_key", key_id=record.key_id, plan=record.plan)
    else:
        try:
            decoded_token = await verify_websocket_token(token)
        except ValueError as exc:
            await ws.send_json({"type": "error", "error": str(exc)})
            await ws.close(code=WS_POLICY_VIOLATION)
            return
        auth = AuthContext(uid=decoded_token.get("uid"), auth_method="firebase")

    if auth.auth_method == "api_key":
        max_requests, window_seconds = plan_rate_limit(auth.plan)
        bucket_key = f"apikey:{auth.key_id}"
    else:
        max_requests, window_seconds = None, None
        bucket_key = auth.uid

    try:
        check_rate_limit(bucket_key, max_requests, window_seconds)
    except RateLimitExceededError as exc:
        await ws.send_json({"type": "error", "error": str(exc)})
        await ws.close(code=1013)  # "réessaie plus tard"
        return

    await ws.send_json({"type": "auth_ok"})

    is_api_key = auth.auth_method == "api_key"
    # Pour une clé API, une "session" peut couvrir plusieurs chunks envoyés
    # à la suite sur cette même connexion (ex. une détection en direct
    # pendant un appel, découpée en clips de quelques secondes) — voir
    # prepare_deepfake_job. Pour une session Firebase, la boucle
    # ci-dessous ne tourne qu'une fois, exactement comme avant.
    max_session_seconds = plan_session_seconds(auth.plan) if is_api_key else None
    session_seconds_used = 0.0
    first_chunk = True

    while True:
        if is_api_key and not first_chunk:
            # Chaque chunk d'une session multi-chunks compte comme une
            # requête distincte pour la limitation de débit, comme le
            # ferait une suite d'appels HTTP.
            try:
                check_rate_limit(bucket_key, max_requests, window_seconds)
            except RateLimitExceededError as exc:
                await ws.send_json({"type": "error", "error": str(exc)})
                await ws.close(code=1013)
                return
        first_chunk = False

        # --- métadonnées + upload du fichier --------------------------------
        try:
            start_msg = await ws.receive_json()
        except Exception:
            if is_api_key and session_seconds_used > 0:
                # Le client a simplement clos la session après un ou
                # plusieurs chunks réussis — fin normale, pas une violation.
                return
            await ws.close(code=WS_POLICY_VIOLATION, reason="Expected a start message")
            return

        if start_msg.get("type") != "start" or not start_msg.get("file_name"):
            await ws.send_json({"type": "error", "error": "Expected {type: 'start', file_name: ...}"})
            await ws.close(code=WS_POLICY_VIOLATION)
            return
        file_name = start_msg["file_name"]

        try:
            raw_body = await ws.receive_bytes()
        except Exception:
            await ws.send_json({"type": "error", "error": "Expected a binary frame with the file contents"})
            await ws.close(code=WS_POLICY_VIOLATION)
            return

        # Une frame binaire WebSocket arrive comme un seul message déjà
        # bufferisé — il n'y a pas ici d'option de streaming/abandon anticipé
        # comme les routes HTTP en ont via request.stream(), donc la meilleure
        # protection disponible est de la rejeter immédiatement, avant qu'elle
        # soit écrite sur disque ou qu'un slot de quota soit réservé pour elle.
        if len(raw_body) > config.MAX_UPLOAD_SIZE_BYTES:
            await ws.send_json({
                "type": "error",
                "error": f"File too large — max {config.MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB per upload.",
            })
            await ws.close(code=1009)  # 1009 = "Message Too Big"
            return

        # --- préparation (upload/durée/quota/commande) -----------------------
        try:
            job = await asyncio.to_thread(
                prepare_deepfake_job, auth, raw_body, file_name, session_seconds_used
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
            await ws.send_json({"type": "error", **detail})
            await ws.close(code=1008)
            return

        # --- exécution + streaming de la progression --------------------------
        cancel_event = threading.Event()
        disconnected = False
        chunk_succeeded = False
        async for event in run_deepfake_stream(job["command"], config.SCRIPT_DIR, cancel_event=cancel_event):
            if event.get("type") == "__done__":
                # Toujours exécuté — même après déconnexion — pour que le slot
                # de quota réservé pour ce job soit libéré/validé. Avant, un
                # WebSocketDisconnect ici faisait un `return` immédiat sans
                # aller jusqu'à "__done__", ce qui fuyait la réservation
                # indéfiniment.
                final_event = await asyncio.to_thread(finalize_deepfake_result, event, job, auth.uid)
                chunk_succeeded = final_event.get("type") == "result"
                if chunk_succeeded:
                    session_seconds_used += job["duration_seconds"]
                    if is_api_key:
                        final_event["session_seconds_used"] = session_seconds_used
                if not disconnected:
                    try:
                        await ws.send_json(final_event)
                    except WebSocketDisconnect:
                        disconnected = True
                break

            if disconnected:
                continue

            try:
                await ws.send_json(event)
            except WebSocketDisconnect:
                # Le client est parti en plein flux : annule le sous-processus
                # et continue de vider le flux (sans essayer d'envoyer quoi que
                # ce soit d'autre) jusqu'à ce que "__done__" arrive, pour que la
                # réservation ci-dessus soit quand même libérée/validée.
                disconnected = True
                cancel_event.set()

        if disconnected:
            return

        if not is_api_key:
            break  # session Firebase : un seul chunk, comme avant

        if not chunk_succeeded:
            # Un chunk en erreur termine la session — pas de nouvelle
            # tentative automatique dans la même connexion ; le client doit
            # ouvrir une nouvelle session s'il veut réessayer.
            await ws.close(code=1008)
            return

        if session_seconds_used + config.FILE_DURATION_EPSILON_SECONDS >= max_session_seconds:
            # Budget de cette session épuisé — fermeture normale plutôt que
            # d'attendre un nouveau "start" qui échouerait de toute façon.
            await ws.close(code=1000)
            return

    await ws.close()
