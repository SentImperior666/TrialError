"""Jobs-ledger exceptions. Mirrors ``trialerror.stores.errors``'s pattern
(design Section 12, M1 row precedent): a common base class every caller
that only cares "did this ledger call fail" can catch, plus specific
subclasses for callers that need to branch on *why* (retry vs. surface to
an operator vs. bug)."""

from __future__ import annotations

__all__ = [
    "JobError",
    "JobNotFoundError",
    "NotClaimableError",
    "ForeignWorkerError",
    "JobPausedError",
    "InvalidTransitionError",
    "UnknownHandlerError",
]


class JobError(Exception):
    """Base class for every error the jobs ledger / worker runtime raises."""


class JobNotFoundError(JobError):
    """No job exists with the given ``job_id``."""


class NotClaimableError(JobError):
    """A specific job was targeted for claim but is not currently eligible
    (wrong state, backoff/defer window not yet elapsed, or already held by
    a live, unexpired lease)."""


class ForeignWorkerError(JobError):
    """Design Section 4.4: ``claimed_by`` is documented as "worker_id =
    pid + start_ts; PID-ownership verified, codemap pattern" (the
    ``ErrForeignDaemonPID``-style distinction docs/mining/G05 describes).
    Raised whenever a caller's ``worker_id`` does not match the job's
    current ``claimed_by`` -- this caller does not hold the lease it is
    trying to act on. This IS the "PID-ownership check refuses foreign
    pid" acceptance criterion, enforced structurally: every settle/
    heartbeat call's SQL WHERE clause requires ``claimed_by = :worker_id``,
    so a foreign caller's write simply matches zero rows."""


class JobPausedError(JobError):
    """Raised by :func:`trialerror.jobs.ledger.heartbeat` when an operator has
    called ``trialerror jobs pause`` on this job since the caller's last
    heartbeat -- the cooperative signal a long-running handler observes at
    its next checkpoint and must stop for, without treating it as a
    failure."""


class InvalidTransitionError(JobError):
    """A settle/pause/resume call was attempted from a state that does not
    permit it (e.g. resuming a job that isn't paused, pausing one already
    complete, completing a job this worker never claimed)."""


class UnknownHandlerError(JobError):
    """A job's ``kind`` (or, for ``kind='custom'``, its
    ``payload['handler']``) names a handler that was never registered
    (via ``@register_handler`` auto-discovery or an explicit
    ``--handler-module``)."""
