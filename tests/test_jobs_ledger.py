"""Unit tests for ``trialerror.jobs.ledger``: claim/lease/heartbeat/backoff/
failure-class, pause/resume, and lease reclaim. Design Section 12 (M2 row)
acceptance criteria covered here: "env-failure does not consume attempt"
(:func:`test_environmental_failure_does_not_consume_attempt`) and
"PID-ownership check refuses foreign pid"
(:func:`test_heartbeat_foreign_worker_refused`,
:func:`test_complete_foreign_worker_refused`,
:func:`test_fail_foreign_worker_refused`). The third criterion ("kill -9 a
worker mid-job -> next tick reclaims + resumes from checkpoint") needs a
real OS process and lives in ``tests/test_jobs_worker.py``; this file
exercises the SAME reclaim primitive (:func:`trialerror.jobs.ledger.sweep_expired_leases`)
at the ledger level, deterministically, without a subprocess."""

from __future__ import annotations

import json

import pytest

from trialerror.jobs import ledger
from trialerror.jobs.errors import (
    ForeignWorkerError,
    InvalidTransitionError,
    JobNotFoundError,
    JobPausedError,
    NotClaimableError,
)
from trialerror.util.ids import new_id


def _enqueue(store, **kw):
    kw.setdefault("kind", "custom")
    kw.setdefault("payload", {"handler": "noop"})
    return ledger.enqueue(store, **kw)


def test_enqueue_creates_pending_job(store):
    job = _enqueue(store)
    assert job["state"] == "pending"
    assert job["attempts"] == 0
    assert job["max_attempts"] == ledger.DEFAULT_MAX_ATTEMPTS
    events = ledger.list_events(store, job["job_id"])
    assert [e["type"] for e in events] == ["enqueued"]


def test_claim_next_claims_oldest_pending_and_transitions_state(store):
    job = _enqueue(store)
    claimed = ledger.claim_next(store, worker_id="111:ts1")
    assert claimed["job_id"] == job["job_id"]
    assert claimed["state"] == "claimed"
    assert claimed["claimed_by"] == "111:ts1"
    assert claimed["lease_expires_ts"] is not None
    assert claimed["heartbeat_ts"] is not None
    fetched = ledger.get_job(store, job["job_id"])
    assert fetched["state"] == "claimed"


def test_claim_next_returns_none_when_nothing_eligible(store):
    assert ledger.claim_next(store, worker_id="111:ts1") is None


def test_claim_next_respects_kind_filter(store):
    _enqueue(store, kind="ocr", payload={})
    embed_job = _enqueue(store, kind="embed", payload={})
    claimed = ledger.claim_next(store, kinds=["embed"], worker_id="111:ts1")
    assert claimed["job_id"] == embed_job["job_id"]
    assert claimed["kind"] == "embed"


def test_double_claim_is_prevented(store):
    """The design's named "conditional UPDATE (no double-claim)" property:
    once worker A holds an unexpired lease, worker B's claim attempt finds
    nothing eligible."""
    job = _enqueue(store)
    first = ledger.claim_next(store, worker_id="111:ts1")
    assert first["job_id"] == job["job_id"]
    second = ledger.claim_next(store, worker_id="222:ts2")
    assert second is None
    assert ledger.get_job(store, job["job_id"])["claimed_by"] == "111:ts1"


def test_claim_specific_returns_none_when_not_eligible(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")  # now claimed, unexpired lease
    assert ledger.claim_specific(store, job["job_id"], worker_id="222:ts2") is None


def test_claim_or_create_creates_then_claims(store):
    jid = new_id("JOB")
    claimed = ledger.claim_or_create(
        store, jid, kind="custom", payload={"handler": "noop"}, worker_id="111:ts1"
    )
    assert claimed["job_id"] == jid
    assert claimed["state"] == "claimed"
    assert json.loads(claimed["payload"]) == {"handler": "noop"}


def test_claim_or_create_on_existing_but_unclaimable_job_raises(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")  # now held by another worker
    with pytest.raises(NotClaimableError):
        ledger.claim_or_create(store, job["job_id"], kind="custom", payload={}, worker_id="222:ts2")


def test_heartbeat_renews_lease_and_transitions_claimed_to_running(store):
    job = _enqueue(store)
    claimed = ledger.claim_next(store, worker_id="111:ts1")
    assert claimed["state"] == "claimed"
    beat = ledger.heartbeat(store, job["job_id"], "111:ts1")
    assert beat["state"] == "running"
    assert beat["lease_expires_ts"] >= claimed["lease_expires_ts"]


def test_heartbeat_persists_checkpoint(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    beat = ledger.heartbeat(store, job["job_id"], "111:ts1", checkpoint={"completed_steps": 3})
    assert json.loads(beat["checkpoint"]) == {"completed_steps": 3}
    # a heartbeat with no checkpoint arg leaves the prior checkpoint untouched
    beat2 = ledger.heartbeat(store, job["job_id"], "111:ts1")
    assert json.loads(beat2["checkpoint"]) == {"completed_steps": 3}


def test_heartbeat_foreign_worker_refused(store):
    """PID-ownership check refuses foreign pid (design Section 4.4 /
    codemap-pattern acceptance criterion)."""
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    with pytest.raises(ForeignWorkerError):
        ledger.heartbeat(store, job["job_id"], "999:ts9")
    # the legitimate owner is unaffected
    assert ledger.get_job(store, job["job_id"])["claimed_by"] == "111:ts1"


def test_heartbeat_unknown_job_raises_not_found(store):
    with pytest.raises(JobNotFoundError):
        ledger.heartbeat(store, "JOB-nonexistent", "111:ts1")


def test_heartbeat_on_paused_job_raises_job_paused_error(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    ledger.pause(store, job["job_id"])
    with pytest.raises(JobPausedError):
        ledger.heartbeat(store, job["job_id"], "111:ts1")


def test_complete_settles_job_and_releases_lease(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    done = ledger.complete(store, job["job_id"], "111:ts1")
    assert done["state"] == "complete"
    assert done["claimed_by"] is None
    assert done["lease_expires_ts"] is None
    assert done["settled_ts"] is not None
    events = [e["type"] for e in ledger.list_events(store, job["job_id"])]
    assert events == ["enqueued", "claimed", "completed"]


def test_complete_foreign_worker_refused(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    with pytest.raises(ForeignWorkerError):
        ledger.complete(store, job["job_id"], "999:ts9")


def test_environmental_failure_does_not_consume_attempt(store):
    """Design Section 4.4 acceptance criterion, verbatim."""
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    result = ledger.fail(store, job["job_id"], "111:ts1", failure_class="environmental", error="gpu busy")
    assert result["attempts"] == 0
    assert result["state"] == "pending"
    assert result["failure_class"] == "environmental"
    assert result["next_attempt_ts"] is not None
    assert result["claimed_by"] is None
    # and it's claimable again once next_attempt_ts elapses -- simulate by
    # rewinding next_attempt_ts (test-only direct write; the ledger itself
    # exposes no "unschedule a defer" API on purpose).
    with store.jobs:
        store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE job_id = ?", (job["job_id"],))
    reclaimed = ledger.claim_next(store, worker_id="222:ts2")
    assert reclaimed["job_id"] == job["job_id"]
    assert reclaimed["attempts"] == 0


def test_logic_failure_backoff_then_abandon_after_max_attempts(store):
    job = _enqueue(store, max_attempts=3)
    jid = job["job_id"]

    ledger.claim_next(store, worker_id="w1")
    r1 = ledger.fail(store, jid, "w1", failure_class="logic", error="boom 1")
    assert r1["attempts"] == 1
    assert r1["state"] == "failed"
    assert r1["failure_class"] == "logic"
    assert r1["next_attempt_ts"] is not None
    # not yet claimable: backoff window hasn't elapsed
    assert ledger.claim_next(store, worker_id="w2") is None
    # force the backoff window open (test-only direct write)
    with store.jobs:
        store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE job_id = ?", (jid,))

    ledger.claim_next(store, worker_id="w2")
    r2 = ledger.fail(store, jid, "w2", failure_class="logic", error="boom 2")
    assert r2["attempts"] == 2
    assert r2["state"] == "failed"
    with store.jobs:
        store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE job_id = ?", (jid,))

    ledger.claim_next(store, worker_id="w3")
    r3 = ledger.fail(store, jid, "w3", failure_class="logic", error="boom 3")
    assert r3["attempts"] == 3
    assert r3["state"] == "abandoned"
    assert r3["next_attempt_ts"] is None
    assert r3["settled_ts"] is not None
    # terminal: never claimable again
    assert ledger.claim_next(store, worker_id="w4") is None

    event_types = [e["type"] for e in ledger.list_events(store, jid)]
    assert event_types == ["enqueued", "claimed", "retry_scheduled", "claimed", "retry_scheduled", "claimed", "abandoned"]


def test_fail_foreign_worker_refused(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    with pytest.raises(ForeignWorkerError):
        ledger.fail(store, job["job_id"], "999:ts9", failure_class="logic", error="not yours")


def test_backoff_seconds_matches_spec():
    """Design Section 4.4: "exponential backoff (60s base, 1h cap)"."""
    assert ledger.backoff_seconds(1) == 60
    assert ledger.backoff_seconds(2) == 120
    assert ledger.backoff_seconds(3) == 240
    assert ledger.backoff_seconds(10) == 3600  # capped
    assert ledger.backoff_seconds(20) == 3600  # still capped


def test_pause_then_resume_roundtrip(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    paused = ledger.pause(store, job["job_id"])
    assert paused["state"] == "paused"
    resumed = ledger.resume(store, job["job_id"])
    assert resumed["state"] == "pending"
    assert resumed["next_attempt_ts"] is None
    # claimable again by anyone
    claimed = ledger.claim_next(store, worker_id="222:ts2")
    assert claimed["job_id"] == job["job_id"]


def test_pause_is_idempotent_when_already_paused(store):
    job = _enqueue(store)
    first = ledger.pause(store, job["job_id"])
    second = ledger.pause(store, job["job_id"])
    assert first["state"] == second["state"] == "paused"


def test_pause_refused_on_terminal_state(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")
    ledger.complete(store, job["job_id"], "111:ts1")
    with pytest.raises(InvalidTransitionError):
        ledger.pause(store, job["job_id"])


def test_resume_refused_when_not_paused(store):
    job = _enqueue(store)
    with pytest.raises(InvalidTransitionError):
        ledger.resume(store, job["job_id"])


def test_sweep_expired_leases_reclaims_and_preserves_checkpoint(store):
    """The ledger-level half of "next tick reclaims + resumes from
    checkpoint" -- deterministic, no subprocess (see
    ``tests/test_jobs_worker.py`` for the real-kill version)."""
    job = _enqueue(store)
    jid = job["job_id"]
    ledger.claim_next(store, worker_id="dead-worker:ts1", lease_s=1)
    ledger.heartbeat(store, jid, "dead-worker:ts1", lease_s=1, checkpoint={"completed_steps": 4})

    # simulate the lease having expired (test-only direct write, instead of
    # sleeping past a real lease_s=1 -- deterministic and instant).
    with store.jobs:
        store.jobs.execute("UPDATE job SET lease_expires_ts = '2000-01-01T00:00:00.000Z' WHERE job_id = ?", (jid,))

    reclaimed = ledger.sweep_expired_leases(store)
    assert len(reclaimed) == 1
    assert reclaimed[0]["job_id"] == jid
    assert reclaimed[0]["state"] == "pending"
    assert reclaimed[0]["claimed_by"] is None
    assert json.loads(reclaimed[0]["checkpoint"]) == {"completed_steps": 4}

    # claimable again, and the checkpoint survived the round trip
    claimed = ledger.claim_next(store, worker_id="new-worker:ts2")
    assert claimed["job_id"] == jid
    assert json.loads(claimed["checkpoint"]) == {"completed_steps": 4}
    assert claimed["attempts"] == 0  # a crash is not counted as a failed attempt

    event_types = [e["type"] for e in ledger.list_events(store, jid)]
    assert "reclaimed" in event_types


def test_sweep_expired_leases_is_a_noop_when_nothing_expired(store):
    job = _enqueue(store)
    ledger.claim_next(store, worker_id="111:ts1")  # fresh, unexpired lease
    assert ledger.sweep_expired_leases(store) == []
    assert ledger.get_job(store, job["job_id"])["state"] == "claimed"


def test_list_jobs_filters_by_state_and_kind(store):
    a = _enqueue(store, kind="ocr", payload={})
    b = _enqueue(store, kind="embed", payload={})
    ledger.claim_next(store, worker_id="w1", kinds=["ocr"])
    pending_only = ledger.list_jobs(store, state="pending")
    assert [j["job_id"] for j in pending_only] == [b["job_id"]]
    ocr_only = ledger.list_jobs(store, kind="ocr")
    assert [j["job_id"] for j in ocr_only] == [a["job_id"]]
