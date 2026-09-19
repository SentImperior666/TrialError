"""M2's doctor checks: ``stale_lease``, ``heartbeat_age``. Mirrors
``tests/test_stores_checks.py``'s auto-discovery + planted-fixture
convention exactly."""

from __future__ import annotations

from trialerror.jobs import ledger
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks


def _run(names, program_root):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def test_jobs_checks_are_auto_discovered_without_import():
    from trialerror.util.doctor import clear_registry, registered_checks

    clear_registry()
    discover_and_register_checks()
    names = set(registered_checks())
    assert {"stale_lease", "heartbeat_age"} <= names


def test_stale_lease_skips_when_jobs_db_absent(tmp_path):
    results = _run(["stale_lease"], tmp_path / "never_initialized")
    assert results["stale_lease"].status == "skip"


def test_stale_lease_passes_on_clean_store(store, program_root):
    ledger.enqueue(store, kind="custom", payload={})
    results = _run(["stale_lease"], program_root)
    assert results["stale_lease"].status == "pass"


def test_stale_lease_warns_on_expired_lease(store, program_root):
    job = ledger.enqueue(store, kind="custom", payload={})
    ledger.claim_next(store, worker_id="dead:ts1")
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET lease_expires_ts = '2000-01-01T00:00:00.000Z' WHERE job_id = ?", (job["job_id"],)
        )
    results = _run(["stale_lease"], program_root)
    r = results["stale_lease"]
    assert r.status == "warn"
    assert r.details["jobs"][0]["job_id"] == job["job_id"]


def test_heartbeat_age_skips_when_jobs_db_absent(tmp_path):
    results = _run(["heartbeat_age"], tmp_path / "never_initialized")
    assert results["heartbeat_age"].status == "skip"


def test_heartbeat_age_passes_on_freshly_claimed_job(store, program_root):
    job = ledger.enqueue(store, kind="custom", payload={})
    ledger.claim_next(store, worker_id="w1")
    results = _run(["heartbeat_age"], program_root)
    assert results["heartbeat_age"].status == "pass"


def test_heartbeat_age_warns_on_old_heartbeat_within_a_still_valid_lease(store, program_root):
    """Distinct from ``stale_lease``: the lease itself has NOT expired, but
    the heartbeat is old -- an early-warning signal, not a reclaim-ready
    one."""
    job = ledger.enqueue(store, kind="custom", payload={})
    ledger.claim_next(store, worker_id="w1", lease_s=999_999)  # lease far in the future
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET heartbeat_ts = '2000-01-01T00:00:00.000Z' WHERE job_id = ?", (job["job_id"],)
        )
    results = _run(["heartbeat_age"], program_root)
    r = results["heartbeat_age"]
    assert r.status == "warn"
    assert r.details["jobs"][0]["job_id"] == job["job_id"]
    # and stale_lease must NOT also fire for this same job -- the lease is
    # still valid, only the heartbeat looks old.
    stale_lease_result = _run(["stale_lease"], program_root)["stale_lease"]
    assert stale_lease_result.status == "pass"
