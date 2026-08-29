"""Doc-pooled embedding vectors. Design Section 9.6 stratification is
specified over "document embeddings", but M7/M8 only ship CHUNK-level
vectors (``vec_chunks__<model_key>`` / ``trialerror.retrieve.vecsearch.fetch_vectors``,
keyed by ``chunk_id``) — Section 4.1's ``document`` table has no vector
column of its own.

TRIALERROR-DEV-NOTE (integration contract, stated in this build's brief):
"if the design expects doc-level vectors and M7 only shipped chunk-level,
pool them mean+L2-renorm as the origin-project convention does". This module is that
pooling step: every chunk belonging to a document is fetched via M8's own
``fetch_vectors`` (so both the real ``sqlite-vec`` backend and the
pure-stdlib fallback are handled uniformly — see that function's own
docstring), then mean-pooled element-wise and L2-renormalized. No new
numerical dependency (plain Python, matching
``trialerror.retrieve.vecsearch.cosine_similarity``'s own "no numpy" convention
— this build's lane has no license to add one either).
"""

from __future__ import annotations

import math
from typing import Sequence

from trialerror.retrieve.vecsearch import fetch_vectors
from trialerror.stores.store import Store

__all__ = ["mean_pool_l2", "fetch_doc_vector", "fetch_doc_vectors"]


def mean_pool_l2(vectors: Sequence[Sequence[float]]) -> list[float] | None:
    """Element-wise mean of ``vectors``, then L2-renormalized to unit
    length. Returns ``None`` for an empty input (a document with zero
    embedded chunks — "no vector" is a valid, non-error outcome a caller
    decides how to handle, mirroring ``fetch_vectors``'s own "missing ids
    are simply absent" contract) or when every vector mean-pools to the
    zero vector (degenerate fixture data)."""
    if not vectors:
        return None
    dims = len(vectors[0])
    sums = [0.0] * dims
    for vec in vectors:
        if len(vec) != dims:
            raise ValueError(
                f"mean_pool_l2: inconsistent vector dims ({len(vec)} != {dims}) — "
                "chunks embedded under different model_keys were mixed"
            )
        for i, v in enumerate(vec):
            sums[i] += v
    n = float(len(vectors))
    mean = [s / n for s in sums]
    norm = math.sqrt(sum(x * x for x in mean))
    if norm == 0.0:
        return None
    return [x / norm for x in mean]


def _doc_chunk_ids(store: Store, doc_id: str) -> list[str]:
    rows = store.knowledge.execute(
        "SELECT chunk_id FROM chunk WHERE doc_id = ? ORDER BY seq ASC, chunk_id ASC", (doc_id,)
    ).fetchall()
    return [r["chunk_id"] for r in rows]


def fetch_doc_vector(store: Store, *, model_key: str, doc_id: str) -> list[float] | None:
    """The doc-pooled (mean+L2-renorm) vector for one document, or ``None``
    if it has no chunks or none of its chunks have a vector under
    ``model_key``."""
    chunk_ids = _doc_chunk_ids(store, doc_id)
    if not chunk_ids:
        return None
    by_chunk = fetch_vectors(store, model_key, chunk_ids)
    if not by_chunk:
        return None
    # Deterministic order: chunk_ids as returned by _doc_chunk_ids (seq
    # order), filtered to those that actually have a vector.
    ordered = [by_chunk[cid] for cid in chunk_ids if cid in by_chunk]
    return mean_pool_l2(ordered)


def fetch_doc_vectors(store: Store, *, model_key: str, doc_ids: Sequence[str]) -> dict[str, list[float]]:
    """:func:`fetch_doc_vector` for many documents at once. A document with
    no resolvable vector is simply absent from the result (never an error —
    same "missing is absent" contract ``fetch_vectors`` itself uses);
    callers that need every candidate scoreable check for absence
    themselves (``trialerror.lens.stratify`` raises
    :class:`~trialerror.lens.errors.MissingEmbeddingError` for exactly that)."""
    out: dict[str, list[float]] = {}
    for doc_id in dict.fromkeys(doc_ids):  # dedupe, stable order
        vec = fetch_doc_vector(store, model_key=model_key, doc_id=doc_id)
        if vec is not None:
            out[doc_id] = vec
    return out
