"""Detached worker runtime. Design Section 4.4's long-job contract: "long
jobs are detached CLI workers with heartbeats, NEVER blocking MCP calls."
Three pieces:

- :class:`JobContext` -- the handler-facing API (``ctx.payload``,
  ``ctx.checkpoint``, ``ctx.set_checkpoint(...)``, ``ctx.heartbeat()``).
- :func:`run_one` / :func:`run_loop` -- claim-run-settle, in THIS process.
  This is what actually executes inside a worker, detached or not; tests
  call it directly for every scenario that doesn't need a real OS-level
  kill.
- :func:`spawn_worker` -- the Windows-first detached-process launcher
  (design Section 12, M2 row: "detached worker launcher (DETACHED_PROCESS
  on Win)").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from trialerror.jobs import ledger
from trialerror.jobs.errors import JobPausedError
from trialerror.jobs.registry import discover_and_register_handlers, get_handler
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "EnvironmentalFailure",
    "JobContext",
    "make_worker_id",
    "run_one",
    "run_loop",
    "WorkerHandle",
    "spawn_worker",
]


class EnvironmentalFailure(Exception):
    """Raise from inside a job handler to signal a transient,
    environment-caused failure (GPU busy, rate limit, OOM-retryable --
    design Section 4.4) that must NOT consume a retry attempt. Any OTHER
    exception a handler raises is treated as a logic failure (attempt
    consumed, exponential backoff scheduled) -- this is the one
    handler-facing escape hatch from that default."""

    def __init__(self, reason: str, *, retry_delay_s: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_delay_s = retry_delay_s


def make_worker_id(pid: int | None = None) -> str:
    """``worker_id = pid + start_ts`` (design Section 4.4's ``claimed_by``
    column doc: "PID-ownership verified, codemap pattern"). Encoding the
    start timestamp alongside the OS pid is what makes ownership checks
    (see ``trialerror.jobs.ledger``'s ``claimed_by = :worker_id`` predicates)
    immune to PID reuse: an unrelated process the OS later hands the same
    pid can never produce the same ``worker_id`` string."""
    return f"{pid if pid is not None else os.getpid()}:{now()}"


@dataclass
class JobContext:
    """The handler-facing API. A job handler is ``def handler(ctx:
    JobContext) -> None``; it reads ``ctx.payload``/``ctx.checkpoint`` and
    calls ``ctx.set_checkpoint(...)``/``ctx.heartbeat()`` to durably record
    resumable progress and prove liveness. Raising
    :class:`EnvironmentalFailure` marks the failure environmental; raising
    anything else marks it a logic failure; returning normally completes
    the job. A :class:`~trialerror.jobs.errors.JobPausedError` raised BY
    ``heartbeat()``/``set_checkpoint()`` (an operator paused this job) is
    expected to propagate out of the handler uncaught -- :func:`run_one`
    catches it at the top level and leaves the job in its already-``paused``
    state, no handler cleanup logic required."""

    store: Store
    job: dict[str, Any]
    worker_id: str
    lease_s: int

    @property
    def job_id(self) -> str:
        return self.job["job_id"]

    @property
    def payload(self) -> dict[str, Any]:
        raw = self.job.get("payload")
        return json.loads(raw) if raw else {}

    @property
    def checkpoint(self) -> dict[str, Any]:
        raw = self.job.get("checkpoint")
        return json.loads(raw) if raw else {}

    def heartbeat(self) -> None:
        """Renew the lease without changing the checkpoint. Call this
        periodically inside any handler step that doesn't itself call
        :meth:`set_checkpoint` often enough to keep the lease alive on its
        own."""
        self.job = ledger.heartbeat(self.store, self.job_id, self.worker_id, lease_s=self.lease_s)

    def set_checkpoint(self, data: dict[str, Any]) -> None:
        """Durably record resumable progress AND renew the lease in one
        call -- any checkpoint write is proof of liveness (design Section
        4.4: "checkpoint JSON (stage cursor: e.g. last committed batch
        index)"). Call this after each independently-resumable unit of
        work, the same restart-safety shape as the origin-project embed/OCR runners'
        own content-hash-keyed progress caches
        (``research/tools/embeddings_local/corpus_embed_runner.py``'s
        ``chunk_cache.sqlite3``)."""
        self.job = ledger.heartbeat(self.store, self.job_id, self.worker_id, lease_s=self.lease_s, checkpoint=data)


def run_one(
    store: Store,
    *,
    worker_id: str | None = None,
    job_id: str | None = None,
    kind: str | None = None,
    payload: dict[str, Any] | None = None,
    kinds: Sequence[str] | None = None,
    lease_s: int = ledger.LEASE_DURATION_S,
    max_attempts: int = ledger.DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Claim exactly one job and run it to settlement (``complete``/
    ``deferred``/``failed``/``abandoned``) or a cooperative ``paused``
    stop; ``{"status": "idle", ...}`` if nothing was eligible to claim.

    This IS a detached worker's body -- :func:`spawn_worker` launches a new
    OS process that (via ``trialerror jobs start-worker --foreground``) calls
    this, once (``--mode once``) or in :func:`run_loop` (``--mode loop``).
    Tests call it directly, in-process, for every scenario that doesn't
    need a real OS-level kill.

    ``job_id`` given: claim (create-if-missing, via
    :func:`trialerror.jobs.ledger.claim_or_create`) that SPECIFIC job -- a
    targeted relaunch/resume. ``job_id`` omitted: claim the oldest eligible
    job via :func:`trialerror.jobs.ledger.claim_next`, optionally restricted to
    ``kinds`` -- open-queue polling.
    """
    discover_and_register_handlers()
    worker_id = worker_id or make_worker_id()

    if job_id is not None:
        claimed = ledger.claim_or_create(
            store,
            job_id,
            kind=kind or "custom",
            payload=payload or {},
            worker_id=worker_id,
            max_attempts=max_attempts,
            lease_s=lease_s,
        )
    else:
        claimed = ledger.claim_next(store, kinds=kinds, worker_id=worker_id, lease_s=lease_s)
    if claimed is None:
        return {"status": "idle", "worker_id": worker_id}

    handler_name = claimed["kind"]
    if handler_name == "custom":
        handler_name = json.loads(claimed["payload"]).get("handler") if claimed["payload"] else None
    if not handler_name:
        row = ledger.fail(
            store,
            claimed["job_id"],
            worker_id,
            failure_class="logic",
            error="kind='custom' job payload is missing the required 'handler' key",
        )
        status = "abandoned" if row["state"] == "abandoned" else "failed"
        return {"status": status, "job_id": claimed["job_id"], "worker_id": worker_id}

    ctx = JobContext(store=store, job=claimed, worker_id=worker_id, lease_s=lease_s)
    try:
        # Handler resolution deliberately happens INSIDE this try block: an
        # unregistered handler name (UnknownHandlerError, a JobError ->
        # Exception subclass) must settle the job as a logic failure the
        # same way a handler's own runtime exception does, not crash the
        # worker process outright -- one broken/misconfigured job must
        # never take the whole worker down (the same isolation principle
        # trialerror.util.doctor.run_checks applies per-check).
        handler = get_handler(handler_name)
        handler(ctx)
    except JobPausedError:
        return {"status": "paused", "job_id": claimed["job_id"], "worker_id": worker_id}
    except EnvironmentalFailure as exc:
        ledger.fail(
            store,
            claimed["job_id"],
            worker_id,
            failure_class="environmental",
            error=exc.reason,
            environmental_retry_delay_s=exc.retry_delay_s,
        )
        return {"status": "deferred", "job_id": claimed["job_id"], "worker_id": worker_id}
    except Exception as exc:  # noqa: BLE001 - deliberate: any other handler exception is a logic failure
        row = ledger.fail(
            store,
            claimed["job_id"],
            worker_id,
            failure_class="logic",
            error=f"{type(exc).__name__}: {exc}",
        )
        status = "abandoned" if row["state"] == "abandoned" else "failed"
        return {"status": status, "job_id": claimed["job_id"], "worker_id": worker_id}
    else:
        ledger.complete(store, claimed["job_id"], worker_id)
        return {"status": "complete", "job_id": claimed["job_id"], "worker_id": worker_id}


def run_loop(
    store: Store,
    *,
    worker_id: str | None = None,
    kinds: Sequence[str] | None = None,
    lease_s: int = ledger.LEASE_DURATION_S,
    poll_interval_s: float = 2.0,
    max_idle_polls: int = 3,
    max_iterations: int | None = None,
) -> list[dict[str, Any]]:
    """Drain the eligible queue: keep calling :func:`run_one` until
    ``max_idle_polls`` consecutive claims come back idle, or
    ``max_iterations`` non-idle jobs have run. The origin-project embed/OCR runners'
    own shape -- "processes the whole batch, then exits"
    (``research/tools/embeddings_local/corpus_embed_runner.py``,
    ``research/tools/marker_ocr/run_batch.py``) -- not an unbounded daemon.
    """
    worker_id = worker_id or make_worker_id()
    results: list[dict[str, Any]] = []
    idle_streak = 0
    non_idle_count = 0
    while True:
        result = run_one(store, worker_id=worker_id, kinds=kinds, lease_s=lease_s)
        results.append(result)
        if result["status"] == "idle":
            idle_streak += 1
            if idle_streak >= max_idle_polls:
                break
            time.sleep(poll_interval_s)
            continue
        idle_streak = 0
        non_idle_count += 1
        if max_iterations is not None and non_idle_count >= max_iterations:
            break
    return results


@dataclass
class WorkerHandle:
    """What :func:`spawn_worker` hands back. ``process`` is the live
    ``subprocess.Popen`` -- the ONLY "find this worker again" primitive
    this function itself offers, because the design's stated liveness
    contract (Section 10/13: "liveness judged by ledger/heartbeat
    side-effects") means any OTHER caller (a later ``trialerror jobs tick``, a
    wholly different process) finds and judges a worker exclusively through
    the jobs ledger (``claimed_by``/``heartbeat_ts``/``lease_expires_ts``
    on whatever job it claims), never through OS process enumeration."""

    pid: int
    argv: list[str]
    log_path: Path
    process: subprocess.Popen


def spawn_worker(
    *,
    program_root: Path | str,
    platform_root: Path | str | None = None,
    kinds: Sequence[str] | None = None,
    job_id: str | None = None,
    kind: str | None = None,
    payload: dict[str, Any] | None = None,
    mode: str = "once",
    lease_s: int | None = None,
    poll_interval_s: float = 2.0,
    max_idle_polls: int = 3,
    max_iterations: int | None = None,
    log_dir: Path | str | None = None,
    extra_handler_modules: Sequence[str] | None = None,
    python_exe: str | None = None,
    env: dict[str, str] | None = None,
) -> WorkerHandle:
    """Launch a detached ``trialerror jobs start-worker --foreground`` child
    process. On Windows: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` --
    the child outlives this process (surviving the parent's exit / the
    launching console closing) AND is not delivered console control events
    meant for the parent (Ctrl+C in the parent's console does not reach
    it). This is the Python ``subprocess.Popen`` translation of the
    proven ``.cmd``-relaunch pattern already in production use in the origin-project
    repo (``research/tools/embeddings_local/relaunch_reembed.cmd``,
    ``research/tools/marker_ocr/relaunch_batch.cmd``: ``Start-Process
    -WindowStyle Hidden`` over a ``.cmd`` wrapper that redirects stdout/
    stderr to log files) -- same shape, invoked directly as a CLI
    subcommand instead of a hand-written ``.cmd`` file, and restart-safe
    the same way: skip-what-checkpoint-already-covers, not
    re-run-from-scratch.

    ``mode='once'`` claims and runs exactly one job then exits (used for a
    single targeted ``--job-id`` relaunch/resume). ``mode='loop'`` drains
    the eligible queue (see :func:`run_loop`) then exits -- a whole-batch
    run, matching the origin-project runners' own "process everything, then exit"
    shape rather than an unbounded daemon.

    Returns a :class:`WorkerHandle` immediately (does not wait for the
    child); the caller's only handle on the freshly-spawned process is
    ``handle.process`` (kill/poll/wait) until it claims a job, at which
    point the jobs ledger becomes the durable way to observe it (see
    :class:`WorkerHandle`'s docstring).
    """
    program_root = Path(program_root)
    log_dir_path = Path(log_dir) if log_dir is not None else program_root / "jobs_logs"
    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_stem = job_id or f"worker-{new_id('WRK')}"
    log_path = log_dir_path / f"{log_stem}.log"

    argv = [
        python_exe or sys.executable,
        "-m",
        "trialerror.cli",
        "jobs",
        "start-worker",
        "--program-root",
        str(program_root),
        "--mode",
        mode,
        "--foreground",
        "--poll-interval-s",
        str(poll_interval_s),
        "--max-idle-polls",
        str(max_idle_polls),
    ]
    if platform_root is not None:
        argv += ["--platform-root", str(platform_root)]
    if lease_s is not None:
        argv += ["--lease-s", str(lease_s)]
    if max_iterations is not None:
        argv += ["--max-iterations", str(max_iterations)]
    if job_id is not None:
        argv += ["--job-id", job_id]
    if kind is not None:
        argv += ["--kind", kind]
    if payload is not None:
        argv += ["--payload", json.dumps(payload, ensure_ascii=False)]
    if kinds:
        argv += ["--kinds", ",".join(kinds)]
    for mod in extra_handler_modules or ():
        argv += ["--handler-module", mod]

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX equivalent of DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP:
        # setsid() detaches from the controlling terminal so the child
        # survives the parent shell and is not hit by its Ctrl-C. Exercised
        # on every Linux run -- no `pragma: no cover` here, or coverage
        # would hide the only branch that platform ever takes.
        popen_kwargs["start_new_session"] = True

    log_fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(program_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env if env is not None else os.environ.copy(),
            **popen_kwargs,
        )
    finally:
        log_fh.close()  # the child holds its own duplicated handle; ours is done
    return WorkerHandle(pid=proc.pid, argv=argv, log_path=log_path, process=proc)
