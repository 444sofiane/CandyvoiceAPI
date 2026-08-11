"""Blocking prep work shared by the NDJSON-over-HTTP and WebSocket deepfake
routes: save+validate upload, check duration cap, reserve quota, build the
exe command. Everything here is synchronous on purpose (Firestore is
blocking) — call it via asyncio.to_thread.
"""
import wave

from fastapi import HTTPException

from app import config
from app.services import audio
from app.services.detector import build_deepfake_command
from app.services.firebase import FirestoreUnavailableError, get_firestore_client
from app.services.quota import QuotaExceededError, reserve_usage_file


def prepare_deepfake_job(uid: str, raw_body: bytes, file_name: str) -> dict:
    """Returns {"command", "firestore_client", "usage_ref", "minutes_needed",
    "input_path"} on success, or raises HTTPException/ValueError on failure
    (the input file has already been cleaned up in that case)."""
    try:
        input_path = audio.save_upload(uid, raw_body, file_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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

    try:
        firestore_client = get_firestore_client()
        usage_ref = firestore_client.collection("usage").document(uid)
        reserve_usage_file(firestore_client.transaction(), usage_ref, config.FEATURE_KEY_DEEPFAKE)
    except FirestoreUnavailableError as exc:
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=503, detail=str(exc))
    except QuotaExceededError as exc:
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=429, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        print(f"Quota reservation failed for {uid}/deepfake: {exc}")
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=503, detail=f"Could not verify quota: {exc}")

    try:
        command = build_deepfake_command(input_path)
    except FileNotFoundError as exc:
        from app.services.quota import release_quota_safely
        release_quota_safely(firestore_client, usage_ref, config.FEATURE_KEY_DEEPFAKE, "missing executable")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "command": command,
        "firestore_client": firestore_client,
        "usage_ref": usage_ref,
        "minutes_needed": minutes_needed,
        "input_path": input_path,
    }


def finalize_deepfake_result(done_event: dict, job: dict, uid: str) -> dict:
    """Turns the "__done__" event from run_deepfake_stream into the final
    "result" or "error" event, including the quota commit/release — mirrors
    the tail of NoiseFilterHandler._handle_deepfake_request."""
    from app.services.detector import parse_deepfake_percent
    from app.services.quota import commit_reserved_file, release_quota_safely

    firestore_client = job["firestore_client"]
    usage_ref = job["usage_ref"]
    feature_key = config.FEATURE_KEY_DEEPFAKE

    if done_event.get("cancelled"):
        release_quota_safely(firestore_client, usage_ref, feature_key, "client disconnected")
        return {"type": "error", "error": "Connection closed before detection finished"}

    if done_event.get("timed_out"):
        release_quota_safely(firestore_client, usage_ref, feature_key, "timeout")
        return {
            "type": "error",
            "error": "The deepfake detection process timed out",
            "stdout": done_event.get("full_stdout", ""),
        }

    returncode = done_event.get("returncode")
    if returncode != 0:
        release_quota_safely(firestore_client, usage_ref, feature_key, "processing error")
        return {
            "type": "error",
            "exit_code": returncode,
            "stdout": done_event.get("full_stdout", ""),
            "error": "The deepfake detection process failed",
        }

    deepfake_percent = parse_deepfake_percent(done_event.get("full_stdout", ""))
    if deepfake_percent is None:
        release_quota_safely(firestore_client, usage_ref, feature_key, "unparseable result")
        return {
            "type": "error",
            "exit_code": returncode,
            "stdout": done_event.get("full_stdout", ""),
            "error": "Could not parse a deepfake score from the detector's output",
        }

    try:
        files_used = commit_reserved_file(firestore_client.transaction(), usage_ref, feature_key)
    except Exception as exc:  # noqa: BLE001
        print(f"Quota commit failed for {uid}/{feature_key}: {exc}")
        release_quota_safely(firestore_client, usage_ref, feature_key, "commit failure")
        files_used = None

    if files_used is not None:
        from app.services.zoho import log_usage_event
        log_usage_event(uid, feature_key)

    result_event = {
        "type": "result",
        "ok": True,
        "exit_code": returncode,
        "deepfake_percent": deepfake_percent,
        "threshold_percent": config.DEEPFAKE_THRESHOLD_PERCENT,
        "verdict": "synthetic" if deepfake_percent >= config.DEEPFAKE_THRESHOLD_PERCENT else "genuine",
        "uid": uid,
        "duration_seconds": job["minutes_needed"] * 60,
        "files_used": files_used,
        "max_files": config.MAX_FILES_PER_FEATURE,
    }
    if files_used is None:
        result_event["usage_warning"] = (
            "Detection succeeded, but quota could not be recorded. Please contact support if this persists."
        )
    return result_event
