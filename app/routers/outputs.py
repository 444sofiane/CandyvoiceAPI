import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from app import config
from app.deps import _verify_bearer_token
from app.services.downloads import verify_download_token

router = APIRouter()


@router.get("/outputs/{output_name}")
async def get_output(output_name: str, token: str | None = None, authorization: str = Header(default="")):
    """Deux façons d'y accéder, comme dans l'original : un token signé à
    courte durée de vie dans l'URL (ce qu'utilisent réellement <audio src>/
    <a download>, puisqu'ils ne peuvent pas envoyer d'en-tête Authorization),
    ou un bearer token dont l'uid correspond au préfixe propriétaire du nom
    de fichier (voir build_output_path)."""
    safe_output_name = os.path.basename(output_name)

    authorized = verify_download_token(safe_output_name, token)

    if not authorized and authorization:
        decoded_token = _verify_bearer_token(authorization)
        uid = decoded_token.get("uid")
        authorized = safe_output_name.split("_", 1)[0] == uid

    if not authorized:
        # 404, pas 403 — ne pas confirmer qu'un fichier existe quand il
        # n'est pas accessible à cet appelant.
        raise HTTPException(status_code=404, detail="File not found")

    file_path = os.path.join(config.OUTPUT_DIR, safe_output_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path)
