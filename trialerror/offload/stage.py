"""The sandbox side of the seam: resolve a stage from published outputs,
or park it.

This is the five-step logic design section 4 spells out for a handler
whose backend is ``"offload"``, in one place so ``run_ocr`` and
``run_embed`` share it exactly:

1. ``done/<job_id>/`` present -> verify every payload sha and the
   ``expect`` block -> feed the result into the SAME tail the Real backends
   feed. Mismatch -> the result is moved to ``failed/`` and the stage
   raises a LOGIC failure (attempt burned, visible).
2. else if the manifest already exists in ``pending/ u claimed/*/ u done/ u
   failed/`` -> nothing new is written.
3. else write inputs + manifest (``offload_attempts = 0``).
4. if ``failed/<job_id>/`` exists -> raise a LOGIC failure (terminal).
5. otherwise raise ``EnvironmentalFailure("awaiting DEV GPU",
   retry_delay_s=1800)``.

Steps 1 and 4 are checked before 2/3 here, which is the same set of rules
in the only order that can be evaluated once: a job cannot be both
published and pending.

**Why an environmental failure and not a pause.** Parking as
``EnvironmentalFailure`` means the ledger re-queues the job WITHOUT
consuming an attempt (``trialerror.jobs.ledger.fail``'s environmental arm),
so a DEV laptop that stays off for three weeks costs the job nothing: its
``attempts`` column is still 0 when the GPU finally comes back. That is
the property the whole "DEV is intermittent" design rests on, and it is
why the retry budget is tracked separately, inside the marker, as
``offload_attempts`` -- that counter measures GPU failures, which are real
failures, rather than absences, which are not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from trialerror.jobs.worker import EnvironmentalFailure
from trialerror.offload import protocol
from trialerror.offload.marker import config_hash

__all__ = [
    "PARK_RETRY_DELAY_S",
    "OCR_INPUT_STEM",
    "OCR_OUTPUT_NAME",
    "EMBED_INPUT_NAME",
    "EMBED_OUTPUT_NAME",
    "ocr_input_name",
    "build_chunks_payload",
    "read_chunks_payload",
    "offload_ocr_result",
    "offload_embed_vectors",
]

#: design section 4 step 5: ``retry_delay_s=1800``. Half an hour is the
#: cadence the sandbox's jobs loop already runs at, so a parked job is
#: re-examined roughly once per loop rather than spinning.
PARK_RETRY_DELAY_S = 1800

OCR_INPUT_STEM = "input"
OCR_OUTPUT_NAME = "pages.json"
EMBED_INPUT_NAME = "chunks.jsonl"
EMBED_OUTPUT_NAME = "vectors.jsonl"

_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}\Z")


def _read_input_under_program_root(program_root: Path | str, raw_path: Path | str) -> bytes:
    """Read a stage input, refusing any path that resolves outside the
    program root (SEC-8).

    ``document.raw_path`` is data: it comes from whatever registered the
    document, it is stored verbatim, and ``_resolve_raw_path`` honours it
    as-is when it is absolute. Locally that only ever names a file the
    process could already read. Offloading changes the consequence: this
    byte string is copied into ``offload/pending/<job>/`` and then handed
    over SSH to another machine, so an arbitrary absolute ``raw_path``
    would be an exfiltration channel with a queue and a courier attached.
    The queue only ever carries the program's own files."""
    root = Path(program_root).resolve()
    resolved = Path(raw_path).resolve()
    try:
        inside = resolved.is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - different drives on Windows
        inside = False
    if not inside:
        raise RuntimeError(
            f"offload: refusing to queue {resolved} -- an offloaded stage may only send files "
            f"from inside the program root ({root}). Fix the document's raw_path, or re-register "
            "the document from a location inside the program."
        )
    return resolved.read_bytes()


def ocr_input_name(raw_path: Path | str) -> str:
    """``input.pdf`` for a ``.pdf`` raw file, ``input`` for anything whose
    extension is not a plain short alphanumeric one. The extension is
    carried because marker dispatches on it; it is sanitized because the
    name becomes a path component on two machines."""
    suffix = Path(raw_path).suffix
    return f"{OCR_INPUT_STEM}{suffix.lower()}" if _SAFE_SUFFIX_RE.match(suffix) else OCR_INPUT_STEM


# ---------------------------------------------------------------------------
# payload encoding (shared with the DEV worker, which decodes them)
# ---------------------------------------------------------------------------
def build_chunks_payload(chunks: Sequence[dict[str, Any]]) -> bytes:
    """``chunks.jsonl``: one ``{"chunk_id", "seq", "text"}`` per line, in
    the order the manifest's ``expect.chunk_ids`` lists them. ALL chunks of
    the document travel together (design v3 delta N2: the real backend
    embeds in batches of eight, so batching is the WORKER's business, not
    the queue's)."""
    lines = [
        json.dumps(
            {"chunk_id": c["chunk_id"], "seq": c.get("seq"), "text": c["text"]},
            ensure_ascii=False,
        )
        for c in chunks
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def read_chunks_payload(data: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# the shared resolve-or-park core
# ---------------------------------------------------------------------------
def _root(ctx) -> Path:
    return protocol.offload_root(ctx.store.program_root)


def _reject(root: Path, job_id: str, manifest: dict[str, Any], reason: str) -> RuntimeError:
    """Move a bad published result to ``failed/`` and build the logic
    failure the caller raises. Separated from the raise so every rejection
    site records the same way."""
    protocol.fail_marker(root, job_id, manifest=manifest, error=reason)
    return RuntimeError(f"offload {job_id}: {reason}")


def _resolve_or_park(
    ctx,
    *,
    stage: str,
    doc_id: str | None,
    config: dict[str, Any],
    expect: dict[str, Any],
    build_inputs: Callable[[], Iterable[tuple[str, bytes]]],
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Returns ``(result, done_dir, manifest)`` when the DEV worker has
    published a verified result; otherwise raises
    :class:`~trialerror.jobs.worker.EnvironmentalFailure` (parked) or
    ``RuntimeError`` (terminal, a logic failure)."""
    root = _root(ctx)
    job_id = protocol.validate_job_id(ctx.job_id)
    cfg_hash = config_hash(config)
    located = protocol.find_manifest(root, job_id)

    # -- step 1: a published result ---------------------------------------
    if located is not None and located[0] == "done":
        # SEC-1: prefer the sandbox's OWN copy of the manifest. The one in
        # done/<job>/ arrived through a directory the restricted DEV key
        # can write into; queued/ never was.
        manifest = protocol.trusted_manifest(root, job_id, located[1])
        error = protocol.published_error(root, job_id)
        if error is not None:
            _handle_worker_error(root, job_id, manifest, error, build_inputs)  # always raises
        if manifest.get("config_hash") != cfg_hash:
            # SEC-2. The published result answers a question this stage is
            # no longer asking: the marker was queued against a different
            # [ingest.*] table than the one loaded now. The design is
            # explicit that "SANDBOX owns the jobs ledger and the truth",
            # and the truth is the CURRENT configuration -- so the stale
            # result is discarded and the work re-queued under the current
            # hash, rather than folded in because it verifies against the
            # stale manifest that shipped with it. No offload attempt is
            # burned: the previous attempt did not fail, it answered a
            # superseded question.
            protocol.requeue_marker(
                root,
                job_id,
                manifest={**manifest, "config_hash": cfg_hash, "expect": dict(expect)},
                inputs=build_inputs(),
            )
            ctx.set_checkpoint({"offload": "requeued_config_change", "job_id": job_id, "stage": stage})
            raise EnvironmentalFailure(
                f"offload {job_id}: the published result was produced for a different "
                f"[ingest.{stage}] configuration than this program now has; discarded and "
                "re-queued for the DEV GPU",
                retry_delay_s=PARK_RETRY_DELAY_S,
            )
        try:
            result = protocol.verify_published(root, job_id, manifest)
        except protocol.OffloadVerificationError as exc:
            raise _reject(root, job_id, manifest, str(exc)) from exc
        return result, protocol.done_dir(root) / job_id, manifest

    # -- step 4: terminal ---------------------------------------------------
    if located is not None and located[0] == "failed":
        manifest = protocol.trusted_manifest(root, job_id, located[1])
        err = protocol.read_json(protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME)
        raise RuntimeError(
            f"offload {job_id}: terminal after {manifest.get('offload_attempts')} DEV attempt(s) -- "
            f"{err.get('error')} (inspect {protocol.failed_dir(root) / job_id}; delete that directory "
            "to let the stage queue a fresh attempt)"
        )

    # -- step 2: already queued or in flight -------------------------------
    if located is not None:
        ctx.set_checkpoint({"offload": located[0], "job_id": job_id, "stage": stage})
        raise EnvironmentalFailure(
            f"awaiting DEV GPU: offload {stage} job {job_id} is {located[0]} "
            "(run the GPU worker on DEV)",
            retry_delay_s=PARK_RETRY_DELAY_S,
        )

    # -- step 3: queue it ---------------------------------------------------
    protocol.queue_marker(
        root,
        job_id=job_id,
        stage=stage,
        doc_id=doc_id,
        expect=expect,
        config_hash=cfg_hash,
        inputs=build_inputs(),
    )
    ctx.set_checkpoint({"offload": "pending", "job_id": job_id, "stage": stage})
    raise EnvironmentalFailure(
        f"awaiting DEV GPU: offload {stage} job {job_id} queued (run the GPU worker on DEV)",
        retry_delay_s=PARK_RETRY_DELAY_S,
    )


def _handle_worker_error(
    root: Path,
    job_id: str,
    manifest: dict[str, Any],
    error: dict[str, Any],
    build_inputs: Callable[[], Iterable[tuple[str, bytes]]],
) -> None:
    """The DEV worker published an ``error.json`` instead of a result.

    Burn one OFFLOAD attempt. Under budget: re-queue (environmental, so the
    ledger row keeps its own attempts) -- inputs are rebuilt from the store
    rather than salvaged. Out of budget: the marker becomes terminal and
    the stage raises a logic failure, which the ledger consumes an attempt
    for; the next two claims hit the ``failed/`` branch above and consume
    the remaining two, so the row ends ``abandoned`` exactly as design
    acceptance C-fail requires."""
    attempts = int(manifest.get("offload_attempts", 0)) + 1
    max_attempts = int(manifest.get("max_attempts", protocol.DEFAULT_MAX_OFFLOAD_ATTEMPTS))
    manifest = {**manifest, "offload_attempts": attempts}
    reason = str(error.get("error") or "unknown DEV-side failure")
    if attempts >= max_attempts:
        protocol.fail_marker(root, job_id, manifest=manifest, error=reason)
        raise RuntimeError(
            f"offload {job_id}: DEV worker failed {attempts}/{max_attempts} times -- {reason}"
        )
    protocol.requeue_marker(root, job_id, manifest=manifest, inputs=build_inputs())
    raise EnvironmentalFailure(
        f"offload {job_id}: DEV attempt {attempts}/{max_attempts} failed ({reason}); re-queued",
        retry_delay_s=PARK_RETRY_DELAY_S,
    )


# ---------------------------------------------------------------------------
# the two stage entry points the handlers call
# ---------------------------------------------------------------------------
def offload_ocr_result(ctx, *, doc: dict[str, Any], raw_path: Path, ocr_cfg: dict[str, Any]):
    """Return an :class:`~trialerror.ingest.backends.OcrResult` built from
    the DEV worker's published pages, or park/fail the job."""
    from trialerror.ingest.backends import OcrPage, OcrResult

    input_name = ocr_input_name(raw_path)
    expect = {
        "stage": "ocr",
        "backend": ocr_cfg.get("expect_backend") or None,
        "version": ocr_cfg.get("expect_version") or None,
        "outputs": [OCR_OUTPUT_NAME],
        "input_name": input_name,
    }

    def build_inputs() -> list[tuple[str, bytes]]:
        return [(input_name, _read_input_under_program_root(ctx.store.program_root, raw_path))]

    result, done, manifest = _resolve_or_park(
        ctx,
        stage="ocr",
        doc_id=doc.get("doc_id"),
        config=ocr_cfg,
        expect=expect,
        build_inputs=build_inputs,
    )
    root = _root(ctx)
    job_id = ctx.job_id
    try:
        payload = json.loads((done / OCR_OUTPUT_NAME).read_text(encoding="utf-8"))
        pages_raw = payload["pages"]
        pages = [
            OcrPage(page_number=int(p["page_number"]), text=str(p["text"]))
            for p in pages_raw
        ]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _reject(root, job_id, manifest, f"unreadable {OCR_OUTPUT_NAME}: {exc}") from exc
    if not pages:
        raise _reject(root, job_id, manifest, f"{OCR_OUTPUT_NAME} contains no pages")
    ctx.set_checkpoint({"offload": "published", "ocr_pages": len(pages), "job_id": job_id})
    return OcrResult(
        pages=pages,
        ocr_backend=str(result.get("backend")),
        ocr_version=str(result.get("version")),
    )


def offload_embed_vectors(
    ctx,
    *,
    doc_id: str,
    chunks: Sequence[dict[str, Any]],
    embed_cfg: dict[str, Any],
    model_key: str,
    dims: int,
) -> dict[str, list[float]]:
    """Return ``{chunk_id: vector}`` for EVERY chunk of the document from
    the DEV worker's published vectors, or park/fail the job."""
    chunk_ids = [c["chunk_id"] for c in chunks]
    expect = {
        "stage": "embed",
        "model_key": model_key,
        "dims": int(dims),
        "chunk_count": len(chunk_ids),
        "chunk_ids": chunk_ids,
        "outputs": [EMBED_OUTPUT_NAME],
        "input_name": EMBED_INPUT_NAME,
    }

    def build_inputs() -> list[tuple[str, bytes]]:
        return [(EMBED_INPUT_NAME, build_chunks_payload(chunks))]

    _result, done, manifest = _resolve_or_park(
        ctx,
        stage="embed",
        doc_id=doc_id,
        config=embed_cfg,
        expect=expect,
        build_inputs=build_inputs,
    )
    root = _root(ctx)
    job_id = ctx.job_id
    vectors: list[list[float]] = []
    try:
        for line in (done / EMBED_OUTPUT_NAME).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                vectors.append([float(x) for x in json.loads(line)])
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _reject(root, job_id, manifest, f"unreadable {EMBED_OUTPUT_NAME}: {exc}") from exc

    if len(vectors) != len(chunk_ids):
        raise _reject(
            root,
            job_id,
            manifest,
            f"{EMBED_OUTPUT_NAME} has {len(vectors)} vector(s) for {len(chunk_ids)} chunk(s)",
        )
    bad = next((i for i, v in enumerate(vectors) if len(v) != int(dims)), None)
    if bad is not None:
        raise _reject(
            root,
            job_id,
            manifest,
            f"vector {bad} has {len(vectors[bad])} dimension(s), expected {dims}",
        )
    ctx.set_checkpoint(
        {"offload": "published", "embedded": len(vectors), "model_key": model_key, "job_id": job_id}
    )
    return dict(zip(chunk_ids, vectors))
