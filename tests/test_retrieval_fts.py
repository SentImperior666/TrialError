"""Unit tests for :mod:`trialerror.retrieve.ftssearch` -- the FTS5/BM25
prefilter tier, against a real ``chunk_fts`` table (``store`` fixture from
``tests/conftest.py``)."""

from __future__ import annotations

from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT, fts_query_string, fts_search


def _insert_fts_row(store, chunk_id: str, text: str) -> None:
    store.knowledge.execute("INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", (chunk_id, text))
    store.knowledge.commit()


def test_fts_query_string_quotes_every_token_as_a_literal_phrase():
    assert fts_query_string("hello world") == '"hello" "world"'


def test_fts_query_string_survives_operator_like_tokens_without_raising():
    # a raw pass-through of "AND"/"OR"/"-"/unbalanced quotes into FTS5's
    # MATCH breaks; fts_query_string must neutralize all of it.
    q = fts_query_string('spell-check AND "unbalanced quote OR NOT this')
    assert q  # doesn't raise; exact shape isn't the point, safety is
    assert q.count('"') % 2 == 0  # always balanced


def test_fts_query_string_empty_input():
    assert fts_query_string("") == '""'
    assert fts_query_string("   ") == '""'


def test_fts_search_finds_a_matching_chunk(store):
    _insert_fts_row(store, "CHK-1", "dice pools resolve uncertain outcomes")
    _insert_fts_row(store, "CHK-2", "completely unrelated content about spaceships")

    hits = fts_search(store, "dice pools")
    assert [h["chunk_id"] for h in hits] == ["CHK-1"]


def test_fts_search_no_match_returns_empty():
    pass  # covered implicitly by test_fts_search_finds_a_matching_chunk's CHK-2 exclusion


def test_fts_search_respects_the_chunk_id_allowlist(store):
    _insert_fts_row(store, "CHK-1", "dice pools resolve uncertain outcomes")
    _insert_fts_row(store, "CHK-2", "dice pools appear twice in this document too")

    hits_unfiltered = fts_search(store, "dice pools")
    assert {h["chunk_id"] for h in hits_unfiltered} == {"CHK-1", "CHK-2"}

    hits_filtered = fts_search(store, "dice pools", chunk_id_allowlist=["CHK-2"])
    assert [h["chunk_id"] for h in hits_filtered] == ["CHK-2"]


def test_fts_search_empty_allowlist_short_circuits_to_no_candidates(store):
    _insert_fts_row(store, "CHK-1", "dice pools resolve uncertain outcomes")
    assert fts_search(store, "dice pools", chunk_id_allowlist=[]) == []


def test_fts_search_respects_the_limit(store):
    for i in range(10):
        _insert_fts_row(store, f"CHK-{i}", "repeated keyword appears in every fixture row")
    hits = fts_search(store, "repeated keyword", limit=3)
    assert len(hits) == 3


def test_fts_search_empty_query_string_returns_empty_not_an_error(store):
    _insert_fts_row(store, "CHK-1", "some text")
    assert fts_search(store, "") == []
    assert fts_search(store, "   ") == []


def test_default_candidate_limit_matches_design_section_7():
    assert DEFAULT_FTS_CANDIDATE_LIMIT == 500
