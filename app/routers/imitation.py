import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import config
from app.deps import AuthContext, get_current_uid_rate_limited
from app.services import audio
from app.services.detector import build_imitation_command
from app.services.process_flow import run_feature_processing

router = APIRouter()


@router.post("/api/imitation")
@router.post("/api/voice-imitation")
async def imitation(
    request: Request,
    auth: AuthContext = Depends(get_current_uid_rate_limited),
    x_file_name: str | None = Header(default=None, alias="X-File-Name"),
    x_output_name: str | None = Header(default=None, alias="X-Output-Name"),
    x_confidential_check: str | None = Header(default=None, alias="X-Confidential-Check"),
    x_voice_model: str | None = Header(default=None, alias="X-Voice-Model"),
):
    raw_body = await audio.read_limited_upload(request)
    query = request.query_params
    file_name = x_file_name or query.get("file_name")
    output_name = x_output_name or query.get("output_file")
    confidential = (x_confidential_check or "").lower() == "true"

    voice_model = (x_voice_model or "").strip()
    if not voice_model:
        raise HTTPException(status_code=400, detail="X-Voice-Model header is required")
    if voice_model not in config.ALLOWED_VOICE_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown voice model: {voice_model}")

    if not file_name:
        raise HTTPException(status_code=400, detail="X-File-Name header (or file_name param) is required")

    def build_command(input_path, output_path):
        return build_imitation_command(input_path, output_path, voice_model)

    payload = await asyncio.to_thread(
        run_feature_processing,
        uid=auth.uid,
        raw_body=raw_body,
        file_name=file_name,
        output_name=output_name,
        confidential=confidential,
        feature_key=config.FEATURE_KEY_IMITATION,
        processed_output_feature_name="voice-imitation",
        build_command=build_command,
        extra_response_fields={"voice_model": voice_model},
        error_label="voice imitation",
        bypass_quota_and_storage=(auth.auth_method == "api_key"),
    )
    return payload
