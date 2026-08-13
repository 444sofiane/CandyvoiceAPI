"""Route d'amorçage OAuth ponctuelle pour Zoho CRM. Ne fait pas partie du
trafic normal en fonctionnement — à appeler une fois pendant la mise en
place (ou à nouveau si le refresh token est un jour révoqué) pour générer
ZOHO_REFRESH_TOKEN, puis elle reste inutilisée jusqu'au prochain besoin."""
import requests
from fastapi import APIRouter, HTTPException, Request

from app import config

router = APIRouter()


@router.get("/api/zoho/oauth/callback")
async def zoho_oauth_callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    accounts_server = request.query_params.get("accounts-server", config.ZOHO_ACCOUNTS_URL)

    if error:
        raise HTTPException(status_code=400, detail=f"Zoho authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code' query param")

    token_resp = requests.post(
        f"{accounts_server}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": config.ZOHO_CLIENT_ID,
            "client_secret": config.ZOHO_CLIENT_SECRET,
            "redirect_uri": config.ZOHO_REDIRECT_URI,
            "code": code,
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()
    print(f"Zoho token response: {tokens}")
    
    if "refresh_token" not in tokens:
        raise HTTPException(
            status_code=400,
            detail="No refresh_token in response — re-run the authorization URL with prompt=consent",
        )

    # Amorçage uniquement : affiché sur la console serveur, jamais retourné
    # dans le corps de la réponse, pour qu'il ne finisse pas dans
    # l'historique du navigateur ni dans les logs d'accès.
    print("=== ZOHO REFRESH TOKEN (save to ZOHO_REFRESH_TOKEN and remove this route) ===")
    print(tokens["refresh_token"])

    return {"ok": True, "message": "Check server logs for the refresh token."}