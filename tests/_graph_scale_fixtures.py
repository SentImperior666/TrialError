"""Not a test module (pytest only collects ``test_*.py``) -- a deterministic
10x-scale (50,000 entities / 200,000 relations) graph fixture builder for
the mandated bounded-latency test (design Section 11 v1 deliverable 2's
own instruction: "reuse/adapt spikes/kuzu/fixture_gen.py").

**Adapted, not imported**, from ``spikes/kuzu/fixture_gen.py``: that
script targets a STANDALONE minimal SQLite DB (no ``quote_anchor``/
``document``/``source`` tables, ``relation.evidence_anchor`` FK dropped --
see its own module docstring, deviation 1) built as a script, not an
importable package (``spikes/kuzu/`` has no ``__init__.py`` and is an
explicitly isolated spike lane per its own mission brief: "nothing in
trialerror/ was touched this wave"). This module instead populates a REAL
:class:`~trialerror.stores.store.Store`'s ``knowledge.db`` -- same scale (10x:
50k/200k, matching ``spikes/kuzu/fixture_gen.py``'s own ``SCALE_10X``),
same deterministic seeded-RNG generation shape (entity/relation type
vocab, uniformly-random src/dst pairs -- the exact graph shape that drove
the spike's SQLite recursive-CTE k=3/path-between finding unbounded), but
against the REAL DDL, which means ``relation.evidence_anchor`` is a
genuine ``NOT NULL`` same-file FK to ``quote_anchor(anchor_id)``
(``PRAGMA foreign_keys=ON``, ``trialerror/stores/connection.py``) -- so a small
POOL of real ``quote_anchor`` rows (tied to one real, tiny document/source)
is built first and referenced cyclically across all 200,000 relations,
exactly the "anchor pool" convention ``fixture_gen.py`` itself uses for
its own (FK-free) fixture.

Bulk ``executemany`` throughout (bypassing the validated
``trialerror.stores.writer.insert`` per-row API and the M2 jobs-ledger/handler
machinery entirely) purely for fixture-build SPEED -- the exact
``tests/_retrieve_fixtures.py::build_bulk_corpus`` precedent for its own
15k-chunk latency fixture, applied here at graph scale instead.
"""

from __future__ import annotations

import random
from typing import Any

from trialerror.ingest.anchors import sha256_hex
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["N_ENTITIES_10X", "N_RELATIONS_10X", "build_graph_scale_corpus"]

#: matches spikes/kuzu/fixture_gen.py's SCALE_10X exactly (50_000, 200_000).
N_ENTITIES_10X = 50_000
N_RELATIONS_10X = 200_000

_ENTITY_TYPES = ["mechanic", "system", "source", "concept", "artifact", "person", "register"]
_REL_TYPES = ["derives_from", "conflicts_with", "resembles", "co_occurs_with", "supersedes_mechanic", "cites"]


def _bootstrap_launch(store: Store) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "graph-scale fixture", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-graph-scale",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": "graph-scale fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    return launch_id


def _build_anchor_pool(store: Store, *, launch_id: str, pool_size: int) -> list[str]:
    """A handful of REAL ``quote_anchor`` rows (over one small, real
    document/source) -- enough to satisfy ``relation.evidence_anchor``'s
    genuine FK at scale without building 200,000 distinct anchors (they're
    cyclically reused, exactly as ``fixture_gen.py``'s own synthetic
    ``anchor_pool`` is)."""
    source_id = new_id("SRC")
    insert(
        store, "source",
        {
            "source_id": source_id, "kind": "report", "title": "Graph-scale fixture source",
            "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )
    doc_id = new_id("DOC")
    insert(
        store, "document",
        {
            "doc_id": doc_id, "source_id": source_id, "rel_path": "archive/graph_scale.md",
            "media_type": "md", "normalizer_id": "fixture", "normalizer_version": "1",
            "sha256": "0" * 64, "status": "indexed",
        },
    )
    anchor_ids: list[str] = []
    for i in range(pool_size):
        anchor_id = new_id("ANC")
        char_start, char_end = i * 40, i * 40 + 30
        insert(
            store, "quote_anchor",
            {
                "anchor_id": anchor_id, "doc_id": doc_id, "chunk_id": None, "page_number": None,
                "char_start": char_start, "char_end": char_end, "stream_fn": "stream_v1",
                "doc_sha256": "0" * 64, "quote_sha256": sha256_hex(f"fixture anchor {i}"),
                "quote_text": f"fixture anchor {i}", "created_by_launch": launch_id, "created_ts": now(),
            },
        )
        anchor_ids.append(anchor_id)
    return anchor_ids


def build_graph_scale_corpus(
    store: Store,
    *,
    n_entities: int = N_ENTITIES_10X,
    n_relations: int = N_RELATIONS_10X,
    seed: int = 42,
    anchor_pool_size: int = 500,
) -> dict[str, Any]:
    """Build (bulk-insert) a deterministic ``n_entities``/``n_relations``
    graph directly into ``store.knowledge`` -- fully seeded (no wall-clock/
    OS entropy in the graph SHAPE; ``created_at`` timestamps use
    :func:`trialerror.util.timeutil.now` once per call, not per row, matching
    ``build_bulk_corpus``'s own single-``now()``-per-run convention). Every
    relation is LIVE (``expired_at``/``invalid_at`` both NULL) -- this
    fixture exercises the default live-view BFS path, the one the spike's
    own k-hop/path-between latency table is about.

    Returns ``{"launch_id", "entity_ids" (full list), "n_entities",
    "n_relations", "sample_seed_entity_ids" (8, deterministic), "sample_pairs"
    (4 (src, dst) tuples, deterministic) -- the exact "8-seed / 4-pair
    deterministic sample" shape ``spikes/kuzu/bench.py`` itself uses.}``.
    """
    rng = random.Random(seed)
    launch_id = _bootstrap_launch(store)
    anchor_pool = _build_anchor_pool(store, launch_id=launch_id, pool_size=anchor_pool_size)

    entity_ids = [f"ENT-SCALE{i:07d}" for i in range(n_entities)]
    ts = now()
    entity_rows = [
        (eid, f"Entity {eid}", rng.choice(_ENTITY_TYPES), None, None, None, "confirmed", None, launch_id, ts)
        for eid in entity_ids
    ]
    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO entity (entity_id,name,entity_type,aliases,summary,attributes,"
            "resolution,merge_group,created_by_launch,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            entity_rows,
        )

    rel_rows = []
    for i in range(n_relations):
        src = entity_ids[rng.randrange(n_entities)]
        dst = entity_ids[rng.randrange(n_entities)]
        while dst == src:
            dst = entity_ids[rng.randrange(n_entities)]
        rel_rows.append(
            (
                f"REL-SCALE{i:07d}", src, dst, rng.choice(_REL_TYPES), f"Synthetic fixture fact for relation {i}.",
                anchor_pool[i % len(anchor_pool)], None, round(rng.uniform(0.4, 0.99), 3),
                ts, None, ts, None, None,
            )
        )
    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO relation (rel_id,src_entity,dst_entity,rel_type,fact_text,evidence_anchor,"
            "extra_anchors,confidence,created_at,expired_at,valid_at,invalid_at,superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rel_rows,
        )

    sample_rng = random.Random(seed ^ 0xC0FFEE)
    sample_seed_entity_ids = [entity_ids[sample_rng.randrange(n_entities)] for _ in range(8)]
    sample_pairs = [(entity_ids[sample_rng.randrange(n_entities)], entity_ids[sample_rng.randrange(n_entities)]) for _ in range(4)]

    return {
        "launch_id": launch_id,
        "n_entities": n_entities,
        "n_relations": n_relations,
        "sample_seed_entity_ids": sample_seed_entity_ids,
        "sample_pairs": sample_pairs,
    }
