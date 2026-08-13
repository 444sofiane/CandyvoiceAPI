"""Tokens de téléchargement signés à courte durée de vie pour /outputs/,
plus le nettoyage par TTL en arrière-plan des fichiers de sortie traités.
Porté 1:1 depuis api_server.py — <audio src>/<a href> ne peuvent pas
envoyer d'en-tête Authorization, donc la propriété sur ces GET est
appliquée via un token signé et expirant, intégré dans l'URL.
"""
import asyncio
import hashlib
import hmac
import os
import threading
import time

from app import config

# Fréquence à laquelle le balayage de secours (voir periodic_cleanup_sweep
# ci-dessous) rescanne uploads/outputs, indépendamment des nettoyages par
# threading.Timer par fichier.
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
    """Supprime `file_path` après `ttl_seconds`, sans condition. Se
    déclenche sur un thread daemon en arrière-plan pour ne pas bloquer la
    réponse/l'event loop, et ne lève jamais d'exception. Utilisé aussi
    bien pour les fichiers de sortie que (puisque les fichiers d'entrée
    sont maintenant conservés pour le flux de pièce jointe Zoho — voir
    zoho.py) pour les fichiers d'entrée.
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
    """Supprime les fichiers de `directory` dont le mtime est plus vieux que
    `ttl_seconds`. Contrairement à schedule_file_cleanup, ceci ne repose
    sur rien de mémorisé dans la mémoire du processus — ça redérive
    "est-ce que ça devrait être parti maintenant ?" à partir du fichier
    lui-même, donc ça rattrape quand même les restes même si le timer
    censé les supprimer n'a jamais eu l'occasion de s'exécuter.
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
    """Filet de sécurité pour schedule_file_cleanup() : le threading.Timer
    de cette fonction ne vit que le temps du processus qui l'a programmé.
    Chaque redémarrage de conteneur ou redéploiement — chose courante dans
    une installation conteneurisée multi-réplicas — fait silencieusement
    disparaître tous les timers en attente, laissant leurs fichiers
    orphelins pour toujours puisque rien d'autre ne les suivait.

    Ceci s'exécute une fois immédiatement (pour que les fichiers rendus
    orphelins par un *précédent* redémarrage soient balayés tout de suite)
    puis à intervalle fixe tant que le processus vit, en vérifiant l'âge
    réel du fichier (mtime) plutôt que de faire confiance à une
    planification en mémoire. À appeler via asyncio.create_task(...) au
    démarrage, et à annuler à l'arrêt — voir app/main.py.
    """
    while True:
        for directory in (config.UPLOAD_DIR, config.OUTPUT_DIR):
            swept = _sweep_directory(directory, config.OUTPUT_FILE_TTL_SECONDS)
            if swept:
                print(f"Cleanup sweep: removed {swept} stale file(s) from {directory}")
        await asyncio.sleep(CLEANUP_SWEEP_INTERVAL_SECONDS)
