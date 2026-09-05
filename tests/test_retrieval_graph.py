"""Tests for the graph tier additions to :mod:`trialerror.retrieve.engine`
(design Section 11 v1 deliverable 2): :func:`~trialerror.retrieve.engine.k_hop_neighbors`,
:func:`~trialerror.retrieve.engine.path_between`, :func:`~trialerror.retrieve.engine.graph_tier_candidates`,
and the ``search()`` ``"graph"`` tier wiring. A NEW file (not an edit to
``tests/test_retrieval_engine.py``) per this build's lane-isolation
convention -- reuses ``tests._retrieve_fixtures.build_small_corpus`` (a
landed, non-concurrent-lane fixture builder) and plants ``entity``/
``relation`` rows directly, the exact pattern
``tests/test_retrieval_engine.py``'s own FX-4 graph_neighbors tests use."""

from __future__ import annotations

import pytest

from trialerror.retrieve import engine
from trialerror.retrieve.errors import EntityNotFoundError
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import build_small_corpus


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


def _entity(store, launch_id, name="E", entity_type="x"):
    eid = new_id("ENT")
    insert(store, "entity", {"entity_id": eid, "name": name, "entity_type": entity_type, "resolution": "confirmed", "created_by_launch": launch_id, "created_at": now()})
    return eid


def _relation(store, src, dst, *, anchor_id, rel_type="r", fact_text="f", **extra):
    row = {"rel_id": new_id("REL"), "src_entity": src, "dst_entity": dst, "rel_type": rel_type, "fact_text": fact_text, "evidence_anchor": anchor_id, "created_at": now()}
    row.update(extra)
    return insert(store, "relation", row)


def _any_anchor(store):
    return dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor LIMIT 1").fetchone())["anchor_id"]


def _chain(store, corpus, n: int) -> list[str]:
    """A path graph e0 -> e1 -> ... -> e(n-1), n-1 relations."""
    anchor = _any_anchor(store)
    nodes = [_entity(store, corpus["launch_id"], name=f"N{i}") for i in range(n)]
    for i in range(n - 1):
        _relation(store, nodes[i], nodes[i + 1], anchor_id=anchor)
    return nodes


# ---------------------------------------------------------------------------
# k_hop_neighbors
# ---------------------------------------------------------------------------


def test_k_hop_neighbors_default_2_hops_reaches_distance_2_not_3(store, corpus):
    nodes = _chain(store, corpus, 4)  # n0-n1-n2-n3
    result = engine.k_hop_neighbors(store, nodes[0])
    assert result["max_hops"] == 2
    assert result["hops_reached"] == 2
    assert set(result["nodes"]) == {nodes[0], nodes[1], nodes[2]}
    assert nodes[3] not in result["nodes"]
    assert result["count"] == 2
    assert result["truncated"] is False


def test_k_hop_neighbors_max_hops_1_stops_at_immediate_neighbors(store, corpus):
    nodes = _chain(store, corpus, 3)
    result = engine.k_hop_neighbors(store, nodes[0], max_hops=1)
    assert set(result["nodes"]) == {nodes[0], nodes[1]}
    assert result["hops_reached"] == 1


def test_k_hop_neighbors_max_hops_past_ceiling_raises_value_error(store, corpus):
    nodes = _chain(store, corpus, 2)
    with pytest.raises(ValueError):
        engine.k_hop_neighbors(store, nodes[0], max_hops=engine.ABSOLUTE_MAX_HOPS_CEILING + 1)


def test_k_hop_neighbors_max_hops_zero_raises_value_error(store, corpus):
    nodes = _chain(store, corpus, 2)
    with pytest.raises(ValueError):
        engine.k_hop_neighbors(store, nodes[0], max_hops=0)


def test_k_hop_neighbors_not_found_raises(store):
    with pytest.raises(EntityNotFoundError):
        engine.k_hop_neighbors(store, "ENT-does-not-exist")


def test_k_hop_neighbors_hop_limit_truncates_and_flags(store, corpus):
    anchor = _any_anchor(store)
    center = _entity(store, corpus["launch_id"], name="hub")
    leaves = [_entity(store, corpus["launch_id"], name=f"leaf{i}") for i in range(10)]
    for leaf in leaves:
        _relation(store, center, leaf, anchor_id=anchor)

    result = engine.k_hop_neighbors(store, center, max_hops=1, hop_limit=3)
    assert result["truncated"] is True
    assert result["count"] <= 3
    assert result["hop_limit"] == 3


def test_k_hop_neighbors_no_edges_is_empty_not_an_error(store, corpus):
    lone = _entity(store, corpus["launch_id"], name="lonely")
    result = engine.k_hop_neighbors(store, lone)
    assert result["count"] == 0
    assert result["nodes"] == [lone]


def test_k_hop_neighbors_bitemporal_as_of_tx_matches_graph_neighbors_semantics(store, corpus):
    e1, e2 = _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"])
    anchor = _any_anchor(store)
    _relation(store, e1, e2, anchor_id=anchor, created_at="2020-01-01T00:00:00.000Z", expired_at="2020-06-01T00:00:00.000Z")

    live = engine.k_hop_neighbors(store, e1)
    assert live["count"] == 0

    historical = engine.k_hop_neighbors(store, e1, as_of_tx="2020-03-01T00:00:00.000Z")
    assert historical["count"] == 1


def test_k_hop_neighbors_fences_and_wraps_like_graph_neighbors(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    e1, e2, e3 = _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"])
    restricted_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["restricted_doc_id"],)).fetchone())["anchor_id"]
    open_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["open_doc_id"],)).fetchone())["anchor_id"]
    long_fact = "word " * 30
    _relation(store, e1, e2, anchor_id=open_anchor, fact_text="short open fact")
    _relation(store, e2, e3, anchor_id=restricted_anchor, fact_text=long_fact.strip())

    result = engine.k_hop_neighbors(store, e1, max_hops=2)
    assert result["count"] == 2
    by_dst = {e["dst_entity"]: e for e in result["edges"]}
    assert by_dst[e2]["fenced"] is False
    assert by_dst[e3]["fenced"] is True
    assert len(by_dst[e3]["fact_text"].replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "").split()) <= 20


# ---------------------------------------------------------------------------
# path_between
# ---------------------------------------------------------------------------


def test_path_between_direct_neighbor(store, corpus):
    nodes = _chain(store, corpus, 2)
    result = engine.path_between(store, nodes[0], nodes[1])
    assert result["found"] is True
    assert result["hops"] == 1
    assert result["nodes"] == [nodes[0], nodes[1]]
    assert len(result["edges"]) == 1


def test_path_between_multi_hop_within_max_hops(store, corpus):
    nodes = _chain(store, corpus, 4)
    result = engine.path_between(store, nodes[0], nodes[3], max_hops=3)
    assert result["found"] is True
    assert result["hops"] == 3
    assert result["nodes"] == nodes


def test_path_between_out_of_reach_within_default_max_hops_not_found(store, corpus):
    nodes = _chain(store, corpus, 5)  # distance 4 > default max_hops=2
    result = engine.path_between(store, nodes[0], nodes[4])
    assert result["found"] is False
    assert result["hops_searched"] == 2
    assert result["nodes"] == []


def test_path_between_disconnected_entities_not_found(store, corpus):
    a = _entity(store, corpus["launch_id"], name="isolated-a")
    b = _entity(store, corpus["launch_id"], name="isolated-b")
    result = engine.path_between(store, a, b, max_hops=3)
    assert result["found"] is False


def test_path_between_same_entity_trivial_path(store, corpus):
    e = _entity(store, corpus["launch_id"])
    result = engine.path_between(store, e, e)
    assert result["found"] is True
    assert result["hops"] == 0
    assert result["nodes"] == [e]
    assert result["edges"] == []


def test_path_between_undirected_traversal_follows_edges_either_direction(store, corpus):
    anchor = _any_anchor(store)
    a, b, c = _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"])
    _relation(store, a, b, anchor_id=anchor)  # a -> b
    _relation(store, c, b, anchor_id=anchor)  # c -> b (reversed relative to a path a->b->c)
    result = engine.path_between(store, a, c, max_hops=3)
    assert result["found"] is True
    assert result["hops"] == 2
    assert result["nodes"] == [a, b, c]


def test_path_between_not_found_entity_raises(store, corpus):
    e = _entity(store, corpus["launch_id"])
    with pytest.raises(EntityNotFoundError):
        engine.path_between(store, e, "ENT-does-not-exist")
    with pytest.raises(EntityNotFoundError):
        engine.path_between(store, "ENT-does-not-exist", e)


def test_path_between_max_hops_out_of_range_raises(store, corpus):
    nodes = _chain(store, corpus, 2)
    with pytest.raises(ValueError):
        engine.path_between(store, nodes[0], nodes[1], max_hops=engine.ABSOLUTE_MAX_HOPS_CEILING + 1)


def test_path_between_fences_returned_edges(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_OPEN

    restricted_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["restricted_doc_id"],)).fetchone())["anchor_id"]
    a, b = _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"])
    long_fact = "word " * 30
    _relation(store, a, b, anchor_id=restricted_anchor, fact_text=long_fact.strip())
    result = engine.path_between(store, a, b)
    assert result["found"] is True
    assert result["edges"][0]["fenced"] is True
    assert result["edges"][0]["fact_text"].startswith(UNTRUSTED_OPEN)


def test_path_between_respects_config_ceiling(store, program_root, corpus):
    (program_root / "trialerror.toml").write_text('[program]\nid = "test"\n\n[retrieve.graph]\nmax_hops_ceiling = 1\n', encoding="utf-8")
    nodes = _chain(store, corpus, 3)
    with pytest.raises(ValueError):
        engine.path_between(store, nodes[0], nodes[2], max_hops=2)
    # max_hops=1 (within the tightened ceiling) still works fine
    result = engine.path_between(store, nodes[0], nodes[1], max_hops=1)
    assert result["found"] is True


# ---------------------------------------------------------------------------
# graph_tier_candidates
# ---------------------------------------------------------------------------


def test_graph_tier_candidates_empty_without_seeds():
    assert engine.graph_tier_candidates(None, []) == []  # store never touched for an empty seed list


def test_graph_tier_candidates_finds_neighbor_chunk_via_shared_entity(store, corpus):
    open_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["open_doc_id"],)).fetchone())["anchor_id"]
    restricted_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["restricted_doc_id"],)).fetchone())["anchor_id"]
    e1, e2 = _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"])
    # a relation anchored to the OPEN chunk connects e1 to e2 ...
    _relation(store, e1, e2, anchor_id=open_anchor)
    # ... and e2 has ANOTHER relation whose evidence anchor lives on the
    # RESTRICTED chunk -- that's the neighbor chunk graph_tier_candidates
    # should surface starting from the open chunk as the seed.
    e3 = _entity(store, corpus["launch_id"])
    _relation(store, e2, e3, anchor_id=restricted_anchor)

    seeds = corpus["open_chunk_ids"]
    result = engine.graph_tier_candidates(store, seeds)
    assert corpus["restricted_chunk_ids"][0] in result


def test_graph_tier_candidates_no_entities_on_seed_chunk_is_empty(store, corpus):
    result = engine.graph_tier_candidates(store, corpus["open_chunk_ids"])
    assert result == []


def test_graph_tier_candidates_seed_limit_applied(store, corpus):
    # more seeds than DEFAULT_GRAPH_TIER_SEED_LIMIT are simply truncated,
    # not an error
    many_seeds = corpus["open_chunk_ids"] * (engine.DEFAULT_GRAPH_TIER_SEED_LIMIT + 5)
    result = engine.graph_tier_candidates(store, many_seeds)
    assert result == []  # still correct (no entities anchored here), just proving it doesn't blow up


# ---------------------------------------------------------------------------
# search() -- "graph" tier wiring
# ---------------------------------------------------------------------------


def test_search_includes_graph_tier_when_kg_populated(store, corpus):
    open_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["open_doc_id"],)).fetchone())["anchor_id"]
    restricted_anchor = dict(store.knowledge.execute("SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (corpus["restricted_doc_id"],)).fetchone())["anchor_id"]
    e1, e2 = _entity(store, corpus["launch_id"]), _entity(store, corpus["launch_id"])
    _relation(store, e1, e2, anchor_id=open_anchor)
    e3 = _entity(store, corpus["launch_id"])
    _relation(store, e2, e3, anchor_id=restricted_anchor)

    result = engine.search(store, query="retry budgets bound tail latency", mode="hybrid")
    assert "graph" in result["tiers_used"]
    assert result["stats"]["graph_candidates"] >= 1
    result_ids = {r["chunk_id"] for r in result["results"]}
    assert corpus["restricted_chunk_ids"][0] in result_ids


def test_search_mode_fts_never_runs_graph_tier(store, corpus):
    result = engine.search(store, query="retry budgets", mode="fts")
    assert "graph" not in result["tiers_used"]
    assert "graph_candidates" not in result["stats"]


def test_search_graph_tier_absent_when_kg_empty(store, corpus):
    result = engine.search(store, query="retry budgets bound tail latency", mode="hybrid")
    assert "graph" not in result["tiers_used"]
