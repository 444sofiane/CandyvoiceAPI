import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from app import config
from app.deps import _verify_bearer_token
from app.services.downloads import verify_download_token

router = APIRouter()


@router.get("/outputs/{output_name}")
async def get_output(output_name: str, token: str | None = None, authorization: str = Header(default="")):
    """Two ways in, same as the original: a signed short-lived token in the
    URL (what <audio src>/<a download> actually use, since they can't send
    an Authorization header), or a bearer token whose uid matches the
    filename's owner prefix (see build_output_path)."""
    safe_output_name = os.path.basename(output_name)

    authorized = verify_download_token(safe_output_name, token)

    if not authorized and authorization:
        decoded_token = _verify_bearer_token(authorization)
        uid = decoded_token.get("uid")
        authorized = safe_output_name.startswith(f"{uid}_")

    if not authorized:
        # 404, not 403 — don't confirm a file exists when it isn't
        # accessible to this caller.
        raise HTTPException(status_code=404, detail="File not found")

    file_path = os.path.join(config.OUTPUT_DIR, safe_output_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path)
