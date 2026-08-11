"""Shared pytest fixtures for tests that need a real Firestore transaction
(not a mock) — quota.py's whole reason for existing is transaction
semantics under concurrency, and mocking `firestore.transactional` closely
enough to be trustworthy is more work and less confidence than just
pointing the google-cloud-firestore client at the local emulator.

Requires the Firestore emulator running before pytest starts:

    firebase emulators:start --only firestore

See tests/README.md for full setup.
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
    """Skip the whole session with a clear message instead of failing with
    a raw connection-refused error, if someone runs pytest without the
    emulator up."""
    host = os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    if not _emulator_reachable(host):
        pytest.skip(
            f"Firestore emulator not reachable at {host}. Start it first: "
            "firebase emulators:start --only firestore "
            "(see tests/README.md)",
        )


@pytest.fixture
def db():
    # Imported here, not at module level, so that collecting this file
    # doesn't hard-fail in environments without google-cloud-firestore
    # installed (e.g. a quick `pytest --collect-only` sanity check).
    from google.cloud import firestore

    return firestore.Client(project="candyvoice-test")


@pytest.fixture
def usage_ref(db):
    """A fresh usage/{uid} doc per test, cleaned up afterwards regardless
    of pass/fail so tests never leak state into each other."""
    ref = db.collection("usage").document("test-uid")
    yield ref
    ref.delete()
