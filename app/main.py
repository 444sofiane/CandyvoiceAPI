import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app import config
from app.services.downloads import periodic_cleanup_sweep
from app.services.firebase import init_firebase_admin
from app.routers import admin_api_keys, admin_reports, api_keys, deepfake, frame_recovery, health, imitation, noise_filter, outputs, webhooks, zoho_auth

app = FastAPI(
    title="CandyVoice API",
    version="1.0.0",
    # Server header used to say "CandyVoiceAPI" instead of leaking the
    # exact Python version — FastAPI/uvicorn set their own Server header;
    # override it at the reverse-proxy layer (nginx/Caddy) in production
    # rather than trying to fight uvicorn's header here.
)

# CORS: same allow-list as the original ALLOWED_ORIGINS set. Browsers only
# — this is not the access-control layer (the bearer token check is), same
# reasoning as the original file's comment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type", "X-File-Name", "X-Output-Name", "X-Voice-Model",
        "X-Frame-Recovery-Factor", "X-Confidential-Check", "Authorization",
        "X-API-Key",
    ],
)


@app.on_event("startup")
async def on_startup():
    init_firebase_admin()
    # Backstop for the per-file threading.Timer cleanup in downloads.py —
    # see periodic_cleanup_sweep's docstring for why this is needed in a
    # containerized/multi-replica setup.
    app.state.cleanup_sweep_task = asyncio.create_task(periodic_cleanup_sweep())


@app.on_event("shutdown")
async def on_shutdown():
    task = getattr(app.state, "cleanup_sweep_task", None)
    if task is not None:
        task.cancel()


app.include_router(health.router)
app.include_router(outputs.router)
app.include_router(noise_filter.router)
app.include_router(imitation.router)
app.include_router(frame_recovery.router)
app.include_router(deepfake.router)
app.include_router(webhooks.router)
app.include_router(zoho_auth.router)
app.include_router(admin_reports.router)
app.include_router(admin_api_keys.router)
app.include_router(api_keys.router)


@app.get("/")
@app.get("/index.html")
async def index():
    index_path = os.path.join(config.SCRIPT_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
    """Overrides FastAPI's default {"detail": ...} envelope so error
    responses keep the exact shape the current frontend already expects
    (e.g. {"error": "..."} or, for exe-failure cases, the full
    {"ok": False, "error": ..., "stdout": ...} dict) — this is what lets
    the frontend cut over without also needing a rewrite for this
    migration.
    """
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort safety net so an unexpected error still comes back as
    # JSON rather than a bare 500 HTML page.
    print(f"Unhandled exception on {request.url.path}: {exc}")
    return JSONResponse({"error": "Internal server error"}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=bool(os.environ.get("UVICORN_RELOAD")),
    )
