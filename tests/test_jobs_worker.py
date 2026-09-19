"""Tests for ``trialerror.jobs.worker``: the claim-run-settle loop
(:func:`run_one`/:func:`run_loop`) and the detached-process launcher
(:func:`spawn_worker`). Design Section 12 (M2 row) acceptance criterion
covered here: "kill -9 a worker mid-job -> next tick reclaims + resumes
from checkpoint" --
:func:`test_kill_mid_job_worker_is_reclaimed_by_tick_and_resumes_from_checkpoint`,
the one genuinely subprocess-heavy test in this module (real
``DETACHED_PROCESS``/``CREATE_NEW_PROCESS_GROUP`` spawn, real
``TerminateProcess`` kill via ``Popen.kill()`` -- the Windows analog of
``kill -9``: abrupt termination, no cleanup handler runs)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import tests._job_handlers  # noqa: F401 - registers test_* handlers in THIS process (module-level
# dict, mirrors trialerror.util.doctor's check registry -- registration is
# idempotent and process-lifetime, no per-test reset needed since nothing
# in this module clears the registry mid-run).
from trialerror.jobs import ledger
from trialerror.jobs.worker import make_worker_id, run_loop, run_one, spawn_worker

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_one_idle_when_queue_empty(store):
    result = run_one(store, worker_id=make_worker_id())
    assert result == {"status": "idle", "worker_id": result["worker_id"]}


def test_run_one_completes_noop_handler(store):
    job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    result = run_one(store, worker_id=make_worker_id())
    assert result["status"] == "complete"
    assert result["job_id"] == job["job_id"]
    final = ledger.get_job(store, job["job_id"])
    assert final["state"] == "complete"
    assert json.loads(final["checkpoint"]) == {"ran": True}


def test_run_one_custom_job_missing_handler_key_settles_as_failed(store):
    job = ledger.enqueue(store, kind="custom", payload={})
    result = run_one(store, worker_id=make_worker_id())
    assert result["status"] == "failed"
    final = ledger.get_job(store, job["job_id"])
    assert final["state"] == "failed"
    assert final["attempts"] == 1


def test_run_one_unknown_handler_name_settles_as_failed_not_a_crash(store):
    job = ledger.enqueue(store, kind="custom", payload={"handler": "does_not_exist"})
    result = run_one(store, worker_id=make_worker_id())
    assert result["status"] == "failed"
    assert ledger.get_job(store, job["job_id"])["attempts"] == 1


def test_run_one_environmental_failure_defers_without_consuming_attempt(store):
    ledger.enqueue(store, kind="custom", payload={"handler": "test_environmental_failure", "reason": "gpu busy"})
    result = run_one(store, worker_id=make_worker_id())
    assert result["status"] == "deferred"
    job = ledger.get_job(store, result["job_id"])
    assert job["attempts"] == 0
    assert job["state"] == "pending"


def test_run_one_logic_failure_settles_failed_then_abandoned(store):
    job = ledger.enqueue(
        store, kind="custom", payload={"handler": "test_always_fails", "message": "nope"}, max_attempts=2
    )
    jid = job["job_id"]

    r1 = run_one(store, worker_id=make_worker_id())
    assert r1["status"] == "failed"
    with store.jobs:
        store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE job_id = ?", (jid,))

    r2 = run_one(store, worker_id=make_worker_id())
    assert r2["status"] == "abandoned"
    assert ledger.get_job(store, jid)["state"] == "abandoned"


def test_run_one_pending_job_paused_before_claim_is_not_claimable(store):
    """A job paused before it was ever claimed simply isn't eligible --
    ``run_one`` finds nothing to do (idle), the same as any other
    ineligible job. Pause's REAL effect on an in-flight run is covered by
    :func:`test_run_one_job_paused_mid_run_returns_paused_status`."""
    job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    ledger.pause(store, job["job_id"])
    result = run_one(store, worker_id=make_worker_id())
    assert result["status"] == "idle"
    assert ledger.get_job(store, job["job_id"])["state"] == "paused"


def test_run_one_job_paused_mid_run_returns_paused_status(store):
    """The unit-level version of the cooperative-pause contract: a handler
    that gets paused out from under it (here, deterministically, by
    pausing itself mid-run via ``test_pauses_itself`` -- simulating what an
    operator's ``trialerror jobs pause`` from another process would do to a live
    detached worker) sees ``JobPausedError`` at its next heartbeat, and
    ``run_one`` translates that into ``status: "paused"`` without raising."""
    job = ledger.enqueue(store, kind="custom", payload={"handler": "test_pauses_itself"})
    result = run_one(store, worker_id=make_worker_id())
    assert result["status"] == "paused"
    final = ledger.get_job(store, job["job_id"])
    assert final["state"] == "paused"
    # the checkpoint written just BEFORE the pause took effect is preserved;
    # the one after (unreachable) never got written.
    assert json.loads(final["checkpoint"]) == {"phase": "before_pause"}


def test_run_loop_drains_queue_and_stops_when_idle(store):
    for _ in range(3):
        ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    results = run_loop(store, poll_interval_s=0.01, max_idle_polls=2)
    statuses = [r["status"] for r in results]
    assert statuses.count("complete") == 3
    assert statuses[-2:] == ["idle", "idle"]
    assert len(ledger.list_jobs(store, state="complete")) == 3


def test_run_loop_respects_max_iterations(store):
    for _ in range(5):
        ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    results = run_loop(store, poll_interval_s=0.01, max_iterations=2, max_idle_polls=99)
    non_idle = [r for r in results if r["status"] != "idle"]
    assert len(non_idle) == 2
    assert len(ledger.list_jobs(store, state="pending")) == 3


def test_spawn_worker_detaches_the_child_on_every_platform(store, program_root, platform_root, monkeypatch):
    """Doesn't actually spawn a real process -- asserts the Popen call
    site requests DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP on Windows,
    the design's explicit M2 acceptance shape ("detached worker launcher
    (DETACHED_PROCESS on Win)"), without the cost/flakiness of a real
    subprocess for a test that's really about the flags."""
    import subprocess as subprocess_mod

    captured = {}

    class _FakeProc:
        pid = 424242

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess_mod, "Popen", _fake_popen)
    job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    handle = spawn_worker(program_root=program_root, platform_root=platform_root, job_id=job["job_id"])

    assert handle.pid == 424242
    if sys.platform == "win32":
        assert captured["kwargs"]["creationflags"] == (
            subprocess_mod.DETACHED_PROCESS | subprocess_mod.CREATE_NEW_PROCESS_GROUP
        )
    else:
        # The POSIX arm used to be asserted by nothing, so on Linux this test
        # proved only that argv was right -- a regression dropping detachment
        # entirely would have passed. setsid() is what makes the worker
        # outlive the launching shell.
        assert captured["kwargs"]["start_new_session"] is True
        assert "creationflags" not in captured["kwargs"]
    assert "--foreground" in captured["argv"]
    assert "--job-id" in captured["argv"]
    assert job["job_id"] in captured["argv"]
    assert handle.log_path.parent == program_root / "jobs_logs"


# ---------------------------------------------------------------------------
# The flagship acceptance test: a real detached OS process, killed abruptly
# mid-job, reclaimed by a tick, and resumed from its last durable checkpoint.
# ---------------------------------------------------------------------------


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + existing if existing else "")
    return env


def test_kill_mid_job_worker_is_reclaimed_by_tick_and_resumes_from_checkpoint(store, program_root, platform_root):
    """Design Section 12, M2 row, acceptance criterion (verbatim): "kill -9
    a worker mid-job -> next tick reclaims + resumes from checkpoint".

    1. spawn a REAL detached worker (DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP)
       running a slow, checkpointed multi-step handler against a short lease;
    2. poll the ledger until it shows partial (but not full) progress;
    3. ``TerminateProcess`` it (``Popen.kill()``) -- no graceful shutdown,
       no chance to release its lease or mark itself failed;
    4. assert the job is left ``claimed``/``running`` with a PARTIAL
       checkpoint (the crash left no trace of an orderly settlement);
    5. wait past the short lease, call the ledger's tick equivalent
       (``sweep_expired_leases`` -- what ``trialerror jobs tick`` calls) and
       assert the job is reclaimed to ``pending`` with its checkpoint intact;
    6. resume it (in-process ``run_one``, same ledger, same handler code
       path -- resumption is process-agnostic) and assert it completes,
       finishing EXACTLY the steps the first run didn't, not restarting
       from zero (checked structurally: the final checkpoint count, AND
       -- FX-5 -- the exact number of NEW checkpoint-update ``job_event``
       rows round 2 writes, which can only equal ``total_steps -
       crashed_checkpoint`` if it truly resumed rather than replayed;
       see the inline TRIALERROR-DEV-NOTE at that assertion for why this
       replaced an earlier wall-clock-margin assertion).
    """
    total_steps = 8
    step_delay_s = 0.3
    job_id = "JOB-killtest"
    job = ledger.enqueue(
        store,
        kind="custom",
        payload={"handler": "test_step_counter", "total_steps": total_steps, "step_delay_s": step_delay_s},
        job_id=job_id,
    )
    assert job["job_id"] == job_id

    handle = spawn_worker(
        program_root=program_root,
        platform_root=platform_root,
        job_id=job_id,
        mode="once",
        lease_s=2,  # short lease: fast, deterministic reclaim without a real 15-min wait
        extra_handler_modules=["tests._job_handlers"],
        env=_subprocess_env(),
    )

    # Poll until real, partial progress is visible -- bounded wait so a
    # startup hiccup fails the test loudly instead of hanging forever.
    deadline = time.time() + 10.0
    completed_before_kill = 0
    while time.time() < deadline:
        row = ledger.get_job(store, job_id)
        if row["checkpoint"]:
            completed_before_kill = json.loads(row["checkpoint"]).get("completed_steps", 0)
        if completed_before_kill > 0:
            break
        time.sleep(0.05)
    assert 0 < completed_before_kill < total_steps, (
        f"expected to observe PARTIAL progress before killing (got {completed_before_kill} "
        f"of {total_steps}) -- either the worker never started or it finished before we could kill it"
    )

    # The kill -9 analog on Windows: TerminateProcess, no cleanup.
    handle.process.kill()
    handle.process.wait(timeout=5)

    crashed = ledger.get_job(store, job_id)
    assert crashed["state"] in ("claimed", "running")
    assert crashed["claimed_by"] is not None  # the dead worker's lease is still on record
    crashed_checkpoint = json.loads(crashed["checkpoint"])["completed_steps"]
    assert crashed_checkpoint >= completed_before_kill  # monotonic; possibly one more step slipped in before the kill landed
    assert crashed_checkpoint < total_steps

    # Wait past the short lease, then reclaim -- exactly what `trialerror jobs
    # tick` does.
    time.sleep(2.2)
    reclaimed = ledger.sweep_expired_leases(store)
    assert [r["job_id"] for r in reclaimed] == [job_id]
    after_reclaim = ledger.get_job(store, job_id)
    assert after_reclaim["state"] == "pending"
    assert after_reclaim["claimed_by"] is None
    assert json.loads(after_reclaim["checkpoint"])["completed_steps"] == crashed_checkpoint
    assert after_reclaim["attempts"] == 0  # a crash never consumes a retry attempt

    # Resume: same ledger, same handler, a fresh run_one call (process
    # identity doesn't matter to the resumption contract -- the checkpoint
    # in jobs.db is the only thing that does).
    #
    # FX-5 (IMPL_REVIEW_VERDICT.md Tier 2 / IMPL_REVIEW_C_ops.md N-1 /
    # INTEGRATION_NOTES.md item 17): this used to assert
    # `round2_elapsed < full_replay_duration - (step_delay_s * 0.5)` -- a
    # ~150ms wall-clock margin that failed intermittently under build-host
    # load (observed ~1/3 of full-suite runs, round2_elapsed consistently
    # right at the 2.25s edge). Re-anchored on CHECKPOINT-SKIP EVIDENCE
    # instead: every `ctx.set_checkpoint(...)` call durably records progress
    # via `trialerror.jobs.ledger.heartbeat`, which logs exactly one
    # `job_event(type='heartbeat', detail={'checkpoint_updated': True})` row
    # per call. If round 2 genuinely RESUMED from `crashed_checkpoint`
    # (the handler's own `start = ctx.checkpoint.get('completed_steps', 0)`
    # contract) rather than replaying from zero, it can only write
    # `total_steps - crashed_checkpoint` NEW checkpoint-update events --
    # structurally fewer than the `total_steps` a from-scratch run would
    # write. This proves the same "it actually skipped completed work" fact
    # the old wall-clock margin was reaching for, deterministically, with no
    # scheduling-jitter sensitivity.
    def _checkpoint_update_count() -> int:
        events = ledger.list_events(store, job_id, limit=1000)
        return sum(
            1
            for e in events
            if e["type"] == "heartbeat" and json.loads(e["detail"] or "{}").get("checkpoint_updated")
        )

    checkpoint_writes_before_round2 = _checkpoint_update_count()

    round2_start = time.perf_counter()
    result = run_one(store, job_id=job_id, worker_id=make_worker_id(), lease_s=ledger.LEASE_DURATION_S)
    round2_elapsed = time.perf_counter() - round2_start  # informational only -- see the note above

    assert result["status"] == "complete"
    final = ledger.get_job(store, job_id)
    assert final["state"] == "complete"
    assert json.loads(final["checkpoint"])["completed_steps"] == total_steps

    checkpoint_writes_in_round2 = _checkpoint_update_count() - checkpoint_writes_before_round2
    expected_resumed_steps = total_steps - crashed_checkpoint
    assert checkpoint_writes_in_round2 == expected_resumed_steps, (
        f"round 2 wrote {checkpoint_writes_in_round2} checkpoint update(s), expected exactly "
        f"{expected_resumed_steps} (= total_steps {total_steps} - the {crashed_checkpoint} step(s) "
        f"already durable at reclaim); a from-scratch replay would have written {total_steps} "
        f"(round2_elapsed={round2_elapsed:.2f}s, informational only)"
    )
    assert checkpoint_writes_in_round2 < total_steps, (
        f"round 2 wrote as many checkpoint updates ({checkpoint_writes_in_round2}) as a full "
        f"from-scratch replay ({total_steps}) would -- looks like it restarted instead of resuming "
        f"(round2_elapsed={round2_elapsed:.2f}s, informational only)"
    )
