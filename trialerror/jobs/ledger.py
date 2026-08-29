"""The durable execution ledger. Design Section 4.4 (jobs.db) + Section 12
(M2 row): "claim/lease/heartbeat/backoff/failure-class"; ported from
atomic's ``scheduler/ledger.rs`` (docs/mining/G21-docstruct-2__atomic.md):
"``claim_or_create`` is a conditional UPDATE (no double-claim); heartbeat
every 5min renews a 15min lease; a crashed worker's row is reclaimed by the
next ``trialerror jobs tick``, resuming from ``checkpoint``. Environmental
failures ... ``defer_until`` WITHOUT consuming an attempt ... logic
failures consume an attempt with exponential backoff (60s base, 1h cap)."

**Atomicity, precisely.** Every state transition below is exactly ONE
``UPDATE ... WHERE <ownership/eligibility predicate> RETURNING *``
statement. SQLite takes its write lock for a statement's whole execution
(WAL mode: one writer at a time, serialized via ``busy_timeout``), so the
eligibility check and the write happen as one atomic unit -- there is no
separate "SELECT to check, then UPDATE" window for two callers to race
through (the exact "conditional UPDATE" shape the design names). A zero-row
``RETURNING`` result means the predicate didn't match anything, at which
point a follow-up read distinguishes *why* (not found / foreign owner /
paused / wrong state) purely for a clear exception message -- never for
the transition decision itself.

**State machine** (job.state, per the Section 4.4 DDL CHECK constraint):

    pending --claim--> claimed --heartbeat--> running --complete--> complete
       ^                  |                      |
       |                  `--- (env failure) -----+---------> pending (next_attempt_ts, attempts UNCHANGED)
       |                  |                      |
       |                  `--- (logic failure, attempts+1 < max) --> failed --(next_attempt_ts elapses)--> [claimable again]
       |                                          |
       `--- (logic failure, attempts+1 >= max) ---+---------> abandoned (terminal)
       |
       `<--- resume ---- paused <--- pause ---- (claimed | running | pending | failed)

``claimed``/``running`` whose ``lease_expires_ts`` has passed are reclaimed
by :func:`sweep_expired_leases` (``trialerror jobs tick``) back to ``pending``,
``checkpoint`` untouched -- the crashed-worker recovery path.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Sequence

from trialerror.jobs.errors import (
    ForeignWorkerError,
    InvalidTransitionError,
    JobNotFoundError,
    JobPausedError,
    NotClaimableError,
)
from trialerror.stores.errors import ValidationError
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now, parse

__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "LEASE_DURATION_S",
    "DEFAULT_MAX_ATTEMPTS",
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "ENV_RETRY_DEFAULT_S",
    "enqueue",
    "get_job",
    "list_jobs",
    "list_events",
    "claim_next",
    "claim_specific",
    "claim_or_create",
    "heartbeat",
    "complete",
    "fail",
    "pause",
    "resume",
    "sweep_expired_leases",
    "backoff_seconds",
]

#: design Section 4.4: "heartbeat every 5min renews a 15min lease".
HEARTBEAT_INTERVAL_S = 300
LEASE_DURATION_S = 900

#: matches the ``job.max_attempts`` DDL default (design Section 4.4).
DEFAULT_MAX_ATTEMPTS = 3

#: design Section 4.4: "exponential backoff (60s base, 1h cap)".
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 3600

#: Not spec'd by name in Section 4.4 (only "defer_until" is named, with no
#: stated default window) -- a reasonable default defer delay when a
#: handler raises ``EnvironmentalFailure`` without stating its own
#: ``retry_delay_s``. Kept as a named constant (not a magic number) so a
#: later module can tune it without hunting through ``fail()``.
ENV_RETRY_DEFAULT_S = 30


def _plus_seconds(ts: str, seconds: float) -> str:
    """``ts`` (a ``trialerror.util.timeutil.now()``-shaped string) plus
    ``seconds``, re-rendered in the same format. Duplicates ``now()``'s
    private millisecond-formatting (4 lines) rather than importing a
    private helper from ``trialerror.util.timeutil`` -- that module is outside
    this build's lane (M0-owned); this keeps the lane boundary honest at
    the cost of a small, self-contained duplication. See the M2 build
    report's deviations note."""
    dt = parse(ts) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff for the ``attempts``-th logic-failure retry
    (1-indexed: the delay scheduled *after* the attempt that brought the
    counter to ``attempts``), base 60s doubling, capped at 3600s -- design
    Section 4.4 verbatim, ported from atomic's ``BACKOFF_BASE``/
    ``BACKOFF_CAP``."""
    return min(BACKOFF_BASE_S * (2 ** (attempts - 1)), BACKOFF_CAP_S)


#: Shared by every claim path (:func:`claim_next`, :func:`claim_specific`):
#: a job is claimable iff it has never been tried (``pending``), is a
#: logic-failure awaiting its backoff retry (``failed`` with budget left),
#: or is sitting under an expired lease (crashed-worker reclaim) -- AND, in
#: every case, any scheduled ``next_attempt_ts``/defer window has elapsed.
#: Bound via SQLite's named-parameter ``:now`` (millisecond ISO-8601 UTC
#: strings sort and compare correctly as plain SQL string comparisons --
#: the same trick ``trialerror.stores.bitemporal`` uses for its temporal
#: predicates).
_ELIGIBLE_PREDICATE = (
    "("
    "  state = 'pending'"
    "  OR (state = 'failed' AND attempts < max_attempts)"
    "  OR (state IN ('claimed', 'running') AND lease_expires_ts IS NOT NULL AND lease_expires_ts < :now)"
    ")"
    " AND (next_attempt_ts IS NULL OR next_attempt_ts <= :now)"
)


def _log_event(store: Store, job_id: str, type_: str, detail: dict[str, Any] | None) -> None:
    insert(
        store,
        "job_event",
        {
            "job_id": job_id,
            "ts": now(),
            "type": type_,
            "detail": json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        },
    )


def get_job(store: Store, job_id: str) -> dict[str, Any] | None:
    row = store.jobs.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def list_jobs(
    store: Store, *, state: str | None = None, kind: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.jobs.execute(
        f"SELECT * FROM job {where} ORDER BY created_ts DESC LIMIT ?", (*params, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def list_events(store: Store, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = store.jobs.execute(
        "SELECT * FROM job_event WHERE job_id = ? ORDER BY id ASC LIMIT ?", (job_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def enqueue(
    store: Store,
    *,
    kind: str,
    payload: dict[str, Any],
    job_id: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Create a new ``pending`` job row. Plain validated insert (via
    ``trialerror.stores.writer.insert`` -- no business-logic conditional needed
    for a brand-new row); the ledger's atomicity concerns start at claim
    time, not creation time."""
    jid = job_id or new_id("JOB")
    insert(
        store,
        "job",
        {
            "job_id": jid,
            "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False),
            "state": "pending",
            "max_attempts": max_attempts,
            "created_ts": now(),
        },
    )
    _log_event(store, jid, "enqueued", {"kind": kind})
    row = get_job(store, jid)
    assert row is not None  # just inserted, inside the same connection
    return row


def claim_specific(store: Store, job_id: str, *, worker_id: str, lease_s: int = LEASE_DURATION_S) -> dict[str, Any] | None:
    """Atomically claim ``job_id`` iff it is currently eligible (see
    :data:`_ELIGIBLE_PREDICATE`). Returns ``None`` (never raises) when it
    isn't -- claim failure is an ordinary, expected outcome (another
    worker won the race, or nothing is due yet), not an error."""
    now_s = now()
    lease_until = _plus_seconds(now_s, lease_s)
    sql = f"""
        UPDATE job
        SET state = 'claimed', claimed_by = :worker_id,
            lease_expires_ts = :lease_until, heartbeat_ts = :now
        WHERE job_id = :job_id AND {_ELIGIBLE_PREDICATE}
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(
            sql, {"job_id": job_id, "worker_id": worker_id, "lease_until": lease_until, "now": now_s}
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    _log_event(store, job_id, "claimed", {"worker_id": worker_id})
    return result


def claim_next(
    store: Store, *, kinds: Sequence[str] | None = None, worker_id: str, lease_s: int = LEASE_DURATION_S
) -> dict[str, Any] | None:
    """Atomically claim the oldest eligible job (optionally restricted to
    ``kinds``), or ``None`` if nothing is currently claimable. This is the
    open-queue polling shape a worker loop uses (:func:`trialerror.jobs.worker.run_one`
    with no specific ``job_id``)."""
    now_s = now()
    lease_until = _plus_seconds(now_s, lease_s)
    params: dict[str, Any] = {"worker_id": worker_id, "lease_until": lease_until, "now": now_s}
    kind_clause = ""
    if kinds:
        placeholders = []
        for i, k in enumerate(kinds):
            key = f"kind{i}"
            params[key] = k
            placeholders.append(f":{key}")
        kind_clause = f"AND kind IN ({','.join(placeholders)})"
    sql = f"""
        UPDATE job
        SET state = 'claimed', claimed_by = :worker_id,
            lease_expires_ts = :lease_until, heartbeat_ts = :now
        WHERE job_id = (
            SELECT job_id FROM job
            WHERE {_ELIGIBLE_PREDICATE} {kind_clause}
            ORDER BY created_ts ASC
            LIMIT 1
        )
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, params).fetchone()
    if row is None:
        return None
    result = dict(row)
    _log_event(store, result["job_id"], "claimed", {"worker_id": worker_id})
    return result


def claim_or_create(
    store: Store,
    job_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
    worker_id: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    lease_s: int = LEASE_DURATION_S,
) -> dict[str, Any]:
    """atomic's own naming for its scheduler primitive (design Section
    4.4: "``claim_or_create`` is a conditional UPDATE"). Creates ``job_id``
    with ``kind``/``payload`` if it doesn't exist yet, then claims it.
    Unlike :func:`claim_specific`, this ALWAYS returns a job or raises
    :class:`~trialerror.jobs.errors.NotClaimableError` -- calling code asked for
    ONE named job, so silent "nothing happened" is not an acceptable
    outcome the way it is for open-queue polling."""
    if get_job(store, job_id) is None:
        try:
            enqueue(store, kind=kind, payload=payload, job_id=job_id, max_attempts=max_attempts)
        except ValidationError:
            pass  # lost a create race to another caller; the row exists now regardless
    claimed = claim_specific(store, job_id, worker_id=worker_id, lease_s=lease_s)
    if claimed is None:
        current = get_job(store, job_id)
        raise NotClaimableError(
            f"job {job_id!r} exists but is not currently claimable "
            f"(state={current['state'] if current else '<missing>'})"
        )
    return claimed


def heartbeat(
    store: Store, job_id: str, worker_id: str, *, lease_s: int = LEASE_DURATION_S, checkpoint: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Renew the lease (and, if ``checkpoint`` is given, durably persist
    it in the same statement -- any checkpoint write IS a heartbeat, per
    the design's "checkpoint JSON (stage cursor...)" framing). The first
    heartbeat on a freshly-claimed job also flips ``claimed -> running``.

    Raises :class:`~trialerror.jobs.errors.JobPausedError` if an operator called
    ``trialerror jobs pause`` on this job since the caller's last heartbeat --
    the cooperative pause signal a handler must observe. Raises
    :class:`~trialerror.jobs.errors.ForeignWorkerError` if ``worker_id`` does not
    match the job's current ``claimed_by`` (the PID-ownership check)."""
    now_s = now()
    lease_until = _plus_seconds(now_s, lease_s)
    checkpoint_json = json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None
    sql = """
        UPDATE job
        SET lease_expires_ts = :lease_until,
            heartbeat_ts = :now,
            state = CASE WHEN state = 'claimed' THEN 'running' ELSE state END,
            checkpoint = COALESCE(:checkpoint, checkpoint)
        WHERE job_id = :job_id AND claimed_by = :worker_id AND state IN ('claimed', 'running')
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(
            sql,
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "lease_until": lease_until,
                "now": now_s,
                "checkpoint": checkpoint_json,
            },
        ).fetchone()
    if row is not None:
        _log_event(store, job_id, "heartbeat", {"checkpoint_updated": checkpoint is not None})
        return dict(row)

    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    if current["state"] == "paused":
        raise JobPausedError(f"job {job_id!r} was paused by an operator")
    if current["claimed_by"] != worker_id:
        raise ForeignWorkerError(
            f"worker {worker_id!r} does not own job {job_id!r} (currently claimed_by={current['claimed_by']!r})"
        )
    raise InvalidTransitionError(
        f"job {job_id!r} is in state {current['state']!r}, not claimed/running -- cannot heartbeat"
    )


def complete(store: Store, job_id: str, worker_id: str) -> dict[str, Any]:
    now_s = now()
    sql = """
        UPDATE job
        SET state = 'complete', settled_ts = :now, claimed_by = NULL, lease_expires_ts = NULL
        WHERE job_id = :job_id AND claimed_by = :worker_id AND state IN ('claimed', 'running')
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, {"job_id": job_id, "worker_id": worker_id, "now": now_s}).fetchone()
    if row is not None:
        _log_event(store, job_id, "completed", None)
        return dict(row)

    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    if current["claimed_by"] != worker_id:
        raise ForeignWorkerError(
            f"worker {worker_id!r} does not own job {job_id!r} (currently claimed_by={current['claimed_by']!r})"
        )
    raise InvalidTransitionError(f"job {job_id!r} is in state {current['state']!r}; cannot complete")


def fail(
    store: Store,
    job_id: str,
    worker_id: str,
    *,
    failure_class: str,
    error: str,
    environmental_retry_delay_s: float | None = None,
) -> dict[str, Any]:
    """Design Section 4.4's failure-disposition split, ported from atomic's
    ``FailureDispositionPolicy``: ``failure_class='environmental'`` (GPU
    busy, rate limit, OOM-retryable) re-queues to ``pending`` after
    ``environmental_retry_delay_s`` (default :data:`ENV_RETRY_DEFAULT_S`)
    WITHOUT touching ``attempts``; ``failure_class='logic'`` increments
    ``attempts`` and either schedules a backoff retry (state ``failed``) or,
    once ``attempts`` reaches ``max_attempts``, settles the job
    ``abandoned`` (terminal).

    Reads the current row first (ownership/state-checked) to compute the
    new ``attempts``/backoff values in Python, then writes them in one
    ownership-gated conditional UPDATE. This two-step shape is safe under
    the ledger's single-claimant invariant: only the worker holding
    ``claimed_by`` can ever reach the write below, so no concurrent writer
    can invalidate the values computed from the read in between (the write
    step still re-checks ownership/state, so a lost race -- e.g. a
    concurrent :func:`sweep_expired_leases` reclaiming this same row --
    fails loudly via :class:`~trialerror.jobs.errors.ForeignWorkerError` rather
    than silently clobbering a state nobody who called this expected)."""
    if failure_class not in ("environmental", "logic"):
        raise ValueError(f"failure_class must be 'environmental' or 'logic', got {failure_class!r}")
    now_s = now()
    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    if current["claimed_by"] != worker_id:
        raise ForeignWorkerError(
            f"worker {worker_id!r} does not own job {job_id!r} (currently claimed_by={current['claimed_by']!r})"
        )
    if current["state"] not in ("claimed", "running"):
        raise InvalidTransitionError(f"job {job_id!r} is in state {current['state']!r}; cannot fail")

    if failure_class == "environmental":
        new_attempts = current["attempts"]  # UNCHANGED -- the acceptance criterion
        new_state = "pending"
        delay = environmental_retry_delay_s if environmental_retry_delay_s is not None else ENV_RETRY_DEFAULT_S
        next_attempt = _plus_seconds(now_s, delay)
        settled = current["settled_ts"]
        event_type = "deferred"
    else:
        new_attempts = current["attempts"] + 1
        if new_attempts >= current["max_attempts"]:
            new_state = "abandoned"
            next_attempt = None
            settled = now_s
            event_type = "abandoned"
        else:
            new_state = "failed"
            next_attempt = _plus_seconds(now_s, backoff_seconds(new_attempts))
            settled = current["settled_ts"]
            event_type = "retry_scheduled"

    sql = """
        UPDATE job
        SET attempts = :attempts, failure_class = :failure_class, last_error = :error,
            claimed_by = NULL, lease_expires_ts = NULL, state = :state,
            next_attempt_ts = :next_attempt, settled_ts = :settled
        WHERE job_id = :job_id AND claimed_by = :worker_id AND state IN ('claimed', 'running')
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(
            sql,
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "attempts": new_attempts,
                "failure_class": failure_class,
                "error": error,
                "state": new_state,
                "next_attempt": next_attempt,
                "settled": settled,
            },
        ).fetchone()
    if row is None:
        raise ForeignWorkerError(
            f"job {job_id!r} ownership/state changed concurrently; "
            f"worker {worker_id!r} lost the race to record this failure"
        )
    _log_event(store, job_id, event_type, {"error": error, "attempts": new_attempts})
    return dict(row)


def pause(store: Store, job_id: str) -> dict[str, Any]:
    """Operator-level control op (no ``worker_id`` -- ``trialerror jobs pause``
    doesn't need to hold the lease to request a stop). Idempotent on an
    already-``paused`` job; refused on a terminal (``complete``/
    ``abandoned``) one."""
    sql = """
        UPDATE job SET state = 'paused'
        WHERE job_id = :job_id AND state NOT IN ('complete', 'abandoned', 'paused')
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, {"job_id": job_id}).fetchone()
    if row is not None:
        _log_event(store, job_id, "paused", None)
        return dict(row)

    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    if current["state"] == "paused":
        return current  # idempotent
    raise InvalidTransitionError(f"job {job_id!r} is in terminal state {current['state']!r}; cannot pause")


def resume(store: Store, job_id: str) -> dict[str, Any]:
    """Make a paused job claimable again. Does NOT itself launch a worker
    -- ``trialerror jobs start-worker`` (or the next open-queue poll) is what
    actually picks it back up; see the CLI group's ``resume`` command
    docstring for why that split is deliberate."""
    sql = """
        UPDATE job SET state = 'pending', next_attempt_ts = NULL
        WHERE job_id = :job_id AND state = 'paused'
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, {"job_id": job_id}).fetchone()
    if row is not None:
        _log_event(store, job_id, "resumed", None)
        return dict(row)

    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    raise InvalidTransitionError(f"job {job_id!r} is in state {current['state']!r}, not 'paused'; cannot resume")


def sweep_expired_leases(store: Store) -> list[dict[str, Any]]:
    """``trialerror jobs tick``'s core: every ``claimed``/``running`` job whose
    lease has expired is released back to ``pending`` -- ``checkpoint`` is
    untouched, ``attempts`` is untouched (a crash is not a failure of the
    JOB, only of the worker that was running it) -- so the very next claim
    (by this same or any other worker) resumes exactly where the dead
    worker's last durable checkpoint left off. This is the structural fix
    named in the build brief: "the watchdog is now a table" (design
    Section 10/P7) instead of a keep-alive loop that can die silently."""
    now_s = now()
    sql = """
        UPDATE job
        SET state = 'pending', claimed_by = NULL, lease_expires_ts = NULL
        WHERE state IN ('claimed', 'running') AND lease_expires_ts IS NOT NULL AND lease_expires_ts < :now
        RETURNING *
    """
    with store.jobs:
        rows = store.jobs.execute(sql, {"now": now_s}).fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        _log_event(store, r["job_id"], "reclaimed", {"kind": r["kind"]})
    return results
