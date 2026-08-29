"""The hybrid retrieval engine. Design Section 7 (Retrieval API contract) +
Section 12 (M8 row): "hybrid engine (fts->vec->RRF), citation bundle,
untrusted-wrap, serving-path license fence (Section 7), resolve_quote."

This module is the ONE place ``SearchResponse``-shaped results get built.
``trialerror/cli/query.py`` (the ``query`` CLI group) and
``trialerror/mcp/knowledge.py`` (the ``trialerror-knowledge`` MCP server) are both
thin wrappers calling straight into these functions -- neither re-derives
citation blocks, fencing, or the untrusted-wrap.

**Pipeline (Section 7, "Q5 applied"):**

1. FTS5/BM25 prefilter to <=500 candidates (:mod:`trialerror.retrieve.ftssearch`).
2. Vector rerank of exactly that candidate set with the program's
   configured embed backend (:mod:`trialerror.retrieve.vecsearch`) -- ``mode``
   ``"auto"``/``"hybrid"`` both run this two-stage pipeline; ``"fts"``/
   ``"vector"`` run exactly one tier.
3. Reciprocal-rank fusion (:mod:`trialerror.retrieve.fusion`) across whichever
   tiers actually ran.
4. Citation-bundle assembly + the F3 serving-path license fence
   (:mod:`trialerror.retrieve.fence`) + the untrusted-wrap
   (:mod:`trialerror.retrieve.wrap`) -- applied uniformly to every result row,
   never conditionally skipped for a caller that "should already know
   better" (the whole point of an engine-level fence).

``mode="summary"`` (build-v2-summary, design Section 11 "summary tier (L1
overviews)" / Section 7 pipeline step 5) is a SEPARATE, summary-FIRST
search path over ``knowledge.summary`` -- entirely independent of the
fts/vector/RRF pipeline above (a ``summary_id`` lives in a different
id-space than a ``chunk_id``, so there is nothing to reciprocal-rank-fuse
against); see :func:`_search_summary_tier`. ``mode="graph"`` is a
separate lane's own tier -- see its own section of this module for
current behavior (design Section 7: "engine reports what it used").
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Sequence

from trialerror.events.api import append_event
from trialerror.ingest.backends import EmbedBackend, load_embed_backend
from trialerror.retrieve.errors import (
    ChunkNotFoundError,
    DocumentNotFoundError,
    EntityNotFoundError,
    InvalidSearchModeError,
    SourceNotFoundError,
)
from trialerror.retrieve.fence import citation_quote, excerpt_words, fence_chunk_text, is_fenced_license, source_license_tier
from trialerror.retrieve.fusion import reciprocal_rank_fusion
from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT, fts_search
from trialerror.retrieve.vecsearch import fetch_native_knn, fetch_vectors, rank_by_query_vector, vec_backend_for, vec_table_exists
from trialerror.retrieve.wrap import untrusted_wrap
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, deserialize_vector_fallback, try_load_sqlite_vec
from trialerror.stores.writer import get as store_get
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "SEARCH_MODES",
    "DEFAULT_K",
    "search",
    "get_chunk",
    "get_source",
    "get_document_outline",
    "resolve_quote",
    "similar",
    "graph_neighbors",
    "k_hop_neighbors",
    "path_between",
    "graph_tier_candidates",
    "DEFAULT_MAX_HOPS",
    "ABSOLUTE_MAX_HOPS_CEILING",
    "DEFAULT_HOP_LIMIT",
    "ABSOLUTE_HOP_LIMIT_CEILING",
    "corpus_stats",
    "list_requests",
]

SEARCH_MODES: tuple[str, ...] = ("auto", "fts", "vector", "hybrid", "graph", "summary")
DEFAULT_K = 12

#: design Section 11 deliverable 2's MANDATED cap, resolving the spike's own
#: finding (``spikes/kuzu/SPIKE_REPORT.md`` Sec 3/5: "SQLite k-hop k=3/
#: path-between queries are UNBOUNDED at 10x scale" -- the benchmark's own
#: wall-clock abort-cutoff fired on 100% of sampled 10x-scale runs for both
#: query classes). Default hop count for :func:`k_hop_neighbors`/
#: :func:`path_between` when a caller doesn't name one.
DEFAULT_MAX_HOPS = 2

#: The hard ceiling ``trialerror.toml [retrieve.graph] max_hops_ceiling`` cannot
#: be configured past (a program MAY tighten it below this, never loosen it
#: above) -- a second, code-level backstop so a config mistake can't
#: silently reintroduce the spike's unbounded-cost regime.
ABSOLUTE_MAX_HOPS_CEILING = 5

#: The per-hop LIMIT GUARD (design Section 11 deliverable 2: "LIMIT
#: guards") every :func:`_fetch_relation_edges_touching` call applies --
#: this, not the hop cap alone, is what bounds a single hop's cost when a
#: frontier node is a high-degree hub: worst-case total cost across a
#: k_hop_neighbors/path_between call is ``max_hops * hop_limit`` edge rows,
#: REGARDLESS of corpus size or hub degree (the exact property the spike's
#: single-query recursive-CTE k-hop lacked -- it has no per-level cap at
#: all, see SPIKE_REPORT.md Sec 3's uncensored-run finding).
DEFAULT_HOP_LIMIT = 500

#: Hard ceiling for ``trialerror.toml [retrieve.graph] hop_limit`` (same
#: code-level-backstop reasoning as :data:`ABSOLUTE_MAX_HOPS_CEILING`).
ABSOLUTE_HOP_LIMIT_CEILING = 5000

#: :func:`graph_tier_candidates`'s own hop depth -- fixed at 1, never a
#: caller-configurable parameter, because that function is a RECALL-
#: widening step over an already-relevant seed set (design Section 7
#: pipeline step 4: "entity/claim neighbors as additional candidates"), not
#: an open-ended traversal; deeper expansion belongs to
#: :func:`k_hop_neighbors` instead.
DEFAULT_GRAPH_TIER_HOPS = 1

#: Seed chunks :func:`graph_tier_candidates` looks up entities for are
#: capped here so a large fused fts/vector result can't blow up the number
#: of anchor/entity lookups the graph tier issues per :func:`search` call.
DEFAULT_GRAPH_TIER_SEED_LIMIT = 20

#: design Section 4.1's ``element.type`` taxonomy entries this package
#: treats as "structural" for :func:`get_document_outline` (design: "titles/
#: sections/tables").
_OUTLINE_ELEMENT_TYPES = frozenset({"Title", "Header", "Table", "FigureCaption"})


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _load_program_config(store: Store) -> dict[str, Any]:
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = store.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def _graph_cap_config(store: Store) -> tuple[int, int]:
    """``(max_hops_ceiling, default_hop_limit)`` resolved from ``trialerror.toml
    [retrieve.graph]`` (``max_hops_ceiling``/``hop_limit``), each clamped
    into ``[1, ABSOLUTE_*_CEILING]`` -- a program's config can only ever
    TIGHTEN these below the code-level ceiling, never loosen past it (see
    :data:`ABSOLUTE_MAX_HOPS_CEILING`/:data:`ABSOLUTE_HOP_LIMIT_CEILING`'s
    own docstrings for why). Unconfigured (the zero-setup default) resolves
    to the ceilings themselves."""
    config = _load_program_config(store)
    graph_cfg = config.get("retrieve", {}).get("graph", {})
    hops_ceiling = int(graph_cfg.get("max_hops_ceiling", ABSOLUTE_MAX_HOPS_CEILING))
    hops_ceiling = max(1, min(hops_ceiling, ABSOLUTE_MAX_HOPS_CEILING))
    hop_limit = int(graph_cfg.get("hop_limit", DEFAULT_HOP_LIMIT))
    hop_limit = max(1, min(hop_limit, ABSOLUTE_HOP_LIMIT_CEILING))
    return hops_ceiling, hop_limit


def _resolve_max_hops(store: Store, max_hops: int | None, hop_limit: int | None) -> tuple[int, int]:
    """Validate/resolve the ``(max_hops, hop_limit)`` pair every capped
    graph query (:func:`k_hop_neighbors`/:func:`path_between`) uses.
    ``max_hops=None`` -> :data:`DEFAULT_MAX_HOPS` (2, the mandated default);
    an EXPLICIT value past the program's configured ceiling is a clean
    refusal (:class:`ValueError`), never a silent clamp -- the same
    refuse-visibly posture this codebase applies elsewhere (cost-gate
    refusal, XID-target-missing, ...) rather than quietly doing less than
    what was asked. ``hop_limit=None`` -> the program's configured default
    (:func:`_graph_cap_config`); an explicit value is clamped into
    ``[1, ABSOLUTE_HOP_LIMIT_CEILING]`` (loosening the LIMIT guard is a
    per-call perf/recall tradeoff, not a correctness one, so this one is
    clamped rather than refused)."""
    hops_ceiling, default_hop_limit = _graph_cap_config(store)
    resolved_hops = DEFAULT_MAX_HOPS if max_hops is None else max_hops
    if resolved_hops < 1 or resolved_hops > hops_ceiling:
        raise ValueError(
            f"max_hops must be between 1 and this program's configured ceiling "
            f"({hops_ceiling}; trialerror.toml [retrieve.graph] max_hops_ceiling, itself capped at "
            f"{ABSOLUTE_MAX_HOPS_CEILING} -- spikes/kuzu/SPIKE_REPORT.md's own mandated finding: "
            f"SQLite k-hop/path queries are unbounded past k=2 at 10x scale), got {resolved_hops!r}"
        )
    resolved_limit = default_hop_limit if hop_limit is None else max(1, min(int(hop_limit), ABSOLUTE_HOP_LIMIT_CEILING))
    return resolved_hops, resolved_limit


def _relation_bitemporal_clause(*, as_of: str | None, as_of_tx: str | None) -> tuple[list[str], list[Any]]:
    """The bi-temporal live/point-in-time predicate :func:`graph_neighbors`
    applies, factored out so :func:`k_hop_neighbors`/:func:`path_between`/
    :func:`_fetch_relation_edges_touching` share IDENTICAL semantics rather
    than re-deriving them (default, no ``as_of``/``as_of_tx``: the live
    view -- ``expired_at IS NULL`` and ``invalid_at IS NULL``)."""
    clauses: list[str] = []
    params: list[Any] = []
    if as_of_tx:
        clauses.append("created_at <= ?")
        clauses.append("(expired_at IS NULL OR expired_at > ?)")
        params.extend([as_of_tx, as_of_tx])
    else:
        clauses.append("expired_at IS NULL")
    if as_of:
        clauses.append("(valid_at IS NULL OR valid_at <= ?)")
        clauses.append("(invalid_at IS NULL OR invalid_at > ?)")
        params.extend([as_of, as_of])
    else:
        clauses.append("invalid_at IS NULL")
    return clauses, params


def _fetch_relation_edges_touching(
    store: Store, node_ids: Sequence[str] | set[str], *, as_of: str | None, as_of_tx: str | None, limit: int
) -> list[dict[str, Any]]:
    """One BOUNDED hop of relation-edge expansion: every live (or
    ``as_of``/``as_of_tx``-filtered) relation touching ANY entity in
    ``node_ids``, capped at ``limit`` distinct edges -- the per-hop LIMIT
    GUARD that keeps :func:`k_hop_neighbors`/:func:`path_between`'s
    worst-case cost bounded by ``max_hops * limit`` regardless of corpus
    size or hub-entity degree (spike finding: SQLite's own single-query
    recursive-CTE k-hop has NO such per-level cap at all -- exactly what
    goes unbounded at 10x scale, ``spikes/kuzu/SPIKE_REPORT.md`` Sec 3).

    Queried as two separate ``src_entity IN (...)`` / ``dst_entity IN
    (...)`` statements, each LIMIT-guarded and each hitting its own index
    (``idx_relation_src``/``idx_relation_dst``) -- not one ``OR``
    predicate that would defeat both indexes -- then deduplicated by
    ``rel_id`` in Python and truncated to ``limit`` overall."""
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return []
    clauses, bt_params = _relation_bitemporal_clause(as_of=as_of, as_of_tx=as_of_tx)
    ph = ",".join("?" for _ in ids)
    where = " AND ".join(clauses)
    seen: dict[str, dict[str, Any]] = {}
    for col in ("src_entity", "dst_entity"):
        sql = f"SELECT * FROM relation WHERE {col} IN ({ph}) AND {where} LIMIT ?"
        for r in store.knowledge.execute(sql, [*ids, *bt_params, limit]):
            d = dict(r)
            seen.setdefault(d["rel_id"], d)
    return list(seen.values())[:limit]


def _resolve_embed_backend(store: Store) -> tuple[str, EmbedBackend]:
    """Same resolution M7's ``embed``/``index`` handlers use (design
    Section 4.1: model-keyed embedding cache) -- retrieval-time query
    embedding MUST use the same ``model_key`` the corpus was embedded
    under, or ``vec_chunks__<model_key>`` lookups silently miss everything.
    Defaults to the fake backend when unconfigured (same zero-setup
    default M7 ships), which is exactly what makes the M8 15k-chunk
    latency fixture GPU-free (design Section 13 flag F18)."""
    config = _load_program_config(store)
    embed_cfg = config.get("ingest", {}).get("embed", {})
    backend = load_embed_backend(embed_cfg)
    return backend.model_key, backend


def _filtered_chunk_ids(store: Store, filters: Mapping[str, Any] | None) -> list[str] | None:
    """``None`` means "no restriction, use every chunk"; otherwise the
    exact (possibly empty) list of chunk ids matching every given filter
    key (design Section 7 ``SearchRequest.filters``:
    ``source_ids``/``kind``/``license_tier``/``year``)."""
    if not filters:
        return None
    clauses: list[str] = []
    params: list[Any] = []
    if filters.get("source_ids"):
        ids = list(filters["source_ids"])
        clauses.append(f"source.source_id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    if filters.get("kind"):
        kinds = list(filters["kind"])
        clauses.append(f"source.kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    if filters.get("license_tier"):
        tiers = list(filters["license_tier"])
        clauses.append(f"source.license_tier IN ({','.join('?' for _ in tiers)})")
        params.extend(tiers)
    if filters.get("year"):
        years = list(filters["year"])
        clauses.append(f"source.year IN ({','.join('?' for _ in years)})")
        params.extend(years)
    if not clauses:
        return None
    sql = (
        "SELECT chunk.chunk_id FROM chunk "
        "JOIN document ON document.doc_id = chunk.doc_id "
        "JOIN source ON source.source_id = document.source_id "
        f"WHERE {' AND '.join(clauses)}"
    )
    rows = store.knowledge.execute(sql, params).fetchall()
    return [r["chunk_id"] for r in rows]


def _fetch_chunk_context(store: Store, chunk_ids: Sequence[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Batch-fetch chunk/document/source/anchor rows for a set of
    ``chunk_ids`` in a handful of ``IN (...)`` queries -- avoids an N+1
    query per result row when building a page of search results."""
    ids = list(dict.fromkeys(chunk_ids))
    empty: dict[str, dict[str, dict[str, Any]]] = {"chunks": {}, "documents": {}, "sources": {}, "anchors": {}}
    if not ids:
        return empty
    ph = ",".join("?" for _ in ids)
    chunks = {r["chunk_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM chunk WHERE chunk_id IN ({ph})", ids)}
    doc_ids = {c["doc_id"] for c in chunks.values()}
    documents: dict[str, dict[str, Any]] = {}
    if doc_ids:
        dph = ",".join("?" for _ in doc_ids)
        documents = {r["doc_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM document WHERE doc_id IN ({dph})", list(doc_ids))}
    source_ids = {d["source_id"] for d in documents.values()}
    sources: dict[str, dict[str, Any]] = {}
    if source_ids:
        sph = ",".join("?" for _ in source_ids)
        sources = {r["source_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM source WHERE source_id IN ({sph})", list(source_ids))}
    anchors: dict[str, dict[str, Any]] = {}
    for r in store.knowledge.execute(f"SELECT * FROM quote_anchor WHERE chunk_id IN ({ph}) ORDER BY created_ts ASC", ids):
        d = dict(r)
        anchors.setdefault(d["chunk_id"], d)  # first (earliest) anchor is the chunk's primary one
    return {"chunks": chunks, "documents": documents, "sources": sources, "anchors": anchors}


def _log_unfenced_bypass(store: Store, *, chunk_ids: list[str], source_ids: list[str], launch_id: str | None) -> None:
    """Design Section 7: full text stays available to "explicitly non-agent
    surfaces (``trialerror query search --unfenced``, human-flagged and logged
    as an event)". Called exactly once per :func:`search` invocation that
    actually bypassed the fence for at least one result -- never on a call
    where nothing needed fencing in the first place."""
    append_event(
        store,
        event_type="retrieval_unfenced_bypass",
        payload={"chunk_ids": chunk_ids, "source_ids": source_ids, "surface": "cli:query.search --unfenced"},
        launch_id=launch_id,
    )


def _build_result_row(
    chunk_id: str,
    *,
    rank: int,
    score: float,
    fusion: Mapping[str, int],
    ctx: Mapping[str, Mapping[str, dict[str, Any]]],
    unfenced: bool = False,
) -> dict[str, Any] | None:
    """Build one ``SearchResponse.results[]`` row (design Section 7).
    Returns ``None`` when the chunk has no resolvable citation block (no
    source, or no anchor) -- the caller drops such a chunk from the result
    set entirely rather than emit a row that fails the "a result row
    without a citation block is a bug" contract."""
    chunk = ctx["chunks"].get(chunk_id)
    if chunk is None:
        return None
    doc = ctx["documents"].get(chunk["doc_id"])
    source = ctx["sources"].get(doc["source_id"]) if doc else None
    anchor = ctx["anchors"].get(chunk_id)
    if source is None or anchor is None:
        return None

    fenced = is_fenced_license(source.get("license_tier")) and not unfenced
    if fenced:
        text = fence_chunk_text(
            chunk_text=chunk["text"],
            source_title=source["title"],
            page_start=chunk.get("page_start"),
            page_end=chunk.get("page_end"),
            seq=chunk["seq"],
            token_count=chunk["token_count"],
        )
    else:
        text = chunk["text"]

    return {
        "rank": rank,
        "score": score,
        "fusion": dict(fusion),
        "chunk_id": chunk_id,
        "doc_id": chunk["doc_id"],
        "source_id": source["source_id"],
        "text": untrusted_wrap(text),
        "fenced": fenced,
        "citation": {
            "source_id": source["source_id"],
            "title": source["title"],
            "license_tier": source["license_tier"],
            "anchor": {
                "anchor_id": anchor["anchor_id"],
                "page": anchor.get("page_number"),
                "char_start": anchor["char_start"],
                "char_end": anchor["char_end"],
            },
            "quote": citation_quote(chunk["text"], fenced=fenced),
        },
    }


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def search(
    store: Store,
    *,
    query: str,
    k: int = DEFAULT_K,
    mode: str = "auto",
    filters: Mapping[str, Any] | None = None,
    tiers: Sequence[str] | None = None,
    as_of: str | None = None,
    unfenced: bool = False,
    launch_id: str | None = None,
) -> dict[str, Any]:
    """Design Section 7's ``search`` -- the one function every surface
    (MCP ``search`` tool, ``trialerror query search``, M9's citecheck) calls.

    ``unfenced`` is the CLI-only, human-flagged, logged escape hatch
    (Section 7: "``trialerror query search --unfenced``, human-flagged and
    logged as an event") -- the MCP server never passes it (see
    ``trialerror/mcp/knowledge.py``'s module docstring), so agent surfaces
    structurally cannot request it. ``as_of`` is accepted for
    ``SearchRequest`` schema fidelity but is a no-op for chunk search in
    v0: chunks are not bi-temporal (only ``claim``/``relation`` are,
    design Section 4.1) -- :func:`graph_neighbors` is where ``as_of``
    actually filters.
    """
    if mode not in SEARCH_MODES:
        raise InvalidSearchModeError(f"search: mode must be one of {SEARCH_MODES!r}, got {mode!r}")
    requested_tiers = set(tiers) if tiers else {"fts", "vector", "graph", "summary"}
    t0 = time.perf_counter()

    candidate_ids = _filtered_chunk_ids(store, filters)
    if candidate_ids is not None and not candidate_ids:
        # a filter matched zero chunks -- an empty, well-formed response,
        # never an error (an over-narrow filter is a normal outcome).
        return {
            "ok": True,
            "query_id": new_id("QRY"),
            "tiers_used": [],
            "results": [],
            "stats": {"fts_candidates": 0, "vector_scored": 0, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)},
        }

    if mode == "summary":
        # build-v2-summary: a summary-first search path, entirely separate
        # from the fts/vector/RRF pipeline below (module docstring) --
        # "summary" still honors the SAME requested_tiers gate every other
        # mode/tier pairing does (a caller naming mode="summary" but
        # excluding "summary" from tiers gets an empty, well-formed
        # response, not a silent override of its own filter).
        if "summary" not in requested_tiers:
            return {
                "ok": True,
                "query_id": new_id("QRY"),
                "tiers_used": [],
                "results": [],
                "stats": {"fts_candidates": 0, "vector_scored": 0, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)},
            }
        return _search_summary_tier(store, query=query, k=k, candidate_chunk_ids=candidate_ids, unfenced=unfenced, launch_id=launch_id, t0=t0)

    want_fts = mode in ("auto", "fts", "hybrid", "graph") and "fts" in requested_tiers
    want_vector = mode in ("auto", "vector", "hybrid", "graph") and "vector" in requested_tiers
    want_graph = mode in ("auto", "hybrid", "graph") and "graph" in requested_tiers

    tier_rankings: dict[str, list[str]] = {}
    stats: dict[str, Any] = {"fts_candidates": 0, "vector_scored": 0}

    if want_fts and query.strip():
        fts_hits = fts_search(store, query, limit=DEFAULT_FTS_CANDIDATE_LIMIT, chunk_id_allowlist=candidate_ids)
        tier_rankings["fts"] = [h["chunk_id"] for h in fts_hits]
        stats["fts_candidates"] = len(fts_hits)

    if want_vector and query.strip():
        # design Section 7 step 2: vector-score exactly the FTS candidate
        # set in the two-stage modes; in pure "vector" mode there is no FTS
        # stage, so the universe is the (filtered) whole corpus instead.
        model_key, backend = _resolve_embed_backend(store)

        # B.4b (build-arxiv-kaggle-index session, spikes/index_bakeoffs/
        # BAKEOFF_REPORT.md Sec B.4b): mode="vector" with NO filters is the
        # genuinely UNBOUNDED case that bake-off names as the native-MATCH
        # trigger (fetch_vectors's IN-list hits a hard 32,766-variable
        # ceiling and a ~20GB memory-pressure risk at scale -- Sec B.3).
        # Only engages when THIS model_key's table was actually built as a
        # real vec0 table (vec_backend_for -- TRIALERROR_VEC_BACKEND=sqlite_vec
        # at index-build time, opt-in, per trialerror.stores.vecindex.
        # ensure_vec_table's own B.4a default); every other combination
        # below (filtered mode="vector", the two-stage FTS-prefiltered
        # modes, or a fallback-backend table) is BYTE-IDENTICAL to this
        # function's pre-B.4b behavior -- nothing here changes the default
        # fallback path.
        if mode == "vector" and candidate_ids is None and vec_table_exists(store, model_key) and vec_backend_for(store, model_key) == VecBackend.SQLITE_VEC:
            query_vector = backend.embed_batch([query], kind="query")[0]
            ranked = fetch_native_knn(store, model_key, query_vector, k=max(k, 0))
            tier_rankings["vector"] = [cid for cid, _ in ranked]
            stats["vector_scored"] = len(ranked)
        else:
            if mode == "vector":
                vector_universe = candidate_ids if candidate_ids is not None else _all_chunk_ids(store)
            else:
                vector_universe = tier_rankings.get("fts", [])
            if vector_universe and vec_table_exists(store, model_key):
                query_vector = backend.embed_batch([query], kind="query")[0]
                vectors = fetch_vectors(store, model_key, vector_universe)
                ranked = rank_by_query_vector(query_vector, vectors)
                tier_rankings["vector"] = [cid for cid, _ in ranked]
                stats["vector_scored"] = len(ranked)

    if want_graph and query.strip():
        # design Section 7 pipeline step 4 / Section 11 deliverable 2:
        # "optional graph tier (entity/claim neighbors as additional
        # candidates)" -- widens recall from the already-ranked fts/vector
        # seeds via one bounded hop (see graph_tier_candidates's own
        # docstring); contributes nothing (tier absent from tiers_used,
        # design Section 7: "engine reports what it used") for a corpus
        # whose KG hasn't been populated yet.
        graph_seed_ids = tier_rankings.get("vector") or tier_rankings.get("fts") or []
        if graph_seed_ids:
            graph_chunk_ids = graph_tier_candidates(store, graph_seed_ids)
            if graph_chunk_ids:
                tier_rankings["graph"] = graph_chunk_ids
                stats["graph_candidates"] = len(graph_chunk_ids)

    fused = reciprocal_rank_fusion(tier_rankings) if tier_rankings else []
    top = fused[: max(k, 0)]
    ctx = _fetch_chunk_context(store, [cid for cid, _, _ in top])

    bypassed_chunk_ids: list[str] = []
    bypassed_source_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    for i, (cid, score, fusion_ranks) in enumerate(top):
        would_fence = ctx["sources"].get(
            (ctx["documents"].get(ctx["chunks"].get(cid, {}).get("doc_id"), {}) or {}).get("source_id"), {}
        ).get("license_tier")
        row = _build_result_row(cid, rank=i + 1, score=score, fusion=fusion_ranks, ctx=ctx, unfenced=unfenced)
        if row is None:
            continue
        if unfenced and is_fenced_license(would_fence):
            bypassed_chunk_ids.append(cid)
            bypassed_source_ids.add(row["source_id"])
        results.append(row)

    if bypassed_chunk_ids:
        _log_unfenced_bypass(store, chunk_ids=bypassed_chunk_ids, source_ids=sorted(bypassed_source_ids), launch_id=launch_id)

    stats["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "ok": True,
        "query_id": new_id("QRY"),
        "tiers_used": sorted(tier_rankings.keys()),
        "results": results,
        "stats": stats,
    }


def _all_chunk_ids(store: Store) -> list[str]:
    return [r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk")]


# ---------------------------------------------------------------------------
# summary tier (build-v2-summary, design Section 11 "summary tier (L1
# overviews)" / Section 7 pipeline step 5)
# ---------------------------------------------------------------------------


def _build_summary_result_row(
    store: Store, row: Mapping[str, Any], *, rank: int, score: float, unfenced: bool
) -> dict[str, Any] | None:
    """Build one summary-tier result row -- the same "citation block or
    drop the row" discipline :func:`_build_result_row` applies to chunks
    (design Section 7: "a result row without a citation block is a bug").
    ``cited_sources`` exposes every one of the summary's
    ``source_doc_ids`` (the build brief's "returning L1 overviews with
    their doc citations", plural) as ``{doc_id, source_id, title,
    license_tier}``; the flat ``citation``/``doc_id``/``source_id`` fields
    (matching every other tier's row shape) report the FIRST cited doc as
    the primary one -- exact for a ``document``-kind summary (its
    ``source_doc_ids`` is always a single-element list), a reasonable
    representative pick for a ``collection``-kind one.

    Fencing (build brief, D-COC-1): the served ``text`` is the summary
    BODY IN FULL, regardless of any cited source's license tier -- an L1
    overview is EXTRACTION, not verbatim reproduction, so it is never
    itself truncated the way a fenced CHUNK's ``text`` is
    (:func:`~trialerror.retrieve.fence.fence_chunk_text`). ``fenced: true``
    still marks "at least one cited source is commercial_restricted"
    (design Section 7's own ``SearchResponse`` comment: "true => text is
    extraction/summary" -- exactly what an L1 overview always is), and
    ``citation.quote`` -- the grounding excerpt DERIVED from the body,
    never the body itself -- is capped through the SAME
    :func:`~trialerror.retrieve.fence.citation_quote` function every other
    tier's quote field routes through, so a fenced summary's citation
    quote structurally cannot carry a verbatim run over the D-COC-1
    20-word cap. (:func:`trialerror.summarize.api.store_summary` additionally
    refuses to PERSIST a fenced summary whose body embeds an over-length
    verbatim quote in the first place -- this is the belt to that
    braces.)
    """
    source_doc_ids = json.loads(row["source_doc_ids"])
    cited_sources: list[dict[str, Any]] = []
    for doc_id in source_doc_ids:
        doc = store_get(store, "document", pk_column="doc_id", pk_value=doc_id)
        source = store_get(store, "source", pk_column="source_id", pk_value=doc["source_id"]) if doc else None
        cited_sources.append(
            {
                "doc_id": doc_id,
                "source_id": source["source_id"] if source else None,
                "title": source["title"] if source else None,
                "license_tier": source_license_tier(source),
            }
        )
    if not cited_sources:
        return None

    would_fence = any(is_fenced_license(c["license_tier"]) for c in cited_sources)
    fenced = would_fence and not unfenced
    body = row["body"]
    primary = cited_sources[0]

    return {
        "rank": rank,
        "score": score,
        "fusion": {"summary": rank},
        "kind": "summary",
        "summary_id": row["summary_id"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"],
        "chunk_id": None,
        "doc_id": primary["doc_id"],
        "source_id": primary["source_id"],
        "text": untrusted_wrap(body),
        "fenced": fenced,
        "citation": {
            "source_id": primary["source_id"],
            "title": primary["title"],
            "license_tier": primary["license_tier"],
            "anchor": None,
            "quote": citation_quote(body, fenced=fenced),
        },
        "cited_sources": cited_sources,
    }


def _search_summary_tier(
    store: Store,
    *,
    query: str,
    k: int,
    candidate_chunk_ids: list[str] | None,
    unfenced: bool,
    launch_id: str | None,
    t0: float,
) -> dict[str, Any]:
    """``mode="summary"``'s own search path (called from :func:`search`,
    already past the mode/requested-tiers gate). Ranks every
    ``status='current'`` ``knowledge.summary`` row by a simple
    case-insensitive term-occurrence count over its ``body`` (a coarse
    index over overview text, design Section 7: "L1 per-document overviews
    as a coarse index" -- not the fts5/vector machinery the chunk tiers
    use, since a summary body is a handful of paragraphs, not a corpus to
    prefilter). A blank ``query`` returns every eligible summary, newest
    first (a "browse the overviews" mode, matching the coarse-index
    framing).

    ``candidate_chunk_ids`` is :func:`search`'s already-computed
    ``SearchRequest.filters`` translation (``source_ids``/``kind``/
    ``license_tier``/``year``, resolved to a chunk_id allowlist) -- reused
    here rather than re-querying, translated to the set of ``doc_id``s
    those chunks belong to; a summary is eligible only when at least one
    of its ``source_doc_ids`` falls in that set (``None`` means "no
    restriction", matching :func:`_filtered_chunk_ids`'s own contract).
    """
    rows = [dict(r) for r in store.knowledge.execute("SELECT * FROM summary WHERE status = 'current'").fetchall()]

    allowed_doc_ids: set[str] | None = None
    if candidate_chunk_ids is not None:
        if not candidate_chunk_ids:
            allowed_doc_ids = set()
        else:
            ph = ",".join("?" for _ in candidate_chunk_ids)
            allowed_doc_ids = {
                r["doc_id"]
                for r in store.knowledge.execute(f"SELECT DISTINCT doc_id FROM chunk WHERE chunk_id IN ({ph})", candidate_chunk_ids)
            }

    query_terms = [t for t in query.lower().split() if t]

    def _term_score(body: str) -> int:
        text = body.lower()
        return sum(text.count(term) for term in query_terms)

    scored: list[tuple[dict[str, Any], int]] = []
    for row in rows:
        source_doc_ids = json.loads(row["source_doc_ids"])
        if allowed_doc_ids is not None and not (set(source_doc_ids) & allowed_doc_ids):
            continue
        score = _term_score(row["body"]) if query_terms else 0
        if query_terms and score == 0:
            continue
        scored.append((row, score))

    # stable two-pass sort: recency first (secondary key), then score
    # (primary key) -- Python's sort stability makes this a clean multi-key
    # sort without a composite key over a string timestamp.
    scored.sort(key=lambda pair: pair[0]["created_ts"], reverse=True)
    scored.sort(key=lambda pair: pair[1], reverse=True)
    top = scored[: max(k, 0)]

    bypassed_summary_ids: list[str] = []
    bypassed_source_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    for i, (row, score) in enumerate(top):
        built = _build_summary_result_row(store, row, rank=i + 1, score=float(score), unfenced=unfenced)
        if built is None:
            continue
        if unfenced and any(is_fenced_license(c["license_tier"]) for c in built["cited_sources"]):
            bypassed_summary_ids.append(row["summary_id"])
            bypassed_source_ids.update(c["source_id"] for c in built["cited_sources"] if c["source_id"])
        results.append(built)

    if bypassed_summary_ids:
        append_event(
            store,
            event_type="retrieval_unfenced_bypass",
            payload={
                "summary_ids": bypassed_summary_ids,
                "source_ids": sorted(bypassed_source_ids),
                "surface": "cli:query.search --unfenced (summary tier)",
            },
            launch_id=launch_id,
        )

    return {
        "ok": True,
        "query_id": new_id("QRY"),
        "tiers_used": ["summary"],
        "results": results,
        "stats": {
            "fts_candidates": 0,
            "vector_scored": 0,
            "summary_candidates": len(rows),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        },
    }


# ---------------------------------------------------------------------------
# get_chunk
# ---------------------------------------------------------------------------


def get_chunk(store: Store, chunk_id: str) -> dict[str, Any]:
    """Design Section 5.1: "chunk text + element/page context + anchors"."""
    chunk = store_get(store, "chunk", pk_column="chunk_id", pk_value=chunk_id)
    if chunk is None:
        raise ChunkNotFoundError(f"no such chunk: {chunk_id!r}")
    doc = store_get(store, "document", pk_column="doc_id", pk_value=chunk["doc_id"])
    source = store_get(store, "source", pk_column="source_id", pk_value=doc["source_id"]) if doc else None
    fenced = is_fenced_license(source.get("license_tier")) if source else False

    if fenced:
        text = fence_chunk_text(
            chunk_text=chunk["text"],
            source_title=source["title"],
            page_start=chunk.get("page_start"),
            page_end=chunk.get("page_end"),
            seq=chunk["seq"],
            token_count=chunk["token_count"],
        )
    else:
        text = chunk["text"]

    anchors = [
        {
            "anchor_id": r["anchor_id"],
            "page": r["page_number"],
            "char_start": r["char_start"],
            "char_end": r["char_end"],
            "quote": citation_quote(r["quote_text"], fenced=fenced),
        }
        for r in (dict(row) for row in store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ? ORDER BY created_ts ASC", (chunk_id,)))
    ]

    element_first = store_get(store, "element", pk_column="element_id", pk_value=chunk["element_first"])
    element_last = store_get(store, "element", pk_column="element_id", pk_value=chunk["element_last"])

    return {
        "chunk_id": chunk_id,
        "doc_id": chunk["doc_id"],
        "seq": chunk["seq"],
        "text": untrusted_wrap(text),
        "fenced": fenced,
        "token_count": chunk["token_count"],
        "page_start": chunk.get("page_start"),
        "page_end": chunk.get("page_end"),
        "element_context": {
            "first": {"element_id": element_first["element_id"], "type": element_first["type"], "page_number": element_first.get("page_number")} if element_first else None,
            "last": {"element_id": element_last["element_id"], "type": element_last["type"], "page_number": element_last.get("page_number")} if element_last else None,
        },
        "source": {"source_id": source["source_id"], "title": source["title"], "license_tier": source["license_tier"]} if source else None,
        "anchors": anchors,
    }


# ---------------------------------------------------------------------------
# get_source / get_document_outline
# ---------------------------------------------------------------------------


def get_source(store: Store, source_id: str) -> dict[str, Any]:
    source = store_get(store, "source", pk_column="source_id", pk_value=source_id)
    if source is None:
        raise SourceNotFoundError(f"no such source: {source_id!r}")
    documents = [dict(r) for r in store.knowledge.execute("SELECT * FROM document WHERE source_id = ? ORDER BY rel_path", (source_id,))]
    return {"source": source, "documents": documents}


def get_document_outline(store: Store, doc_id: str) -> dict[str, Any]:
    doc = store_get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise DocumentNotFoundError(f"no such document: {doc_id!r}")
    source = store_get(store, "source", pk_column="source_id", pk_value=doc["source_id"])
    fenced = is_fenced_license(source.get("license_tier")) if source else False

    outline: list[dict[str, Any]] = []
    for r in store.knowledge.execute("SELECT * FROM element WHERE doc_id = ? ORDER BY seq", (doc_id,)):
        d = dict(r)
        if d["type"] not in _OUTLINE_ELEMENT_TYPES:
            continue
        preview = excerpt_words(d.get("text"), 20) if fenced else (d.get("text") or "")
        outline.append(
            {
                "element_id": d["element_id"],
                "type": d["type"],
                "seq": d["seq"],
                "page_number": d.get("page_number"),
                "parent_element": d.get("parent_element"),
                "category_depth": d.get("category_depth"),
                "text_preview": preview,
            }
        )
    return {"doc_id": doc_id, "fenced": fenced, "outline": outline}


# ---------------------------------------------------------------------------
# resolve_quote
# ---------------------------------------------------------------------------


def resolve_quote(store: Store, quote: str, *, source_id: str | None = None, doc_id: str | None = None) -> dict[str, Any]:
    """Design Section 5.1: "quote text -> matching anchors (doc, page,
    span) or NOT_FOUND". Fast path: exact ``quote_sha256`` match (a caller
    supplying the FULL text an anchor was hashed from -- "known-quote
    query returns its anchor page/span", the M8 acceptance wording). Falls
    back to a substring scan over ``quote_anchor.quote_text`` (a caller
    supplying a partial quote) when the exact hash misses."""
    from trialerror.ingest.anchors import sha256_hex

    qsha = sha256_hex(quote)
    clauses = ["quote_sha256 = ?"]
    params: list[Any] = [qsha]
    if doc_id:
        clauses.append("doc_id = ?")
        params.append(doc_id)
    rows = [dict(r) for r in store.knowledge.execute(f"SELECT * FROM quote_anchor WHERE {' AND '.join(clauses)}", params)]
    match_type = "exact"

    if not rows:
        escaped = quote.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_clauses = ["quote_text LIKE ? ESCAPE '\\'"]
        like_params: list[Any] = [f"%{escaped}%"]
        if doc_id:
            like_clauses.append("doc_id = ?")
            like_params.append(doc_id)
        rows = [
            dict(r)
            for r in store.knowledge.execute(
                f"SELECT * FROM quote_anchor WHERE {' AND '.join(like_clauses)} ORDER BY created_ts ASC LIMIT 20", like_params
            )
        ]
        match_type = "substring"

    if source_id and rows:
        doc_ids = {r["doc_id"] for r in rows}
        ph = ",".join("?" for _ in doc_ids)
        docs = {r["doc_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM document WHERE doc_id IN ({ph})", list(doc_ids))}
        rows = [r for r in rows if docs.get(r["doc_id"], {}).get("source_id") == source_id]

    if not rows:
        return {"found": False, "match_type": None, "matches": []}

    matches: list[dict[str, Any]] = []
    for r in rows:
        doc = store_get(store, "document", pk_column="doc_id", pk_value=r["doc_id"])
        source = store_get(store, "source", pk_column="source_id", pk_value=doc["source_id"]) if doc else None
        fenced = is_fenced_license(source.get("license_tier")) if source else False
        matches.append(
            {
                "anchor_id": r["anchor_id"],
                "doc_id": r["doc_id"],
                "chunk_id": r.get("chunk_id"),
                "source_id": source["source_id"] if source else None,
                "page": r.get("page_number"),
                "char_start": r["char_start"],
                "char_end": r["char_end"],
                "fenced": fenced,
                "quote": citation_quote(r.get("quote_text"), fenced=fenced),
            }
        )
    return {"found": True, "match_type": match_type, "matches": matches}


# ---------------------------------------------------------------------------
# similar
# ---------------------------------------------------------------------------


def similar(store: Store, ref_id: str, *, kind: str = "chunk", k: int = 10) -> dict[str, Any]:
    """Design Section 5.1: "nearest chunks/claims to a given id"."""
    if kind == "claim":
        return {"ok": True, "results": [], "note": "claim embeddings are v1 scope (design Section 11 extract-stage minimal); no claim vectors exist in v0"}
    if kind != "chunk":
        raise InvalidSearchModeError(f"similar: unsupported kind {kind!r} (choices: 'chunk', 'claim')")

    chunk = store_get(store, "chunk", pk_column="chunk_id", pk_value=ref_id)
    if chunk is None:
        raise ChunkNotFoundError(f"no such chunk: {ref_id!r}")

    model_key, _backend = _resolve_embed_backend(store)
    emb_row = store.knowledge.execute(
        "SELECT vector FROM emb WHERE chunk_sha256 = ? AND model_key = ?", (chunk["sha256"], model_key)
    ).fetchone()
    if emb_row is None:
        return {"ok": True, "results": [], "note": f"chunk {ref_id!r} has no embedding for model_key={model_key!r} yet"}
    if not vec_table_exists(store, model_key):
        return {"ok": True, "results": [], "note": "no vector index yet for this model_key"}

    query_vector = deserialize_vector_fallback(emb_row["vector"])
    # B.4b (see trialerror.retrieve.vecsearch's module docstring): similar()
    # always ranks against the WHOLE corpus -- the other genuinely
    # UNBOUNDED path bake-off B.4b names. Same opt-in gate as
    # search(mode="vector")'s own B.4b branch: only when this model_key's
    # table is a real vec0 table; the fallback-backend path below is
    # otherwise byte-identical to this function's pre-B.4b behavior.
    if vec_backend_for(store, model_key) == VecBackend.SQLITE_VEC:
        ranked = fetch_native_knn(store, model_key, query_vector, k=k, exclude_chunk_id=ref_id)
    else:
        universe = [cid for cid in _all_chunk_ids(store) if cid != ref_id]
        vectors = fetch_vectors(store, model_key, universe)
        ranked = rank_by_query_vector(query_vector, vectors)[: max(k, 0)]
    ctx = _fetch_chunk_context(store, [cid for cid, _ in ranked])

    results = []
    for i, (cid, score) in enumerate(ranked):
        row = _build_result_row(cid, rank=i + 1, score=score, fusion={"vector": i + 1}, ctx=ctx)
        if row is not None:
            results.append(row)
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# graph_neighbors
# ---------------------------------------------------------------------------


def _fence_relation_edges(store: Store, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FX-4 (IMPL_REVIEW_VERDICT.md Tier 1 / IMPL_REVIEW_B_bypass.md EP-5
    Bypass C): route ``relation.fact_text`` through the SAME fence +
    untrusted-wrap every other retrieval path in this module uses, before
    it ever leaves the engine. No v0 writer populates ``relation`` yet (the
    table is empty -- design Section 11: KG extraction is v1), but the row
    shape is real, ``fact_text`` is ``NOT NULL`` and evidence-anchored, and
    can carry a verbatim run pulled from a ``commercial_restricted``
    source the moment a v1 KG-writer starts populating it -- this closes
    that hole before it can ever be silently live.

    License provenance is resolved the same way :func:`_fetch_chunk_context`
    resolves it for chunks: ``relation.evidence_anchor`` ->
    ``quote_anchor.doc_id`` -> ``document.source_id`` -> ``source.
    license_tier``. A relation whose anchor/document/source doesn't resolve
    (shouldn't happen -- ``evidence_anchor`` is a NOT NULL FK -- but the
    engine never trusts a foreign row to exist) is treated as unfenced
    rather than raising, matching this module's existing "missing context
    drops fencing, never crashes the caller" posture elsewhere.

    Uses :func:`trialerror.retrieve.fence.citation_quote` (not
    :func:`trialerror.retrieve.fence.fence_chunk_text`, which is shaped for a
    ``chunk`` row's page/seq/token_count fields a ``relation`` row doesn't
    have) -- the same excerpt function :func:`get_chunk`'s per-anchor
    ``quote`` field already routes through, so a fenced fact still gets the
    D-COC-1 <=20-word cap and an open fact still gets the Section 7
    <=300-char grounding cap."""
    anchor_ids = {e["evidence_anchor"] for e in edges if e.get("evidence_anchor")}
    anchors: dict[str, dict[str, Any]] = {}
    if anchor_ids:
        aph = ",".join("?" for _ in anchor_ids)
        anchors = {r["anchor_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM quote_anchor WHERE anchor_id IN ({aph})", list(anchor_ids))}
    doc_ids = {a["doc_id"] for a in anchors.values() if a.get("doc_id")}
    documents: dict[str, dict[str, Any]] = {}
    if doc_ids:
        dph = ",".join("?" for _ in doc_ids)
        documents = {r["doc_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM document WHERE doc_id IN ({dph})", list(doc_ids))}
    source_ids = {d["source_id"] for d in documents.values()}
    sources: dict[str, dict[str, Any]] = {}
    if source_ids:
        sph = ",".join("?" for _ in source_ids)
        sources = {r["source_id"]: dict(r) for r in store.knowledge.execute(f"SELECT * FROM source WHERE source_id IN ({sph})", list(source_ids))}

    fenced_edges: list[dict[str, Any]] = []
    for e in edges:
        anchor = anchors.get(e.get("evidence_anchor"))
        doc = documents.get(anchor["doc_id"]) if anchor else None
        source = sources.get(doc["source_id"]) if doc else None
        fenced = is_fenced_license(source_license_tier(source))
        display_text = citation_quote(e.get("fact_text"), fenced=fenced)
        fenced_edges.append({**e, "fact_text": untrusted_wrap(display_text), "fenced": fenced})
    return fenced_edges


def _wrap_entity_summary(entity: dict[str, Any]) -> dict[str, Any]:
    """FX-4: ``entity.summary`` is free-text derived from the corpus (a
    future v1 KG-writer's synthesis, per design Section 11) but -- unlike
    ``chunk``/``relation`` -- the v0 ``entity`` schema carries no
    evidence-anchor column to resolve a ``source.license_tier`` from, so it
    cannot be LICENSE-fenced the way chunk text or ``relation.fact_text``
    can (see :func:`_fence_relation_edges`). It still gets the same
    untrusted-wrap every other served free-text BODY field in this module
    gets (``text``, ``citation.quote``, now ``fact_text``), so it can never
    be mistaken for trusted/instruction-bearing content by whatever reads
    the tool result. ``entity.name`` is deliberately left unwrapped -- a
    short structured label, the same treatment ``source["title"]`` gets
    elsewhere in this module, not a free-text body."""
    if not entity.get("summary"):
        return entity
    return {**entity, "summary": untrusted_wrap(entity["summary"])}


def graph_neighbors(
    store: Store, entity_id: str, *, as_of: str | None = None, as_of_tx: str | None = None, k: int = 50
) -> dict[str, Any]:
    """Design Section 5.1: "entity/claim edges; ``as_of`` = valid-time
    (event) axis, optional ``as_of_tx`` = transaction axis". No v0 writer
    populates ``entity``/``relation`` yet (design Section 11: full KG
    extraction is v1) -- this is schema-now query support, correct against
    the bi-temporal ``relation`` shape (Graphiti 4-timestamp pattern,
    design Section 4.1) whenever a future writer (or a test fixture) does
    populate it.

    Default (no ``as_of``/``as_of_tx``): the LIVE view -- ``expired_at IS
    NULL`` (not transactionally superseded) and ``invalid_at IS NULL`` (not
    event-time invalidated).
    """
    entity = store_get(store, "entity", pk_column="entity_id", pk_value=entity_id)
    if entity is None:
        raise EntityNotFoundError(f"no such entity: {entity_id!r}")

    clauses, params = _relation_bitemporal_clause(as_of=as_of, as_of_tx=as_of_tx)
    sql = f"SELECT * FROM relation WHERE (src_entity = ? OR dst_entity = ?) AND {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
    edges = [dict(r) for r in store.knowledge.execute(sql, [entity_id, entity_id, *params, k])]
    edges = _fence_relation_edges(store, edges)
    entity = _wrap_entity_summary(entity)
    return {"entity": entity, "edges": edges, "count": len(edges), "as_of": as_of, "as_of_tx": as_of_tx}


# ---------------------------------------------------------------------------
# k_hop_neighbors / path_between -- design Section 11 deliverable 2's "new
# path/subgraph query surface", both carrying the MANDATED caps (module
# constants above) per the spike's own finding. Both use level-by-level
# Python BFS -- issuing one LIMIT-guarded SQL query per hop via
# :func:`_fetch_relation_edges_touching` -- rather than a single recursive
# query, which is the structural fix for the spike's unbounded-worst-case
# finding: no single query here ever enumerates more than
# ``max_hops * hop_limit`` edges, regardless of corpus size.
# ---------------------------------------------------------------------------


def k_hop_neighbors(
    store: Store,
    entity_id: str,
    *,
    max_hops: int | None = None,
    hop_limit: int | None = None,
    as_of: str | None = None,
    as_of_tx: str | None = None,
) -> dict[str, Any]:
    """The bounded "subgraph" query surface: extends :func:`graph_neighbors`'s
    1-hop view to ``max_hops`` hops (default :data:`DEFAULT_MAX_HOPS` = 2;
    a caller may raise it up to this program's configured ceiling --
    :class:`ValueError` past that, see :func:`_resolve_max_hops`).

    Bi-temporal semantics (``as_of``/``as_of_tx``) and edge fencing/entity-
    summary wrapping are IDENTICAL to :func:`graph_neighbors` (shared
    :func:`_relation_bitemporal_clause`/:func:`_fence_relation_edges`/
    :func:`_wrap_entity_summary`). ``truncated: true`` in the response
    means at least one hop hit ``hop_limit`` and may have missed edges past
    it -- reported rather than silently absorbed, matching this module's
    "missing context drops fencing, never crashes/lies to the caller"
    posture elsewhere (see :func:`_fence_relation_edges`'s own docstring).
    """
    entity = store_get(store, "entity", pk_column="entity_id", pk_value=entity_id)
    if entity is None:
        raise EntityNotFoundError(f"no such entity: {entity_id!r}")
    hops, limit = _resolve_max_hops(store, max_hops, hop_limit)

    visited_nodes: set[str] = {entity_id}
    frontier: set[str] = {entity_id}
    edges_by_id: dict[str, dict[str, Any]] = {}
    truncated = False
    hops_reached = 0

    for hop in range(1, hops + 1):
        if not frontier:
            break
        edges = _fetch_relation_edges_touching(store, frontier, as_of=as_of, as_of_tx=as_of_tx, limit=limit)
        if len(edges) >= limit:
            truncated = True
        hops_reached = hop
        next_frontier: set[str] = set()
        for e in edges:
            edges_by_id.setdefault(e["rel_id"], e)
            for node in (e["src_entity"], e["dst_entity"]):
                if node not in visited_nodes:
                    next_frontier.add(node)
        visited_nodes |= next_frontier
        frontier = next_frontier

    fenced_edges = _fence_relation_edges(store, list(edges_by_id.values()))
    return {
        "entity": _wrap_entity_summary(entity),
        "nodes": sorted(visited_nodes),
        "node_count": len(visited_nodes),
        "edges": fenced_edges,
        "count": len(fenced_edges),
        "max_hops": hops,
        "hops_reached": hops_reached,
        "hop_limit": limit,
        "truncated": truncated,
        "as_of": as_of,
        "as_of_tx": as_of_tx,
    }


def path_between(
    store: Store,
    src_entity_id: str,
    dst_entity_id: str,
    *,
    max_hops: int | None = None,
    hop_limit: int | None = None,
    as_of: str | None = None,
    as_of_tx: str | None = None,
) -> dict[str, Any]:
    """The bounded "path" query surface: shortest-path search between two
    entities, same level-by-level LIMIT-guarded BFS as
    :func:`k_hop_neighbors` (see its docstring for the bounded-cost
    argument), with an EARLY EXIT the instant ``dst_entity_id`` enters the
    frontier -- the direct fix for the spike's OTHER unbounded query class
    (``spikes/kuzu/SPIKE_REPORT.md``'s ``path_between``: 100% abort rate at
    10x scale/depth<=3 on its own wall-clock cutoff): a single recursive-CTE
    enumerates every path up to the depth bound before returning
    shortest-first, whereas this function stops exploring the moment a
    shortest path is found.

    Returns ``{"found": True, "nodes": [...], "edges": [...], "hops": N,
    "truncated": bool, ...}`` (``edges`` fenced/wrapped exactly like
    :func:`k_hop_neighbors`, ordered src->dst along the path) when a path
    exists within ``max_hops``, else ``{"found": False, "hops_searched":
    N, "truncated": bool, ...}`` -- never raises for "no path found", only
    for a missing ``src``/``dst`` entity or an out-of-range ``max_hops``
    (see :func:`_resolve_max_hops`).
    """
    src = store_get(store, "entity", pk_column="entity_id", pk_value=src_entity_id)
    if src is None:
        raise EntityNotFoundError(f"no such entity: {src_entity_id!r}")
    dst = store_get(store, "entity", pk_column="entity_id", pk_value=dst_entity_id)
    if dst is None:
        raise EntityNotFoundError(f"no such entity: {dst_entity_id!r}")
    hops, limit = _resolve_max_hops(store, max_hops, hop_limit)

    if src_entity_id == dst_entity_id:
        return {
            "found": True, "nodes": [src_entity_id], "edges": [], "hops": 0, "truncated": False,
            "as_of": as_of, "as_of_tx": as_of_tx,
        }

    parent: dict[str, tuple[str, dict[str, Any]]] = {}
    visited: set[str] = {src_entity_id}
    frontier: set[str] = {src_entity_id}
    truncated = False
    hop = 0

    for hop in range(1, hops + 1):
        if not frontier:
            break
        edges = _fetch_relation_edges_touching(store, frontier, as_of=as_of, as_of_tx=as_of_tx, limit=limit)
        if len(edges) >= limit:
            truncated = True
        next_frontier: set[str] = set()
        reached = False
        for e in edges:
            for a, b in ((e["src_entity"], e["dst_entity"]), (e["dst_entity"], e["src_entity"])):
                if a in frontier and b not in visited:
                    visited.add(b)
                    parent[b] = (a, e)
                    next_frontier.add(b)
                    if b == dst_entity_id:
                        reached = True
        if reached:
            node_path = [dst_entity_id]
            edge_path: list[dict[str, Any]] = []
            cur = dst_entity_id
            while cur != src_entity_id:
                prev, edge = parent[cur]
                edge_path.append(edge)
                node_path.append(prev)
                cur = prev
            node_path.reverse()
            edge_path.reverse()
            return {
                "found": True, "nodes": node_path, "edges": _fence_relation_edges(store, edge_path),
                "hops": hop, "truncated": truncated, "as_of": as_of, "as_of_tx": as_of_tx,
            }
        frontier = next_frontier

    return {
        "found": False, "nodes": [], "edges": [], "hops_searched": min(hop, hops), "truncated": truncated,
        "as_of": as_of, "as_of_tx": as_of_tx,
    }


# ---------------------------------------------------------------------------
# graph_tier_candidates -- design Section 7 pipeline step 4 / Section 11
# deliverable 2: "graph tier (entity/claim neighbors as additional
# candidates)", wired into search() below.
# ---------------------------------------------------------------------------


def graph_tier_candidates(
    store: Store, seed_chunk_ids: Sequence[str], *, hops: int = DEFAULT_GRAPH_TIER_HOPS, hop_limit: int | None = None
) -> list[str]:
    """Given an already-ranked set of seed chunk ids (the fts/vector tiers'
    own top results inside :func:`search`), find the entities anchored to
    those chunks, expand ONE bounded hop of their graph neighbors (same
    LIMIT-guarded :func:`_fetch_relation_edges_touching` machinery as
    :func:`k_hop_neighbors`), and return the chunk ids those neighbor
    relations' evidence anchors point back to -- additional retrieval
    candidates :func:`search` fuses in as the ``"graph"`` tier.

    Deliberately fixed at :data:`DEFAULT_GRAPH_TIER_HOPS` (1) rather than
    exposing ``max_hops`` here: this is a recall-widening step over an
    already-relevant seed set, not an open-ended traversal (that's what
    :func:`k_hop_neighbors` is for). Seeds are capped at
    :data:`DEFAULT_GRAPH_TIER_SEED_LIMIT` so a large fused fts/vector
    result can't blow up the number of anchor/entity lookups this issues.
    Returns ``[]`` (never raises) whenever no seeds, no anchors, no
    entities, or no neighbor edges are found -- the ordinary case for any
    corpus whose KG hasn't been populated yet (design Section 11: "v1 once
    KG is populated").
    """
    seeds = list(dict.fromkeys(seed_chunk_ids))[:DEFAULT_GRAPH_TIER_SEED_LIMIT]
    if not seeds:
        return []
    sph = ",".join("?" for _ in seeds)
    anchor_ids = [r["anchor_id"] for r in store.knowledge.execute(f"SELECT anchor_id FROM quote_anchor WHERE chunk_id IN ({sph})", seeds)]
    if not anchor_ids:
        return []

    aph = ",".join("?" for _ in anchor_ids)
    seed_entities: set[str] = set()
    for r in store.knowledge.execute(
        f"SELECT DISTINCT src_entity, dst_entity FROM relation WHERE evidence_anchor IN ({aph}) AND expired_at IS NULL",
        anchor_ids,
    ):
        seed_entities.add(r["src_entity"])
        seed_entities.add(r["dst_entity"])
    if not seed_entities:
        return []

    _, default_limit = _graph_cap_config(store)
    limit = default_limit if hop_limit is None else max(1, min(int(hop_limit), ABSOLUTE_HOP_LIMIT_CEILING))
    edges: list[dict[str, Any]] = _fetch_relation_edges_touching(store, seed_entities, as_of=None, as_of_tx=None, limit=limit)
    for _extra_hop in range(2, max(hops, 1) + 1):  # DEFAULT_GRAPH_TIER_HOPS is 1; this only fires for an explicit override
        frontier = {n for e in edges for n in (e["src_entity"], e["dst_entity"])} - seed_entities
        if not frontier:
            break
        more = _fetch_relation_edges_touching(store, frontier, as_of=None, as_of_tx=None, limit=limit)
        seen_ids = {e["rel_id"] for e in edges}
        edges.extend(e for e in more if e["rel_id"] not in seen_ids)

    edge_anchor_ids = [e["evidence_anchor"] for e in edges if e.get("evidence_anchor")]
    if not edge_anchor_ids:
        return []
    eph = ",".join("?" for _ in edge_anchor_ids)
    chunk_rows = store.knowledge.execute(
        f"SELECT DISTINCT chunk_id FROM quote_anchor WHERE anchor_id IN ({eph}) AND chunk_id IS NOT NULL",
        edge_anchor_ids,
    )
    return [r["chunk_id"] for r in chunk_rows]


# ---------------------------------------------------------------------------
# corpus_stats
# ---------------------------------------------------------------------------


def corpus_stats(store: Store) -> dict[str, Any]:
    """Design Section 5.1: "sources/docs/chunks/index freshness/doctor
    summary"."""
    # CRITICAL RULE (M7's own live bug, carried forward): sqlite-vec's
    # loadable extension is per-CONNECTION -- this function may run on a
    # freshly-opened Store (e.g. one call per MCP tools/call) that has
    # never loaded it, and every ``vec_chunks__*`` table below is queried
    # by name.
    try_load_sqlite_vec(store.knowledge)

    def _count(sql: str, params: Sequence[Any] = ()) -> int:
        return int(store.knowledge.execute(sql, params).fetchone()[0])

    sources = _count("SELECT COUNT(*) FROM source")
    documents = _count("SELECT COUNT(*) FROM document")
    chunks = _count("SELECT COUNT(*) FROM chunk")
    chunk_fts_rows = _count("SELECT COUNT(*) FROM chunk_fts")
    anchors = _count("SELECT COUNT(*) FROM quote_anchor")

    emb_by_model = {
        r["model_key"]: r["n"]
        for r in store.knowledge.execute("SELECT model_key, COUNT(*) AS n FROM emb GROUP BY model_key")
    }
    registry_rows = [dict(r) for r in store.knowledge.execute("SELECT * FROM vec_index_registry")]

    vec_by_model: dict[str, int] = {}
    for reg in registry_rows:
        table = reg["table_name"]
        vec_by_model[reg["model_key"]] = _count(f"SELECT COUNT(*) FROM {table}")

    return {
        "sources": sources,
        "documents": documents,
        "chunks": chunks,
        "chunk_fts_rows": chunk_fts_rows,
        "chunks_missing_fts": max(chunks - chunk_fts_rows, 0),
        "quote_anchors": anchors,
        "embeddings_by_model_key": emb_by_model,
        "vector_index": registry_rows,
        "vec_rows_by_model_key": vec_by_model,
        "chunks_missing_vec_by_model_key": {mk: max(chunks - n, 0) for mk, n in vec_by_model.items()},
    }


# ---------------------------------------------------------------------------
# list_requests
# ---------------------------------------------------------------------------

#: mirrors ``source.request_state``'s DDL CHECK domain (design Section
#: 4.1) -- duplicated here (rather than importing ``trialerror.ingest.requests``)
#: to keep this read-only query self-contained; both are pinned to the
#: same schema constraint so they cannot silently drift.
_REQUEST_STATES: tuple[str, ...] = ("wanted", "requested", "delivered", "verifying", "archived", "indexed", "rejected", "failed")


def list_requests(store: Store, *, state: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Design Section 5.1: "acquisition queue by state"."""
    if state is not None:
        rows = [
            dict(r)
            for r in store.knowledge.execute(
                "SELECT * FROM source WHERE request_state = ? ORDER BY registered_ts LIMIT ?", (state, limit)
            )
        ]
    else:
        rows = [dict(r) for r in store.knowledge.execute("SELECT * FROM source ORDER BY request_state, registered_ts LIMIT ?", (limit,))]

    counts_by_state = {s: 0 for s in _REQUEST_STATES}
    for r in store.knowledge.execute("SELECT request_state, COUNT(*) AS n FROM source GROUP BY request_state"):
        counts_by_state[r["request_state"]] = r["n"]

    return {"requests": rows, "count": len(rows), "counts_by_state": counts_by_state}
