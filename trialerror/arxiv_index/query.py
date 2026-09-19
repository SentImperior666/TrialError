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
from typing import Any

from trialerror.arxiv_index.store import META_TABLE_NAME, VEC_TABLE_NAME, VecBackend, deserialize_vector_fallback, get_build_state
from trialerror.retrieve.vecsearch import cosine_similarity
from trialerror.stores.vecindex import serialize_vector_fallback

__all__ = [
    "SemanticSearchResult",
    "current_backend",
    "semantic_search",
    "semantic_search_native",
    "semantic_search_bruteforce",
]


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
