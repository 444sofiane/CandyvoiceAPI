"""Constructeurs de commandes pour l'exécutable Windows, regex de parsing
de la sortie, et le wrapper de sous-processus en streaming
(_DetectorProcess). Porté 1:1 depuis api_server.py — c'est la partie de
l'app qui est intrinsèquement synchrone/bloquante (elle exécute un .exe
Windows en sous-processus), donc les appelants dans les routers FastAPI
l'exécutent via asyncio.to_thread plutôt que de la réécrire en code async.
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
except ImportError:  # pragma: no cover - pywinpty est une dépendance optionnelle, Windows uniquement
    _PtyProcess = None

try:
    import pty as _pty_module
except ImportError:  # pragma: no cover - pty est POSIX uniquement, no-op sur Windows
    _pty_module = None

# Correspond aux dimensions winpty ci-dessous (50 lignes, 400 colonnes) —
# sans ça le pty revient par défaut à 80 colonnes et le noyau coupe
# durement toute ligne que le détecteur affiche au-delà de la colonne 80,
# scindant des messages de progression uniques en deux lectures (ex.
# "...instantané : 0%" / ", deep fake moyen : 0%") et cassant les regex
# de progression/en-tête en une seule ligne.
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
    """Retourne le *préfixe* de commande utilisé pour invoquer le
    détecteur — une liste pour qu'un hôte non-Windows puisse préfixer un
    lanceur `wine` de façon transparente.

    Sur Windows (ou une fois que le binaire Linux natif remplace
    imitation.exe), c'est juste `[executable]`, exactement comme avant.
    Sur Linux, tant que `wine` est dans le PATH (voir
    docker/Dockerfile.api), ça devient `["wine", executable]` — c'est un
    pis-aller pour que le .exe actuel puisse tourner dans un conteneur
    Linux/pod K8s tant que le binaire natif n'est pas prêt. Rien ne change
    du côté des appelants ci-dessous quand ce binaire arrivera : il suffit
    de supprimer la branche `wine` ici.
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
    """Exécute la commande du détecteur et produit ses lignes de sortie au
    fur et à mesure qu'elles arrivent. Porté depuis la version basée sur
    ConPTY de api_server.py, plus un repli POSIX-pty pour Linux (voir les
    deux imports try/except ci-dessus) pour que la progression en direct
    fonctionne aussi quand l'exe tourne sous Wine plutôt que sous Windows
    natif. Cette classe est encore bloquante/synchrone par conception ;
    exécute-la dans asyncio.to_thread depuis les routers async."""

    def __init__(self, command, cwd):
        self._pty = None
        self._popen = None
        self._posix_pty_fd = None
        self.used_pty = False
        if _PtyProcess is not None:
            self._pty = _PtyProcess.spawn(command, cwd=cwd, dimensions=(_PTY_ROWS, _PTY_COLS))
            self.used_pty = True
        elif _pty_module is not None:
            # Pas de ConPTY ici (pywinpty ne fait qu'envelopper l'API
            # Windows), mais un simple pty POSIX fait quand même que Wine
            # traite stdout comme un terminal plutôt que comme un pipe, ce
            # qui est ce qui contrôle réellement si l'exe vide sa
            # progression ligne par ligne ou la bufferise jusqu'à la fin.
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
                    # EIO une fois que le côté slave a disparu et que
                    # l'enfant s'est terminé — le signal EOF normal du
                    # pty POSIX.
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
            except Exception:  # noqa: BLE001 - kill au mieux en cas de timeout
                pass
        elif self._popen is not None:
            self._popen.kill()

    def wait(self):
        if self._pty is not None:
            try:
                self._pty.wait()
            except Exception:  # noqa: BLE001 - déjà mort / bizarrerie du backend
                pass
        elif self._popen is not None:
            self._popen.wait()
        # Défensif : si iter_lines() a été abandonné en cours de boucle
        # (ex. annulé avant EOF), il n'a jamais atteint son propre nettoyage
        # par os.close().
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
        """Retire les codes couleur et les séquences de contrôle
        curseur/effacement/titre pour que les regex de progression ne
        voient que du texte brut."""
        return OSC_ESCAPE_RE.sub("", ANSI_ESCAPE_RE.sub("", line))
