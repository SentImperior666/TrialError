"""The query path. Build brief item 1 (B.4b native-MATCH wiring, scoped
primarily here) + item 4 (``trialerror lit arxiv-semantic``).

:func:`semantic_search` is the native-``MATCH`` path
(BAKEOFF_REPORT.md Sec B.4b's own named trigger: "a 2M+-row index built
from this Kaggle dataset is exactly the scale [that] bake-off's own B.4b
recommendation names as the native-MATCH trigger case"): ``SELECT
arxiv_id, distance FROM arxiv_vec WHERE embedding MATCH ? ORDER BY
distance LIMIT ?`` -- the EXACT syntax ``spikes/index_bakeoffs/bench_vec.py``
``query_native_knn_ceiling`` already confirmed working against the real
installed extension (this build reused that confirmed shape rather than
guessing sqlite-vec's KNN syntax fresh).

:func:`semantic_search_bruteforce` is the fallback path for a machine
without the sqlite-vec extension (the ``arxiv_vec`` fallback-backend
table -- plain SQL fetch + Python cosine, reusing
``trialerror.retrieve.vecsearch.cosine_similarity``/``rank_by_query_vector``
rather than re-deriving ranking logic a second time). :func:`semantic_search`
dispatches to whichever backend :func:`trialerror.arxiv_index.store.ensure_schema`
actually created for this connection.

``distance`` here is sqlite-vec's own metric (L2/euclidean by default for
``vec0`` unless a distance metric is configured at table-creation time,
which this build's schema does not override -- see
``trialerror.arxiv_index.store.ensure_schema``) -- LOWER is more similar, the
OPPOSITE sense of ``cosine_similarity``'s "higher is more similar". Both
functions below sort their own metric in its own "best first" direction and
return the SAME row shape (``{"arxiv_id", "score", ...}``, ``score`` always
"higher is better" -- :func:`semantic_search` negates raw L2 distance into
a score so a caller never has to remember which backend it got, matching
this repo's general "callers don't need to know which backend" posture,
e.g. ``trialerror.stores.vecindex``'s wire-format-compatibility docstring).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trialerror.arxiv_index.store import META_TABLE_NAME, VEC_TABLE_NAME, VecBackend, deserialize_vector_fallback, get_build_state
from trialerror.retrieve.vecsearch import cosine_similarity
from trialerror.stores.vecindex import serialize_vector_fallback
from trialerror.util.vecmath import _block_rows, numpy_module

__all__ = [
    "SemanticSearchResult",
    "MAX_BATCH_QUERIES",
    "current_backend",
    "semantic_search",
    "semantic_search_native",
    "semantic_search_bruteforce",
    "semantic_search_many",
]

#: The most queries one :func:`semantic_search_many` call will take. A batch is
#: ONE pass whatever its size, so the ceiling is not about the scan -- it is
#: about the ``(Q, dims)`` query matrix and the per-query candidate sets, which
#: are linear in Q and live in memory for the whole pass.
MAX_BATCH_QUERIES = 256


@dataclass(frozen=True)
class SemanticSearchResult:
    arxiv_id: str
    score: float
    title: str | None
    categories: str | None
    published: str | None
    authors: str | None
    doi: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "score": self.score,
            "title": self.title,
            "categories": self.categories,
            "published": self.published,
            "authors": self.authors,
            "doi": self.doi,
        }


def current_backend(conn: sqlite3.Connection) -> VecBackend:
    state = get_build_state(conn)
    raw = state.get("backend")
    if raw == VecBackend.SQLITE_VEC.value:
        return VecBackend.SQLITE_VEC
    return VecBackend.FALLBACK


def _fetch_meta(conn: sqlite3.Connection, arxiv_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not arxiv_ids:
        return {}
    placeholders = ",".join("?" for _ in arxiv_ids)
    rows = conn.execute(
        f"SELECT * FROM {META_TABLE_NAME} WHERE arxiv_id IN ({placeholders})", arxiv_ids
    ).fetchall()
    return {r["arxiv_id"]: r for r in rows}


def _to_result(arxiv_id: str, score: float, meta: dict[str, sqlite3.Row]) -> SemanticSearchResult:
    row = meta.get(arxiv_id)
    return SemanticSearchResult(
        arxiv_id=arxiv_id,
        score=score,
        title=row["title"] if row else None,
        categories=row["categories"] if row else None,
        published=row["published"] if row else None,
        authors=row["authors"] if row else None,
        doi=row["doi"] if row else None,
    )


def semantic_search_native(conn: sqlite3.Connection, query_vector: list[float], *, k: int = 10) -> list[SemanticSearchResult]:
    """``vec0``'s native KNN operator (module docstring). Requires the live
    connection to already have the sqlite-vec extension loaded (the SAME
    per-connection rule ``trialerror.retrieve.vecsearch``'s module docstring
    states -- callers use :func:`semantic_search`, which loads it, rather
    than calling this directly against a fresh unprimed connection)."""
    blob = serialize_vector_fallback(query_vector)
    rows = conn.execute(
        f"SELECT arxiv_id, distance FROM {VEC_TABLE_NAME} WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (blob, k),
    ).fetchall()
    meta = _fetch_meta(conn, [r["arxiv_id"] for r in rows])
    # L2 distance: lower = more similar. Negate so this function's own
    # "score" contract (higher = better) matches semantic_search_bruteforce's.
    return [_to_result(r["arxiv_id"], -float(r["distance"]), meta) for r in rows]


def semantic_search_bruteforce(conn: sqlite3.Connection, query_vector: list[float], *, k: int = 10) -> list[SemanticSearchResult]:
    """Fallback-backend path: fetch every stored vector, rank in plain
    Python. Only viable at fixture/small-corpus scale (module docstring's
    own framing: this dataset's real scale is exactly why native MATCH is
    mandatory) -- kept for the no-extension-installed case and as the
    ground truth :func:`semantic_search_native`'s correctness is checked
    against in tests (offline, same fixture, both code paths, same
    top-k)."""
    rows = conn.execute(f"SELECT arxiv_id, dims, vector FROM {VEC_TABLE_NAME}").fetchall()
    scored = [
        (r["arxiv_id"], cosine_similarity(query_vector, deserialize_vector_fallback(r["vector"])))
        for r in rows
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    top = scored[:k]
    meta = _fetch_meta(conn, [aid for aid, _ in top])
    return [_to_result(aid, score, meta) for aid, score in top]


def _dims_of(conn: sqlite3.Connection, fallback: int) -> int:
    raw = get_build_state(conn).get("dims")
    try:
        dims = int(raw)
    except (TypeError, ValueError):
        return fallback
    return dims if dims > 0 else fallback


def _batch_candidates(
    conn: sqlite3.Connection,
    query_vectors: Sequence[Sequence[float]],
    *,
    dims: int,
    keep: int,
    backend: VecBackend,
    numpy,
) -> tuple[list[list[tuple[str, bytes]]], int, int, str]:
    """ONE streaming pass over the vector table, scoring every row against
    EVERY query, and returning per query the ``keep`` best candidate ids.

    Memory is bounded by the block, not by the table: ``block =
    trialerror.util.vecmath._block_rows(dims)`` is the tree's existing bound
    (at 3072 dims ~2,666 rows, ~32 MB of float32 per block), so a
    three-million-row table is scanned in the same scratch a thousand-row one
    needs. The per-query candidate sets are ``Q x keep`` ids.

    Returns ``(candidates_per_query, rows_scanned, skipped_rows, statement)``,
    where each candidate is an ``(arxiv_id, blob)`` pair.
    """
    column = "embedding" if backend == VecBackend.SQLITE_VEC else "vector"
    statement = f"SELECT arxiv_id, {column} FROM {VEC_TABLE_NAME}"
    width = dims * 4
    block = _block_rows(dims)

    queries = numpy.asarray(
        [[float(v) for v in vec] for vec in query_vectors], dtype=numpy.float32
    ).astype(numpy.float64)
    q_sq = numpy.einsum("ij,ij->i", queries, queries)
    if backend == VecBackend.SQLITE_VEC:
        q_norm = None
    else:
        q_norm = numpy.sqrt(q_sq)
        q_norm[q_norm == 0.0] = 1.0

    n_queries = len(query_vectors)
    # Per query: parallel arrays of the best `keep` (score, id, blob) seen so
    # far. The BLOB is kept, not just the id, so the exact finish below needs no
    # second look at the table -- see _exact_finish for why that matters. The
    # cost is bounded and independent of the table: Q x keep x dims x 4 bytes,
    # at most ~81 MB at MAX_BATCH_QUERIES and 3072 dims, and a few hundred KB
    # for any batch a person actually types.
    best_scores: list = [numpy.empty(0, dtype=numpy.float64) for _ in range(n_queries)]
    best_ids: list[list[str]] = [[] for _ in range(n_queries)]
    best_blobs: list[list[bytes]] = [[] for _ in range(n_queries)]
    rows_scanned = 0
    skipped_rows = 0

    cursor = conn.execute(statement)
    while True:
        rows = cursor.fetchmany(block)
        if not rows:
            break
        ids: list[str] = []
        blobs: list[bytes] = []
        for row in rows:
            rows_scanned += 1
            blob = row[1]
            if blob is None or len(blob) != width:
                # A row whose blob is not dims*4 wide cannot be scored against
                # a dims-wide query. Counted rather than crashed on: one bad
                # row must not make the other three million unsearchable.
                skipped_rows += 1
                continue
            ids.append(row[0])
            blobs.append(blob)
        if not ids:
            continue
        matrix = numpy.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(ids), dims).astype(numpy.float64)
        dots = queries @ matrix.T  # (Q, n)
        if backend == VecBackend.SQLITE_VEC:
            # Mirror the native backend's own metric so scores keep today's
            # meaning: L2 distance, negated (higher is better).
            x_sq = numpy.einsum("ij,ij->i", matrix, matrix)
            d2 = q_sq[:, None] + x_sq[None, :] - 2.0 * dots
            numpy.clip(d2, 0.0, None, out=d2)
            scores = -numpy.sqrt(d2)
        else:
            x_norm = numpy.sqrt(numpy.einsum("ij,ij->i", matrix, matrix))
            x_norm[x_norm == 0.0] = 1.0
            scores = dots / (q_norm[:, None] * x_norm[None, :])
        for qi in range(n_queries):
            row_scores = scores[qi]
            if len(ids) > keep:
                top = numpy.argpartition(-row_scores, keep - 1)[:keep]
            else:
                top = numpy.arange(len(ids))
            merged_scores = numpy.concatenate([best_scores[qi], row_scores[top]])
            merged_ids = best_ids[qi] + [ids[int(i)] for i in top]
            merged_blobs = best_blobs[qi] + [blobs[int(i)] for i in top]
            if len(merged_ids) > keep:
                pick = numpy.argpartition(-merged_scores, keep - 1)[:keep]
                best_scores[qi] = merged_scores[pick]
                best_ids[qi] = [merged_ids[int(i)] for i in pick]
                best_blobs[qi] = [merged_blobs[int(i)] for i in pick]
            else:
                best_scores[qi] = merged_scores
                best_ids[qi] = merged_ids
                best_blobs[qi] = merged_blobs

    candidates = [list(zip(best_ids[qi], best_blobs[qi])) for qi in range(n_queries)]
    return candidates, rows_scanned, skipped_rows, statement


def _exact_finish(
    conn: sqlite3.Connection,
    query_vector: Sequence[float],
    candidates: Sequence[tuple[str, bytes]],
    *,
    k: int,
    backend: VecBackend,
) -> list[tuple[str, float]]:
    """Re-score the narrowed candidates with the SAME arithmetic the
    single-query path uses, and sort with the same ``(-score, arxiv_id)`` rule.

    This is :mod:`trialerror.util.vecmath`'s posture, reused rather than
    re-derived: numpy is allowed to NARROW, never to decide. The returned ids,
    their order and their scores are produced by the same function
    :func:`semantic_search` would have used -- ``vec_distance_l2`` from the
    sqlite-vec extension itself on the native backend, ``cosine_similarity`` on
    the fallback -- so a batch's answer is the single-query answer, not
    something close to it.

    Scored from the blobs the pass already held, not from a fresh query. A
    ``WHERE arxiv_id IN (...)`` over the candidates would have been the obvious
    way to write this and is the wrong one on the native backend: ``vec0`` is a
    virtual table and answers that predicate with a full scan (``EXPLAIN QUERY
    PLAN`` says ``SCAN arxiv_vec VIRTUAL TABLE INDEX 0:1``), so it would have
    cost one extra full pass PER QUERY and given back exactly the Q-passes-for-Q-
    queries this function exists to avoid. ``vec_distance_l2`` called as a plain
    scalar over two blobs touches no table at all and returns the identical
    float.
    """
    if not candidates:
        return []
    unique: dict[str, bytes] = {}
    for arxiv_id, blob in candidates:
        unique.setdefault(arxiv_id, blob)
    if backend == VecBackend.SQLITE_VEC:
        query_blob = serialize_vector_fallback(list(query_vector))
        scored = [
            (arxiv_id, -float(conn.execute("SELECT vec_distance_l2(?, ?)", (blob, query_blob)).fetchone()[0]))
            for arxiv_id, blob in unique.items()
        ]
    else:
        vector = list(query_vector)
        scored = [
            (arxiv_id, cosine_similarity(vector, deserialize_vector_fallback(blob)))
            for arxiv_id, blob in unique.items()
        ]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:k]


def semantic_search_many(
    conn: sqlite3.Connection,
    query_vectors: Sequence[Sequence[float]],
    *,
    k: int = 10,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Q queries in ONE pass over the index instead of Q passes.

    **Why this and not a resident process.** ``vec0`` has no approximate
    index: a KNN ``MATCH`` is an exhaustive scan of every stored vector, and
    nothing is "loaded" by this package -- the scan IS the query. At this
    dataset's documented scale (millions of rows x 3072 float32, tens of GB,
    more than RAM) every call is a full disk-bound pass. So a process that
    stayed open would not help by itself: Q queries would still be Q full
    passes. The cheap, exact fix is to make Q queries cost ONE.

    Returns ``{"results", "scan_mode", "passes", "rows_scanned", "backend"}``,
    plus ``"skipped_rows"`` only when some row's blob was not ``dims * 4``
    wide. ``results`` is one list of :class:`SemanticSearchResult` per query,
    in the order the queries were given.

    ``scan_mode`` is ``"single_pass"`` or ``"per_query"``. The single pass needs
    numpy (optional, honouring ``[retrieve] numpy_fastpath`` exactly as the rest
    of the tree does): without it, or for a single query where there is nothing
    to batch, this loops :func:`semantic_search` on the SAME connection and says
    so -- same answers, Q passes. ``rows_scanned`` is ``None`` in that mode,
    because each pass happens inside :func:`semantic_search`, which does not
    report a row count, and counting would mean an extra scan of the very table
    this function exists to scan once.

    The answer is EXACT. numpy is used only to narrow each query to ``k +
    max(16, k)`` candidates; those are then re-scored and sorted by the same
    arithmetic the single-query path uses (:func:`_exact_finish`), so the ids,
    their order and their scores are what :func:`semantic_search` would have
    returned. :func:`semantic_search`, :func:`semantic_search_native` and
    :func:`semantic_search_bruteforce` are not changed at all.
    """
    vectors = [list(v) for v in query_vectors]
    if not vectors:
        return {
            "results": [], "scan_mode": "per_query", "passes": 0, "rows_scanned": None,
            "backend": current_backend(conn).value,
        }

    backend = current_backend(conn)
    if backend == VecBackend.SQLITE_VEC:
        from trialerror.stores.vecindex import try_load_sqlite_vec

        try_load_sqlite_vec(conn)

    numpy = numpy_module(config)
    if numpy is None or len(vectors) == 1:
        return {
            "results": [semantic_search(conn, vec, k=k) for vec in vectors],
            "scan_mode": "per_query",
            "passes": len(vectors),
            "rows_scanned": None,
            "backend": backend.value,
        }

    dims = _dims_of(conn, len(vectors[0]))
    keep = k + max(16, k)
    candidates, rows_scanned, skipped_rows, _ = _batch_candidates(
        conn, vectors, dims=dims, keep=keep, backend=backend, numpy=numpy
    )

    per_query = [
        _exact_finish(conn, vec, cands, k=k, backend=backend)
        for vec, cands in zip(vectors, candidates)
    ]
    union = list(dict.fromkeys(aid for scored in per_query for aid, _ in scored))
    meta = _fetch_meta(conn, union)
    out: dict[str, Any] = {
        "results": [[_to_result(aid, score, meta) for aid, score in scored] for scored in per_query],
        "scan_mode": "single_pass",
        "passes": 1,
        "rows_scanned": rows_scanned,
        "backend": backend.value,
    }
    if skipped_rows:
        out["skipped_rows"] = skipped_rows
    return out


def semantic_search(conn: sqlite3.Connection, query_vector: list[float], *, k: int = 10) -> list[SemanticSearchResult]:
    """Dispatches to whichever backend this db was actually built with
    (:func:`current_backend`, read from the build-state table -- never
    re-probed per call, since a db file's backend is fixed at build time).
    Always calls ``try_load_sqlite_vec`` first when the backend is
    sqlite_vec (per-connection rule, module docstring)."""
    backend = current_backend(conn)
    if backend == VecBackend.SQLITE_VEC:
        from trialerror.stores.vecindex import try_load_sqlite_vec

        try_load_sqlite_vec(conn)
        return semantic_search_native(conn, query_vector, k=k)
    return semantic_search_bruteforce(conn, query_vector, k=k)
