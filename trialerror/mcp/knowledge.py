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
term_lookup             trialerror.lexicon.api.{find_term,get_term,
                         senses_for_term,evidence_for_sense}              (lane e, E3)
======================  ==========================================================

Every handler is a THIN wrapper (build brief's binding instruction): parse
the MCP ``arguments`` dict, call the landed subsystem function, shape the
result as a ``trialerror.util.envelope`` dict. No business logic lives here --
the hybrid pipeline, citation assembly, the F3 serving-path license fence,
and the untrusted-wrap all live in :mod:`trialerror.retrieve.engine` and its
sibling modules, shared verbatim with ``trialerror query search`` (``trialerror/cli/
query.py``) and (later) M9's verification pipelines.

**Tool #12, ``term_lookup`` (lane e, build step E3;
``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` Section 5's "MCP -- mcp/
knowledge.py gains a read-only term_lookup tool (_wrap pattern; fenced
excerpts)")** is the one exception to "wraps M8's retrieve.engine": it
wraps :mod:`trialerror.lexicon.api` instead, read-only exactly like the
other eleven. Its own excerpt-fencing is self-contained in this file
(:func:`_term_evidence_payload`) rather than routed through
``trialerror.retrieve.engine`` -- a term's evidence is not a chunk, so
there is no existing engine function shaped for it -- but it reuses
:mod:`trialerror.retrieve.fence`'s exact functions (:func:`citation_quote`,
:func:`is_fenced_license`) so the SAME <=20-word cap and the SAME "unknown
source license -> serve fenced" rule apply here as everywhere else the
fence is enforced (design Section 7).

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
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    "request queue, job polling, and read-only lexicon term lookups. Every search/get_chunk/"
    "similar result carries a non-null citation block; commercial_restricted sources are "
    "served fenced (<=20-word excerpt, fenced:true) -- never raw verbatim text (design "
    "Section 7), and the same fence applies to a term's anchor-backed evidence excerpts. "
    "See docs/DESIGN_v0.md Section 5.1/7 for the full contract."
)
#: Design Section 5.1 table: 11 tools, plus lane e's ``term_lookup`` (E3).
TOOL_COUNT = 12

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


def _wrap(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    fn,
    *,
    program_root: Path,
    platform_root: Path | None,
    launch_id: str | None = None,
    session_scoped: bool = False,
):
    """Bind one handler to a fresh :class:`~trialerror.stores.store.Store` per
    call (opened and closed exactly once per ``tools/call``, mirroring
    ``trialerror.mcp.ops``'s own ``_wrap``), turn every retrieval/store error
    into a structured envelope, and log the per-call event line.

    ``session_scoped`` handlers additionally receive the ``launch_id`` this
    SERVER PROCESS was started with. It is a constructor argument, not a
    tool argument, and that is the whole point: an agent cannot name its own
    launch, so it cannot name a different one to widen the slice scope the
    launch declares."""

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
                envelope = fn(arguments, store=store, launch_id=launch_id) if session_scoped else fn(arguments, store=store)
            except RetrievalError as exc:
                # lane F-1: a retrieval error may declare its own
                # operator-facing code (``query_embed_backend_unrunnable``);
                # the class name stays the default for every other one.
                envelope = error_envelope(name, getattr(exc, "code", None) or type(exc).__name__, str(exc))
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


def _tool_search(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
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
        # `launch_id` comes from the SESSION (the value this server process
        # was started with), never from `args` -- an agent that could name
        # its own launch could name a different one, and the slice scope
        # would then be a preference rather than a barrier.
        launch_id=launch_id,
        # `unfenced` is deliberately NEVER read from `args` -- see module docstring.
    )
    return ok_envelope("search", result=result)


# ---------------------------------------------------------------------------
# 2. get_chunk
# ---------------------------------------------------------------------------


def _tool_get_chunk(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
    # Session-derived, exactly as `search`'s is: an id-addressed read that
    # ignored the launch scope would be the way around every ranked surface
    # that honours it.
    result = engine.get_chunk(store, args["chunk_id"], launch_id=launch_id)
    return ok_envelope("get_chunk", result=result)


# ---------------------------------------------------------------------------
# 3. get_source
# ---------------------------------------------------------------------------


def _tool_get_source(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
    result = engine.get_source(store, args["source_id"], launch_id=launch_id)
    return ok_envelope("get_source", result=result)


# ---------------------------------------------------------------------------
# 4. get_document_outline
# ---------------------------------------------------------------------------


def _tool_get_document_outline(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
    result = engine.get_document_outline(store, args["doc_id"], launch_id=launch_id)
    return ok_envelope("get_document_outline", result=result)


# ---------------------------------------------------------------------------
# 5. resolve_quote
# ---------------------------------------------------------------------------


def _tool_resolve_quote(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
    result = engine.resolve_quote(
        store, args["quote"], source_id=args.get("source_id"), doc_id=args.get("doc_id"), launch_id=launch_id
    )
    if not result["found"]:
        return error_envelope("resolve_quote", "not_found", "no anchor matches the given quote text", details=result)
    return ok_envelope("resolve_quote", result=result)


# ---------------------------------------------------------------------------
# 6. similar
# ---------------------------------------------------------------------------


def _tool_similar(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
    result = engine.similar(
        store,
        args["id"],
        kind=args.get("kind", "chunk"),
        k=int(args.get("k", 10)),
        # Same session-derived scope `search` carries: the two retrieval
        # surfaces enforce one barrier or neither of them does.
        launch_id=launch_id,
    )
    return ok_envelope("similar", result=result)


# ---------------------------------------------------------------------------
# 7. graph_neighbors
# ---------------------------------------------------------------------------


def _tool_graph_neighbors(args: Mapping[str, Any], *, store: Store, launch_id: str | None = None) -> dict[str, Any]:
    result = engine.graph_neighbors(
        store, args["entity_id"], as_of=args.get("as_of"), as_of_tx=args.get("as_of_tx"),
        k=int(args.get("k", 50)), launch_id=launch_id,
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
# 12. term_lookup (lane e, E3 -- wraps trialerror.lexicon.api, not retrieve.engine)
# ---------------------------------------------------------------------------


#: How many ids one ``IN (...)`` clause carries in the grouped reads below --
#: the same ceiling ``trialerror.lexicon.scan`` uses, for the same reason
#: (SQLite's parameter limit is 999 on builds before 3.32, 32,766 after).
_ID_CHUNK = 500


def _rows_by_id(conn, sql: str, ids: Sequence[str], *, key: str) -> dict[str, dict[str, Any]]:
    """``{row[key]: row}`` for ``ids``, in one statement per chunk. ``sql``
    carries a single ``{marks}`` placeholder for the ``IN`` list."""
    out: dict[str, dict[str, Any]] = {}
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), _ID_CHUNK):
        chunk = unique[i : i + _ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(sql.format(marks=marks), chunk).fetchall():
            record = dict(row)
            out[record[key]] = record
    return out


def _term_evidence_context(store: Store, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The anchor and source rows every anchored evidence row in ``rows``
    needs, read in a bounded number of statements instead of two per row
    (backlog item (c)).

    ``_term_evidence_payload`` used to issue the anchor JOIN and then the
    source lookup for EVERY anchored row it shaped -- the same
    query-per-row shape the Lexicon panel was measured at 48.1 s for on the
    live store. Same two SELECTs, same JOIN, asked once for the whole set:

    * ``anchors`` maps ``anchor_id`` to the anchor JOIN document row. An
      anchor id the JOIN does not return -- no anchor row, or an anchor whose
      document is gone -- is simply absent, which is the ``None`` the
      per-row query returned for it, and the payload's "unknown source ->
      serve FENCED" branch reads it the same way.
    * ``licenses`` maps ``source_id`` to its ``license_tier``. Absent again
      means unresolvable, which fences.

    The fence therefore cannot come out looser than it did per row: every
    absent lookup lands on the same restrictive branch it landed on before.
    """
    anchor_ids = [
        r["anchor_id"]
        for r in rows
        if r["evidence_kind"] == "quote_anchor" and r.get("anchor_id") is not None
    ]
    if not anchor_ids:
        return {"anchors": {}, "licenses": {}}
    anchors = _rows_by_id(
        store.knowledge,
        "SELECT a.anchor_id, a.quote_text, a.page_number, d.source_id FROM quote_anchor a "
        "JOIN document d ON d.doc_id = a.doc_id WHERE a.anchor_id IN ({marks})",
        anchor_ids,
        key="anchor_id",
    )
    licenses = _rows_by_id(
        store.knowledge,
        "SELECT source_id, license_tier FROM source WHERE source_id IN ({marks})",
        [a["source_id"] for a in anchors.values() if a["source_id"]],
        key="source_id",
    )
    return {"anchors": anchors, "licenses": licenses}


def _term_evidence_payload(
    store: Store, row: Mapping[str, Any], *, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """One evidence row, shaped for an agent: fenced the same way every
    other agent-facing surface fences a verbatim excerpt (design Section 7).

    ``context`` is :func:`_term_evidence_context`'s two maps for the whole
    batch this row belongs to. Without it the two reads happen here, per row,
    which is the path a caller shaping a single row still takes.

    Only ``evidence_kind='quote_anchor'`` evidence ever carries an
    ``anchor_id`` (``trialerror.lexicon.api._coerce_evidence_spec`` sets the
    other three kinds' ``anchor_id`` to ``NULL`` by construction), so
    ``anchored`` is exactly that test. An anchored row's excerpt goes
    through :func:`trialerror.retrieve.fence.citation_quote` at the
    anchor's source's ``license_tier`` -- and a source row that cannot be
    resolved is served FENCED, not open, because "unknown" must fail toward
    the more restrictive reading, never the less. A non-anchored row's
    excerpt is the sense's own recorded description (a record's or an
    idea's own-words text) -- already not a verbatim quote, so it is never
    fenced."""
    from trialerror.retrieve.fence import citation_quote, is_fenced_license

    anchored = row["evidence_kind"] == "quote_anchor" and row.get("anchor_id") is not None
    entry: dict[str, Any] = {
        "evidence_id": row["evidence_id"],
        "evidence_kind": row["evidence_kind"],
        "source_key": row["source_key"],
        "cite_raw": row.get("cite_raw"),
        "anchored": anchored,
    }
    if not anchored:
        entry["excerpt"] = row.get("excerpt")
        entry["fenced"] = False
        return entry

    if context is None:
        anchor = store.knowledge.execute(
            "SELECT a.quote_text, a.page_number, d.source_id FROM quote_anchor a "
            "JOIN document d ON d.doc_id = a.doc_id WHERE a.anchor_id = ?",
            (row["anchor_id"],),
        ).fetchone()
        source = None
        if anchor is not None and anchor["source_id"]:
            source = store.knowledge.execute(
                "SELECT license_tier FROM source WHERE source_id = ?", (anchor["source_id"],)
            ).fetchone()
    else:
        anchor = context["anchors"].get(row["anchor_id"])
        source = context["licenses"].get(anchor["source_id"]) if anchor and anchor["source_id"] else None
    license_tier = source["license_tier"] if source is not None else None
    fenced = license_tier is None or is_fenced_license(license_tier)
    entry["page_number"] = anchor["page_number"] if anchor is not None else None
    entry["excerpt"] = citation_quote(anchor["quote_text"] if anchor is not None else None, fenced=fenced)
    entry["fenced"] = fenced
    return entry


def _live_evidence_by_sense(store: Store, sense_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """``{sense_id: [live evidence row, ...]}`` -- one statement per chunk of
    ids, standing in for one :func:`trialerror.lexicon.api.evidence_for_sense`
    call per sense.

    Same predicate and same order that function's default (non-retracted)
    branch uses -- ``retracted_ts IS NULL``, ``ORDER BY created_ts,
    evidence_id`` -- with ``sense_id`` leading the ORDER BY only to make the
    grouping contiguous. A sense with no live evidence maps to ``[]``, which
    is what the per-sense call returns for it."""
    out: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sense_ids}
    unique = list(dict.fromkeys(sense_ids))
    for i in range(0, len(unique), _ID_CHUNK):
        chunk = unique[i : i + _ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = store.knowledge.execute(
            f"SELECT * FROM term_sense_evidence WHERE sense_id IN ({marks}) "
            "AND retracted_ts IS NULL ORDER BY sense_id, created_ts, evidence_id",
            chunk,
        ).fetchall()
        for row in rows:
            record = dict(row)
            out[record["sense_id"]].append(record)
    return out


def _term_sense_payload(
    store: Store,
    sense: Mapping[str, Any],
    *,
    evidence: Sequence[Mapping[str, Any]] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``evidence``/``context`` are the batched reads
    :func:`_tool_term_lookup` takes for all of a term's senses at once; with
    neither, this shapes one sense the way it always did."""
    from trialerror.lexicon.api import evidence_for_sense, needs_review

    rows = evidence_for_sense(store, sense["sense_id"]) if evidence is None else list(evidence)
    return {
        "sense_id": sense["sense_id"],
        "gloss": sense["gloss"],
        "disambiguator": sense["disambiguator"],
        "status": sense["status"],
        "origin_kind": sense["origin_kind"],
        "needs_review": needs_review(sense),
        "review_after": sense["review_after"],
        "evidence": [_term_evidence_payload(store, row, context=context) for row in rows],
    }


def _tool_term_lookup(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    lemma = args.get("lemma")
    term_id = args.get("term_id")
    if not lemma and not term_id:
        return error_envelope("term_lookup", "bad_input", "term_lookup needs 'lemma' or 'term_id'")

    try:
        from trialerror.lexicon import api as lexicon_api
    except ImportError as exc:
        return error_envelope("term_lookup", "lexicon_unavailable", str(exc))

    try:
        term = lexicon_api.get_term(store, term_id) if term_id else lexicon_api.find_term(store, lemma)
    except sqlite3.OperationalError as exc:
        return error_envelope(
            "term_lookup", "not_initialized",
            f"the term store is not available on this program yet (knowledge.db predates schema v5): {exc}",
        )
    if term is None:
        return error_envelope(
            "term_lookup", "not_found",
            f"no term for {'term_id=' + repr(term_id) if term_id else 'lemma=' + repr(lemma)}",
        )

    statuses = None if args.get("include_all_senses") else ("current",)
    # Backlog item (c): FOUR statements for the whole sense tree, whatever the
    # term's sense and evidence counts -- the senses, their live evidence
    # grouped by sense, the anchors those rows name, and the sources those
    # anchors name. It used to be one `evidence_for_sense` per sense plus TWO
    # per anchored evidence row.
    sense_rows = lexicon_api.senses_for_term(store, term["term_id"], statuses=statuses)
    evidence_by_sense = _live_evidence_by_sense(store, [s["sense_id"] for s in sense_rows])
    context = _term_evidence_context(
        store, [row for rows in evidence_by_sense.values() for row in rows]
    )
    senses = [
        _term_sense_payload(store, s, evidence=evidence_by_sense[s["sense_id"]], context=context)
        for s in sense_rows
    ]
    aliases = [
        dict(r) for r in store.knowledge.execute(
            "SELECT alias, alias_norm, kind FROM term_alias WHERE term_id = ? ORDER BY created_ts, alias_id",
            (term["term_id"],),
        ).fetchall()
    ]
    conflicts_open = len([
        r for r in lexicon_api.relations_for_term(store, term["term_id"], statuses=("pending", "confirmed"))
        if r["verb"] == "conflicts_with"
    ])

    result = {
        "term": {
            "term_id": term["term_id"], "lemma": term["lemma"], "lemma_norm": term["lemma_norm"],
            "granularity": term["granularity"], "tags": json.loads(term["tags"]) if term.get("tags") else None,
            "status": term["status"], "preferred_sense_id": term.get("preferred_sense_id"),
            "merged_into": term.get("merged_into"),
        },
        "senses": senses,
        "aliases": aliases,
        "conflicts_open": conflicts_open,
    }
    return ok_envelope("term_lookup", result=result)


# ---------------------------------------------------------------------------
# server assembly
# ---------------------------------------------------------------------------


def build_tools(
    *, program_root: Path, platform_root: Path | None = None, launch_id: str | None = None
) -> dict[str, ToolSpec]:
    """Build the exact 11-tool registry (design Section 5.1), each bound to
    ``program_root``/``platform_root`` for the lifetime of one server
    process.

    ``launch_id`` is the launch this server process serves. When it is given
    and that launch declares a slice, every tool that returns corpus CONTENT
    is restricted to it by the engine itself: the two ranked surfaces
    (``search``, ``similar``) and the five addressed by id (``get_chunk``,
    ``get_source``, ``get_document_outline``, ``resolve_quote``,
    ``graph_neighbors``). The last five matter most, because they are the
    ones that never rank anything -- an id or a quote fragment learned
    anywhere reaches them directly, so a barrier they did not carry would be
    a barrier the ranked surfaces enforced against nobody. ``corpus_stats``,
    ``memory_search``, ``list_requests``, ``poll_job`` and ``term_lookup``
    are unscoped and stay so: none of them serves chunk text.

    No tool schema below exposes ``launch_id``: the value comes from how the
    server was STARTED, so an agent holding these tools cannot widen its own
    scope by asking."""
    w = lambda *a, **kw: _wrap(*a, **kw, program_root=program_root, platform_root=platform_root)  # noqa: E731
    ws = lambda *a, **kw: _wrap(  # noqa: E731
        *a, **kw, program_root=program_root, platform_root=platform_root,
        launch_id=launch_id, session_scoped=True,
    )

    tools = {
        "search": ws(
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
        "get_chunk": ws(
            "get_chunk",
            "Chunk text (fenced+untrusted-wrapped per source license tier) + element/page context "
            "+ anchors (tool #2, wraps trialerror.retrieve.engine.get_chunk).",
            {"type": "object", "properties": {"chunk_id": {"type": "string"}}, "required": ["chunk_id"]},
            _tool_get_chunk,
        ),
        "get_source": ws(
            "get_source",
            "Source record + license tier + document list (tool #3, wraps trialerror.retrieve.engine.get_source).",
            {"type": "object", "properties": {"source_id": {"type": "string"}}, "required": ["source_id"]},
            _tool_get_source,
        ),
        "get_document_outline": ws(
            "get_document_outline",
            "Element-tree outline: titles/sections/tables in seq order (tool #4, wraps "
            "trialerror.retrieve.engine.get_document_outline).",
            {"type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"]},
            _tool_get_document_outline,
        ),
        "resolve_quote": ws(
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
        "similar": ws(
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
        "graph_neighbors": ws(
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
        "term_lookup": w(
            "term_lookup",
            "Look up one lexicon term by lemma or term_id: its current senses (or every sense with "
            "include_all_senses), aliases, and whether it has an open cross-system conflict (tool "
            "#12, lane e E3, wraps trialerror.lexicon.api). Anchor-backed evidence excerpts are fenced "
            "per the anchor's source license_tier (<=20-word excerpt when fenced, design Section 7); "
            "an evidence row whose source cannot be resolved is served fenced, never open.",
            {
                "type": "object",
                "properties": {
                    "lemma": {"type": "string", "description": "resolved through term.lemma_norm then term_alias.alias_norm, following a merge to its canonical term"},
                    "term_id": {"type": "string", "description": "an exact term_id; takes precedence over lemma if both are given"},
                    "include_all_senses": {"type": "boolean", "description": "default false (current senses only); true also returns proposed/superseded/rejected/retired senses"},
                },
            },
            _tool_term_lookup,
        ),
    }
    assert len(tools) == TOOL_COUNT, f"trialerror-knowledge must expose exactly {TOOL_COUNT} tools, got {len(tools)}"
    return tools


def build_server(
    *, program_root: Path, platform_root: Path | None = None, launch_id: str | None = None
) -> ToolServer:
    return ToolServer(
        name=SERVER_NAME,
        version=__version__,
        tools=build_tools(program_root=program_root, platform_root=platform_root, launch_id=launch_id),
        instructions=SERVER_INSTRUCTIONS,
    )


def run_server(
    *,
    program_root: Path | str,
    platform_root: Path | str | None = None,
    launch_id: str | None = None,
    stdin=None,
    stdout=None,
    stderr=None,
) -> None:
    """Entry point for ``trialerror mcp knowledge``. Blocks serving stdio until
    stdin hits EOF. ``launch_id`` binds this process to one booked launch,
    which is what makes that launch's declared slice a scope the served
    agent cannot widen."""
    server = build_server(
        program_root=Path(program_root),
        platform_root=Path(platform_root) if platform_root is not None else None,
        launch_id=launch_id,
    )
    serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
