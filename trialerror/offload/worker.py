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
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

from trialerror.offload import protocol
from trialerror.offload.lock import worker_state_dir
from trialerror.offload.marker import OffloadMarker
from trialerror.offload.stage import (
    EMBED_INPUT_NAME,
    EMBED_OUTPUT_NAME,
    OCR_INPUT_STEM,
    OCR_OUTPUT_NAME,
    read_chunks_payload,
)
from trialerror.offload.transport import Transport, TransportError
from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now

__all__ = [
    "WorkerConfigError",
    "DevBackends",
    "ConfigDevBackends",
    "PUBLISHED_MARKER",
    "IDLE_MESSAGE",
    "default_work_root",
    "run_worker",
]

#: Written beside a finished job's outputs once the sandbox has accepted
#: them, so a restarted worker does not try to publish the same result
#: forever.
PUBLISHED_MARKER = "PUBLISHED"

#: design section 4: "Idle -> exit with 'Queue empty -- safe to switch DEV
#: off' unless ``--stay``".
IDLE_MESSAGE = "Queue empty - safe to switch DEV off"

_HEARTBEAT_INTERVAL_S = 300.0


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


class ConfigDevBackends:
    """Resolve the real backends from a DEV program's ``trialerror.toml``.

    ``[ingest.ocr]``/``[ingest.embed]`` on DEV name the actual local
    installs (``marker_single_exe``, ``python_exe``/``module_dir``) -- the
    "two-machine split" of design section 4: the SANDBOX toml says
    ``backend = "offload"``, the DEV toml says ``backend = "marker"`` /
    ``"qwen3-4b"``.
    """

    def __init__(self, config: dict[str, Any] | None):
        self.config = dict(config or {})

    def _table(self, stage: str) -> dict[str, Any]:
        return (self.config.get("ingest") or {}).get(stage) or {}

    def validate(self) -> None:
        """Refuse a DEV configuration that would produce fake or offloaded
        results. Both stages are checked up front, before a single job is
        claimed, so the operator learns about a misconfiguration in the
        first second of the launcher rather than after a 40-minute claim."""
        for stage in ("ocr", "embed"):
            table = self._table(stage)
            backend_name = table.get("backend", "fake")
            if backend_name in ("fake", "offload"):
                raise WorkerConfigError(
                    f"DEV worker refuses to run: [ingest.{stage}] backend = {backend_name!r} in this "
                    "program root. The DEV program's trialerror.toml must name the REAL local "
                    "backends (marker / qwen3-4b); 'offload' belongs in the SANDBOX toml and 'fake' "
                    "must never write into the record (design D13)."
                )
        # Constructing them also surfaces a missing marker_single_exe /
        # python_exe / module_dir now rather than mid-job.
        self.ocr()
        self.embed()

    def ocr(self) -> Any:
        from trialerror.ingest.backends import load_ocr_backend

        return _refuse_unreal(load_ocr_backend(self._table("ocr")), "ocr")

    def embed(self) -> Any:
        from trialerror.ingest.backends import load_embed_backend

        return _refuse_unreal(load_embed_backend(self._table("embed")), "embed")


def _refuse_unreal(backend: Any, stage: str) -> Any:
    from trialerror.ingest.backends import FakeEmbedBackend, FakeOcrBackend

    if isinstance(backend, (FakeOcrBackend, FakeEmbedBackend, OffloadMarker)):
        raise WorkerConfigError(
            f"DEV worker refuses to run the {stage} stage through {type(backend).__name__} -- "
            "only a real local backend may publish results into the record (design D13)"
        )
    return backend


# ---------------------------------------------------------------------------
# heartbeats while a model runs
# ---------------------------------------------------------------------------
class _Heartbeat:
    """Beat ``transport.heartbeat(job_id)`` on a daemon thread until the
    context exits. A dropped beat is swallowed: a transient SSH failure
    must not kill a running marker job, and the 60-minute expiry is
    forgiving enough to survive several misses."""

    def __init__(self, transport: Transport, job_id: str, interval_s: float = _HEARTBEAT_INTERVAL_S):
        self.transport = transport
        self.job_id = job_id
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_Heartbeat":
        def loop() -> None:
            while not self._stop.wait(self.interval_s):
                try:
                    self.transport.heartbeat(self.job_id)
                except Exception:  # noqa: BLE001 - a missed beat is not a job failure
                    pass

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


def _run_ocr(backend: Any, manifest: dict[str, Any], in_dir: Path, out_dir: Path, scratch: Path) -> dict[str, Any]:
    input_path = in_dir / _input_name(manifest, OCR_INPUT_STEM)
    result = backend.run(input_path=input_path, work_dir=scratch)
    payload = {
        "pages": [{"page_number": int(p.page_number), "text": p.text} for p in result.pages]
    }
    atomic_write_text(out_dir / OCR_OUTPUT_NAME, json.dumps(payload, ensure_ascii=False))
    return {"backend": result.ocr_backend, "version": result.ocr_version}


def _run_embed(
    backend: Any, manifest: dict[str, Any], in_dir: Path, out_dir: Path, *, batch_size: int
) -> dict[str, Any]:
    chunks = read_chunks_payload((in_dir / _input_name(manifest, EMBED_INPUT_NAME)).read_bytes())
    texts = [c["text"] for c in chunks]
    vectors: list[list[float]] = []
    for i in range(0, len(texts), max(1, batch_size)):
        batch = backend.embed_batch(texts[i : i + max(1, batch_size)], kind="document")
        vectors.extend([list(v) for v in batch])
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"embed backend returned {len(vectors)} vector(s) for {len(texts)} text(s)"
        )
    dims = len(vectors[0]) if vectors else int(getattr(backend, "dims", 0))
    body = "".join(json.dumps(v) + "\n" for v in vectors)
    atomic_write_text(out_dir / EMBED_OUTPUT_NAME, body)
    return {
        "model_key": getattr(backend, "model_key", None),
        "dims": int(dims),
        "chunk_ids": [c["chunk_id"] for c in chunks],
    }


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
    batch_size: int = 8,
    heartbeat_interval_s: float = _HEARTBEAT_INTERVAL_S,
    log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Drain the sandbox's offload queue onto this machine's GPU.

    Returns a summary envelope body: ``{"claimed", "published", "failed",
    "lost", "polls", "message"}``. ``max_polls``/``max_jobs`` bound the loop
    for tests and for a "do one batch and stop" launcher; the default
    (``stay=False``) is design section 4's own behaviour -- exit as soon as
    the queue is empty, telling the operator the laptop is free.
    """
    import time

    log = log or (lambda _msg: None)
    sleep = sleep or time.sleep
    work_root = Path(work_root) if work_root is not None else default_work_root()
    work_root.mkdir(parents=True, exist_ok=True)

    backends.validate()

    summary: dict[str, Any] = {
        "claimed": [],
        "published": [],
        "failed": [],
        "lost": [],
        "polls": 0,
        "message": IDLE_MESSAGE,
    }
    jobs_done = 0

    while True:
        summary["polls"] += 1
        summary["published"].extend(_retry_publishes(transport, work_root, log=log))
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
                backends,
                work_root,
                job_id,
                worker_id=worker_id,
                batch_size=batch_size,
                heartbeat_interval_s=heartbeat_interval_s,
                log=log,
            )
            summary[outcome[0]].append(job_id)
            if outcome[0] != "lost":
                jobs_done += 1

        if max_jobs is not None and jobs_done >= max_jobs:
            summary["message"] = f"stopped after {jobs_done} job(s)"
            return summary
        if max_polls is not None and summary["polls"] >= max_polls:
            summary["message"] = f"stopped after {summary['polls']} poll(s)"
            return summary


def _process_one(
    transport: Transport,
    backends: DevBackends,
    work_root: Path,
    job_id: str,
    *,
    worker_id: str,
    batch_size: int,
    heartbeat_interval_s: float,
    log: Callable[[str], None],
) -> tuple[str, str]:
    """Claim and run exactly one job. Returns ``(bucket, detail)`` where
    bucket is ``claimed``/``published``/``failed``/``lost``."""
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

    try:
        with _Heartbeat(transport, job_id, heartbeat_interval_s):
            data = transport.pull(job_id)
            protocol.unpack_into(data, in_dir)
            _verify_inputs(manifest, in_dir)
            if stage == "ocr":
                fields = _run_ocr(backends.ocr(), manifest, in_dir, out_dir, scratch)
            elif stage == "embed":
                fields = _run_embed(backends.embed(), manifest, in_dir, out_dir, batch_size=batch_size)
            else:
                raise RuntimeError(f"unknown offload stage {stage!r}")
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
        _hand_over(transport, base, out_dir, job_id, log=log)
        return "failed", str(exc)

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
