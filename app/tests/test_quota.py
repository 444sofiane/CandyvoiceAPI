"""Automated coverage for app/services/quota.py.

This targets the one piece of the backend where a bug is genuinely hard
to catch by hand: transaction behavior under concurrency. quota.py's own
module docstring documents a real incident ("The transaction has no
transaction ID...") caused by sharing one decorated Transactional object
across concurrent requests. test_concurrent_reserve_at_last_slot_only_one_wins
below is written to fail if that bug (or one shaped like it) comes back.

Run with the Firestore emulator up — see tests/README.md.
"""
import threading

import pytest

from app import config
from app.services import quota


def test_reserve_then_commit_moves_slot_from_reserved_to_used(db, usage_ref):
    quota.reserve_usage_file(db.transaction(), usage_ref, "noiseFilter")
    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "noiseFilter")
    assert (used, reserved) == (0, 1)

    new_used = quota.commit_reserved_file(db.transaction(), usage_ref, "noiseFilter")
    assert new_used == 1

    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "noiseFilter")
    assert (used, reserved) == (1, 0)


def test_release_after_failed_processing_frees_the_slot(db, usage_ref):
    quota.reserve_usage_file(db.transaction(), usage_ref, "imitation")
    quota.release_reserved_file(db.transaction(), usage_ref, "imitation")

    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "imitation")
    assert (used, reserved) == (0, 0)


def test_commit_without_a_prior_reservation_raises(db, usage_ref):
    with pytest.raises(RuntimeError):
        quota.commit_reserved_file(db.transaction(), usage_ref, "deepfake")


def test_release_is_never_negative_if_called_twice(db, usage_ref):
    # release_reserved_file floors at 0 rather than going negative — this
    # matters because release_quota_safely (used in except blocks) can in
    # principle be reached more than once for the same reservation on some
    # error paths.
    quota.reserve_usage_file(db.transaction(), usage_ref, "frameRecovery")
    quota.release_reserved_file(db.transaction(), usage_ref, "frameRecovery")
    quota.release_reserved_file(db.transaction(), usage_ref, "frameRecovery")

    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "frameRecovery")
    assert (used, reserved) == (0, 0)


def test_reserve_fails_once_quota_ceiling_reached(db, usage_ref):
    for _ in range(config.MAX_FILES_PER_FEATURE):
        quota.reserve_usage_file(db.transaction(), usage_ref, "deepfake")
        quota.commit_reserved_file(db.transaction(), usage_ref, "deepfake")

    with pytest.raises(quota.QuotaExceededError):
        quota.reserve_usage_file(db.transaction(), usage_ref, "deepfake")

    # and the failed attempt must not have mutated anything
    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "deepfake")
    assert used == config.MAX_FILES_PER_FEATURE
    assert reserved == 0


def test_concurrent_reserve_at_last_slot_only_one_wins(db, usage_ref):
    """Fill the quota to exactly one slot remaining, then fire two
    concurrent reserve calls at it. Exactly one must succeed and the
    other must cleanly raise QuotaExceededError — no crash, no double
    reservation, no corrupted doc. This is the concurrency-under-load
    scenario the module docstring's incident report describes."""
    for _ in range(config.MAX_FILES_PER_FEATURE - 1):
        quota.reserve_usage_file(db.transaction(), usage_ref, "frameRecovery")
        quota.commit_reserved_file(db.transaction(), usage_ref, "frameRecovery")

    outcomes = []
    lock = threading.Lock()

    def attempt():
        try:
            quota.reserve_usage_file(db.transaction(), usage_ref, "frameRecovery")
            outcome = "reserved"
        except quota.QuotaExceededError:
            outcome = "exceeded"
        except Exception as exc:  # noqa: BLE001 - want to see anything unexpected
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

    # and the doc itself must reflect exactly one extra reservation, not
    # a partial/torn write from a losing transaction
    used, reserved = quota.read_feature_usage_fields(usage_ref.get(), "frameRecovery")
    assert used == config.MAX_FILES_PER_FEATURE - 1
    assert reserved == 1


def test_release_quota_safely_is_a_no_op_without_a_ref(db):
    # Must never raise, even with garbage inputs — it's called from
    # exception handlers where the whole point is not to throw a second
    # error on top of the first one.
    quota.release_quota_safely(db, None, "noiseFilter", "unit test")
    quota.release_quota_safely(None, None, "noiseFilter", "unit test")


def test_release_quota_safely_swallows_transaction_errors(db, usage_ref, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated Firestore outage")

    monkeypatch.setattr(quota, "release_reserved_file", boom)
    # Should print and return, not raise, even when the underlying
    # transaction itself fails.
    quota.release_quota_safely(db, usage_ref, "noiseFilter", "unit test")
