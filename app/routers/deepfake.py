import asyncio
import json
import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app import config
from app.deps import get_current_uid_rate_limited, verify_websocket_token, check_rate_limit, RateLimitExceededError
from app.services import audio
from app.services.deepfake_flow import run_deepfake_stream
from app.services.deepfake_prepare import finalize_deepfake_result, prepare_deepfake_job

router = APIRouter()


# ---------------------------------------------------------------------
# HTTP route — kept NDJSON-over-chunked-response for backward
# compatibility with the current frontend (same content-type, same event
# shapes as the original BaseHTTPRequestHandler version).
# ---------------------------------------------------------------------

@router.post("/api/deepfake-detect")
@router.post("/api/deepfake")
@router.post("/api/deepfake-detection")
async def deepfake_detect(
    request: Request,
    uid: str = Depends(get_current_uid_rate_limited),
    x_file_name: str | None = Header(default=None, alias="X-File-Name"),
):
    raw_body = await audio.read_limited_upload(request)
    file_name = x_file_name or request.query_params.get("file_name")
    if not file_name:
        raise HTTPException(status_code=400, detail="X-File-Name header (or file_name param) is required")

    # Everything up to here can still fail with a normal HTTP status. From
    # here on we commit to the streaming response, same as the original —
    # any further failure has to be reported as a {"type": "error"} event
    # inside the stream rather than an HTTP status.
    job = await asyncio.to_thread(prepare_deepfake_job, uid, raw_body, file_name)

    async def event_stream():
        cancel_event = threading.Event()
        try:
            async for event in run_deepfake_stream(job["command"], config.SCRIPT_DIR, cancel_event=cancel_event):
                if event.get("type") == "__done__":
                    final_event = await asyncio.to_thread(finalize_deepfake_result, event, job, uid)
                    yield (json.dumps(final_event) + "\n").encode("utf-8")
                    return
                yield (json.dumps(event) + "\n").encode("utf-8")
                # Cooperatively notice a client that has gone away between
                # events (Starlette doesn't interrupt a running generator
                # on disconnect by itself).
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
# WebSocket route — the new SaaS-style path. Protocol:
#   client -> {"type": "auth", "token": "<firebase id token>"}
#   server -> {"type": "auth_ok"}                    (or error + close)
#   client -> {"type": "start", "file_name": "clip.wav"}
#   client -> <binary frame: raw file bytes>
#   server -> {"type": "warning"|"progress"|"info"} ...  (0 or more)
#   server -> {"type": "result"|"error"} ...              (exactly one, then closes)
# ---------------------------------------------------------------------

WS_POLICY_VIOLATION = 4401


@router.websocket("/ws/deepfake")
async def deepfake_ws(ws: WebSocket):
    
    origin = ws.headers.get("origin", "")
    if origin and origin not in config.ALLOWED_ORIGINS:
        await ws.close(code=4403)
        return
    await ws.accept()
    

    # --- auth --------------------------------------------------------
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
        await ws.close(code=1013)  # "try again later"
        return

    await ws.send_json({"type": "auth_ok"})

    # --- metadata + file upload ---------------------------------------
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

    # A WebSocket binary frame arrives as one already-buffered message —
    # there's no streaming/early-abort option here like the HTTP routes
    # have via request.stream(), so the best available guard is rejecting
    # it immediately, before it's written to disk or a quota slot is
    # reserved for it.
    if len(raw_body) > config.MAX_UPLOAD_SIZE_BYTES:
        await ws.send_json({
            "type": "error",
            "error": f"File too large — max {config.MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB per upload.",
        })
        await ws.close(code=1009)  # 1009 = "Message Too Big"
        return

    # --- prepare (upload/duration/quota/command) ------------------------
    try:
        job = await asyncio.to_thread(prepare_deepfake_job, uid, raw_body, file_name)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        await ws.send_json({"type": "error", **detail})
        await ws.close(code=1008)
        return

    # --- run + stream progress ------------------------------------------
    cancel_event = threading.Event()
    disconnected = False
    async for event in run_deepfake_stream(job["command"], config.SCRIPT_DIR, cancel_event=cancel_event):
        if event.get("type") == "__done__":
            # Always run this — even post-disconnect — so the quota slot
            # reserved for this job gets released/committed. Previously,
            # a WebSocketDisconnect here `return`ed immediately without
            # draining to "__done__", leaking the reservation forever.
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
            # Client went away mid-stream: cancel the subprocess and keep
            # draining the stream (without trying to send further) until
            # "__done__" arrives, so the reservation above still gets
            # released/committed.
            disconnected = True
            cancel_event.set()

    if not disconnected:
        await ws.close()
