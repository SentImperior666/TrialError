"""M8's doctor checks. Design Section 12 (M8 row) build brief: "doctor
checks in trialerror/retrieval/checks.py (e.g. latency-fixture check,
fence-integrity check)".

Both checks below stay read-only-connection-only (``trialerror.stores.connection.
connect(path, read_only=True)`` against ``knowledge.db`` alone) rather than
opening a full four-DB :class:`~trialerror.stores.store.Store` (platform.db
included) -- the same discipline every other module's ``checks.py`` in this
codebase already follows (``trialerror.ingest.checks``, ``trialerror.jobs.checks``,
...). :mod:`trialerror.retrieve.lexical`/:mod:`trialerror.retrieve.vecsearch`'s
public functions only ever touch a ``store.knowledge`` attribute (plus, since
the fix pass for finding LD-03, ``store.program_root`` -- what lets the
lexical tier's backend resolution find this program's tantivy index), so
:class:`_KnowledgeOnlyStore` duck-types exactly those two attributes rather
than constructing a real ``Store`` with three connections nothing here uses.

Note the M8 ACCEPTANCE criterion itself ("15k-chunk fixture p95 latency
<500ms, fixture vectors synthetic") is a purpose-built test-suite fixture
(``tests/test_m8_acceptance.py``), not this doctor check: a live program's
real corpus could be far smaller (or, someday, far larger) than 15k chunks,
so :func:`check_retrieval_latency` reports a MEASUREMENT against whatever
corpus actually exists, warning only past a generous threshold, rather than
gating on the acceptance fixture's specific bound.
"""

from __future__ import annotations

import time

from pathlib import Path

from trialerror.ingest.backends import (
    QUERY_EMBED_TABLE,
    embed_backend_runnable,
    embed_backend_runtime_details,
    load_query_embed_backend,
    query_embed_backend_name,
)
from trialerror.retrieve import tantivysearch, vecmatrix
from trialerror.retrieve.engine import QUERY_EMBED_DOCTOR_CHECK
from trialerror.retrieve.fence import excerpt_words
from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT
from trialerror.retrieve.lexical import configured_backend_name, lexical_search
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_fence_integrity",
    "check_retrieval_latency",
    "check_fulltext_index_stale",
    "check_query_embed_backend_runnable",
    "check_vecmatrix_stale",
]

#: How many commercial_restricted chunks :func:`check_fence_integrity`
#: samples per run -- bounded so doctor stays fast even against a large
#: corpus (this is a regression sentinel over real data, not an exhaustive
#: audit; :func:`trialerror.retrieve.fence.excerpt_words` is correct by
#: construction for every input, so a single sample failing would indicate
#: a code regression, not a corpus-specific edge case worth exhaustively
#: hunting for).
_FENCE_SAMPLE_LIMIT = 200

#: Generous warn threshold for the live-corpus latency probe -- NOT the
#: M8 acceptance bound (see module docstring); this is a standing-health
#: signal, not a gate.
_LATENCY_WARN_MS = 2000.0


class _KnowledgeOnlyStore:
    """Duck-types the two attributes (``knowledge``, ``program_root``) that
    :mod:`trialerror.retrieve.lexical` and :mod:`trialerror.retrieve.ftssearch`
    actually read, so this doctor check can reuse the real query primitives
    without opening a full ``Store``. ``program_root`` is what lets
    :func:`~trialerror.retrieve.lexical.resolve_backend` find this program's
    tantivy index (fix pass, finding LD-03) -- without it every call would
    silently fall back to FTS5 regardless of what is actually serving."""

    def __init__(self, knowledge_conn, program_root=None):
        self.knowledge = knowledge_conn
        self.program_root = program_root


def _knowledge_path(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    return paths.knowledge_db_path(ctx.program_root)


def _skip(name: str, message: str = "knowledge.db not found (program_root not configured, or program not yet initialized)") -> CheckResult:
    return CheckResult(name=name, category="retrieve", status="skip", message=message)


@register_check("fence_integrity", category="retrieve")
def check_fence_integrity(ctx: DoctorContext) -> CheckResult:
    """F3 regression sentinel: recompute the serving-path fence
    (:func:`trialerror.retrieve.fence.fence_chunk_text`) for a sample of the
    live program's ``commercial_restricted`` chunks and assert the
    resulting verbatim excerpt is never more than 20 words -- design
    Section 7 / ``DESIGN_REVIEW_v0.md`` F3's own cap."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("fence_integrity")
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT chunk.chunk_id AS chunk_id, chunk.text AS text
            FROM chunk
            JOIN document ON document.doc_id = chunk.doc_id
            JOIN source ON source.source_id = document.source_id
            WHERE source.license_tier = 'commercial_restricted'
            LIMIT ?
            """,
            (_FENCE_SAMPLE_LIMIT,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return CheckResult(
            name="fence_integrity", category="retrieve", status="skip",
            message="no commercial_restricted chunks in this program's corpus yet", details={"sampled": 0},
        )

    offenders = [r["chunk_id"] for r in rows if len(excerpt_words(r["text"]).split()) > 20]
    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} of {len(rows)} sampled commercial_restricted chunk(s) fence to a >20-word excerpt (F3 violation)"
        if offenders
        else f"all {len(rows)} sampled commercial_restricted chunk(s) fence to <=20-word excerpts"
    )
    return CheckResult(
        name="fence_integrity", category="retrieve", status=status, message=message,
        details={"sampled": len(rows), "offender_chunk_ids": offenders},
    )


@register_check("retrieval_latency", category="retrieve")
def check_retrieval_latency(ctx: DoctorContext) -> CheckResult:
    """Live-corpus lexical-tier latency probe (see module docstring for why
    this is NOT the M8 acceptance's 15k-synthetic-chunk gate).

    Fix pass, finding LD-03: this used to call ``fts_search`` directly, so
    on a program actually served by tantivy the probe measured a tier
    nothing was querying -- exactly backwards for a lane whose whole point
    is the lexical tier's latency. It now goes through
    :func:`~trialerror.retrieve.lexical.lexical_search`, the SAME
    resolution :mod:`trialerror.retrieve.engine` uses, and reports which
    backend actually answered."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("retrieval_latency")
    config = _program_config(ctx.program_root)
    conn = connect(path, read_only=True)
    try:
        chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])
        if chunk_count == 0:
            return CheckResult(
                name="retrieval_latency", category="retrieve", status="skip",
                message="no chunks in this program's corpus yet", details={"chunks": 0},
            )
        shim = _KnowledgeOnlyStore(conn, program_root=ctx.program_root)
        t0 = time.perf_counter()
        hits, backend = lexical_search(shim, "the", limit=DEFAULT_FTS_CANDIDATE_LIMIT, config=config)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        conn.close()

    status = "warn" if elapsed_ms > _LATENCY_WARN_MS else "pass"
    message = f"{backend} prefilter over {chunk_count} chunk(s) took {elapsed_ms:.1f}ms ({len(hits)} candidate(s))"
    return CheckResult(
        name="retrieval_latency", category="retrieve", status=status, message=message,
        details={"chunks": chunk_count, "fulltext_backend": backend, "fts_candidates": len(hits), "elapsed_ms": round(elapsed_ms, 2)},
    )


def _program_config(program_root: Path | None) -> dict:
    """``trialerror.toml``'s raw dict, or ``{}``. Same best-effort posture
    every other config read in this codebase takes -- a doctor check must
    never itself be the thing that raises."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    if program_root is None:
        return {}
    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


@register_check("fulltext_index_stale", category="retrieve")
def check_fulltext_index_stale(ctx: DoctorContext) -> CheckResult:
    """C-0080's freshness sentinel for the tantivy lexical index -- design
    Section 5.2's own "index freshness" doctor row, now that there is a
    second index to keep fresh.

    The tantivy index is DERIVED state
    (:mod:`trialerror.retrieve.tantivysearch`): ``knowledge.db``'s ``chunk``
    table is truth, the index is rebuildable from it, and the only failure
    mode that matters is the two drifting apart. This check compares three
    numbers -- the corpus's own ``(count, fingerprint)``, the index
    sidecar's claim, and the tantivy index's actual document count -- so
    both kinds of drift are caught: index-vs-corpus (an ingest that never
    reached the index, a chunk written by some other path) AND
    sidecar-vs-index (a crash between ``commit()`` and the sidecar write).
    The fingerprint is an order-independent XOR fold over ``chunk_id``\\ s,
    so the corpus side costs one unordered single-column scan -- no text
    read, no sort, no join.

    Statuses:

    - ``skip`` -- no ``knowledge.db``; or ``[retrieve] fulltext_backend =
      "fts5"`` (the program opted out, so a missing index is correct); or
      tantivy-py isn't installed (the serving path is on FTS5 by rule 3,
      which is a deployment fact, not a corpus fault -- ``doctor``'s
      dependency reporting is where a missing optional package belongs);
      or the corpus has no chunks yet.
    - ``warn`` -- no index built yet on a program that HAS chunks. A warn,
      not a fail: search still answers correctly out of ``chunk_fts``
      (rule 4's fallback), it is simply slower than this program asked for.
      This is the expected state of every existing program the moment
      C-0080 merges, until someone runs the reindex.
    - ``fail`` -- an index exists but disagrees with the corpus. THIS is
      the dangerous one: the serving path trusts a ready index, so a stale
      one silently under-recalls.
    - ``pass`` -- all three numbers agree.
    """
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("fulltext_index_stale")

    config = _program_config(ctx.program_root)
    backend = configured_backend_name(config)
    if backend != "tantivy":
        return CheckResult(
            name="fulltext_index_stale", category="retrieve", status="skip",
            message=f"trialerror.toml [retrieve] fulltext_backend = {backend!r}; no tantivy index expected",
            details={"configured_backend": backend},
        )
    if not tantivysearch.tantivy_available():
        return CheckResult(
            name="fulltext_index_stale", category="retrieve", status="skip",
            message="the 'tantivy' package is not installed; the lexical tier is served by SQLite FTS5",
            details={"configured_backend": backend, "tantivy_available": False},
        )

    index_dir = paths.fulltext_index_path(ctx.program_root, config)
    conn = connect(path, read_only=True)
    try:
        status = tantivysearch.index_status(conn, index_dir)
    finally:
        conn.close()

    details = {"configured_backend": backend, **status}
    if status["db_chunks"] == 0:
        return CheckResult(
            name="fulltext_index_stale", category="retrieve", status="skip",
            message="no chunks in this program's corpus yet", details=details,
        )
    if status["state"] == "missing":
        return CheckResult(
            name="fulltext_index_stale", category="retrieve", status="warn",
            message=(
                f"no tantivy full-text index at {index_dir} for {status['db_chunks']} chunk(s); "
                "searches are falling back to the SQLite FTS5 tier -- run "
                "`trialerror ingest reindex-fulltext` to build it"
            ),
            details=details,
        )
    if status["state"] != "ok":
        return CheckResult(
            name="fulltext_index_stale", category="retrieve", status="fail",
            message=(
                f"tantivy full-text index is STALE: corpus has {status['db_chunks']} chunk(s) "
                f"(fingerprint {status['db_fingerprint']}), index holds {status['index_docs']} doc(s) "
                f"(sidecar claims {status['meta_chunks']}, fingerprint {status['meta_fingerprint']}) -- "
                "run `trialerror ingest reindex-fulltext`"
            ),
            details=details,
        )
    return CheckResult(
        name="fulltext_index_stale", category="retrieve", status="pass",
        message=f"tantivy full-text index is current with all {status['db_chunks']} chunk(s)",
        details=details,
    )


# ---------------------------------------------------------------------------
# lane F-1: the query side of retrieval
# ---------------------------------------------------------------------------


@register_check(QUERY_EMBED_DOCTOR_CHECK, category="retrieve")
def check_query_embed_backend_runnable(ctx: DoctorContext) -> CheckResult:
    """Can THIS process embed a query?

    The check exists because the answer is routinely no, and used to be
    invisible. A program whose document embeddings are produced on another
    machine (``[ingest.embed] backend = "offload"``) has a document backend
    whose ``embed_batch`` raises by design -- correctly -- and until lane F-1
    every query-time caller that reached for it raised too, in the middle of
    a search. The fix is a separate query-side backend
    (``[ingest.embed.query]``); this check is how an operator finds out
    whether theirs works, in one line, before an agent finds out by getting
    lexical-only results all afternoon.

    Statuses:

    - ``skip`` -- no ``knowledge.db``, or a corpus with no embeddings at all
      under any key. Nothing has been embedded, so there is no vector tier
      to be unable to serve, and reporting a backend problem would be
      reporting it for a search that cannot run either way.
    - ``warn`` -- not runnable here, with the backend's own reason. A warn,
      not a fail: the corpus is intact, ``search`` still answers out of the
      full-text tier and says that it did, and on a two-machine program in
      the middle of delivering a CPU encoder this is the EXPECTED state.
      What it is not is silent.
    - ``pass`` -- the query-side backend reports it can compute here.

    Read-only and side-effect-free by construction: it asks
    :meth:`~trialerror.ingest.backends.EmbedBackend.runnable`, which is
    allowed to load a model and is not allowed to embed anything.
    """
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip(QUERY_EMBED_DOCTOR_CHECK)

    conn = connect(path, read_only=True)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM emb").fetchone()
        embeddings = int(row["n"]) if row is not None else 0
    except Exception:  # noqa: BLE001 - a store predating the table is "no embeddings"
        embeddings = 0
    finally:
        conn.close()

    config = _program_config(ctx.program_root)
    embed_config = (config.get("ingest") or {}).get("embed") or {}
    query_backend_name = query_embed_backend_name(embed_config)
    details = {
        "query_backend": query_backend_name,
        "document_backend": embed_config.get("backend", "fake"),
        "config_table": QUERY_EMBED_TABLE,
        "embeddings": embeddings,
    }

    if embeddings == 0:
        return CheckResult(
            name=QUERY_EMBED_DOCTOR_CHECK, category="retrieve", status="skip",
            message="no embeddings in this program's corpus yet; there is no vector tier to serve",
            details=details,
        )

    try:
        backend = load_query_embed_backend(embed_config)
    except Exception as exc:  # noqa: BLE001 - a refused/malformed table is exactly what this reports
        return CheckResult(
            name=QUERY_EMBED_DOCTOR_CHECK, category="retrieve", status="warn",
            message=(
                f"the query-side embed backend could not be resolved ({type(exc).__name__}: {exc}) -- "
                f"fix [{QUERY_EMBED_TABLE}] in trialerror.toml"
            ),
            details=details,
        )

    details["model_key"] = getattr(backend, "model_key", None)
    details["dims"] = getattr(backend, "dims", None)
    runnable, reason = embed_backend_runnable(backend)
    # Lane F-1b item 5: WHICH runtime, with the numbers that decide whether it
    # is usable -- the in-process encoder's n_threads/n_threads_batch/cgroup
    # quota (a wrong batch-thread count is a 7x slowdown and nothing else
    # reports it), or the sidecar client's URL and last health reading.
    # Collected AFTER the runnable() probe so the health line is the one the
    # probe just took, and through the never-raises accessor so a detail field
    # can never be what breaks a check.
    runtime = embed_backend_runtime_details(backend)
    if runtime:
        details["runtime"] = runtime
    if not runnable:
        return CheckResult(
            name=QUERY_EMBED_DOCTOR_CHECK, category="retrieve", status="warn",
            message=(
                f"this process cannot embed a query: {reason}. Searches run the full-text tier only "
                f"(and say so); `verify hypothesis` and `lens screen` refuse. Configure a runnable "
                f"query-side backend in [{QUERY_EMBED_TABLE}]"
            ),
            details={**details, "reason": reason},
        )
    return CheckResult(
        name=QUERY_EMBED_DOCTOR_CHECK, category="retrieve", status="pass",
        message=(
            f"query-side embed backend {query_backend_name!r} is runnable here under model_key "
            f"{details['model_key']!r} ({details['dims']} dims)"
        ),
        details=details,
    )


@register_check("vecmatrix_stale", category="retrieve")
def check_vecmatrix_stale(ctx: DoctorContext) -> CheckResult:
    """Whether any cached similarity matrix
    (:mod:`trialerror.retrieve.vecmatrix`) still describes the table it was
    built from.

    A stale cache here is not a correctness risk -- the fingerprint is
    checked on every query and a mismatch rebuilds before ranking, so a
    stale file can only ever cost the rebuild it is about to trigger. That
    is precisely why this is a ``warn`` and never a ``fail``: what it
    reports is a known cost (the next unlucky query pays a full rebuild),
    not a wrong answer. ``skip`` when no key has a cache at all, which is
    every program until its first large unbounded ranking call.
    """
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("vecmatrix_stale")

    conn = connect(path, read_only=True)
    try:
        try:
            registry = [dict(r) for r in conn.execute("SELECT model_key FROM vec_index_registry")]
        except Exception:  # noqa: BLE001 - fresh program, no registry table
            registry = []
        store = _KnowledgeOnlyStore(conn, ctx.program_root)
        statuses = {}
        for reg in registry:
            model_key = reg["model_key"]
            statuses[model_key] = vecmatrix.matrix_status(store, model_key, _program_config(ctx.program_root))
    finally:
        conn.close()

    present = {k: v for k, v in statuses.items() if v["present"]}
    details = {"model_keys": statuses, "numpy_available": vecmatrix.numpy_available()}
    if not present:
        return CheckResult(
            name="vecmatrix_stale", category="retrieve", status="skip",
            message="no resident similarity matrix cached for any model key yet", details=details,
        )
    stale = sorted(k for k, v in present.items() if v["stale"])
    if stale:
        return CheckResult(
            name="vecmatrix_stale", category="retrieve", status="warn",
            message=(
                f"the cached similarity matrix for {', '.join(repr(k) for k in stale)} no longer matches "
                "its vector table; the next unbounded ranking call rebuilds it (correct, but it pays "
                "for the rebuild)"
            ),
            details=details,
        )
    return CheckResult(
        name="vecmatrix_stale", category="retrieve", status="pass",
        message=f"the cached similarity matrix is current for {len(present)} model key(s)",
        details=details,
    )
