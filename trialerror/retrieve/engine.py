"""The hybrid retrieval engine. Design Section 7 (Retrieval API contract) +
Section 12 (M8 row): "hybrid engine (fts->vec->RRF), citation bundle,
untrusted-wrap, serving-path license fence (Section 7), resolve_quote."

This module is the ONE place ``SearchResponse``-shaped results get built.
``trialerror/cli/query.py`` (the ``query`` CLI group) and
``trialerror/mcp/knowledge.py`` (the ``trialerror-knowledge`` MCP server) are both
thin wrappers calling straight into these functions -- neither re-derives
citation blocks, fencing, or the untrusted-wrap.

**Pipeline (Section 7, "Q5 applied"):**

1. Lexical/BM25 prefilter to <=500 candidates. The backend is pluggable
   (:mod:`trialerror.retrieve.lexical`): tantivy
   (:mod:`trialerror.retrieve.tantivysearch`) by default since C-0080, SQLite
   FTS5 (:mod:`trialerror.retrieve.ftssearch`) as the always-available
   fallback. The tier is still called ``"fts"`` in ``tiers_used`` (that is
   design Section 7's own tier name and a caller-visible contract);
   ``stats.fulltext_backend`` reports which engine served it.
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
import sys
import time
from typing import Any, Mapping, Sequence

from trialerror.events.api import append_event
from trialerror.ingest.backends import (
    CpuQueryEmbedBackend,
    EmbedBackend,
    QUERY_EMBED_TABLE,
    embed_backend_runnable,
    load_embed_backend,
    load_query_embed_backend,
)
from trialerror.retrieve.errors import (
    QUERY_EMBED_UNRUNNABLE_CODE,
    ChunkNotFoundError,
    DocumentNotFoundError,
    EntityNotFoundError,
    InvalidSearchModeError,
    QueryEmbedBackendUnrunnableError,
    SourceNotFoundError,
)
from trialerror.retrieve.fence import (
    MAX_FENCED_EXCERPT_WORDS,
    citation_quote,
    excerpt_words,
    fence_chunk_text,
    is_fenced_license,
    source_license_tier,
)
from trialerror.retrieve.fusion import reciprocal_rank_fusion
from trialerror.retrieve import lexical, tantivysearch, vecmatrix
from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT
from trialerror.retrieve.lexical import lexical_search, per_term_candidates
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
    "DEFAULT_EXCLUDED_KINDS",
    "LAUNCH_SLICE_ATTR",
    "launch_slice_doc_ids",
    "search",
    "query_vector_or_reason",
    "query_embed_runnable",
    "query_embed_next_action_argv",
    "query_embed_refusal_message",
    "QUERY_EMBED_DOCTOR_CHECK",
    "QUERY_EMBED_TABLE",
    "QUERY_EMBED_UNRUNNABLE_CODE",
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

#: Source kinds this engine excludes unless a caller names them in
#: ``filters["kind"]`` explicitly.
#:
#: The framework's novelty screen measures each idea against the program's
#: structured inventory (source kind ``inventory``, one chunk per row). A
#: generator that could retrieve those rows would be reading the reference
#: set of the screen that judges it -- the design classes that barrier as
#: *enforced in code*, not as a convention in a prompt, and this constant is
#: where it is enforced. The screen itself asks for the rows by kind, which
#: is exactly the explicit request that lifts the exclusion.
#:
#: **Which surfaces, named rather than implied.** Enforced on every function
#: an agent can reach through ``trialerror/mcp/knowledge.py``:
#: :func:`search` (all tiers, the graph tier included),
#: :func:`similar`, :func:`get_chunk`, :func:`get_source` (its document
#: list), :func:`get_document_outline`, :func:`resolve_quote` and
#: :func:`graph_neighbors` (by each edge's evidence anchor). NOT enforced on
#: :func:`k_hop_neighbors`, :func:`path_between` or :func:`corpus_stats`:
#: the first two are API/CLI-only (no MCP tool wraps them, so no lens holds
#: them) and the third reports counts, never content. A blanket "every
#: surface" would be the claim, not the guarantee -- this list is the
#: guarantee, and adding an MCP tool means adding it here or adding the
#: barrier to it.
#:
#: The same list is what the per-launch slice scope covers
#: (:func:`launch_slice_doc_ids`), for the same reason and through the same
#: predicate (:func:`_scope_clauses`): two barriers that resolved on
#: different sets of surfaces would each be an argument for the other's
#: gaps.
#:
#: Deliberately NOT a config knob: a barrier a program can switch off in its
#: own trialerror.toml is a barrier the audited party controls.
DEFAULT_EXCLUDED_KINDS: tuple[str, ...] = ("inventory",)

#: The ``launch.attrs`` key carrying a lens launch's assigned slice, as
#: document ids. When a retrieval call names a launch that has it, this
#: engine restricts that call to those documents (:func:`launch_slice_doc_ids`).
LAUNCH_SLICE_ATTR = "slice_doc_ids"

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


def _resolve_embed_backend(store: Store, *, side: str = "document") -> tuple[str, EmbedBackend]:
    """Same resolution M7's ``embed``/``index`` handlers use (design
    Section 4.1: model-keyed embedding cache) -- retrieval-time query
    embedding MUST use the same ``model_key`` the corpus was embedded
    under, or ``vec_chunks__<model_key>`` lookups silently miss everything.
    Defaults to the fake backend when unconfigured (same zero-setup
    default M7 ships), which is exactly what makes the M8 15k-chunk
    latency fixture GPU-free (design Section 13 flag F18).

    ``side`` (lane F-1) picks WHICH of a program's two possible backends:

    - ``"document"`` (the default, unchanged) -- ``[ingest.embed]``, the
      backend the corpus was embedded under. This is the side that OWNS the
      ``model_key``, so every ``vec_chunks__<key>``/``emb`` lookup resolves
      it from here.
    - ``"query"`` -- ``[ingest.embed]`` overlaid with its optional
      ``[ingest.embed.query]`` sub-table
      (:func:`trialerror.ingest.backends.load_query_embed_backend`), the backend
      that embeds text no document stage will ever see. Unconfigured, it
      IS the document backend object (``backend = "same"``), so nothing
      changes for a program that never writes the table.

    Both sides always agree on ``model_key``/``dims`` -- a query-side
    configuration that does not is refused at resolution
    (:class:`~trialerror.ingest.backends.QueryEmbedBackendMismatchError`), never
    used -- which is why the returned key is meaningful whichever side was
    asked for.
    """
    if side not in ("document", "query"):
        raise ValueError(f"_resolve_embed_backend: side must be 'document' or 'query', got {side!r}")
    config = _load_program_config(store)
    embed_cfg = config.get("ingest", {}).get("embed", {})
    backend = load_query_embed_backend(embed_cfg) if side == "query" else load_embed_backend(embed_cfg)
    if side == "query" and isinstance(backend, CpuQueryEmbedBackend):
        # An offload program's local CPU query embedder has to PROVE it
        # reproduces the vectors the GPU executor stored, or its query
        # vectors rank the corpus by a number that means nothing
        # (docs/VASTAI_EMBED_DESIGN.md section 6). The check is cached per
        # backend identity, so it costs one subprocess once.
        from trialerror.retrieve.query_embed import ensure_calibrated

        ensure_calibrated(store, backend)
    return backend.model_key, backend


def _resolve_query_embed_backend(store: Store) -> tuple[str, EmbedBackend]:
    """``(model_key, backend)`` for embedding a QUERY string.

    The query side of :func:`_resolve_embed_backend`, under the name the
    rest of the tree already imports it by."""
    return _resolve_embed_backend(store, side="query")


#: The doctor check that answers "can this process embed a query?" -- named
#: in every degrade warning and every refusal this module produces, so the
#: sentence an operator reads always ends somewhere they can go.
QUERY_EMBED_DOCTOR_CHECK = "query_embed_backend_runnable"


def query_embed_next_action_argv(program_root: Any | None = None) -> list[str]:
    """The literal shell command a surface should offer after a query-side
    embedding failure (the envelope's ``nextActions`` shape)."""
    argv = ["trialerror", "doctor", "--only", QUERY_EMBED_DOCTOR_CHECK]
    if program_root is not None:
        argv += ["--program-root", str(program_root)]
    return argv


def query_embed_runnable(store: Store) -> tuple[bool, str]:
    """Whether this process could embed a query AT ALL -- resolution plus
    :meth:`~trialerror.ingest.backends.EmbedBackend.runnable`, no embedding
    performed.

    The cheap gate for a caller that must REFUSE rather than degrade
    (:mod:`trialerror.verify.hypothesis`, :mod:`trialerror.lens.novelty`): it
    answers before any prereg is committed, any job is enqueued or any
    dossier is written, and it costs no model call on the happy path."""
    try:
        _model_key, backend = _resolve_embed_backend(store, side="query")
    except Exception as exc:  # noqa: BLE001 - a bad config table is a reason, not a traceback
        return False, f"the query-side embed backend could not be resolved ({type(exc).__name__}: {exc})"
    return embed_backend_runnable(backend)


def query_vector_or_reason(store: Store, text: str, *, kind: str = "query") -> tuple[list[float] | None, str]:
    """One vector for ``text`` from the QUERY-side backend, or ``None`` and
    the reason it could not be produced.

    The whole point is that "this process cannot embed" is a STATE, not an
    exception: on a two-machine program it is the normal state, all day,
    and every retrieval surface has to be able to say so in an envelope.
    So this wraps the three things that can go wrong -- resolving the
    backend (a malformed or mismatched ``[ingest.embed.query]`` table),
    asking whether it can run here
    (:func:`~trialerror.ingest.backends.embed_backend_runnable`), and the embed
    call itself -- and returns prose for all three.

    Deliberately broad on the embed call: a driver that crashed, a model
    file that vanished mid-session, a GGUF loader that dies in ``ctypes``
    are all the same thing to a caller holding a query, and a traceback out
    of ``search`` reaches an agent as a broken tool rather than as an
    answerable condition. The reason string always carries the exception
    TYPE, so nothing is swallowed silently -- and callers that must NOT
    degrade (:mod:`trialerror.verify.hypothesis`,
    :mod:`trialerror.lens.novelty`) turn the same reason into a refusal
    instead.

    ``kind`` is passed through to the backend: ``"query"`` gets a retrieval
    instruction prefix on backends that use one, ``"document"`` does not.
    Callers embedding a STATEMENT to compare against stored document
    vectors (the novelty screen) pass ``"document"`` on purpose.
    """
    try:
        _model_key, backend = _resolve_embed_backend(store, side="query")
    except Exception as exc:  # noqa: BLE001 - a bad config table is a reason, not a traceback
        return None, f"the query-side embed backend could not be resolved ({type(exc).__name__}: {exc})"
    ok, reason = embed_backend_runnable(backend)
    if not ok:
        return None, reason
    try:
        vectors = backend.embed_batch([text], kind=kind)
    except Exception as exc:  # noqa: BLE001 - see the docstring: every failure is a reason here
        return None, f"the query-side embed backend failed ({type(exc).__name__}: {exc})"
    if not vectors:
        return None, "the query-side embed backend returned no vector"
    return list(vectors[0]), ""


def query_embed_refusal_message(reason: str, *, action: str) -> str:
    """One sentence shape for every REFUSAL built on
    :func:`query_vector_or_reason`'s reason: what could not be done, why,
    the doctor check that reports it, and the table to change."""
    return (
        f"{action} needs a query embedding and this process cannot produce one: {reason}. "
        f"Check `trialerror doctor --only {QUERY_EMBED_DOCTOR_CHECK}` and configure a runnable "
        f"query-side backend in [{QUERY_EMBED_TABLE}]."
    )


def _excluded_kinds_present(store: Store) -> bool:
    """Whether this corpus actually holds a source of a
    :data:`DEFAULT_EXCLUDED_KINDS` kind.

    The default exclusion has to apply to an UNFILTERED search too, and
    ``_filtered_chunk_ids`` answers a filtered search by materializing a
    candidate id list. Materializing every chunk id in the corpus just to
    subtract nothing would put an O(corpus) query on the hot path of every
    program that has no inventory at all -- which is every program until it
    ingests one. So the exclusion is skipped, and ``None`` (no restriction)
    returned, exactly when there is provably nothing to exclude. One indexed
    lookup answers that."""
    row = store.knowledge.execute(
        f"SELECT 1 FROM source WHERE kind IN ({','.join('?' for _ in DEFAULT_EXCLUDED_KINDS)}) LIMIT 1",
        list(DEFAULT_EXCLUDED_KINDS),
    ).fetchone()
    return row is not None


def _slice_from_lens_launch(store: Store, launch_id: str | None) -> list[str] | None:
    """The slice a lens booking declares by BEING LINKED to its assignment
    rows (``lens_assignment.lens_launch_id``, ops schema-v10).

    The third way a launch names a slice, and the only one that does not
    depend on what the caller put in ``attrs``: ``budget book --assign-id``
    writes the link from the assignment side, so a booking made without the
    exported attrs still resolves. ``None`` when no row names this launch --
    a launch nothing was assigned to is not a lens launch."""
    if not launch_id:
        return None
    rows = store.ops.execute(
        "SELECT slice_spec FROM lens_assignment WHERE lens_launch_id = ?", (launch_id,)
    ).fetchall()
    if not rows:
        return None
    doc_ids: list[str] = []
    for row in rows:
        try:
            spec = json.loads(row["slice_spec"])
        except (TypeError, ValueError):
            continue
        candidate_id = spec.get("candidate_id") if isinstance(spec, dict) else None
        if candidate_id and str(candidate_id) not in doc_ids:
            doc_ids.append(str(candidate_id))
    return doc_ids


def _slice_from_assignments(store: Store, attrs: Mapping[str, Any]) -> list[str] | None:
    """The slice a lens booking declares INDIRECTLY, through the assignment
    rows it names.

    ``lens/export.py`` writes ``assign_ids`` and ``roster_id`` onto every
    bookable row; each ``lens_assignment`` row's ``slice_spec`` names the
    document it assigned as ``candidate_id``. These are the same two
    fallbacks ``lens/checks.py::_slice_docs_for_launch`` resolves, and they
    are here for one reason: an audit that can find a slice the enforcement
    cannot is an audit reporting on a barrier that never engaged.

    ``None`` when neither key is present, so a non-lens launch keeps falling
    through to "no scope"."""
    for column, key in (("assign_id", "assign_ids"), ("roster_id", "roster_id")):
        raw = attrs.get(key)
        values = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else ([str(raw)] if raw else [])
        if not values:
            continue
        placeholders = ",".join("?" for _ in values)
        rows = store.ops.execute(
            f"SELECT slice_spec FROM lens_assignment WHERE {column} IN ({placeholders})", values
        ).fetchall()
        doc_ids: list[str] = []
        for row in rows:
            try:
                spec = json.loads(row["slice_spec"])
            except (TypeError, ValueError):
                continue
            candidate_id = spec.get("candidate_id") if isinstance(spec, dict) else None
            if candidate_id and str(candidate_id) not in doc_ids:
                doc_ids.append(str(candidate_id))
        # A key that named assignment rows resolves to what they hold, even
        # when that is nothing -- "booked against these rows, which assign no
        # document" is a declared empty slice, not an undeclared one.
        return doc_ids
    return None


def launch_slice_doc_ids(store: Store, launch_id: str | None) -> list[str] | None:
    """The document ids a launch was booked against, or ``None`` when the
    launch declares no slice at all.

    This is the per-launch retrieval scope the design asks for: a lens holds
    the knowledge tools, and those tools take ``source_ids``/``kind`` as
    *caller-supplied arguments*, so "only read your own slice" was a
    sentence in a prompt with a doctor check behind it. Reading the slice
    off the launch row makes it a restriction the caller cannot widen,
    because the caller does not supply it.

    Four sources, most direct first, matching
    ``lens/checks.py::_slice_docs_for_launch`` exactly so the enforcement
    and the audit resolve the same slice for the same launch:
    ``attrs.slice_doc_ids``; the ``assign_ids`` a bookable row carries; the
    ``roster_id`` it also carries; and the ``lens_assignment.lens_launch_id``
    link ``budget book --assign-id`` writes from the assignment side.
    Reading only the first would leave the scope engaged for no launch on
    the shipped booking path, since ``lens/export.py`` emits the next two --
    and reading only the attrs would leave it off for a booking made from
    the CLI, which carries none of them.

    **Absent is not empty.** ``None`` -- no restriction -- means the launch
    declares no slice: no launch id, a launch id that names no row, an
    unreadable ``attrs``, a non-lens launch, or a ``slice_doc_ids`` that is
    not a list. An explicitly DECLARED empty slice (``slice_doc_ids: []``,
    or assignment rows that assign nothing) returns ``[]`` and restricts the
    call to nothing. Reading a declared empty slice as "unrestricted" would
    make the enforcement fail OPEN exactly where
    ``lens_citations_within_slice`` fails closed -- the audit already treats
    that launch as one where every citation is a crossing.
    """
    if not launch_id:
        return None
    row = store.platform.execute("SELECT attrs FROM launch WHERE launch_id = ?", (launch_id,)).fetchone()
    attrs: Any = None
    if row is not None and row["attrs"]:
        try:
            attrs = json.loads(row["attrs"])
        except (TypeError, ValueError):
            attrs = None
    if isinstance(attrs, dict):
        slice_doc_ids = attrs.get(LAUNCH_SLICE_ATTR)
        if isinstance(slice_doc_ids, (list, tuple)):
            return [str(d) for d in slice_doc_ids]
        from_attrs = _slice_from_assignments(store, attrs)
        if from_attrs is not None:
            return from_attrs
    # Last, and deliberately not first: the link written from the assignment
    # side by `budget book --assign-id`. A launch with no attrs at all used
    # to fall straight out here as "no scope", which is the barrier off for a
    # lens booked through the CLI rather than from the exported row.
    return _slice_from_lens_launch(store, launch_id)


def _effective_filters(
    store: Store, filters: Mapping[str, Any] | None, *, launch_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(filters as the engine will actually apply them, scope note)``.

    One place, so the two retrieval surfaces (:func:`search`,
    :func:`similar`) cannot end up enforcing different barriers. The scope
    note is ``None`` when nothing was forced, and is echoed on the response
    when something was -- a restriction the caller did not ask for and
    cannot see would be indistinguishable from a corpus that simply has
    nothing to say.
    """
    effective = dict(filters) if filters else {}
    scope: dict[str, Any] | None = None
    slice_doc_ids = launch_slice_doc_ids(store, launch_id)
    if slice_doc_ids is not None:
        effective["doc_ids"] = slice_doc_ids
        scope = {"launch_id": launch_id, "doc_ids": list(slice_doc_ids), "reason": "launch_slice"}
    return (effective or None), scope


def _scope_clauses(store: Store, filters: Mapping[str, Any] | None) -> tuple[list[str], list[Any]]:
    """``(clauses, params)`` for one filter set, referencing ``document.*``
    and ``source.*`` only -- never ``chunk.*``.

    That restraint is what lets the SAME predicate answer a chunk-level
    question (:func:`_filtered_chunk_ids`) and a document-level one
    (:func:`_scoped_doc_ids`), so the id-addressed surfaces below cannot end
    up enforcing a different barrier from the ranked ones.

    Keys (design Section 7 ``SearchRequest.filters``:
    ``source_ids``/``kind``/``license_tier``/``year``, plus ``doc_ids``, the
    per-launch slice scope's own key). **A key present with an EMPTY list
    restricts to nothing** rather than being ignored: an empty list is a
    caller (or a booking) that named its universe and named nothing in it,
    and reading that as "no restriction" is the one misreading that turns a
    barrier into its opposite.

    One clause is added that no caller asked for:
    :data:`DEFAULT_EXCLUDED_KINDS` are excluded unless the caller named
    ``kind`` explicitly. Naming ``kind`` is the explicit request that lifts
    it -- ``kind=["inventory"]`` returns inventory rows and nothing else,
    which is precisely what the novelty screen asks for and precisely what
    no lens tool call does by accident.
    """
    filters = filters or {}
    clauses: list[str] = []
    params: list[Any] = []

    for key, column in (("source_ids", "source.source_id"), ("doc_ids", "document.doc_id")):
        if key not in filters or filters[key] is None:
            continue
        values = list(filters[key])
        if not values:
            clauses.append("0")  # declared, and empty: nothing is in scope
            continue
        clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
        params.extend(values)

    if "kind" in filters and filters["kind"] is not None:
        kinds = list(filters["kind"])
        if not kinds:
            clauses.append("0")
        else:
            clauses.append(f"source.kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
    elif _excluded_kinds_present(store):
        clauses.append(f"source.kind NOT IN ({','.join('?' for _ in DEFAULT_EXCLUDED_KINDS)})")
        params.extend(DEFAULT_EXCLUDED_KINDS)

    for key, column in (("license_tier", "source.license_tier"), ("year", "source.year")):
        if key not in filters or filters[key] is None:
            continue
        values = list(filters[key])
        if not values:
            clauses.append("0")
            continue
        clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
        params.extend(values)

    return clauses, params


def _filtered_chunk_ids(store: Store, filters: Mapping[str, Any] | None) -> list[str] | None:
    """``None`` means "no restriction, use every chunk"; otherwise the
    exact (possibly empty) list of chunk ids matching every filter clause
    :func:`_scope_clauses` built."""
    clauses, params = _scope_clauses(store, filters)
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


def _scoped_doc_ids(store: Store, doc_ids: Sequence[str], filters: Mapping[str, Any] | None) -> set[str]:
    """Which of ``doc_ids`` survive ``filters`` -- the document-level form of
    :func:`_filtered_chunk_ids`, for the surfaces addressed by id rather than
    by query."""
    ids = [d for d in dict.fromkeys(doc_ids) if d]
    if not ids:
        return set()
    clauses, params = _scope_clauses(store, filters)
    where = [f"document.doc_id IN ({','.join('?' for _ in ids)})", *clauses]
    sql = (
        "SELECT document.doc_id FROM document "
        "JOIN source ON source.source_id = document.source_id "
        f"WHERE {' AND '.join(where)}"
    )
    rows = store.knowledge.execute(sql, [*ids, *params]).fetchall()
    return {r["doc_id"] for r in rows}


def _doc_in_scope(store: Store, doc_id: str | None, *, launch_id: str | None) -> bool:
    """Whether ``doc_id`` is inside the barriers this call carries: the
    default kind exclusion, and the slice its launch declares. The one
    predicate every id-addressed surface below asks."""
    if not doc_id:
        return False
    effective, _scope = _effective_filters(store, None, launch_id=launch_id)
    return doc_id in _scoped_doc_ids(store, [doc_id], effective)


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

    effective_filters, scope = _effective_filters(store, filters, launch_id=launch_id)
    candidate_ids = _filtered_chunk_ids(store, effective_filters)
    if candidate_ids is not None and not candidate_ids:
        # a filter matched zero chunks -- an empty, well-formed response,
        # never an error (an over-narrow filter is a normal outcome).
        return _with_scope({
            "ok": True,
            "query_id": new_id("QRY"),
            "tiers_used": [],
            "results": [],
            "stats": {"fts_candidates": 0, "vector_scored": 0, "fulltext_backend": None, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)},
        }, scope)

    if mode == "summary":
        # build-v2-summary: a summary-first search path, entirely separate
        # from the fts/vector/RRF pipeline below (module docstring) --
        # "summary" still honors the SAME requested_tiers gate every other
        # mode/tier pairing does (a caller naming mode="summary" but
        # excluding "summary" from tiers gets an empty, well-formed
        # response, not a silent override of its own filter).
        if "summary" not in requested_tiers:
            return _with_scope({
                "ok": True,
                "query_id": new_id("QRY"),
                "tiers_used": [],
                "results": [],
                "stats": {"fts_candidates": 0, "vector_scored": 0, "fulltext_backend": None, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)},
            }, scope)
        return _with_scope(
            _search_summary_tier(store, query=query, k=k, candidate_chunk_ids=candidate_ids, unfenced=unfenced, launch_id=launch_id, t0=t0),
            scope,
        )

    want_fts = mode in ("auto", "fts", "hybrid", "graph") and "fts" in requested_tiers
    want_vector = mode in ("auto", "vector", "hybrid", "graph") and "vector" in requested_tiers
    want_graph = mode in ("auto", "hybrid", "graph") and "graph" in requested_tiers

    tier_rankings: dict[str, list[str]] = {}
    # LD-07: `fulltext_backend` is set below only when the fts tier actually
    # runs (`want_fts and query.strip()`) -- initialized here alongside its
    # two siblings so it is unconditionally present (as `None` when the fts
    # tier didn't run) rather than a key a caller must guard with
    # `.get(...)` on some code paths (vector-only mode, an empty query) and
    # not others.
    stats: dict[str, Any] = {"fts_candidates": 0, "vector_scored": 0, "fulltext_backend": None}

    if want_fts and query.strip():
        # C-0080: the lexical tier is now backend-pluggable
        # (:mod:`trialerror.retrieve.lexical`) -- tantivy by default, FTS5
        # whenever tantivy-py or a ready index is absent. Same rows, same
        # order semantics, same allowlist contract either way; the only
        # visible difference is ``stats.fulltext_backend``, which reports
        # which one actually ran (design Section 7's "engine reports what
        # it used", one level below the tier).
        fts_hits, fulltext_backend = lexical_search(
            store,
            query,
            limit=DEFAULT_FTS_CANDIDATE_LIMIT,
            chunk_id_allowlist=candidate_ids,
            config=_load_program_config(store),
        )
        tier_rankings["fts"] = [h["chunk_id"] for h in fts_hits]
        stats["fts_candidates"] = len(fts_hits)
        stats["fulltext_backend"] = fulltext_backend

    if want_vector and query.strip():
        # design Section 7 step 2: vector-score exactly the FTS candidate
        # set in the two-stage modes; in pure "vector" mode there is no FTS
        # stage, so the universe is the (filtered) whole corpus instead.
        #
        # The DOCUMENT side owns the model_key (it is the key the stored
        # vectors carry); the QUERY vector comes from the query side, which
        # may be a different backend object under the same key (lane F-1,
        # `[ingest.embed.query]`). Resolution has already refused any
        # configuration where those two keys disagree.
        model_key, _document_backend = _resolve_embed_backend(store)

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
        native_knn = (
            mode == "vector"
            and candidate_ids is None
            and vec_table_exists(store, model_key)
            and vec_backend_for(store, model_key) == VecBackend.SQLITE_VEC
        )
        vector_universe: list[str] | None = None
        if not native_knn:
            if mode == "vector":
                vector_universe = candidate_ids if candidate_ids is not None else _all_chunk_ids(store)
            else:
                vector_universe = tier_rankings.get("fts", [])

        # Only ASK for a query vector on a path that would actually rank
        # with one. A corpus with no vector table for this key, or a
        # two-stage mode whose FTS tier found nothing, was already a
        # no-vector-tier search before this lane and stays one -- reporting
        # a backend problem there would be reporting it for a search that
        # never needed the backend.
        if native_knn or (vector_universe and vec_table_exists(store, model_key)):
            query_vector, vector_reason = query_vector_or_reason(store, query)
            if query_vector is None:
                # D. Degrade vs refuse. mode="vector" IS the vector tier, so
                # there is nothing to fall back to; every other mode runs
                # its remaining tiers and says what it skipped.
                if mode == "vector":
                    raise QueryEmbedBackendUnrunnableError(
                        query_embed_refusal_message(vector_reason, action='search(mode="vector")')
                    )
                stats["vector_skipped_reason"] = vector_reason
                # Same fact under the key the public surfaces already read,
                # and on stderr: a degraded search that says so only inside
                # an envelope field is a silent one for anybody running the
                # CLI by hand.
                stats["vector_unavailable"] = query_embed_refusal_message(
                    vector_reason, action=f'search(mode="{mode}")'
                )
                print(
                    f"trialerror: vector tier unavailable, ranking lexically ({mode} mode): {vector_reason}",
                    file=sys.stderr,
                )
            elif native_knn:
                ranked = fetch_native_knn(store, model_key, query_vector, k=max(k, 0))
                tier_rankings["vector"] = [cid for cid, _ in ranked]
                stats["vector_scored"] = len(ranked)
            else:
                # E. The resident matrix (:mod:`trialerror.retrieve.vecmatrix`)
                # answers the LARGE universe -- in practice mode="vector",
                # whose universe is the whole corpus; the two-stage modes
                # rank a few hundred FTS candidates and stay on the Python
                # path they were always on. ``None`` means "not applicable
                # here" (numpy absent, small universe, unreadable table) and
                # the uncached path below runs exactly as before. When it
                # does apply, its ranking and its count are byte-identical to
                # that path's -- see the module docstring for how.
                matrix_ranked = vecmatrix.top_ranked(
                    store,
                    model_key,
                    query_vector,
                    k=max(k, 0),
                    restrict=None if (mode == "vector" and candidate_ids is None) else (vector_universe or []),
                    config=_load_program_config(store),
                )
                if matrix_ranked is not None:
                    ranked, n_scored = matrix_ranked
                    tier_rankings["vector"] = [cid for cid, _ in ranked]
                    stats["vector_scored"] = n_scored
                    stats["vector_matrix"] = True
                else:
                    vectors = fetch_vectors(store, model_key, vector_universe or [])
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
            if candidate_ids is not None:
                # The graph tier WIDENS recall by one hop, and a hop out of
                # the allowlist is a hop out of the barrier. Every other tier
                # is bounded by `candidate_ids` (fts takes it as an
                # allowlist, vector ranks over it); this one fetches
                # neighbours by relation edge and knows nothing about
                # filters, so the intersection has to happen here or the
                # default kind exclusion and the launch slice are both one
                # relation edge away from being advisory.
                allowed = set(candidate_ids)
                graph_chunk_ids = [cid for cid in graph_chunk_ids if cid in allowed]
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

    # Lane FB-1 item F3: a zero result explains itself. Every lexical backend
    # ANDs a multi-token query, so one unmatched term returns nothing for a
    # query whose other terms have hundreds of hits -- and the response said
    # only "0". Computed ONLY here: `results` is empty, the query is not
    # blank, and the fts tier was actually requested (a vector-only search's
    # emptiness has nothing to do with terms). At most
    # `MAX_PER_TERM_CANDIDATES` probes, and none at all on a search that
    # returned something, so the cost lands exactly where the diagnosis is
    # wanted.
    if not results and want_fts and query.strip():
        # `stats["fulltext_backend"]` is already the name of the backend that
        # served this search: the fts tier above runs under exactly this
        # block's guard and sets it unconditionally. The diagnosis therefore
        # has nothing to add about which index answered -- it came from the
        # same one, resolved the same way (fix-accept, V-9: the guard that
        # used to sit here could not fire).
        term_counts, _diag_backend = per_term_candidates(
            store,
            query,
            limit=DEFAULT_FTS_CANDIDATE_LIMIT,
            chunk_id_allowlist=candidate_ids,
            config=_load_program_config(store),
        )
        if term_counts:
            # Both counts are scoped to `candidate_ids` -- the filters this
            # search ran under -- so a zero here means "nothing the search was
            # allowed to see", never "nothing in the corpus". Every surface
            # that words these keys says so (fix-accept, V-8).
            stats["per_term_candidates"] = term_counts
            stats["zero_result_terms"] = [t for t, n in term_counts.items() if n == 0]

    stats["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return _with_scope({
        "ok": True,
        "query_id": new_id("QRY"),
        "tiers_used": sorted(tier_rankings.keys()),
        "results": results,
        "stats": stats,
    }, scope)


def _with_scope(response: dict[str, Any], scope: dict[str, Any] | None) -> dict[str, Any]:
    """Attach the per-launch scope note to a response, and ONLY when a scope
    was actually forced.

    A restriction the caller did not ask for and cannot see is
    indistinguishable from a corpus with nothing to say, which is exactly
    the confusion a scoped agent would report as "the corpus is empty". A
    response with no forced scope is left byte-identical to what this engine
    returned before the scope existed."""
    if scope is not None:
        response["scope"] = scope
    return response


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
            "fulltext_backend": None,
            "summary_candidates": len(rows),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        },
    }


# ---------------------------------------------------------------------------
# get_chunk
# ---------------------------------------------------------------------------


def get_chunk(store: Store, chunk_id: str, *, launch_id: str | None = None) -> dict[str, Any]:
    """Design Section 5.1: "chunk text + element/page context + anchors".

    Carries the same two engine-level barriers :func:`search` does. A chunk
    addressed BY ID skips ranking entirely, so without this an id learned
    anywhere -- a citation, a neighbour listing, :func:`resolve_quote` --
    would read an excluded row or an out-of-slice document verbatim, and the
    barrier would hold only against callers who went the long way round.
    Out of scope raises :class:`ChunkNotFoundError`: within this call's
    universe the chunk genuinely is not there, and a distinct "exists but
    withheld" error would itself answer the question the barrier exists to
    refuse."""
    chunk = store_get(store, "chunk", pk_column="chunk_id", pk_value=chunk_id)
    if chunk is None:
        raise ChunkNotFoundError(f"no such chunk: {chunk_id!r}")
    if not _doc_in_scope(store, chunk["doc_id"], launch_id=launch_id):
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


def get_source(store: Store, source_id: str, *, launch_id: str | None = None) -> dict[str, Any]:
    """The source record and its document list, filtered to the documents
    this call may see. The source ROW itself (title, licence tier) is
    metadata, not corpus text -- what the barriers withhold is content, and
    a document list that named excluded or out-of-slice documents would be a
    list of ids to feed straight to :func:`get_chunk`."""
    source = store_get(store, "source", pk_column="source_id", pk_value=source_id)
    if source is None:
        raise SourceNotFoundError(f"no such source: {source_id!r}")
    documents = [dict(r) for r in store.knowledge.execute("SELECT * FROM document WHERE source_id = ? ORDER BY rel_path", (source_id,))]
    effective, _scope = _effective_filters(store, None, launch_id=launch_id)
    in_scope = _scoped_doc_ids(store, [d["doc_id"] for d in documents], effective)
    documents = [d for d in documents if d["doc_id"] in in_scope]
    return {"source": source, "documents": documents}


def get_document_outline(store: Store, doc_id: str, *, launch_id: str | None = None) -> dict[str, Any]:
    """Design Section 5.1's document outline, behind the same barriers
    :func:`get_chunk` carries -- the outline previews element text, so an
    out-of-scope outline is out-of-scope content in smaller pieces.

    **A fenced document still gets an outline.** An outline is structural
    metadata -- which headings exist, where they start and end, what their
    element ids are -- and the only verbatim bytes in it are each heading's
    own text, which is truncated to the fence's word limit exactly as every
    other fenced excerpt is. Withholding it would not protect the licence;
    it would only make outline-first reading impossible for precisely the
    documents where reading the whole thing is not an option, which is what
    sent a lens back to scoped vector search over a fenced rulebook.

    **An empty outline says why.** A document whose normalizer emitted no
    heading elements at all is a different fact from a document whose
    outline this call declined to serve, and ``outline: []`` alone reads as
    the second. ``reason: "no_heading_elements"`` is present exactly when
    the list is empty, beside ``n_elements`` -- so a caller can tell "this
    document has no headings" from "this document has no elements" without
    a second query."""
    doc = store_get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise DocumentNotFoundError(f"no such document: {doc_id!r}")
    if not _doc_in_scope(store, doc_id, launch_id=launch_id):
        raise DocumentNotFoundError(f"no such document: {doc_id!r}")
    source = store_get(store, "source", pk_column="source_id", pk_value=doc["source_id"])
    fenced = is_fenced_license(source.get("license_tier")) if source else False

    elements = [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT * FROM element WHERE doc_id = ? ORDER BY seq", (doc_id,)
        )
    ]
    structural = [i for i, d in enumerate(elements) if d["type"] in _OUTLINE_ELEMENT_TYPES]
    outline: list[dict[str, Any]] = []
    for position, index in enumerate(structural):
        d = elements[index]
        # The section a heading opens runs to the element before the next
        # heading -- that span is the "page range" an outline-first reader
        # uses to decide what to fetch, and it cannot be derived from a
        # per-heading page number alone.
        end_index = structural[position + 1] - 1 if position + 1 < len(structural) else len(elements) - 1
        pages = [
            e.get("page_number") for e in elements[index:end_index + 1] if e.get("page_number") is not None
        ]
        preview = excerpt_words(d.get("text"), MAX_FENCED_EXCERPT_WORDS) if fenced else (d.get("text") or "")
        outline.append(
            {
                "element_id": d["element_id"],
                "type": d["type"],
                "seq": d["seq"],
                "page_number": d.get("page_number"),
                "page_start": min(pages) if pages else None,
                "page_end": max(pages) if pages else None,
                "parent_element": d.get("parent_element"),
                "category_depth": d.get("category_depth"),
                "text_preview": preview,
            }
        )
    envelope: dict[str, Any] = {
        "doc_id": doc_id,
        "fenced": fenced,
        "outline": outline,
        "n_elements": len(elements),
    }
    if not outline:
        envelope["reason"] = "no_heading_elements"
    return envelope


# ---------------------------------------------------------------------------
# resolve_quote
# ---------------------------------------------------------------------------


def resolve_quote(
    store: Store, quote: str, *, source_id: str | None = None, doc_id: str | None = None, launch_id: str | None = None
) -> dict[str, Any]:
    """Design Section 5.1: "quote text -> matching anchors (doc, page,
    span) or NOT_FOUND". Fast path: exact ``quote_sha256`` match (a caller
    supplying the FULL text an anchor was hashed from -- "known-quote
    query returns its anchor page/span", the M8 acceptance wording). Falls
    back to a substring scan over ``quote_anchor.quote_text`` (a caller
    supplying a partial quote) when the exact hash misses.

    Both paths are then cut to the documents this call may see. The
    substring path is why that matters: it is an unranked ``LIKE`` over
    every anchor in the corpus, so a one-character quote enumerates anchor
    and chunk ids across the whole store -- which is a directory of exactly
    the ids the other barriers exist to keep out of reach."""
    from trialerror.ingest.anchors import sha256_hex

    # The scope clauses are ANDed into BOTH queries rather than applied to
    # their results: the substring path's LIMIT 20 has to count anchors this
    # caller may see, or a corpus with 20 out-of-scope matches would answer
    # "not found" for an in-scope one.
    effective, _scope = _effective_filters(store, None, launch_id=launch_id)
    scope_clauses, scope_params = _scope_clauses(store, effective)
    joined = (
        "quote_anchor JOIN document ON document.doc_id = quote_anchor.doc_id "
        "JOIN source ON source.source_id = document.source_id"
    )

    qsha = sha256_hex(quote)
    clauses = ["quote_anchor.quote_sha256 = ?", *scope_clauses]
    params: list[Any] = [qsha, *scope_params]
    if doc_id:
        clauses.append("quote_anchor.doc_id = ?")
        params.append(doc_id)
    rows = [
        dict(r)
        for r in store.knowledge.execute(f"SELECT quote_anchor.* FROM {joined} WHERE {' AND '.join(clauses)}", params)
    ]
    match_type = "exact"

    if not rows:
        escaped = quote.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_clauses = ["quote_anchor.quote_text LIKE ? ESCAPE '\\'", *scope_clauses]
        like_params: list[Any] = [f"%{escaped}%", *scope_params]
        if doc_id:
            like_clauses.append("quote_anchor.doc_id = ?")
            like_params.append(doc_id)
        rows = [
            dict(r)
            for r in store.knowledge.execute(
                f"SELECT quote_anchor.* FROM {joined} WHERE {' AND '.join(like_clauses)} "
                "ORDER BY quote_anchor.created_ts ASC LIMIT 20",
                like_params,
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


def similar(
    store: Store,
    ref_id: str,
    *,
    kind: str = "chunk",
    k: int = 10,
    filters: Mapping[str, Any] | None = None,
    launch_id: str | None = None,
) -> dict[str, Any]:
    """Design Section 5.1: "nearest chunks/claims to a given id".

    Carries the SAME two engine-level restrictions :func:`search` does, and
    for the same reason: a barrier enforced on one retrieval surface and not
    the other is not enforced. ``filters`` lifts the
    :data:`DEFAULT_EXCLUDED_KINDS` exclusion the same way (name ``kind``
    explicitly), and ``launch_id`` applies that launch's declared slice
    scope.

    When either restriction is in force this function ranks over the
    restricted universe rather than post-filtering a native-KNN page --
    post-filtering would silently return fewer than ``k`` results, and "your
    nearest neighbours, minus some you may not see, and we won't say how
    many" is not an answer. Unrestricted calls keep the native-KNN fast path
    exactly as before."""
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
    effective_filters, scope = _effective_filters(store, filters, launch_id=launch_id)
    allowed = _filtered_chunk_ids(store, effective_filters)
    if allowed is not None and not allowed:
        return _with_scope({"ok": True, "results": [], "note": "no chunk is within this call's retrieval scope"}, scope)

    # B.4b (see trialerror.retrieve.vecsearch's module docstring): an
    # UNRESTRICTED similar() ranks against the WHOLE corpus -- the other
    # genuinely UNBOUNDED path bake-off B.4b names. Same opt-in gate as
    # search(mode="vector")'s own B.4b branch: only when this model_key's
    # table is a real vec0 table; the fallback-backend path below is
    # otherwise byte-identical to this function's pre-B.4b behavior.
    if allowed is None and vec_backend_for(store, model_key) == VecBackend.SQLITE_VEC:
        ranked = fetch_native_knn(store, model_key, query_vector, k=k, exclude_chunk_id=ref_id)
    else:
        # E (lane F-1): THE path this lane was measured on. An unrestricted
        # similar() ranks the whole corpus, which on the default fallback
        # table means deserialising every row in Python -- seconds of wall
        # clock at 16k x 2048. The resident matrix returns the same top k,
        # byte for byte, without the per-row Python. It declines (``None``)
        # for a small universe or an absent numpy, and then nothing about
        # this function has changed.
        matrix_ranked = vecmatrix.top_ranked(
            store, model_key, query_vector, k=max(k, 0), restrict=allowed, exclude=ref_id,
            config=_load_program_config(store),
        )
        if matrix_ranked is not None:
            ranked = matrix_ranked[0]
        else:
            universe = [cid for cid in (allowed if allowed is not None else _all_chunk_ids(store)) if cid != ref_id]
            vectors = fetch_vectors(store, model_key, universe)
            # ``k=`` rather than a slice afterwards: this caller only ever
            # wanted the top k, and saying so lets vecmath narrow with numpy
            # instead of running a Python cosine over the whole universe.
            # Same ids, same order, same scores (trialerror.util.vecmath).
            ranked = rank_by_query_vector(
                query_vector, vectors, k=max(k, 0), config=_load_program_config(store)
            )
    ctx = _fetch_chunk_context(store, [cid for cid, _ in ranked])

    results = []
    for i, (cid, score) in enumerate(ranked):
        row = _build_result_row(cid, rank=i + 1, score=score, fusion={"vector": i + 1}, ctx=ctx)
        if row is not None:
            results.append(row)
    return _with_scope({"ok": True, "results": results}, scope)


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


def _edges_within_scope(
    store: Store, edges: list[dict[str, Any]], *, launch_id: str | None
) -> list[dict[str, Any]]:
    """Drop the edges whose evidence sits outside this call's barriers.

    A ``relation`` row's only link to the corpus is ``evidence_anchor`` ->
    ``quote_anchor.doc_id``, and ``fact_text`` is a sentence extracted FROM
    that document. So an edge anchored in an excluded or out-of-slice
    document is that document's content, one indirection along, and is held
    back for the same reason the chunk is. An edge whose anchor no longer
    resolves is dropped too -- the same posture
    :func:`_fence_relation_edges` takes for a missing source: unknown
    provenance is served as if it were fenced, never as if it were open."""
    if not edges:
        return edges
    anchor_ids = [e["evidence_anchor"] for e in edges if e.get("evidence_anchor")]
    if not anchor_ids:
        return []
    ph = ",".join("?" for _ in anchor_ids)
    doc_by_anchor = {
        r["anchor_id"]: r["doc_id"]
        for r in store.knowledge.execute(f"SELECT anchor_id, doc_id FROM quote_anchor WHERE anchor_id IN ({ph})", anchor_ids)
    }
    effective, _scope = _effective_filters(store, None, launch_id=launch_id)
    in_scope = _scoped_doc_ids(store, list(doc_by_anchor.values()), effective)
    return [e for e in edges if doc_by_anchor.get(e.get("evidence_anchor")) in in_scope]


def graph_neighbors(
    store: Store,
    entity_id: str,
    *,
    as_of: str | None = None,
    as_of_tx: str | None = None,
    k: int = 50,
    launch_id: str | None = None,
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
    edges = _edges_within_scope(store, edges, launch_id=launch_id)
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
    summary".

    ``documents`` is the LIVE corpus. A retracted document
    (:mod:`trialerror.ingest.retract`) is still a row -- the withdrawal is
    part of the record -- but it is no longer part of the corpus and none
    of its derived rows survive, so counting it here would tell an operator
    the garbage they just removed is still there. Reported separately as
    ``retracted_documents`` rather than silently dropped, through the same
    ``retracted_doc_ids`` seam the dashboard's corpus panel uses: ``trialerror
    query stats`` and the knowledge MCP server answer the same operator
    question that panel does, and answering it two different ways depending
    on the surface is worse than either answer."""
    # CRITICAL RULE (M7's own live bug, carried forward): sqlite-vec's
    # loadable extension is per-CONNECTION -- this function may run on a
    # freshly-opened Store (e.g. one call per MCP tools/call) that has
    # never loaded it, and every ``vec_chunks__*`` table below is queried
    # by name.
    try_load_sqlite_vec(store.knowledge)

    from trialerror.ingest.retract import retracted_doc_ids

    def _count(sql: str, params: Sequence[Any] = ()) -> int:
        return int(store.knowledge.execute(sql, params).fetchone()[0])

    sources = _count("SELECT COUNT(*) FROM source")
    retracted_documents = len(retracted_doc_ids(store.knowledge))
    documents = _count("SELECT COUNT(*) FROM document") - retracted_documents
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

    # C-0080: the lexical tier's second, non-SQLite index. Reported
    # alongside chunk_fts (never instead of it) -- FTS5 stays the fallback
    # backend, so "how fresh is chunk_fts" remains a real question even on
    # a program serving searches out of tantivy.
    fulltext_backend = lexical.resolve_backend(store).name
    index_dir = lexical.fulltext_index_dir(store)
    fulltext_index: dict[str, Any] = {"backend": fulltext_backend, "index_dir": str(index_dir) if index_dir else None}
    if index_dir is not None and tantivysearch.tantivy_available():
        # LD-04: this is a summary call (`trialerror query stats` / MCP tool
        # #8), not the doctor -- `cheap=True` compares chunk COUNTS instead
        # of running the doctor's full XOR fingerprint scan over every
        # chunk_id, so a "summary" stays a summary at multi-million-chunk
        # scale. Only `state` and `index_docs` are consumed below, and
        # neither needs the fingerprint to be meaningful.
        status = tantivysearch.index_status(store.knowledge, index_dir, cheap=True)
        fulltext_index.update(
            {
                "state": status["state"],
                "indexed_docs": status["index_docs"],
                "chunks_missing_index": max(chunks - (status["index_docs"] or 0), 0),
            }
        )
    else:
        fulltext_index["state"] = "unavailable"

    # Lane F-1 item E: the resident similarity matrix is DERIVED state with a
    # fingerprint, and an operator asking "what does this corpus hold" is
    # asking whether the fast path is built and current for each key.
    # ``[paths] index_dir`` moves the cache, so the status has to be read
    # with the program's config in hand -- without it this reports
    # ``present: false`` and a path that does not exist for a cache that is
    # on disk and being served, and disagrees with
    # ``doctor --only vecmatrix_stale`` (which does pass it) on the same
    # program in the same second.
    program_config = _load_program_config(store)
    vecmatrix_by_model = {
        reg["model_key"]: vecmatrix.matrix_status(store, reg["model_key"], program_config)
        for reg in registry_rows
    }

    return {
        "sources": sources,
        "documents": documents,
        "retracted_documents": retracted_documents,
        "chunks": chunks,
        "chunk_fts_rows": chunk_fts_rows,
        "chunks_missing_fts": max(chunks - chunk_fts_rows, 0),
        "fulltext_backend": fulltext_backend,
        "fulltext_index": fulltext_index,
        "quote_anchors": anchors,
        "embeddings_by_model_key": emb_by_model,
        "vector_index": registry_rows,
        "vec_rows_by_model_key": vec_by_model,
        "chunks_missing_vec_by_model_key": {mk: max(chunks - n, 0) for mk, n in vec_by_model.items()},
        "vecmatrix_by_model_key": vecmatrix_by_model,
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
