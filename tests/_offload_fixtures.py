"""Not a test module (pytest only collects ``test_*.py``) -- shared builders
for the lane L0-C offload suite.

Two things live here:

- ``Stub*Backend`` -- deterministic stand-ins for marker/Qwen3 that are
  NOT the ``Fake*`` classes. That distinction is load-bearing: the DEV
  worker refuses to run through ``FakeOcrBackend``/``FakeEmbedBackend``
  (design D13), so a test that needs to exercise the whole round trip has
  to supply a backend that is neither real (no GPU in CI) nor fake (would
  be refused). These classes are exactly that seam, in the same spirit as
  ``FakeEmbedBackend``'s own documented ``delay_s`` test hook.
- ``offload_program`` -- a program root whose ``trialerror.toml`` routes
  both GPU stages through the offload queue, i.e. the SANDBOX half of the
  two-machine split.

No SSH anywhere: every test drives
:class:`trialerror.offload.transport.LocalTransport`, which routes through
the same verb/id gate the real wrapper applies (design section 4's C-unit
row: "an in-process fake transport (no SSH in tests)").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from trialerror.ingest.backends import OcrPage, OcrResult

__all__ = [
    "STUB_OCR_NAME",
    "STUB_OCR_VERSION",
    "STUB_MODEL_KEY",
    "STUB_DIMS",
    "StubOcrBackend",
    "StubEmbedBackend",
    "StubDevBackends",
    "ClosableStubEmbedBackend",
    "ControlTransport",
    "write_offload_toml",
    "queue_one",
    "queue_chunks",
    "publish_stub_result",
]

STUB_OCR_NAME = "stubmarker"
STUB_OCR_VERSION = "9.9"
STUB_MODEL_KEY = "stub-embed"
STUB_DIMS = 8


class StubOcrBackend:
    """Form-feed page splitter, same input shape as the pdf-scan fixture.

    ``fail_on`` makes it raise for a given input text, which is how the
    "deterministic GPU failure" path (design acceptance C-fail) is driven
    without a GPU."""

    name = STUB_OCR_NAME
    version = STUB_OCR_VERSION

    def __init__(self, *, fail_on: str | None = None, before_run: Any = None):
        self.fail_on = fail_on
        self.calls = 0
        #: The same determinism seam ``StubEmbedBackend.before_batch`` is, for
        #: the stage whose unit is the whole job (C-0097 D6).
        self.before_run = before_run

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult:
        if self.before_run is not None:
            self.before_run(self.calls)
        self.calls += 1
        raw = input_path.read_text(encoding="utf-8", errors="replace")
        if self.fail_on is not None and self.fail_on in raw:
            raise RuntimeError("stub marker: this document is corrupt")
        work_dir.mkdir(parents=True, exist_ok=True)
        pages = [
            OcrPage(page_number=i + 1, text=t.strip())
            for i, t in enumerate(raw.split("\x0c"))
            if t.strip()
        ]
        return OcrResult(pages=pages, ocr_backend=self.name, ocr_version=self.version)


class StubEmbedBackend:
    """Deterministic ``STUB_DIMS``-dimensional vectors, derived from the
    text so a wrong-order result is detectable.

    ``before_batch`` is the seam the C-0097 control tests need and the reason
    they contain no sleeps: a worker's cooperative checkpoint can only act on a
    control word the heartbeat thread has already seen, so a test that wants a
    pause to land at a KNOWN batch boundary has to be able to block the model
    until the word is in. The hook is called with the batch index before each
    call, exactly like ``FakeEmbedBackend``'s own documented ``delay_s`` test
    hook -- a seam for determinism, not a behaviour."""

    def __init__(
        self,
        *,
        model_key: str = STUB_MODEL_KEY,
        dims: int = STUB_DIMS,
        before_batch: Any = None,
    ):
        self.model_key = model_key
        self.dims = dims
        self.batches: list[int] = []
        self.texts_seen: list[str] = []
        self.before_batch = before_batch

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        if self.before_batch is not None:
            self.before_batch(len(self.batches))
        self.batches.append(len(texts))
        self.texts_seen.extend(texts)
        out = []
        for t in texts:
            seed = float(sum(ord(c) for c in t) % 97) / 97.0
            out.append([seed + i / 100.0 for i in range(self.dims)])
        return out


class ClosableStubEmbedBackend(StubEmbedBackend):
    """A stub with the ``close()`` a resident driver has, so D9's kind-switch
    unload can be OBSERVED rather than assumed: the real thing's ``close``
    exits a subprocess and frees VRAM, which a test cannot see, so the thing
    worth proving is that the policy calls it at the right moment."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class StubDevBackends:
    """A :class:`trialerror.offload.worker.DevBackends` that hands back the
    stubs above and validates cleanly."""

    def __init__(self, ocr: Any = None, embed: Any = None):
        self._ocr = ocr if ocr is not None else StubOcrBackend()
        self._embed = embed if embed is not None else StubEmbedBackend()
        self.validated = 0

    def validate(self) -> None:
        self.validated += 1

    def ocr(self) -> Any:
        return self._ocr

    def embed(self) -> Any:
        return self._embed


class ControlTransport:
    """A control-aware fake shell (C-0097 acceptance A): every verb of
    :class:`trialerror.offload.transport.LocalTransport`, plus a scripted
    ``heartbeat`` reply.

    Why a wrapper rather than writing ``CONTROL.json`` and letting
    ``LocalTransport`` read it: the control word's TIMING is the thing under
    test. A test that wants "paused at the third batch, resumed once the worker
    has reported ``paused`` once" has to decide the word per beat, and a file
    on disk can only say what the word is, not when it changes. ``word_for``
    receives ``(beat_number, job_id, payload_dict_or_None)`` and returns one of
    ``none``/``pause``/``resume``/``stop``.

    Everything else delegates, so the verb/id gate, the claim semantics and the
    payload refusals are still the real ones -- including the progress file the
    real wrapper would write, which is what the dashboard tests then read.
    """

    def __init__(self, root: Any, *, worker_id: str = "dev", word_for: Any = None):
        import threading

        from trialerror.offload.transport import LocalTransport

        self.inner = LocalTransport(root, worker_id=worker_id)
        self.root = self.inner.root
        self.worker_id = worker_id
        self.word_for = word_for
        #: ``(job_id, payload)`` per beat, in order -- the whole observability
        #: surface a test needs to assert "kept heartbeating while paused".
        self.beats: list[tuple[str, dict | None]] = []
        self._cond = threading.Condition()

    # -- the six verbs that are unchanged ---------------------------------
    def list_jobs(self) -> list[str]:
        return self.inner.list_jobs()

    def claim(self, job_id: str) -> dict:
        return self.inner.claim(job_id)

    def pull(self, job_id: str) -> bytes:
        return self.inner.pull(job_id)

    def push(self, job_id: str, data: bytes) -> None:
        self.inner.push(job_id, data)

    def publish(self, job_id: str) -> None:
        self.inner.publish(job_id)

    def return_job(self, job_id: str) -> None:
        self.inner.return_job(job_id)

    # -- the one that carries the control channel -------------------------
    def heartbeat(self, job_id: str, *, progress: bytes | None = None) -> str:
        decoded = json.loads(progress.decode("utf-8")) if progress else None
        self.inner.heartbeat(job_id, progress=progress)
        with self._cond:
            self.beats.append((job_id, decoded))
            self._cond.notify_all()
        if self.word_for is None:
            return "none"
        return self.word_for(len(self.beats), job_id, decoded)

    # -- assertions a test would otherwise re-derive ----------------------
    def states(self) -> list[str]:
        return [(p or {}).get("state") for _job, p in self.beats]

    def beats_for(self, job_id: str) -> list[dict | None]:
        return [p for jid, p in self.beats if jid == job_id]

    def wait_for_beats(self, job_id: str, count: int, timeout: float = 5.0) -> bool:
        """Block until ``count`` beats for ``job_id`` have been recorded.

        This is the happens-before a control test needs, and it is why this
        module contains no sleeps. The worker's heartbeat thread runs
        ``beat -> observe -> wait -> beat``, strictly in order, so by the time
        the Nth beat is RECORDED here the (N-1)th word has certainly been
        applied to the worker's control flag. A test that waits for two beats
        and then lets the model run is therefore guaranteed to hit its next
        checkpoint with the first word in hand -- no interval to tune, nothing
        that gets flakier on a slower machine."""
        deadline_cond = lambda: len(self.beats_for(job_id)) >= count  # noqa: E731
        with self._cond:
            return self._cond.wait_for(deadline_cond, timeout=timeout)


def queue_chunks(root: Path, job_id: str = "JOB-embed-1", *, count: int = 12) -> dict:
    """One pending EMBED marker carrying ``count`` distinct chunks.

    Distinct texts on purpose: the resume acceptance ("no unit repeated, none
    skipped") is only checkable if every unit is identifiable, and
    :class:`StubEmbedBackend` records every text it was handed."""
    from trialerror.offload import protocol

    rows = [{"chunk_id": f"CHK-{i:03d}", "text": f"chunk body {i}"} for i in range(count)]
    payload = "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")
    return protocol.queue_marker(
        root,
        job_id=job_id,
        stage="embed",
        doc_id="DOC-test",
        expect={
            "stage": "embed",
            "model_key": STUB_MODEL_KEY,
            "dims": STUB_DIMS,
            "chunk_count": count,
            "outputs": ["vectors.jsonl"],
            "input_name": "chunks.jsonl",
        },
        config_hash="cfg-hash",
        inputs=[("chunks.jsonl", payload)],
    )


def write_offload_toml(
    program_root: Path,
    *,
    program_id: str = "offload-test",
    require_real_backends: bool = True,
    expect_backend: str | None = STUB_OCR_NAME,
    model_key: str = STUB_MODEL_KEY,
    dims: int = STUB_DIMS,
) -> Path:
    """The SANDBOX half of design section 4's "config split": both GPU
    stages routed to the queue, and the program declaring that it will not
    accept fake backends."""
    lines = [
        "[program]",
        f'id = "{program_id}"',
        "",
        "[ingest]",
        f"require_real_backends = {'true' if require_real_backends else 'false'}",
        "",
        "[ingest.ocr]",
        'backend = "offload"',
    ]
    if expect_backend:
        lines.append(f'expect_backend = "{expect_backend}"')
    lines += [
        "",
        "[ingest.embed]",
        'backend = "offload"',
        f'model_key = "{model_key}"',
        f"dims = {dims}",
        "",
    ]
    path = program_root / "trialerror.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def queue_one(root: Path, job_id: str = "JOB-test-1", *, stage: str = "ocr", payload: bytes = b"hello\n") -> dict:
    """One minimal pending marker, for protocol-level tests that do not
    need a whole ingest pipeline behind them."""
    from trialerror.offload import protocol

    return protocol.queue_marker(
        root,
        job_id=job_id,
        stage=stage,
        doc_id="DOC-test",
        expect={"stage": stage, "backend": STUB_OCR_NAME, "outputs": ["pages.json"], "input_name": "input.txt"},
        config_hash="cfg-hash",
        inputs=[("input.txt", payload)],
    )


def publish_stub_result(
    root: Path,
    job_id: str,
    *,
    worker_id: str = "dev",
    manifest: dict | None = None,
    outputs: dict[str, bytes] | None = None,
    result_overrides: dict | None = None,
) -> None:
    """Claim + push + publish a hand-built result, for tests that want a
    published state without running the worker loop."""
    from trialerror.offload import protocol

    manifest = manifest or protocol.server_claim(root, job_id, worker_id=worker_id)
    outputs = outputs if outputs is not None else {"pages.json": json.dumps({"pages": [{"page_number": 1, "text": "hi"}]}).encode()}
    entries = [
        {"name": name, "sha256": protocol.sha256_bytes(data), "bytes": len(data)}
        for name, data in outputs.items()
    ]
    result = {
        "schema": protocol.RESULT_SCHEMA,
        "job_id": job_id,
        "stage": manifest["stage"],
        "worker_id": worker_id,
        "finished_ts": "2026-09-05T00:00:00.000Z",
        "outputs": entries,
        "backend": STUB_OCR_NAME,
        "version": STUB_OCR_VERSION,
    }
    result.update(result_overrides or {})
    files = {**outputs, protocol.RESULT_FILENAME: json.dumps(result).encode()}
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    protocol.server_push(root, job_id, buf.getvalue(), worker_id=worker_id)
    protocol.server_publish(root, job_id, worker_id=worker_id)
