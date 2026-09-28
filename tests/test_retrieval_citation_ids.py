"""Lane FB-acq item 6 (F21): a search hit carries the source's EXTERNAL ids.

A ``query search`` hit named its source only by the programme-internal
``source_id`` and the title, so nothing came back that a caller could paste
into a bibliography, feed to a lookup, or match against another tool's rows --
although the source row was already in hand when the citation block was built.

This module pins the two added keys (``doi`` and ``arxiv_id``, always present
and ``None`` when the source carries none) on the chunk-hit and summary-hit
citation blocks, on the ``similar`` surface that shares the chunk builder, and
under the licence fence; and it pins the surrounding shapes that must NOT have
moved: the hit's top-level key set, and the four keys of a summary's
``cited_sources`` entries.

Identifiers are metadata about a source, not corpus text, so the fence is not
involved: a fenced hit gets the same two ids as an open one, and its quote is
still capped.
"""

from __future__ import annotations

import pytest

from trialerror.retrieve import engine
from trialerror.stores.writer import update
from trialerror.summarize.api import build_summary_envelope, store_summary

from tests._retrieve_fixtures import build_small_corpus

DOI = "10.1000/example"
ARXIV_ID = "2101.00001"

#: The chunk-hit citation block, exactly. Written out rather than derived, so
#: a key added or dropped anywhere in the engine fails HERE and has to be a
#: decision rather than a side effect.
CITATION_KEYS = {"source_id", "title", "doi", "arxiv_id", "license_tier", "anchor", "quote"}

#: The chunk-hit row's own key set, unchanged by this item.
HIT_KEYS = {
    "rank", "score", "fusion", "chunk_id", "doc_id", "source_id", "text", "fenced", "citation",
}

#: A summary hit's ``cited_sources`` entries keep exactly these four -- they
#: are a list of what the summary drew on, not a citation block each.
CITED_SOURCE_KEYS = {"doc_id", "source_id", "title", "license_tier"}


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


@pytest.fixture()
def identified(store, corpus):
    """The corpus with external ids on its OPEN source and none on the
    restricted one -- both halves of "always present" in one fixture."""
    update(
        store, "source",
        pk_column="source_id", pk_value=corpus["open_source_id"],
        changes={"doi": DOI, "arxiv_id": ARXIV_ID},
    )
    return corpus


def _open_hits(store, corpus, response):
    return [row for row in response["results"] if row["source_id"] == corpus["open_source_id"]]


# ---------------------------------------------------------------------------
# chunk hits
# ---------------------------------------------------------------------------


def test_every_hit_on_an_identified_source_carries_its_doi_and_arxiv_id(store, identified):
    r = engine.search(store, query="retry budgets bound tail latency")
    hits = _open_hits(store, identified, r)
    assert hits
    for row in hits:
        assert row["citation"]["doi"] == DOI
        assert row["citation"]["arxiv_id"] == ARXIV_ID


def test_a_source_with_neither_id_reports_both_keys_as_none(store, corpus):
    r = engine.search(store, query="retry budgets bound tail latency")
    assert r["results"]
    for row in r["results"]:
        citation = row["citation"]
        assert "doi" in citation and "arxiv_id" in citation
        assert citation["doi"] is None
        assert citation["arxiv_id"] is None


def test_the_citation_key_set_and_the_hits_key_set_are_exactly_these(store, identified):
    r = engine.search(store, query="retry budgets bound tail latency")
    assert r["results"]
    for row in r["results"]:
        assert set(row) == HIT_KEYS
        assert set(row["citation"]) == CITATION_KEYS


def test_the_two_ids_land_directly_after_the_title(store, identified):
    """Ordering is not semantics, but a JSON block a human reads is easier to
    read with the identifiers next to the name they identify."""
    r = engine.search(store, query="retry budgets bound tail latency")
    keys = list(r["results"][0]["citation"])
    assert keys[:4] == ["source_id", "title", "doi", "arxiv_id"]


@pytest.mark.parametrize("mode", ["fts", "vector", "auto", "hybrid"])
def test_every_search_mode_that_returns_chunk_hits_carries_them(store, identified, mode):
    r = engine.search(store, query="retry budgets bound tail latency", mode=mode)
    hits = _open_hits(store, identified, r)
    assert hits
    for row in hits:
        assert row["citation"]["doi"] == DOI


def test_similar_shares_the_builder_and_so_inherits_the_two_keys(store, identified):
    ref = identified["open_chunk_ids"][0]
    r = engine.similar(store, ref_id=ref, kind="chunk", k=5)
    assert r["results"]
    for row in r["results"]:
        assert set(row["citation"]) == CITATION_KEYS
    same_source = [row for row in r["results"] if row["source_id"] == identified["open_source_id"]]
    for row in same_source:
        assert row["citation"]["doi"] == DOI
        assert row["citation"]["arxiv_id"] == ARXIV_ID


# ---------------------------------------------------------------------------
# the fence is not involved
# ---------------------------------------------------------------------------


def test_a_fenced_hit_still_reports_the_ids_and_is_still_fenced(store, corpus):
    update(
        store, "source",
        pk_column="source_id", pk_value=corpus["restricted_source_id"],
        changes={"doi": DOI, "arxiv_id": ARXIV_ID},
    )
    r = engine.search(store, query="leader election timeouts heartbeat intervals")
    fenced = [row for row in r["results"] if row["source_id"] == corpus["restricted_source_id"]]
    assert fenced
    for row in fenced:
        assert row["fenced"] is True
        assert row["citation"]["license_tier"] == "commercial_restricted"
        assert row["citation"]["doi"] == DOI
        assert row["citation"]["arxiv_id"] == ARXIV_ID
        # still the capped grounding excerpt, not the paragraph
        assert len(row["citation"]["quote"].split()) <= 20


# ---------------------------------------------------------------------------
# summary hits
# ---------------------------------------------------------------------------


def _store_doc_summary(store, corpus, doc_key, body):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus[doc_key])
    return store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])


def test_a_summary_hit_carries_the_primary_sources_ids(store, identified):
    _store_doc_summary(store, identified, "open_doc_id", "An overview of retry budgets under failover.")
    r = engine.search(store, query="retry budgets", mode="summary")
    assert len(r["results"]) == 1
    citation = r["results"][0]["citation"]
    assert citation["doi"] == DOI
    assert citation["arxiv_id"] == ARXIV_ID
    assert citation["anchor"] is None


def test_a_summary_hit_whose_source_has_no_ids_reports_both_as_none(store, corpus):
    _store_doc_summary(store, corpus, "open_doc_id", "An overview of retry budgets under failover.")
    r = engine.search(store, query="retry budgets", mode="summary")
    citation = r["results"][0]["citation"]
    assert "doi" in citation and "arxiv_id" in citation
    assert citation["doi"] is None and citation["arxiv_id"] is None


def test_a_summarys_cited_sources_entries_keep_exactly_their_four_keys(store, identified):
    _store_doc_summary(store, identified, "open_doc_id", "An overview of retry budgets under failover.")
    r = engine.search(store, query="retry budgets", mode="summary")
    entries = r["results"][0]["cited_sources"]
    assert entries
    for entry in entries:
        assert set(entry) == CITED_SOURCE_KEYS
