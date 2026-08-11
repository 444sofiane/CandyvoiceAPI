"""One-time OAuth bootstrap route for Zoho CRM. Not part of normal runtime
traffic — hit this once during setup (or again if the refresh token is
ever revoked) to mint ZOHO_REFRESH_TOKEN, then it's unused until needed
again."""
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

    # Bootstrap-only: printed to the server console, never returned in the
    # response body, so it doesn't end up in browser history or access logs.
    print("=== ZOHO REFRESH TOKEN (save to ZOHO_REFRESH_TOKEN and remove this route) ===")
    print(tokens["refresh_token"])

    return {"ok": True, "message": "Check server logs for the refresh token."}