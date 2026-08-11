"""Bridges the blocking _DetectorProcess line-iterator to an async
generator of event dicts, so it can be consumed either by a chunked NDJSON
HTTP StreamingResponse (backward-compatible with the current frontend) or
by a WebSocket handler (the new SaaS-style route). This is the async
equivalent of the for-loop inside
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
    """Returns an async generator yielding event dicts:
      {"type": "warning", ...}   - no live progress available (no pywinpty)
      {"type": "progress", ...}  - per-frame progress
      {"type": "info", ...}      - one-time header (total_frames, estimated_duration_sec)
      {"type": "__done__", "returncode": int, "timed_out": bool, "full_stdout": str}

    The caller is responsible for turning "__done__" into the final
    "result"/"error" event (that part needs the quota-commit call, which is
    the caller's job, not this generator's).
    """
    q: "queue.Queue" = queue.Queue()
    cancel_event = cancel_event or threading.Event()

    def worker():
        try:
            detector = _DetectorProcess(command, cwd)
        except Exception as exc:  # noqa: BLE001 - spawn can fail via OSError or a pty backend error
            q.put({"type": "error", "error": str(exc)})
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
