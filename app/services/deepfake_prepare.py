"""Travail de préparation bloquant partagé par les routes deepfake
NDJSON-sur-HTTP et WebSocket : sauvegarde+validation de l'upload,
vérification du plafond de durée, réservation de quota, construction de
la commande de l'exe. Tout ici est synchrone à dessein (Firestore est
bloquant) — à appeler via asyncio.to_thread.
"""
import wave

from fastapi import HTTPException

from app import config
from app.deps import AuthContext
from app.services import audio, api_key_quota
from app.services.detector import build_deepfake_command
from app.services.firebase import FirestoreUnavailableError, get_firestore_client
from app.services.quota import QuotaExceededError, reserve_usage_file


def prepare_deepfake_job(
    auth: AuthContext, raw_body: bytes, file_name: str, session_seconds_used: float = 0.0
) -> dict:
    """Retourne {"command", "firestore_client", "usage_ref",
    "minutes_needed", "duration_seconds", "input_path", ...} en cas de
    succès, ou lève HTTPException/ValueError en cas d'échec (le fichier
    d'entrée a déjà été nettoyé dans ce cas).

    Pour un appelant par clé API (`auth.auth_method == "api_key"`), il n'y
    a pas de réservation de quota Firestore : le budget est vérifié en
    temps réel par rapport à l'allocation de secondes de l'offre de cette
    clé pour une "session" (un appel /ws/deepfake en direct — cumulé sur
    tous ses chunks via `session_seconds_used`, toujours 0 pour le chemin
    HTTP one-shot — ou un seul clip pour /api/deepfake-detect), plutôt que
    par rapport au plafond fixe MAX_FILE_DURATION_SECONDS appliqué au
    chemin Firebase. En cas d'échec ci-dessous pour une clé API, l'entrée
    uploadée est supprimée immédiatement plutôt que laissée pour le
    balayage par TTL — voir la docstring de
    process_flow.run_feature_processing pour la justification complète
    (partagée par les deux chemins de traitement)."""
    uid = auth.uid
    is_api_key = auth.auth_method == "api_key"

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

    # Clé API : quota payant par session, un dépassement reste un rejet dur.
    # Session Firebase (le site démo) : on coupe au lieu de rejeter, avec la
    # limite deepfake dédiée (plus généreuse que les trois autres
    # fonctionnalités, voir config.MAX_FILE_DURATION_SECONDS_DEEPFAKE).
    truncated = False
    max_session_seconds = None
    if is_api_key:
        max_session_seconds = api_key_quota.plan_session_seconds(auth.plan)
        remaining = max_session_seconds - session_seconds_used
        if duration_seconds > remaining + config.FILE_DURATION_EPSILON_SECONDS:
            audio.cleanup_file(input_path)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This chunk is about {duration_seconds:.0f}s, but only "
                    f"{max(remaining, 0):.0f}s remain in this session's "
                    f"{max_session_seconds:.0f}s budget ({auth.plan} plan)."
                ),
            )
    elif duration_seconds > config.MAX_FILE_DURATION_SECONDS_DEEPFAKE + config.FILE_DURATION_EPSILON_SECONDS:
        duration_seconds = audio.truncate_wav_to_seconds(
            input_path, config.MAX_FILE_DURATION_SECONDS_DEEPFAKE
        )
        minutes_needed = duration_seconds / 60.0
        truncated = True

    firestore_client = None
    usage_ref = None
    max_files = None
    if not is_api_key:
        try:
            firestore_client = get_firestore_client()
            usage_ref = firestore_client.collection("usage").document(uid)
            max_files = config.MAX_FILES_PER_FEATURE
            reserve_usage_file(firestore_client.transaction(), usage_ref, config.FEATURE_KEY_DEEPFAKE, max_files)
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
        if not is_api_key:
            from app.services.quota import release_quota_safely
            release_quota_safely(firestore_client, usage_ref, config.FEATURE_KEY_DEEPFAKE, "missing executable")
        else:
            audio.cleanup_file(input_path)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "command": command,
        "firestore_client": firestore_client,
        "usage_ref": usage_ref,
        "minutes_needed": minutes_needed,
        "duration_seconds": duration_seconds,
        "truncated": truncated,
        "input_path": input_path,
        "original_file_name": file_name,
        "max_files": max_files,
        "max_session_seconds": max_session_seconds,
        "is_api_key": is_api_key,
        "plan": auth.plan,
        "key_id": auth.key_id,
    }


def finalize_deepfake_result(done_event: dict, job: dict, uid: str) -> dict:
    """Transforme l'événement "__done__" de run_deepfake_stream en
    l'événement final "result" ou "error", y compris la
    validation/libération du quota (Firebase) ou le suivi d'usage de
    session (clé API) — reflète la fin de
    NoiseFilterHandler._handle_deepfake_request."""
    from app.services.detector import parse_deepfake_percent
    from app.services.quota import commit_reserved_file, release_quota_safely

    firestore_client = job["firestore_client"]
    usage_ref = job["usage_ref"]
    feature_key = config.FEATURE_KEY_DEEPFAKE
    is_api_key = job.get("is_api_key", False)
    input_path = job["input_path"]
    duration_seconds = job["duration_seconds"]

    if done_event.get("cancelled"):
        if is_api_key:
            audio.cleanup_file(input_path)
        else:
            release_quota_safely(firestore_client, usage_ref, feature_key, "client disconnected")
        return {"type": "error", "error": "Connection closed before detection finished"}

    if done_event.get("timed_out"):
        if is_api_key:
            audio.cleanup_file(input_path)
        else:
            release_quota_safely(firestore_client, usage_ref, feature_key, "timeout")
        return {
            "type": "error",
            "error": "The deepfake detection process timed out",
            "stdout": done_event.get("full_stdout", ""),
        }

    returncode = done_event.get("returncode")
    if returncode != 0:
        if is_api_key:
            audio.cleanup_file(input_path)
        else:
            release_quota_safely(firestore_client, usage_ref, feature_key, "processing error")
        return {
            "type": "error",
            "exit_code": returncode,
            "stdout": done_event.get("full_stdout", ""),
            "error": "The deepfake detection process failed",
        }

    deepfake_percent = parse_deepfake_percent(done_event.get("full_stdout", ""))
    if deepfake_percent is None:
        if is_api_key:
            audio.cleanup_file(input_path)
        else:
            release_quota_safely(firestore_client, usage_ref, feature_key, "unparseable result")
        return {
            "type": "error",
            "exit_code": returncode,
            "stdout": done_event.get("full_stdout", ""),
            "error": "Could not parse a deepfake score from the detector's output",
        }

    verdict = "synthetic" if deepfake_percent >= config.DEEPFAKE_THRESHOLD_PERCENT else "genuine"

    files_used = None
    if is_api_key:
        audio.cleanup_file(input_path)
        try:
            client = get_firestore_client()
            api_key_quota.log_session_usage(client, job.get("key_id"), feature_key, duration_seconds)
        except FirestoreUnavailableError:
            pass  # le suivi mensuel est purement indicatif — jamais bloquant
        except Exception as exc:  # noqa: BLE001
            print(f"Could not log deepfake session usage for key {job.get('key_id')}: {exc}")
    else:
        try:
            files_used = commit_reserved_file(firestore_client.transaction(), usage_ref, feature_key)
        except Exception as exc:  # noqa: BLE001
            print(f"Quota commit failed for {uid}/{feature_key}: {exc}")
            release_quota_safely(firestore_client, usage_ref, feature_key, "commit failure")
            files_used = None

        if files_used is not None:
            from app.services.zoho import log_usage_event
            log_usage_event(uid, feature_key)

        # La détection de deepfake n'a pas de sortie audio traitée propre —
        # enregistre juste l'entrée pour que le flux Zoho du sondage
        # d'insatisfaction (webhooks.py) puisse l'attacher au Contact de
        # l'utilisateur.
        from app.services.downloads import schedule_file_cleanup
        from app.services.zoho import save_processed_output_record

        save_processed_output_record(
            uid,
            feature_key,
            None,
            input_path=input_path,
            original_file_name=job.get("original_file_name"),
        )
        # Même TTL utilisé ailleurs dans ce but, pour que l'entrée ne soit
        # pas retenue indéfiniment si le flux Zoho du sondage
        # d'insatisfaction ne se déclenche jamais pour cette soumission
        # (voir le commentaire identique dans process_flow.py).
        schedule_file_cleanup(input_path)

    result_event = {
        "type": "result",
        "ok": True,
        "exit_code": returncode,
        "deepfake_percent": deepfake_percent,
        "threshold_percent": config.DEEPFAKE_THRESHOLD_PERCENT,
        "verdict": verdict,
        "uid": uid,
        "duration_seconds": duration_seconds,
        "truncated": job.get("truncated", False),
        "files_used": files_used,
        "max_files": job.get("max_files"),
    }
    if is_api_key:
        result_event["plan"] = job.get("plan")
        result_event["max_session_seconds"] = job.get("max_session_seconds")
    elif files_used is None:
        result_event["usage_warning"] = (
            "Detection succeeded, but quota could not be recorded. Please contact support if this persists."
        )
    return result_event
