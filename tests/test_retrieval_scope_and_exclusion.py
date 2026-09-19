"""The two engine-level retrieval barriers the framework's design classes as
*enforced in code* rather than as conventions in a prompt:

1. **Server-side kind exclusion** — sources of a
   :data:`~trialerror.retrieve.engine.DEFAULT_EXCLUDED_KINDS` kind
   (``inventory``) are absent from every retrieval surface unless a caller
   names ``kind`` explicitly. The screen that judges ideas asks for those
   rows by kind; nothing a lens tool call does asks for them by accident.
2. **Per-launch retrieval scope** — when a call names a launch whose booking
   declares ``attrs.slice_doc_ids``, ``search`` and ``similar`` are
   restricted to those documents, and the restriction comes from the
   booking, not from the caller's arguments.

Both are tested on EVERY surface that serves corpus content, because a
barrier enforced on the ranked surfaces and not on the ones addressed by id
is a barrier enforced against nobody: a chunk id, a document id or a quote
fragment reaches the corpus without ranking anything.
"""

from __future__ import annotations

import pytest

from trialerror.lens.export import export_launch_bookable
from trialerror.mcp.knowledge import build_tools
from trialerror.retrieve import engine
from trialerror.retrieve.errors import ChunkNotFoundError, DocumentNotFoundError
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory


@pytest.fixture()
def corpus(store):
    return build_corpus_with_inventory(store)


def _source_ids(result) -> set[str]:
    return {row["source_id"] for row in result["results"]}


def _anchor_of(store, chunk_id: str) -> str:
    return store.knowledge.execute(
        "SELECT anchor_id FROM quote_anchor WHERE chunk_id = ? LIMIT 1", (chunk_id,)
    ).fetchone()["anchor_id"]


def _link_chunks_by_relation(store, seed_chunk_id: str, target_chunk_id: str, *, launch_id: str) -> str:
    """One shared entity carrying an edge anchored in each chunk — the
    minimum ``graph_tier_candidates`` needs to walk from a ranked seed to a
    chunk the filters excluded. Returns the entity id.

    The tier's own route is anchor -> relation -> entity -> neighbour
    relation -> anchor -> chunk, and ``graph_neighbors`` is scoped by the
    same ``evidence_anchor``, so this one fixture drives both."""
    entity_id = new_id("ENT")
    insert(
        store, "entity",
        {
            "entity_id": entity_id, "name": "shared", "entity_type": "concept",
            "resolution": "confirmed", "created_by_launch": launch_id, "created_at": now(),
        },
    )
    for chunk_id in (seed_chunk_id, target_chunk_id):
        insert(
            store, "relation",
            {
                "rel_id": new_id("REL"), "src_entity": entity_id, "dst_entity": entity_id,
                "rel_type": "mentions", "fact_text": "the two rows describe one mechanism",
                "evidence_anchor": _anchor_of(store, chunk_id), "created_at": now(),
            },
        )
    return entity_id


# ---------------------------------------------------------------------------
# 1. the default kind exclusion
# ---------------------------------------------------------------------------


def test_inventory_rows_are_absent_from_an_unfiltered_search(store, corpus):
    """The barrier that matters: a lens issues an ordinary search and the
    reference set it is being judged against is simply not in the corpus it
    can see."""
    result = engine.search(store, query="shared track", k=20)
    assert result["results"]
    assert corpus["inventory_source_id"] not in _source_ids(result)
    assert corpus["corpus_source_id"] in _source_ids(result)


def test_inventory_rows_come_back_when_a_caller_names_the_kind(store, corpus):
    """The screen's own call. Naming the kind is the explicit request that
    lifts the exclusion -- and it returns inventory and nothing else."""
    result = engine.search(
        store, query="shared track", k=20, filters={"kind": ["inventory"]}
    )
    assert result["results"]
    assert _source_ids(result) == {corpus["inventory_source_id"]}


def test_naming_a_different_kind_still_excludes_inventory(store, corpus):
    result = engine.search(store, query="coordinator", k=20, filters={"kind": ["paper"]})
    assert _source_ids(result) == {corpus["corpus_source_id"]}


def test_a_source_id_filter_does_not_smuggle_inventory_back_in(store, corpus):
    """``source_ids`` is a caller-supplied argument a lens tool call carries.
    Naming the inventory source by id is not naming its KIND, so the
    exclusion still applies -- otherwise the barrier would be one
    ``get_source`` call away from useless."""
    result = engine.search(
        store, query="shared track", k=20,
        filters={"source_ids": [corpus["inventory_source_id"]]},
    )
    assert result["results"] == []


def test_similar_carries_the_same_exclusion_as_search(store, corpus):
    """``similar`` takes a chunk id, not a query. Ranking the whole corpus
    from an inventory row's neighbour would hand back the rest of the
    inventory."""
    ref = corpus["corpus_chunk_ids"][0]
    result = engine.similar(store, ref, k=50)
    returned = {row["source_id"] for row in result["results"]}
    assert returned and corpus["inventory_source_id"] not in returned

    lifted = engine.similar(store, ref, k=50, filters={"kind": ["inventory"]})
    assert {row["source_id"] for row in lifted["results"]} == {corpus["inventory_source_id"]}


def test_a_corpus_with_no_inventory_is_left_completely_unrestricted(store):
    """The exclusion costs nothing where there is nothing to exclude: with
    no inventory source on file, an unfiltered search must still take the
    "no restriction" path rather than materializing every chunk id."""
    from tests._retrieve_fixtures import build_small_corpus

    build_small_corpus(store)
    assert engine._excluded_kinds_present(store) is False
    assert engine._filtered_chunk_ids(store, None) is None
    result = engine.search(store, query="retry budgets bound tail latency")
    assert result["results"]


def test_the_exclusion_reaches_the_mcp_search_and_similar_tools(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_corpus_with_inventory(store)
    store.close()

    tools = build_tools(program_root=program_root, platform_root=platform_root)
    env = tools["search"].handler({"query": "shared track", "k": 20})
    assert env["ok"] is True
    assert corpus["inventory_source_id"] not in {
        row["citation"]["source_id"] for row in env["result"]["results"]
    }

    env = tools["similar"].handler({"id": corpus["corpus_chunk_ids"][0], "k": 50})
    assert corpus["inventory_source_id"] not in {
        row["citation"]["source_id"] for row in env["result"]["results"]
    }


# ---------------------------------------------------------------------------
# 2. the per-launch retrieval scope
# ---------------------------------------------------------------------------


def test_a_launch_declaring_a_slice_scopes_search_to_it(store, corpus):
    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids}, purpose="ideation")

    unscoped_result = engine.search(store, query="coordinator", k=20)
    scoped_result = engine.search(store, query="coordinator", k=20, launch_id=scoped)

    unscoped_docs = {row["doc_id"] for row in unscoped_result["results"]}
    scoped_docs = {row["doc_id"] for row in scoped_result["results"]}
    assert len(unscoped_docs) > 1
    assert scoped_docs == set(slice_doc_ids)


def test_the_scope_is_reported_on_the_response_it_restricted(store, corpus):
    """A restriction the caller cannot see is indistinguishable from a
    corpus with nothing to say."""
    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    result = engine.search(store, query="coordinator", k=5, launch_id=scoped)
    assert result["scope"] == {"launch_id": scoped, "doc_ids": slice_doc_ids, "reason": "launch_slice"}


def test_an_unscoped_response_carries_no_scope_key_at_all(store, corpus):
    plain = engine.search(store, query="coordinator", k=5)
    assert "scope" not in plain
    unslicedlaunch = bootstrap_launch(store, attrs={"lens_name": "lens-1"})
    assert "scope" not in engine.search(store, query="coordinator", k=5, launch_id=unslicedlaunch)

    # A launch that NAMES assignment rows is scoped even when those rows
    # resolve to nothing: a booking pointing at a roster the ops store does
    # not know is a broken booking, and "unrestricted" is the one reading of
    # it that hands over the whole corpus.
    dangling = bootstrap_launch(store, attrs={"roster_id": "ROST-does-not-exist"})
    assert engine.launch_slice_doc_ids(store, dangling) == []
    assert engine.search(store, query="coordinator", k=5, launch_id=dangling)["results"] == []


def test_a_caller_supplied_filter_cannot_widen_the_launch_slice(store, corpus):
    """The scope is an AND, not a default. A lens naming every source id it
    can see still sees only its own slice."""
    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    result = engine.search(
        store, query="coordinator", k=20, launch_id=scoped,
        filters={"source_ids": [corpus["corpus_source_id"], corpus["inventory_source_id"]]},
    )
    assert {row["doc_id"] for row in result["results"]} == set(slice_doc_ids)


def test_the_slice_scope_survives_an_explicit_inventory_kind_request(store, corpus):
    """The two barriers compose: a lens that both names the inventory kind
    AND holds a slice gets the intersection, which for a prose slice is
    nothing."""
    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    result = engine.search(
        store, query="shared track", k=20,
        launch_id=scoped, filters={"kind": ["inventory"]},
    )
    assert result["results"] == []


def test_similar_is_scoped_by_the_same_launch_slice(store, corpus):
    """Two documents in the slice, three in the corpus: the neighbour inside
    the slice comes back, the one outside it does not."""
    slice_doc_ids = corpus["corpus_doc_ids"][:2]
    outside = corpus["corpus_doc_ids"][2]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    ref = corpus["corpus_chunk_ids"][0]

    unscoped = engine.similar(store, ref, k=50)
    assert outside in {row["doc_id"] for row in unscoped["results"]}

    result = engine.similar(store, ref, k=50, launch_id=scoped)
    assert result["results"]
    assert {row["doc_id"] for row in result["results"]} <= set(slice_doc_ids)
    assert outside not in {row["doc_id"] for row in result["results"]}
    assert result["scope"]["reason"] == "launch_slice"


def test_launch_slice_doc_ids_returns_none_for_every_shape_that_declares_nothing(store, corpus):
    assert engine.launch_slice_doc_ids(store, None) is None
    assert engine.launch_slice_doc_ids(store, "LNCH-does-not-exist") is None
    assert engine.launch_slice_doc_ids(store, bootstrap_launch(store)) is None
    assert engine.launch_slice_doc_ids(store, bootstrap_launch(store, attrs={"lens_name": "lens-1"})) is None
    assert engine.launch_slice_doc_ids(store, bootstrap_launch(store, attrs={"slice_doc_ids": "DOC-x"})) is None
    got = engine.launch_slice_doc_ids(store, bootstrap_launch(store, attrs={"slice_doc_ids": ["DOC-a", "DOC-b"]}))
    assert got == ["DOC-a", "DOC-b"]


def test_a_declared_empty_slice_restricts_to_nothing_rather_than_to_everything(store, corpus):
    """Absent is not empty. ``lens_citations_within_slice`` reads a launch
    that resolves to an empty slice as one where every citation is a
    crossing; the engine reading the same launch as "unrestricted" would be
    fail-open exactly where the audit is fail-closed."""
    empty = bootstrap_launch(store, attrs={"slice_doc_ids": []}, purpose="ideation")
    assert engine.launch_slice_doc_ids(store, empty) == []

    result = engine.search(store, query="coordinator", k=20, launch_id=empty)
    assert result["results"] == []
    assert result["scope"] == {"launch_id": empty, "doc_ids": [], "reason": "launch_slice"}

    ref = corpus["corpus_chunk_ids"][0]
    assert engine.similar(store, ref, k=50, launch_id=empty)["results"] == []
    with pytest.raises(ChunkNotFoundError):
        engine.get_chunk(store, ref, launch_id=empty)


def test_the_engine_resolves_a_slice_the_audit_can_find(store):
    """The scope used to fire only on ``attrs.slice_doc_ids`` while the only
    in-tree producer of lens attrs emitted ``assign_ids``/``roster_id`` — so
    on the shipped booking path it engaged for no launch at all. Both keys
    now resolve, and the export emits the explicit list as well."""
    from trialerror.lens.assign import run_assignment
    from trialerror.lens.roster import add_lens

    from tests._lens_fixtures import build_doc_pool

    pool = build_doc_pool(store, n_docs=12)
    lens_row = add_lens(store, round_id="r-scope", lens_name="lens-1", vantage="v", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id="r-scope", model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=[{"roster_id": lens_row["roster_id"]}],
        slices_per_lens=5, seed="seed-A",
    )

    rows = export_launch_bookable(store, round_id="r-scope")
    assert rows
    row = rows[0]
    assert len(row["attrs"]["slice_doc_ids"]) == row["attrs"]["slice_count"] == 5
    assert set(row["attrs"]["slice_doc_ids"]) <= set(candidate_ids)

    by_explicit = bootstrap_launch(store, attrs={"slice_doc_ids": row["attrs"]["slice_doc_ids"]})
    by_assign = bootstrap_launch(store, attrs={"assign_ids": row["attrs"]["assign_ids"]})
    by_roster = bootstrap_launch(store, attrs={"roster_id": row["attrs"]["roster_id"]})

    expected = set(row["attrs"]["slice_doc_ids"])
    assert set(engine.launch_slice_doc_ids(store, by_explicit) or []) == expected
    assert set(engine.launch_slice_doc_ids(store, by_assign) or []) == expected
    assert set(engine.launch_slice_doc_ids(store, by_roster) or []) == expected


# ---------------------------------------------------------------------------
# 3. the surfaces that are not ranked: ids, quotes and graph edges
# ---------------------------------------------------------------------------


def test_get_chunk_refuses_an_inventory_row_and_an_out_of_slice_document(store, corpus):
    """An id-addressed read skips ranking entirely, so an id learned
    anywhere would otherwise read the row verbatim."""
    inventory_chunk = corpus["inventory_chunk_ids"][0]
    with pytest.raises(ChunkNotFoundError):
        engine.get_chunk(store, inventory_chunk)

    # ... while an ordinary chunk still resolves.
    assert engine.get_chunk(store, corpus["corpus_chunk_ids"][0])["chunk_id"] == corpus["corpus_chunk_ids"][0]

    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    outside = next(
        cid for cid in corpus["corpus_chunk_ids"]
        if engine.get_chunk(store, cid)["doc_id"] not in slice_doc_ids
    )
    with pytest.raises(ChunkNotFoundError):
        engine.get_chunk(store, outside, launch_id=scoped)


def test_resolve_quote_cannot_enumerate_the_inventory_or_the_rest_of_the_corpus(store, corpus):
    """A one-character substring is the whole attack: an unfiltered LIKE over
    every anchor hands back a directory of ids to feed to get_chunk."""
    wide = engine.resolve_quote(store, " a ")
    assert wide["found"] is True
    inventory_docs = set(corpus["inventory_doc_ids"].values())
    assert not ({m["doc_id"] for m in wide["matches"]} & inventory_docs)

    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    narrow = engine.resolve_quote(store, " a ", launch_id=scoped)
    assert {m["doc_id"] for m in narrow["matches"]} <= set(slice_doc_ids)


def test_get_document_outline_and_get_source_carry_the_same_barriers(store, corpus):
    inventory_doc = next(iter(corpus["inventory_doc_ids"].values()))
    with pytest.raises(DocumentNotFoundError):
        engine.get_document_outline(store, inventory_doc)

    listed = engine.get_source(store, corpus["inventory_source_id"])
    assert listed["source"]["source_id"] == corpus["inventory_source_id"]
    assert listed["documents"] == []

    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    scoped_source = engine.get_source(store, corpus["corpus_source_id"], launch_id=scoped)
    assert [d["doc_id"] for d in scoped_source["documents"]] == slice_doc_ids


def test_the_graph_tier_cannot_widen_a_search_past_its_own_allowlist(store, corpus):
    """One relation edge used to be enough: the tier fetches neighbours by
    edge and knew nothing about filters, so its results were fused in
    unfiltered — under a scope whose `scope` key still claimed the slice."""
    inventory_chunk = corpus["inventory_chunk_ids"][0]
    seed_chunk = corpus["corpus_chunk_ids"][0]
    _link_chunks_by_relation(store, seed_chunk, inventory_chunk, launch_id=corpus["launch_id"])

    result = engine.search(store, query="shared track", k=20, mode="auto")
    assert corpus["inventory_source_id"] not in _source_ids(result)

    outside_doc = corpus["corpus_doc_ids"][2]
    outside_chunk = next(
        cid for cid in corpus["corpus_chunk_ids"] if engine.get_chunk(store, cid)["doc_id"] == outside_doc
    )
    _link_chunks_by_relation(store, seed_chunk, outside_chunk, launch_id=corpus["launch_id"])
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": corpus["corpus_doc_ids"][:1]})
    scoped_result = engine.search(store, query="coordinator", k=20, mode="auto", launch_id=scoped)
    assert {row["doc_id"] for row in scoped_result["results"]} <= set(corpus["corpus_doc_ids"][:1])


def test_graph_neighbors_withholds_an_edge_anchored_outside_the_scope(store, corpus):
    inventory_chunk = corpus["inventory_chunk_ids"][0]
    entity_id = _link_chunks_by_relation(
        store, corpus["corpus_chunk_ids"][0], inventory_chunk, launch_id=corpus["launch_id"]
    )
    result = engine.graph_neighbors(store, entity_id)
    anchored_docs = {
        store.knowledge.execute(
            "SELECT doc_id FROM quote_anchor WHERE anchor_id = ?", (e["evidence_anchor"],)
        ).fetchone()["doc_id"]
        for e in result["edges"]
    }
    assert not (anchored_docs & set(corpus["inventory_doc_ids"].values()))


def test_the_five_id_addressed_mcp_tools_are_bound_to_the_session_launch(program_root, platform_root):
    """The reproduction that made this a blocker: through the real registry,
    bound to a scoped launch, `resolve_quote` + `get_chunk` read an inventory
    row verbatim."""
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_corpus_with_inventory(store)
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": corpus["corpus_doc_ids"][:1]})
    store.close()

    tools = build_tools(program_root=program_root, platform_root=platform_root, launch_id=scoped)
    for name in ("get_chunk", "get_source", "get_document_outline", "resolve_quote", "graph_neighbors"):
        assert "launch_id" not in tools[name].input_schema["properties"]

    env = tools["resolve_quote"].handler({"quote": " a "})
    matched = env["result"]["matches"] if env["ok"] else []
    assert {m["doc_id"] for m in matched} <= set(corpus["corpus_doc_ids"][:1])

    env = tools["get_chunk"].handler({"chunk_id": corpus["inventory_chunk_ids"][0]})
    assert env["ok"] is False
    assert env["error"]["code"] == "ChunkNotFoundError"


def test_the_mcp_server_takes_its_launch_from_the_process_not_from_the_agent(program_root, platform_root):
    """The whole reason the scope holds: ``launch_id`` is a ``build_tools``
    argument, so it is fixed when the server process starts. An agent
    passing ``launch_id`` (or anything else) in a tool call cannot reach
    it — the tool schemas do not even name it."""
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_corpus_with_inventory(store)
    slice_doc_ids = corpus["corpus_doc_ids"][:1]
    scoped = bootstrap_launch(store, attrs={"slice_doc_ids": slice_doc_ids})
    other = bootstrap_launch(store, attrs={"slice_doc_ids": corpus["corpus_doc_ids"]})
    store.close()

    tools = build_tools(program_root=program_root, platform_root=platform_root, launch_id=scoped)
    assert "launch_id" not in tools["search"].input_schema["properties"]
    assert "launch_id" not in tools["similar"].input_schema["properties"]

    env = tools["search"].handler(
        {"query": "coordinator", "k": 20, "launch_id": other}
    )
    assert env["ok"] is True
    assert env["result"]["scope"]["launch_id"] == scoped
    assert {row["doc_id"] for row in env["result"]["results"]} == set(slice_doc_ids)


def test_an_unbound_mcp_server_scopes_nothing(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    build_corpus_with_inventory(store)
    store.close()

    tools = build_tools(program_root=program_root, platform_root=platform_root)
    env = tools["search"].handler({"query": "coordinator", "k": 20})
    assert "scope" not in env["result"]
