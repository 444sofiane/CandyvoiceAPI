"""Fixtures pytest partagées pour les tests qui ont besoin d'une vraie
transaction Firestore (pas un mock) — toute la raison d'être de quota.py
est la sémantique de transaction sous concurrence, et mocker
`firestore.transactional` d'assez près pour être fiable demande plus de
travail et donne moins de confiance que de simplement pointer le client
google-cloud-firestore vers l'émulateur local.

Nécessite que l'émulateur Firestore tourne avant le démarrage de pytest :

    firebase emulators:start --only firestore

Voir tests/README.md pour la mise en place complète.
"""
import os
import socket

import pytest


def _emulator_reachable(host: str) -> bool:
    hostname, port = host.split(":")
    try:
        with socket.create_connection((hostname, int(port)), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_firestore_emulator():
    """Ignore toute la session avec un message clair plutôt que d'échouer
    avec une brute erreur de connexion refusée, si quelqu'un lance pytest
    sans que l'émulateur ne tourne."""
    host = os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    if not _emulator_reachable(host):
        pytest.skip(
            f"Firestore emulator not reachable at {host}. Start it first: "
            "firebase emulators:start --only firestore "
            "(see tests/README.md)",
        )


@pytest.fixture
def db():
    # Importé ici, pas au niveau module, pour que la collecte de ce
    # fichier n'échoue pas durement dans les environnements sans
    # google-cloud-firestore installé (ex. une simple vérification
    # `pytest --collect-only`).
    from google.cloud import firestore

    return firestore.Client(project="candyvoice-test")


@pytest.fixture
def usage_ref(db):
    """Un nouveau doc usage/{uid} par test, nettoyé après coup peu importe
    la réussite/l'échec, pour que les tests ne fuient jamais d'état entre
    eux."""
    ref = db.collection("usage").document("test-uid")
    yield ref
    ref.delete()
