"""Windows executable command builders, output parsing regexes, and the
streaming subprocess wrapper (_DetectorProcess). Ported 1:1 from
api_server.py — this is the part of the app that is inherently
synchronous/blocking (it shells out to a Windows .exe), so callers in the
FastAPI routers run it via asyncio.to_thread rather than rewriting it as
async code.
"""
import fcntl
import os
import re
import shutil
import struct
import subprocess
import termios

from app import config

try:
    from winpty import PtyProcess as _PtyProcess
except ImportError:  # pragma: no cover - pywinpty is an optional, Windows-only dep
    _PtyProcess = None

try:
    import pty as _pty_module
except ImportError:  # pragma: no cover - pty is POSIX-only, no-op on Windows
    _pty_module = None

# Matches the winpty dimensions below (50 rows, 400 cols) — without this the
# pty defaults to 80 columns and the kernel hard-wraps any line the detector
# prints past column 80, splitting single progress messages across two
# reads (e.g. "...instantané : 0%" / ", deep fake moyen : 0%") and breaking
# the single-line progress/header regexes.
_PTY_ROWS, _PTY_COLS = 50, 400


DEEPFAKE_RESULT_RE = re.compile(r"%\s*de\s*deep\s*fake\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
DEEPFAKE_PROGRESS_RE = re.compile(
    r"deepFake,\s*([0-9]+(?:\.[0-9]+)?)%.*?temps\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*sec"
    r".*?insta\w*.*?:\s*([0-9]+(?:\.[0-9]+)?)%.*?moyen\s*:\s*([0-9]+(?:\.[0-9]+)?)%",
    re.IGNORECASE,
)
DEEPFAKE_HEADER_RE = re.compile(
    r"deepFake,\s*([0-9]+)\s*trames.*?:\s*([0-9]+(?:\.[0-9]+)?)\s*sec",
    re.IGNORECASE,
)


def resolve_executable():
    """Returns the command *prefix* used to invoke the detector — a list so
    that a non-Windows host can prepend a `wine` launcher transparently.

    On Windows (or once the native Linux binary replaces imitation.exe),
    this is just `[executable]`, exactly as before. On Linux, as long as
    `wine` is on PATH (see docker/Dockerfile.api), it becomes
    `["wine", executable]` — this is a stopgap so the current .exe can run
    inside a Linux container/K8s pod while the native binary isn't ready
    yet. Nothing about the callers below changes when that binary lands:
    just drop the `wine` branch here.
    """
    candidates = [os.path.join(config.SCRIPT_DIR, "imitation.exe")]
    for candidate in candidates:
        if os.path.exists(candidate):
            if os.name != "nt" and shutil.which("wine"):
                return ["wine", candidate]
            return [candidate]
    raise FileNotFoundError("No imitation executable was found next to this API server.")


def build_processing_command(input_path, output_path, inout=None):
    executable = resolve_executable()
    command = [*executable, "-noiseFilter", "1", "-input_file", input_path, "-output_file", output_path]
    if inout is not None:
        command.extend(["-inout", str(inout)])
    return command


def build_deepfake_command(input_path):
    executable = resolve_executable()
    return [
        *executable,
        "-deep_fake", "1",
        "-neurone_dir", config.DEEPFAKE_NEURONE_DIR,
        "-neurone_file", config.DEEPFAKE_NEURONE_FILE,
        "-input_file", input_path,
    ]


def build_imitation_command(input_path, output_path, voice_model):
    if voice_model not in config.ALLOWED_VOICE_MODELS:
        raise ValueError(f"Unknown voice model: {voice_model!r}")

    executable = resolve_executable()
    voice_dir = os.path.join(config.IMITATION_MODEL_DIR, voice_model) + os.sep
    neurone_dir = config.IMITATION_MODEL_DIR + os.sep

    return [
        *executable,
        "-voice_dir", voice_dir,
        "-neurone_dir", neurone_dir,
        "-neurone_file", config.IMITATION_NEURONE_FILE,
        "-input_file", input_path,
        "-output_file", output_path,
    ]


def build_frame_recovery_command(input_path, output_path, frame_recovery_factor):
    if not (0 < frame_recovery_factor <= config.FRAME_RECOVERY_FACTOR_MAX):
        raise ValueError(f"frame_recovery_factor out of range: {frame_recovery_factor!r}")

    executable = resolve_executable()
    return [
        *executable,
        "-frameRecoveryFactor", f"{frame_recovery_factor:.2f}",
        "-input_file", input_path,
        "-output_file", output_path,
    ]


def parse_deepfake_percent(stdout):
    matches = DEEPFAKE_RESULT_RE.findall(stdout or "")
    if not matches:
        return None
    try:
        return float(matches[-1]) * 100
    except ValueError:
        return None


def _decode_detector_bytes(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


class _DetectorProcess:
    """Runs the detector command and yields its output lines as they arrive.
    Ported from api_server.py's ConPTY-based version, plus a POSIX-pty
    fallback for Linux (see the two try/except imports above) so live
    progress also works when the exe runs under Wine instead of native
    Windows. This class is still blocking/synchronous by design; run it
    inside asyncio.to_thread from the async routers."""

    def __init__(self, command, cwd):
        self._pty = None
        self._popen = None
        self._posix_pty_fd = None
        self.used_pty = False
        if _PtyProcess is not None:
            self._pty = _PtyProcess.spawn(command, cwd=cwd, dimensions=(_PTY_ROWS, _PTY_COLS))
            self.used_pty = True
        elif _pty_module is not None:
            # No ConPTY here (pywinpty only wraps the Windows API), but a
            # plain POSIX pty still makes Wine treat stdout as a terminal
            # instead of a pipe, which is what actually controls whether the
            # exe flushes progress line-by-line or buffers it all until exit.
            master_fd, slave_fd = _pty_module.openpty()
            winsize = struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
            try:
                self._popen = subprocess.Popen(
                    command, cwd=cwd, stdout=slave_fd, stderr=slave_fd, close_fds=True,
                )
            finally:
                os.close(slave_fd)
            self._posix_pty_fd = master_fd
            self.used_pty = True
        else:
            self._popen = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            )

    @staticmethod
    def _first_break(buffer, newline, carriage):
        idx_n = buffer.find(newline)
        idx_r = buffer.find(carriage)
        candidates = [i for i in (idx_n, idx_r) if i != -1]
        return min(candidates) if candidates else -1

    def iter_lines(self):
        if self._pty is not None:
            buffer = ""
            while True:
                try:
                    data = self._pty.read(4096)
                except EOFError:
                    data = ""
                if not data:
                    if not self._pty.isalive():
                        break
                    continue
                buffer += data
                while True:
                    idx = self._first_break(buffer, "\n", "\r")
                    if idx == -1:
                        break
                    line, buffer = buffer[:idx], buffer[idx + 1:]
                    if line:
                        yield line
            if buffer:
                yield buffer
            return

        if self._posix_pty_fd is not None:
            buffer = b""
            while True:
                try:
                    chunk = os.read(self._posix_pty_fd, 4096)
                except OSError:
                    # EIO once the slave side is gone and the child has
                    # exited — the normal POSIX pty EOF signal.
                    chunk = b""
                if not chunk:
                    break
                buffer += chunk
                while True:
                    idx = self._first_break(buffer, b"\n", b"\r")
                    if idx == -1:
                        break
                    line, buffer = buffer[:idx], buffer[idx + 1:]
                    if line:
                        yield _decode_detector_bytes(line)
            if buffer:
                yield _decode_detector_bytes(buffer)
            os.close(self._posix_pty_fd)
            self._posix_pty_fd = None
            return

        stream = self._popen.stdout
        buffer = b""
        while True:
            chunk = stream.read(4096)
            if not chunk:
                if buffer:
                    yield _decode_detector_bytes(buffer)
                return
            buffer += chunk
            while True:
                idx = self._first_break(buffer, b"\n", b"\r")
                if idx == -1:
                    break
                line, buffer = buffer[:idx], buffer[idx + 1:]
                if line:
                    yield _decode_detector_bytes(line)

    def kill(self):
        if self._pty is not None:
            try:
                self._pty.terminate(force=True)
            except Exception:  # noqa: BLE001 - best-effort kill on timeout
                pass
        elif self._popen is not None:
            self._popen.kill()

    def wait(self):
        if self._pty is not None:
            try:
                self._pty.wait()
            except Exception:  # noqa: BLE001 - already dead / backend quirk
                pass
        elif self._popen is not None:
            self._popen.wait()
        # Defensive: if iter_lines() was abandoned mid-loop (e.g. cancelled
        # before EOF), it never reached its own os.close() cleanup.
        if self._posix_pty_fd is not None:
            try:
                os.close(self._posix_pty_fd)
            except OSError:
                pass
            self._posix_pty_fd = None

    @property
    def returncode(self):
        if self._pty is not None:
            return self._pty.exitstatus
        return self._popen.returncode

    def clean_line(self, line):
        """Strips colour codes and cursor/erase/title control sequences so
        the progress regexes only see plain text."""
        return OSC_ESCAPE_RE.sub("", ANSI_ESCAPE_RE.sub("", line))
