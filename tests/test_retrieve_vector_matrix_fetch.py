"""``fetch_vector_matrix`` and the batched table scan behind it (lane FB-7
item 1).

Two claims are tested here and neither is about speed. First, the matrix a
caller gets back holds exactly the rows ``fetch_vectors`` would have
returned, with the same values -- the fast path may not quietly serve a
different corpus. Second, the scan is BATCHED: an unbounded caller hands
this module every chunk id in the program, and a single ``IN (...)`` over a
hundred thousand of them is an ``OperationalError`` from SQLite rather than
a slow query. The batching test drives the number of ids past a deliberately
tiny bind window so the loop has to run more than once.
"""

from __future__ import annotations

import pytest

from tests._retrieve_fixtures import build_bulk_corpus
from trialerror.retrieve import vecsearch
from trialerror.util import vecmath

_DIMS = 16


@pytest.fixture()
def bulk(store):
    return build_bulk_corpus(store, n_chunks=900, n_docs=3, dims=_DIMS)


def test_matrix_holds_exactly_what_fetch_vectors_holds(store, bulk):
    ids = list(bulk["chunk_ids"])
    model_key = bulk["model_key"]
    by_chunk = vecsearch.fetch_vectors(store, model_key, ids)
    fetched = vecsearch.fetch_vector_matrix(store, model_key, ids)
    if fetched is None:
        pytest.skip("no numpy here; the matrix path declines and callers use fetch_vectors")
    matrix_ids, matrix = fetched
    assert set(matrix_ids) == set(by_chunk)
    assert matrix.shape == (len(matrix_ids), _DIMS)
    for row_index, chunk_id in enumerate(matrix_ids):
        assert [float(v) for v in matrix[row_index]] == by_chunk[chunk_id]


def test_matrix_scan_is_batched_past_the_bind_window(store, bulk, monkeypatch):
    """A tiny bind window forces many round trips; the answer may not move."""
    monkeypatch.setattr(vecsearch, "_ID_BIND_CHUNK", 7)
    ids = list(bulk["chunk_ids"])
    model_key = bulk["model_key"]
    by_chunk = vecsearch.fetch_vectors(store, model_key, ids)
    assert len(by_chunk) == len(ids)
    fetched = vecsearch.fetch_vector_matrix(store, model_key, ids)
    if fetched is None:
        pytest.skip("no numpy here")
    matrix_ids, matrix = fetched
    assert len(matrix_ids) == len(ids)
    for row_index, chunk_id in enumerate(matrix_ids):
        assert [float(v) for v in matrix[row_index]] == by_chunk[chunk_id]


def test_matrix_decode_is_blocked(store, bulk, monkeypatch):
    """Past the decode's own row block, too -- the module's memory bound is
    only a bound if the loop actually runs."""
    monkeypatch.setattr(vecmath, "MAX_BLOCK_ROWS", 11)
    monkeypatch.setattr(vecmath, "_BLOCK_BYTES", _DIMS * 4 * 11)
    ids = list(bulk["chunk_ids"])
    fetched = vecsearch.fetch_vector_matrix(store, bulk["model_key"], ids)
    if fetched is None:
        pytest.skip("no numpy here")
    matrix_ids, matrix = fetched
    by_chunk = vecsearch.fetch_vectors(store, bulk["model_key"], ids)
    assert matrix.shape[0] == len(matrix_ids) == len(by_chunk)
    for row_index, chunk_id in enumerate(matrix_ids):
        assert [float(v) for v in matrix[row_index]] == by_chunk[chunk_id]


def test_matrix_declines_when_the_knob_is_off(store, bulk):
    assert (
        vecsearch.fetch_vector_matrix(
            store, bulk["model_key"], list(bulk["chunk_ids"]), config={"retrieve": {"numpy_fastpath": "off"}}
        )
        is None
    )


def test_matrix_declines_on_an_empty_or_unknown_id_set(store, bulk):
    assert vecsearch.fetch_vector_matrix(store, bulk["model_key"], []) is None
    assert vecsearch.fetch_vector_matrix(store, bulk["model_key"], ["no-such-chunk"]) is None
    assert vecsearch.fetch_vector_matrix(store, "no-such-model-key", list(bulk["chunk_ids"])) is None


def test_fetch_vectors_survives_more_ids_than_sqlite_binds(store, bulk):
    """The batching is not decoration. SQLite's parameter ceiling is 999 on
    an older build; an unbounded caller passes every chunk id there is."""
    ids = list(bulk["chunk_ids"])
    padded = ids + [f"absent-{i:05d}" for i in range(2_000)]
    by_chunk = vecsearch.fetch_vectors(store, bulk["model_key"], padded)
    assert len(by_chunk) == len(ids)
    assert set(by_chunk) == set(ids)


def test_rank_by_query_vector_with_k_matches_the_unsliced_ranking(store, bulk):
    """``k=`` is an optimisation hint, never a different answer."""
    ids = list(bulk["chunk_ids"])
    vectors = vecsearch.fetch_vectors(store, bulk["model_key"], ids)
    query = vectors[ids[0]]
    full = vecsearch.rank_by_query_vector(query, vectors)
    for k in (1, 5, 20, len(ids)):
        assert vecsearch.rank_by_query_vector(query, vectors, k=k) == full[:k]
        assert (
            vecsearch.rank_by_query_vector(
                query, vectors, k=k, config={"retrieve": {"numpy_fastpath": "off"}}
            )
            == full[:k]
        )
