"""The MANDATED bounded-latency test at the spike's own 10x fixture scale
(design Section 11 v1 deliverable 2's own instruction, verbatim: "your
graph queries MUST carry hop/depth caps + LIMIT guards + a config-capped
default, with the cap enforced at the engine layer and tested at the
spike's 10x fixture scale"). Fixture: ``tests/_graph_scale_fixtures.py``
(adapted from ``spikes/kuzu/fixture_gen.py``'s own ``SCALE_10X`` = 50,000
entities / 200,000 relations).

**What the spike found** (``spikes/kuzu/SPIKE_REPORT.md`` Sec 3, quoted in
:mod:`trialerror.retrieve.engine`'s own module constants): at 10x scale, SQLite's
single-query recursive-CTE k-hop (k=3) and path-between queries hit their
own benchmark's 15s/5s wall-clock abort-cutoff on **100% of sampled runs**
-- unbounded, not merely slow. This test proves the REPLACEMENT
(:func:`~trialerror.retrieve.engine.k_hop_neighbors`/:func:`~trialerror.retrieve.engine.path_between`'s
level-by-level, per-hop LIMIT-guarded Python BFS) stays fast and bounded
at the IDENTICAL scale, on the SAME uniformly-random relation shape that
drove the spike's finding, plus one intentionally adversarial high-degree
hub node to prove the LIMIT guard (not just the hop cap) is what bounds a
single hop's cost.

One test function builds the (expensive) 10x corpus ONCE and runs every
latency assertion against it -- rebuilding it per-assertion would multiply
the (already nontrivial) fixture-build cost for no additional coverage.
"""

from __future__ import annotations

import time

from trialerror.retrieve import engine
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._graph_scale_fixtures import N_ENTITIES_10X, N_RELATIONS_10X, build_graph_scale_corpus

#: Generous ceiling -- MUCH looser than what this actually measures (see
#: the printed numbers), but the point of this bound is categorical, not
#: tight: "does not hit the spike's own 15s/5s abort-cutoff regime",
#: proven with a wide margin rather than a brittle micro-benchmark assertion.
_LATENCY_BOUND_S = 5.0


def _add_hub(store: Store, *, launch_id: str, entity_ids: list[str], degree: int, anchor_id: str) -> str:
    """One additional high-degree "hub" entity connected to ``degree``
    OTHER entities already in the fixture -- the adversarial-topology
    stress case (the spike's own "one adversarial seed/pair" framing,
    Sec 3's latency-table note) that proves the per-hop LIMIT guard, not
    just the hop cap, is what bounds cost: a hub with degree > hop_limit
    would otherwise blow the per-hop query wide open."""
    hub_id = new_id("ENT")
    insert(store, "entity", {"entity_id": hub_id, "name": "hub", "entity_type": "hub", "resolution": "confirmed", "created_by_launch": launch_id, "created_at": now()})
    ts = now()
    rows = [(f"REL-HUB{i:06d}", hub_id, entity_ids[i], "hub_edge", f"hub fact {i}", anchor_id, None, 0.5, ts, None, ts, None, None) for i in range(degree)]
    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO relation (rel_id,src_entity,dst_entity,rel_type,fact_text,evidence_anchor,"
            "extra_anchors,confidence,created_at,expired_at,valid_at,invalid_at,superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return hub_id


def test_bounded_latency_at_10x_fixture_scale(store):
    t_build0 = time.perf_counter()
    corpus = build_graph_scale_corpus(store)
    build_elapsed = time.perf_counter() - t_build0
    assert corpus["n_entities"] == N_ENTITIES_10X
    assert corpus["n_relations"] == N_RELATIONS_10X
    print(f"\n[graph-scale] fixture build (50k entities / 200k relations): {build_elapsed:.2f}s")

    # --- k_hop_neighbors: k=1,2,3 over 8 deterministic seed entities, the
    # SAME uniformly-random-relation shape the spike's own k=3/10x row
    # measured as "unbounded, 100% abort" -----------------------------------
    k_hop_timings: dict[int, list[float]] = {1: [], 2: [], 3: []}
    for seed_entity in corpus["sample_seed_entity_ids"]:
        for k in (1, 2, 3):
            t0 = time.perf_counter()
            result = engine.k_hop_neighbors(store, seed_entity, max_hops=k)
            elapsed = time.perf_counter() - t0
            k_hop_timings[k].append(elapsed)
            assert elapsed < _LATENCY_BOUND_S, f"k_hop_neighbors(max_hops={k}) took {elapsed:.3f}s (bound {_LATENCY_BOUND_S}s) -- spike's own 10x k=3 finding was UNBOUNDED"
            assert result["hops_reached"] <= k

    for k, timings in k_hop_timings.items():
        p_max = max(timings)
        p_avg = sum(timings) / len(timings)
        print(f"[graph-scale] k_hop_neighbors(max_hops={k}) over 8 seeds: avg={p_avg*1000:.1f}ms max={p_max*1000:.1f}ms (bound {_LATENCY_BOUND_S*1000:.0f}ms)")

    # --- path_between: the OTHER spike-flagged unbounded class (100% abort
    # at 10x/depth<=3), 4 deterministic sample pairs, max_hops=3 -----------
    path_timings: list[float] = []
    path_found_flags: list[bool] = []
    for src, dst in corpus["sample_pairs"]:
        t0 = time.perf_counter()
        result = engine.path_between(store, src, dst, max_hops=3)
        elapsed = time.perf_counter() - t0
        path_timings.append(elapsed)
        path_found_flags.append(result["found"])
        assert elapsed < _LATENCY_BOUND_S, f"path_between took {elapsed:.3f}s (bound {_LATENCY_BOUND_S}s) -- spike's own 10x path_between finding was UNBOUNDED"
        assert result["found"] in (True, False)  # both are legitimate outcomes on a random sample

    print(f"[graph-scale] path_between(max_hops=3) over 4 pairs: avg={sum(path_timings)/len(path_timings)*1000:.1f}ms max={max(path_timings)*1000:.1f}ms found={path_found_flags}")

    # --- adversarial hub: an entity with degree 2000 (>> DEFAULT_HOP_LIMIT
    # = 500) proves the per-hop LIMIT GUARD, not merely the hop cap, is
    # what bounds a single hop's cost -- the spike's own "one adversarial
    # seed/pair drove the search space far past anything seen at smaller
    # k" finding, deliberately reproduced here rather than left hypothetical.
    anchor_id = store.knowledge.execute("SELECT anchor_id FROM quote_anchor LIMIT 1").fetchone()["anchor_id"]
    hub_degree = 2000
    hub_id = _add_hub(store, launch_id=corpus["launch_id"], entity_ids=[f"ENT-SCALE{i:07d}" for i in range(hub_degree)], degree=hub_degree, anchor_id=anchor_id)

    # max_hops=1 here (not 2): isolates the assertion to exactly ONE hop's
    # LIMIT guard -- at 2+ hops the leaf entities' OWN edges into the
    # surrounding 200k-relation random graph would add a second capped
    # batch on top, muddying "one hop, one cap" into a less legible number.
    t0 = time.perf_counter()
    hub_result = engine.k_hop_neighbors(store, hub_id, max_hops=1)
    hub_elapsed = time.perf_counter() - t0
    print(f"[graph-scale] k_hop_neighbors on a degree-{hub_degree} adversarial hub, max_hops=1: {hub_elapsed*1000:.1f}ms, truncated={hub_result['truncated']}, count={hub_result['count']}")
    assert hub_elapsed < _LATENCY_BOUND_S
    assert hub_result["truncated"] is True
    assert hub_result["count"] == engine.DEFAULT_HOP_LIMIT  # capped, not the full 2000-degree fan-out
