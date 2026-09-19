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

from trialerror.ingest import quality
from trialerror.ingest.anchors import spot_resolve
from trialerror.ingest.chunker import CHUNKER_ID, CHUNKER_VERSION
from trialerror.ingest.extract import EXTRACT_REGISTER_KEY
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "configured_embed_model_key",
    "VECTOR_INDEX_SAMPLE_LIMIT",
    "check_chunker_missing",
    "check_chunker_outdated",
    "check_embedding_missing",
    "check_embedding_stale",
    "check_vector_index_stale",
    "check_anchor_spot_resolve",
    "check_extract_pending_backlog",
    "check_entity_dupes_suspected",
    "check_extraction_quality_suspect",
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


def _retracted_doc_ids(conn) -> set[str]:
    """Documents withdrawn via ``trialerror ingest retract``. A local import
    so this module keeps its "import nothing that imports the store writer"
    shape, and one indirection so the eventual
    ``document.status = 'retracted'`` migration lands in exactly one place
    (:func:`trialerror.ingest.retract.retracted_doc_ids`'s own note)."""
    from trialerror.ingest.retract import retracted_doc_ids

    return retracted_doc_ids(conn)


def _fake_model_key(dims: Any) -> str:
    """``FakeEmbedBackend``'s own ``model_key`` naming, mirrored: the loader
    calls ``FakeEmbedBackend(dims=config.get("dims", DEFAULT_FAKE_EMBED_DIMS))``
    and that ``__init__`` sets ``self.model_key = f"fake-{dims}"`` with the
    value VERBATIM -- no coercion. So ``dims`` absent (``None``) means the
    default, and any other value is interpolated exactly as the backend would,
    including one TOML let through that is not an integer."""
    from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS

    if dims is None:
        return f"fake-{DEFAULT_FAKE_EMBED_DIMS}"
    return f"fake-{dims}"


def _embed_model_key(embed_cfg: dict[str, Any]) -> str:
    """The ``model_key`` that ``[ingest.embed]`` table's backend actually
    STAMPS ON ITS ``emb`` ROWS -- mirroring
    :func:`trialerror.ingest.backends.load_embed_backend` branch for branch,
    because that is the function whose choice the rows on disk record.

    Branch for branch, in the loader's own order -- and note where an explicit
    ``model_key`` is read and where it is IGNORED, because the loader ignores
    it on two of these three branches:

    1. ``backend = "fake"`` (the default when nothing is configured) ->
       ``fake-<dims>`` (:func:`_fake_model_key`). ``FakeEmbedBackend`` stamps
       its own key from ``dims`` and never looks at ``model_key``, so a stale
       ``model_key`` line left behind by an earlier backend must NOT win here.
    2. ``backend = "offload"`` -> the configured ``model_key``.
       :class:`trialerror.offload.marker.OffloadMarker` refuses to construct
       without one (``emb``'s PK is ``(chunk_sha256, model_key)``, so a
       defaulted key would open a parallel key space the DEV worker never
       writes into), which is why this is the ONE branch where the config's
       value is the answer. With no ``model_key`` the config cannot load at
       all, so there is no row naming to mirror -- fall back to the fake
       default rather than invent ``"offload"`` as a key.
    3. any other backend name -> that name, verbatim: ``load_embed_backend``
       passes ``model_key=backend_name`` into :class:`RealQwenEmbedBackend`
       and ignores the config's ``model_key`` entirely, so
       ``backend = "qwen3-4b"`` really does write ``model_key='qwen3-4b'``
       rows whatever else the table says.

    The defect this replaces (found by the sandbox-audit lane, 2026-09-06):
    the old code returned the BACKEND NAME for every non-fake backend, so a
    program running ``backend = "offload"`` + ``model_key = "qwen3-4b"``
    made ``embedding_missing``/``embedding_stale`` hunt for
    ``model_key='offload'`` rows that nothing ever writes -- reporting the
    WHOLE corpus as unembedded on a corpus that was fully embedded. The
    explicit-model_key-first ordering that first replaced it re-created the
    same defect with the arrow reversed (a program moved back to ``fake``
    without deleting its old ``model_key`` line would be audited against a key
    nothing on disk carries), which is why the branches -- not the keys -- are
    what this function follows.
    """
    backend_name = embed_cfg.get("backend", "fake")
    if backend_name == "fake":
        return _fake_model_key(embed_cfg.get("dims", None))
    from trialerror.offload.marker import OFFLOAD_BACKEND_NAME

    if backend_name == OFFLOAD_BACKEND_NAME:
        explicit = embed_cfg.get("model_key")
        return str(explicit) if explicit else _fake_model_key(None)
    return str(backend_name)


def configured_embed_model_key(config: dict[str, Any] | None) -> str:
    """The embed ``model_key`` a program with this RAW CONFIG writes --
    :func:`_embed_model_key` applied to ``config["ingest"]["embed"]``, with
    every absent level treated as "nothing configured" (so ``None`` means
    the fake backend's default naming, exactly as the loader would).

    Public because it is not only the doctor's question. ``trialerror ingest
    purge-embeddings`` has to refuse the ACTIVE key, and "which key is
    active" must have one answer in this codebase rather than two that drift
    -- the whole defect history in :func:`_embed_model_key`'s docstring is
    what two answers cost. :func:`_active_model_key` is the same resolution
    reached from a :class:`DoctorContext` (it loads the toml itself); this
    one is for a caller that already holds the parsed config."""
    ingest_cfg = (config or {}).get("ingest") or {}
    embed_cfg = ingest_cfg.get("embed") if isinstance(ingest_cfg, dict) else {}
    return _embed_model_key(embed_cfg if isinstance(embed_cfg, dict) else {})


def _program_config(ctx: DoctorContext) -> dict[str, Any]:
    """The program's ``trialerror.toml`` as a raw dict, or ``{}``.

    Doctor must never REQUIRE a live config to run -- only report less
    precisely without one -- so every failure mode (no program root, no
    file, an unparseable file) reads the same as "nothing configured".
    That is the opposite of ``trialerror.ingest.handlers._load_config``'s
    fail-closed contract, deliberately: a handler that mis-reads config
    can write a wrong vector into the corpus, while a check that mis-reads
    it can only report against a default.
    """
    if ctx.program_root is None:
        return {}
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = ctx.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        raw = load_config(cfg_path).raw
    except Exception:  # noqa: BLE001 - see the docstring
        return {}
    return raw if isinstance(raw, dict) else {}


def _active_model_key(ctx: DoctorContext) -> str:
    """Best-effort active embed model_key from ``trialerror.toml`` (resolved
    by :func:`_embed_model_key`), falling back to the fake backend's default
    naming when no config is available -- doctor must never require a live
    config to run, only report less precisely without one."""
    ingest_cfg = _program_config(ctx).get("ingest")
    embed_cfg = ingest_cfg.get("embed") if isinstance(ingest_cfg, dict) else None
    if not isinstance(embed_cfg, dict):
        return _fake_model_key(None)
    return _embed_model_key(embed_cfg)


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
        # A fully retracted document has neither elements nor chunks, so it
        # cannot reach the query above -- but a PARTIALLY retracted one (an
        # older retraction, or a crash between the two deletes) could, and
        # "rechunk this" is the worst possible suggestion for a document
        # the operator deliberately withdrew. Excluded explicitly rather
        # than left to the happy path.
        retracted = _retracted_doc_ids(conn)
    finally:
        conn.close()
    doc_ids = [r["doc_id"] for r in rows if r["doc_id"] not in retracted]
    count = len(doc_ids)
    status = "warn" if count else "pass"
    message = f"{count} document(s) with elements but zero chunks" if count else "no documents missing chunks"
    return CheckResult(
        name="chunker_missing", category="ingest", status=status, message=message,
        details={"doc_ids": doc_ids},
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
        rows = conn.execute(
            "SELECT doc_id, COUNT(*) AS n FROM chunk WHERE chunker_id != ? OR chunker_version != ? "
            "GROUP BY doc_id",
            (CHUNKER_ID, CHUNKER_VERSION),
        ).fetchall()
        # Same reason as chunker_missing: a retracted document's chunks are
        # already gone, so this only bites a partially-retracted one -- but
        # "your chunker is out of date on N chunks" pointing at a document
        # nobody will ever rechunk is a count an operator has to chase down
        # to dismiss.
        retracted = _retracted_doc_ids(conn)
    finally:
        conn.close()
    count = sum(int(r["n"]) for r in rows if r["doc_id"] not in retracted)
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


#: How many missing ``chunk_id``s one key's ``details`` carries. A bounded
#: sample, not the list: the live finding this check was written for is
#: ~13,900 chunks on one key, and a doctor result is JSON an operator reads.
VECTOR_INDEX_SAMPLE_LIMIT = 20


def _registry_model_keys(conn) -> list[str] | None:
    """Every ``model_key`` in ``vec_index_registry``, or ``None`` when this
    program has no registry table at all (nothing has ever been indexed)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'vec_index_registry'"
    ).fetchone()
    if exists is None:
        return None
    return [r[0] for r in conn.execute("SELECT model_key FROM vec_index_registry ORDER BY model_key").fetchall()]


def _vector_index_gap(conn, model_key: str) -> dict[str, Any]:
    """One key's row of the comparison: how many ``emb`` rows it has, how
    many entries its vector table holds, and -- the number that decides the
    status -- how many CHUNKS have an embedding under that key but no entry
    in its index.

    Why the chunk-level count and not just the two totals: ``emb`` is
    hash-addressed (one row per ``chunk_sha256``) and the vector table is
    chunk-addressed (one entry per ``chunk_id``), so the totals differ
    legitimately whenever two chunks share text -- on the program this check
    was written for, the placeholder key's index held 14,000 entries for
    13,950 ``emb`` rows and was COMPLETE. Subtracting one total from the
    other would have called that a 50-row surplus and a real 13,915-chunk
    hole merely "fewer rows". The join answers the question retrieval
    actually asks: can this chunk be found by vector search?
    """
    from trialerror.stores.vecindex import vec_table_name

    # Derived, never read out of the registry row: the table name becomes an
    # SQL identifier below, and every other reader of these tables
    # (``retrieve.vecsearch``, ``ingest.purge``) derives it the same way.
    table = vec_table_name(model_key)
    emb_rows = int(
        conn.execute("SELECT COUNT(*) AS n FROM emb WHERE model_key = ?", (model_key,)).fetchone()["n"]
    )
    out: dict[str, Any] = {
        "model_key": model_key,
        "table": table,
        "emb_rows": emb_rows,
        "vec_entries": None,
        "chunks_with_embedding": 0,
        "chunks_missing_entry": 0,
        "missing_sample": [],
        "readable": True,
    }
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    embeddable = conn.execute(
        """
        SELECT COUNT(*) AS n FROM chunk c
        WHERE EXISTS (SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256 AND e.model_key = ?)
        """,
        (model_key,),
    ).fetchone()["n"]
    out["chunks_with_embedding"] = int(embeddable)
    if table_exists is None:
        # Registered but never created (or dropped since). Every embedded
        # chunk is unreachable by vector search, which is the same finding
        # as an empty table and is reported as such rather than skipped.
        out["vec_entries"] = 0
        out["chunks_missing_entry"] = int(embeddable)
        out["missing_sample"] = [
            r["chunk_id"]
            for r in conn.execute(
                """
                SELECT c.chunk_id FROM chunk c
                WHERE EXISTS (SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256 AND e.model_key = ?)
                ORDER BY c.chunk_id LIMIT ?
                """,
                (model_key, VECTOR_INDEX_SAMPLE_LIMIT),
            ).fetchall()
        ]
        return out
    try:
        out["vec_entries"] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        rows = conn.execute(
            f"""
            SELECT c.chunk_id FROM chunk c
            WHERE EXISTS (SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256 AND e.model_key = ?)
              AND NOT EXISTS (SELECT 1 FROM {table} v WHERE v.chunk_id = c.chunk_id)
            ORDER BY c.chunk_id
            """,
            (model_key,),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - an unreadable index is a reported state, not a crash
        # A ``vec0`` virtual table on a connection whose sqlite-vec extension
        # would not load. Reported as unreadable (a WARN), never as a hole:
        # this check has no measurement in that case, and inventing one
        # would make a sqlite-vec program's doctor lie in either direction.
        out["readable"] = False
        out["error"] = str(exc)
        return out
    out["chunks_missing_entry"] = len(rows)
    out["missing_sample"] = [r["chunk_id"] for r in rows[:VECTOR_INDEX_SAMPLE_LIMIT]]
    return out


@register_check("vector_index_stale", category="ingest")
def check_vector_index_stale(ctx: DoctorContext) -> CheckResult:
    """Chunks with an ``emb`` row for a model key but NO entry in that key's
    ``vec_chunks__<model_key>`` table -- embedded, and invisible to semantic
    retrieval anyway.

    The missing half of the pair. :func:`check_embedding_stale` looks at the
    index entries that OUTLIVED their embedding; nothing looked at the
    embeddings that never reached the index, and that is the direction the
    pipeline actually skews: ``emb`` rows are written by the embed stage and
    the index entries by a separate ``index`` job that can be skipped,
    deferred, or (the live defect -- see
    :func:`trialerror.ingest.handlers.index_job_id`) never enqueued at all.
    On the program this was written for, ``embedding_missing`` reported 35
    chunks and PASSED on 16,088 embedding rows while semantic retrieval was
    answering from 2,173 vectors, and no check said a word.

    FAIL for the key ``[ingest.embed]`` configures -- that is the live search
    surface, and a hole in it is a corpus-correctness defect, not
    housekeeping. WARN for any other registered key (a superseded one's index
    is stale by definition; ``ingest purge-embeddings`` is what retires it).
    WARN, with the reason, for a key whose table cannot be read. Repair:
    ``trialerror ingest reindex-vectors --model-key K --launch-id L``.
    """
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("vector_index_stale")
    active = _active_model_key(ctx)
    conn = connect(path, read_only=True)
    try:
        from trialerror.stores.vecindex import try_load_sqlite_vec

        # Same reason as embedding_stale: a vec0 virtual table is only
        # queryable on a connection that has the extension loaded, and this
        # read-only doctor connection is a fresh one.
        try_load_sqlite_vec(conn)

        keys = _registry_model_keys(conn)
        if keys is None:
            return CheckResult(
                name="vector_index_stale", category="ingest", status="pass",
                message="no vector index registry yet (nothing has been indexed)",
                details={"active_model_key": active, "keys": [], "count": 0},
            )
        gaps = [_vector_index_gap(conn, key) for key in keys]
    finally:
        conn.close()

    total_missing = sum(g["chunks_missing_entry"] for g in gaps)
    unreadable = [g for g in gaps if not g["readable"]]
    active_hole = next(
        (g for g in gaps if g["model_key"] == active and g["chunks_missing_entry"]), None
    )
    if active_hole is not None:
        status = "fail"
    elif total_missing or unreadable:
        status = "warn"
    else:
        status = "pass"

    parts = []
    for g in gaps:
        tag = " (active)" if g["model_key"] == active else ""
        if not g["readable"]:
            parts.append(f"{g['model_key']}{tag}: index unreadable ({g.get('error')})")
        elif g["chunks_missing_entry"]:
            parts.append(
                f"{g['model_key']}{tag}: {g['vec_entries']} index entr(ies) for {g['emb_rows']} "
                f"embedding row(s) -- {g['chunks_missing_entry']} of {g['chunks_with_embedding']} "
                "embedded chunk(s) not in the index"
            )
        else:
            parts.append(
                f"{g['model_key']}{tag}: {g['vec_entries']} index entr(ies) for {g['emb_rows']} "
                "embedding row(s), complete"
            )
    message = "; ".join(parts) if parts else "no model key has a vector index registered"
    return CheckResult(
        name="vector_index_stale", category="ingest", status=status, message=message,
        details={"active_model_key": active, "keys": gaps, "count": total_missing},
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


@register_check("extraction_quality_suspect", category="ingest")
def check_extraction_quality_suspect(ctx: DoctorContext) -> CheckResult:
    """Documents whose extracted text looks unusable, by the four measures
    in :mod:`trialerror.ingest.quality` (glued-token rate, characters that
    are not text, sentence-terminator density, per-page character
    variation).

    **WARN, never FAIL.** A measure is a signal about text, not a verdict
    about a document: a glossary is legitimately terminator-poor and a
    table-heavy appendix is legitimately glued-token-rich. The corpus is
    intact either way, every other stage already succeeded, and the only
    correct next move is a human looking at the worst ones
    (``trialerror ingest quality --doc-id ...``). A check that could fail a
    doctor run on a stylistic outlier is a check an operator learns to
    silence.

    **It SAMPLES, and the sample is seeded.** A full corpus measurement on
    every ``trialerror doctor`` run would make the cheapest health command
    in the system proportional to the corpus -- on a five-figure corpus
    that is the difference between a second and a coffee break, paid every
    time anyone asks about anything. ``[ingest.quality] sample`` documents
    (default :data:`trialerror.ingest.quality.DEFAULT_SAMPLE`) are drawn
    with ``[ingest.quality] seed``, so two runs against an unchanged corpus
    report the same documents instead of looking like a corpus that
    changes every time it is read. ``trialerror ingest quality --all`` is
    the exhaustive pass, run when the operator asks for it.

    **A size floor, because three of the four measures are rates.**
    Documents shorter than ``[ingest.quality] min_tokens`` (default
    :data:`trialerror.ingest.quality.DEFAULT_MIN_TOKENS`) are measured and
    reported -- ``details['below_min_tokens']`` counts them and the message
    names them -- but are never counted suspect. The first day this check ran
    live it flagged 8-to-31-token notes purely by denominator: one long URL
    in a twelve-token note is a glued-token rate of 0.08, and a note with no
    full stop is a terminator density of 0.0. Neither says anything about
    extraction, and a WARN made of them is the WARN an operator learns to
    silence.

    ``skip`` when there are no documents at all: a corpus with nothing in
    it has no extraction quality, which is a different statement from
    "clean". A document whose measurement itself fails is reported in
    ``details['unreadable']`` rather than raising -- ``run_checks`` turns a
    raise into a FAIL, which this check has just promised never to be.
    """
    name = "extraction_quality_suspect"
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip(name)

    thresholds = quality.thresholds_from_config(_program_config(ctx))
    sample_size = int(thresholds["sample"])
    seed = int(thresholds["seed"])
    worst_n = int(thresholds["worst_n"])

    conn = connect(path, read_only=True)
    try:
        doc_ids = quality.corpus_doc_ids(conn)
        corpus_size = len(doc_ids)
        sampled = quality.sample_doc_ids(doc_ids, sample_size, seed)
        rows: list[dict[str, Any]] = []
        unreadable: list[dict[str, str]] = []
        for doc_id in sampled:
            try:
                rows.append(quality.measure_document(conn, doc_id))
            except Exception as exc:  # noqa: BLE001 - a WARN-only check never raises
                unreadable.append({"doc_id": doc_id, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()

    if corpus_size == 0:
        return CheckResult(
            name=name, category="ingest", status="skip",
            message="no documents in this corpus yet (nothing to measure)",
            details={"corpus_documents": 0},
        )

    annotated = quality.worst_first(rows, thresholds=thresholds)
    ranked = annotated[: max(0, worst_n)]
    suspects = [r for r in annotated if r["suspect"]]
    measurable = [r for r in rows if r.get("measurable")]
    # Measured, reported, never counted suspect: a document below
    # `[ingest.quality] min_tokens` has no denominator worth judging (the
    # first day's live run flagged 8-to-31-token notes by arithmetic alone).
    # Counted separately so the operator can see WHY a corpus of notes reads
    # clean, rather than having to infer it from a suspect count of zero.
    small = [r for r in annotated if r.get("below_min_tokens")]
    min_tokens = int(thresholds["min_tokens"])

    details = {
        "corpus_documents": corpus_size,
        "sampled": len(rows),
        "sample_size": sample_size,
        "sample_seed": seed,
        "measurable": len(measurable),
        "suspect_count": len(suspects),
        "below_min_tokens": len(small),
        "min_tokens": min_tokens,
        "thresholds": thresholds,
        "worst": ranked,
        "unreadable": unreadable,
    }

    scope = "the whole corpus" if len(sampled) >= corpus_size else f"a seeded sample of {len(sampled)} of {corpus_size}"
    floor_note = (
        f" {len(small)} document(s) are shorter than [ingest.quality] min_tokens ({min_tokens}): "
        "measured, reported here, never counted suspect."
        if small
        else ""
    )
    if suspects:
        worst = suspects[0]
        return CheckResult(
            name=name, category="ingest", status="warn",
            message=(
                f"{len(suspects)} of {len(measurable)} measurable document(s) in {scope} look badly "
                f"extracted -- worst: {worst['doc_id']} ({'; '.join(worst['reasons'])}). "
                "Read the numbers with `trialerror ingest quality --all --worst 10`; nothing is broken, "
                "the text may be unusable." + floor_note
            ),
            details=details,
        )
    if not measurable:
        return CheckResult(
            name=name, category="ingest", status="pass",
            message=(
                f"no document in {scope} has extracted text yet (nothing measurable -- the pipeline's "
                "own stage chain reports where they are)"
            ),
            details=details,
        )
    return CheckResult(
        name=name, category="ingest", status="pass",
        message=(
            f"{len(measurable)} measurable document(s) in {scope} measure clean on all four "
            f"extraction checks.{floor_note}"
        ),
        details=details,
    )
