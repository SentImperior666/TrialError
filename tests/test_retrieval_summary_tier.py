"""build-v2-summary: :mod:`trialerror.retrieve.engine`'s ``mode="summary"``
search path -- the summary-first tier over ``knowledge.summary``, returning
L1 overviews with their doc citations, D-COC-1-fenced on the ``citation.
quote`` field (never the served body) when a cited source is
``commercial_restricted``. Built against real stores via
``tests/_summarize_fixtures.build_small_corpus`` (both an ``open`` and a
``commercial_restricted`` source)."""

from __future__ import annotations

import pytest

from trialerror.retrieve import engine
from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from trialerror.summarize.api import MAX_EMBEDDED_QUOTE_WORDS, build_summary_envelope, store_summary

from tests._summarize_fixtures import build_small_corpus


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


def _store_doc_summary(store, corpus, doc_key: str, body: str):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus[doc_key])
    return store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])


# ---------------------------------------------------------------------------
# basic shape / empty corpus
# ---------------------------------------------------------------------------


def test_summary_mode_with_no_summaries_yet_returns_empty(store, corpus):
    r = engine.search(store, query="anything", mode="summary")
    assert r["ok"] is True
    assert r["tiers_used"] == ["summary"]
    assert r["results"] == []
    assert r["stats"]["summary_candidates"] == 0


def test_summary_mode_excluded_via_tiers_returns_nothing(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "Retry budgets resolve uncertain outcomes in play.")
    r = engine.search(store, query="retry budgets", mode="summary", tiers=["fts"])
    assert r["tiers_used"] == []
    assert r["results"] == []


def test_summary_mode_never_touches_fts_or_vector_stats(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "An overview about retry budgets.")
    r = engine.search(store, query="retry budgets", mode="summary")
    assert r["stats"]["fts_candidates"] == 0
    assert r["stats"]["vector_scored"] == 0


# ---------------------------------------------------------------------------
# citation shape (open source)
# ---------------------------------------------------------------------------


def test_summary_result_row_carries_a_non_null_citation_block(store, corpus):
    row = _store_doc_summary(store, corpus, "open_doc_id", "Retry budgets resolve uncertain outcomes during play.")
    r = engine.search(store, query="retry budgets", mode="summary")
    assert len(r["results"]) == 1
    result = r["results"][0]
    assert result["kind"] == "summary"
    assert result["summary_id"] == row["summary_id"]
    assert result["subject_kind"] == "document"
    assert result["subject_id"] == corpus["open_doc_id"]
    assert result["chunk_id"] is None
    assert result["doc_id"] == corpus["open_doc_id"]
    citation = result["citation"]
    assert citation["source_id"] == corpus["open_source_id"]
    assert citation["title"]
    assert citation["license_tier"] == "open"
    assert citation["anchor"] is None
    assert citation["quote"]
    assert result["fenced"] is False
    assert len(result["cited_sources"]) == 1
    assert result["cited_sources"][0]["doc_id"] == corpus["open_doc_id"]


def test_summary_text_is_untrusted_wrapped(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "Retry budgets resolve uncertain outcomes during play.")
    r = engine.search(store, query="retry budgets", mode="summary")
    text = r["results"][0]["text"]
    assert text.startswith(UNTRUSTED_OPEN)
    assert text.endswith(UNTRUSTED_CLOSE)
    assert "Retry budgets resolve uncertain outcomes during play." in text


# ---------------------------------------------------------------------------
# D-COC-1 fence on the serving path
# ---------------------------------------------------------------------------


def test_restricted_summary_is_fenced_true_but_body_serves_in_full(store, corpus):
    long_body = "This is a fully paraphrased overview with no quotes. " * 20  # well over 20 words, zero quotes
    row = _store_doc_summary(store, corpus, "restricted_doc_id", long_body)
    assert row["fenced"] == 1  # stored flag, from store_summary's own fresh recomputation

    r = engine.search(store, query="paraphrased overview", mode="summary")
    result = r["results"][0]
    assert result["fenced"] is True
    assert long_body in result["text"]  # the BODY itself is never truncated (extraction, not verbatim)
    assert result["citation"]["license_tier"] == "commercial_restricted"


def test_restricted_summary_citation_quote_never_exceeds_the_dcoc1_cap(store, corpus):
    body = "An overview of the rulebook's combat chapter, paraphrased and short."
    _store_doc_summary(store, corpus, "restricted_doc_id", body)
    r = engine.search(store, query="combat", mode="summary")
    quote = r["results"][0]["citation"]["quote"]
    assert len(quote.split()) <= MAX_EMBEDDED_QUOTE_WORDS


def test_open_summary_is_never_fenced(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "An open overview, freely quotable at any length here.")
    r = engine.search(store, query="open overview", mode="summary")
    assert r["results"][0]["fenced"] is False


# ---------------------------------------------------------------------------
# --unfenced escape hatch (CLI-only, logged)
# ---------------------------------------------------------------------------


def test_unfenced_bypasses_fenced_flag_and_logs_an_event(store, corpus):
    _store_doc_summary(store, corpus, "restricted_doc_id", "A short restricted overview for the bypass test.")
    r = engine.search(store, query="restricted overview", mode="summary", unfenced=True, launch_id=corpus["launch_id"])
    assert r["results"][0]["fenced"] is False

    events = [
        dict(row)
        for row in store.ops.execute("SELECT * FROM event WHERE type = 'retrieval_unfenced_bypass' ORDER BY event_id").fetchall()
    ]
    assert events
    import json as _json

    payload = _json.loads(events[-1]["payload"])
    assert corpus["restricted_source_id"] in payload["source_ids"]
    assert "summary tier" in payload["surface"]


# ---------------------------------------------------------------------------
# query scoring / blank query / k / filters
# ---------------------------------------------------------------------------


def test_blank_query_browses_every_current_summary_newest_first(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "first overview")
    _store_doc_summary(store, corpus, "restricted_doc_id", "second overview, no quotes here")
    r = engine.search(store, query="", mode="summary")
    assert len(r["results"]) == 2


def test_query_terms_filter_out_non_matching_summaries(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "an overview about retry budgets and outcomes")
    _store_doc_summary(store, corpus, "restricted_doc_id", "an overview about leader election only")
    r = engine.search(store, query="retry budgets", mode="summary")
    assert len(r["results"]) == 1
    assert r["results"][0]["doc_id"] == corpus["open_doc_id"]


def test_k_limits_result_count(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "overview overview overview")
    _store_doc_summary(store, corpus, "restricted_doc_id", "overview overview overview overview")
    r = engine.search(store, query="overview", mode="summary", k=1)
    assert len(r["results"]) == 1


def test_source_id_filter_restricts_to_matching_summaries(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "overview text")
    _store_doc_summary(store, corpus, "restricted_doc_id", "overview text")
    r = engine.search(store, query="overview", mode="summary", filters={"source_ids": [corpus["open_source_id"]]})
    assert len(r["results"]) == 1
    assert r["results"][0]["source_id"] == corpus["open_source_id"]


def test_filter_matching_zero_chunks_returns_empty_summary_results_too(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "overview text")
    r = engine.search(store, query="overview", mode="summary", filters={"source_ids": ["SRC-does-not-exist"]})
    assert r["results"] == []
    assert r["tiers_used"] == []


def test_result_rank_and_score_fields_present(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "overview text about retry budgets")
    r = engine.search(store, query="retry budgets", mode="summary")
    row = r["results"][0]
    assert row["rank"] == 1
    assert isinstance(row["score"], float)
    assert row["fusion"] == {"summary": 1}
