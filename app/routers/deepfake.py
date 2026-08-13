import asyncio
import json
import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app import config
from app.deps import AuthContext, get_current_uid_rate_limited, verify_websocket_token, check_rate_limit, RateLimitExceededError
from app.services import audio
from app.services.deepfake_flow import run_deepfake_stream
from app.services.deepfake_prepare import finalize_deepfake_result, prepare_deepfake_job

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

    if first.get("type") != "auth" or not first.get("token"):
        await ws.send_json({"type": "error", "error": "First message must be {type: 'auth', token: ...}"})
        await ws.close(code=WS_POLICY_VIOLATION)
        return

    try:
        decoded_token = await verify_websocket_token(first["token"])
    except ValueError as exc:
        await ws.send_json({"type": "error", "error": str(exc)})
        await ws.close(code=WS_POLICY_VIOLATION)
        return

    uid = decoded_token.get("uid")
    try:
        check_rate_limit(uid)
    except RateLimitExceededError as exc:
        await ws.send_json({"type": "error", "error": str(exc)})
        await ws.close(code=1013)  # "réessaie plus tard"
        return

    await ws.send_json({"type": "auth_ok"})

    # --- métadonnées + upload du fichier --------------------------------
    try:
        start_msg = await ws.receive_json()
    except Exception:
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
    # L'auth WS est Firebase uniquement (voir la docstring du module) —
    # pas de chemin par clé API ici.
    auth = AuthContext(uid=uid, auth_method="firebase")
    try:
        job = await asyncio.to_thread(prepare_deepfake_job, auth, raw_body, file_name)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        await ws.send_json({"type": "error", **detail})
        await ws.close(code=1008)
        return

    # --- exécution + streaming de la progression --------------------------
    cancel_event = threading.Event()
    disconnected = False
    async for event in run_deepfake_stream(job["command"], config.SCRIPT_DIR, cancel_event=cancel_event):
        if event.get("type") == "__done__":
            # Toujours exécuté — même après déconnexion — pour que le slot
            # de quota réservé pour ce job soit libéré/validé. Avant, un
            # WebSocketDisconnect ici faisait un `return` immédiat sans
            # aller jusqu'à "__done__", ce qui fuyait la réservation
            # indéfiniment.
            final_event = await asyncio.to_thread(finalize_deepfake_result, event, job, uid)
            if not disconnected:
                try:
                    await ws.send_json(final_event)
                except WebSocketDisconnect:
                    pass
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

    if not disconnected:
        await ws.close()
