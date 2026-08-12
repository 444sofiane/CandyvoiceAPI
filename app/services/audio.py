"""Upload validation/saving and audio-duration helpers, ported 1:1 from
api_server.py."""
import os
import uuid
import wave

import filetype
from fastapi import HTTPException

from app import config


async def read_limited_upload(request, max_bytes=None):
    """Reads an HTTP request body incrementally via `request.stream()`,
    aborting as soon as it exceeds `max_bytes` — unlike a plain
    `await request.body()`, which buffers the entire payload in memory
    (and only then lets the caller find out it was too big), this rejects
    an oversized upload before it ever gets written to disk.

    Raises HTTPException(413) if the payload is too large.
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
    """Sniffs the actual file content (magic bytes) rather than trusting the
    client-supplied filename or Content-Type, either of which can be
    spoofed by anyone calling the API directly."""
    kind = filetype.guess(payload)
    if kind is None:
        return False
    return kind.mime.startswith(config.ALLOWED_AUDIO_MIME_PREFIXES) or kind.mime in config.ALLOWED_AUDIO_MIME_EXTRAS


def save_uploaded_file(payload, destination):
    with open(destination, "wb") as handle:
        handle.write(payload)


def save_upload(uid, raw_body, file_name):
    """Validates `raw_body` looks like audio and saves it under UPLOAD_DIR.
    Returns the saved path, or raises ValueError with a user-facing message."""
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
