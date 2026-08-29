"""Integration tests for :mod:`trialerror.retrieve.engine` -- the hybrid
retrieval engine, against real stores built via ``tests/_retrieve_fixtures.
build_small_corpus`` (real chunker/anchor primitives, both an ``open`` and
a ``commercial_restricted`` source)."""

from __future__ import annotations

import pytest

from trialerror.retrieve import engine
from trialerror.retrieve.errors import ChunkNotFoundError, DocumentNotFoundError, EntityNotFoundError, InvalidSearchModeError, SourceNotFoundError
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import build_small_corpus


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


# ---------------------------------------------------------------------------
# search -- shape, modes, filters, RRF
# ---------------------------------------------------------------------------


def test_search_returns_a_non_null_citation_block_on_every_result(store, corpus):
    r = engine.search(store, query="dice pools resolve uncertain outcomes")
    assert r["ok"] is True
    assert r["results"]
    for row in r["results"]:
        citation = row["citation"]
        assert citation["source_id"]
        assert citation["title"]
        assert citation["license_tier"]
        anchor = citation["anchor"]
        assert anchor["anchor_id"]
        assert anchor["char_start"] is not None
        assert anchor["char_end"] is not None
        assert citation["quote"] is not None


def test_search_query_id_is_a_qry_typed_id(store, corpus):
    r = engine.search(store, query="dice pools")
    assert r["query_id"].startswith("QRY-")


def test_search_mode_fts_only_uses_the_fts_tier(store, corpus):
    r = engine.search(store, query="dice pools", mode="fts")
    assert r["tiers_used"] == ["fts"]
    assert r["stats"]["vector_scored"] == 0


def test_search_mode_vector_only_uses_the_vector_tier(store, corpus):
    r = engine.search(store, query="dice pools", mode="vector")
    assert r["tiers_used"] == ["vector"]
    assert r["stats"]["fts_candidates"] == 0


def test_search_mode_auto_and_hybrid_use_both_tiers(store, corpus):
    for mode in ("auto", "hybrid"):
        r = engine.search(store, query="dice pools resolve uncertain outcomes", mode=mode)
        assert r["tiers_used"] == ["fts", "vector"]
        assert r["results"][0]["fusion"]  # has per-tier rank entries


def test_search_invalid_mode_raises(store, corpus):
    with pytest.raises(InvalidSearchModeError):
        engine.search(store, query="x", mode="not-a-real-mode")


def test_search_filters_by_license_tier(store, corpus):
    r = engine.search(store, query="combat resolution", mode="fts", filters={"license_tier": ["commercial_restricted"]})
    assert len(r["results"]) == 1
    assert r["results"][0]["source_id"] == corpus["restricted_source_id"]

    r_wrong = engine.search(store, query="combat resolution", mode="fts", filters={"license_tier": ["open"]})
    assert r_wrong["results"] == []


def test_search_filters_by_source_ids(store, corpus):
    r = engine.search(store, query="dice pools", mode="fts", filters={"source_ids": [corpus["open_source_id"]]})
    assert all(row["source_id"] == corpus["open_source_id"] for row in r["results"])


def test_search_filter_matching_nothing_returns_a_well_formed_empty_response(store, corpus):
    r = engine.search(store, query="dice pools", filters={"source_ids": ["SRC-does-not-exist"]})
    assert r["ok"] is True
    assert r["results"] == []
    assert r["tiers_used"] == []


def test_search_stats_carries_fts_candidates_vector_scored_and_elapsed_ms(store, corpus):
    r = engine.search(store, query="dice pools resolve uncertain outcomes")
    assert isinstance(r["stats"]["fts_candidates"], int)
    assert isinstance(r["stats"]["vector_scored"], int)
    assert isinstance(r["stats"]["elapsed_ms"], float)
    assert r["stats"]["elapsed_ms"] >= 0


def test_search_k_limits_result_count(store, corpus):
    r = engine.search(store, query="dice pools resolve uncertain outcomes", k=1)
    assert len(r["results"]) <= 1


def test_search_results_are_ranked_1_based_and_score_descending(store, corpus):
    r = engine.search(store, query="dice pools resolve uncertain outcomes")
    ranks = [row["rank"] for row in r["results"]]
    assert ranks == list(range(1, len(ranks) + 1))
    scores = [row["score"] for row in r["results"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# F3 fence, applied through search()
# ---------------------------------------------------------------------------


def test_search_fences_commercial_restricted_results(store, corpus):
    r = engine.search(store, query="combat resolution initiative order proprietary special ability", mode="fts")
    assert r["results"]
    row = r["results"][0]
    assert row["fenced"] is True
    assert row["citation"]["license_tier"] == "commercial_restricted"
    # the fenced excerpt (inside the <=20-word verbatim-excerpt quotes) never exceeds 20 words
    assert len(row["citation"]["quote"].split()) <= 20


def test_search_does_not_fence_open_results(store, corpus):
    r = engine.search(store, query="dice pools resolve uncertain outcomes", mode="fts")
    assert r["results"]
    row = r["results"][0]
    assert row["fenced"] is False
    assert row["citation"]["license_tier"] == "open"


def test_search_text_field_is_untrusted_wrapped(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    r = engine.search(store, query="dice pools resolve uncertain outcomes", mode="fts")
    text = r["results"][0]["text"]
    assert text.startswith(UNTRUSTED_OPEN)
    assert text.endswith(UNTRUSTED_CLOSE)


def test_search_unfenced_true_serves_raw_text_and_logs_one_event(store, corpus):
    r = engine.search(
        store, query="combat resolution initiative order proprietary special ability", mode="fts",
        unfenced=True, launch_id=corpus["launch_id"],
    )
    row = r["results"][0]
    assert row["fenced"] is False
    assert "license-fenced" not in row["text"]

    events = [dict(x) for x in store.ops.execute("SELECT * FROM event WHERE type = 'retrieval_unfenced_bypass'")]
    assert len(events) == 1


def test_search_unfenced_false_never_logs_a_bypass_event(store, corpus):
    engine.search(store, query="combat resolution initiative order proprietary special ability", mode="fts")
    events = list(store.ops.execute("SELECT * FROM event WHERE type = 'retrieval_unfenced_bypass'"))
    assert events == []


# ---------------------------------------------------------------------------
# get_chunk
# ---------------------------------------------------------------------------


def test_get_chunk_open_source_serves_raw_text(store, corpus):
    result = engine.get_chunk(store, corpus["open_chunk_ids"][0])
    assert result["fenced"] is False
    assert "license-fenced" not in result["text"]
    assert result["anchors"]


def test_get_chunk_restricted_source_is_fenced(store, corpus):
    result = engine.get_chunk(store, corpus["restricted_chunk_ids"][0])
    assert result["fenced"] is True
    assert "license-fenced" in result["text"]
    for anchor in result["anchors"]:
        assert len((anchor["quote"] or "").split()) <= 20


def test_get_chunk_not_found_raises(store):
    with pytest.raises(ChunkNotFoundError):
        engine.get_chunk(store, "CHK-does-not-exist")


# ---------------------------------------------------------------------------
# get_source / get_document_outline
# ---------------------------------------------------------------------------


def test_get_source_returns_documents(store, corpus):
    result = engine.get_source(store, corpus["open_source_id"])
    assert result["source"]["source_id"] == corpus["open_source_id"]
    assert any(d["doc_id"] == corpus["open_doc_id"] for d in result["documents"])


def test_get_source_not_found_raises(store):
    with pytest.raises(SourceNotFoundError):
        engine.get_source(store, "SRC-does-not-exist")


def test_get_document_outline_includes_title_elements(store, corpus):
    insert(
        store, "element",
        {"element_id": new_id("ELM"), "doc_id": corpus["open_doc_id"], "seq": 99, "type": "Title", "text": "Chapter One: Dice and Fate", "page_number": 1},
    )
    outline = engine.get_document_outline(store, corpus["open_doc_id"])
    assert outline["fenced"] is False
    titles = [o for o in outline["outline"] if o["type"] == "Title"]
    assert len(titles) == 1
    assert titles[0]["text_preview"] == "Chapter One: Dice and Fate"


def test_get_document_outline_fences_titles_for_restricted_sources(store, corpus):
    long_title = " ".join(f"w{i}" for i in range(40))
    insert(
        store, "element",
        {"element_id": new_id("ELM"), "doc_id": corpus["restricted_doc_id"], "seq": 99, "type": "Title", "text": long_title, "page_number": 1},
    )
    outline = engine.get_document_outline(store, corpus["restricted_doc_id"])
    assert outline["fenced"] is True
    titles = [o for o in outline["outline"] if o["type"] == "Title"]
    assert len(titles[0]["text_preview"].split()) <= 20


def test_get_document_outline_not_found_raises(store):
    with pytest.raises(DocumentNotFoundError):
        engine.get_document_outline(store, "DOC-does-not-exist")


# ---------------------------------------------------------------------------
# resolve_quote
# ---------------------------------------------------------------------------


def test_resolve_quote_exact_match_returns_page_and_span(store, corpus):
    anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)).fetchone())
    result = engine.resolve_quote(store, anchor["quote_text"])
    assert result["found"] is True
    assert result["match_type"] == "exact"
    match = result["matches"][0]
    assert match["anchor_id"] == anchor["anchor_id"]
    assert match["char_start"] == anchor["char_start"]
    assert match["char_end"] == anchor["char_end"]
    assert match["page"] == anchor["page_number"]


def test_resolve_quote_substring_match_falls_back(store, corpus):
    result = engine.resolve_quote(store, "dice pools to resolve uncertain outcomes")
    assert result["found"] is True
    assert result["match_type"] == "substring"


def test_resolve_quote_not_found(store, corpus):
    result = engine.resolve_quote(store, "this exact string appears nowhere in the corpus at all")
    assert result["found"] is False
    assert result["matches"] == []


def test_resolve_quote_fences_a_restricted_match(store, corpus):
    anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["restricted_chunk_ids"][0],)).fetchone())
    result = engine.resolve_quote(store, anchor["quote_text"])
    assert result["found"] is True
    match = result["matches"][0]
    assert match["fenced"] is True
    assert len(match["quote"].split()) <= 20


def test_resolve_quote_source_id_filter(store, corpus):
    result = engine.resolve_quote(store, "dice pools to resolve uncertain outcomes", source_id=corpus["restricted_source_id"])
    assert result["found"] is False  # this text lives in the OPEN source, not the restricted one


# ---------------------------------------------------------------------------
# similar
# ---------------------------------------------------------------------------


def test_similar_returns_other_chunks_ranked(store, corpus):
    result = engine.similar(store, corpus["open_chunk_ids"][0])
    assert result["ok"] is True
    for row in result["results"]:
        assert row["chunk_id"] != corpus["open_chunk_ids"][0]
        assert row["citation"]


def test_similar_claim_kind_is_a_graceful_v1_stub(store, corpus):
    result = engine.similar(store, "anything", kind="claim")
    assert result["ok"] is True
    assert result["results"] == []
    assert "note" in result


def test_similar_unsupported_kind_raises(store, corpus):
    with pytest.raises(InvalidSearchModeError):
        engine.similar(store, corpus["open_chunk_ids"][0], kind="bogus")


def test_similar_not_found_raises(store):
    with pytest.raises(ChunkNotFoundError):
        engine.similar(store, "CHK-does-not-exist")


# ---------------------------------------------------------------------------
# graph_neighbors
# ---------------------------------------------------------------------------


def test_graph_neighbors_returns_live_edges(store, corpus):
    e1, e2 = new_id("ENT"), new_id("ENT")
    insert(store, "entity", {"entity_id": e1, "name": "Fireball", "entity_type": "spell", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    insert(store, "entity", {"entity_id": e2, "name": "Mage", "entity_type": "class", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor LIMIT 1").fetchone())
    insert(store, "relation", {"rel_id": new_id("REL"), "src_entity": e1, "dst_entity": e2, "rel_type": "cast_by", "fact_text": "Fireball is cast by a Mage", "evidence_anchor": anchor["anchor_id"], "created_at": now()})

    result = engine.graph_neighbors(store, e1)
    assert result["count"] == 1
    assert result["edges"][0]["rel_type"] == "cast_by"

    # symmetric: looking up the OTHER side of the edge finds it too
    result2 = engine.graph_neighbors(store, e2)
    assert result2["count"] == 1


def test_graph_neighbors_no_writer_populated_is_empty_not_an_error(store, corpus):
    e1 = new_id("ENT")
    insert(store, "entity", {"entity_id": e1, "name": "Lonely Entity", "entity_type": "concept", "resolution": "draft", "created_by_launch": corpus["launch_id"], "created_at": now()})
    result = engine.graph_neighbors(store, e1)
    assert result["count"] == 0
    assert result["edges"] == []


def test_graph_neighbors_not_found_raises(store):
    with pytest.raises(EntityNotFoundError):
        engine.graph_neighbors(store, "ENT-does-not-exist")


def test_graph_neighbors_expired_relation_excluded_from_default_live_view(store, corpus):
    e1, e2 = new_id("ENT"), new_id("ENT")
    insert(store, "entity", {"entity_id": e1, "name": "A", "entity_type": "x", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    insert(store, "entity", {"entity_id": e2, "name": "B", "entity_type": "x", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor LIMIT 1").fetchone())
    insert(store, "relation", {"rel_id": new_id("REL"), "src_entity": e1, "dst_entity": e2, "rel_type": "r", "fact_text": "f", "evidence_anchor": anchor["anchor_id"], "created_at": "2020-01-01T00:00:00.000Z", "expired_at": "2020-06-01T00:00:00.000Z"})
    result = engine.graph_neighbors(store, e1)
    assert result["count"] == 0  # expired_at set -> not in the live (default) view

    result_tx = engine.graph_neighbors(store, e1, as_of_tx="2020-03-01T00:00:00.000Z")
    assert result_tx["count"] == 1  # was live as of a transaction-time point before expiry


# ---------------------------------------------------------------------------
# graph_neighbors -- FX-4 fence + untrusted-wrap (IMPL_REVIEW_VERDICT.md
# Tier 1 / IMPL_REVIEW_B_bypass.md EP-5 Bypass C). No v0 writer populates
# entity/relation, so these plant the rows directly -- mirroring the
# search()/get_chunk() fence acceptance tests above, plugged into
# graph_neighbors instead.
# ---------------------------------------------------------------------------


def test_graph_neighbors_fences_fact_text_for_commercial_restricted_source(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    e1, e2 = new_id("ENT"), new_id("ENT")
    insert(store, "entity", {"entity_id": e1, "name": "Fireball", "entity_type": "spell", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    insert(store, "entity", {"entity_id": e2, "name": "Mage", "entity_type": "class", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})

    restricted_anchor = dict(
        store.knowledge.execute(
            "SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["restricted_doc_id"],)
        ).fetchone()
    )
    verbatim_fact = (
        "This proprietary rulebook fact is intentionally long so that fencing it down to twenty words "
        "is a meaningful, observable transformation rather than a no-op in this acceptance test."
    )
    assert len(verbatim_fact.split()) > 20  # the test is meaningless if the fixture text isn't actually long
    insert(
        store, "relation",
        {
            "rel_id": new_id("REL"), "src_entity": e1, "dst_entity": e2, "rel_type": "cast_by",
            "fact_text": verbatim_fact, "evidence_anchor": restricted_anchor["anchor_id"], "created_at": now(),
        },
    )

    result = engine.graph_neighbors(store, e1)
    assert result["count"] == 1
    edge = result["edges"][0]
    assert edge["fenced"] is True

    # untrusted-wrapped, same as every other served free-text body field
    assert edge["fact_text"].startswith(UNTRUSTED_OPEN)
    assert edge["fact_text"].endswith(UNTRUSTED_CLOSE)
    unwrapped = edge["fact_text"][len(UNTRUSTED_OPEN):-len(UNTRUSTED_CLOSE)].strip()
    # the >20-word rule: never a verbatim run longer than the D-COC-1 cap
    assert len(unwrapped.split()) <= 20
    assert unwrapped != verbatim_fact  # actually capped, not a no-op


def test_graph_neighbors_does_not_fence_open_source_fact_text_but_still_wraps(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    e1, e2 = new_id("ENT"), new_id("ENT")
    insert(store, "entity", {"entity_id": e1, "name": "Fireball", "entity_type": "spell", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    insert(store, "entity", {"entity_id": e2, "name": "Mage", "entity_type": "class", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})

    open_anchor = dict(
        store.knowledge.execute(
            "SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["open_doc_id"],)
        ).fetchone()
    )
    insert(
        store, "relation",
        {
            "rel_id": new_id("REL"), "src_entity": e1, "dst_entity": e2, "rel_type": "cast_by",
            "fact_text": "Fireball is cast by a Mage", "evidence_anchor": open_anchor["anchor_id"], "created_at": now(),
        },
    )

    result = engine.graph_neighbors(store, e1)
    edge = result["edges"][0]
    assert edge["fenced"] is False
    assert edge["fact_text"].startswith(UNTRUSTED_OPEN)
    assert edge["fact_text"].endswith(UNTRUSTED_CLOSE)
    assert "Fireball is cast by a Mage" in edge["fact_text"]  # short + open -> not capped away


def test_graph_neighbors_wraps_entity_summary(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    e1 = new_id("ENT")
    insert(
        store, "entity",
        {
            "entity_id": e1, "name": "Fireball", "entity_type": "spell", "summary": "A powerful evocation spell.",
            "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now(),
        },
    )
    result = engine.graph_neighbors(store, e1)
    summary = result["entity"]["summary"]
    assert summary.startswith(UNTRUSTED_OPEN)
    assert summary.endswith(UNTRUSTED_CLOSE)
    assert "A powerful evocation spell." in summary
    # entity.name is a short structured label, not a free-text body -- left unwrapped
    assert result["entity"]["name"] == "Fireball"


def test_graph_neighbors_entity_without_summary_is_unaffected(store, corpus):
    e1 = new_id("ENT")
    insert(store, "entity", {"entity_id": e1, "name": "Lonely Entity", "entity_type": "concept", "resolution": "draft", "created_by_launch": corpus["launch_id"], "created_at": now()})
    result = engine.graph_neighbors(store, e1)
    assert result["entity"]["summary"] is None


# ---------------------------------------------------------------------------
# corpus_stats / list_requests
# ---------------------------------------------------------------------------


def test_corpus_stats_counts_match_the_fixture(store, corpus):
    stats = engine.corpus_stats(store)
    assert stats["sources"] == 2
    assert stats["documents"] == 2
    assert stats["chunks"] == len(corpus["open_chunk_ids"]) + len(corpus["restricted_chunk_ids"])
    assert stats["chunks_missing_fts"] == 0
    assert stats["embeddings_by_model_key"][corpus["model_key"]] == stats["chunks"]
    assert stats["chunks_missing_vec_by_model_key"][corpus["model_key"]] == 0


def test_list_requests_groups_by_state(store, corpus):
    result = engine.list_requests(store)
    assert result["count"] == 2
    assert result["counts_by_state"]["indexed"] == 2


def test_list_requests_filters_by_state(store, corpus):
    result = engine.list_requests(store, state="wanted")
    assert result["requests"] == []
    assert result["count"] == 0
