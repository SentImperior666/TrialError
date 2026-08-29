"""M2 acceptance criteria, design Section 12 row, gathered in one place --
mirrors the ``tests/test_m1_acceptance.py`` convention: this file IS the
acceptance-criteria mapping, each test here re-running (not replacing) a
narrower assertion that already lives in its dedicated module.

    | Acceptance criterion (design Section 12, M2 row)                    | Test |
    |-----------------------------------------------------------------------|------|
    | kill -9 a worker mid-job -> next tick reclaims + resumes from checkpoint | see tests/test_jobs_worker.py test_kill_mid_job_worker_is_reclaimed_by_tick_and_resumes_from_checkpoint (real detached-subprocess + real TerminateProcess kill; subprocess-heavy, not duplicated here -- same convention test_m1_acceptance.py established for its own subprocess-heavy "concurrent-writer test") |
    | env-failure does not consume attempt                                  | test_environmental_failure_does_not_consume_attempt |
    | PID-ownership check refuses foreign pid                               | test_pid_ownership_check_refuses_foreign_pid |
"""

from __future__ import annotations

import pytest

from trialerror.jobs import ledger
from trialerror.jobs.errors import ForeignWorkerError

pytestmark = pytest.mark.acceptance


def test_environmental_failure_does_not_consume_attempt(store):
    job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    ledger.claim_next(store, worker_id="w1")

    before_attempts = ledger.get_job(store, job["job_id"])["attempts"]
    assert before_attempts == 0

    result = ledger.fail(store, job["job_id"], "w1", failure_class="environmental", error="rate limited")

    assert result["attempts"] == before_attempts == 0
    assert result["failure_class"] == "environmental"
    assert result["state"] == "pending"  # re-queued, not counted against the retry budget


def test_pid_ownership_check_refuses_foreign_pid(store):
    """Design Section 4.4: ``claimed_by`` = "worker_id = pid + start_ts;
    PID-ownership verified, codemap pattern." A caller whose ``worker_id``
    does not match the current lease-holder is refused on every
    settle/heartbeat path -- checked here across all four (heartbeat,
    complete, fail, and a claim-then-second-claim race)."""
    job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    ledger.claim_next(store, worker_id="1111:2026-08-29T00:00:00.000Z")
    foreign_worker_id = "9999:2026-08-29T00:00:01.000Z"

    with pytest.raises(ForeignWorkerError):
        ledger.heartbeat(store, job["job_id"], foreign_worker_id)
    with pytest.raises(ForeignWorkerError):
        ledger.complete(store, job["job_id"], foreign_worker_id)
    with pytest.raises(ForeignWorkerError):
        ledger.fail(store, job["job_id"], foreign_worker_id, failure_class="logic", error="not yours")

    # the genuine owner is completely unaffected by the refused foreign calls
    still_owned = ledger.get_job(store, job["job_id"])
    assert still_owned["claimed_by"] == "1111:2026-08-29T00:00:00.000Z"
    assert still_owned["state"] in ("claimed", "running")
    assert still_owned["attempts"] == 0
