"""Unit tests for :mod:`trialerror.retrieve.vecsearch` -- the vector tier.
Exercises BOTH ``vec_chunks__*`` backends (the CRITICAL RULE this build's
brief names verbatim: sqlite-vec's loadable extension is per-connection,
and a fresh connection must call ``try_load_sqlite_vec`` before it can even
recognize a ``vec0``-backed table)."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.retrieve.vecsearch import (
    cosine_similarity,
    fetch_native_knn,
    fetch_vectors,
    rank_by_query_vector,
    vec_backend_for,
    vec_table_exists,
)
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, try_load_sqlite_vec


def _sqlite_vec_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return try_load_sqlite_vec(conn)
    finally:
        conn.close()


class _KnowledgeOnlyStore:
    """Same duck-typing seam ``trialerror.retrieve.checks`` uses -- these
    functions only ever read ``store.knowledge``."""

    def __init__(self, knowledge_conn):
        self.knowledge = knowledge_conn


def test_cosine_similarity_identical_vectors_is_one():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors_is_negative_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_degenerate_inputs_never_raise():
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0  # mismatched length


def test_rank_by_query_vector_best_first_with_deterministic_ties():
    vectors = {"B": [1.0, 0.0], "A": [1.0, 0.0], "C": [0.0, 1.0]}
    ranked = rank_by_query_vector([1.0, 0.0], vectors)
    assert [cid for cid, _ in ranked] == ["A", "B", "C"]  # A/B tie on score, break on id


def test_vec_table_exists_false_before_any_index_created(store):
    assert vec_table_exists(store, "model-that-was-never-indexed") is False


def test_fetch_vectors_empty_input_returns_empty_dict(store):
    assert fetch_vectors(store, "any-model", []) == {}


@pytest.mark.parametrize("force_fallback", [True, False], ids=["fallback-backend", "sqlite-vec-backend-if-available"])
def test_fetch_vectors_roundtrips_both_backends(store, force_fallback, monkeypatch):
    if not force_fallback and not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    if not force_fallback:
        monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")  # B.4a: vec0 is opt-in now

    model_key = "test-model"
    dims = 4
    backend = ensure_vec_table(store.knowledge, model_key, dims, _force_fallback=force_fallback)
    expected_backend = VecBackend.FALLBACK if force_fallback else VecBackend.SQLITE_VEC
    assert backend == expected_backend

    from trialerror.stores.vecindex import vec_table_name

    table = vec_table_name(model_key)
    vectors = {"CHK-1": [0.1, 0.2, 0.3, 0.4], "CHK-2": [0.5, -0.5, 0.5, -0.5]}
    for chunk_id, vec in vectors.items():
        blob = serialize_vector_fallback(vec)
        if backend == VecBackend.SQLITE_VEC:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, blob))
        else:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", (chunk_id, model_key, dims, blob))
    store.knowledge.commit()

    assert vec_table_exists(store, model_key) is True

    fetched = fetch_vectors(store, model_key, ["CHK-1", "CHK-2", "CHK-does-not-exist"])
    assert set(fetched) == {"CHK-1", "CHK-2"}
    assert fetched["CHK-1"] == pytest.approx(vectors["CHK-1"])
    assert fetched["CHK-2"] == pytest.approx(vectors["CHK-2"])


def test_fetch_vectors_on_a_fresh_connection_that_never_loaded_the_extension(program_root, platform_root, monkeypatch):
    """The CRITICAL RULE, proven directly: build the sqlite-vec-backed
    table on one connection, then query it through a SEPARATE, freshly
    opened Store/connection that has never called ``try_load_sqlite_vec``
    itself -- :func:`fetch_vectors` must load it internally rather than
    relying on the caller to have done so."""
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")  # B.4a: vec0 is opt-in now
    from trialerror.stores.store import open_store
    from trialerror.stores.vecindex import vec_table_name

    model_key = "fresh-conn-model"
    dims = 4

    writer_store = open_store(program_root, platform_root=platform_root)
    backend = ensure_vec_table(writer_store.knowledge, model_key, dims)
    assert backend == VecBackend.SQLITE_VEC
    table = vec_table_name(model_key)
    import sqlite_vec

    writer_store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", ("CHK-1", sqlite_vec.serialize_float32([0.1, 0.2, 0.3, 0.4])))
    writer_store.knowledge.commit()
    writer_store.close()

    # a brand-new Store -> brand-new sqlite3.Connection to the SAME file,
    # which has never loaded the extension.
    reader_store = open_store(program_root, platform_root=platform_root)
    try:
        assert vec_table_exists(reader_store, model_key) is True
        fetched = fetch_vectors(reader_store, model_key, ["CHK-1"])
        assert fetched["CHK-1"] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    finally:
        reader_store.close()


# ---------------------------------------------------------------------------
# B.4b native-MATCH wiring (build-arxiv-kaggle-index session):
# vec_backend_for + fetch_native_knn
# ---------------------------------------------------------------------------


def test_vec_backend_for_none_when_table_never_created(store):
    assert vec_backend_for(store, "never-indexed-model") is None


def test_vec_backend_for_reports_fallback(store):
    ensure_vec_table(store.knowledge, "fb-model", 4, _force_fallback=True)
    assert vec_backend_for(store, "fb-model") == VecBackend.FALLBACK


def test_vec_backend_for_reports_sqlite_vec(store, monkeypatch):
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")
    ensure_vec_table(store.knowledge, "sv-model", 4)
    assert vec_backend_for(store, "sv-model") == VecBackend.SQLITE_VEC


def test_fetch_native_knn_ranks_best_first_and_matches_exact_vector(store, monkeypatch):
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")
    model_key = "native-knn-model"
    dims = 4
    backend = ensure_vec_table(store.knowledge, model_key, dims)
    assert backend == VecBackend.SQLITE_VEC

    from trialerror.stores.vecindex import vec_table_name

    table = vec_table_name(model_key)
    vectors = {
        "CHK-A": [1.0, 0.0, 0.0, 0.0],
        "CHK-B": [0.9, 0.1, 0.0, 0.0],
        "CHK-C": [0.0, 1.0, 0.0, 0.0],
    }
    for chunk_id, vec in vectors.items():
        store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, serialize_vector_fallback(vec)))
    store.knowledge.commit()

    ranked = fetch_native_knn(store, model_key, [1.0, 0.0, 0.0, 0.0], k=3)
    assert [cid for cid, _ in ranked] == ["CHK-A", "CHK-B", "CHK-C"]
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)  # higher = better, same convention as rank_by_query_vector


def test_fetch_native_knn_excludes_given_chunk_id(store, monkeypatch):
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")
    model_key = "native-knn-exclude-model"
    dims = 4
    ensure_vec_table(store.knowledge, model_key, dims)
    from trialerror.stores.vecindex import vec_table_name

    table = vec_table_name(model_key)
    vectors = {
        "CHK-SELF": [1.0, 0.0, 0.0, 0.0],
        "CHK-B": [0.9, 0.1, 0.0, 0.0],
        "CHK-C": [0.0, 1.0, 0.0, 0.0],
    }
    for chunk_id, vec in vectors.items():
        store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, serialize_vector_fallback(vec)))
    store.knowledge.commit()

    ranked = fetch_native_knn(store, model_key, [1.0, 0.0, 0.0, 0.0], k=2, exclude_chunk_id="CHK-SELF")
    ids = [cid for cid, _ in ranked]
    assert "CHK-SELF" not in ids
    assert ids == ["CHK-B", "CHK-C"]


def test_fetch_native_knn_zero_k_returns_empty(store, monkeypatch):
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")
    ensure_vec_table(store.knowledge, "zero-k-model", 4)
    assert fetch_native_knn(store, "zero-k-model", [1.0, 0.0, 0.0, 0.0], k=0) == []
