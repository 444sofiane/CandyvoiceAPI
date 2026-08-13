"""Couverture automatisée pour app/services/quota.py.

Ceci cible la seule partie du backend où un bug est véritablement
difficile à attraper à la main : le comportement des transactions sous
concurrence. La docstring du module quota.py lui-même documente un
incident réel ("The transaction has no transaction ID...") causé par le
partage d'un objet Transactional décoré unique entre des requêtes
concurrentes. test_concurrent_reserve_at_last_slot_only_one_wins
ci-dessous est écrit pour échouer si ce bug (ou un bug de forme similaire)
revient.

À exécuter avec l'émulateur Firestore actif — voir tests/README.md.
"""
import threading

import pytest

from app import config
from app.services import quota


def test_reserve_then_commit_moves_slot_from_reserved_to_used(db, usage_ref):
    quota.reserve_usage_file(db.transaction(), usage_ref, "noiseFilter", config.MAX_FILES_PER_FEATURE)
    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "noiseFilter")
    assert (used, reserved) == (0, 1)

    new_used = quota.commit_reserved_file(db.transaction(), usage_ref, "noiseFilter")
    assert new_used == 1

    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "noiseFilter")
    assert (used, reserved) == (1, 0)


def test_release_after_failed_processing_frees_the_slot(db, usage_ref):
    quota.reserve_usage_file(db.transaction(), usage_ref, "imitation", config.MAX_FILES_PER_FEATURE)
    quota.release_reserved_file(db.transaction(), usage_ref, "imitation")

    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "imitation")
    assert (used, reserved) == (0, 0)


def test_commit_without_a_prior_reservation_raises(db, usage_ref):
    with pytest.raises(RuntimeError):
        quota.commit_reserved_file(db.transaction(), usage_ref, "deepfake")


def test_release_is_never_negative_if_called_twice(db, usage_ref):
    # release_reserved_file plafonne à 0 plutôt que de devenir négatif —
    # ça compte parce que release_quota_safely (utilisé dans les blocs
    # except) peut en principe être atteint plus d'une fois pour la même
    # réservation sur certains chemins d'erreur.
    quota.reserve_usage_file(db.transaction(), usage_ref, "frameRecovery", config.MAX_FILES_PER_FEATURE)
    quota.release_reserved_file(db.transaction(), usage_ref, "frameRecovery")
    quota.release_reserved_file(db.transaction(), usage_ref, "frameRecovery")

    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "frameRecovery")
    assert (used, reserved) == (0, 0)


def test_reserve_fails_once_quota_ceiling_reached(db, usage_ref):
    for _ in range(config.MAX_FILES_PER_FEATURE):
        quota.reserve_usage_file(db.transaction(), usage_ref, "deepfake", config.MAX_FILES_PER_FEATURE)
        quota.commit_reserved_file(db.transaction(), usage_ref, "deepfake")

    with pytest.raises(quota.QuotaExceededError):
        quota.reserve_usage_file(db.transaction(), usage_ref, "deepfake", config.MAX_FILES_PER_FEATURE)

    # et la tentative échouée ne doit rien avoir modifié
    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "deepfake")
    assert used == config.MAX_FILES_PER_FEATURE
    assert reserved == 0


def test_concurrent_reserve_at_last_slot_only_one_wins(db, usage_ref):
    """Remplit le quota jusqu'à ce qu'il ne reste exactement qu'un slot,
    puis lance deux appels de réservation concurrents dessus. Exactement
    un doit réussir et l'autre doit lever proprement QuotaExceededError —
    pas de crash, pas de double réservation, pas de doc corrompu. C'est le
    scénario de concurrence sous charge que décrit le rapport d'incident
    de la docstring du module."""
    for _ in range(config.MAX_FILES_PER_FEATURE - 1):
        quota.reserve_usage_file(db.transaction(), usage_ref, "frameRecovery", config.MAX_FILES_PER_FEATURE)
        quota.commit_reserved_file(db.transaction(), usage_ref, "frameRecovery")

    outcomes = []
    lock = threading.Lock()

    def attempt():
        try:
            quota.reserve_usage_file(db.transaction(), usage_ref, "frameRecovery", config.MAX_FILES_PER_FEATURE)
            outcome = "reserved"
        except quota.QuotaExceededError:
            outcome = "exceeded"
        except Exception as exc:  # noqa: BLE001 - on veut voir tout ce qui est inattendu
            outcome = f"unexpected: {type(exc).__name__}: {exc}"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unexpected = [o for o in outcomes if o.startswith("unexpected")]
    assert not unexpected, f"unexpected errors under concurrency: {unexpected}"
    assert outcomes.count("reserved") == 1, f"expected exactly 1 winner, got: {outcomes}"
    assert outcomes.count("exceeded") == 4

    # et le doc lui-même doit refléter exactement une réservation
    # supplémentaire, pas une écriture partielle/déchirée d'une
    # transaction perdante
    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "frameRecovery")
    assert used == config.MAX_FILES_PER_FEATURE - 1
    assert reserved == 1


def test_release_quota_safely_is_a_no_op_without_a_ref(db):
    # Ne doit jamais lever, même avec des entrées absurdes — c'est appelé
    # depuis des gestionnaires d'exception dont tout le but est de ne pas
    # lancer une deuxième erreur par-dessus la première.
    quota.release_quota_safely(db, None, "noiseFilter", "unit test")
    quota.release_quota_safely(None, None, "noiseFilter", "unit test")


def test_release_quota_safely_swallows_transaction_errors(db, usage_ref, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated Firestore outage")

    monkeypatch.setattr(quota, "release_reserved_file", boom)
    # Doit afficher et retourner, pas lever, même quand la transaction
    # sous-jacente elle-même échoue.
    quota.release_quota_safely(db, usage_ref, "noiseFilter", "unit test")
