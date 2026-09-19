"""The DEV GPU worker: claim -> pull -> run the real backend -> push ->
publish.

Design section 4's DEV row, in one loop. Everything about this module is
shaped by two facts: **DEV is intermittent** (the laptop can be closed
mid-job with no chance to run a cleanup hook) and **DEV is stateless**
(the sandbox owns the ledger, the manifests and every decision; this
process owns only a scratch directory and the GPU).

Consequences that are easy to get wrong and are therefore explicit here:

- A transport failure AFTER the model ran never recomputes. The result
  stays in the local work directory and every later poll retries the
  push/publish first, before claiming anything new (:func:`_retry_publishes`).
  GPU minutes are the scarce resource in this whole design; losing them to
  a dropped SSH connection would be the worst kind of waste.
- A handler exception publishes an ``error.json`` rather than returning
  the job. ``return`` means "unrun, someone else can have it"; a failure
  has to travel back to the sandbox with its reason attached, and push +
  publish is the only channel the seven-verb protocol has for that. The
  ``offload_attempts`` counter is then incremented BY THE SANDBOX when it
  reads that error -- the machine that owns the truth is the machine that
  owns the counter (see ``trialerror.offload.stage._handle_worker_error``).
- Ctrl+C returns the claim. A closed lid cannot: nothing runs at
  suspend/hibernate time on Windows that could be trusted to complete an
  SSH round trip. That gap is what the sandbox's 60-minute
  ``trialerror offload reclaim`` exists to close, and it is why this
  worker heartbeats on a background thread while a model is running -- a
  40-minute marker run must not look abandoned.
- A fake backend is refused before the loop starts (D13). A DEV worker
  that quietly wrote hash-derived vectors into the real record would be
  the single worst failure mode this subsystem has.

**Worker control (ruling C-0097).** Anything the interface can start, the
interface can pause and stop -- *cooperatively*. The sandbox cannot reach this
machine at all and nothing in this harness kills anything, so control is a
request left in the queue (``claimed/<worker>/CONTROL.json``, written only by
the sandbox side) and a word printed back on the ``heartbeat`` verb that this
worker already called every few minutes. :class:`WorkerControl` latches that
word; :func:`_checkpoint` is where the loop honours it, at the three points
where it holds no half-finished unit:

    between embed batches (``_run_embed``) · after marker returns
    (``_run_ocr``, where the unit is the whole job -- D6) · between jobs
    (:func:`run_worker`)

``pause`` finishes the unit, KEEPS the claim and keeps heartbeating;
``resume`` continues from the next unit; ``stop`` finishes the unit, hands an
incomplete claim back through the existing ``return`` verb, and exits. Going
the other way, every heartbeat carries a :class:`ProgressState` snapshot up --
including one under a synthetic id while the worker is idle, so the dashboard
can tell an idle worker from no worker at all.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from trialerror.ingest.errors import PageCountMismatchError
from trialerror.offload import control as control_api
from trialerror.offload import protocol
from trialerror.offload.lock import worker_state_dir
from trialerror.offload.marker import OffloadMarker
from trialerror.offload.stage import (
    EMBED_INPUT_NAME,
    EMBED_OUTPUT_NAME,
    OCR_INPUT_STEM,
    OCR_OUTPUT_NAME,
    embeddable_text,
    read_chunks_payload,
)
from trialerror.offload.transport import Transport, TransportError
from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now

__all__ = [
    "WorkerConfigError",
    "DevBackends",
    "ConfigDevBackends",
    "STAGES",
    "UNRUNNABLE_BACKEND_NAMES",
    "BACKEND_PATH_ATTRS",
    "unrunnable_backend_message",
    "ResidentBackends",
    "PUBLISHED_MARKER",
    "STOPPED_MARKER",
    "IDLE_MESSAGE",
    "STOPPED_MESSAGE",
    "DEFAULT_EMBED_BATCH_SIZE",
    "WorkerControl",
    "ProgressState",
    "default_work_root",
    "run_worker",
]

#: Embed batch size the DEV worker uses when the launcher doesn't say
#: otherwise (``trialerror offload worker --batch-size``).
#:
#: TRIALERROR-DEV-NOTE (FX-S1): this was 8, chosen when every batch meant a
#: fresh driver process and a fresh multi-gigabyte model load -- so the
#: number really meant "how much work is it worth paying a model load for",
#: and 8 was already generous. Observed live 2026-09-06: a 217-chunk
#: document at ``--batch-size 8`` cost 28 model loads. With the resident
#: driver (``trialerror.ingest.backends.RealQwenEmbedBackend`` session
#: mode) the model loads once per worker RUN, so this number goes back to
#: meaning what it should -- how many sequences to hand the GPU at once --
#: and 64 looked like a sane default for a 4B embedding model on consumer
#: VRAM.
#:
#: C-0097 D7: it is 4. Measured, not guessed. At 64 the worker handed the
#: driver 64 texts per call; the sentence-transformers backend then batched
#: 16 sequences of up to 2048 tokens in bf16, and on a 16 GB card that spilled
#: about 16 GB of GPU memory into system RAM (14.9 GB dedicated plus 16.3 GB
#: shared, the card pinned at 100% and 87 C) for roughly SIX SECONDS PER
#: CHUNK. Restarting the same queue at 4 removed the spill entirely and ran
#: several times faster with bit-identical vectors -- so 64 was not buying
#: throughput, it was buying thrashing. The driver's own internal batch of 16
#: is unchanged and can no longer exceed the per-call count, which is what
#: makes this number the one that decides the memory high-water mark.
#:
#: Raise it only with a measurement from the card in front of you. "More per
#: call" stops helping the moment the working set leaves VRAM, and the failure
#: mode is not an error -- it is a run that is quietly ten times slower.
DEFAULT_EMBED_BATCH_SIZE = 4

#: Written beside a finished job's outputs once the sandbox has accepted
#: them, so a restarted worker does not try to publish the same result
#: forever.
PUBLISHED_MARKER = "PUBLISHED"

#: C-0097 D2: written beside a job the operator STOPPED. The partial result
#: under it must never be published -- the claim went back to ``pending/`` and
#: the sandbox is entitled to hand the job to the next worker from the start
#: -- so :func:`_retry_publishes` skips any directory carrying this marker.
#: Without it the very next poll would push a half-finished result at a job
#: the queue no longer believes this worker holds.
STOPPED_MARKER = "STOPPED"

#: Lane e1e Part B: the directory under ``work_root`` that holds chunked
#: OCR jobs' finished page ranges, one subdirectory per job id. Its lifetime
#: is the OPPOSITE of a job directory's -- a job directory is wiped at every
#: claim and swept after a stop, and these files exist precisely so a stopped
#: job's next claim does not re-run the GPU work it already paid for. The
#: name is RESERVED in the job-id namespace by
#: :func:`trialerror.offload.protocol.validate_job_id` (FIX V-3 made that
#: true; it had been claimed here and in the guide while the id was still
#: admitted), so :func:`_retry_publishes` can skip it by name without
#: guessing, and no job can ever own this directory.
OCR_RANGE_CACHE_DIRNAME = protocol.RANGE_CACHE_DIRNAME

#: design section 4: "Idle -> exit with 'Queue empty -- safe to switch DEV
#: off' unless ``--stay``".
IDLE_MESSAGE = "Queue empty - safe to switch DEV off"

#: C-0097 D2: what a stopped worker exits with.
STOPPED_MESSAGE = "Stopped on request - the claim went back to the queue"

_HEARTBEAT_INTERVAL_S = control_api.DEFAULT_HEARTBEAT_INTERVAL_S

#: D3: ``pace_s_per_unit`` is the rolling mean of the last 20 units. Twenty is
#: long enough to survive one slow chunk and short enough that an ETA reacts
#: to a real slowdown (the six-seconds-per-chunk incident would have shown up
#: in about two minutes rather than being averaged away over an hour).
_PACE_WINDOW = 20


class WorkerConfigError(RuntimeError):
    """The DEV program root is not configured to run real models."""


def default_work_root() -> Path:
    return worker_state_dir() / "work"


# ---------------------------------------------------------------------------
# backend resolution
# ---------------------------------------------------------------------------
class DevBackends(Protocol):
    """What the worker needs from DEV's own configuration. A Protocol (not
    a concrete class) purely so the tests can drive the whole loop with
    deterministic stand-ins that are nonetheless NOT the ``Fake*`` classes
    :meth:`validate` refuses -- a test must be able to prove the refusal
    works without being unable to test anything else."""

    def validate(self) -> None: ...
    def ocr(self) -> Any: ...
    def embed(self) -> Any: ...


#: The two backend names a DEV worker refuses to run under, and the stages
#: it checks. ``fake`` would write hash-derived vectors into the record
#: (design D13) and ``offload`` is the SANDBOX's own spelling for "this runs
#: on the other machine" -- a worker resolving it would be asking itself to
#: do the work it is the answer to.
STAGES: tuple[str, ...] = ("ocr", "embed")
UNRUNNABLE_BACKEND_NAMES: tuple[str, ...] = ("fake", "offload")

#: The attribute names a constructed real backend carries that point at
#: something on THIS machine's filesystem (marker's own CLI; the embed
#: driver's interpreter and module directory). Read reflectively so this
#: module keeps knowing nothing about where any of them live -- the values
#: reach an operator only through a runtime envelope, never a tracked file
#: (C-0078).
BACKEND_PATH_ATTRS: tuple[str, ...] = ("marker_single_exe", "python_exe", "module_dir")


def unrunnable_backend_message(stage: str, backend_name: str) -> str:
    """Why a DEV worker will not run one stage under ``backend_name``.

    One text, two readers: :meth:`ConfigDevBackends.validate` raises it as a
    :class:`WorkerConfigError` and :meth:`ConfigDevBackends.describe`
    reports it as data for the ``offload_backend_root_resolved`` doctor
    check. A second copy of this sentence is how a doctor that says "fine"
    and a worker that refuses to start come to stand beside each other."""
    return (
        f"DEV worker refuses to run: [ingest.{stage}] backend = {backend_name!r} in this "
        "program root. The DEV program's trialerror.toml must name the REAL local "
        "backends (marker / qwen3-4b); 'offload' belongs in the SANDBOX toml and 'fake' "
        "must never write into the record (design D13)."
    )


class ConfigDevBackends:
    """Resolve the real backends from a DEV program's ``trialerror.toml``.

    ``[ingest.ocr]``/``[ingest.embed]`` on DEV name the actual local
    installs (``marker_single_exe``, ``python_exe``/``module_dir``) -- the
    "two-machine split" of design section 4: the SANDBOX toml says
    ``backend = "offload"``, the DEV toml says ``backend = "marker"`` /
    ``"qwen3-4b"``.

    The root this config was read from is what ``trialerror offload worker
    --backend-config-root`` names (D-FB-6): on the worker it is read for
    these two tables and nothing else -- the queue comes from ``--remote``
    or ``--queue-root``.
    """

    def __init__(self, config: dict[str, Any] | None):
        self.config = dict(config or {})

    def _table(self, stage: str) -> dict[str, Any]:
        return (self.config.get("ingest") or {}).get(stage) or {}

    def backend_name(self, stage: str) -> str:
        """What ``[ingest.<stage>] backend`` says, defaulted the way the
        loaders default it (``fake``)."""
        return str(self._table(stage).get("backend", "fake"))

    def validate(self) -> None:
        """Refuse a DEV configuration that would produce fake or offloaded
        results. Both stages are checked up front, before a single job is
        claimed, so the operator learns about a misconfiguration in the
        first second of the launcher rather than after a 40-minute claim."""
        for stage in STAGES:
            backend_name = self.backend_name(stage)
            if backend_name in UNRUNNABLE_BACKEND_NAMES:
                raise WorkerConfigError(unrunnable_backend_message(stage, backend_name))
        # Constructing them also surfaces a missing marker_single_exe /
        # python_exe / module_dir now rather than mid-job.
        self.ocr()
        self.embed()

    def describe(self) -> dict[str, Any]:
        """:meth:`validate`'s own reading of this root, as DATA instead of a
        refusal -- what the ``offload_backend_root_resolved`` doctor check
        reports (lane FB-1b, D-FB-6).

        Per stage: the backend name, whether this machine is the one that
        runs it, the refusal text if a worker would decline it, whether the
        backend OBJECT could be constructed (the same
        :meth:`ocr`/:meth:`embed` calls ``validate`` makes, so a
        construction the worker would fail cannot pass here), the error if
        it could not, and for each filesystem attribute the object carries
        whether that path exists right now.

        A ``fake``/``offload`` stage is NOT constructed, exactly as
        ``validate`` does not reach its construction: the refusal comes
        first for both readers.

        Never raises. A doctor check that tracebacked over the thing it is
        checking would leave an operator with less than the check they ran.
        """
        stages: dict[str, Any] = {}
        for stage in STAGES:
            name = self.backend_name(stage)
            entry: dict[str, Any] = {
                "backend": name,
                "runs_here": name not in UNRUNNABLE_BACKEND_NAMES,
                "refusal": None,
                "constructed": None,
                "error": None,
                "paths": {},
            }
            if not entry["runs_here"]:
                entry["refusal"] = unrunnable_backend_message(stage, name)
                stages[stage] = entry
                continue
            try:
                backend = self.ocr() if stage == "ocr" else self.embed()
            except Exception as exc:  # noqa: BLE001 - the failure IS the reading
                entry["constructed"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
            else:
                entry["constructed"] = True
                for attr in BACKEND_PATH_ATTRS:
                    value = getattr(backend, attr, None)
                    if value:
                        entry["paths"][attr] = {"value": str(value), "exists": Path(str(value)).exists()}
            stages[stage] = entry
        return {"stages": stages}

    def ocr(self) -> Any:
        from trialerror.ingest.backends import load_ocr_backend

        return _refuse_unreal(load_ocr_backend(self._table("ocr")), "ocr")

    def embed(self) -> Any:
        from trialerror.ingest.backends import load_embed_backend

        return _refuse_unreal(load_embed_backend(self._table("embed")), "embed")


class ResidentBackends:
    """Resolve each stage's backend AT MOST ONCE for a whole worker run,
    and close them on the way out.

    Why this exists (FX-S1, observed live 2026-09-06). ``_process_one``
    used to call ``backends.ocr()`` / ``backends.embed()`` per job, and
    ``ConfigDevBackends`` builds a NEW backend object on every such call.
    With the one-shot embed driver that was invisible -- the model was
    reloaded per BATCH anyway, so per-job made no difference. With the
    resident driver it is the whole game: a per-job backend would start
    (and abandon) a driver process per job, which is the bug moved rather
    than fixed. One instance per run means one model load per run.

    It also owns the shutdown that a resident process now requires: the
    ``finally`` in :func:`run_worker` calls :meth:`close`, so a worker that
    drains the queue and exits leaves no GPU process behind (the
    ``atexit`` hook in ``trialerror.ingest.backends`` is the backstop for
    the paths that never reach that ``finally``, not the design).

    Wraps any :class:`DevBackends` -- including the tests' stand-ins -- and
    is itself one, so nothing downstream has to know it is here.

    **C-0097 D9 -- unload the idle backend on a kind switch.** Measured on the
    DEV worker the same day as the batch-size spill: once the embed queue
    drained into the OCR jobs, the resident embedding driver stayed loaded
    (11.2 GB RSS, 19 GB commit) while marker ran beside it (6.5 GB RSS,
    18.6 GB commit), leaving 0.9 GB of RAM free and 63 of 69.5 GB committed --
    a machine one allocation away from the pager. "One instance per run" was
    never meant to mean "both stages resident for the whole run on a box that
    has room for one". So :meth:`for_stage` unloads the OTHER stage before
    starting this one, which for the embed driver means its subprocess exits
    and its VRAM goes back. ``keep_resident=True`` preserves the old
    behaviour for a machine with the memory; reloading costs about a minute
    per switch, and the queue's lexical order makes switches rare.
    """

    def __init__(
        self,
        backends: "DevBackends",
        *,
        log: Callable[[str], None] | None = None,
        keep_resident: bool = False,
    ):
        self._backends = backends
        self._log = log
        self._keep_resident = keep_resident
        self._ocr: Any = None
        self._embed: Any = None

    def validate(self) -> None:
        self._backends.validate()

    def ocr(self) -> Any:
        if self._ocr is None:
            self._ocr = self._backends.ocr()
        return self._ocr

    def embed(self) -> Any:
        if self._embed is None:
            self._embed = self._backends.embed()
            # The one narration this subsystem's operator actually needs
            # out of session mode: "driver started" appearing ONCE in a
            # run is the proof the model is not being reloaded per batch.
            if self._log is not None and hasattr(self._embed, "log"):
                self._embed.log = self._log
        return self._embed

    def for_stage(self, stage: str) -> Any:
        """The backend for one job's stage, with the other stage unloaded
        first unless ``keep_resident`` (D9).

        This is the ONLY entry point the loop uses, so the policy cannot be
        bypassed by a later caller reaching for :meth:`ocr`/:meth:`embed`
        directly -- those two stay as the plain resolvers they were."""
        if stage == "ocr":
            if not self._keep_resident:
                self._unload("embed")
            return self.ocr()
        if stage == "embed":
            if not self._keep_resident:
                self._unload("ocr")
            return self.embed()
        raise RuntimeError(f"unknown offload stage {stage!r}")

    def resident_kinds(self) -> list[str]:
        """Which stages currently hold a loaded backend -- reported as
        ``settings.resident_backends`` in the heartbeat payload (D9), so an
        operator watching the card can see the switch happen instead of
        inferring it from a memory graph."""
        return [name for name, backend in (("ocr", self._ocr), ("embed", self._embed)) if backend is not None]

    def _unload(self, stage: str) -> None:
        backend = self._embed if stage == "embed" else self._ocr
        if backend is None:
            return
        self._close_one(backend)
        if stage == "embed":
            self._embed = None
        else:
            self._ocr = None
        if self._log is not None:
            self._log(f"  - unloaded the resident {stage} backend (job kind switched)")

    @staticmethod
    def _close_one(backend: Any) -> None:
        closer = getattr(backend, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must not fail a finished run
                pass

    def close(self) -> None:
        """Best-effort shutdown of both stages. A backend with no
        ``close`` (every OCR backend, the fake/stub embed backends) is
        simply skipped, and a failure to close is swallowed -- the worker
        has already published its results by then, and a noisy teardown
        must not turn a good run into a bad exit."""
        for backend in (self._embed, self._ocr):
            self._close_one(backend)
        self._embed = None
        self._ocr = None


def _refuse_unreal(backend: Any, stage: str) -> Any:
    from trialerror.ingest.backends import FakeEmbedBackend, FakeOcrBackend

    if isinstance(backend, (FakeOcrBackend, FakeEmbedBackend, OffloadMarker)):
        raise WorkerConfigError(
            f"DEV worker refuses to run the {stage} stage through {type(backend).__name__} -- "
            "only a real local backend may publish results into the record (design D13)"
        )
    return backend


# ---------------------------------------------------------------------------
# control: the flag the heartbeat sets and the checkpoints read (C-0097 D1/D2)
# ---------------------------------------------------------------------------
class WorkerControl:
    """The worker's side of the control word: a LATCH pair, set from the
    heartbeat reply and read at the loop's cooperative checkpoints.

    Latches, not momentary signals, and that is the whole semantics:

    * ``pause`` is set by the word ``pause`` and cleared ONLY by ``resume``.
      ``none`` means "no new instruction", never "carry on": the alternative
      would be a control file that has to stay on disk for as long as the
      pause lasts, and D1's one-hour TTL would then silently resume a paused
      worker an hour later -- the opposite of a deliberate control surface.
    * ``stop`` is set by ``stop`` and is never cleared. There is no un-stop:
      the worker finishes its unit, hands the claim back and exits, and the
      next run starts clean.
    * ``stop`` while ``paused`` wins immediately (D2's last clause), which is
      why :meth:`wait_while_paused` returns on either event rather than only
      on a resume.

    Thread-safe by a single lock: the only writer is the heartbeat thread
    (:meth:`observe`), the only readers are the checkpoints on the main
    thread. ``transitions`` is the audit trail D2 asks for -- one entry per
    state change, logged as it happens and carried into the next heartbeat
    payload as ``control_seen``.
    """

    def __init__(self, *, log: Callable[[str], None] | None = None):
        self._lock = threading.Lock()
        self._paused = False
        self._stopping = False
        self._seen: str | None = None
        self._wake = threading.Event()
        self._log = log or (lambda _msg: None)
        self.transitions: list[dict[str, Any]] = []

    # -- the heartbeat thread's end ---------------------------------------
    def observe(self, word: str | None) -> None:
        """Apply one control word from a heartbeat reply."""
        if not word or word == control_api.CONTROL_NONE:
            return
        if word not in control_api.CONTROL_REQUESTS:
            return
        with self._lock:
            self._seen = word
            if word == "pause" and not self._paused:
                self._paused = True
                self._record("paused")
            elif word == "resume" and self._paused:
                self._paused = False
                self._record("resumed")
            elif word == "stop" and not self._stopping:
                self._stopping = True
                self._record("stopping")
        self._wake.set()

    def _record(self, transition: str) -> None:
        """Called with the lock held."""
        entry = {"transition": transition, "ts": now()}
        self.transitions.append(entry)
        self._log(f"  ~ control: {transition}")

    # -- the checkpoints' end ---------------------------------------------
    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping

    @property
    def control_seen(self) -> str | None:
        with self._lock:
            return self._seen

    def expire_pause(self) -> bool:
        """Clear a pause because the caller's own bound ran out (FIX V-4).

        Only :meth:`wait_while_paused` calls this, and only when its caller
        passed a ``max_pause_s``. It is a real transition, logged and carried
        on the next beat like every other one: a worker that silently went
        back to work while still reporting ``paused`` -- which is what this
        latch used to do on expiry -- is a worker whose card lies about what
        the GPU is doing."""
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
            self._record("pause_expired")
            return True

    def wait_while_paused(
        self,
        *,
        poll_s: float = 1.0,
        max_pause_s: float | None = None,
        beat: Callable[[], Any] | None = None,
        beat_interval_s: float | None = None,
    ) -> str:
        """Block while paused. Returns ``"resumed"``, ``"stopping"`` or
        ``"expired"``.

        **The wait BEATS** (FIX V-1). A pause is only a pause while the
        sandbox can still be heard: the control word arrives on a heartbeat
        reply and nowhere else, so a wait that stops beating cannot be
        resumed, cannot be stopped, and -- with the production default of no
        bound at all -- never ends. It also ages out of the queue's lost
        window and the card says ``LOST`` for a worker that is sitting
        obediently where it was told to. Where a heartbeat THREAD is already
        running (inside a job) the caller passes no ``beat`` and this loop
        just waits on the event that thread sets; where there is none
        (between jobs, at the top of the poll loop) the caller passes one and
        this loop beats it every ``beat_interval_s``.

        ``max_pause_s`` bounds ONE pause, measured from the moment this wait
        starts -- not a budget for the whole job (FIX V-4) -- and expiring
        clears the latch through :meth:`expire_pause` so the worker's
        reported state and its behaviour cannot disagree. ``None``, the
        production default, is the design's own semantics: a pause lasts
        until an operator resumes or stops it."""
        interval = float(beat_interval_s) if beat_interval_s else None
        deadline = None if max_pause_s is None else time.monotonic() + float(max_pause_s)
        next_beat = time.monotonic()
        while True:
            with self._lock:
                if self._stopping:
                    return "stopping"
                if not self._paused:
                    return "resumed"
            if deadline is not None and time.monotonic() >= deadline:
                self.expire_pause()
                return "expired"
            if beat is not None and time.monotonic() >= next_beat:
                next_beat = time.monotonic() + (interval if interval else poll_s)
                beat()
                # Re-read the flags: that beat may have carried the resume or
                # the stop that ends this wait.
                continue
            timeout = poll_s
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - time.monotonic()))
            if beat is not None:
                timeout = min(timeout, max(0.0, next_beat - time.monotonic()))
            self._wake.clear()
            self._wake.wait(timeout)


class ProgressState:
    """What the heartbeat carries up (C-0097 D3): one mutable reading of
    "what is this worker doing", shared between the loop (which writes it)
    and the heartbeat thread (which serialises and sends it).

    Guarded by its own lock for the same reason :class:`WorkerControl` is: two
    threads, one of them reading a multi-field snapshot that must not be torn
    halfway through a unit boundary.

    ``pace_s_per_unit`` and ``eta_s`` are DERIVED here rather than on the
    sandbox, because only this side knows how long a unit took. The ETA is
    deliberately the simplest possible arithmetic over the rolling pace: an
    ETA that models anything more than "the last twenty units, repeated" is
    an ETA that is confidently wrong when the job changes shape."""

    def __init__(self, *, worker_id: str, settings: dict[str, Any] | None = None):
        self._lock = threading.Lock()
        self.worker_id = worker_id
        self._state = "idle"
        self._kind: str | None = None
        self._job_id: str | None = None
        self._unit: str | None = None
        self._units_done = 0
        self._units_total: int | None = None
        self._started_ts: str | None = None
        self._last_error: str | None = None
        self._settings: dict[str, Any] = dict(settings or {})
        self._paces: deque[float] = deque(maxlen=_PACE_WINDOW)
        self._last_unit_at: float | None = None

    # -- the loop's end ---------------------------------------------------
    def start_job(self, job_id: str, *, kind: str | None, unit: str, units_total: int | None) -> None:
        with self._lock:
            self._state = "running"
            self._job_id = job_id
            self._kind = kind
            self._unit = unit
            self._units_done = 0
            self._units_total = units_total
            self._started_ts = now()
            self._paces.clear()
            self._last_unit_at = time.monotonic()

    def claiming(self, job_id: str) -> None:
        with self._lock:
            self._state = "claiming"
            self._job_id = job_id

    def units_done(self, done: int) -> None:
        """Record progress to ``done`` units, timing the interval since the
        last call as the pace of the units in between."""
        with self._lock:
            previous = self._units_done
            self._units_done = int(done)
            moment = time.monotonic()
            if self._last_unit_at is not None and done > previous:
                per_unit = (moment - self._last_unit_at) / float(done - previous)
                for _ in range(min(done - previous, _PACE_WINDOW)):
                    self._paces.append(per_unit)
            self._last_unit_at = moment

    def set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def set_settings(self, **fields: Any) -> None:
        with self._lock:
            self._settings.update({k: v for k, v in fields.items()})

    def set_last_error(self, message: str | None) -> None:
        with self._lock:
            self._last_error = message

    def idle(self) -> None:
        self._reset("idle")

    def exited(self) -> None:
        """The state a run's LAST beat carries (FIX V-9).

        Every run used to end with an ``idle`` beat, so a worker whose process
        had exited read as "alive, nothing to do" for the whole lost window --
        eleven minutes at the default interval -- and an operator could not tell
        a drained queue from a laptop that had gone. This worker cannot delete
        its own progress file (no verb of the seven-verb contract can), so the
        honest alternative is a word the card and the status line can label."""
        self._reset("exited")

    def _reset(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._kind = None
            self._job_id = None
            self._unit = None
            self._units_done = 0
            self._units_total = None
            self._paces.clear()
            self._last_unit_at = None

    @property
    def state(self) -> str:
        """What this worker last said it was doing. Read by
        :func:`_checkpoint`, which RESTORES it after a pause rather than
        assuming ``running``: a worker paused between jobs holds no claim, and
        a beat that called that state ``running`` would put a job on the card
        that does not exist."""
        with self._lock:
            return self._state

    @property
    def done(self) -> int:
        with self._lock:
            return self._units_done

    @property
    def total(self) -> int | None:
        with self._lock:
            return self._units_total

    # -- the heartbeat thread's end ---------------------------------------
    def snapshot(self, *, control_seen: str | None = None) -> dict[str, Any]:
        """One D3 payload. Keys with nothing to say are omitted rather than
        sent as ``null``: the cap is on bytes, and a payload is read by a
        card that draws only what it was told."""
        with self._lock:
            pace = sum(self._paces) / len(self._paces) if self._paces else None
            remaining = (
                None
                if self._units_total is None or pace is None
                else max(0, self._units_total - self._units_done) * pace
            )
            payload: dict[str, Any] = {
                "worker_id": self.worker_id,
                "state": self._state,
                "units_done": self._units_done,
            }
            if self._kind is not None:
                payload["kind"] = self._kind
            if self._job_id is not None:
                payload["job_id"] = self._job_id
            if self._unit is not None:
                payload["unit"] = self._unit
            if self._units_total is not None:
                payload["units_total"] = self._units_total
            if self._started_ts is not None:
                payload["started_ts"] = self._started_ts
            if pace is not None:
                payload["pace_s_per_unit"] = round(pace, 4)
            if remaining is not None:
                payload["eta_s"] = round(remaining, 1)
            if self._settings:
                payload["settings"] = dict(self._settings)
            if control_seen is not None:
                payload["control_seen"] = control_seen
            if self._last_error is not None:
                payload["last_error"] = self._last_error
            return payload


# ---------------------------------------------------------------------------
# heartbeats while a model runs
# ---------------------------------------------------------------------------
class _Heartbeat:
    """Beat ``transport.heartbeat(job_id)`` on a daemon thread until the
    context exits. A dropped beat is swallowed: a transient SSH failure
    must not kill a running marker job, and the 60-minute expiry is
    forgiving enough to survive several misses.

    C-0097 adds both halves of the control channel to this one thread: the
    progress payload goes UP with each beat and the control word comes DOWN,
    straight into :meth:`WorkerControl.observe`. Nothing else in the worker
    talks to the control surface -- which is why a pause takes effect within
    one beat everywhere, including inside a 40-minute marker run's ONE unit
    (where it simply has to wait, D6).

    The first beat is sent immediately rather than after ``interval_s``: on a
    five-minute interval, a worker that claimed a job would otherwise be
    invisible to the dashboard for five minutes, and an operator who paused it
    one second after it started would wait out the same five minutes to see
    ``PAUSED``. A beat costs one SSH round trip."""

    def __init__(
        self,
        transport: Transport,
        job_id: str,
        interval_s: float = _HEARTBEAT_INTERVAL_S,
        *,
        progress: "ProgressState | None" = None,
        control: "WorkerControl | None" = None,
        log: Callable[[str], None] | None = None,
    ):
        self.transport = transport
        self.job_id = job_id
        self.interval_s = interval_s
        self.progress = progress
        self.control = control
        self.beats = 0
        #: FIX V-6: how many beats the queue refused because of the payload, and
        #: how many of those were then delivered BARE. Counters rather than a
        #: log line alone, because "the wrapper out there is older than this
        #: worker" is a rollout state a test has to be able to assert.
        self.payload_refusals = 0
        self.bare_beats = 0
        self._log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> str | None:
        """One beat, inline. Returns the control word, or ``None`` when the
        beat could not be delivered at all."""
        payload = None
        if self.progress is not None:
            seen = self.control.control_seen if self.control is not None else None
            try:
                payload = control_api.encode_progress(self.progress.snapshot(control_seen=seen))
            except control_api.ControlError:
                # A payload this worker's own code built wrongly must not cost
                # the job its heartbeat. Beat without it; the card then shows
                # the claim as live with no detail, which is the honest state.
                payload = None
        try:
            word = self.transport.heartbeat(self.job_id, progress=payload)
        except Exception:  # noqa: BLE001 - a missed beat is not a job failure
            if payload is None:
                return None
            # FIX V-6: the payload is OPTIONAL (D3) and this worker was treating
            # it as mandatory. A queue that refuses it -- a wrapper one version
            # behind this worker, since the two live on different machines, or
            # any key it does not know -- would otherwise cost the job EVERY
            # stamp for its whole run: the 60-minute reclaim then re-queues a
            # job that is still running, no control word ever arrives again, and
            # the worker has no row for the doctor to notice. So drop the
            # payload and beat bare, which is all the protocol ever required.
            self.payload_refusals += 1
            if self.payload_refusals == 1:
                self._log(
                    "  ! heartbeat payload refused by the queue -- beating without it "
                    "(deploy the queue-host wrapper before this worker)"
                )
            try:
                word = self.transport.heartbeat(self.job_id, progress=None)
            except Exception:  # noqa: BLE001 - now it really is a missed beat
                return None
            self.bare_beats += 1
        self.beats += 1
        if self.control is not None:
            self.control.observe(word)
        return word

    def __enter__(self) -> "_Heartbeat":
        def loop() -> None:
            self.beat()
            while not self._stop.wait(self.interval_s):
                self.beat()

        self._thread = threading.Thread(target=loop, name=f"offload-hb-{self.job_id}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# the stages, DEV side
# ---------------------------------------------------------------------------
def _input_name(manifest: dict[str, Any], default: str) -> str:
    expect = manifest.get("expect") or {}
    name = expect.get("input_name")
    if isinstance(name, str) and name:
        return name
    entries = manifest.get("inputs") or []
    if entries and isinstance(entries[0], dict) and entries[0].get("name"):
        return str(entries[0]["name"])
    return default


def _verify_inputs(manifest: dict[str, Any], in_dir: Path) -> None:
    for entry in manifest.get("inputs") or []:
        name = entry.get("name")
        path = in_dir / str(name)
        if not path.is_file():
            raise RuntimeError(f"pulled archive is missing declared input {name!r}")
        actual = protocol.sha256_bytes(path.read_bytes())
        if actual != entry.get("sha256"):
            raise RuntimeError(
                f"pulled input {name!r} sha256 mismatch (declared "
                f"{str(entry.get('sha256'))[:12]}..., actual {actual[:12]}...)"
            )


def _checkpoint(
    control: "WorkerControl | None",
    *,
    progress: "ProgressState | None" = None,
    max_pause_s: float | None = None,
    beat: Callable[[], Any] | None = None,
    beat_interval_s: float | None = None,
) -> str:
    """One cooperative checkpoint (C-0097 D2). Returns ``"stop"`` or ``"go"``.

    The three places this is called from -- between embed batches, after
    marker returns, between jobs -- are the complete list, and each one sits
    exactly where the worker holds no half-finished unit. That is the whole
    safety argument for cooperative control: at a checkpoint there is nothing
    to roll back, so "stop" can be honoured by doing nothing further rather
    than by undoing something.

    A pause here reports ``state: "paused"`` on the next beat and BLOCKS,
    claim held, until a resume or a stop arrives. A stop that arrives while
    paused wins without waiting for another unit.

    **``beat`` is mandatory for any checkpoint that can be reached with no
    heartbeat thread running** (FIX V-1). The control word arrives on a
    heartbeat reply and nowhere else, so a wait with nothing beating can never
    learn the word that would end it. The two checkpoints in
    :func:`run_worker` (top of the poll loop, between jobs) are exactly those:
    they pass the idle beat. The two inside a job are covered by
    ``_process_one``'s own :class:`_Heartbeat` thread and pass nothing.

    ``max_pause_s`` bounds ONE pause rather than the job it happens in, and an
    expiry clears the latch (FIX V-4), so the state this restores afterwards
    is the truth either way."""
    if control is None:
        return "go"
    if control.stopping:
        return "stop"
    if control.paused:
        previous = progress.state if progress is not None else None
        if progress is not None:
            progress.set_state("paused")
        control.wait_while_paused(
            max_pause_s=max_pause_s, beat=beat, beat_interval_s=beat_interval_s
        )
        if control.stopping:
            if progress is not None:
                progress.set_state("stopping")
            return "stop"
        if progress is not None and previous is not None:
            progress.set_state(previous)
    return "go"


@dataclass
class StageOutcome:
    """What one stage did, and whether the operator's stop landed inside it.

    The distinction ``complete`` carries is the one thing a naive reading of
    D2 gets wrong. "Stop = finish the current unit, write a partial result,
    hand the claim back" is exactly right when a unit boundary sits INSIDE the
    job (embed: one chunk batch of many). When the unit IS the job (OCR, D6),
    finishing the current unit finishes the whole thing -- and handing that
    claim back would throw away a forty-minute marker run that already
    succeeded, in a subsystem whose first principle is that GPU minutes are
    never recomputed. So a stop on a COMPLETE unit publishes the result and
    ends the run after it; only an INCOMPLETE one takes the partial path."""

    fields: dict[str, Any]
    stopped: bool = False
    complete: bool = True
    units_done: int = 0
    units_total: int | None = None
    unit: str = "job"


def _ocr_ranges_incomplete() -> type[Exception]:
    """:class:`trialerror.ingest.backends.OcrRangesIncomplete`, imported when
    it is needed rather than at module import.

    ``trialerror.ingest.backends`` imports ``trialerror.offload.marker``, and
    this module is on the other side of that seam on purpose: the worker
    reaches into the ingest package through
    :class:`ConfigDevBackends`'s own lazy imports and nowhere else. One
    function so the exception class is named once."""
    from trialerror.ingest.backends import OcrRangesIncomplete

    return OcrRangesIncomplete


def _range_cache_dir(work_root: Path, job_id: str) -> Path:
    """Where a chunked OCR job keeps the ranges it has already produced.

    **Beside the job directories, never inside one.** ``_process_one`` wipes
    ``<work_root>/<job_id>`` at every claim and ``_retry_publishes`` removes
    it outright once a stop has been swept, which is exactly right for a
    partial result nobody may publish -- and exactly wrong for the GPU hours
    a resume is supposed to reuse. The two have opposite lifetimes, so they
    live in two places: the name is reserved in the job-id namespace
    (:func:`trialerror.offload.protocol.validate_job_id` refuses it, FIX
    V-3), so no job can ever own this directory, and
    :func:`_retry_publishes` skips it by name.

    The cache is removed when the job's OCR completes; a job stopped and
    never resumed leaves one behind on purpose, because that is the state it
    exists for."""
    return work_root / OCR_RANGE_CACHE_DIRNAME / job_id


def _expected_page_count(manifest: dict[str, Any]) -> int | None:
    expect = manifest.get("expect") or {}
    value = expect.get("page_count")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _run_ocr(
    backend: Any,
    manifest: dict[str, Any],
    in_dir: Path,
    out_dir: Path,
    scratch: Path,
    *,
    control: "WorkerControl | None" = None,
    progress: "ProgressState | None" = None,
    max_pause_s: float | None = None,
    range_cache: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> StageOutcome:
    """One OCR job.

    **The unit is the JOB when marker runs once for the document** (C-0097
    D6) and **the RANGE when it does not** (lane e1e Part B). Which one this
    is depends on the document: a backend that can plan page ranges is asked
    to, and a plan of more than one range is driven range by range with the
    cooperative checkpoint between them -- the same checkpoint
    :func:`_run_embed` takes between batches, on the stage that used to offer
    no pause point at all. On a 540-page A4 scan that is a place to stop
    every 16 pages at the default budget instead of one place to stop, forty
    minutes in.

    A stop that lands on a range boundary is an INCOMPLETE unit: the claim
    goes back and the ranges already produced stay in ``range_cache``, so the
    next worker to claim this job re-runs only what was never produced. A
    stop after the LAST range is a complete one and publishes, for the reason
    :class:`StageOutcome` gives -- GPU minutes that have already been spent
    are never thrown away."""
    input_path = in_dir / _input_name(manifest, OCR_INPUT_STEM)
    job_id = str(manifest.get("job_id") or "")
    plan = backend.plan_ranges(input_path) if getattr(backend, "supports_page_ranges", False) else None

    if not plan or len(plan) <= 1:
        if progress is not None:
            progress.start_job(job_id, kind="ocr", unit="job", units_total=1)
        result = backend.run(input_path=input_path, work_dir=scratch)
        if progress is not None:
            progress.units_done(1)
        _write_ocr_pages(out_dir, manifest, result)
        fields = {"backend": result.ocr_backend, "version": result.ocr_version}
        # The checkpoint is AFTER the output is written: the unit is finished, so
        # a stop here keeps the page text this GPU minute already bought.
        stopped = _checkpoint(control, progress=progress, max_pause_s=max_pause_s) == "stop"
        return StageOutcome(
            fields=fields, stopped=stopped, complete=True, units_done=1, units_total=1, unit="job"
        )

    if progress is not None:
        progress.start_job(job_id, kind="ocr", unit="range", units_total=len(plan))
    # The plan, in the worker's own log rather than on the heartbeat. The
    # payload's settings block is an ALLOWLIST both halves of the protocol
    # share, and the queue-host half is a POSIX-sh wrapper on another machine:
    # a key this worker invents is refused on arrival, and the FIX V-6
    # fallback then beats BARE for the whole job -- a card with no detail at
    # all, which is a worse trade than a log line for a number nobody steers
    # by. "43 of 500 pages" belongs on the card the day the wrapper's
    # allowlist is rolled forward with it, not before.
    # The planning DPI belongs on this line because it is the number the
    # range count is derived FROM: page area goes as DPI squared, so "4
    # ranges of 65" and "34 ranges of 16" are the same document planned
    # against two different beliefs about how big marker's page images are.
    # An operator who thinks the plan is too fine is looking for exactly
    # this.
    planning_dpi = getattr(backend, "planning_dpi", None)
    at_dpi = (
        f" at {planning_dpi:g} dpi ({getattr(backend, 'planning_dpi_source', 'bounded_dpi')})"
        if planning_dpi
        else ""
    )
    log_plan = (
        f"  . {job_id}: ocr planned as {len(plan)} range(s) of up to "
        f"{plan[0].count} page(s) over {plan[-1].last + 1} page(s){at_dpi}"
    )
    if log is not None:
        log(log_plan)

    def _after_range(done: int, total: int) -> str:
        if progress is not None:
            progress.units_done(done)
        return _checkpoint(control, progress=progress, max_pause_s=max_pause_s)

    def _range_cost(record: dict[str, Any]) -> None:
        """What one range cost, in this worker's own log.

        The reason it is here and not on the heartbeat is the reason the
        plan line is (see above): the card's settings block is an allowlist
        the queue host shares, so a key this worker invents is refused on
        arrival. The reason it exists at all is that `max_range_pixels`
        cannot be sized from the raster arithmetic -- it under-states a
        marker process by two orders of magnitude -- so the operator needs a
        MEASUREMENT of one small range, and the machine that could take one
        by hand is usually not the machine they are sitting at."""
        if log is None:
            return
        peak = record.get("peak_rss_bytes")
        # GB, because this is a number read next to a memory budget. The
        # source rides with it: a POSIX reading is a high-water mark over
        # every child so far, not this range's own peak, and a number
        # printed without that is a number somebody will subtract.
        cost = (
            f"peak {peak / 1_000_000_000:.1f} GB [{record.get('peak_rss_source')}]"
            if isinstance(peak, (int, float)) and peak
            else "peak not measured on this platform"
        )
        where = "from cache" if record.get("cached") else "ran"
        log(
            f"  . {job_id}: ocr range {record['first_page']}-{record['last_page']} {where} "
            f"in {float(record.get('range_wall_s') or 0.0):.1f}s, {cost}"
        )

    try:
        result = backend.run(
            input_path=input_path,
            work_dir=scratch,
            ranges=plan,
            on_range=_after_range,
            on_range_record=_range_cost,
            cache_dir=range_cache,
        )
    except _ocr_ranges_incomplete() as stop:
        # Nothing is written to out_dir: the per-range outputs are in the
        # cache, which is where a resume reads them from, and a partial
        # pages.json would be a publishable-looking file for a document
        # whose later pages do not exist yet.
        return StageOutcome(
            fields={
                "backend": getattr(backend, "name", "marker"),
                "version": getattr(backend, "version", None),
                "ranges_total": stop.ranges_total,
            },
            stopped=True,
            complete=False,
            units_done=stop.ranges_done,
            units_total=stop.ranges_total,
            unit="range",
        )

    # WHICH convention the `{N}` markers were read under, and what settled
    # it. A page number is only meaningful next to the rule that produced it,
    # and this one is a fact about the marker release on THIS machine -- so
    # it belongs in the result a different machine reads, and in this
    # worker's own log where an operator watching the run can see it.
    numbering_line = (
        f"  . {job_id}: ocr page numbering = {result.page_range_numbering} "
        f"(decided by {result.page_range_numbering_decided_by})"
    )
    if log is not None:
        log(numbering_line)
    _write_ocr_pages(out_dir, manifest, result)
    fields = {
        "backend": result.ocr_backend,
        "version": result.ocr_version,
        "page_count": result.page_count,
        "page_range_numbering": result.page_range_numbering,
        # What the ranges were sized against. marker holds every page of a
        # range as a high-res image at once, so the plan is only as honest as
        # this number is.
        "planning_dpi": result.planning_dpi,
        "planning_dpi_source": result.planning_dpi_source,
        # Namespaced rather than a bare `decided_by`: the result manifest is
        # a flat document several stages write into, and "decided_by" alone
        # would be a key nobody could attribute a year from now.
        "page_range_numbering_decided_by": result.page_range_numbering_decided_by,
        # One entry per range with its own sha256 -- what makes a resumed
        # document's output reconcilable range by range rather than only in
        # total, and the record of which invocation produced which pages.
        "ranges": [dict(r) for r in result.ranges],
    }
    stopped = _checkpoint(control, progress=progress, max_pause_s=max_pause_s) == "stop"
    if range_cache is not None:
        # The document is finished; the ranges that built it are no longer
        # anybody's resume.
        shutil.rmtree(range_cache, ignore_errors=True)
    return StageOutcome(
        fields=fields,
        stopped=stopped,
        complete=True,
        units_done=len(plan),
        units_total=len(plan),
        unit="range",
    )


def _write_ocr_pages(out_dir: Path, manifest: dict[str, Any], result: Any) -> None:
    """The job's ``pages.json``, after the one check the worker can make
    about whether this is the right document.

    ``expect.page_count`` is the count the QUEUE side recorded for this
    document, when it recorded one; ``result.page_count`` is the page tree
    this machine just read. Two different numbers mean the bytes here are not
    the bytes the record is about, and every page number in the output would
    be an assertion about the wrong document -- so the job fails with both
    numbers in it rather than publishing pages that will never line up with
    the anchors drawn from them.

    **How often it fires, stated rather than assumed** (FIX V-7): nothing
    counts pages at registration, and ``document.page_count`` is written by
    one route in the tree (the DjVu -> derived-PDF conversion), so a PDF
    acquired directly declares ``None`` and is not checked at all. A backend
    that reports no page count (the stand-ins, and the unchunked path on a
    non-PDF input) is not checked either. Absence is not disagreement in both
    directions: this is a check that catches a swapped file when both sides
    happen to know the number, not a guarantee that they do."""
    declared = _expected_page_count(manifest)
    actual = getattr(result, "page_count", None)
    if declared is not None and isinstance(actual, int) and actual != declared:
        raise PageCountMismatchError(
            f"the manifest declares {declared} page(s) for this document and the file on this "
            f"machine has {actual}. The pages this job would publish are not the pages the "
            "record is about; re-queue the document rather than folding these in."
        )
    payload = {
        "pages": [{"page_number": int(p.page_number), "text": p.text} for p in result.pages]
    }
    atomic_write_text(out_dir / OCR_OUTPUT_NAME, json.dumps(payload, ensure_ascii=False))


def _run_embed(
    backend: Any,
    manifest: dict[str, Any],
    in_dir: Path,
    out_dir: Path,
    *,
    batch_size: int,
    control: "WorkerControl | None" = None,
    progress: "ProgressState | None" = None,
    max_pause_s: float | None = None,
) -> StageOutcome:
    """Embed every chunk of one document.

    **The cooperative checkpoint is between batches** (C-0097 D2a), which is
    what makes a long document controllable at all: at ``--batch-size 4`` a
    four-thousand-chunk document offers a thousand places to pause, each of
    them a point where no vector is half-computed. A stop there leaves the
    chunks that WERE embedded in the work directory and hands the claim back
    (see :class:`StageOutcome`): the sandbox re-queues the job whole, which is
    the only correct thing while the result format is all-or-nothing per
    document, and the partial file is evidence rather than input.

    ``embeddable_text`` is applied at the model boundary and nowhere else
    (see its own docstring): a chunk whose text is nothing but control
    characters is embedded as the EMPTY STRING rather than dropped, because
    dropping it would change the vector count the sandbox verifies against
    ``expect.chunk_count`` and fail the whole document's job over one
    whitespace-only chunk. A decode failure now arrives as
    :class:`~trialerror.offload.stage.ChunkPayloadError` naming the line and
    the chunk, which ``_process_one`` publishes as this job's ``error.json``
    -- the sandbox then has the offending chunk's id in the record instead of
    a column number."""
    chunks = read_chunks_payload((in_dir / _input_name(manifest, EMBED_INPUT_NAME)).read_bytes())
    texts = [embeddable_text(str(c.get("text", ""))) for c in chunks]
    step = max(1, batch_size)
    if progress is not None:
        progress.start_job(
            str(manifest.get("job_id") or ""), kind="embed", unit="chunk", units_total=len(texts)
        )
    vectors: list[list[float]] = []
    stopped = False
    done = 0
    while done < len(texts):
        batch = backend.embed_batch(texts[done : done + step], kind="document")
        vectors.extend([list(v) for v in batch])
        done += len(texts[done : done + step])
        if progress is not None:
            progress.units_done(done)
        if _checkpoint(control, progress=progress, max_pause_s=max_pause_s) == "stop":
            stopped = True
            break
    complete = done >= len(texts)
    if complete and len(vectors) != len(texts):
        raise RuntimeError(
            f"embed backend returned {len(vectors)} vector(s) for {len(texts)} text(s)"
        )
    dims = len(vectors[0]) if vectors else int(getattr(backend, "dims", 0))
    body = "".join(json.dumps(v) + "\n" for v in vectors)
    atomic_write_text(out_dir / EMBED_OUTPUT_NAME, body)
    fields = {
        "model_key": getattr(backend, "model_key", None),
        "dims": int(dims),
        "chunk_ids": [c["chunk_id"] for c in chunks[: len(vectors)]],
    }
    return StageOutcome(
        fields=fields,
        stopped=stopped,
        complete=complete,
        units_done=done,
        units_total=len(texts),
        unit="chunk",
    )


def _write_result(out_dir: Path, manifest: dict[str, Any], *, worker_id: str, fields: dict[str, Any]) -> None:
    outputs = []
    for child in sorted(out_dir.iterdir()):
        if child.is_file() and child.name != protocol.RESULT_FILENAME:
            data = child.read_bytes()
            outputs.append(
                {"name": child.name, "sha256": protocol.sha256_bytes(data), "bytes": len(data)}
            )
    # SEC-2: no `config_hash` here. DEV used to copy the manifest's value
    # into the result and the sandbox used to compare the two, which is a
    # tautology dressed as a check. DEV states only what it actually
    # DETERMINED -- which backend ran, at what version, with what model key
    # and dimensionality -- and the sandbox decides everything it can decide
    # for itself from its own record.
    payload = {
        "schema": protocol.RESULT_SCHEMA,
        "job_id": manifest["job_id"],
        "stage": manifest["stage"],
        "worker_id": worker_id,
        "finished_ts": now(),
        "outputs": outputs,
        **fields,
    }
    protocol.write_json(out_dir / protocol.RESULT_FILENAME, payload)


def _write_error(out_dir: Path, job_id: str, stage: str | None, *, worker_id: str, error: str) -> None:
    protocol.write_json(
        out_dir / protocol.ERROR_FILENAME,
        {
            "schema": protocol.ERROR_SCHEMA,
            "job_id": job_id,
            "stage": stage,
            "worker_id": worker_id,
            "error": error,
            "ts": now(),
        },
    )


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
def _job_dirs(work_root: Path, job_id: str) -> tuple[Path, Path, Path]:
    base = work_root / job_id
    return base, base / "in", base / "out"


def _retry_publishes(transport: Transport, work_root: Path, *, log: Callable[[str], None]) -> list[str]:
    """Finish any result this worker already computed but could not hand
    over. Runs BEFORE any new claim, every poll -- a laptop that dropped
    its connection mid-publish resumes exactly where it stopped, and never
    re-runs the model."""
    published: list[str] = []
    if not work_root.is_dir():
        return published
    for base in sorted(work_root.iterdir()):
        if not base.is_dir():
            continue
        if base.name == OCR_RANGE_CACHE_DIRNAME:
            # Not a job directory: the page ranges stopped OCR jobs are
            # resumed from. Nothing here is ever published, and the sweep
            # below would delete exactly the work a resume exists to reuse.
            continue
        if (base / STOPPED_MARKER).exists():
            # FIX V-10: swept here, on the first sweep of a LATER run. The
            # operator's stop left this directory as a record of the GPU time
            # that was spent, and it outlives the run that wrote it so it can be
            # read; but the partial under it is never publishable (the claim went
            # back to `pending/`), so keeping it for ever would leak a work
            # directory per stop. Dropping it also makes "a stopped partial is
            # never published" true by construction rather than by a check.
            shutil.rmtree(base, ignore_errors=True)
            log(f"  - {base.name}: stopped partial swept (the claim is back in the queue)")
            continue
        out_dir = base / "out"
        if not (out_dir / protocol.RESULT_FILENAME).is_file() and not (out_dir / protocol.ERROR_FILENAME).is_file():
            continue
        if (base / PUBLISHED_MARKER).exists():
            continue
        job_id = base.name
        try:
            transport.push(job_id, protocol.pack_dir(out_dir))
            transport.publish(job_id)
        except TransportError as exc:
            if "already published" in str(exc):
                atomic_write_text(base / PUBLISHED_MARKER, now() + "\n")
                published.append(job_id)
                continue
            log(f"  ! {job_id}: could not hand over the finished result yet ({exc})")
            continue
        atomic_write_text(base / PUBLISHED_MARKER, now() + "\n")
        published.append(job_id)
        log(f"  = {job_id}: published (retried)")
    return published


def run_worker(
    *,
    transport: Transport,
    backends: DevBackends,
    work_root: Path | str | None = None,
    worker_id: str = "dev",
    stay: bool = False,
    poll_interval_s: float = 30.0,
    max_polls: int | None = None,
    max_jobs: int | None = None,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    heartbeat_interval_s: float = _HEARTBEAT_INTERVAL_S,
    keep_resident: bool = False,
    max_pause_s: float | None = None,
    log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Drain the sandbox's offload queue onto this machine's GPU.

    Returns a summary envelope body: ``{"claimed", "published", "failed",
    "lost", "stopped", "polls", "message", "control"}``. ``max_polls``/
    ``max_jobs`` bound the loop for tests and for a "do one batch and stop"
    launcher; the default (``stay=False``) is design section 4's own behaviour
    -- exit as soon as the queue is empty, telling the operator the laptop is
    free.

    **C-0097.** Three things are new and all three are cooperative: the worker
    reports what it is doing on every heartbeat (an idle beat under
    :func:`trialerror.offload.protocol.idle_job_id` when it holds no claim, so
    the dashboard can tell an idle worker from no worker); it checks the
    control word at the three checkpoints D2 names; and ``stop`` ends the run
    after the unit in flight, handing an incomplete claim back. Nothing here
    can be killed from the outside and nothing here kills anything (D8) --
    ``pause``/``stop`` are requests this loop chooses to honour, which is why
    a worker that ignores them is a DEFECT the doctor reports rather than a
    process someone shoots.

    ``keep_resident`` preserves the pre-C-0097 behaviour of holding both
    stages' backends for the whole run (D9). ``max_pause_s`` bounds how long a
    single pause may hold: ``None``, the default, is the design's own
    behaviour -- a pause lasts until an operator resumes or stops it -- and a
    number is for a launcher (or a test) that must not sit paused forever.
    """
    log = log or (lambda _msg: None)
    sleep = sleep or time.sleep
    work_root = Path(work_root) if work_root is not None else default_work_root()
    work_root.mkdir(parents=True, exist_ok=True)

    backends.validate()
    # FX-S1: ONE backend instance per run, for every job in it -- so the
    # embedding model is loaded once by this worker, not once per job and
    # certainly not once per batch. The finally below is the shutdown that
    # a resident driver process now requires.
    resident = ResidentBackends(backends, log=log, keep_resident=keep_resident)
    control = WorkerControl(log=log)
    progress = ProgressState(worker_id=worker_id, settings={"batch_size": int(batch_size)})
    progress.set_settings(resident_backends=resident.resident_kinds())

    summary: dict[str, Any] = {
        "claimed": [],
        "published": [],
        "failed": [],
        "lost": [],
        "stopped": [],
        "polls": 0,
        "message": IDLE_MESSAGE,
        "control": control.transitions,
    }
    jobs_done = 0

    def _stop_now(reason: str) -> dict[str, Any]:
        summary["message"] = STOPPED_MESSAGE
        log(f"  . {reason}")
        return summary

    def _pause_beat() -> str | None:
        """One beat under the idle id, WITHOUT resetting the progress state.

        FIX V-1: this is what a pause taken outside a job waits on. It must not
        call :meth:`ProgressState.idle` the way :func:`_idle_beat` does -- the
        state this wait is reporting is ``paused``, and overwriting it with
        ``idle`` would take the card's PAUSED chip away from the one place an
        operator looks to confirm the pause landed."""
        return _beat_once(
            transport, protocol.idle_job_id(worker_id), progress, control, log=log
        )

    def _between_jobs_checkpoint() -> str:
        """D2c, with the beat the wait needs (FIX V-1). No heartbeat thread is
        running here -- the job's own thread has exited and the next one has not
        started -- so this checkpoint carries its own."""
        return _checkpoint(
            control,
            progress=progress,
            max_pause_s=max_pause_s,
            beat=_pause_beat,
            beat_interval_s=heartbeat_interval_s,
        )

    try:
        while True:
            summary["polls"] += 1
            summary["published"].extend(_retry_publishes(transport, work_root, log=log))
            # The idle beat: D3's "state: idle" report, and the ONLY place the
            # between-jobs checkpoint can learn a control word from, since no
            # per-job heartbeat thread is running between jobs.
            _idle_beat(transport, worker_id, progress, control, log=log)
            if control.stopping:
                return _stop_now("stop honoured with no claim held")
            if _between_jobs_checkpoint() == "stop":
                return _stop_now("stop honoured while paused, no claim held")

            try:
                available = transport.list_jobs()
            except TransportError as exc:
                log(f"! could not reach the offload queue: {exc}")
                summary["message"] = f"transport error: {exc}"
                return summary

            if not available:
                log(f"- queue empty (poll {summary['polls']})")
                if not stay:
                    summary["message"] = IDLE_MESSAGE
                    return summary
                if max_polls is not None and summary["polls"] >= max_polls:
                    summary["message"] = f"stopped after {summary['polls']} poll(s)"
                    return summary
                sleep(poll_interval_s)
                continue

            for job_id in available:
                if max_jobs is not None and jobs_done >= max_jobs:
                    break
                outcome = _process_one(
                    transport,
                    resident,
                    work_root,
                    job_id,
                    worker_id=worker_id,
                    batch_size=batch_size,
                    heartbeat_interval_s=heartbeat_interval_s,
                    control=control,
                    progress=progress,
                    max_pause_s=max_pause_s,
                    log=log,
                )
                summary[outcome[0]].append(job_id)
                if outcome[0] not in ("lost", "stopped"):
                    jobs_done += 1
                # D2c: between jobs. A stop seen during the job that just
                # finished ends the run here rather than claiming another.
                if control.stopping:
                    return _stop_now("stop honoured between jobs")
                if _between_jobs_checkpoint() == "stop":
                    return _stop_now("stop honoured between jobs (while paused)")

            if max_jobs is not None and jobs_done >= max_jobs:
                summary["message"] = f"stopped after {jobs_done} job(s)"
                return summary
            if max_polls is not None and summary["polls"] >= max_polls:
                summary["message"] = f"stopped after {summary['polls']} poll(s)"
                return summary
    finally:
        # FIX V-9: a terminal state, not another `idle`. The row stays on the
        # card until a publish/return/reclaim clears it or it ages out, so what
        # it SAYS is the only thing that can tell "this worker is between jobs"
        # from "this worker's process has ended".
        progress.exited()
        _beat_once(transport, protocol.idle_job_id(worker_id), progress, control, log=log)
        resident.close()


def _beat_once(
    transport: Transport,
    job_id: str,
    progress: "ProgressState",
    control: "WorkerControl",
    *,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """One inline beat: the payload up, the control word down, no thread.

    Split out of :func:`_idle_beat` for FIX V-1: a pause taken outside a job
    has to keep beating while it waits, and it has to do that WITHOUT
    resetting the state it is reporting."""
    return _Heartbeat(
        transport, job_id, progress=progress, control=control, log=log
    ).beat()


def _idle_beat(
    transport: Transport,
    worker_id: str,
    progress: "ProgressState",
    control: "WorkerControl",
    *,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """One heartbeat under the synthetic idle id (C-0097 D3).

    This is how "the worker is alive and has nothing claimed" reaches the
    dashboard at all -- without it, an idle worker and a switched-off laptop
    are the same empty claim directory. It is also the between-jobs
    checkpoint's only source of a control word, which is why it runs before
    the poll rather than only after one."""
    progress.idle()
    return _beat_once(transport, protocol.idle_job_id(worker_id), progress, control, log=log)


def _process_one(
    transport: Transport,
    backends: "ResidentBackends",
    work_root: Path,
    job_id: str,
    *,
    worker_id: str,
    batch_size: int,
    heartbeat_interval_s: float,
    control: "WorkerControl | None" = None,
    progress: "ProgressState | None" = None,
    max_pause_s: float | None = None,
    log: Callable[[str], None],
) -> tuple[str, str]:
    """Claim and run exactly one job. Returns ``(bucket, detail)`` where
    bucket is ``claimed``/``published``/``failed``/``lost``/``stopped``."""
    if progress is not None:
        progress.claiming(job_id)
    try:
        manifest = transport.claim(job_id)
    except TransportError as exc:
        log(f"  ~ {job_id}: not ours ({exc})")
        return "lost", str(exc)

    base, in_dir, out_dir = _job_dirs(work_root, job_id)
    shutil.rmtree(base, ignore_errors=True)
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = base / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    stage = manifest.get("stage")
    log(f"  > {job_id}: claimed ({stage})")

    started = time.monotonic()
    outcome: StageOutcome | None = None
    try:
        with _Heartbeat(
            transport, job_id, heartbeat_interval_s, progress=progress, control=control
        ):
            data = transport.pull(job_id)
            protocol.unpack_into(data, in_dir)
            _verify_inputs(manifest, in_dir)
            if stage == "ocr":
                backend = backends.for_stage("ocr")
                if progress is not None:
                    progress.set_settings(
                        backend=str(getattr(backend, "name", type(backend).__name__)),
                        resident_backends=backends.resident_kinds(),
                    )
                outcome = _run_ocr(
                    backend,
                    manifest,
                    in_dir,
                    out_dir,
                    scratch,
                    control=control,
                    progress=progress,
                    max_pause_s=max_pause_s,
                    range_cache=_range_cache_dir(work_root, job_id),
                    log=log,
                )
            elif stage == "embed":
                backend = backends.for_stage("embed")
                if progress is not None:
                    progress.set_settings(
                        batch_size=int(batch_size),
                        model_key=getattr(backend, "model_key", None),
                        resident_backends=backends.resident_kinds(),
                    )
                outcome = _run_embed(
                    backend,
                    manifest,
                    in_dir,
                    out_dir,
                    batch_size=batch_size,
                    control=control,
                    progress=progress,
                    max_pause_s=max_pause_s,
                )
            else:
                raise RuntimeError(f"unknown offload stage {stage!r}")
            # FX-S1: per-job timing in the log. With one model load per RUN
            # rather than per batch, "how long did this document take" is
            # finally a number about the GPU rather than about process
            # startup -- and it is the number that tells an operator
            # whether --batch-size is doing anything.
            log(f"  . {job_id}: {stage} ran in {time.monotonic() - started:.1f}s")
            fields = dict(outcome.fields)
            if outcome.stopped and not outcome.complete:
                # C-0097 D2: the partial result, with what was done, in the work
                # directory only. FIX V-10: the marker goes down FIRST. It is
                # what keeps `_retry_publishes` off this directory, so a crash
                # between the two writes used to leave a publishable partial
                # with nothing to stop the next poll pushing it at a job the
                # queue had already given to somebody else.
                atomic_write_text(base / STOPPED_MARKER, now() + "\n")
                fields.update(
                    {
                        "status": "stopped",
                        "units_done": outcome.units_done,
                        "units_total": outcome.units_total,
                        "unit": outcome.unit,
                    }
                )
            _write_result(out_dir, manifest, worker_id=worker_id, fields=fields)
    except KeyboardInterrupt:
        log(f"  ^ {job_id}: interrupted -- returning the claim")
        try:
            transport.return_job(job_id)
        except TransportError:  # pragma: no cover - best effort on the way out
            pass
        raise
    except TransportError as exc:
        # Nothing ran (pull failed) or the wire dropped: hand it straight
        # back rather than sitting on a claim we cannot use.
        log(f"  ! {job_id}: transport failure before any GPU work ({exc})")
        try:
            transport.return_job(job_id)
        except TransportError:
            pass
        return "lost", str(exc)
    except Exception as exc:  # noqa: BLE001 - deliberate: a stage failure travels back as data
        log(f"  x {job_id}: {type(exc).__name__}: {exc}")
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_error(out_dir, job_id, stage, worker_id=worker_id, error=f"{type(exc).__name__}: {exc}")
        if progress is not None:
            progress.set_last_error(f"{type(exc).__name__}: {exc}")
        _hand_over(transport, base, out_dir, job_id, log=log)
        return "failed", str(exc)

    if outcome is not None and outcome.stopped and not outcome.complete:
        # C-0097 D2, the incomplete-stop path: the claim goes back through the
        # EXISTING `return` verb -- no new verb, and the job is pending again
        # for whichever worker gets it next, from the start. The partial result
        # stays here as a record of the GPU time that was spent; the marker
        # (written with it, above) is what keeps the next poll from trying to
        # publish it, and the next RUN's first sweep is what removes both.
        if progress is not None:
            progress.set_state("stopping")
            if control is not None:
                # FIX V-9: one last beat for THIS job, before the claim goes
                # back. On the path where the return cannot be delivered, that
                # file is the row the card keeps -- and `running` there would
                # describe a claim this process has already let go of.
                _beat_once(transport, job_id, progress, control, log=log)
        try:
            transport.return_job(job_id)
        except TransportError as exc:
            # The claim will come back on its own: the sandbox's 60-minute
            # reclaim exists for exactly the case where this worker could not
            # hand it over (a closed lid, a dropped connection).
            log(f"  ! {job_id}: stopped, but the claim could not be returned yet ({exc})")
            return "stopped", str(exc)
        log(
            f"  ^ {job_id}: stopped after {outcome.units_done}/{outcome.units_total} "
            f"{outcome.unit}(s) - claim returned to the queue"
        )
        return "stopped", ""

    _hand_over(transport, base, out_dir, job_id, log=log)
    return ("published" if (base / PUBLISHED_MARKER).exists() else "claimed"), ""


def _hand_over(transport: Transport, base: Path, out_dir: Path, job_id: str, *, log: Callable[[str], None]) -> None:
    """push + publish, marking the local copy handed over on success. A
    failure here is deliberately NOT an error: the outputs stay on disk and
    :func:`_retry_publishes` picks them up on the next poll."""
    try:
        transport.push(job_id, protocol.pack_dir(out_dir))
        transport.publish(job_id)
    except TransportError as exc:
        if "already published" in str(exc):
            atomic_write_text(base / PUBLISHED_MARKER, now() + "\n")
            return
        log(f"  ! {job_id}: result computed but not handed over yet ({exc}) -- will retry")
        return
    atomic_write_text(base / PUBLISHED_MARKER, now() + "\n")
    log(f"  = {job_id}: published")
