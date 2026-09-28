"""Unit tests for :mod:`trialerror.arxiv_index.query` -- native-MATCH vs
brute-force correctness, including one real-3072-dims exercise (build
brief item 7: "at least one test at real 3072 dims")."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.arxiv_index.ingest import build_index_from_zip
from trialerror.arxiv_index.query import (
    current_backend,
    semantic_search,
    semantic_search_bruteforce,
    semantic_search_many,
    semantic_search_native,
)
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


# ---------------------------------------------------------------------------
# lane FB-acq item 3: semantic_search_many -- Q queries, ONE pass
#
# vec0 has no approximate index: a KNN MATCH is an exhaustive scan of every
# stored vector, so Q queries were Q full passes over a tens-of-GB table. The
# fix is not a resident process (Q passes would still be Q passes) -- it is
# making Q queries cost one.
# ---------------------------------------------------------------------------


def _many_backends():
    return [
        pytest.param(True, id="fallback-backend"),
        pytest.param(False, id="sqlite-vec-backend-if-available"),
    ]


@pytest.mark.parametrize("force_fallback", _many_backends())
def test_semantic_search_many_returns_exactly_what_the_single_query_path_returns(
    tmp_path, force_fallback, monkeypatch
):
    """The identity that makes this a performance change and not a different
    answer: same ids, same order, same scores, for every query."""
    if not force_fallback and not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    conn = _build_fixture_conn(tmp_path, dims=16, n=40, force_fallback=force_fallback, monkeypatch=monkeypatch)
    queries = [deterministic_vector(i, 16) for i in (1, 3, 7, 20, 31)]

    batch = semantic_search_many(conn, queries, k=5)

    assert batch["scan_mode"] == "single_pass"
    assert batch["passes"] == 1
    assert batch["rows_scanned"] == 40
    assert batch["backend"] == ("fallback" if force_fallback else "sqlite_vec")
    assert "skipped_rows" not in batch
    assert len(batch["results"]) == len(queries)
    for query, got in zip(queries, batch["results"]):
        expected = semantic_search(conn, query, k=5)
        assert [r.arxiv_id for r in got] == [r.arxiv_id for r in expected]
        for a, b in zip(got, expected):
            assert a.score == pytest.approx(b.score, abs=1e-5)
            assert a.title == b.title and a.doi == b.doi and a.categories == b.categories


@pytest.mark.parametrize("force_fallback", _many_backends())
def test_semantic_search_many_scans_the_table_exactly_once(tmp_path, force_fallback, monkeypatch):
    """FAILS BEFORE this lane: there was no batch entry point at all, and 8
    queries meant 8 full-table statements."""
    if not force_fallback and not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    conn = _build_fixture_conn(tmp_path, dims=16, n=40, force_fallback=force_fallback, monkeypatch=monkeypatch)
    column = "vector" if force_fallback else "embedding"
    full_scan = f"SELECT arxiv_id, {column} FROM arxiv_vec"
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        batch = semantic_search_many(conn, [deterministic_vector(i, 16) for i in range(8)], k=4)
    finally:
        conn.set_trace_callback(None)

    assert batch["passes"] == 1
    # EXACTLY the unqualified full scan -- the exact-finish re-score reads the
    # same columns but under a WHERE arxiv_id IN (...) over at most k + 16 ids
    # per query, which is a primary-key lookup, not a pass.
    assert [s for s in seen if s.strip() == full_scan] == [full_scan]


def test_a_single_query_needs_no_batch_and_says_so(tmp_path, monkeypatch):
    conn = _build_fixture_conn(tmp_path, dims=8, n=15, force_fallback=True, monkeypatch=monkeypatch)

    batch = semantic_search_many(conn, [deterministic_vector(7, 8)], k=3)

    assert batch["scan_mode"] == "per_query"
    assert batch["passes"] == 1
    assert batch["rows_scanned"] is None
    assert [r.arxiv_id for r in batch["results"][0]] == [
        r.arxiv_id for r in semantic_search(conn, deterministic_vector(7, 8), k=3)
    ]


def test_without_numpy_the_answers_are_the_same_and_the_mode_says_per_query(tmp_path, monkeypatch):
    """numpy is optional and stays optional: turning the fast path off by knob
    must change the cost, never the answer."""
    conn = _build_fixture_conn(tmp_path, dims=16, n=30, force_fallback=True, monkeypatch=monkeypatch)
    queries = [deterministic_vector(i, 16) for i in (2, 5, 11)]

    fast = semantic_search_many(conn, queries, k=4)
    plain = semantic_search_many(conn, queries, k=4, config={"retrieve": {"numpy_fastpath": "off"}})

    assert fast["scan_mode"] == "single_pass"
    assert plain["scan_mode"] == "per_query"
    assert plain["passes"] == 3
    for a, b in zip(fast["results"], plain["results"]):
        assert [r.arxiv_id for r in a] == [r.arxiv_id for r in b]
        for x, y in zip(a, b):
            assert x.score == pytest.approx(y.score, abs=1e-5)


def test_a_row_whose_blob_is_the_wrong_width_is_skipped_and_counted(tmp_path, monkeypatch):
    """One bad row must not make the other rows unsearchable."""
    conn = _build_fixture_conn(tmp_path, dims=8, n=10, force_fallback=True, monkeypatch=monkeypatch)
    conn.execute(
        "INSERT INTO arxiv_vec(arxiv_id, dims, vector) VALUES (?, ?, ?)", ("9999.99999", 8, b"\x00" * 12)
    )
    conn.commit()

    batch = semantic_search_many(conn, [deterministic_vector(i, 8) for i in (1, 2)], k=3)

    assert batch["skipped_rows"] == 1
    assert batch["rows_scanned"] == 11
    assert all("9999.99999" not in [r.arxiv_id for r in results] for results in batch["results"])


def test_semantic_search_many_with_no_queries_returns_nothing_and_scans_nothing(tmp_path, monkeypatch):
    conn = _build_fixture_conn(tmp_path, dims=8, n=5, force_fallback=True, monkeypatch=monkeypatch)

    batch = semantic_search_many(conn, [], k=3)

    assert batch["results"] == []
    assert batch["passes"] == 0


def test_semantic_search_many_at_real_3072_dims(tmp_path, monkeypatch):
    conn = _build_fixture_conn(tmp_path, dims=3072, n=6, force_fallback=True, monkeypatch=monkeypatch)
    queries = [deterministic_vector(i, 3072) for i in (2, 4)]

    batch = semantic_search_many(conn, queries, k=3)

    assert batch["scan_mode"] == "single_pass"
    assert [r.arxiv_id for r in batch["results"][0]][0] == "9999.00002"
    assert [r.arxiv_id for r in batch["results"][1]][0] == "9999.00004"
