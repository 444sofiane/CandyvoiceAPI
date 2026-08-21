"""Flux synchrone partagé pour noise-filter / imitation / frame-recovery :
upload -> vérification de durée -> réservation de quota -> exécution de
l'exe -> validation du quota -> réponse. Porté 1:1 depuis les corps
quasi-identiques de _handle_noise_filter_request / _handle_imitation_request
/ _handle_frame_recovery_request dans api_server.py, factorisé puisque les
trois étaient dupliqués à ~90%.

Cette fonction entière est intentionnellement synchrone/bloquante
(Firestore + subprocess.run sont tous deux bloquants) — les routers
FastAPI l'appellent via asyncio.to_thread pour que l'event loop reste
libre pour les autres requêtes/websockets pendant ce temps.
"""
import os
import subprocess
import wave

from fastapi import HTTPException, Response

from app import config
from app.deps import AuthContext
from app.services import audio, api_key_quota
from app.services.downloads import generate_download_token, schedule_file_cleanup
from app.services.firebase import FirestoreUnavailableError, get_firestore_client
from app.services.zoho import save_processed_output_record, sync_confidential_flag_to_zoho, log_usage_event
from app.services.quota import QuotaExceededError, commit_reserved_file, release_quota_safely, reserve_usage_file


def _header_name(key: str) -> str:
    """"voice_model" -> "Voice-Model", pour transformer extra_response_fields
    en en-têtes de réponse sur le chemin binaire par clé API."""
    return "-".join(word.capitalize() for word in key.split("_"))


def _header_value(value) -> str:
    """Retire les CR/LF avant qu'une valeur n'entre dans un en-tête de
    réponse. Chaque appelant actuel (voice_model, frame_recovery_factor,
    uid, plan) est déjà validé/borné en amont, donc c'est de la défense en
    profondeur plutôt que la correction d'un bug atteignable aujourd'hui —
    mais la construction des en-têtes ne devrait pas reposer sur le fait
    que ça reste vrai pour chaque futur champ que quelqu'un ajoute à
    extra_response_fields."""
    return str(value).replace("\r", "").replace("\n", "")


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
) -> dict | Response:
    """Pour un appelant par clé API (`auth.auth_method == "api_key"`), le
    quota n'est pas compté au fichier/mois mais au budget de secondes
    d'une "session" — comme deepfake (voir deepfake_prepare.py et
    api_key_quota.py) : puisque ces trois endpoints sont toujours
    one-shot (une requête = une session d'un seul clip, pas de notion
    multi-chunks comme /ws/deepfake), c'est simplement le clip qui doit
    tenir dans le budget entier de l'offre. Il n'y a donc plus de
    réservation Firestore avant traitement ; l'usage en secondes est
    seulement journalisé après coup, à titre indicatif pour le tableau de
    bord et le rapport mensuel — jamais pour bloquer une requête.

    Puisque leurs fichiers sont les données de leur propre application
    plutôt que les nôtres, rien n'est persisté dans Firestore ni
    synchronisé vers Zoho pour un appelant par clé API, et l'entrée
    uploadée comme la sortie traitée sont supprimées du disque local
    immédiatement après la construction de la réponse (plutôt que
    conservées avec un TTL).

    Ce chemin par clé API retourne aussi une *forme* de réponse différente :
    l'audio traité revient comme le corps de réponse binaire brut
    (`audio/wav`) avec les métadonnées dans des en-têtes `X-*`, au lieu de
    l'enveloppe JSON avec un `output_url` du chemin session Firebase — le
    retourner en ligne en JSON base64 a été essayé en premier, mais
    l'inflation de ~33% de taille du base64 par-dessus un WAV déjà non
    compressé faisait gonfler les réponses à des dizaines de Mo pour les
    clips plus longs. Les erreurs reviennent quand même en JSON dans les
    deux cas (levées en HTTPException, comme toujours) — seule la réponse
    de succès de ces trois routes diffère selon la méthode d'auth.
    """
    uid = auth.uid
    is_api_key = auth.auth_method == "api_key"
    extra_response_fields = extra_response_fields or {}
    if not is_api_key:
        sync_confidential_flag_to_zoho(uid, confidential)

    # --- upload ----------------------------------------------------------
    try:
        input_path = audio.save_upload(uid, raw_body, file_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    output_path = audio.build_output_path(input_path, output_name, owner_uid=uid)

    # --- vérification de la durée ----------------------------------------
    try:
        minutes_needed = audio.get_audio_duration_minutes(input_path)
    except (OSError, ValueError, wave.Error) as exc:
        audio.cleanup_file(input_path)
        raise HTTPException(status_code=400, detail=f"Could not determine audio duration: {exc}")

    duration_seconds = minutes_needed * 60

    # Clé API : quota payant, un dépassement reste un rejet dur (voir
    # process_flow docstring). Session Firebase (le site démo) : on coupe
    # au lieu de rejeter, pour que l'utilisateur obtienne quand même un
    # résultat sur les premières config.MAX_FILE_DURATION_SECONDS secondes.
    truncated = False
    max_session_seconds = None
    if is_api_key:
        max_session_seconds = api_key_quota.plan_session_seconds(auth.plan)
        if duration_seconds > max_session_seconds + config.FILE_DURATION_EPSILON_SECONDS:
            audio.cleanup_file(input_path)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This file is about {duration_seconds:.0f}s, but the {auth.plan} plan's "
                    f"session budget for this feature is {max_session_seconds:.0f}s."
                ),
            )
    elif duration_seconds > config.MAX_FILE_DURATION_SECONDS + config.FILE_DURATION_EPSILON_SECONDS:
        duration_seconds = audio.truncate_wav_to_seconds(input_path, config.MAX_FILE_DURATION_SECONDS)
        minutes_needed = duration_seconds / 60.0
        truncated = True

    # --- réservation de quota (session Firebase uniquement) ----------------
    # Les appels par clé API n'ont rien à réserver — leur budget par
    # session vient d'être vérifié ci-dessus, en temps réel, sans passer
    # par Firestore. Seul le chemin session Firebase garde le plafond fixe
    # à vie du site, avec sa réservation transactionnelle habituelle.
    firestore_client = None
    usage_ref = None
    if not is_api_key:
        try:
            firestore_client = get_firestore_client()
            usage_ref = firestore_client.collection("usage").document(uid)
            reserve_usage_file(firestore_client.transaction(), usage_ref, feature_key, config.MAX_FILES_PER_FEATURE)
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

    # --- exécution de l'exe -----------------------------------------------
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

    # --- validation du quota (session Firebase) / journalisation (clé API) -
    files_used = None
    if is_api_key:
        try:
            usage_client = get_firestore_client()
            api_key_quota.log_session_usage(usage_client, auth.key_id, feature_key, duration_seconds)
        except FirestoreUnavailableError:
            pass  # le suivi mensuel est purement indicatif — jamais bloquant
        except Exception as exc:  # noqa: BLE001
            print(f"Could not log session usage for key {auth.key_id}/{feature_key}: {exc}")
    else:
        try:
            files_used = commit_reserved_file(firestore_client.transaction(), usage_ref, feature_key)
        except Exception as exc:  # noqa: BLE001
            print(f"Quota commit failed for {uid}/{feature_key}: {exc}")
            release_quota_safely(firestore_client, usage_ref, feature_key, "commit failure")
            files_used = None

        if files_used is not None:
            log_usage_event(uid, feature_key)

    # --- chemin clé API : corps binaire brut + en-têtes, nettoyage immédiat -
    if is_api_key:
        if not os.path.exists(output_path):
            # Ne devrait normalement pas arriver pour une exécution réussie
            # de ces trois fonctionnalités, mais il n'y a rien à servir
            # comme corps binaire — on retombe sur une simple enveloppe
            # JSON plutôt qu'une réponse vide.
            audio.cleanup_file(input_path)
            return {
                "ok": True,
                "exit_code": completed.returncode,
                "output_url": None,
                "uid": uid,
                "duration_seconds": duration_seconds,
                "files_used": None,
                "max_files": None,
                "plan": auth.plan,
                "max_session_seconds": max_session_seconds,
                **extra_response_fields,
            }

        with open(output_path, "rb") as handle:
            output_bytes = handle.read()
        audio.cleanup_file(output_path)
        audio.cleanup_file(input_path)

        headers = {
            "X-Exit-Code": _header_value(completed.returncode),
            "X-Uid": _header_value(uid),
            "X-Duration-Seconds": _header_value(duration_seconds),
            "X-Max-Session-Seconds": _header_value(max_session_seconds),
            "X-Plan": _header_value(auth.plan or ""),
        }
        for field_name, value in extra_response_fields.items():
            headers[f"X-{_header_name(field_name)}"] = _header_value(value)

        return Response(content=output_bytes, media_type="audio/wav", headers=headers)

    # --- chemin session Firebase : enveloppe JSON + URL téléchargeable -----
    output_url = None
    if os.path.exists(output_path):
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

    # Conserve le fichier d'entrée (plutôt que de le supprimer
    # immédiatement) pour que le flux de pièce jointe Zoho du sondage
    # d'insatisfaction (webhooks.py) puisse attacher la soumission
    # originale à côté de la sortie. Même TTL que la sortie, pour qu'il ne
    # soit pas retenu indéfiniment si le flux sondage/pièce jointe ne se
    # déclenche jamais pour ce fichier.
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
        "truncated": truncated,
        "files_used": files_used,
        "max_files": config.MAX_FILES_PER_FEATURE,
        **extra_response_fields,
    }
    if files_used is None:
        payload["usage_warning"] = (
            f"{error_label.capitalize()} succeeded, but quota could not be recorded. "
            "Please contact support if this persists."
        )
    return payload
