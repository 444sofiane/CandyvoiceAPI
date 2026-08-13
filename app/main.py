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
    # L'en-tête Server disait "CandyVoiceAPI" plutôt que de laisser fuiter
    # la version Python exacte — FastAPI/uvicorn définissent leur propre
    # en-tête Server ; à surcharger au niveau du reverse-proxy (nginx/Caddy)
    # en production plutôt que d'essayer de lutter contre l'en-tête d'uvicorn ici.
)

# CORS : même liste d'autorisation que l'ensemble ALLOWED_ORIGINS d'origine.
# Navigateurs uniquement — ce n'est pas la couche de contrôle d'accès (c'est
# la vérification du bearer token qui l'est), même raisonnement que le
# commentaire du fichier d'origine.
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
    # Par défaut, les navigateurs cachent tous les en-têtes de réponse à JS
    # sauf un petit ensemble "simple" (Content-Type etc.) — sans ceci, les
    # en-têtes de métadonnées X-* de la réponse binaire par clé API (voir
    # process_flow.py) seraient envoyés mais illisibles par fetch()/XHR
    # depuis une origine autorisée.
    expose_headers=[
        "X-Exit-Code", "X-Uid", "X-Duration-Seconds", "X-Files-Used",
        "X-Max-Files", "X-Plan", "X-Voice-Model", "X-Frame-Recovery-Factor",
    ],
)


@app.on_event("startup")
async def on_startup():
    init_firebase_admin()
    # Filet de sécurité pour le nettoyage par threading.Timer par fichier
    # dans downloads.py — voir la docstring de periodic_cleanup_sweep pour
    # savoir pourquoi c'est nécessaire dans une installation conteneurisée
    # multi-réplicas.
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
    """Surcharge l'enveloppe par défaut {"detail": ...} de FastAPI pour que
    les réponses d'erreur gardent exactement la forme que le frontend
    actuel attend déjà (ex. {"error": "..."} ou, pour les cas d'échec de
    l'exe, le dict complet {"ok": False, "error": ..., "stdout": ...}) —
    c'est ce qui permet au frontend de basculer sans avoir besoin d'une
    réécriture pour cette migration.
    """
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Filet de sécurité de dernier recours pour qu'une erreur inattendue
    # revienne quand même en JSON plutôt qu'en une simple page HTML 500.
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
