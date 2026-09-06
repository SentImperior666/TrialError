"""M7's job handlers -- ride M2's ledger (build brief: "your workers are
handlers; drop trialerror/ingest/handlers.py with
@register_handler("normalize")/"ocr"/"chunk"/"embed"/"index" etc.").

Stage graph as built (design Section 6):

    add_document -> {normalize | ocr}  (media-type dispatch, decided inline
                                         in trialerror.ingest.pipeline.add_document,
                                         not its own job)
    normalize    -> chunk   (direct formats: pdf-text/html/epub/md)
    ocr          -> chunk   (routed formats: pdf-scan/image; marker GPU or
                              the fake backend produce the SAME element
                              shape normalize's direct formats do, so both
                              paths converge here)
    chunk        -> embed
    embed        -> index
    index        -> (terminal; ``extract`` is registered but NOT
                      auto-chained -- opt-in only, see its own docstring
                      below; ``extract`` -> nothing further -- candidates
                      land in the merge-review queue, ``trialerror.ingest.extract``,
                      for an explicit accept/reject step, never auto-chained
                      onward into entity/relation/claim)

Restart-safety (design Section 6: "each idempotent, content-hash-keyed,
and resumable via the jobs ledger"): every handler below re-derives "what's
already durably written" from the KNOWLEDGE STORE ITSELF at the top of each
run (existing chunk seqs, existing emb rows by (chunk_sha256, model_key),
existing chunk_fts/vec_chunks rows by chunk_id) rather than trusting only
the ledger's ``checkpoint`` JSON -- the store is the durable source of
truth; ``ctx.set_checkpoint`` is called for liveness/heartbeat and an
informational progress payload, same division of labor
``trialerror.jobs.worker.JobContext.set_checkpoint``'s own docstring describes
for the origin-project embed/OCR runners this ports the shape from. This is what
makes a kill-mid-embed resume byte-identical to an uninterrupted run: a
chunk whose ``(sha256, model_key)`` already has an ``emb`` row is simply
skipped, whichever attempt produced it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trialerror.ingest.anchors import build_chunk_anchor, sha256_hex
from trialerror.ingest.backends import load_embed_backend, load_ocr_backend
from trialerror.ingest.chunker import build_chunks
from trialerror.ingest.errors import InvalidNormalizerOverrideError
from trialerror.ingest.normalizers import NORMALIZER_ID, NORMALIZER_VERSION, normalize_direct
from trialerror.ingest.sanitizer import SANITIZER_VERSION, sanitize
from trialerror.ingest.stream import stream_v1
from trialerror.jobs import ledger
from trialerror.jobs.registry import register_handler
from trialerror.offload.marker import is_offload_config
from trialerror.retrieve import lexical
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import get, insert, update
from trialerror.util.atomic import atomic_write_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["run_djvu", "run_normalize", "run_ocr", "run_chunk", "run_embed", "run_index", "run_extract"]


def _enqueue_next_stage(store: Store, *, stage: str, payload: dict[str, Any], job_id: str) -> None:
    """``ledger.enqueue`` is create-only (a second call with the same
    ``job_id`` raises on the PK conflict) -- but a handler that crashes
    AFTER enqueueing its next stage and BEFORE its own settlement will
    redo this same enqueue call on resume (design Section 6: "each
    idempotent ... resumable via the jobs ledger"). Skip the create when
    the next stage's job already exists, so a resumed normalize/ocr/chunk/
    embed handler never fails on a duplicate-job_id conflict for a stage
    it already handed off successfully. ``stage`` is the logical stage name,
    mapped to the real ``job.kind`` via
    ``trialerror.ingest.pipeline.stage_job_kind_and_payload`` (``normalize``/
    ``chunk`` ride ``kind='custom'`` -- see that function's docstring)."""
    from trialerror.ingest.pipeline import stage_job_kind_and_payload

    job_kind, job_payload = stage_job_kind_and_payload(stage, payload)
    if ledger.get_job(store, job_id) is None:
        ledger.enqueue(store, kind=job_kind, payload=job_payload, job_id=job_id)


def _load_config(store: Store) -> dict[str, Any]:
    """The program's ``trialerror.toml``, or ``{}`` when there is none.

    **Fail-closed (design D13, lane L0-C).** This used to swallow every
    exception and return ``{}`` -- which meant a single mistyped character
    in ``trialerror.toml`` silently reverted a GPU-configured program to
    the fake OCR/embed backends and wrote hash-derived stand-ins into the
    record. An ABSENT config still means "a scratch program, use the
    defaults" (that is what most of the test suite runs on); a PRESENT but
    unparseable one now raises, because the operator's stated intent
    exists and could not be read.

    :func:`~trialerror.ingest.backends.assert_real_backends_if_required`
    then enforces the program's own ``[ingest] require_real_backends``
    posture at the same single load point."""
    from trialerror.ingest.backends import assert_real_backends_if_required
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = store.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    raw = load_config(cfg_path).raw
    assert_real_backends_if_required(raw)
    return raw


def _resolve_raw_path(store: Store, doc: dict[str, Any]) -> Path:
    raw_path = Path(doc["raw_path"])
    return raw_path if raw_path.is_absolute() else (store.program_root / raw_path)


def _load_elements(store: Store, doc_id: str) -> list[dict[str, Any]]:
    rows = store.knowledge.execute(
        "SELECT * FROM element WHERE doc_id = ? ORDER BY seq", (doc_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _finish_normalize_stage(
    ctx,
    doc: dict[str, Any],
    drafts: list[dict[str, Any]],
    *,
    ocr_backend: str | None,
    ocr_version: str | None,
    normalizer_id: str = NORMALIZER_ID,
    normalizer_version: str = NORMALIZER_VERSION,
) -> None:
    """Shared tail of ``normalize``/``ocr``: sanitize + insert every
    element draft, build ``stream_v1`` over them, stamp the document's
    ``sha256`` (design Section 4.1: "sha256 (normalized text)") and
    ``status = 'normalized'``, archive the stream text to disk, then
    enqueue the ``chunk`` stage.

    ``normalizer_id``/``normalizer_version`` default to the generic
    ``trialerror.ingest.normalizers`` constants every direct format and every
    OCR route gets -- ``run_normalize``/``run_ocr`` below forward
    ``payload['normalizer_id_override']``/``['normalizer_version_override']``
    here instead when present (via :func:`_resolve_normalizer_override`,
    which validates the override -- Fix pass F11), which is how a document
    that went through ``trialerror.ingest.normalize_djvu``'s ``djvu`` stage
    first ends up stamped ``normalizer_id='djvu-ddjvu'`` rather than the
    generic id, without this shared tail needing to know DjVu exists at
    all."""
    store = ctx.store
    doc_id = doc["doc_id"]

    existing = {r["seq"] for r in store.knowledge.execute("SELECT seq FROM element WHERE doc_id=?", (doc_id,)).fetchall()}
    element_rows: list[dict[str, Any]] = []
    for d in sorted(drafts, key=lambda x: x["seq"]):
        sanitized_text, _removed = sanitize(d.get("text") or "")
        row = {
            "element_id": new_id("ELM"),
            "doc_id": doc_id,
            "seq": d["seq"],
            "type": d["type"],
            "text": sanitized_text,
            "text_as_html": d.get("text_as_html"),
            "page_number": d.get("page_number"),
            "bbox": d.get("bbox"),
            "parent_element": d.get("parent_element"),
            "category_depth": d.get("category_depth"),
            "detection_origin": d.get("detection_origin"),
        }
        if d["seq"] not in existing:
            insert(store, "element", row)
        element_rows.append(row)

    if not element_rows:
        element_rows = _load_elements(store, doc_id)

    stream_text = stream_v1(element_rows)
    changes: dict[str, Any] = {
        "sha256": sha256_hex(stream_text),
        "status": "normalized",
        "normalizer_id": normalizer_id,
        "normalizer_version": normalizer_version,
        "sanitizer_version": SANITIZER_VERSION,
    }
    if ocr_backend is not None:
        changes["ocr_backend"] = ocr_backend
        changes["ocr_version"] = ocr_version
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes=changes)

    archive_path = store.program_root / doc["rel_path"]
    atomic_write_text(archive_path, stream_text)

    ctx.set_checkpoint({"elements": len(element_rows)})

    created_by_launch = ctx.payload["created_by_launch"]
    _enqueue_next_stage(
        store,
        stage="chunk",
        payload={"doc_id": doc_id, "created_by_launch": created_by_launch},
        job_id=f"JOB-ingest-{doc_id}-chunk",
    )


def _resolve_normalizer_override(payload: dict[str, Any]) -> tuple[str, str]:
    """Fix pass (VERIFY_ingest-djvu.md F11): before the DjVu lane,
    ``document.normalizer_id``/``normalizer_version`` could only ever hold
    the two generic ``trialerror.ingest.normalizers`` constants -- no caller
    could influence them. ``_finish_normalize_stage`` now takes them from
    ``payload.get('normalizer_id_override'/'normalizer_version_override')``,
    and ``run_one`` accepts a caller-supplied job payload
    (``trialerror jobs start-worker --payload '{...}'`` -> ``cli/jobs.py``'s
    ``json.loads`` -> ``claim_or_create``), so an unvalidated override let
    any hand-written payload stamp ``document.normalizer_id`` with
    arbitrary free text on an ordinary document that never went near DjVu.
    Validated here against the only override this codebase actually
    produces -- :data:`trialerror.ingest.normalize_djvu.NORMALIZER_ID_DJVU`
    -- via a LOCAL import (mirrors ``run_djvu``'s own local import of the
    same module, and keeps ``normalize_djvu`` import-optional for callers
    of this module that never touch DjVu)."""
    from trialerror.ingest.normalize_djvu import NORMALIZER_ID_DJVU

    normalizer_id = payload.get("normalizer_id_override", NORMALIZER_ID)
    normalizer_version = payload.get("normalizer_version_override", NORMALIZER_VERSION)
    if normalizer_id not in (NORMALIZER_ID, NORMALIZER_ID_DJVU):
        raise InvalidNormalizerOverrideError(
            f"payload['normalizer_id_override'] = {normalizer_id!r} is not a recognized "
            f"normalizer id ({NORMALIZER_ID!r}, {NORMALIZER_ID_DJVU!r}) -- refusing to stamp "
            "document.normalizer_id from an unvalidated job payload"
        )
    return normalizer_id, normalizer_version


@register_handler("djvu")
def run_djvu(ctx) -> None:
    """design Section 6 stage 3 extension (trialerror.ingest.normalize_djvu's own
    module docstring has the full design): converts a ``.djvu``/``.djv``
    source to a derived PDF via DjVuLibre's ``ddjvu``, decides the
    pdf-text/pdf-scan route via ``djvutxt``'s text-layer probe, rewrites
    THIS document's ``media_type``/``raw_path`` to that derived PDF, then
    re-enqueues ``normalize`` or ``ocr`` for it -- carrying
    ``normalizer_id_override``/``normalizer_version_override`` in that
    job's payload so ``_finish_normalize_stage`` stamps
    ``NORMALIZER_ID_DJVU`` instead of the generic normalizer id/version.

    Rides ``kind='custom'``/``payload['handler']='djvu'``
    (``trialerror.ingest.pipeline._CUSTOM_STAGE_KINDS`` -- a stage that will
    never become a first-class ``job.kind`` value, unlike ``normalize``/
    ``chunk``), enqueued by ``add_document`` for ``media_type='djvu'``
    exactly like every other stage's first job.

    **Restart-safety** (module docstring's "each idempotent ... resumable
    via the jobs ledger", same as every other handler here): a WORKER
    crash can land between this handler's document-row rewrite and its own
    settlement, same as any other stage -- but unlike ``run_normalize``/
    ``run_ocr`` (which always re-derive from an UNCHANGING ``raw_path``/
    ``media_type``), a resumed ``run_djvu`` would otherwise try to feed the
    derived PDF it already produced back into ``ddjvu`` as if it were the
    original ``.djvu`` source. So this checks ``doc['media_type']`` FIRST:
    still ``'djvu'`` means convert for real; anything else means a prior
    attempt at this SAME job already got at least as far as the row
    rewrite, and this run just re-derives the next stage from what is
    already on disk/in the ledger (the checkpoint this same prior attempt
    wrote BEFORE that rewrite -- see the ordering below -- carries the
    ``normalizer_version`` a bare resume has no other way to recover)."""
    from trialerror.ingest.normalize_djvu import MEDIA_TYPE_DJVU, NORMALIZER_ID_DJVU, convert_and_route
    from trialerror.ingest.errors import DjVuResumeMediaTypeError
    from trialerror.ingest.pipeline import DEFAULT_ARCHIVE_DIR

    payload = ctx.payload
    doc_id = payload["doc_id"]
    created_by_launch = payload["created_by_launch"]
    store = ctx.store
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise RuntimeError(f"djvu: no such document {doc_id!r}")

    if doc["media_type"] == MEDIA_TYPE_DJVU:
        raw_path = _resolve_raw_path(store, doc)
        config = _load_config(store)
        djvu_cfg = config.get("ingest", {}).get("djvu", {})
        archive_dir_value = config.get("paths", {}).get("archive_dir", DEFAULT_ARCHIVE_DIR)

        # Fix pass (F5): renew the lease right before the (potentially
        # 30-minute) ddjvu call -- DEFAULT_DJVU_TIMEOUT_S (1800s) exceeds
        # trialerror.jobs.ledger.LEASE_DURATION_S (900s default), so a
        # legitimate long conversion can otherwise outlive its lease and be
        # reclaimed by another worker mid-convert (see that constant's own
        # docstring). This alone doesn't cover the conversion call itself
        # (one blocking subprocess.run with no heartbeat granularity
        # inside it) -- deployments still need to pair [ingest.djvu]
        # timeout_s with a matching --lease-s for real long-running books.
        ctx.heartbeat()
        result = convert_and_route(
            program_root=store.program_root,
            doc_id=doc_id,
            src_path=raw_path,
            config=djvu_cfg,
            archive_dir=archive_dir_value,
        )

        # No free-form JSON/notes column exists on `document` (checked
        # against trialerror/stores/schema/knowledge.py, not assumed) -- the
        # derived PDF's own sha256 (and the routing signal that produced
        # it, and the normalizer_version a resumed attempt below needs)
        # lands on THIS stage's own job checkpoint instead, the nearest
        # already-existing durable JSON slot every handler in this module
        # already treats as free-form informational metadata (module
        # docstring above, and trialerror.ingest.normalize_djvu's own
        # "Provenance note" says the same). Written BEFORE the document
        # row itself changes, so it survives a crash that lands between
        # the two (the restart-safety note above).
        ctx.set_checkpoint(
            {
                "djvu_pdf_sha256": result["derived_pdf_sha256"],
                "djvu_text_layer": result["text_layer"],
                "djvu_text_chars": result["text_chars"],
                "djvu_route": result["media_type"],
                "djvu_normalizer_version": result["normalizer_version"],
            }
        )
        update(
            store,
            "document",
            pk_column="doc_id",
            pk_value=doc_id,
            changes={"media_type": result["media_type"], "raw_path": result["derived_pdf_rel_path"]},
        )
        resolved_media_type = result["media_type"]
        normalizer_version = result["normalizer_version"]
    else:
        # Fix pass (F6): this branch used to trust whatever media_type it
        # found on the row with no guard that it is one the conversion
        # could ever have produced -- a misuse-only path (requeue_stage
        # against a document that never went through 'djvu') silently sent
        # an unrelated document through OCR and stamped it 'djvu-ddjvu'.
        # Only the two outcomes convert_and_route can actually produce are
        # accepted; anything else is a named error, not a guess.
        resolved_media_type = doc["media_type"]
        if resolved_media_type not in ("pdf-text", "pdf-scan"):
            raise DjVuResumeMediaTypeError(
                f"djvu: resumed job for document {doc_id!r} found media_type="
                f"{resolved_media_type!r}, but a djvu conversion can only ever have left "
                "'pdf-text' or 'pdf-scan' behind -- this document did not go through the "
                "djvu stage (or its row was rewritten by something else since)"
            )
        normalizer_version = ctx.checkpoint.get("djvu_normalizer_version", "unknown")

    next_stage = "normalize" if resolved_media_type == "pdf-text" else "ocr"
    _enqueue_next_stage(
        store,
        stage=next_stage,
        payload={
            "doc_id": doc_id,
            "created_by_launch": created_by_launch,
            "normalizer_id_override": NORMALIZER_ID_DJVU,
            "normalizer_version_override": normalizer_version,
        },
        job_id=f"JOB-ingest-{doc_id}-{next_stage}",
    )


@register_handler("normalize")
def run_normalize(ctx) -> None:
    """design Section 6 stage 3 for a directly-normalizable ``media_type``
    (pdf-text/html/epub/md, or the derived-pdf-text route
    ``trialerror.ingest.normalize_djvu``'s ``djvu`` stage re-dispatches into)."""
    payload = ctx.payload
    doc_id = payload["doc_id"]
    store = ctx.store
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise RuntimeError(f"normalize: no such document {doc_id!r}")
    raw_path = _resolve_raw_path(store, doc)
    drafts = normalize_direct(doc["media_type"], raw_path)
    normalizer_id, normalizer_version = _resolve_normalizer_override(payload)
    _finish_normalize_stage(
        ctx,
        doc,
        drafts,
        ocr_backend=None,
        ocr_version=None,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
    )


@register_handler("ocr")
def run_ocr(ctx) -> None:
    """design Section 6 stage 4: "marker GPU (existing); detached job;
    GPU-only (standing law); batch-chunked; page anchors preserved" --
    routed formats (pdf-scan/image, or the derived-pdf-scan route
    ``trialerror.ingest.normalize_djvu``'s ``djvu`` stage re-dispatches into
    when the source has no usable text layer), backend chosen via
    ``trialerror.ingest.backends.load_ocr_backend`` (fake by default; real
    marker per ``trialerror.toml [ingest.ocr]``)."""
    payload = ctx.payload
    doc_id = payload["doc_id"]
    store = ctx.store
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise RuntimeError(f"ocr: no such document {doc_id!r}")
    raw_path = _resolve_raw_path(store, doc)
    config = _load_config(store)
    ocr_cfg = config.get("ingest", {}).get("ocr", {})

    if is_offload_config(ocr_cfg):
        # Lane L0-C (design D5): this box has no GPU. Resolve the pages
        # from a result the DEV worker already published, or park the job
        # (EnvironmentalFailure, attempt NOT consumed) until it does.
        # Everything below this branch is unchanged -- the offload path
        # feeds the SAME tail the real backends feed.
        from trialerror.offload.stage import offload_ocr_result

        result = offload_ocr_result(ctx, doc=doc, raw_path=raw_path, ocr_cfg=ocr_cfg)
    else:
        backend = load_ocr_backend(ocr_cfg)
        work_dir = store.program_root / "jobs_work" / doc_id / "ocr"
        result = backend.run(input_path=raw_path, work_dir=work_dir)
    ctx.set_checkpoint({"ocr_pages": len(result.pages)})

    drafts = [
        {
            "seq": i,
            "type": "NarrativeText",
            "text": page.text,
            "page_number": page.page_number,
            # ``result.ocr_backend`` rather than ``backend.name``: identical
            # for every local backend (both are "fake"/"marker"), and the
            # only correct answer for an offloaded stage, where the backend
            # object here is a marker and the real name came back with the
            # published result.
            "detection_origin": f"ocr:{result.ocr_backend}",
        }
        for i, page in enumerate(result.pages)
    ]
    normalizer_id, normalizer_version = _resolve_normalizer_override(payload)
    _finish_normalize_stage(
        ctx,
        doc,
        drafts,
        ocr_backend=result.ocr_backend,
        ocr_version=result.ocr_version,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
    )


@register_handler("chunk")
def run_chunk(ctx) -> None:
    """design Section 6 stage 5: the two-pass boundary-aware chunker +
    per-chunk ``quote_anchor`` (design Section 4.1's ``stream_v1``
    anchoring)."""
    payload = ctx.payload
    doc_id = payload["doc_id"]
    created_by_launch = payload["created_by_launch"]
    store = ctx.store
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise RuntimeError(f"chunk: no such document {doc_id!r}")

    elements = _load_elements(store, doc_id)
    chunk_drafts = build_chunks(elements)

    existing_seqs = {r["seq"] for r in store.knowledge.execute("SELECT seq FROM chunk WHERE doc_id=?", (doc_id,)).fetchall()}
    written = 0
    for draft in chunk_drafts:
        if draft["seq"] in existing_seqs:
            continue
        chunk_id = new_id("CHK")
        row = {
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "seq": draft["seq"],
            "text": draft["text"],
            "token_count": draft["token_count"],
            "element_first": draft["element_first"],
            "element_last": draft["element_last"],
            "page_start": draft["page_start"],
            "page_end": draft["page_end"],
            "sha256": sha256_hex(draft["text"]),
            "chunker_id": draft["chunker_id"],
            "chunker_version": draft["chunker_version"],
            "created_ts": now(),
        }
        insert(store, "chunk", row)

        anchor_draft = build_chunk_anchor(
            doc_id=doc_id,
            doc_sha256=doc["sha256"],
            elements=elements,
            chunk_id=chunk_id,
            element_first=row["element_first"],
            element_last=row["element_last"],
            page_number=row["page_start"],
        )
        insert(
            store,
            "quote_anchor",
            {
                "anchor_id": new_id("ANC"),
                **anchor_draft,
                "created_by_launch": created_by_launch,
                "created_ts": now(),
            },
        )
        written += 1
        ctx.set_checkpoint({"chunks_written": written, "total": len(chunk_drafts)})

    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "chunked"})
    _enqueue_next_stage(
        store,
        stage="embed",
        payload={"doc_id": doc_id, "created_by_launch": created_by_launch},
        job_id=f"JOB-ingest-{doc_id}-embed",
    )


@register_handler("embed")
def run_embed(ctx) -> None:
    """design Section 6 stage 6: "existing embed_backend pattern:
    model-keyed cache, chunk-sha addressing, per-batch WAL commit ...
    detached worker w/ heartbeat + pause/resume." Backend chosen via
    ``trialerror.ingest.backends.load_embed_backend`` (fake by default; real
    Qwen3-4B per ``trialerror.toml [ingest.embed]``). Per-batch commit +
    DB-state-driven skip (see module docstring) is what makes a
    kill-mid-batch resume land on byte-identical final ``emb`` rows."""
    payload = ctx.payload
    doc_id = payload["doc_id"]
    created_by_launch = payload["created_by_launch"]
    store = ctx.store
    config = _load_config(store)
    embed_cfg = config.get("ingest", {}).get("embed", {})
    backend = load_embed_backend(embed_cfg)
    model_key = backend.model_key
    dims = backend.dims
    batch_size = int(embed_cfg.get("batch_size", 8))

    chunks = [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT chunk_id, text, sha256 FROM chunk WHERE doc_id = ? ORDER BY seq", (doc_id,)
        ).fetchall()
    ]

    def _is_cached(sha256: str) -> bool:
        return (
            store.knowledge.execute(
                "SELECT 1 FROM emb WHERE chunk_sha256 = ? AND model_key = ?", (sha256, model_key)
            ).fetchone()
            is not None
        )

    pending = [c for c in chunks if not _is_cached(c["sha256"])]

    if is_offload_config(embed_cfg):
        # Lane L0-C (design D5 / v3 delta N2): the whole document's chunk
        # list travels to the DEV GPU as one payload -- batching is the
        # worker's business, since the backend there embeds in batches of
        # eight and this side has no model at all. Nothing to offload when
        # every chunk is already cached (a resumed run), which is why this
        # is guarded rather than unconditional.
        if pending:
            from trialerror.offload.stage import offload_embed_vectors

            vectors_by_chunk = offload_embed_vectors(
                ctx,
                doc_id=doc_id,
                chunks=chunks,
                embed_cfg=embed_cfg,
                model_key=model_key,
                dims=dims,
            )
            done = 0
            for c in chunks:
                if _is_cached(c["sha256"]):
                    continue
                insert(
                    store,
                    "emb",
                    {
                        "chunk_sha256": c["sha256"],
                        "model_key": model_key,
                        "dims": dims,
                        "vector": serialize_vector_fallback(list(vectors_by_chunk[c["chunk_id"]])),
                        "created_ts": now(),
                    },
                )
                done += 1
                ctx.set_checkpoint(
                    {"embedded": done, "total_pending": len(pending), "model_key": model_key}
                )
    else:
        done = 0
        for i in range(0, len(pending), batch_size):
            batch = pending[i : i + batch_size]
            vectors = backend.embed_batch([c["text"] for c in batch], kind="document")
            for c, vector in zip(batch, vectors):
                if _is_cached(c["sha256"]):  # a resumed attempt may have already committed this one
                    continue
                insert(
                    store,
                    "emb",
                    {
                        "chunk_sha256": c["sha256"],
                        "model_key": model_key,
                        "dims": dims,
                        "vector": serialize_vector_fallback(list(vector)),
                        "created_ts": now(),
                    },
                )
            done += len(batch)
            ctx.set_checkpoint({"embedded": done, "total_pending": len(pending), "model_key": model_key})

    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "embedded"})
    _enqueue_next_stage(
        store,
        stage="index",
        payload={"doc_id": doc_id, "created_by_launch": created_by_launch, "model_key": model_key},
        job_id=f"JOB-ingest-{doc_id}-index",
    )


@register_handler("index")
def run_index(ctx) -> None:
    """design Section 6 stage 7: "FTS5 + sqlite-vec | rebuildable from
    chunks+emb (indexes are cache, never truth)." Populates ``chunk_fts``,
    the program's tantivy full-text index (C-0080 -- see
    :func:`trialerror.retrieve.lexical.maintain_index`), and the active
    model's ``vec_chunks__<model_key>`` table from already-written
    ``chunk``/``emb`` rows -- reads only, no embedding calls, so this stage
    never needs the GPU even with the real embed backend configured
    upstream.

    ``chunk_fts`` is maintained UNCONDITIONALLY, even on a program serving
    its searches out of tantivy: FTS5 is the fallback backend
    (``trialerror.retrieve.lexical`` rules 3/4), and a fallback that has been
    allowed to rot is not a fallback. The tantivy write is the LAST thing
    this handler does, after the SQLite writes have committed, so a crash
    between the two leaves the source of truth intact and only the derived
    index behind -- exactly the direction of skew ``doctor``'s
    ``fulltext_index_stale`` check and ``trialerror ingest reindex-fulltext``
    exist to repair."""
    payload = ctx.payload
    doc_id = payload["doc_id"]
    store = ctx.store
    model_key = payload.get("model_key")
    if model_key is None:
        config = _load_config(store)
        backend = load_embed_backend(config.get("ingest", {}).get("embed", {}))
        model_key = backend.model_key
        dims = backend.dims
    else:
        dims_row = store.knowledge.execute("SELECT dims FROM emb WHERE model_key = ? LIMIT 1", (model_key,)).fetchone()
        dims = dims_row["dims"] if dims_row is not None else 0

    backend_kind = ensure_vec_table(store.knowledge, model_key, dims) if dims else VecBackend.FALLBACK
    table = vec_table_name(model_key)

    chunks = [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT chunk_id, text, sha256 FROM chunk WHERE doc_id = ? ORDER BY seq", (doc_id,)
        ).fetchall()
    ]
    indexed = 0
    for c in chunks:
        fts_hit = store.knowledge.execute("SELECT 1 FROM chunk_fts WHERE chunk_id = ?", (c["chunk_id"],)).fetchone()
        if fts_hit is None:
            with store.knowledge:
                store.knowledge.execute(
                    "INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", (c["chunk_id"], c["text"])
                )

        emb_row = store.knowledge.execute(
            "SELECT vector, dims FROM emb WHERE chunk_sha256 = ? AND model_key = ?", (c["sha256"], model_key)
        ).fetchone()
        if emb_row is None:
            continue  # embedding_missing -- doctor flags this; index just skips it for now

        vec_hit = store.knowledge.execute(f"SELECT 1 FROM {table} WHERE chunk_id = ?", (c["chunk_id"],)).fetchone()
        if vec_hit is None:
            with store.knowledge:
                if backend_kind == VecBackend.SQLITE_VEC:
                    store.knowledge.execute(
                        f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (c["chunk_id"], emb_row["vector"])
                    )
                else:
                    store.knowledge.execute(
                        f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)",
                        (c["chunk_id"], model_key, emb_row["dims"], emb_row["vector"]),
                    )
        indexed += 1
        ctx.set_checkpoint({"indexed": indexed, "total": len(chunks), "model_key": model_key})

    # tantivy full-text index (C-0080). Idempotent and self-healing: it
    # appends only what the index doesn't already hold, and builds the
    # whole index once if this program has never had one. A no-op when
    # tantivy-py is absent or [retrieve] fulltext_backend = "fts5".
    fulltext = lexical.maintain_index(store, [(c["chunk_id"], c["text"]) for c in chunks])
    ctx.set_checkpoint(
        {"indexed": indexed, "total": len(chunks), "model_key": model_key, "fulltext_index": fulltext}
    )

    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "indexed"})


@register_handler("extract")
def run_extract(ctx) -> None:
    """design Section 6 stage 8 / Section 11 v1 deliverable: "full
    entity/relation extraction + merge review + graph retrieval tier."
    Deliberately NOT auto-chained from ``index`` (opt-in only, unchanged
    from v0) -- a caller enqueues ``kind="extract"`` explicitly, same as
    the v0 stub always required.

    **No LLM-calling infrastructure exists in this offline jobs/CLI layer**
    (design Section 5.3: "one-shot orchestration lives [in skills], not in
    servers") -- unchanged by this v1 upgrade. What changes: this handler
    now does REAL work when the caller supplies ``payload["judgments_path"]``
    -- a JSON file, already written to disk by an agent that ran the real
    per-chunk extraction judgment OUT-OF-BAND (disk-to-disk, design Section
    6 preamble: "page text never transits the orchestrator's context;
    agents get ids + stats back", C-0007) -- shaped
    ``{"<chunk_id>": {"entities": [...], "relations": [...], "claims":
    [...]}}`` (:func:`trialerror.ingest.extract.build_extraction_judgment_envelope`'s
    own docstring names the exact per-chunk shape). This handler reads that
    file, builds a plain dict-lookup ``judge`` callable from it (the exact
    ``trialerror.cli.verify._judge_from_table`` pattern), and calls
    :func:`trialerror.ingest.extract.run_extract_document` -- checkpointing
    (``ctx.set_checkpoint``) after every chunk, so a kill-mid-document
    resume skips whatever chunks already have their
    ``kg_extract_chunk_processed`` event (restart-safety, same convention
    every other handler in this module documents).

    Omitting ``judgments_path`` preserves the ORIGINAL v0 stub behavior
    exactly (schema-ready settle, zero claims/entities/relations queued) --
    a caller that just wants to prove the queue wiring works, or a job
    enqueued before an agent has produced judgments yet, still settles
    cleanly rather than failing."""
    payload = ctx.payload
    judgments_path = payload.get("judgments_path")
    if not judgments_path:
        ctx.set_checkpoint({"claims_extracted": 0, "note": "v0 stub -- no judgments_path given, see docstring"})
        return

    from trialerror.ingest.extract import run_extract_document

    doc_id = payload["doc_id"]
    created_by_launch = payload["created_by_launch"]
    store = ctx.store

    judgments_file = Path(judgments_path)
    if not judgments_file.is_file():
        raise RuntimeError(f"extract: judgments_path {judgments_path!r} does not exist")
    judgments = json.loads(judgments_file.read_text(encoding="utf-8"))

    def judge(envelope: dict[str, Any]) -> Any:
        chunk_id = envelope["chunk_id"]
        if chunk_id not in judgments:
            raise RuntimeError(f"extract: no judgment supplied for chunk_id={chunk_id!r} in {judgments_path}")
        return judgments[chunk_id]

    def on_chunk(totals: dict[str, Any]) -> None:
        ctx.set_checkpoint(totals)

    result = run_extract_document(store, doc_id, judge=judge, created_by_launch=created_by_launch, on_chunk=on_chunk)
    ctx.set_checkpoint(
        {
            "chunks_processed": result["chunks_processed"],
            "chunks_skipped": result["chunks_skipped"],
            "entities_queued": result["entities_queued"],
            "relations_queued": result["relations_queued"],
            "claims_queued": result["claims_queued"],
            "done": True,
        }
    )
