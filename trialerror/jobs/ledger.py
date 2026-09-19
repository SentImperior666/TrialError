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
       |
       `<--- retry ----- (failed | abandoned)    attempts -> 0, budget restored

``claimed``/``running`` whose ``lease_expires_ts`` has passed are reclaimed
by :func:`sweep_expired_leases` (``trialerror jobs tick``) back to ``pending``,
``checkpoint`` untouched -- the crashed-worker recovery path.

``retry`` (lane FB-8a) is the only edge OUT of a settled-unsuccessful state.
It is deliberately narrower than ``resume`` in what it accepts (two states,
never ``paused``) and wider in what it does (``attempts`` back to 0,
``failure_class``/``settled_ts`` cleared), because the two verbs answer
different questions: ``resume`` lifts a hold nothing ever counted against
the job, ``retry`` says the failures that WERE counted no longer describe
it.
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
    "abandon",
    "abandon_pending_for_doc",
    "ABANDONABLE_STATES",
    "HELD_STATES",
    "RETRYABLE_STATES",
    "RETRY_MAX_ATTEMPTS_RANGE",
    "RETRY_ERROR_PREFIX",
    "RETRY_NO_PREVIOUS_ERROR",
    "retry_refusal",
    "retry",
    "kick",
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


#: The id prefix every ingest-pipeline job carries
#: (``trialerror.ingest.pipeline.add_document``/``requeue_stage`` and
#: ``trialerror.ingest.handlers``'s stage hand-offs all build
#: ``JOB-ingest-<doc_id>...``). Transcribed here because
#: :func:`list_jobs_for_doc`'s fallback match is a string prefix, and the
#: convention it depends on must be named in the module that depends on it.
INGEST_JOB_ID_PREFIX = "JOB-ingest-"


def _like_literal(value: str) -> str:
    """``value`` escaped for use inside a LIKE pattern with
    ``ESCAPE '\\'`` -- a doc_id is generated and contains none of these
    today, but a LIKE pattern built by concatenation is exactly the kind of
    thing that silently matches the wrong rows the first time an id
    convention changes."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_jobs_for_doc(store: Store, doc_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Every job row belonging to one ingested document, oldest first, each
    annotated with the ``match_kind`` that found it.

    **Two matches, because there is no ``doc_id`` column on ``job`` and
    this lane adds no migration.** The authoritative one is the payload:
    every pipeline stage's payload carries ``doc_id``
    (``json_extract(payload, '$.doc_id')``). The fallback is the job-id
    convention ``JOB-ingest-<doc_id>...``
    (:data:`INGEST_JOB_ID_PREFIX`), which catches a row whose payload was
    written by hand or by an older shape -- and it is a FALLBACK, not a
    substitute: it cannot see a job whose id was minted some other way, so
    a caller that reports a chain must say which match found each row
    rather than implying the ledger has a foreign key it does not have.

    ``match_kind`` is ``"payload"``, ``"job_id_prefix"``, or
    ``"payload+job_id_prefix"`` for a row both agree on.
    """
    by_payload = {
        r["job_id"]: dict(r)
        for r in store.jobs.execute(
            "SELECT * FROM job WHERE json_extract(payload, '$.doc_id') = ? "
            "ORDER BY created_ts ASC, job_id ASC LIMIT ?",
            (doc_id, limit),
        ).fetchall()
    }
    by_prefix = {
        r["job_id"]: dict(r)
        for r in store.jobs.execute(
            "SELECT * FROM job WHERE job_id LIKE ? ESCAPE '\\' ORDER BY created_ts ASC, job_id ASC LIMIT ?",
            (f"{_like_literal(INGEST_JOB_ID_PREFIX + doc_id)}%", limit),
        ).fetchall()
    }

    out: list[dict[str, Any]] = []
    for job_id in sorted(set(by_payload) | set(by_prefix)):
        row = by_payload.get(job_id) or by_prefix[job_id]
        in_payload, in_prefix = job_id in by_payload, job_id in by_prefix
        row["match_kind"] = (
            "payload+job_id_prefix" if in_payload and in_prefix else ("payload" if in_payload else "job_id_prefix")
        )
        out.append(row)
    out.sort(key=lambda r: (r.get("created_ts") or "", r["job_id"]))
    return out[:limit]


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


#: The job states :func:`abandon` may settle from. A ``claimed``/``running``
#: job is held by a worker with a live lease, and settling it terminally
#: under that worker would let it complete a job the ledger has already
#: closed; ``jobs pause`` (cooperative) then ``jobs abandon``, or ``jobs
#: tick`` once the lease expires, is the path there.
ABANDONABLE_STATES = ("pending", "paused", "failed")

#: The states in which a worker holds the job with a live lease. Not
#: abandonable, and -- fix pass V-6 -- not silent either: they are what
#: :func:`abandon_pending_for_doc` reports back as ``held``.
HELD_STATES = ("claimed", "running")


def abandon(store: Store, job_id: str, *, reason: str, worker_id: str | None = None) -> dict[str, Any]:
    """Settle a job ``abandoned`` (terminal) with a stated reason.

    Lane FB-3 item 9. Two callers, one write: ``trialerror ingest retract``
    cancelling the pending work of a document it has just withdrawn, and
    ``trialerror jobs abandon`` for an operator's own cleanup. Until now the
    only route to ``abandoned`` was exhausting ``max_attempts`` through
    :func:`fail`, which means the only way to cancel work was to let it run
    and fail three times -- or to pause it and leave a paused row in the
    queue forever.

    ``reason`` is required and lands in ``last_error`` and on the ledger
    event: a terminal state with no stated cause is the kind of row that
    gets re-run by the next person who finds it.

    ``worker_id`` is the one way past :data:`ABANDONABLE_STATES`: the worker
    that HOLDS the lease may settle its own job, which is what the run-time
    retracted-document guard does at claim time (it has just claimed the
    row, so the row is ``claimed`` and nobody else is racing it). It only
    ever matches a row whose ``claimed_by`` is that same worker -- the same
    ownership predicate :func:`heartbeat`/:func:`complete` enforce -- so it
    is not a way for one worker to close another's job.

    Idempotent on an already-``abandoned`` job; refuses a ``complete`` one
    (there is nothing to cancel) and a ``claimed``/``running`` one held by
    somebody else (see :data:`ABANDONABLE_STATES`)."""
    ts = now()
    placeholders = ",".join(f":s{i}" for i in range(len(ABANDONABLE_STATES)))
    params: dict[str, Any] = {"job_id": job_id, "reason": reason, "ts": ts}
    params.update({f"s{i}": state for i, state in enumerate(ABANDONABLE_STATES)})
    predicate = f"state IN ({placeholders})"
    if worker_id is not None:
        params["worker_id"] = worker_id
        predicate += " OR (state IN ('claimed','running') AND claimed_by = :worker_id)"
    sql = f"""
        UPDATE job SET state = 'abandoned', settled_ts = :ts, last_error = :reason,
                       claimed_by = NULL, lease_expires_ts = NULL, next_attempt_ts = NULL
        WHERE job_id = :job_id AND ({predicate})
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, params).fetchone()
    if row is not None:
        _log_event(store, job_id, "abandoned", {"reason": reason, "by": worker_id or "operator"})
        return dict(row)

    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    if current["state"] == "abandoned":
        return current  # idempotent
    raise InvalidTransitionError(
        f"job {job_id!r} is in state {current['state']!r}; abandon accepts "
        f"{ABANDONABLE_STATES!r}"
        + (
            " -- a worker holds this one, so pause it first (`trialerror jobs pause`) and abandon "
            "the paused row, or wait for its lease to expire and `trialerror jobs tick` to reclaim it"
            if current["state"] in ("claimed", "running")
            else ""
        )
    )


def abandon_pending_for_doc(store: Store, doc_id: str, *, reason: str) -> dict[str, list[dict[str, Any]]]:
    """Settle every cancellable job belonging to ``doc_id`` as ``abandoned``,
    and REPORT the ones a worker is holding.

    The 2026-09-15 observation this exists for: a duplicate registration was
    retracted while its normalize job was still ``pending``, and the job
    survived the retraction -- an orphan the next worker would have picked
    up and run against a document whose derived rows had just been removed.
    The jobs were paused by hand.

    Returns ``{"cancelled": [...], "held": [...]}``. Jobs a worker currently
    holds (``claimed``/``running``) are not settled here -- settling a held
    row would let its worker complete a job the ledger had already closed
    (see :func:`abandon`) -- but they ARE named, which is the fix-pass V-6
    correction: this used to skip them with a bare ``continue`` while its own
    docstring claimed the caller reported them, so an operator retracting a
    duplicate whose normalize stage was RUNNING was told what had been
    cancelled and not that a stage was still running against the document
    they had just withdrawn. The run-time guard in
    :mod:`trialerror.jobs.worker` fires at CLAIM, so it catches that job's
    successors and not the job itself."""
    settled: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for job in list_jobs_for_doc(store, doc_id):
        if job["state"] in HELD_STATES:
            held.append(
                {
                    "job_id": job["job_id"],
                    "kind": job["kind"],
                    "state": job["state"],
                    "claimed_by": job["claimed_by"],
                    "lease_expires_ts": job["lease_expires_ts"],
                }
            )
            continue
        if job["state"] not in ABANDONABLE_STATES:
            continue
        row = abandon(store, job["job_id"], reason=reason)
        settled.append({"job_id": row["job_id"], "kind": row["kind"], "was": job["state"]})
    return {"cancelled": settled, "held": held}


#: The two states :func:`retry` may bring back. Both are SETTLED-UNSUCCESSFUL
#: and nothing else is: ``complete`` has nothing to redo, ``pending`` is
#: already claimable, ``claimed``/``running`` are held by a worker with a live
#: lease, and ``paused`` has its own verb (:func:`resume`) which is a
#: different power -- un-pausing does not reset an attempt counter.
#:
#: ``failed`` is in the list even though a ``failed`` row with budget left is
#: claimable on its own (:data:`_ELIGIBLE_PREDICATE`): the row this verb
#: exists for is the one whose OFFLOAD attempts are spent, which re-fails on
#: every remaining ledger attempt until it abandons. Retrying it is the only
#: way that budget comes back.
RETRYABLE_STATES = ("failed", "abandoned")

#: The inclusive bounds ``--max-attempts`` accepts. 1 because a job an
#: operator wants tried exactly once is a real request; 10 because a retry
#: budget past that is not a budget, and the same terminal marker would burn
#: ten GPU claims before anybody looked at it again.
RETRY_MAX_ATTEMPTS_RANGE = (1, 10)

#: What :func:`retry` prefixes the previous ``last_error`` with. The error is
#: KEPT rather than cleared: ``jobs list`` shows ``last_error`` and nothing
#: else about a row's history, so a retry that blanked it would hand the next
#: operator a ``pending`` job with no sign it had ever failed, and the ledger
#: event they would have to read instead is exactly the thing ``jobs list``
#: exists to save them.
RETRY_ERROR_PREFIX = "retried"

#: Stands in for the previous ``last_error`` of a row that had none (a job
#: abandoned by a route that recorded no message). The prefix still lands, so
#: the RETRY is visible on ``jobs list`` either way.
RETRY_NO_PREVIOUS_ERROR = "(no previous error recorded)"


def retry_refusal(store: Store, job_id: str, *, max_attempts: int | None = None) -> None:
    """Raise if :func:`retry` would refuse this job, and return ``None`` if
    it would proceed. Reads; writes nothing.

    Split out from :func:`retry` because ``trialerror jobs retry`` moves the
    OFFLOAD queue entry before it touches the store (see
    :mod:`trialerror.offload.retry`), and a retry that is going to be refused
    for the row's state must be refused BEFORE a directory tree moves --
    otherwise "retry a job a worker currently holds" returns a clean refusal
    and has already taken that worker's queue marker away from it."""
    if max_attempts is not None:
        low, high = RETRY_MAX_ATTEMPTS_RANGE
        if not (low <= int(max_attempts) <= high):
            raise ValueError(
                f"--max-attempts must be between {low} and {high}, got {max_attempts!r}"
            )
    current = get_job(store, job_id)
    if current is None:
        raise JobNotFoundError(f"no such job: {job_id!r}")
    state = current["state"]
    if state in RETRYABLE_STATES:
        return None
    hint = ""
    if state == "paused":
        hint = (
            " -- an operator's hold is lifted with `trialerror jobs resume`, which is a "
            "different act: it does not reset attempts or clear the failure class"
        )
    elif state == "complete":
        hint = " -- there is nothing to retry; re-enqueue the stage if you want it run again"
    elif state == "pending":
        hint = " -- it is already claimable; `trialerror jobs kick` clears a retry delay"
    elif state in HELD_STATES:
        hint = (
            f" -- worker {current['claimed_by']!r} holds it; pause it (`trialerror jobs pause`) "
            "and let it stop, or wait for its lease to expire and run `trialerror jobs tick`"
        )
    raise InvalidTransitionError(
        f"job {job_id!r} is in state {state!r}; retry accepts {RETRYABLE_STATES!r}{hint}"
    )


def retry(
    store: Store,
    job_id: str,
    *,
    reason: str,
    by_launch: str | None = None,
    max_attempts: int | None = None,
    clear_checkpoint: bool = False,
    ts: str | None = None,
) -> dict[str, Any]:
    """Return a settled-unsuccessful job to the queue with its retry budget
    restored -- the sanctioned way back that lane FB-8a exists for.

    The observation: a tool-side defect was fixed, and the three documents
    that had MET that defect were the only ones the fix could not help. Two
    of their jobs were ``failed`` with their offload attempts spent, one was
    ``abandoned``; :func:`resume` covers ``paused`` only, and the alternative
    was editing ``jobs.db`` by hand, which this harness's own rules forbid.

    One conditional UPDATE, gated on :data:`RETRYABLE_STATES`, so the
    eligibility check and the write are one atomic unit exactly like every
    other transition in this module -- a row that a concurrent
    :func:`claim_next` took between the caller's read and this write matches
    zero rows and raises, rather than being quietly reset under its new
    owner.

    What changes: ``state`` -> ``pending``, ``attempts`` -> 0,
    ``next_attempt_ts``/``claimed_by``/``lease_expires_ts``/``settled_ts``/
    ``failure_class`` -> NULL. What does NOT: ``last_error`` is kept, prefixed
    (:data:`RETRY_ERROR_PREFIX`) so the history stays visible on ``jobs
    list``; ``checkpoint`` is kept unless ``clear_checkpoint`` (a stage that
    was resuming from a cursor should keep resuming from it -- the defect the
    operator just fixed is rarely in the cursor); ``max_attempts`` is
    unchanged unless the caller states a new one.

    The ledger event is ``job_retried`` and it carries the PREVIOUS state,
    the previous attempts, the previous ``last_error`` IN FULL (the prefix
    truncates nothing, but the column is also the one thing a later retry
    will prefix again), the reason, the launch and the stamp.
    """
    if not reason or not reason.strip():
        raise ValueError("jobs retry: --reason is required and must not be empty")
    retry_refusal(store, job_id, max_attempts=max_attempts)
    current = get_job(store, job_id)
    assert current is not None  # retry_refusal just read it in this same connection

    ts = ts or now()
    previous_error = current["last_error"]
    kept = previous_error if previous_error is not None else RETRY_NO_PREVIOUS_ERROR
    placeholders = ",".join(f":s{i}" for i in range(len(RETRYABLE_STATES)))
    params: dict[str, Any] = {
        "job_id": job_id,
        "last_error": f"{RETRY_ERROR_PREFIX} {ts}: {kept}",
        "max_attempts": int(max_attempts) if max_attempts is not None else current["max_attempts"],
    }
    params.update({f"s{i}": state for i, state in enumerate(RETRYABLE_STATES)})
    checkpoint_sql = "NULL" if clear_checkpoint else "checkpoint"
    sql = f"""
        UPDATE job
        SET state = 'pending',
            attempts = 0,
            next_attempt_ts = NULL,
            claimed_by = NULL,
            lease_expires_ts = NULL,
            settled_ts = NULL,
            failure_class = NULL,
            last_error = :last_error,
            checkpoint = {checkpoint_sql},
            max_attempts = :max_attempts
        WHERE job_id = :job_id AND state IN ({placeholders})
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, params).fetchone()
    if row is None:
        # Only a writer that changed the row between :func:`retry_refusal`'s
        # read and this statement reaches here -- the UPDATE's own
        # ``state IN (...)`` is the guard, not that read, which is what
        # ``test_probe_a3_the_conditional_update_is_the_guard_not_the_read``
        # holds in place by neutering the read and trying twice.
        latest = get_job(store, job_id)
        raise InvalidTransitionError(
            f"job {job_id!r} changed state concurrently (now "
            f"{latest['state'] if latest else '<missing>'}); nothing was retried"
        )
    _log_event(
        store,
        job_id,
        "job_retried",
        {
            "ts": ts,
            "previous_state": current["state"],
            "previous_attempts": current["attempts"],
            "previous_last_error": previous_error,
            "previous_failure_class": current["failure_class"],
            "reason": reason,
            "by_launch": by_launch,
            "max_attempts": params["max_attempts"],
            "checkpoint_cleared": bool(clear_checkpoint),
        },
    )
    return dict(row)


def kick(store: Store, job_id: str) -> dict[str, Any] | None:
    """Clear a deferred job's ``next_attempt_ts`` so it becomes claimable
    NOW. Returns the updated row, or ``None`` if there was nothing to clear
    (already claimable, terminal, paused, or no such job) -- like
    :func:`claim_specific`, "nothing happened" is an ordinary outcome here,
    not an error.

    Added for lane L0-C (design section 4: "``trialerror offload kick``:
    resets ``next_attempt_ts`` (new ``ledger.kick``) for pending jobs whose
    ``done/`` landed"). The offload branch parks a stage with an
    ``EnvironmentalFailure`` carrying a 30-minute retry delay, because the
    DEV GPU may be off for weeks and polling it faster would be pure
    noise. But once the result actually lands, that same delay is the only
    thing standing between the document and its finished ingest -- and the
    landing is observable (a directory appeared), so the sandbox can simply
    say "now" instead of waiting out a timer that was sized for absence.

    Deliberately narrower than :func:`resume`: it never changes ``state``,
    never touches ``attempts``, and refuses to act on anything that is not
    already ``pending``/``failed``-with-budget. Un-delaying is not the same
    power as un-pausing, and this is called by an unattended loop."""
    sql = """
        UPDATE job SET next_attempt_ts = NULL
        WHERE job_id = :job_id AND next_attempt_ts IS NOT NULL
          AND (state = 'pending' OR (state = 'failed' AND attempts < max_attempts))
        RETURNING *
    """
    with store.jobs:
        row = store.jobs.execute(sql, {"job_id": job_id}).fetchone()
    if row is None:
        return None
    _log_event(store, job_id, "kicked", {"reason": "offload result published"})
    return dict(row)


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
