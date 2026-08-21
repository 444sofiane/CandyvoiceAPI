"""Helpers de validation/sauvegarde d'upload et de durée audio, portés
1:1 depuis api_server.py."""
import os
import uuid
import wave

import filetype
from fastapi import HTTPException

from app import config


async def read_limited_upload(request, max_bytes=None):
    """Lit un corps de requête HTTP progressivement via `request.stream()`,
    en abandonnant dès qu'il dépasse `max_bytes` — contrairement à un
    simple `await request.body()`, qui bufferise tout le payload en
    mémoire (et ne laisse l'appelant découvrir qu'il était trop gros
    qu'ensuite), ceci rejette un upload surdimensionné avant même qu'il
    soit écrit sur disque.

    Lève HTTPException(413) si le payload est trop gros.
    """
    max_bytes = config.MAX_UPLOAD_SIZE_BYTES if max_bytes is None else max_bytes
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large — max {max_bytes // (1024 * 1024)}MB per upload.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def looks_like_audio(payload):
    """Renifle le contenu réel du fichier (magic bytes) plutôt que de faire
    confiance au nom de fichier ou au Content-Type fournis par le client,
    tous deux pouvant être falsifiés par quiconque appelle l'API
    directement."""
    kind = filetype.guess(payload)
    if kind is None:
        return False
    return kind.mime.startswith(config.ALLOWED_AUDIO_MIME_PREFIXES) or kind.mime in config.ALLOWED_AUDIO_MIME_EXTRAS


def save_uploaded_file(payload, destination):
    with open(destination, "wb") as handle:
        handle.write(payload)


def save_upload(uid, raw_body, file_name):
    """Valide que `raw_body` ressemble à de l'audio et le sauvegarde sous
    UPLOAD_DIR. Retourne le chemin sauvegardé, ou lève ValueError avec un
    message destiné à l'utilisateur."""
    if not raw_body:
        raise ValueError("No file data received")
    if not looks_like_audio(raw_body):
        raise ValueError("Uploaded file does not look like a supported audio format")

    input_name = os.path.basename(file_name)
    input_path = os.path.join(config.UPLOAD_DIR, f"{uid}_{uuid.uuid4().hex}_{input_name}")
    save_uploaded_file(raw_body, input_path)
    return input_path


def get_audio_duration_minutes(file_path):
    with wave.open(file_path, "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        if not frame_rate:
            raise ValueError("Unable to determine audio duration from this WAV file.")
        return wav_file.getnframes() / float(frame_rate) / 60.0


def truncate_wav_to_seconds(file_path, max_seconds):
    """Coupe le fichier WAV situé à `file_path` à ses `max_seconds`
    premières secondes, en le réécrivant sur place. Ne fait rien si le
    fichier est déjà assez court. Retourne la durée résultante en
    secondes, utilisée pour remplacer `duration_seconds` chez l'appelant."""
    with wave.open(file_path, "rb") as wav_in:
        params = wav_in.getparams()
        frame_rate = wav_in.getframerate()
        max_frames = int(max_seconds * frame_rate)
        if wav_in.getnframes() <= max_frames:
            return wav_in.getnframes() / float(frame_rate)
        frames = wav_in.readframes(max_frames)

    with wave.open(file_path, "wb") as wav_out:
        wav_out.setparams(params)
        wav_out.writeframes(frames)

    return max_frames / float(frame_rate)


def build_output_path(input_path, requested_name=None, owner_uid=None):
    input_name = os.path.basename(input_path)
    stem = os.path.splitext(input_name)[0]
    requested_name = requested_name or f"{stem}_filtered.wav"
    safe_name = os.path.basename(requested_name)

    if owner_uid:
        safe_name = f"{owner_uid}_{uuid.uuid4().hex}_{safe_name}"

    return os.path.join(config.OUTPUT_DIR, safe_name)


def cleanup_file(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
