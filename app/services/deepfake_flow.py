"""Fait le pont entre l'itérateur de lignes bloquant de _DetectorProcess et
un générateur async de dicts d'événements, pour qu'il puisse être consommé
soit par une HTTP StreamingResponse NDJSON chunkée (rétrocompatible avec
le frontend actuel), soit par un handler WebSocket (la nouvelle route
façon SaaS). C'est l'équivalent async de la boucle for à l'intérieur de
NoiseFilterHandler._handle_deepfake_request.
"""
import asyncio
import queue
import threading

from app.services.detector import (
    DEEPFAKE_HEADER_RE,
    DEEPFAKE_PROGRESS_RE,
    _DetectorProcess,
)

_SENTINEL = object()


def run_deepfake_stream(command, cwd, timeout_seconds=600, cancel_event: threading.Event = None):
    """Retourne un générateur async produisant des dicts d'événements :
      {"type": "warning", ...}   - pas de progression en direct disponible (pas de pywinpty)
      {"type": "progress", ...}  - progression par trame
      {"type": "info", ...}      - en-tête unique (total_frames, estimated_duration_sec)
      {"type": "__done__", "returncode": int, "timed_out": bool, "full_stdout": str}

    C'est à l'appelant de transformer "__done__" en événement final
    "result"/"error" (cette partie a besoin de l'appel de commit de quota,
    qui est le travail de l'appelant, pas celui de ce générateur).
    """
    q: "queue.Queue" = queue.Queue()
    cancel_event = cancel_event or threading.Event()

    def worker():
        try:
            detector = _DetectorProcess(command, cwd)
        except Exception as exc:  # noqa: BLE001 - le lancement peut échouer via OSError ou une erreur du backend pty
            # Émis comme "__done__" (pas un simple "error") pour que le
            # chemin de complétion normal de l'appelant s'exécute et libère
            # le slot de quota réservé avant le début de ce flux — un simple
            # événement "error" ici faisait auparavant perdre silencieusement
            # cette réservation pour toujours.
            q.put({
                "type": "__done__",
                "cancelled": False,
                "timed_out": False,
                "returncode": None,
                "full_stdout": f"Failed to start detector: {exc}",
            })
            q.put(_SENTINEL)
            return

        if not detector.used_pty:
            q.put({
                "type": "warning",
                "message": "Live progress is unavailable on this server (pywinpty not installed); "
                           "only the final result will be reported.",
            })

        timed_out = {"flag": False}

        def _on_timeout():
            timed_out["flag"] = True
            detector.kill()

        timer = threading.Timer(timeout_seconds, _on_timeout)
        timer.start()

        output_lines = []
        try:
            for line in detector.iter_lines():
                if cancel_event.is_set():
                    detector.kill()
                    break
                output_lines.append(line)
                clean_line = detector.clean_line(line)

                progress_match = DEEPFAKE_PROGRESS_RE.search(clean_line)
                if progress_match:
                    percent_processed, elapsed_sec, instant_percent, average_percent = progress_match.groups()
                    q.put({
                        "type": "progress",
                        "percent_processed": float(percent_processed),
                        "elapsed_sec": float(elapsed_sec),
                        "instant_percent": float(instant_percent),
                        "average_percent": float(average_percent),
                    })
                    continue

                header_match = DEEPFAKE_HEADER_RE.search(clean_line)
                if header_match:
                    q.put({
                        "type": "info",
                        "total_frames": int(header_match.group(1)),
                        "estimated_duration_sec": float(header_match.group(2)),
                    })
        finally:
            timer.cancel()
            detector.wait()

        q.put({
            "type": "__done__",
            "cancelled": cancel_event.is_set(),
            "timed_out": timed_out["flag"],
            "returncode": detector.returncode,
            "full_stdout": "\n".join(output_lines),
        })
        q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def _generator():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is _SENTINEL:
                return
            yield item
            if item.get("type") == "__done__":
                return

    return _generator()
