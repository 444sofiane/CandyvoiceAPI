import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import config
from app.deps import get_current_uid_rate_limited
from app.services.detector import build_frame_recovery_command
from app.services.process_flow import run_feature_processing

router = APIRouter()


@router.post("/api/frame-recovery")
@router.post("/api/frameRecovery")
async def frame_recovery(
    request: Request,
    uid: str = Depends(get_current_uid_rate_limited),
    x_file_name: str | None = Header(default=None, alias="X-File-Name"),
    x_output_name: str | None = Header(default=None, alias="X-Output-Name"),
    x_confidential_check: str | None = Header(default=None, alias="X-Confidential-Check"),
    x_frame_recovery_factor: str | None = Header(default=None, alias="X-Frame-Recovery-Factor"),
):
    raw_body = await request.body()
    query = request.query_params
    file_name = x_file_name or query.get("file_name")
    output_name = x_output_name or query.get("output_file")
    confidential = (x_confidential_check or "").lower() == "true"

    factor_header = (x_frame_recovery_factor or "").strip()
    if not factor_header:
        raise HTTPException(status_code=400, detail="X-Frame-Recovery-Factor header is required")
    try:
        frame_recovery_factor = float(factor_header)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Frame-Recovery-Factor must be a number")
    if not (0 < frame_recovery_factor <= config.FRAME_RECOVERY_FACTOR_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"X-Frame-Recovery-Factor must be greater than 0 and at most {config.FRAME_RECOVERY_FACTOR_MAX}",
        )

    if not file_name:
        raise HTTPException(status_code=400, detail="X-File-Name header (or file_name param) is required")

    def build_command(input_path, output_path):
        return build_frame_recovery_command(input_path, output_path, frame_recovery_factor)

    payload = await asyncio.to_thread(
        run_feature_processing,
        uid=uid,
        raw_body=raw_body,
        file_name=file_name,
        output_name=output_name,
        confidential=confidential,
        feature_key=config.FEATURE_KEY_FRAME_RECOVERY,
        processed_output_feature_name="frame-recovery",
        build_command=build_command,
        extra_response_fields={"frame_recovery_factor": frame_recovery_factor},
        error_label="frame recovery",
    )
    return payload
