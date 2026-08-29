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
from trialerror.ingest.normalizers import NORMALIZER_ID, NORMALIZER_VERSION, normalize_direct
from trialerror.ingest.sanitizer import SANITIZER_VERSION, sanitize
from trialerror.ingest.stream import stream_v1
from trialerror.jobs import ledger
from trialerror.jobs.registry import register_handler
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import get, insert, update
from trialerror.util.atomic import atomic_write_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["run_normalize", "run_ocr", "run_chunk", "run_embed", "run_index", "run_extract"]


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
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = store.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


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
) -> None:
    """Shared tail of ``normalize``/``ocr``: sanitize + insert every
    element draft, build ``stream_v1`` over them, stamp the document's
    ``sha256`` (design Section 4.1: "sha256 (normalized text)") and
    ``status = 'normalized'``, archive the stream text to disk, then
    enqueue the ``chunk`` stage."""
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
        "normalizer_id": NORMALIZER_ID,
        "normalizer_version": NORMALIZER_VERSION,
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


@register_handler("normalize")
def run_normalize(ctx) -> None:
    """design Section 6 stage 3 for a directly-normalizable ``media_type``
    (pdf-text/html/epub/md)."""
    payload = ctx.payload
    doc_id = payload["doc_id"]
    store = ctx.store
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise RuntimeError(f"normalize: no such document {doc_id!r}")
    raw_path = _resolve_raw_path(store, doc)
    drafts = normalize_direct(doc["media_type"], raw_path)
    _finish_normalize_stage(ctx, doc, drafts, ocr_backend=None, ocr_version=None)


@register_handler("ocr")
def run_ocr(ctx) -> None:
    """design Section 6 stage 4: "marker GPU (existing); detached job;
    GPU-only (standing law); batch-chunked; page anchors preserved" --
    routed formats (pdf-scan/image), backend chosen via
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
    backend = load_ocr_backend(config.get("ingest", {}).get("ocr", {}))

    work_dir = store.program_root / "jobs_work" / doc_id / "ocr"
    result = backend.run(input_path=raw_path, work_dir=work_dir)
    ctx.set_checkpoint({"ocr_pages": len(result.pages)})

    drafts = [
        {
            "seq": i,
            "type": "NarrativeText",
            "text": page.text,
            "page_number": page.page_number,
            "detection_origin": f"ocr:{backend.name}",
        }
        for i, page in enumerate(result.pages)
    ]
    _finish_normalize_stage(ctx, doc, drafts, ocr_backend=result.ocr_backend, ocr_version=result.ocr_version)


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
    chunks+emb (indexes are cache, never truth)." Populates ``chunk_fts``
    and the active model's ``vec_chunks__<model_key>`` table from
    already-written ``chunk``/``emb`` rows -- reads only, no embedding
    calls, so this stage never needs the GPU even with the real embed
    backend configured upstream."""
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
