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

MINING ADOPTION rowboat-F8 (``docs/reviews/MINING_2026-09_OPERATOR_LINKS.md``
section 3, ``docs/mining/G25-operator-2026-09__rowboat.md`` finding 8;
verdict "adopt-now:jobs -- jitter + wake-signal in run_loop ... port with
the two bug fixes named"): :func:`run_loop` previously slept a bare,
identical ``poll_interval_s`` with no way for an outside caller to
collapse that wait. Two conveniences are bolted on here -- deliberately
NOT an architecture change, since the ledger's claim/lease/heartbeat state
machine is already stronger than the source's single-writer JSON loop:

- :func:`jittered_poll_interval` -- a DETERMINISTIC per-worker (and
  per-poll) offset inside the poll window, so N workers started in the
  same second stop hammering the ledger on the same tick. Deterministic
  (a hash of ``worker_id`` + poll index) rather than ``random`` so a
  given worker's schedule is reproducible in a test and in a postmortem.
- the wake signal -- a token file (:func:`wake_signal_path`, written by
  :func:`kick` / ``trialerror jobs kick``) that a sleeping loop polls; a
  changed token ends the nap immediately. Token CONTENT is compared, never
  mtime, so the mechanism does not depend on filesystem timestamp
  granularity, and the file is never unlinked, so every worker sleeping on
  it observes the same kick exactly once.

The two source bugs the mining report named are fixed on the way in and
marked ``PORTED BUG FIX`` at the lines that fix them: (1) the source never
handles a window whose end precedes its start, and (2) the source can pick
a run time already in the past.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from trialerror.jobs import ledger
from trialerror.jobs.errors import JobPausedError
from trialerror.jobs.registry import discover_and_register_handlers, get_handler
from trialerror.stores import paths
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "DEFAULT_JITTER_FRACTION",
    "EnvironmentalFailure",
    "JobContext",
    "WAKE_SIGNAL_FILENAME",
    "jittered_poll_interval",
    "kick",
    "make_worker_id",
    "read_wake_token",
    "run_one",
    "run_loop",
    "wake_signal_path",
    "WorkerHandle",
    "spawn_worker",
]

#: The wake-signal token file's name. It lives in the program's RESOLVED
#: store directory (``[paths].stores_dir``, default ``stores/``) next to
#: ``jobs.db``, because it is worker coordination state, which design
#: Section 3.2 places with the jobs DB. It is therefore as gitignored as
#: the DB it sits beside and no more: this repo's ``.gitignore`` anchors
#: the pattern as ``/stores/``, so a program scaffolded at the repo root
#: hides its kick automatically, while one scaffolded anywhere else hides
#: it exactly when its own store directory is ignored.
WAKE_SIGNAL_FILENAME = "jobs.wake"

#: Jitter as a fraction of the nominal poll interval, applied +/- around
#: it: 0.25 means "sleep somewhere in [0.75x, 1.25x] of the interval".
#: The mean wait is unchanged; only the phase differs per worker.
DEFAULT_JITTER_FRACTION = 0.25

#: How often a napping loop re-reads the wake token. Small enough that a
#: kick feels immediate, large enough that a 2s nap costs ~40 stats.
DEFAULT_WAKE_TICK_S = 0.05


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


# ---------------------------------------------------------------------------
# rowboat-F8: poll-window jitter + wake signal
# ---------------------------------------------------------------------------
def _load_paths_config(program_root: Path | str) -> dict[str, Any] | None:
    """Best-effort ``[paths]`` read from ``<program_root>/trialerror.toml``
    -- the same private-per-module loader convention
    ``trialerror.dashboard.store_ro`` and every doctor ``checks.py`` already
    use, mirroring the "ambient, no caller opt-in needed" spirit
    ``trialerror.stores.store.open_store``'s ``_auto_load_paths_config``
    established for ``[paths].stores_dir``. Missing/invalid
    ``trialerror.toml`` -> ``None`` (the hardcoded-default-literal
    behavior)."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return None
    try:
        return load_config(cfg_path).raw
    except Exception:  # noqa: BLE001 - a malformed trialerror.toml is not this function's concern
        return None


def wake_signal_path(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    """Where the wake token lives for ``program_root`` -- inside the
    RESOLVED store directory, so a program that moved its stores via
    ``[paths].stores_dir`` moves its wake signal with them.

    ``config=None`` means "discover it" here, NOT "assume the default
    literal" (the convention most ``paths.*`` callers follow). Both sides
    of this mechanism reach the file with only a program root in hand --
    :func:`run_loop` via :func:`_resolve_wake_path`, ``trialerror jobs
    kick`` via :func:`kick`, and the CLI verb deliberately opens no store
    at all -- so a ``config`` neither of them can supply would leave the
    promise above unkept and, worse, let a later fix to ONE side move the
    token out from under the other. Discovering it here keeps the token
    next to the ``jobs.db`` that ``open_store`` (which auto-discovers the
    same way) actually opened. An explicit ``config`` still wins."""
    if config is None:
        config = _load_paths_config(program_root)
    return paths.program_store_dir(program_root, config) / WAKE_SIGNAL_FILENAME


def read_wake_token(path: Path | str | None) -> str | None:
    """The current wake token, or ``None`` when there is no signal file
    (or it cannot be read). Deliberately total: a napping worker must
    never die because the signal file was mid-replace, on a directory that
    does not exist yet, or unreadable -- the worst case is that it sleeps
    out its nap, which is exactly the pre-adoption behaviour."""
    if path is None:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def kick(program_root: Path | str, *, config: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    """Write a fresh wake token: every worker currently napping on this
    program's queue ends its nap at its next tick. Idempotence is NOT
    wanted here -- each call writes a NEW token, which is precisely what
    makes "kick twice" wake a worker twice.

    Write-temp-then-``os.replace`` so a reader can never observe a
    half-written token (``os.replace`` is atomic on both POSIX and NTFS);
    the file itself is never unlinked, so a token is a monotonically
    replaced value rather than a presence flag two workers could race to
    consume.
    """
    path = wake_signal_path(program_root, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = token or new_id("KICK")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.replace(tmp, path)
    return {"wake_signal_path": str(path), "token": token, "ts": now()}


def jittered_poll_interval(
    poll_interval_s: float,
    *,
    worker_id: str,
    poll_index: int = 0,
    jitter_frac: float = DEFAULT_JITTER_FRACTION,
) -> float:
    """A nap length inside ``[poll*(1-jitter_frac), poll*(1+jitter_frac)]``,
    chosen deterministically from ``worker_id`` and ``poll_index``.

    Deterministic, not random: two DIFFERENT workers get different phases
    (the point of the adoption -- they stop claiming on the same tick),
    while the SAME worker's schedule is reproducible, so a test can assert
    an exact nap and an operator reading a log can reconstruct one.
    ``jitter_frac=0`` disables jitter entirely and returns the nominal
    interval.
    """
    lo = poll_interval_s * (1.0 - jitter_frac)
    hi = poll_interval_s * (1.0 + jitter_frac)
    # PORTED BUG FIX 1 (mining report: the source "has no wrap handling
    # when end < start"). A negative jitter_frac -- or any caller that
    # hands the bounds over backwards -- produced a reversed window the
    # source would have sampled as a negative span. Normalize instead.
    if hi < lo:
        lo, hi = hi, lo
    # PORTED BUG FIX 2 (mining report: in the source "the random time can
    # be stamped into the past"). A jitter_frac > 1 drives the low bound
    # below zero; a negative nap is a nap "already over" -- clamp both
    # bounds at zero so the worst case is "poll immediately", never a
    # negative sleep or a deadline behind now().
    lo = max(0.0, lo)
    hi = max(0.0, hi)
    if hi <= lo:
        return lo
    digest = hashlib.blake2b(f"{worker_id}#{poll_index}".encode("utf-8"), digest_size=8).digest()
    frac = int.from_bytes(digest, "big") / float(1 << 64)  # [0, 1)
    return lo + frac * (hi - lo)


def _nap(
    duration_s: float,
    *,
    wake_path: Path | None,
    baseline_token: str | None,
    tick_s: float = DEFAULT_WAKE_TICK_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, str | None]:
    """Sleep up to ``duration_s``, cut short as soon as the wake token
    differs from ``baseline_token``. Returns ``(woken_early, token)``.

    The token is checked BEFORE the first sleep, so a kick that landed
    while the previous job was still running is honoured immediately
    rather than after a full nap. ``monotonic`` (never the wall clock)
    bounds the nap, so a system clock adjustment mid-nap cannot strand a
    worker.
    """
    if wake_path is not None:
        token = read_wake_token(wake_path)
        if token is not None and token != baseline_token:
            return True, token
    if duration_s <= 0:
        return False, baseline_token
    deadline = monotonic() + duration_s
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False, baseline_token
        sleep(min(tick_s, remaining))
        if wake_path is not None:
            token = read_wake_token(wake_path)
            if token is not None and token != baseline_token:
                return True, token


def _resolve_wake_path(store: Store, wake_signal: bool | Path | str) -> Path | None:
    if wake_signal is False:
        return None
    if wake_signal is True:
        return wake_signal_path(store.program_root)
    return Path(wake_signal)


def run_loop(
    store: Store,
    *,
    worker_id: str | None = None,
    kinds: Sequence[str] | None = None,
    lease_s: int = ledger.LEASE_DURATION_S,
    poll_interval_s: float = 2.0,
    max_idle_polls: int = 3,
    max_iterations: int | None = None,
    jitter_frac: float = DEFAULT_JITTER_FRACTION,
    wake_signal: bool | Path | str = True,
    wake_tick_s: float = DEFAULT_WAKE_TICK_S,
) -> list[dict[str, Any]]:
    """Drain the eligible queue: keep calling :func:`run_one` until
    ``max_idle_polls`` consecutive claims come back idle, or
    ``max_iterations`` non-idle jobs have run. The origin-project embed/OCR runners'
    own shape -- "processes the whole batch, then exits"
    (``research/tools/embeddings_local/corpus_embed_runner.py``,
    ``research/tools/marker_ocr/run_batch.py``) -- not an unbounded daemon.

    rowboat-F8 adds two things to the idle wait between polls:

    - ``jitter_frac`` spreads the nap deterministically per worker (see
      :func:`jittered_poll_interval`); pass ``0.0`` for the old fixed nap.
    - ``wake_signal`` (``True`` = this program's
      :func:`wake_signal_path`, a path = that file, ``False`` = disabled)
      lets ``trialerror jobs kick`` end a nap immediately.

    **A wake resets the idle streak.** A kick is an outside caller
    asserting that work now exists, which is the same evidence a
    successful claim gives -- so a loop about to exit on its third idle
    poll stays alive to look. Each wake is recorded in the returned list
    as a ``{"status": "woken", ...}`` entry, so the caller can see WHY a
    loop outlived its ``max_idle_polls`` instead of having to infer it.
    """
    worker_id = worker_id or make_worker_id()
    wake_path = _resolve_wake_path(store, wake_signal)
    last_token = read_wake_token(wake_path)
    results: list[dict[str, Any]] = []
    idle_streak = 0
    non_idle_count = 0
    poll_index = 0
    while True:
        result = run_one(store, worker_id=worker_id, kinds=kinds, lease_s=lease_s)
        results.append(result)
        if result["status"] == "idle":
            idle_streak += 1
            if idle_streak >= max_idle_polls:
                break
            nap_s = jittered_poll_interval(
                poll_interval_s, worker_id=worker_id, poll_index=poll_index, jitter_frac=jitter_frac
            )
            poll_index += 1
            woken, last_token = _nap(
                nap_s, wake_path=wake_path, baseline_token=last_token, tick_s=wake_tick_s
            )
            if woken:
                idle_streak = 0
                results.append(
                    {"status": "woken", "worker_id": worker_id, "token": last_token, "napped_s": nap_s}
                )
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
    jitter_frac: float | None = None,
    wake_signal: bool = True,
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
    # rowboat-F8: appended only when the caller actually overrode the
    # default, so the argv a pre-adoption test asserts on is unchanged.
    if jitter_frac is not None:
        argv += ["--jitter-frac", str(jitter_frac)]
    if not wake_signal:
        argv += ["--no-wake-signal"]
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
        # would hide the only branch that platform ever takes; the branch has
        # its own direct coverage in tests/test_posix_detach.py (which skips
        # cleanly on win32).
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
