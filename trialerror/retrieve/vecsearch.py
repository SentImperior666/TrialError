"""Vector tier. Design Section 7 pipeline step 2: "vector scoring of
candidates with local Qwen3-4B query embedding". Reads
``vec_chunks__<model_key>`` (``trialerror.stores.vecindex``, M1/M7).

**CRITICAL RULE (M7 hit this live, carried forward verbatim in this
build's brief):** ``sqlite-vec``'s loadable extension is per-CONNECTION,
not per-database-file -- a fresh ``sqlite3.Connection`` that never called
``try_load_sqlite_vec`` will fail to even recognize a ``vec0``-backed
``vec_chunks__*`` table (SQLite errors resolving the virtual-table module),
regardless of whether the table was created with the real backend. Every
function in this module that touches a ``vec_chunks__*`` table calls
:func:`trialerror.stores.vecindex.try_load_sqlite_vec` on its connection FIRST
-- unconditionally, every call, never cached across calls (a caller may
pass a connection this module has never seen before).

**Both serializations, one code path.** ``trialerror.stores.vecindex``'s own
module docstring: a fallback-backend row's ``vector`` BLOB is "the same
wire format sqlite-vec itself uses" (packed little-endian float32,
``struct.pack(f"<{n}f", ...)``) -- and ``trialerror.ingest.handlers.run_embed``
writes the ``emb`` cache (the source every ``vec_chunks__*`` row is
populated from) through that SAME :func:`serialize_vector_fallback`
regardless of which backend ``run_index`` ends up writing to. So a plain
``SELECT vector FROM vec_chunks__<model_key> WHERE chunk_id = ...``
followed by :func:`~trialerror.stores.vecindex.deserialize_vector_fallback`
round-trips correctly whether the table is a real ``vec0`` virtual table or
the pure-stdlib fallback -- for the BOUNDED (FTS-prefiltered candidate-set)
paths, this module still never needs sqlite-vec's own KNN ``MATCH``
operator (which does not support an arbitrary ``chunk_id IN (...)``
restriction to a candidate set anyway); cosine similarity is computed in
plain Python over exactly the candidate vectors fetched, uniformly for
both backends.

**B.4b native-MATCH wiring (build-arxiv-kaggle-index session,
``spikes/index_bakeoffs/BAKEOFF_REPORT.md`` Sec B.4b's named trigger):**
:func:`fetch_native_knn` is the ONE exception to the paragraph above --
used only by ``trialerror.retrieve.engine``'s two genuinely UNBOUNDED paths
(``search(mode="vector")`` with no ``filters``, and ``similar()``, which
always ranks against the WHOLE corpus) and only when
:func:`vec_backend_for` reports this ``model_key``'s table was actually
built as a real ``vec0`` table (``TRIALERROR_VEC_BACKEND=sqlite_vec`` at index-
build time, per ``trialerror.stores.vecindex.ensure_vec_table``'s own opt-in
default) -- never for the bounded FTS-prefiltered case, and never when the
table is the fallback backend (nothing changes for either of those; the
existing ``fetch_vectors``+``rank_by_query_vector`` path is untouched).
This is deliberately narrow: B.4a's own finding is that vec0's ORDINARY
row-access path is 7-17x SLOWER than the fallback table at production's
CURRENT read pattern, so this wiring only ever engages sqlite-vec's own
native operator (170-360x faster than either backend's ordinary access,
per the same bake-off), never plain row fetches against a vec0 table.
"""

from __future__ import annotations

import math
from typing import Sequence

from trialerror.stores.store import Store
from trialerror.stores.vecindex import (
    VecBackend,
    deserialize_vector_fallback,
    serialize_vector_fallback,
    try_load_sqlite_vec,
    vec_table_name,
)

__all__ = [
    "vec_table_exists",
    "fetch_vectors",
    "cosine_similarity",
    "rank_by_query_vector",
    "vec_backend_for",
    "fetch_native_knn",
]


def _table_exists(store: Store, table: str) -> bool:
    row = store.knowledge.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def vec_table_exists(store: Store, model_key: str) -> bool:
    """Whether an index table has ever been created for ``model_key``
    (design: a fresh program has no vector tier until M7's ``embed``/
    ``index`` stages run at least once) -- callers use this to decide
    whether the vector tier is even attemptable, never letting an absent
    table surface as a raw ``sqlite3.OperationalError``."""
    try_load_sqlite_vec(store.knowledge)  # per-connection; see module docstring
    return _table_exists(store, vec_table_name(model_key))


def fetch_vectors(store: Store, model_key: str, chunk_ids: Sequence[str]) -> dict[str, list[float]]:
    """Fetch the stored vector for each of ``chunk_ids`` that has one in
    ``vec_chunks__<model_key>``. Missing ids (never indexed, or indexed
    under a different model_key) are simply absent from the returned dict
    -- never an error; ``trialerror.retrieve.engine`` treats "no vector" as "this
    candidate doesn't participate in the vector tier", not a failure."""
    ids = list(dict.fromkeys(chunk_ids))  # dedupe, stable order
    if not ids:
        return {}
    try_load_sqlite_vec(store.knowledge)  # per-connection; see module docstring
    table = vec_table_name(model_key)
    if not _table_exists(store, table):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = store.knowledge.execute(
        f"SELECT chunk_id, vector FROM {table} WHERE chunk_id IN ({placeholders})", ids
    ).fetchall()
    return {r["chunk_id"]: deserialize_vector_fallback(r["vector"]) for r in rows}


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain-Python cosine similarity -- no numpy dependency (this build's
    lane may only touch ``pyproject.toml``'s ``mcp`` dependency line, so no
    new numerical dependency is available to reach for here even if it
    were otherwise desirable). Returns ``0.0`` for a zero-length or
    zero-norm vector pair rather than raising a ``ZeroDivisionError`` --
    a synthetic/degenerate test vector must never crash a ranking pass."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def vec_backend_for(store: Store, model_key: str) -> VecBackend | None:
    """Which backend ``vec_chunks__<model_key>`` was actually built as
    (``trialerror.stores.vecindex.ensure_vec_table``'s own ``vec_index_registry``
    bookkeeping row) -- ``None`` when no table was ever created for this
    ``model_key`` (registry table absent, or no matching row). Used by
    ``trialerror.retrieve.engine`` to decide whether :func:`fetch_native_knn` is
    even usable for a given corpus -- the physical table's real backend,
    not the CURRENT ``TRIALERROR_VEC_BACKEND`` env var (which only affects a
    NEW table's creation, not an already-built one)."""
    try:
        row = store.knowledge.execute(
            "SELECT backend FROM vec_index_registry WHERE model_key = ?", (model_key,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - deliberate: registry table absent (fresh program) -> None, never raise
        return None
    if row is None:
        return None
    try:
        return VecBackend(row["backend"])
    except ValueError:
        return None


def fetch_native_knn(
    store: Store, model_key: str, query_vector: Sequence[float], *, k: int, exclude_chunk_id: str | None = None
) -> list[tuple[str, float]]:
    """sqlite-vec's native ``vector MATCH ? ORDER BY distance LIMIT ?``
    KNN operator (module docstring's B.4b paragraph) -- confirmed syntax,
    ported directly from ``spikes/index_bakeoffs/bench_vec.py``'s own
    ``query_native_knn_ceiling`` (that spike's own smoke-tested, working
    shape, not re-derived/guessed here). Callers MUST already know this
    ``model_key``'s table is real ``vec0`` (:func:`vec_backend_for`) --
    this function does not check, so calling it against a fallback-backend
    table raises a plain ``sqlite3.OperationalError`` from SQLite itself
    ("no such table" or "no such module: vec0"'s cousin), not a friendly
    error -- the guard belongs at the call site (module docstring: "only
    used ... when vec_backend_for reports ... a real vec0 table").

    Returns ``(chunk_id, score)`` pairs, best (most similar) FIRST, same
    "higher = better" convention :func:`rank_by_query_vector` returns --
    sqlite-vec's own ``distance`` column is the OPPOSITE sense (lower =
    more similar, L2/euclidean by default), negated here so a caller never
    has to remember which convention which function uses.

    ``exclude_chunk_id`` (``similar()``'s own "never rank a chunk against
    itself" requirement) fetches ``k + 1`` rows and drops a match on that
    id before truncating back to ``k`` -- cheaper than a second query, and
    correct even in the (unlikely) case the excluded id isn't in the
    top ``k + 1`` at all (nothing to drop, the extra row is simply
    trimmed off)."""
    try_load_sqlite_vec(store.knowledge)  # per-connection; see module docstring
    table = vec_table_name(model_key)
    fetch_k = k + 1 if exclude_chunk_id is not None else k
    if fetch_k <= 0:
        return []
    blob = serialize_vector_fallback(list(query_vector))
    rows = store.knowledge.execute(
        f"SELECT chunk_id, distance FROM {table} WHERE vector MATCH ? ORDER BY distance LIMIT ?",
        (blob, fetch_k),
    ).fetchall()
    out = [(r["chunk_id"], -float(r["distance"])) for r in rows if r["chunk_id"] != exclude_chunk_id]
    return out[:k]


def rank_by_query_vector(
    query_vector: Sequence[float], vectors: dict[str, list[float]]
) -> list[tuple[str, float]]:
    """Rank ``vectors`` (``chunk_id -> vector``) by cosine similarity to
    ``query_vector``, best (highest similarity) first. Ties break on
    ``chunk_id`` for deterministic ordering (test reproducibility -- design
    Section 12's own "stratify on fixture corpus reproduces byte-identical
    arms from same seed" bar, applied here to search ranking)."""
    scored = [(cid, cosine_similarity(query_vector, vec)) for cid, vec in vectors.items()]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored
