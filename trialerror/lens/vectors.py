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

from typing import Any, Sequence

from trialerror.retrieve.vecsearch import fetch_vectors
from trialerror.stores.store import Store
from trialerror.util import vecmath

__all__ = ["mean_pool_l2", "fetch_doc_vector", "fetch_doc_vectors", "program_config"]


def program_config(store: Store, config: Any = None) -> Any:
    """``config`` if the caller handed one, else this program's own
    ``trialerror.toml`` read off the store.

    **Why a function that a caller could have written inline** (lane FB-7
    fix pass, V-1). ``[retrieve] numpy_fastpath = "off"`` is documented as
    the one line that turns the fast path off for a whole program, and it is
    read from a config MAPPING that the caller supplies. Every lens entry
    point used to supply ``None``, so the knob fell through to the process
    environment and the program's own file was never consulted -- ``off``
    turned nothing off for three of the four families ``USER_SETUP`` names
    under it. A function that resolves the store's config when nobody passed
    one means a call site cannot silently opt out of the knob by forgetting
    a keyword: forgetting it now reads the file.

    An explicitly passed mapping always wins, including an empty one -- a
    caller that has already resolved the config (or that deliberately wants
    the code-level default) is not overruled."""
    if config is not None:
        return config
    from trialerror.retrieve import engine as retrieve_engine

    return retrieve_engine._load_program_config(store)


def mean_pool_l2(vectors: Sequence[Sequence[float]], *, config: Any = None) -> list[float] | None:
    """Element-wise mean of ``vectors``, then L2-renormalized to unit
    length. Returns ``None`` for an empty input (a document with zero
    embedded chunks — "no vector" is a valid, non-error outcome a caller
    decides how to handle, mirroring ``fetch_vectors``'s own "missing ids
    are simply absent" contract) or when every vector mean-pools to the
    zero vector (degenerate fixture data).

    :func:`trialerror.util.vecmath.mean_pool_l2` does the summing — a plain
    Python loop for the handful of chunks a normal document has, a blocked
    float64 accumulation for a book-length one. A document whose chunks
    disagree about width still raises ``ValueError`` on both paths: chunks
    embedded under two model keys are not a pool to average, and padding
    them would be a third vector space."""
    return vecmath.mean_pool_l2(vectors, config=config)


def _doc_chunk_ids(store: Store, doc_id: str) -> list[str]:
    rows = store.knowledge.execute(
        "SELECT chunk_id FROM chunk WHERE doc_id = ? ORDER BY seq ASC, chunk_id ASC", (doc_id,)
    ).fetchall()
    return [r["chunk_id"] for r in rows]


def fetch_doc_vector(
    store: Store, *, model_key: str, doc_id: str, config: Any = None
) -> list[float] | None:
    """The doc-pooled (mean+L2-renorm) vector for one document, or ``None``
    if it has no chunks or none of its chunks have a vector under
    ``model_key``.

    ``config`` is the program config the pooling scan reads
    ``[retrieve] numpy_fastpath`` from; omitted, it is read off the store
    (:func:`program_config`), so a program that turned the fast path off has
    turned it off here."""
    chunk_ids = _doc_chunk_ids(store, doc_id)
    if not chunk_ids:
        return None
    by_chunk = fetch_vectors(store, model_key, chunk_ids)
    if not by_chunk:
        return None
    # Deterministic order: chunk_ids as returned by _doc_chunk_ids (seq
    # order), filtered to those that actually have a vector.
    ordered = [by_chunk[cid] for cid in chunk_ids if cid in by_chunk]
    return mean_pool_l2(ordered, config=program_config(store, config))


def fetch_doc_vectors(
    store: Store, *, model_key: str, doc_ids: Sequence[str], config: Any = None
) -> dict[str, list[float]]:
    """:func:`fetch_doc_vector` for many documents at once. A document with
    no resolvable vector is simply absent from the result (never an error —
    same "missing is absent" contract ``fetch_vectors`` itself uses);
    callers that need every candidate scoreable check for absence
    themselves (``trialerror.lens.stratify`` raises
    :class:`~trialerror.lens.errors.MissingEmbeddingError` for exactly that)."""
    # Resolved ONCE for the whole batch rather than per document: the knob
    # is a property of the program, and re-reading trialerror.toml for every
    # document would make a read of a hundred documents a hundred file reads.
    resolved = program_config(store, config)
    out: dict[str, list[float]] = {}
    for doc_id in dict.fromkeys(doc_ids):  # dedupe, stable order
        vec = fetch_doc_vector(store, model_key=model_key, doc_id=doc_id, config=resolved)
        if vec is not None:
            out[doc_id] = vec
    return out
