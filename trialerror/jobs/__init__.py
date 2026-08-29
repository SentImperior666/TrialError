"""``trialerror.jobs`` -- the durable execution ledger + detached worker
runtime (design Section 4.4/12, M2). See ``trialerror.jobs.ledger`` for the
claim/lease/heartbeat/backoff state machine and ``trialerror.jobs.worker`` for
the handler contract + Windows-first detached-process launcher.
"""

from trialerror.jobs.errors import (
    ForeignWorkerError,
    InvalidTransitionError,
    JobError,
    JobNotFoundError,
    JobPausedError,
    NotClaimableError,
    UnknownHandlerError,
)
from trialerror.jobs.ledger import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    DEFAULT_MAX_ATTEMPTS,
    ENV_RETRY_DEFAULT_S,
    HEARTBEAT_INTERVAL_S,
    LEASE_DURATION_S,
    backoff_seconds,
    claim_next,
    claim_or_create,
    claim_specific,
    complete,
    enqueue,
    fail,
    get_job,
    heartbeat,
    list_events,
    list_jobs,
    pause,
    resume,
    sweep_expired_leases,
)
from trialerror.jobs.registry import (
    discover_and_register_handlers,
    get_handler,
    register_handler,
    registered_handlers,
)
from trialerror.jobs.worker import (
    EnvironmentalFailure,
    JobContext,
    WorkerHandle,
    make_worker_id,
    run_loop,
    run_one,
    spawn_worker,
)

__all__ = [
    # errors
    "JobError",
    "JobNotFoundError",
    "NotClaimableError",
    "ForeignWorkerError",
    "JobPausedError",
    "InvalidTransitionError",
    "UnknownHandlerError",
    # ledger
    "HEARTBEAT_INTERVAL_S",
    "LEASE_DURATION_S",
    "DEFAULT_MAX_ATTEMPTS",
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "ENV_RETRY_DEFAULT_S",
    "backoff_seconds",
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
    # registry
    "register_handler",
    "get_handler",
    "registered_handlers",
    "discover_and_register_handlers",
    # worker
    "EnvironmentalFailure",
    "JobContext",
    "WorkerHandle",
    "make_worker_id",
    "run_one",
    "run_loop",
    "spawn_worker",
]
