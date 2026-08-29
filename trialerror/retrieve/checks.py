"""M8's doctor checks. Design Section 12 (M8 row) build brief: "doctor
checks in trialerror/retrieval/checks.py (e.g. latency-fixture check,
fence-integrity check)".

Both checks below stay read-only-connection-only (``trialerror.stores.connection.
connect(path, read_only=True)`` against ``knowledge.db`` alone) rather than
opening a full four-DB :class:`~trialerror.stores.store.Store` (platform.db
included) -- the same discipline every other module's ``checks.py`` in this
codebase already follows (``trialerror.ingest.checks``, ``trialerror.jobs.checks``,
...). :mod:`trialerror.retrieve.ftssearch`/:mod:`trialerror.retrieve.vecsearch`'s
public functions only ever touch a ``store.knowledge`` attribute, so
:class:`_KnowledgeOnlyStore` duck-types exactly that one attribute rather
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

from trialerror.retrieve.fence import excerpt_words
from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT, fts_search
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_fence_integrity", "check_retrieval_latency"]

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
    """Duck-types the single attribute (``knowledge``) that
    :mod:`trialerror.retrieve.ftssearch` actually reads, so this doctor check can
    reuse the real query primitive without opening a full ``Store``."""

    def __init__(self, knowledge_conn):
        self.knowledge = knowledge_conn


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
    """Live-corpus FTS-tier latency probe (see module docstring for why
    this is NOT the M8 acceptance's 15k-synthetic-chunk gate)."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("retrieval_latency")
    conn = connect(path, read_only=True)
    try:
        chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])
        if chunk_count == 0:
            return CheckResult(
                name="retrieval_latency", category="retrieve", status="skip",
                message="no chunks in this program's corpus yet", details={"chunks": 0},
            )
        shim = _KnowledgeOnlyStore(conn)
        t0 = time.perf_counter()
        hits = fts_search(shim, "the", limit=DEFAULT_FTS_CANDIDATE_LIMIT)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        conn.close()

    status = "warn" if elapsed_ms > _LATENCY_WARN_MS else "pass"
    message = f"FTS prefilter over {chunk_count} chunk(s) took {elapsed_ms:.1f}ms ({len(hits)} candidate(s))"
    return CheckResult(
        name="retrieval_latency", category="retrieve", status=status, message=message,
        details={"chunks": chunk_count, "fts_candidates": len(hits), "elapsed_ms": round(elapsed_ms, 2)},
    )
