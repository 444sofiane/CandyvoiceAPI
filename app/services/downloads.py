"""Signed, short-lived download tokens for /outputs/, plus the background
TTL cleanup for processed output files. Ported 1:1 from api_server.py —
<audio src>/<a href> can't send an Authorization header, so ownership on
those GETs is enforced via a signed, expiring token embedded in the URL.
"""
import asyncio
import hashlib
import hmac
import os
import threading
import time

from app import config

# How often the backstop sweep (see periodic_cleanup_sweep below) re-scans
# uploads/outputs, independent of the per-file threading.Timer cleanups.
CLEANUP_SWEEP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_SWEEP_INTERVAL_SECONDS", "300"))


def generate_download_token(filename, ttl_seconds=config.DOWNLOAD_TOKEN_TTL_SECONDS):
    expiry = int(time.time()) + ttl_seconds
    message = f"{filename}:{expiry}".encode("utf-8")
    signature = hmac.new(config.DOWNLOAD_TOKEN_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def verify_download_token(filename, token):
    if not token or "." not in token:
        return False
    expiry_str, _, signature = token.partition(".")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if time.time() > expiry:
        return False

    message = f"{filename}:{expiry}".encode("utf-8")
    expected = hmac.new(config.DOWNLOAD_TOKEN_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def schedule_file_cleanup(file_path, ttl_seconds=config.OUTPUT_FILE_TTL_SECONDS):
    """Deletes `file_path` after `ttl_seconds`, unconditionally. Fires on a
    background daemon thread so it doesn't block the response/event loop,
    and never raises. Used for both output files and (since inputs are now
    kept around for the Zoho attachment flow — see zoho.py) input files.
    """

    def _cleanup():
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"Deleted file after TTL ({ttl_seconds}s): {file_path}")
        except OSError as exc:
            print(f"Could not delete file {file_path}: {exc}")

    timer = threading.Timer(ttl_seconds, _cleanup)
    timer.daemon = True
    timer.start()


def _sweep_directory(directory, ttl_seconds):
    """Removes files in `directory` whose mtime is older than `ttl_seconds`.
    Unlike schedule_file_cleanup, this doesn't rely on anything remembered
    in process memory — it re-derives "should this be gone by now?" from
    the file itself, so it still catches leftovers even if the timer that
    was supposed to delete them never got the chance to run.
    """
    swept = 0
    now = time.time()
    try:
        entries = os.listdir(directory)
    except OSError as exc:
        print(f"Cleanup sweep: could not list {directory}: {exc}")
        return swept

    for name in entries:
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path):
                continue
            if now - os.path.getmtime(path) > ttl_seconds:
                os.remove(path)
                swept += 1
        except OSError as exc:
            print(f"Cleanup sweep: could not remove {path}: {exc}")

    return swept


async def periodic_cleanup_sweep():
    """Backstop for schedule_file_cleanup(): that function's threading.Timer
    only lives as long as the process that scheduled it. Every container
    restart or redeploy — routine in a containerized/multi-replica setup —
    silently drops any pending timers, leaving their files orphaned forever
    since nothing else was tracking them.

    This runs once immediately (so files orphaned by a *previous* restart
    get swept right away) and then on a fixed interval for as long as the
    process lives, checking real file age (mtime) rather than trusting any
    in-memory schedule. Call via asyncio.create_task(...) from startup and
    cancel the task on shutdown — see app/main.py.
    """
    while True:
        for directory in (config.UPLOAD_DIR, config.OUTPUT_DIR):
            swept = _sweep_directory(directory, config.OUTPUT_FILE_TTL_SECONDS)
            if swept:
                print(f"Cleanup sweep: removed {swept} stale file(s) from {directory}")
        await asyncio.sleep(CLEANUP_SWEEP_INTERVAL_SECONDS)
