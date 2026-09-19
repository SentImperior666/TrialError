"""B.4b native-MATCH wiring, engine-level (build-arxiv-kaggle-index
session, spikes/index_bakeoffs/BAKEOFF_REPORT.md Sec B.4b): proves
``trialerror.retrieve.engine.search(mode="vector")`` (unfiltered) and
``similar()`` produce the SAME results whether the corpus's vec table is
the fallback backend (existing ``fetch_vectors``+``rank_by_query_vector``
path, untouched) or a real sqlite-vec ``vec0`` table (the new native-MATCH
path) -- parity between the two code paths is the correctness bar, not
just "it doesn't crash". ``TRIALERROR_VEC_BACKEND`` must be set BEFORE
``build_small_corpus`` calls ``ensure_vec_table`` (that function reads the
env var at call time), so this file builds its own corpora directly
(``tests._retrieve_fixtures.build_small_corpus``) rather than using
``tests/test_retrieval_engine.py``'s ``corpus`` pytest fixture (which
would already have run before a test body's own ``monkeypatch.setenv``
call takes effect)."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.retrieve import engine
from trialerror.retrieve.vecsearch import vec_backend_for
from trialerror.stores.vecindex import VecBackend, try_load_sqlite_vec
from tests._retrieve_fixtures import build_small_corpus


def _sqlite_vec_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return try_load_sqlite_vec(conn)
    finally:
        conn.close()


@pytest.fixture()
def sqlite_vec_corpus(store, monkeypatch):
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")
    corpus = build_small_corpus(store)
    assert vec_backend_for(store, corpus["model_key"]) == VecBackend.SQLITE_VEC
    return corpus


@pytest.fixture()
def fallback_corpus(store):
    corpus = build_small_corpus(store)
    assert vec_backend_for(store, corpus["model_key"]) == VecBackend.FALLBACK
    return corpus


def test_search_mode_vector_unfiltered_same_top_result_both_backends(program_root, platform_root):
    """Two independent stores (fallback vs sqlite-vec), built from
    IDENTICAL fixture content (same deterministic FakeEmbedBackend hash of
    the same text) -- the same query must rank the SAME chunk_id first
    through either code path."""
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")

    from trialerror.stores.store import open_store

    fb_store = open_store(program_root, platform_root=platform_root)
    fb_corpus = build_small_corpus(fb_store)
    fb_result = engine.search(fb_store, query="retry budgets bound tail latency", mode="vector")
    assert vec_backend_for(fb_store, fb_corpus["model_key"]) == VecBackend.FALLBACK
    fb_store.close()

    sv_root = program_root.parent / "program_sv"
    sv_root.mkdir()
    sv_store = open_store(sv_root, platform_root=platform_root)
    import os

    os.environ["TRIALERROR_VEC_BACKEND"] = "sqlite_vec"
    try:
        sv_corpus = build_small_corpus(sv_store)
        assert vec_backend_for(sv_store, sv_corpus["model_key"]) == VecBackend.SQLITE_VEC
        sv_result = engine.search(sv_store, query="retry budgets bound tail latency", mode="vector")
    finally:
        del os.environ["TRIALERROR_VEC_BACKEND"]
        sv_store.close()

    assert fb_result["tiers_used"] == ["vector"]
    assert sv_result["tiers_used"] == ["vector"]
    # chunk_id itself is a fresh new_id() per store (not content-derived),
    # so compare by ORDINAL position within each store's own corpus
    # (build_small_corpus's own deterministic paragraph order) rather than
    # raw chunk_id strings -- what must match is WHICH paragraph ranks
    # where, not the id spelling.
    fb_ordinal = {cid: i for i, cid in enumerate(fb_corpus["open_chunk_ids"] + fb_corpus["restricted_chunk_ids"])}
    sv_ordinal = {cid: i for i, cid in enumerate(sv_corpus["open_chunk_ids"] + sv_corpus["restricted_chunk_ids"])}
    fb_order = [fb_ordinal[r["chunk_id"]] for r in fb_result["results"]]
    sv_order = [sv_ordinal[r["chunk_id"]] for r in sv_result["results"]]
    assert fb_order == sv_order


def test_search_mode_vector_sqlite_vec_backend_stats_reported(store, sqlite_vec_corpus):
    r = engine.search(store, query="retry budgets bound tail latency", mode="vector")
    assert r["ok"] is True
    assert r["tiers_used"] == ["vector"]
    assert r["stats"]["vector_scored"] > 0
    assert r["results"]  # native-MATCH path still produces fenced/cited rows correctly
    for row in r["results"]:
        assert row["citation"]["source_id"]


def test_search_mode_vector_filtered_still_works_with_sqlite_vec_backend(store, sqlite_vec_corpus):
    """Filtered mode="vector" (candidate_ids is not None) must NOT take the
    native-MATCH branch (module docstring's own guard) -- confirm it still
    returns correctly-scoped, correct results even though the table is a
    real vec0 table."""
    r = engine.search(
        store, query="retry budgets bound tail latency", mode="vector",
        filters={"source_ids": [sqlite_vec_corpus["open_source_id"]]},
    )
    assert r["ok"] is True
    for row in r["results"]:
        assert row["source_id"] == sqlite_vec_corpus["open_source_id"]


def test_similar_same_ranking_both_backends(program_root, platform_root):
    """Same ordinal-position-not-raw-id comparison as the search() test
    above (chunk_id is a fresh new_id() per store, never content-derived)."""
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed in this environment")

    from trialerror.stores.store import open_store

    fb_store = open_store(program_root, platform_root=platform_root)
    fb_corpus = build_small_corpus(fb_store)
    fb_ref_id = fb_corpus["open_chunk_ids"][0]  # ordinal 0
    fb_result = engine.similar(fb_store, fb_ref_id)
    fb_store.close()

    sv_root = program_root.parent / "program_sv2"
    sv_root.mkdir()
    sv_store = open_store(sv_root, platform_root=platform_root)
    import os

    os.environ["TRIALERROR_VEC_BACKEND"] = "sqlite_vec"
    try:
        sv_corpus = build_small_corpus(sv_store)  # same deterministic content, ordinal-equivalent chunks
        sv_ref_id = sv_corpus["open_chunk_ids"][0]  # the SAME ordinal-0 chunk in this store
        sv_result = engine.similar(sv_store, sv_ref_id)
    finally:
        del os.environ["TRIALERROR_VEC_BACKEND"]
        sv_store.close()

    fb_ordinal = {cid: i for i, cid in enumerate(fb_corpus["open_chunk_ids"] + fb_corpus["restricted_chunk_ids"])}
    sv_ordinal = {cid: i for i, cid in enumerate(sv_corpus["open_chunk_ids"] + sv_corpus["restricted_chunk_ids"])}
    fb_order = [fb_ordinal[r["chunk_id"]] for r in fb_result["results"]]
    sv_order = [sv_ordinal[r["chunk_id"]] for r in sv_result["results"]]

    assert 0 not in fb_order  # ref chunk (ordinal 0) never ranks against itself
    assert 0 not in sv_order
    assert fb_order == sv_order


def test_similar_excludes_ref_id_with_sqlite_vec_backend(store, sqlite_vec_corpus):
    ref_id = sqlite_vec_corpus["open_chunk_ids"][0]
    result = engine.similar(store, ref_id, k=5)
    assert result["ok"] is True
    ids = [r["chunk_id"] for r in result["results"]]
    assert ref_id not in ids
    assert len(ids) > 0
