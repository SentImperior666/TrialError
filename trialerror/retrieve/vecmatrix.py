"""A resident float32 matrix of one model key's stored vectors -- the same
ranking, two orders of magnitude faster, for the two paths whose universe is
the whole table.

**The problem, measured.** The default vector table
(:mod:`trialerror.stores.vecindex`, backend ``fallback`` by the bake-off's own
finding) stores one packed-float32 BLOB per chunk, and the read path
deserialises each one in Python
(:func:`~trialerror.stores.vecindex.deserialize_vector_fallback`) and scores it
with a Python cosine (:func:`~trialerror.retrieve.vecsearch.cosine_similarity`).
Bounded by a full-text prefilter that is a few hundred rows and nobody
notices. Unbounded -- ``similar()``, which always ranks against the whole
corpus, and ``search(mode="vector")`` with no filters -- it is tens of
thousands of rows per query, and on a 16k x 2048 corpus one ``query similar
--k 20`` costs seconds of wall clock, almost all of it per-row
deserialisation.

**The fix, and its one hard constraint.** Keep every row of the table in one
contiguous float32 matrix on disk, load it once per process, and let numpy do
the arithmetic. The constraint is that the ANSWER may not change: this is a
cache, and a cache that ranks differently from the thing it caches is not a
cache, it is a second search engine with its own bugs.

Bit-identical float arithmetic between numpy and Python is not available --
CPython's ``sum()`` compensates its rounding (3.12+) and numpy's reductions
do not, so the last bits of a 2048-term cosine differ. So this module does
not try to reproduce the number; it uses numpy only to NARROW, and then
computes the answer with the very functions the uncached path uses:

1. score every candidate row with a chunked float64 matmul -- fast, and
   accurate to ~1e-15 relative;
2. take a SUPERSET of the top ``k``: every row scoring within
   :data:`_SELECTION_MARGIN` of the ``k``-th, which is orders of magnitude
   wider than the error above, so the true top ``k`` cannot be outside it
   (nor can any row that EXACTLY ties the k-th, which the id tie-break has
   to see);
3. re-score exactly that superset with
   :func:`~trialerror.retrieve.vecsearch.cosine_similarity` and order it with
   :func:`~trialerror.retrieve.vecsearch.rank_by_query_vector`.

The returned list is therefore produced BY the uncached path's own ranking
function, on the same inputs, and is byte-identical to
``rank_by_query_vector(q, fetch_vectors(...))[:k]`` -- scores included.
``tests/test_retrieve_vecmatrix.py`` asserts exactly that equality on a
fixture rather than an approximation of it.

**numpy is optional.** It is not a declared dependency of this project
(:mod:`trialerror.retrieve.vecsearch`'s cosine is deliberately dependency-free),
so every entry point here returns ``None`` when it is absent and every
caller falls back to the path it used before. Same posture as sqlite-vec in
:mod:`trialerror.stores.vecindex`: the fast path is an optimisation, never a
requirement.

**And a cache never breaks a query.** That posture is absolute, not just
about numpy. ``search()`` and ``similar()`` were read-only operations before
this module existed, and they still are from the caller's point of view: the
on-disk write is BEST-EFFORT (:func:`_persist`), so a read-only volume, a
full one, or an index dir owned by another account serves the ranking out of
the matrix just built in memory and pays the build again next process rather
than raising. :func:`load_or_build` additionally swallows anything
``_build`` did not anticipate. No entry point here raises; ``None`` and a
slower correct answer are the only two outcomes.

**One invariant this module leans on.** On the matrix path the universe is
``vec_chunks__<key>``; on the uncached path it is ``chunk`` INTERSECT that
table. The two are the same set only because no vector row outlives its
chunk: ``ingest reindex-vectors`` repopulates from ``chunk JOIN emb``,
``ingest retract`` deletes a document's chunk rows and its ``vec_chunks__*``
rows in one transaction, ``ingest purge-embeddings`` only removes rows, and
nothing else in the tree deletes from ``chunk``. An orphan vector row would
be ranked here and then dropped when its chunk could not be read, returning
fewer than ``k`` results where the uncached path returned ``k`` -- so the
byte-identity claim above rests on that schema invariant as well as on this
code.

**Freshness is not a promise, it is a fingerprint.** The cache carries the
row count, the table's max rowid and the registry's ``created_ts`` for its
model key. Any difference and it is rebuilt on the spot. The two verbs that
rewrite a key's vectors (``ingest reindex-vectors``,
``ingest purge-embeddings``) also invalidate it explicitly, so the rebuild
does not wait for the next fingerprint mismatch to be noticed, and
``vecmatrix_stale`` reports a cache whose fingerprint has drifted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from trialerror.retrieve.vecsearch import rank_by_query_vector
from trialerror.stores.vecindex import (
    deserialize_vector_fallback,
    safe_model_key,
    try_load_sqlite_vec,
    vec_table_name,
)

__all__ = [
    "DEFAULT_MIN_UNIVERSE",
    "MATRIX_DIRNAME",
    "ResidentMatrix",
    "matrix_paths",
    "table_fingerprint",
    "cached_fingerprint",
    "matrix_status",
    "load_or_build",
    "top_ranked",
    "invalidate",
    "clear_process_cache",
    "numpy_available",
]

#: Below this many candidate rows the uncached path wins outright (a few
#: hundred Python cosines cost less than a fingerprint check plus a matrix
#: load), so the matrix is not engaged at all. Deliberately above the
#: full-text prefilter's own ceiling
#: (:data:`trialerror.retrieve.ftssearch.DEFAULT_FTS_CANDIDATE_LIMIT` = 500), so
#: the two-stage modes -- every ``auto``/``hybrid`` search there is -- keep
#: taking exactly the path they took before this module existed.
DEFAULT_MIN_UNIVERSE = 2_000

#: Subdirectory of the program's index dir holding one ``.npy`` +
#: ``.ids.json`` pair per model key.
MATRIX_DIRNAME = "vecmatrix"

#: How far below the k-th score a row may be and still be re-scored exactly.
#: Twelve orders of magnitude above the float64 matmul's own error, and
#: still tight enough that a normal query re-scores a handful of rows.
_SELECTION_MARGIN = 1e-9

#: Rows per float64 block in the scoring pass. 2,048 rows x 2,048 dims x 8
#: bytes is 32 MB of scratch -- big enough for BLAS to be worth calling,
#: small enough that a 3-million-row table never materialises as float64.
_BLOCK_ROWS = 2_048

#: One entry per (path, fingerprint) actually loaded in THIS process.
#: Keyed by fingerprint as well as path so a rebuild replaces rather than
#: shadows, and a stale entry can never be served.
_PROCESS_CACHE: dict[tuple[str, str], "ResidentMatrix"] = {}


def numpy_available() -> bool:
    """Whether the fast path is available here at all. Public because
    ``corpus_stats`` and the doctor both have to report the difference
    between "no cache" and "no numpy"."""
    return _numpy() is not None


def _numpy():
    try:
        import numpy  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - absent, or a broken build: both mean "no fast path"
        return None
    return numpy


@dataclass
class ResidentMatrix:
    """One model key's vectors, loaded once."""

    model_key: str
    chunk_ids: list[str]
    dims: int
    fingerprint: dict[str, Any]
    matrix: Any  # numpy.ndarray (rows, dims), float32, usually memory-mapped
    position: dict[str, int]
    _norms: Any = None

    @property
    def rows(self) -> int:
        return len(self.chunk_ids)

    def row_norms(self, np) -> Any:
        """L2 norm per row, float64, computed once per process. Used only to
        NARROW (the exact re-score recomputes its own norms in Python), so
        block-wise accumulation is fine here."""
        if self._norms is None:
            norms = np.empty(self.rows, dtype=np.float64)
            for start in range(0, self.rows, _BLOCK_ROWS):
                block = np.asarray(self.matrix[start : start + _BLOCK_ROWS], dtype=np.float64)
                norms[start : start + block.shape[0]] = np.sqrt((block * block).sum(axis=1))
            self._norms = norms
        return self._norms


# ---------------------------------------------------------------------------
# paths + fingerprint
# ---------------------------------------------------------------------------


def matrix_paths(program_root: Path | str, model_key: str, config: dict[str, Any] | None = None) -> tuple[Path, Path]:
    """``(<index_dir>/vecmatrix/<key>.npy, ...<key>.ids.json)``. The key is
    spelled by :func:`~trialerror.stores.vecindex.safe_model_key` -- the same
    sanitiser the table name uses, so one key never writes two files."""
    from trialerror.stores import paths as store_paths

    base = store_paths.program_index_dir(program_root, config) / MATRIX_DIRNAME
    safe = safe_model_key(model_key)
    return base / f"{safe}.npy", base / f"{safe}.ids.json"


def table_fingerprint(store, model_key: str) -> dict[str, Any] | None:
    """What the vector table looks like right now: ``{rows, max_rowid,
    created_ts, dims}``, or ``None`` when there is no table for this key (or
    it cannot be read on this connection -- a ``vec0`` table without the
    extension).

    Cheap by construction: two aggregates over one indexed column and one
    registry row, so a per-query freshness check costs nothing measurable.
    ``created_ts`` is in the fingerprint because a delete-and-refill rebuild
    can legitimately land on the same row count AND the same max rowid."""
    try_load_sqlite_vec(store.knowledge)
    table = vec_table_name(model_key)
    try:
        row = store.knowledge.execute(
            f"SELECT COUNT(*) AS rows, MAX(rowid) AS max_rowid FROM {table}"
        ).fetchone()
    except Exception:  # noqa: BLE001 - absent or unreadable table: no fingerprint, no cache
        return None
    if row is None:
        return None
    created_ts = None
    dims = None
    try:
        reg = store.knowledge.execute(
            "SELECT dims, created_ts FROM vec_index_registry WHERE model_key = ?", (model_key,)
        ).fetchone()
        if reg is not None:
            created_ts = reg["created_ts"]
            dims = int(reg["dims"])
    except Exception:  # noqa: BLE001 - a fresh program has no registry table yet
        pass
    return {
        "rows": int(row["rows"] or 0),
        "max_rowid": int(row["max_rowid"]) if row["max_rowid"] is not None else None,
        "created_ts": created_ts,
        "dims": dims,
    }


def _fingerprint_key(fingerprint: dict[str, Any] | None) -> str:
    return json.dumps(fingerprint or {}, sort_keys=True, default=str)


def cached_fingerprint(program_root: Path | str, model_key: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The fingerprint the cache on disk was built from, or ``None`` when
    there is no cache (or its sidecar is unreadable, which is treated as no
    cache -- never as an error)."""
    _npy, ids_path = matrix_paths(program_root, model_key, config)
    if not ids_path.is_file():
        return None
    try:
        return dict(json.loads(ids_path.read_text(encoding="utf-8")).get("fingerprint") or {})
    except Exception:  # noqa: BLE001 - a truncated sidecar is a cache miss
        return None


def matrix_status(store, model_key: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """What ``corpus_stats`` and the doctor report: whether a cache is
    present, whether its fingerprint still matches the table, and both
    fingerprints so a stale one can be read rather than guessed at."""
    live = table_fingerprint(store, model_key)
    cached = cached_fingerprint(store.program_root, model_key, config)
    npy_path, _ids = matrix_paths(store.program_root, model_key, config)
    return {
        "model_key": model_key,
        "numpy_available": numpy_available(),
        "present": bool(cached is not None and npy_path.is_file()),
        "path": str(npy_path),
        "fingerprint": cached,
        "table_fingerprint": live,
        "stale": bool(cached is not None and live is not None and cached != live),
    }


# ---------------------------------------------------------------------------
# build + load
# ---------------------------------------------------------------------------


def _persist(
    np,
    store,
    model_key: str,
    matrix: Any,
    chunk_ids: list[str],
    dims: int,
    fingerprint: dict[str, Any],
    config: dict[str, Any] | None,
) -> bool:
    """Write the cache to disk, BEST-EFFORT. ``True`` when it landed.

    A search was a read-only operation before this module existed, and it
    stays one: a program directory that cannot be written (a read-only or
    full volume, a mount without write access, an index dir owned by another
    account -- an MCP server or a reader process) must still answer, out of
    the in-memory matrix this build just produced, and pay the build again
    next process. The cache is an optimisation; the ONE thing it may never
    do is turn a query that worked into an error.

    Written through the same atomic-replace helper every other derived
    artifact in this codebase uses, so a crash mid-write leaves the OLD cache
    (or none), never a half matrix that would rank a truncated corpus."""
    import io

    from trialerror.util.atomic import atomic_write_bytes, atomic_write_text

    npy_path, ids_path = matrix_paths(store.program_root, model_key, config)
    try:
        npy_path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        np.save(buffer, matrix, allow_pickle=False)
        atomic_write_bytes(npy_path, buffer.getvalue())
        atomic_write_text(
            ids_path,
            json.dumps(
                {"model_key": model_key, "dims": int(dims), "fingerprint": fingerprint, "chunk_ids": chunk_ids},
                ensure_ascii=False,
            ),
        )
    except OSError:  # noqa: BLE001 - unwritable index dir, full volume: serve from memory
        # Leave nothing half-written behind: the sidecar is what
        # ``load_or_build`` trusts, so a .npy without it is a cache miss
        # anyway, but an .npy whose sidecar failed is dead weight.
        try:
            if not ids_path.is_file() and npy_path.is_file():
                npy_path.unlink()
        except OSError:
            pass
        return False
    return True


def _build(store, model_key: str, fingerprint: dict[str, Any], config: dict[str, Any] | None) -> "ResidentMatrix | None":
    np = _numpy()
    if np is None:
        return None
    table = vec_table_name(model_key)
    try:
        rows = store.knowledge.execute(
            f"SELECT rowid AS rid, chunk_id, vector FROM {table} ORDER BY rowid"
        ).fetchall()
    except Exception:  # noqa: BLE001 - unreadable table: no cache, caller falls back
        return None
    chunk_ids: list[str] = []
    vectors: list[list[float]] = []
    dims = None
    for row in rows:
        vector = deserialize_vector_fallback(row["vector"])
        if dims is None:
            dims = len(vector)
        elif len(vector) != dims:
            # A table whose rows disagree about width cannot become a
            # matrix. Refuse to cache it rather than pad or truncate --
            # ``ingest doctor``'s dims-conflict finding is the right place
            # for that, and the uncached path still answers.
            return None
        chunk_ids.append(row["chunk_id"])
        vectors.append(vector)
    if dims is None:
        dims = int(fingerprint.get("dims") or 0)

    matrix = np.asarray(vectors, dtype=np.float32) if vectors else np.zeros((0, dims), dtype=np.float32)
    _persist(np, store, model_key, matrix, chunk_ids, int(dims), fingerprint, config)
    return ResidentMatrix(
        model_key=model_key,
        chunk_ids=chunk_ids,
        dims=int(dims),
        fingerprint=dict(fingerprint),
        matrix=matrix,
        position={cid: i for i, cid in enumerate(chunk_ids)},
    )


def load_or_build(store, model_key: str, config: dict[str, Any] | None = None) -> "ResidentMatrix | None":
    """The resident matrix for ``model_key``: from this process's cache, from
    disk, or built now -- whichever is the first one whose fingerprint
    matches the table. ``None`` when numpy is absent, when there is no
    readable table, or when the table's rows disagree about width -- and
    never an exception: a build that cannot be written to disk returns its
    in-memory matrix anyway, and one that fails for any other reason returns
    ``None`` so the caller takes the path it took before this module."""
    np = _numpy()
    if np is None:
        return None
    fingerprint = table_fingerprint(store, model_key)
    if fingerprint is None:
        return None
    npy_path, ids_path = matrix_paths(store.program_root, model_key, config)
    cache_key = (str(npy_path), _fingerprint_key(fingerprint))
    resident = _PROCESS_CACHE.get(cache_key)
    if resident is not None:
        return resident

    if npy_path.is_file() and ids_path.is_file():
        try:
            sidecar = json.loads(ids_path.read_text(encoding="utf-8"))
            if dict(sidecar.get("fingerprint") or {}) == fingerprint:
                # ``mmap_mode`` keeps a multi-hundred-megabyte matrix out of
                # this process's heap: the scoring pass reads it in blocks,
                # so the pages it touches are the OS's page cache, shared
                # with every other process on the same program.
                matrix = np.load(npy_path, mmap_mode="r", allow_pickle=False)
                chunk_ids = [str(c) for c in sidecar.get("chunk_ids") or []]
                if matrix.shape[0] == len(chunk_ids):
                    resident = ResidentMatrix(
                        model_key=model_key,
                        chunk_ids=chunk_ids,
                        dims=int(sidecar.get("dims") or (matrix.shape[1] if matrix.ndim == 2 else 0)),
                        fingerprint=dict(fingerprint),
                        matrix=matrix,
                        position={cid: i for i, cid in enumerate(chunk_ids)},
                    )
        except Exception:  # noqa: BLE001 - any unreadable cache is a cache miss, never an error
            resident = None

    if resident is None:
        try:
            resident = _build(store, model_key, fingerprint, config)
        except Exception:  # noqa: BLE001 - the contract is None, never an exception (see below)
            # LAST LINE of the "a cache never breaks a query" contract this
            # module's docstring states. ``_build`` guards what it knows how
            # to fail at (an unreadable table, an unwritable cache dir); this
            # catches whatever it does not -- a MemoryError on a matrix too
            # big for this process, a numpy build that raises on save -- and
            # turns it into the same fallback every other decline takes.
            resident = None
    if resident is not None:
        _PROCESS_CACHE[cache_key] = resident
    return resident


def invalidate(program_root: Path | str, model_key: str, config: dict[str, Any] | None = None) -> bool:
    """Remove the cache for ``model_key`` (files and this process's entry).
    ``True`` when something was actually removed.

    Called by the two verbs that rewrite a key's vectors. The fingerprint
    would catch those anyway; doing it explicitly means the very next query
    is correct AND fast, instead of correct and paying for a rebuild it
    could have had queued a second earlier."""
    npy_path, ids_path = matrix_paths(program_root, model_key, config)
    removed = False
    for path in (npy_path, ids_path):
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError:
            # A cache file that cannot be deleted (locked on Windows, a
            # read-only mount) must not fail the rebuild it was clearing
            # for: the fingerprint check makes a stale file harmless.
            pass
    for key in [k for k in _PROCESS_CACHE if k[0] == str(npy_path)]:
        _PROCESS_CACHE.pop(key, None)
        removed = True
    return removed


def clear_process_cache() -> None:
    """Drop every in-process entry. For tests, and for a long-lived server
    that wants to release the mappings."""
    _PROCESS_CACHE.clear()


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def top_ranked(
    store,
    model_key: str,
    query_vector: Sequence[float],
    *,
    k: int,
    restrict: Iterable[str] | None = None,
    exclude: str | None = None,
    min_universe: int = DEFAULT_MIN_UNIVERSE,
    config: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, float]], int] | None:
    """``(ranked, n_scored)`` -- the top ``k`` ``(chunk_id, score)`` pairs,
    byte-identical to ``rank_by_query_vector(query_vector,
    fetch_vectors(store, model_key, universe))[:k]``, plus how many rows were
    scored (the same number the uncached path would report as
    ``stats.vector_scored``).

    ``None`` means "not applicable here, use the uncached path": numpy
    absent, no readable table, a query vector of the wrong width, a universe
    below ``min_universe``, or an empty one.

    ``restrict`` limits the universe to those chunk ids (ids with no vector
    are simply absent, exactly as ``fetch_vectors`` treats them);
    ``exclude`` drops one id (``similar()``'s "never rank a chunk against
    itself").
    """
    np = _numpy()
    if np is None:
        return None
    resident = load_or_build(store, model_key, config)
    if resident is None or resident.rows == 0:
        return None
    if len(query_vector) != resident.dims:
        return None

    if restrict is None:
        rows = np.arange(resident.rows)
        if exclude is not None and exclude in resident.position:
            rows = np.delete(rows, resident.position[exclude])
    else:
        wanted = [
            resident.position[cid]
            for cid in dict.fromkeys(restrict)
            if cid in resident.position and cid != exclude
        ]
        rows = np.asarray(wanted, dtype=np.int64)
    n_scored = int(rows.shape[0])
    if n_scored == 0 or n_scored < min_universe:
        return None

    query64 = np.asarray(query_vector, dtype=np.float64)
    query_norm = float(np.sqrt((query64 * query64).sum()))
    norms = resident.row_norms(np)[rows]
    scores = np.empty(n_scored, dtype=np.float64)
    for start in range(0, n_scored, _BLOCK_ROWS):
        index_block = rows[start : start + _BLOCK_ROWS]
        block = np.asarray(resident.matrix[index_block], dtype=np.float64)
        scores[start : start + index_block.shape[0]] = block @ query64
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where((norms > 0) & (query_norm > 0), scores / (norms * query_norm), 0.0)

    k_wanted = max(int(k), 0)
    if k_wanted == 0:
        return [], n_scored
    take = min(k_wanted, n_scored)
    # argpartition gives the k best in O(n); the margin then widens the
    # selection to everything that could tie or beat them once the exact
    # cosine is recomputed (see the module docstring).
    partition = np.argpartition(-scores, take - 1)[:take]
    threshold = float(scores[partition].min()) - _SELECTION_MARGIN
    superset = np.nonzero(scores >= threshold)[0]

    vectors = {
        resident.chunk_ids[int(rows[i])]: [float(v) for v in np.asarray(resident.matrix[int(rows[i])], dtype=np.float64)]
        for i in superset
    }
    ranked = rank_by_query_vector(query_vector, vectors)[:k_wanted]
    return ranked, n_scored
