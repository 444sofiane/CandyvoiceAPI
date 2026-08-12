import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import config
from app.deps import get_current_uid_rate_limited
from app.services import audio
from app.services.detector import build_processing_command
from app.services.process_flow import run_feature_processing

router = APIRouter()


@router.post("/api/noise-filter")
@router.post("/process")
@router.post("/api/process")
async def noise_filter(
    request: Request,
    uid: str = Depends(get_current_uid_rate_limited),
    x_file_name: str | None = Header(default=None, alias="X-File-Name"),
    x_output_name: str | None = Header(default=None, alias="X-Output-Name"),
    x_confidential_check: str | None = Header(default=None, alias="X-Confidential-Check"),
):
    raw_body = await audio.read_limited_upload(request)
    query = request.query_params
    file_name = x_file_name or query.get("file_name")
    output_name = x_output_name or query.get("output_file")
    inout = query.get("inout")
    confidential = (x_confidential_check or "").lower() == "true"

    if not file_name:
        raise HTTPException(status_code=400, detail="X-File-Name header (or file_name param) is required")

    def build_command(input_path, output_path):
        return build_processing_command(input_path, output_path, inout)

    payload = await asyncio.to_thread(
        run_feature_processing,
        uid=uid,
        raw_body=raw_body,
        file_name=file_name,
        output_name=output_name,
        confidential=confidential,
        feature_key=config.FEATURE_KEY_NOISE_FILTER,
        processed_output_feature_name="noise-filter",
        build_command=build_command,
        error_label="noise filter",
    )
    return payload
