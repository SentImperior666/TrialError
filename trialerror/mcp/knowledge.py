"""``trialerror-knowledge`` -- the Resource Gateway MCP server. Design Section 12
(M8 row): "MCP server (11 tools)". Design Section 5.1's ``trialerror-knowledge``
table (read-only; all content sanitized, untrusted-wrapped, and
license-fenced per Section 7) pins the exact 11 tools and their landed-API
mapping:

======================  ==========================================================
Tool                    Landed API wrapped
======================  ==========================================================
search                  trialerror.retrieve.engine.search                     (M8)
get_chunk               trialerror.retrieve.engine.get_chunk                  (M8)
get_source              trialerror.retrieve.engine.get_source                 (M8)
get_document_outline    trialerror.retrieve.engine.get_document_outline       (M8)
resolve_quote           trialerror.retrieve.engine.resolve_quote              (M8)
similar                 trialerror.retrieve.engine.similar                    (M8)
graph_neighbors         trialerror.retrieve.engine.graph_neighbors            (M8)
corpus_stats            trialerror.retrieve.engine.corpus_stats               (M8)
memory_search           trialerror.memory.api.{search_items,get_item,
                         boot_bundle}                                    (M11)
list_requests           trialerror.retrieve.engine.list_requests              (M8, over
                         source.request_state -- M7's request queue)
poll_job                trialerror.jobs.ledger.get_job                        (M2)
======================  ==========================================================

Every handler is a THIN wrapper (build brief's binding instruction): parse
the MCP ``arguments`` dict, call the landed subsystem function, shape the
result as a ``trialerror.util.envelope`` dict. No business logic lives here --
the hybrid pipeline, citation assembly, the F3 serving-path license fence,
and the untrusted-wrap all live in :mod:`trialerror.retrieve.engine` and its
sibling modules, shared verbatim with ``trialerror query search`` (``trialerror/cli/
query.py``) and (later) M9's verification pipelines.

Built on ``trialerror.mcp.protocol`` -- the generic stdio JSON-RPC transport
M14's builder shipped for the ``trialerror-ops`` server (``trialerror/mcp/ops.py``)
with an explicit dedup invitation ("point M8's ``trialerror/mcp/knowledge.py``
at this module rather than duplicating it"). This module mirrors
``trialerror.mcp.ops``'s own ``_wrap``/``build_tools``/``build_server``/
``run_server`` shape for consistency across both servers.

**F3 structural enforcement -- the one thing this file is uniquely
responsible for getting right:** no tool below accepts (or forwards) an
``unfenced`` argument. ``trialerror.retrieve.engine.search``'s ``unfenced``
parameter is a CLI-only, human-flagged, logged escape hatch (design
Section 7) that ``trialerror/cli/query.py``'s ``search`` action exposes; THIS
server never reads or passes it, so an MCP client -- an agent -- cannot
request the fence bypass no matter what arguments it sends. That is what
"the fence lives in the retrieval engine itself" (Section 7) combines with
"never expose the bypass on an agent surface" to mean in practice.

**Cross-cutting per-call log line** (``DESIGN_v0.md`` Appendix B: "per-call
log line (tool, input-hash, latency, output-size, error-code) -> events"):
implemented once, centrally, in :func:`_wrap` -- every tool call (both
servers' shared transport, and every direct-dispatch test) gets exactly one
``event`` row, best-effort (a logging failure never fails the tool call
itself).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from trialerror import __version__
from trialerror.events.api import append_event
from trialerror.jobs.ledger import get_job
from trialerror.mcp.protocol import ToolServer, ToolSpec, serve_stdio
from trialerror.memory.api import boot_bundle, get_item, search_items
from trialerror.retrieve import engine
from trialerror.retrieve.errors import RetrievalError
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.envelope import error_envelope, ok_envelope
from trialerror.util.timeutil import now_dt, parse

__all__ = ["SERVER_NAME", "TOOL_COUNT", "build_tools", "build_server", "run_server"]

SERVER_NAME = "trialerror-knowledge"
SERVER_INSTRUCTIONS = (
    "Read-only research-corpus retrieval: hybrid (FTS+vector) search, citation-grounded "
    "chunk/source/document lookups, quote resolution, nearest-neighbor, entity-graph "
    "neighbors, corpus stats, progressive-disclosure memory search, the acquisition "
    "request queue, and job polling. Every search/get_chunk/similar result carries a "
    "non-null citation block; commercial_restricted sources are served fenced "
    "(<=20-word excerpt, fenced:true) -- never raw verbatim text (design Section 7). "
    "See docs/DESIGN_v0.md Section 5.1/7 for the full contract."
)
#: Design Section 5.1 table: exactly 11 tools.
TOOL_COUNT = 11

#: mirrors ``source.request_state``'s DDL CHECK domain, for the ``list_requests`` schema.
_REQUEST_STATES = ("wanted", "requested", "delivered", "verifying", "archived", "indexed", "rejected", "failed")


# ---------------------------------------------------------------------------
# per-call plumbing: fresh Store per call, structured errors, event logging
# ---------------------------------------------------------------------------


def _input_hash(arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(arguments), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _log_call(store: Store, *, name: str, arguments: Mapping[str, Any], envelope: Mapping[str, Any], elapsed_ms: float) -> None:
    """Appendix B cross-cutting rule: "per-call log line (tool, input-hash,
    latency, output-size, error-code) -> events". Best-effort -- a logging
    failure (e.g. a redaction-pass surprise) must never turn a successful
    tool call into a failed one."""
    try:
        error_code = None if envelope.get("ok") else (envelope.get("error") or {}).get("code")
        append_event(
            store,
            event_type="mcp_tool_call",
            payload={
                "server": SERVER_NAME,
                "tool": name,
                "input_hash": _input_hash(arguments),
                "latency_ms": elapsed_ms,
                "output_size": len(json.dumps(envelope, default=str, ensure_ascii=False)),
                "error_code": error_code,
            },
        )
    except Exception:  # noqa: BLE001 -- logging is never allowed to break a tool call
        pass


def _wrap(name: str, description: str, input_schema: dict[str, Any], fn, *, program_root: Path, platform_root: Path | None):
    """Bind one handler to a fresh :class:`~trialerror.stores.store.Store` per
    call (opened and closed exactly once per ``tools/call``, mirroring
    ``trialerror.mcp.ops``'s own ``_wrap``), turn every retrieval/store error
    into a structured envelope, and log the per-call event line."""

    def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
        # FX-3 (IMPL_REVIEW_C_ops.md N-2): the store MUST close on every exit
        # path, including a handler exception of a type not listed below (an
        # unanticipated bug, or e.g. sqlite3.OperationalError surfacing from a
        # busy-timeout race) -- `with` guarantees Store.__exit__/close() runs
        # even when that exception propagates past this function entirely, so
        # no path can strand the 4 WAL connections in this long-lived server.
        with open_store(program_root, platform_root=platform_root) as store:
            t0 = time.perf_counter()
            try:
                envelope = fn(arguments, store=store)
            except RetrievalError as exc:
                envelope = error_envelope(name, type(exc).__name__, str(exc))
            except StoreError as exc:
                envelope = error_envelope(name, "store_error", str(exc))
            except (ValueError, TypeError, KeyError) as exc:
                # a malformed argument that made it past trialerror.mcp.protocol's
                # required-field check (a bad TYPE, or a direct-dispatch test
                # calling this handler without going through tools/call).
                envelope = error_envelope(name, "bad_input", f"{type(exc).__name__}: {exc}")
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            _log_call(store, name=name, arguments=arguments, envelope=envelope, elapsed_ms=elapsed_ms)
            return envelope

    return ToolSpec(name=name, description=description, input_schema=input_schema, handler=handler)


# ---------------------------------------------------------------------------
# 1. search
# ---------------------------------------------------------------------------


def _tool_search(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if args.get("source_ids"):
        filters["source_ids"] = list(args["source_ids"])
    if args.get("kind"):
        filters["kind"] = list(args["kind"])
    if args.get("license_tier"):
        filters["license_tier"] = list(args["license_tier"])
    if args.get("year"):
        filters["year"] = list(args["year"])
    result = engine.search(
        store,
        query=args["query"],
        k=int(args.get("k", engine.DEFAULT_K)),
        mode=args.get("mode", "auto"),
        filters=filters or None,
        tiers=args.get("tiers"),
        as_of=args.get("as_of"),
        # `unfenced` is deliberately NEVER read from `args` -- see module docstring.
    )
    return ok_envelope("search", result=result)


# ---------------------------------------------------------------------------
# 2. get_chunk
# ---------------------------------------------------------------------------


def _tool_get_chunk(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.get_chunk(store, args["chunk_id"])
    return ok_envelope("get_chunk", result=result)


# ---------------------------------------------------------------------------
# 3. get_source
# ---------------------------------------------------------------------------


def _tool_get_source(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.get_source(store, args["source_id"])
    return ok_envelope("get_source", result=result)


# ---------------------------------------------------------------------------
# 4. get_document_outline
# ---------------------------------------------------------------------------


def _tool_get_document_outline(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.get_document_outline(store, args["doc_id"])
    return ok_envelope("get_document_outline", result=result)


# ---------------------------------------------------------------------------
# 5. resolve_quote
# ---------------------------------------------------------------------------


def _tool_resolve_quote(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.resolve_quote(store, args["quote"], source_id=args.get("source_id"), doc_id=args.get("doc_id"))
    if not result["found"]:
        return error_envelope("resolve_quote", "not_found", "no anchor matches the given quote text", details=result)
    return ok_envelope("resolve_quote", result=result)


# ---------------------------------------------------------------------------
# 6. similar
# ---------------------------------------------------------------------------


def _tool_similar(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.similar(store, args["id"], kind=args.get("kind", "chunk"), k=int(args.get("k", 10)))
    return ok_envelope("similar", result=result)


# ---------------------------------------------------------------------------
# 7. graph_neighbors
# ---------------------------------------------------------------------------


def _tool_graph_neighbors(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.graph_neighbors(
        store, args["entity_id"], as_of=args.get("as_of"), as_of_tx=args.get("as_of_tx"), k=int(args.get("k", 50))
    )
    return ok_envelope("graph_neighbors", result=result)


# ---------------------------------------------------------------------------
# 8. corpus_stats
# ---------------------------------------------------------------------------


def _tool_corpus_stats(_args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    return ok_envelope("corpus_stats", result=engine.corpus_stats(store))


# ---------------------------------------------------------------------------
# 9. memory_search (M11: search_items + get_item is the read-only pair this wraps)
# ---------------------------------------------------------------------------


def _tool_memory_search(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    if args.get("id"):
        item = get_item(store, args["id"])
        if item is None:
            return error_envelope("memory_search", "not_found", f"no memory_item {args['id']!r}")
        return ok_envelope("memory_search", result={"item": item})

    if args.get("boot_bundle"):
        kwargs: dict[str, Any] = {"account_id": args.get("account_id")}
        if args.get("token_budget") is not None:
            kwargs["token_budget"] = int(args["token_budget"])
        return ok_envelope("memory_search", result=boot_bundle(store, **kwargs))

    items = search_items(
        store,
        query=args.get("query"),
        tier=args.get("tier"),
        kind=args.get("kind"),
        account_id=args.get("account_id"),
        status=args.get("status", "active"),
        limit=int(args.get("limit", 50)),
    )
    return ok_envelope("memory_search", result={"items": items, "count": len(items)})


# ---------------------------------------------------------------------------
# 10. list_requests
# ---------------------------------------------------------------------------


def _tool_list_requests(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = engine.list_requests(store, state=args.get("state"), limit=int(args.get("limit", 100)))
    return ok_envelope("list_requests", result=result)


# ---------------------------------------------------------------------------
# 11. poll_job
# ---------------------------------------------------------------------------


def _tool_poll_job(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    job = get_job(store, args["job_id"])
    if job is None:
        return error_envelope("poll_job", "not_found", f"no such job: {args['job_id']!r}")
    heartbeat_age_s = (now_dt() - parse(job["heartbeat_ts"])).total_seconds() if job.get("heartbeat_ts") else None
    return ok_envelope("poll_job", result={"job": job, "heartbeat_age_s": heartbeat_age_s})


# ---------------------------------------------------------------------------
# server assembly
# ---------------------------------------------------------------------------


def build_tools(*, program_root: Path, platform_root: Path | None = None) -> dict[str, ToolSpec]:
    """Build the exact 11-tool registry (design Section 5.1), each bound to
    ``program_root``/``platform_root`` for the lifetime of one server
    process."""
    w = lambda *a, **kw: _wrap(*a, **kw, program_root=program_root, platform_root=platform_root)  # noqa: E731

    tools = {
        "search": w(
            "search",
            "Hybrid (FTS prefilter -> vector rerank -> reciprocal-rank fusion) search over the "
            "research corpus (tool #1, wraps trialerror.retrieve.engine.search). Every result row "
            "carries a non-null citation block (source_id/title/license_tier/anchor/quote); "
            "commercial_restricted sources are served fenced (<=20-word excerpt, fenced:true).",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": f"default {engine.DEFAULT_K}"},
                    "mode": {"type": "string", "enum": list(engine.SEARCH_MODES)},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "array", "items": {"type": "string"}},
                    "license_tier": {"type": "array", "items": {"type": "string"}},
                    "year": {"type": "array", "items": {"type": "integer"}},
                    "tiers": {"type": "array", "items": {"type": "string"}, "description": "requested tiers; engine reports what it actually used"},
                    "as_of": {"type": "string", "description": "valid-time point-in-time (ISO-8601); no-op for chunk search in v0 (chunks are not bi-temporal)"},
                },
                "required": ["query"],
            },
            _tool_search,
        ),
        "get_chunk": w(
            "get_chunk",
            "Chunk text (fenced+untrusted-wrapped per source license tier) + element/page context "
            "+ anchors (tool #2, wraps trialerror.retrieve.engine.get_chunk).",
            {"type": "object", "properties": {"chunk_id": {"type": "string"}}, "required": ["chunk_id"]},
            _tool_get_chunk,
        ),
        "get_source": w(
            "get_source",
            "Source record + license tier + document list (tool #3, wraps trialerror.retrieve.engine.get_source).",
            {"type": "object", "properties": {"source_id": {"type": "string"}}, "required": ["source_id"]},
            _tool_get_source,
        ),
        "get_document_outline": w(
            "get_document_outline",
            "Element-tree outline: titles/sections/tables in seq order (tool #4, wraps "
            "trialerror.retrieve.engine.get_document_outline).",
            {"type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"]},
            _tool_get_document_outline,
        ),
        "resolve_quote": w(
            "resolve_quote",
            "Quote text -> matching anchor(s) (doc, page, span), or NOT_FOUND (tool #5, wraps "
            "trialerror.retrieve.engine.resolve_quote). Exact quote_sha256 match first, falls back to "
            "a substring scan.",
            {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "source_id": {"type": "string"},
                    "doc_id": {"type": "string"},
                },
                "required": ["quote"],
            },
            _tool_resolve_quote,
        ),
        "similar": w(
            "similar",
            "Nearest chunks (or claims, v1 -- no claim vectors exist yet) to a given id (tool #6, "
            "wraps trialerror.retrieve.engine.similar).",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["chunk", "claim"]},
                    "k": {"type": "integer", "description": "default 10"},
                },
                "required": ["id"],
            },
            _tool_similar,
        ),
        "graph_neighbors": w(
            "graph_neighbors",
            "Entity/claim graph edges (tool #7, wraps trialerror.retrieve.engine.graph_neighbors). "
            "as_of = valid-time (event) axis; as_of_tx = transaction axis. No v0 writer populates "
            "entity/relation yet (design Section 11: full KG extraction is v1) -- schema-correct, "
            "typically empty until a future writer lands.",
            {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "as_of": {"type": "string"},
                    "as_of_tx": {"type": "string"},
                    "k": {"type": "integer", "description": "default 50"},
                },
                "required": ["entity_id"],
            },
            _tool_graph_neighbors,
        ),
        "corpus_stats": w(
            "corpus_stats",
            "Sources/docs/chunks/index-freshness summary (tool #8, wraps trialerror.retrieve.engine.corpus_stats).",
            {"type": "object", "properties": {}},
            _tool_corpus_stats,
        ),
        "memory_search": w(
            "memory_search",
            "Progressive-disclosure L0->L1->L2 memory search, or `id` for one item's full body, or "
            "`boot_bundle` for the M6 boot payload (tool #9, wraps trialerror.memory.api.{search_items,"
            "get_item,boot_bundle} -- the read-only pair M11's own contract names).",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "fetch ONE full item by id (skips filters)"},
                    "query": {"type": "string"},
                    "tier": {"type": "string", "enum": ["L0", "L1", "L2"]},
                    "kind": {"type": "string", "enum": ["rule", "fact", "lesson", "preference", "index"]},
                    "account_id": {"type": "string"},
                    "status": {"type": "string", "description": "default 'active'"},
                    "limit": {"type": "integer", "description": "default 50"},
                    "boot_bundle": {"type": "boolean"},
                    "token_budget": {"type": "integer"},
                },
            },
            _tool_memory_search,
        ),
        "list_requests": w(
            "list_requests",
            "Acquisition request queue by state (tool #10, wraps trialerror.retrieve.engine.list_requests "
            "over source.request_state -- M7's request queue).",
            {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": list(_REQUEST_STATES)},
                    "limit": {"type": "integer", "description": "default 100"},
                },
            },
            _tool_list_requests,
        ),
        "poll_job": w(
            "poll_job",
            "Job state/progress/heartbeat age -- the async-long-job contract (tool #11, wraps "
            "trialerror.jobs.ledger.get_job).",
            {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
            _tool_poll_job,
        ),
    }
    assert len(tools) == TOOL_COUNT, f"trialerror-knowledge must expose exactly {TOOL_COUNT} tools, got {len(tools)}"
    return tools


def build_server(*, program_root: Path, platform_root: Path | None = None) -> ToolServer:
    return ToolServer(
        name=SERVER_NAME,
        version=__version__,
        tools=build_tools(program_root=program_root, platform_root=platform_root),
        instructions=SERVER_INSTRUCTIONS,
    )


def run_server(
    *,
    program_root: Path | str,
    platform_root: Path | str | None = None,
    stdin=None,
    stdout=None,
    stderr=None,
) -> None:
    """Entry point for ``trialerror mcp knowledge``. Blocks serving stdio until
    stdin hits EOF."""
    server = build_server(
        program_root=Path(program_root),
        platform_root=Path(platform_root) if platform_root is not None else None,
    )
    serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
