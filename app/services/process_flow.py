"""Shared synchronous flow for noise-filter / imitation / frame-recovery:
upload -> duration check -> quota reserve -> run exe -> quota commit ->
response. Ported 1:1 from the near-identical bodies of
_handle_noise_filter_request / _handle_imitation_request /
_handle_frame_recovery_request in api_server.py, factored out since the
three were ~90% duplicated.

This whole function is intentionally synchronous/blocking (Firestore +
subprocess.run are both blocking) — FastAPI routers call it via
asyncio.to_thread so the event loop stays free for other requests/websockets
meanwhile.
"""
import base64
import os
import subprocess
import wave

from fastapi import HTTPException

from app import config
from app.deps import AuthContext
from app.services import audio, api_key_quota
from app.services.downloads import generate_download_token, schedule_file_cleanup
from app.services.firebase import FirestoreUnavailableError, get_firestore_client
from app.services.zoho import save_processed_output_record, sync_confidential_flag_to_zoho, log_usage_event
from app.services.quota import QuotaExceededError, commit_reserved_file, release_quota_safely, reserve_usage_file


def run_feature_processing(
    *,
    auth: AuthContext,
    raw_body: bytes,
    file_name: str,
    output_name: str | None,
    confidential: bool,
    feature_key: str,
    processed_output_feature_name: str,
    build_command,  # callable(input_path, output_path) -> list[str]
    extra_response_fields: dict | None = None,
    error_label: str,
) -> dict:
    """For an API-key caller (`auth.auth_method == "api_key"`), quota is
    checked against that key's plan allowance for the current calendar
    month (api_key_quota.py) rather than the website's flat lifetime
    usage/{uid} cap — and, since their files are their own application's
    data rather than ours, nothing is persisted to Firestore or synced to
    Zoho, and both the uploaded input and the processed output are deleted
    from local disk immediately after the response is built (rather than
    kept around on a TTL) — the output is returned inline as base64
    instead of via a downloadable /outputs/ URL, since there's no file
    left to serve by the time the response is sent.
    """
    uid = auth.uid
    is_api_key = auth.auth_method == "api_key"
    extra_response_fields = extra_response_fields or {}
    if not is_api_key:
        sync_confidential_flag_to_zoho(uid, confidential)

    # --- upload -----------------------------------------------------
    try:
        input_path = audio.save_upload(uid, raw_body, file_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    output_path = audio.build_output_path(input_path, output_name, owner_uid=uid)

    # --- duration check ----------------------------------------------
    try:
        minutes_needed = audio.get_audio_duration_minutes(input_path)
    except (OSError, ValueError, wave.Error) as exc:
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=400, detail=f"Could not determine audio duration: {exc}")

    duration_seconds = minutes_needed * 60
    if duration_seconds > config.MAX_FILE_DURATION_SECONDS + config.FILE_DURATION_EPSILON_SECONDS:
        audio.cleanup_file(input_path)
        raise HTTPException(
            status_code=400,
            detail=(
                f"This file is about {duration_seconds:.0f}s, but files must be "
                f"{config.MAX_FILE_DURATION_SECONDS:.0f}s or shorter. Please trim it and try again."
            ),
        )

    # --- quota reservation --------------------------------------------
    # API-key calls reserve against that key's plan/monthly allowance;
    # Firebase-session calls reserve against the website's flat lifetime cap.
    try:
        firestore_client = get_firestore_client()
        if is_api_key:
            usage_ref = api_key_quota.usage_doc_ref(firestore_client, auth.key_id)
            max_files = api_key_quota.plan_file_limit(auth.plan)
        else:
            usage_ref = firestore_client.collection("usage").document(uid)
            max_files = config.MAX_FILES_PER_FEATURE
        reserve_usage_file(firestore_client.transaction(), usage_ref, feature_key, max_files)
    except FirestoreUnavailableError as exc:
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=503, detail=str(exc))
    except QuotaExceededError as exc:
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=429, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        print(f"Quota reservation failed for {uid}/{feature_key}: {exc}")
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=503, detail=f"Could not verify quota: {exc}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # --- run the exe ----------------------------------------------------
    try:
        command = build_command(input_path, output_path)
        completed = subprocess.run(
            command, cwd=config.SCRIPT_DIR, capture_output=True, text=True, timeout=600, check=False,
        )
    except (FileNotFoundError, ValueError) as exc:
        release_quota_safely(firestore_client, usage_ref, feature_key, "missing executable / bad params")
        if is_api_key:
            audio.cleanup_file(input_path)
        raise HTTPException(status_code=500, detail=str(exc))
    except subprocess.TimeoutExpired as exc:
        release_quota_safely(firestore_client, usage_ref, feature_key, "timeout")
        if is_api_key:
            audio.cleanup_file(input_path)
        raise HTTPException(
            status_code=504,
            detail={"error": f"The {error_label} process timed out", "stdout": exc.stdout or "", "stderr": exc.stderr or ""},
        )

    if completed.returncode != 0:
        release_quota_safely(firestore_client, usage_ref, feature_key, "processing error")
        if is_api_key:
            audio.cleanup_file(input_path)
        raise HTTPException(
            status_code=500,
            detail={
                "ok": False,
                "exit_code": completed.returncode,
                "command": command,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "error": f"The {error_label} process failed",
            },
        )

    # --- quota commit ---------------------------------------------------
    try:
        files_used = commit_reserved_file(firestore_client.transaction(), usage_ref, feature_key)
    except Exception as exc:  # noqa: BLE001
        print(f"Quota commit failed for {uid}/{feature_key}: {exc}")
        release_quota_safely(firestore_client, usage_ref, feature_key, "commit failure")
        files_used = None

    if files_used is not None and not is_api_key:
        log_usage_event(uid, feature_key)

    # --- output: URL + TTL cleanup, or inline bytes + immediate cleanup ----
    output_url = None
    output_audio_base64 = None
    if os.path.exists(output_path):
        if is_api_key:
            with open(output_path, "rb") as handle:
                output_audio_base64 = base64.b64encode(handle.read()).decode("ascii")
            audio.cleanup_file(output_path)
        else:
            output_basename = os.path.basename(output_path)
            download_token = generate_download_token(output_basename)
            output_url = f"/outputs/{output_basename}?token={download_token}"
            if not confidential:
                save_processed_output_record(
                    uid,
                    processed_output_feature_name,
                    output_path,
                    input_path=input_path,
                    original_file_name=file_name,
                )
            schedule_file_cleanup(output_path)

    if is_api_key:
        audio.cleanup_file(input_path)
    else:
        # Keep the input file around (rather than deleting it immediately) so
        # the unsatisfied-survey Zoho attachment flow (webhooks.py) can attach
        # the original submission alongside the output. Same TTL as the
        # output, so it isn't retained indefinitely if the survey/attachment
        # flow never fires for this file.
        schedule_file_cleanup(input_path)

    payload = {
        "ok": True,
        "exit_code": completed.returncode,
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "output_file": output_path,
        "output_url": output_url,
        "uid": uid,
        "duration_seconds": duration_seconds,
        "files_used": files_used,
        "max_files": max_files,
        **extra_response_fields,
    }
    if is_api_key:
        payload["output_audio_base64"] = output_audio_base64
        payload["plan"] = auth.plan
    elif files_used is None:
        payload["usage_warning"] = (
            f"{error_label.capitalize()} succeeded, but quota could not be recorded. "
            "Please contact support if this persists."
        )
    return payload
