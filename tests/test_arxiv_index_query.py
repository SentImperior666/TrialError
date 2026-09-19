"""Unit tests for :mod:`trialerror.arxiv_index.query` -- native-MATCH vs
brute-force correctness, including one real-3072-dims exercise (build
brief item 7: "at least one test at real 3072 dims")."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.arxiv_index.ingest import build_index_from_zip
from trialerror.arxiv_index.query import current_backend, semantic_search, semantic_search_bruteforce, semantic_search_native
from trialerror.arxiv_index.store import VecBackend, ensure_schema
from tests._arxiv_index_fixtures import deterministic_vector, make_record, write_records_zip


def _sqlite_vec_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        from trialerror.stores.vecindex import try_load_sqlite_vec

        return try_load_sqlite_vec(conn)
    finally:
        conn.close()


def _build_fixture_conn(tmp_path, *, dims: int, n: int, force_fallback: bool, monkeypatch):
    if force_fallback:
        import trialerror.arxiv_index.store as store_mod

        monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)

    zip_path = tmp_path / "fixture.zip"
    records = [make_record(i, dims=dims) for i in range(n)]
    write_records_zip(zip_path, {"shard-0000.jsonl": records})

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    build_index_from_zip(conn, zip_path, dims=dims)
    return conn


@pytest.mark.parametrize("force_fallback", [True, False], ids=["fallback-backend", "sqlite-vec-backend-if-available"])
def test_semantic_search_finds_exact_match_as_top_hit(tmp_path, force_fallback, monkeypatch):
    if not force_fallback and not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    conn = _build_fixture_conn(tmp_path, dims=8, n=15, force_fallback=force_fallback, monkeypatch=monkeypatch)

    expected_backend = VecBackend.FALLBACK if force_fallback else VecBackend.SQLITE_VEC
    assert current_backend(conn) == expected_backend

    query_vector = deterministic_vector(7, 8)  # exact copy of record 7's own vector
    results = semantic_search(conn, query_vector, k=5)
    assert len(results) == 5
    assert results[0].arxiv_id == "9999.00007"
    assert results[0].title == "Synthetic Paper 7"
    # best-first: subsequent scores are non-increasing
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_native_match_and_bruteforce_agree_on_top_k_arxiv_ids(tmp_path, monkeypatch):
    """Build brief item 7: "native-MATCH correctness vs brute-force cosine
    on the fixture." The vec0 table only has (arxiv_id, embedding) columns
    -- :func:`semantic_search_bruteforce` reads the FALLBACK table's own
    (dims, vector) shape, so it isn't callable against a vec0-backed db
    directly; this test instead fetches the same rows from the vec0 table
    and computes brute-force cosine independently in Python (reusing the
    same :func:`cosine_similarity` :func:`semantic_search_bruteforce`
    itself calls), then compares against :func:`semantic_search_native`'s
    output -- the actual ground-truth-vs-native comparison the build brief
    asks for."""
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    conn = _build_fixture_conn(tmp_path, dims=16, n=30, force_fallback=False, monkeypatch=monkeypatch)
    assert current_backend(conn) == VecBackend.SQLITE_VEC

    query_vector = deterministic_vector(3, 16)
    native = semantic_search_native(conn, query_vector, k=10)

    from trialerror.arxiv_index.store import deserialize_vector_fallback
    from trialerror.retrieve.vecsearch import cosine_similarity

    rows = conn.execute("SELECT arxiv_id, embedding FROM arxiv_vec").fetchall()
    scored = [(r["arxiv_id"], cosine_similarity(query_vector, deserialize_vector_fallback(r["embedding"]))) for r in rows]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    brute_ids = [aid for aid, _ in scored[:10]]

    native_ids = [r.arxiv_id for r in native]
    # sqlite-vec's default distance metric is L2, brute-force here is
    # cosine -- on unit-normalized vectors (deterministic_vector already
    # L2-normalizes) L2 ranking and cosine ranking produce the IDENTICAL
    # ordering (L2^2 = 2 - 2*cosine for unit vectors), so the top-k id SET
    # and ORDER must agree exactly.
    assert native_ids == brute_ids


def test_semantic_search_returns_fewer_than_k_when_corpus_smaller_than_k(tmp_path, monkeypatch):
    conn = _build_fixture_conn(tmp_path, dims=8, n=3, force_fallback=True, monkeypatch=monkeypatch)
    results = semantic_search(conn, deterministic_vector(0, 8), k=10)
    assert len(results) == 3


def test_semantic_search_at_real_3072_dims(tmp_path, monkeypatch):
    """Build brief item 7: "at least one test at real 3072 dims" -- proves
    the whole ingest+query path (schema, serialization, native-MATCH OR
    brute-force ranking) actually works at the real dataset's real width,
    not just small fixture dims."""
    conn = _build_fixture_conn(tmp_path, dims=3072, n=6, force_fallback=True, monkeypatch=monkeypatch)
    query_vector = deterministic_vector(2, 3072)
    results = semantic_search(conn, query_vector, k=3)
    assert len(results) == 3
    assert results[0].arxiv_id == "9999.00002"


@pytest.mark.skipif(not _sqlite_vec_available(), reason="sqlite-vec extension not installed in this environment")
def test_semantic_search_native_at_real_3072_dims():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=3072)
    from trialerror.arxiv_index.store import serialize_vector_fallback

    for i in range(5):
        vec = deterministic_vector(i, 3072)
        conn.execute(
            "INSERT INTO arxiv_vec(arxiv_id, embedding) VALUES (?, ?)", (f"real.{i}", serialize_vector_fallback(vec))
        )
    conn.commit()
    results = semantic_search_native(conn, deterministic_vector(4, 3072), k=2)
    assert results[0].arxiv_id == "real.4"


def test_current_backend_defaults_to_fallback_when_state_absent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert current_backend(conn) == VecBackend.FALLBACK
