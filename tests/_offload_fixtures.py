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
    "write_offload_toml",
    "queue_one",
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

    def __init__(self, *, fail_on: str | None = None):
        self.fail_on = fail_on
        self.calls = 0

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult:
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
    text so a wrong-order result is detectable."""

    def __init__(self, *, model_key: str = STUB_MODEL_KEY, dims: int = STUB_DIMS):
        self.model_key = model_key
        self.dims = dims
        self.batches: list[int] = []

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        self.batches.append(len(texts))
        out = []
        for t in texts:
            seed = float(sum(ord(c) for c in t) % 97) / 97.0
            out.append([seed + i / 100.0 for i in range(self.dims)])
        return out


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
