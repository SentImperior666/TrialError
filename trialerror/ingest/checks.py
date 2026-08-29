"""M7's doctor checks. Design Section 4.1: "`trialerror ingest doctor` reports
chunker_outdated / chunker_missing / embedding_missing / embedding_stale /
anchors_dangling counts; `rechunk`/`re-embed` fix the first four as
resumable jobs."

``anchors_dangling`` itself is SPLIT across two modules by design (M1's own
``trialerror/stores/checks.py`` docstring, verbatim): "this check reports what
it can from schema alone [doc_sha256 mismatch] and is designed to be
extended, not replaced, once M7 lands." Per this build's lane isolation
(``trialerror/stores/checks.py`` is M1-owned, out of lane -- see build report
deviations), the QUOTE_SHA256 SPOT-RESOLVE half lands here under its own
check name (:func:`check_anchor_spot_resolve`) rather than editing that
file; ``trialerror ingest doctor`` (the CLI subcommand) aggregates both halves
into one reported "anchors_dangling" total without either module touching
the other's file. Both checks also register individually with the generic
``trialerror doctor`` sweep (auto-discovery, no shared file touched either way).
"""

from __future__ import annotations

import json
from typing import Any

from trialerror.ingest.anchors import spot_resolve
from trialerror.ingest.chunker import CHUNKER_ID, CHUNKER_VERSION
from trialerror.ingest.extract import EXTRACT_REGISTER_KEY
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_chunker_missing",
    "check_chunker_outdated",
    "check_embedding_missing",
    "check_embedding_stale",
    "check_anchor_spot_resolve",
    "check_extract_pending_backlog",
    "check_entity_dupes_suspected",
]


def _knowledge_path(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    return paths.knowledge_db_path(ctx.program_root)


def _skip(name: str) -> CheckResult:
    return CheckResult(
        name=name,
        category="ingest",
        status="skip",
        message="knowledge.db not found (program_root not configured, or program not yet initialized)",
    )


def _active_model_key(ctx: DoctorContext) -> str:
    """Best-effort active embed model_key from ``trialerror.toml``, falling
    back to the fake backend's default naming (matches
    ``trialerror.ingest.backends.load_embed_backend``'s own default) when no
    config is available -- doctor must never require a live config to run,
    only report less precisely without one."""
    if ctx.program_root is not None:
        from trialerror.util.config import CONFIG_FILENAME, load_config

        cfg_path = ctx.program_root / CONFIG_FILENAME
        if cfg_path.is_file():
            try:
                raw = load_config(cfg_path).raw
                embed_cfg = raw.get("ingest", {}).get("embed", {})
                backend_name = embed_cfg.get("backend", "fake")
                if backend_name != "fake":
                    return backend_name
                dims = embed_cfg.get("dims", 16)
                return f"fake-{dims}"
            except Exception:
                pass
    return "fake-16"


@register_check("chunker_missing", category="ingest")
def check_chunker_missing(ctx: DoctorContext) -> CheckResult:
    """Documents with elements (normalize/OCR completed) but zero chunk
    rows -- ``trialerror ingest rechunk`` (really, the ``chunk`` stage's own
    idempotent enqueue) fixes these."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("chunker_missing")
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT d.doc_id FROM document d
            WHERE EXISTS (SELECT 1 FROM element e WHERE e.doc_id = d.doc_id)
              AND NOT EXISTS (SELECT 1 FROM chunk c WHERE c.doc_id = d.doc_id)
            """
        ).fetchall()
    finally:
        conn.close()
    count = len(rows)
    status = "warn" if count else "pass"
    message = f"{count} document(s) with elements but zero chunks" if count else "no documents missing chunks"
    return CheckResult(
        name="chunker_missing", category="ingest", status=status, message=message,
        details={"doc_ids": [r["doc_id"] for r in rows]},
    )


@register_check("chunker_outdated", category="ingest")
def check_chunker_outdated(ctx: DoctorContext) -> CheckResult:
    """Chunks stamped with a ``chunker_id``/``chunker_version`` other than
    the currently configured chunker (:data:`trialerror.ingest.chunker.CHUNKER_ID`/
    :data:`CHUNKER_VERSION`) -- a chunker upgrade happened since they were
    produced."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("chunker_outdated")
    conn = connect(path, read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunk WHERE chunker_id != ? OR chunker_version != ?",
            (CHUNKER_ID, CHUNKER_VERSION),
        ).fetchone()
    finally:
        conn.close()
    count = int(row["n"])
    status = "warn" if count else "pass"
    message = f"{count} chunk(s) stamped with an outdated chunker_id/version" if count else "no outdated chunks"
    return CheckResult(
        name="chunker_outdated", category="ingest", status=status, message=message,
        details={"current_chunker_id": CHUNKER_ID, "current_chunker_version": CHUNKER_VERSION, "count": count},
    )


@register_check("embedding_missing", category="ingest")
def check_embedding_missing(ctx: DoctorContext) -> CheckResult:
    """Chunks with NO ``emb`` row at all for the active model_key -- never
    embedded (or never re-embedded after a rechunk changed their sha256)."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("embedding_missing")
    model_key = _active_model_key(ctx)
    conn = connect(path, read_only=True)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM chunk c
            WHERE NOT EXISTS (SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256 AND e.model_key = ?)
            """,
            (model_key,),
        ).fetchone()
    finally:
        conn.close()
    count = int(row["n"])
    status = "warn" if count else "pass"
    message = f"{count} chunk(s) with no embedding for model_key={model_key!r}" if count else "no missing embeddings"
    return CheckResult(
        name="embedding_missing", category="ingest", status=status, message=message,
        details={"model_key": model_key, "count": count},
    )


@register_check("embedding_stale", category="ingest")
def check_embedding_stale(ctx: DoctorContext) -> CheckResult:
    """Chunks whose vector-index entry (``vec_chunks__<model_key>``) exists
    for their ``chunk_id`` but whose CURRENT ``sha256`` has no matching
    ``emb`` row -- the chunk's text changed (a rechunk landed) since it was
    last embedded/indexed; the stale vector is still sitting in the index
    until a re-embed/re-index resolves it."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("embedding_stale")
    model_key = _active_model_key(ctx)
    conn = connect(path, read_only=True)
    try:
        from trialerror.stores.vecindex import try_load_sqlite_vec, vec_table_name

        # a real sqlite-vec vec0 virtual table needs the loadable extension
        # registered on EVERY connection that queries it, not just the one
        # that created it -- this read-only doctor connection is a fresh one.
        try_load_sqlite_vec(conn)

        table = vec_table_name(model_key)
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
        ).fetchone()
        if table_exists is None:
            return CheckResult(
                name="embedding_stale", category="ingest", status="pass",
                message=f"no vector index table yet for model_key={model_key!r}",
                details={"model_key": model_key, "count": 0},
            )
        rows = conn.execute(
            f"""
            SELECT c.chunk_id FROM chunk c
            JOIN {table} v ON v.chunk_id = c.chunk_id
            WHERE NOT EXISTS (SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256 AND e.model_key = ?)
            """,
            (model_key,),
        ).fetchall()
    finally:
        conn.close()
    count = len(rows)
    status = "warn" if count else "pass"
    message = f"{count} chunk(s) with a stale vector-index entry for model_key={model_key!r}" if count else "no stale embeddings"
    return CheckResult(
        name="embedding_stale", category="ingest", status=status, message=message,
        details={"model_key": model_key, "chunk_ids": [r["chunk_id"] for r in rows]},
    )


@register_check("anchor_spot_resolve", category="ingest")
def check_anchor_spot_resolve(ctx: DoctorContext) -> CheckResult:
    """The quote_sha256 spot-resolve half of ``anchors_dangling`` (design
    Section 4.1 / M1's own ``check_anchors_dangling`` docstring: "the other
    half ... needs the stream_v1 function and normalizer outputs, which
    are M7's"). Recomputes ``stream_v1`` over each anchor's document's
    CURRENT elements and compares against the anchor's stored
    ``quote_sha256`` -- flags an anchor whose underlying chunk/element text
    changed (a rechunk or element edit) even when ``document.sha256``
    itself didn't move (the M1 doc-level check's blind spot)."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("anchor_spot_resolve")
    conn = connect(path, read_only=True)
    try:
        anchors = [dict(r) for r in conn.execute("SELECT * FROM quote_anchor").fetchall()]
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for a in anchors:
            by_doc.setdefault(a["doc_id"], []).append(a)

        offenders: list[str] = []
        for doc_id, doc_anchors in by_doc.items():
            elements = [dict(r) for r in conn.execute("SELECT * FROM element WHERE doc_id = ?", (doc_id,)).fetchall()]
            for anchor in doc_anchors:
                if not spot_resolve(elements, anchor):
                    offenders.append(anchor["anchor_id"])
    finally:
        conn.close()

    count = len(offenders)
    status = "warn" if count else "pass"
    message = f"{count} anchor(s) fail quote_sha256 spot-resolve against current elements" if count else "all anchors spot-resolve cleanly"
    return CheckResult(
        name="anchor_spot_resolve", category="ingest", status=status, message=message,
        details={"anchor_ids": offenders},
    )


@register_check("extract_pending_backlog", category="ingest")
def check_extract_pending_backlog(ctx: DoctorContext) -> CheckResult:
    """design Section 11 v1 deliverable 3 ("doctor checks:
    extract_pending_backlog, entity_dupes_suspected"). PENDING extraction
    candidates (``trialerror.ingest.extract.EXTRACT_REGISTER_KEY`` ``record``
    rows whose payload ``status == 'pending'``) waiting on an explicit
    ``trialerror extract accept``/``reject`` decision -- the merge-review
    queue's own "never silent auto-merge" contract means this count can
    only shrink via a human/agent decision, never automatically, so a
    growing backlog is a genuine standing-health signal (same warn-on-any-
    nonzero-count convention as :func:`check_chunker_missing`/
    :func:`check_embedding_missing` above)."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("extract_pending_backlog")
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT record_id, payload FROM record WHERE register_key = ?", (EXTRACT_REGISTER_KEY,)
        ).fetchall()
    finally:
        conn.close()

    pending_ids: list[str] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("status") == "pending":
            pending_ids.append(r["record_id"])

    count = len(pending_ids)
    status = "warn" if count else "pass"
    message = f"{count} extraction candidate(s) awaiting accept/reject" if count else "no pending extraction candidates"
    return CheckResult(
        name="extract_pending_backlog", category="ingest", status=status, message=message,
        details={"record_ids": pending_ids[:200], "count": count},
    )


@register_check("entity_dupes_suspected", category="ingest")
def check_entity_dupes_suspected(ctx: DoctorContext) -> CheckResult:
    """design Section 11 v1 deliverable 3's second named check. DRAFT
    ``merge_proposal`` rows -- entity candidates whose extraction-time
    exact ``(name, entity_type)`` dedup check found a suspected match
    against an already-confirmed entity (``trialerror.ingest.extract.
    _accept_entity_candidate``) but that suggestion has not yet been
    explicitly confirmed/rejected (:func:`trialerror.ingest.extract.
    accept_merge_proposal`/:func:`~trialerror.ingest.extract.reject_merge_proposal`)."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("entity_dupes_suspected")
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT prop_id, canonical_entity, members FROM merge_proposal WHERE status = 'draft'"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "warn" if count else "pass"
    message = f"{count} suspected entity duplicate(s) awaiting a merge decision" if count else "no suspected entity duplicates pending"
    return CheckResult(
        name="entity_dupes_suspected", category="ingest", status=status, message=message,
        details={"proposals": [{"prop_id": r["prop_id"], "canonical_entity": r["canonical_entity"]} for r in rows]},
    )
