"""Pluggable OCR/embed backends. Build brief: "REAL LOCAL MODELS: marker
OCR + Qwen3-Embedding-4B live in the origin-project repo ... implement the OCR and
embed handlers as PLUGGABLE BACKENDS: a real backend that shells out to
those proven origin-project tools (config-pathed in trialerror.toml, NOT hardcoded), plus a
deterministic fake backend for tests (tests must NOT need the GPU)."

Two small interfaces (:class:`OcrBackend`, :class:`EmbedBackend`), each with
exactly two implementations:

- ``Fake*`` -- deterministic, zero-dependency, used by default in every
  test and by ``trialerror.toml``'s own default config (``backend = "fake"``) so
  a fresh program scaffold works out of the box without a GPU.
- ``Real*`` -- shells out to the proven origin-project tools via ``subprocess``, paths
  taken from ``trialerror.toml``'s ``[ingest.ocr]``/``[ingest.embed]`` tables
  (never hardcoded). Exercised only by GPU-gated tests
  (``@pytest.mark.skipif`` on the configured executable's existence --
  design Section 13 flag F18/M15's "state the hardware assumption").

``load_ocr_backend(config)``/``load_embed_backend(config)`` are the one
factory pair callers (the ``ocr``/``embed`` job handlers) use -- neither
constructs a concrete backend class directly, so a third backend (a
different OCR/embedding model down the line) is a config value away.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

# TRIALERROR-DEV-NOTE (FX-1, IMPL_REVIEW_VERDICT.md Tier 1 / IMPL_REVIEW_C_ops.md
# N-3): trialerror.jobs.worker is the sole source of EnvironmentalFailure (the
# handler-facing "this failure is transient/environmental, don't consume a
# retry attempt" escape hatch) -- imported here, not re-derived, so a timed-
# out real backend re-queues through the exact same ledger path a GPU-busy
# handler would use. Non-circular: trialerror.jobs.worker imports only
# trialerror.jobs.{ledger,errors,registry}/trialerror.stores.store/trialerror.util.*, never
# trialerror.ingest.
from trialerror.jobs.worker import EnvironmentalFailure

__all__ = [
    "OcrPage",
    "OcrResult",
    "OcrBackend",
    "FakeOcrBackend",
    "RealMarkerOcrBackend",
    "load_ocr_backend",
    "DEFAULT_OCR_TIMEOUT_S",
    "EmbedBackend",
    "FakeEmbedBackend",
    "RealQwenEmbedBackend",
    "load_embed_backend",
    "DEFAULT_FAKE_EMBED_DIMS",
    "DEFAULT_EMBED_TIMEOUT_S",
]


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class OcrResult:
    pages: list[OcrPage]
    ocr_backend: str
    ocr_version: str


class OcrBackend(Protocol):
    name: str
    version: str

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult: ...


class FakeOcrBackend:
    """Deterministic OCR stand-in for tests and GPU-less machines. Treats
    ``input_path``'s own bytes as already-recognized text (no actual image
    decoding -- test fixtures for the "scanned pdf"/"image" route are plain
    UTF-8 text files carrying that route's extension), splitting into pages
    on a literal ``\\x0c`` (form-feed, the conventional page-break byte)
    or, absent one, treating the whole file as a single page. This keeps
    the OCR-route acceptance path (normalize -> OCR job -> elements ->
    chunk -> embed) exercisable with zero GPU/model dependency, per the
    build brief's "tests must NOT need the GPU.\""""

    name = "fake"
    version = "1"

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult:
        raw = input_path.read_text(encoding="utf-8", errors="replace")
        page_texts = raw.split("\x0c") if "\x0c" in raw else [raw]
        pages = [OcrPage(page_number=i + 1, text=t.strip()) for i, t in enumerate(page_texts) if t.strip()]
        return OcrResult(pages=pages, ocr_backend=self.name, ocr_version=self.version)


#: Default subprocess timeout (seconds) for :class:`RealMarkerOcrBackend`
#: when ``trialerror.toml``'s ``[ingest.ocr]`` table doesn't set its own
#: ``timeout_s`` -- FX-1 (IMPL_REVIEW_VERDICT.md Tier 1 / IMPL_REVIEW_C_ops.md
#: N-3): before this fix ``subprocess.run`` had NO timeout at all, so a
#: wedged marker/torch child blocked the handler forever -- the job's lease
#: (``trialerror.jobs.ledger.LEASE_DURATION_S``, 900s default) would eventually
#: expire and get reclaimed by ANOTHER worker while the zombie subprocess
#: still held the GPU (double execution, the exact failure mode
#: ``EnvironmentalFailure`` exists to manage).
#:
#: TRIALERROR-DEV-NOTE (deviation from the review's literal "~1800s" suggestion,
#: disclosed per the fix brief): 1800s is kept as the OUT-OF-THE-BOX
#: default because a full-book marker GPU run can legitimately take longer
#: than the 900s lease -- but ``subprocess.run`` is one blocking call with
#: no heartbeat granularity inside it, so this default alone does NOT
#: guarantee the double-execution window closes; it only guarantees a
#: truly-wedged process no longer hangs FOREVER (it now dies, frees the
#: GPU, and settles the job as retryable). Deployments running the real
#: backend should pair ``ingest.ocr.timeout_s``/``ingest.embed.timeout_s``
#: with a ``--lease-s`` at least that large (``trialerror/cli/jobs.py``'s
#: ``--lease-s`` flag) so THIS worker's own timeout fires before the
#: ledger's lease-expiry reclaim would. Config-read first (the seam
#: already exists -- ``config`` here is the program's own ``trialerror.toml``
#: table, read generically), this constant is only the built-in fallback.
DEFAULT_OCR_TIMEOUT_S = 1800


class RealMarkerOcrBackend:
    """Shells out to marker-pdf's own ``marker_single`` CLI (the exact
    invocation shape proven in
    ``research/tools/marker_ocr/run_batch.py``: ``marker_single <path>
    --paginate_output --output_dir <dir> --disable_tqdm
    --disable_multiprocessing``) -- GPU-only per the standing origin-project law this
    build brief carries forward (no silent CPU fallback is attempted here;
    a non-zero exit is surfaced as a job failure, not swallowed).

    ``marker_single_exe`` is config-pathed (``trialerror.toml``
    ``[ingest.ocr].marker_single_exe``), never hardcoded to a origin-project-repo
    path -- this module has no idea where that venv lives until told.

    ``timeout_s`` (``trialerror.toml`` ``[ingest.ocr].timeout_s``, default
    :data:`DEFAULT_OCR_TIMEOUT_S`) bounds the ``subprocess.run`` call --
    FX-1: a hung/wedged child now raises :class:`EnvironmentalFailure`
    (job re-queued, retry attempt NOT consumed) instead of blocking the
    handler forever.

    TRIALERROR-DEV-NOTE: marker's ``--paginate_output`` page markers are
    documented (by the origin-project driver this is ported from) as literal
    ``{N}`` lines carrying the ABSOLUTE page number; this backend parses
    those to build :class:`OcrPage` rows. Not exercised against a live
    GPU in this build session (no GPU test in the default suite) --
    live verification is the stated M8/integration-session follow-up
    (design Section 13 F18/M15: "state the hardware assumption").
    """

    name = "marker"

    _PAGE_MARKER_RE = re.compile(r"^\{(\d+)\}-{3,}\s*$", re.MULTILINE)

    def __init__(
        self,
        *,
        marker_single_exe: str,
        version: str = "1.10.2",
        extra_args: Sequence[str] = (),
        timeout_s: float = DEFAULT_OCR_TIMEOUT_S,
    ):
        self.marker_single_exe = marker_single_exe
        self.version = version
        self.extra_args = list(extra_args)
        self.timeout_s = timeout_s

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.marker_single_exe,
            str(input_path),
            "--paginate_output",
            "--output_dir",
            str(work_dir),
            "--disable_tqdm",
            "--disable_multiprocessing",
            *self.extra_args,
        ]
        # FX-2 (IMPL_REVIEW_C_ops.md N-4): encoding="utf-8" so marker's
        # stdout/stderr (UTF-8; progress lines can carry non-ASCII) is never
        # decoded as the Windows ANSI codepage (text=True alone means
        # cp1252 here); errors="replace" so a stray non-UTF-8 byte degrades
        # to U+FFFD instead of raising UnicodeDecodeError out of
        # communicate() and burning a retry attempt on a decode bug rather
        # than the real diagnostics.
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            # FX-1: subprocess.run itself kills the child on timeout (no
            # zombie left behind by THIS call) -- raising
            # EnvironmentalFailure (not RuntimeError) tells
            # trialerror.jobs.worker.run_one this is a transient/environmental
            # failure so the ledger re-queues without consuming an attempt
            # (trialerror/jobs/worker.py run_one's EnvironmentalFailure arm).
            raise EnvironmentalFailure(
                f"marker_single timed out after {self.timeout_s}s for {input_path} "
                "(GPU-bound OCR; raise ingest.ocr.timeout_s in trialerror.toml if this is "
                "expected for large documents)"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"marker_single exited {result.returncode} for {input_path}: {result.stderr[-2000:]}"
            )
        stem = input_path.stem
        produced = work_dir / stem / f"{stem}.md"
        if not produced.exists():
            candidates = list(work_dir.rglob("*.md"))
            if not candidates:
                raise RuntimeError(f"marker_single produced no .md output under {work_dir}")
            produced = candidates[0]
        text = produced.read_text(encoding="utf-8", errors="replace")
        pages = self._split_pages(text)
        return OcrResult(pages=pages, ocr_backend=self.name, ocr_version=self.version)

    @classmethod
    def _split_pages(cls, text: str) -> list[OcrPage]:
        markers = list(cls._PAGE_MARKER_RE.finditer(text))
        if not markers:
            stripped = text.strip()
            return [OcrPage(page_number=1, text=stripped)] if stripped else []
        pages: list[OcrPage] = []
        for i, m in enumerate(markers):
            page_num = int(m.group(1))
            start = m.end()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            body = text[start:end].strip()
            if body:
                pages.append(OcrPage(page_number=page_num, text=body))
        return pages


def load_ocr_backend(config: dict[str, Any]) -> OcrBackend:
    """``config`` = the program's ``trialerror.toml`` ``[ingest.ocr]`` table
    (a plain dict; ``trialerror.util.config.ProgramConfig`` hands these through
    generically per M0's own "fields read generically" note). Defaults to
    the fake backend when unconfigured, so a fresh scaffold works with no
    setup."""
    backend_name = config.get("backend", "fake")
    if backend_name == "fake":
        return FakeOcrBackend()
    if backend_name == "marker":
        marker_single_exe = config.get("marker_single_exe")
        if not marker_single_exe:
            raise ValueError("ingest.ocr.backend = 'marker' requires ingest.ocr.marker_single_exe in trialerror.toml")
        return RealMarkerOcrBackend(
            marker_single_exe=marker_single_exe,
            version=config.get("marker_version", "1.10.2"),
            extra_args=config.get("marker_extra_args", []),
            timeout_s=config.get("timeout_s", DEFAULT_OCR_TIMEOUT_S),
        )
    raise ValueError(f"unknown ingest.ocr.backend {backend_name!r} (choices: 'fake', 'marker')")


# --------------------------------------------------------------------------
# Embed
# --------------------------------------------------------------------------


class EmbedBackend(Protocol):
    model_key: str
    dims: int

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]: ...


#: Small on purpose -- tests embed real (tiny) fixture batches through this
#: backend and must stay fast; production config overrides via
#: ``ingest.embed.dims`` (the real Qwen3-4B backend's matryoshka-truncated
#: 2048, per the origin-project embed_backend.py C-0060 pin).
DEFAULT_FAKE_EMBED_DIMS = 16

#: Default subprocess timeout (seconds) for :class:`RealQwenEmbedBackend`
#: when ``trialerror.toml``'s ``[ingest.embed]`` table doesn't set its own
#: ``timeout_s`` -- FX-1, same rationale as :data:`DEFAULT_OCR_TIMEOUT_S`
#: above (see that constant's docstring for the timeout-vs-lease
#: TRIALERROR-DEV-NOTE, which applies identically here).
DEFAULT_EMBED_TIMEOUT_S = 1800


class FakeEmbedBackend:
    """Deterministic, hash-derived embedding: ``sha256(text)`` expanded into
    ``dims`` floats in ``[-1, 1)``, L2-normalized. Same text -> same vector,
    always -- no model load, no GPU, exercises the full embed/index/anchor
    pipeline shape without needing a real embedding model.

    ``delay_s`` is an internal seam (mirrors
    ``trialerror.util.atomic.atomic_write_bytes``'s own ``_chunk_size``/
    ``_on_chunk`` precedent: "internal seams used by the kill-mid-write
    test to slow the write down deterministically; production callers
    never pass them") letting the kill-mid-embed acceptance test observe
    partial per-batch progress deterministically without any GPU/model
    dependency -- default ``0.0`` is a no-op."""

    def __init__(self, *, dims: int = DEFAULT_FAKE_EMBED_DIMS, delay_s: float = 0.0):
        self.dims = dims
        self.delay_s = delay_s
        # namespaced by dims -- emb's PK is (chunk_sha256, model_key), so two
        # differently-dimensioned fake configs must never collide under one key.
        self.model_key = f"fake-{dims}"

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        if self.delay_s:
            import time

            time.sleep(self.delay_s)
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        needed_bytes = self.dims * 4
        digest = b""
        counter = 0
        while len(digest) < needed_bytes:
            digest += hashlib.sha256(f"{text}::{counter}".encode("utf-8")).digest()
            counter += 1
        raw = struct.unpack(f"<{self.dims}I", digest[:needed_bytes])
        floats = [(v / 0xFFFFFFFF) * 2.0 - 1.0 for v in raw]
        norm = sum(f * f for f in floats) ** 0.5 or 1.0
        return [f / norm for f in floats]


class RealQwenEmbedBackend:
    """Shells out to the real Qwen3-Embedding backend
    (``research/tools/embeddings_local/embed_backend.py``'s
    ``load_backend(name).embed_batch(texts, kind=...)``) via a subprocess
    running in THAT venv (``python_exe``, config-pathed -- never
    hardcoded), so this process never needs torch/sentence-transformers
    itself installed.

    Disk-to-disk (design C-0007: "page text never transits the
    orchestrator's context"): the batch is written to a temp JSON file,
    the driver script (:data:`_DRIVER_SOURCE`, run via ``python -c`` so no
    extra file ships outside this module) reads it, imports
    ``embed_backend`` from ``module_dir`` (added to ``sys.path``), embeds,
    and writes vectors back to a second temp JSON file this process reads.

    ``timeout_s`` (``trialerror.toml`` ``[ingest.embed].timeout_s``, default
    :data:`DEFAULT_EMBED_TIMEOUT_S`) bounds the driver subprocess -- FX-1:
    a hung/wedged embed driver now raises :class:`EnvironmentalFailure`
    (job re-queued, retry attempt NOT consumed) instead of blocking the
    handler forever.

    TRIALERROR-DEV-NOTE: not exercised against a live GPU in this build session
    (no GPU test in the default suite) -- live verification is the stated
    M8/integration-session follow-up (design Section 13 F18/M15).
    """

    _DRIVER_SOURCE = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from embed_backend import load_backend\n"
        "with open(sys.argv[1], 'r', encoding='utf-8') as f:\n"
        "    payload = json.load(f)\n"
        "backend = load_backend(payload['model_key'])\n"
        "vecs = backend.embed_batch(payload['texts'], kind=payload.get('kind', 'document'))\n"
        "out = {'vectors': vecs.tolist(), 'dims': int(vecs.shape[-1])}\n"
        "with open(sys.argv[3], 'w', encoding='utf-8') as f:\n"
        "    json.dump(out, f)\n"
    )

    def __init__(
        self,
        *,
        python_exe: str,
        module_dir: str,
        model_key: str = "qwen3-4b",
        dims: int = 2048,
        timeout_s: float = DEFAULT_EMBED_TIMEOUT_S,
    ):
        self.python_exe = python_exe
        self.module_dir = module_dir
        self.model_key = model_key
        self.dims = dims
        self.timeout_s = timeout_s

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="trialerror-embed-") as tmp:
            in_path = Path(tmp) / "in.json"
            out_path = Path(tmp) / "out.json"
            in_path.write_text(
                json.dumps({"texts": list(texts), "kind": kind, "model_key": self.model_key}, ensure_ascii=False),
                encoding="utf-8",
            )
            # FX-2 (IMPL_REVIEW_C_ops.md N-4): encoding="utf-8" so the
            # driver's own stderr (UTF-8; a torch/transformers traceback can
            # carry non-ASCII) is never decoded as the Windows ANSI
            # codepage; errors="replace" for the same reason
            # RealMarkerOcrBackend.run carries it -- a decode bug must never
            # masquerade as (or pre-empt reporting) the real diagnostics.
            try:
                result = subprocess.run(
                    [self.python_exe, "-c", self._DRIVER_SOURCE, str(in_path), self.module_dir, str(out_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                # FX-1: see RealMarkerOcrBackend.run's identical arm --
                # subprocess.run kills the child on timeout, and
                # EnvironmentalFailure (not RuntimeError) routes this
                # through trialerror.jobs.worker.run_one's environmental-failure
                # arm so the ledger re-queues without consuming an attempt.
                raise EnvironmentalFailure(
                    f"real embed driver timed out after {self.timeout_s}s for a batch of {len(texts)} "
                    "text(s) (GPU-bound embedding; raise ingest.embed.timeout_s in trialerror.toml if this "
                    "is expected for large batches)"
                ) from exc
            if result.returncode != 0:
                raise RuntimeError(f"real embed driver exited {result.returncode}: {result.stderr[-2000:]}")
            out = json.loads(out_path.read_text(encoding="utf-8"))
        return out["vectors"]


def load_embed_backend(config: dict[str, Any]) -> EmbedBackend:
    """``config`` = the program's ``trialerror.toml`` ``[ingest.embed]`` table.
    Defaults to the fake backend when unconfigured."""
    backend_name = config.get("backend", "fake")
    if backend_name == "fake":
        return FakeEmbedBackend(
            dims=config.get("dims", DEFAULT_FAKE_EMBED_DIMS),
            delay_s=config.get("delay_s", 0.0),  # test-only seam -- see FakeEmbedBackend's docstring
        )
    python_exe = config.get("python_exe")
    module_dir = config.get("module_dir")
    if not python_exe or not module_dir:
        raise ValueError(
            f"ingest.embed.backend = {backend_name!r} requires ingest.embed.python_exe and "
            "ingest.embed.module_dir in trialerror.toml"
        )
    return RealQwenEmbedBackend(
        python_exe=python_exe,
        module_dir=module_dir,
        model_key=backend_name,
        dims=config.get("dims", 2048),
        timeout_s=config.get("timeout_s", DEFAULT_EMBED_TIMEOUT_S),
    )
