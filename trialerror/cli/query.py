"""``trialerror query`` -- the retrieval CLI surface. Design Section 5.2 (query
row): "search, quote, similar, stats | same engine as MCP." Thin wrapper
over ``trialerror.retrieve.engine`` -- all retrieval logic lives there; this
module only parses argv and shapes the AgentEnvelope (same convention as
``trialerror/cli/memory.py``/``trialerror/cli/ingest.py``).

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/query.py`` and is auto-discovered by ``trialerror.cli.discover_groups``
-- adding it never touches ``trialerror/cli/__init__.py``.

``--unfenced`` (``search`` only): design Section 7's explicitly-named
non-agent escape hatch -- "full text stays on disk and remains available
to explicitly non-agent surfaces (``trialerror query search --unfenced``,
human-flagged and logged as an event)". The MCP ``search`` tool
(``trialerror/mcp/knowledge.py``) never exposes this flag.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.retrieve import engine
from trialerror.retrieve.errors import RetrievalError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "query"
HELP = "Retrieval: hybrid search, quote resolution, nearest-neighbor, corpus stats (same engine as the trialerror-knowledge MCP server)."


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root/
    # --platform-root the top-level parser resolved.
    p.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)"
    )
    p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_search = actions.add_parser("search", help="hybrid (fts+vector) search with citations")
    _add_program_root_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--k", type=int, default=engine.DEFAULT_K)
    p_search.add_argument("--mode", default="auto", choices=list(engine.SEARCH_MODES))
    p_search.add_argument("--source-id", dest="source_ids", action="append", default=None)
    p_search.add_argument("--kind", dest="kinds", action="append", default=None)
    p_search.add_argument("--license-tier", dest="license_tiers", action="append", default=None)
    p_search.add_argument("--year", dest="years", action="append", type=int, default=None)
    p_search.add_argument(
        "--unfenced", action="store_true",
        help="bypass the commercial_restricted serving fence (human-flagged, logged as an event; non-agent surface only)",
    )
    p_search.add_argument("--launch-id", default=None, dest="launch_id", help="attributes the --unfenced bypass event, if any")
    p_search.set_defaults(handler=_run_search)

    p_quote = actions.add_parser("quote", help="resolve quote text to its anchor(s) (doc, page, span) or NOT_FOUND")
    _add_program_root_arg(p_quote)
    p_quote.add_argument("text")
    p_quote.add_argument("--source-id", default=None, dest="source_id")
    p_quote.add_argument("--doc-id", default=None, dest="doc_id")
    p_quote.set_defaults(handler=_run_quote)

    p_similar = actions.add_parser("similar", help="nearest chunks (or claims, v1) to a given id")
    _add_program_root_arg(p_similar)
    p_similar.add_argument("--id", required=True, dest="ref_id")
    p_similar.add_argument("--kind", default="chunk", choices=["chunk", "claim"])
    p_similar.add_argument("--k", type=int, default=10)
    p_similar.set_defaults(handler=_run_similar)

    p_stats = actions.add_parser("stats", help="sources/docs/chunks/index-freshness summary")
    _add_program_root_arg(p_stats)
    p_stats.set_defaults(handler=_run_stats)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )
    return open_store(program_root, platform_root=args.platform_root), None


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "query", "no_action", "specify an action: search|quote|similar|stats",
        next_actions=[next_action(["trialerror", "query", "--help"], "list query actions")],
    )


def _error_code(exc: Exception) -> str:
    """The envelope code for a retrieval error: the class's own declared
    ``code`` where it has one (lane F-1 -- the operator-facing name is not
    always the class name), else the long-standing class-name default."""
    return getattr(exc, "code", None) or type(exc).__name__


def _search_warnings(result: dict) -> list[dict] | None:
    """The ``warnings`` block for a search that SUCCEEDED with a tier
    missing.

    A search that silently returns lexical-only results because this
    process cannot embed a query is the failure mode lane F-1 exists to
    remove: the results look normal, the recall is a fraction of what was
    asked for, and nothing in the envelope says so. ``tiers_used`` already
    reports what ran; this says what did not, why, which doctor check
    reports it, and which config table changes it."""
    reason = (result.get("stats") or {}).get("vector_skipped_reason")
    if not reason:
        return None
    return [
        {
            "code": engine.QUERY_EMBED_UNRUNNABLE_CODE,
            "message": (
                f"the vector tier was skipped: {reason}. These results are the full-text tier only "
                f"(tiers_used={result.get('tiers_used')})."
            ),
            "doctor_check": engine.QUERY_EMBED_DOCTOR_CHECK,
            "config_table": engine.QUERY_EMBED_TABLE,
        }
    ]


def _rerun_without_zero_terms(args: argparse.Namespace, result: dict) -> list:
    """The next action a zero-result search earns: the SAME search with the
    terms that match nothing dropped (lane FB-1 item F3).

    "Largest-coverage subset" is exactly the terms whose own candidate count
    is non-zero -- dropping a term can only widen an AND, so the biggest
    subset that can possibly match is all of them minus the ones that match
    nothing. Built from numbers the search already computed: this suggests a
    command, it does not run a second query.

    Every flag the caller gave is carried through unchanged, because a re-run
    that quietly widened the filters too would be answering a different
    question. That includes the two that are easiest to forget (fix-accept,
    V-2): ``--launch-id``, because a launch's declared slice becomes a forced
    ``doc_ids`` filter (``engine._effective_filters``) and the per-term counts
    were computed INSIDE that slice, so a re-run without it can return hits
    for the very term the action says matches nothing; and ``--unfenced``,
    because a caller who asked past the serving fence is asking a different
    question from one who did not. ``--program-root``/``--platform-root``
    travel too, so the suggestion resolves the same stores from the same CWD
    (the shape ``events tail`` already uses).

    The query goes LAST, behind a ``--`` separator (fix-accept, V-3): a
    surviving term may begin with ``-``, and argparse would read it as an
    option -- so item F10c's guarantee ("every emitted action parses") needs
    the flags first and the positional after the separator, not one token
    inserted into the old order.

    Returns ``[]`` -- no action at all -- when there is nothing runnable to
    suggest: no zero-count terms (the emptiness is not one word's fault),
    or every term counts zero (the corpus has none of this query, and
    "search for nothing" is not advice)."""
    stats = result.get("stats") or {}
    counts = stats.get("per_term_candidates") or {}
    zero_terms = stats.get("zero_result_terms") or []
    if not counts or not zero_terms:
        return []
    kept = [term for term, n in counts.items() if n > 0]
    if not kept:
        return []
    argv = ["trialerror", "query", "search"]
    if getattr(args, "program_root", None):
        argv += ["--program-root", str(args.program_root)]
    if getattr(args, "platform_root", None):
        argv += ["--platform-root", str(args.platform_root)]
    if args.k != engine.DEFAULT_K:
        argv += ["--k", str(args.k)]
    if args.mode != "auto":
        argv += ["--mode", args.mode]
    for source_id in args.source_ids or []:
        argv += ["--source-id", source_id]
    for kind in args.kinds or []:
        argv += ["--kind", kind]
    for tier in args.license_tiers or []:
        argv += ["--license-tier", tier]
    for year in args.years or []:
        argv += ["--year", str(year)]
    if getattr(args, "unfenced", False):
        argv += ["--unfenced"]
    if getattr(args, "launch_id", None):
        argv += ["--launch-id", str(args.launch_id)]
    # The query is positional and may start with "-": everything above is a
    # flag, this separator ends the flags, and the terms follow.
    argv += ["--", " ".join(kept)]
    dropped = ", ".join(zero_terms)
    return [
        next_action(
            argv,
            f"re-run without the term(s) no chunk matches under the filters in force ({dropped})",
        )
    ]


def _run_search(args: argparse.Namespace) -> dict:
    store, err = _open(args, "query.search")
    if err is not None:
        return err
    try:
        filters: dict = {}
        if args.source_ids:
            filters["source_ids"] = args.source_ids
        if args.kinds:
            filters["kind"] = args.kinds
        if args.license_tiers:
            filters["license_tier"] = args.license_tiers
        if args.years:
            filters["year"] = args.years
        result = engine.search(
            store, query=args.query, k=args.k, mode=args.mode, filters=filters or None,
            unfenced=args.unfenced, launch_id=args.launch_id,
        )
        actions = list(_rerun_without_zero_terms(args, result))
        if (result.get("stats") or {}).get("vector_skipped_reason"):
            actions.append(
                next_action(
                    engine.query_embed_next_action_argv(store.program_root),
                    "why the vector tier was skipped",
                )
            )
        return ok_envelope(
            "query.search",
            result=result,
            warnings=_search_warnings(result),
            next_actions=actions or None,
        )
    except RetrievalError as exc:
        return error_envelope(
            "query.search", _error_code(exc), str(exc),
            next_actions=[next_action(engine.query_embed_next_action_argv(store.program_root), "check the query-side embed backend")]
            if getattr(exc, "code", None) == engine.QUERY_EMBED_UNRUNNABLE_CODE
            else None,
        )
    finally:
        store.close()


def _run_quote(args: argparse.Namespace) -> dict:
    store, err = _open(args, "query.quote")
    if err is not None:
        return err
    try:
        result = engine.resolve_quote(store, args.text, source_id=args.source_id, doc_id=args.doc_id)
        if not result["found"]:
            return error_envelope("query.quote", "not_found", "no anchor matches the given quote text", details=result)
        return ok_envelope("query.quote", result=result)
    finally:
        store.close()


def _run_similar(args: argparse.Namespace) -> dict:
    store, err = _open(args, "query.similar")
    if err is not None:
        return err
    try:
        result = engine.similar(store, args.ref_id, kind=args.kind, k=args.k)
        return ok_envelope("query.similar", result=result)
    except RetrievalError as exc:
        return error_envelope("query.similar", _error_code(exc), str(exc))
    finally:
        store.close()


def _stats_next_actions(result: dict) -> list:
    """The repairs this corpus actually needs, and nothing else (FB-1 item
    F10c).

    Each one is conditional on a number in the same envelope, and each names
    the verb that exists for exactly that skew -- a full-text index that does
    not cover the chunks, or a vector index short of the embeddings a key
    has. An empty corpus gets the ingest verb; a corpus in good order gets
    no actions at all."""
    actions = []
    fulltext = result.get("fulltext_index") or {}
    # Only when the backend actually SERVING this corpus is the one whose
    # index is short. A program pinned to `fulltext_backend = "fts5"` reports
    # the tantivy index as missing by construction -- suggesting a rebuild
    # there would be advising an operator to build an index their own config
    # says not to use.
    if (
        result.get("chunks")
        and result.get("fulltext_backend") == "tantivy"
        and (fulltext.get("state") != "ready" or fulltext.get("chunks_missing_index"))
    ):
        actions.append(
            next_action(
                ["trialerror", "ingest", "reindex-fulltext"],
                "the full-text index this corpus is served from does not cover every chunk: rebuild "
                "it from the chunk table",
            )
        )
    if result.get("chunks_missing_fts"):
        actions.append(
            next_action(
                ["trialerror", "doctor", "--only", "fulltext_index_stale"],
                "chunks with no chunk_fts row: the check reports the skew and what repairs it",
            )
        )
    if any((result.get("chunks_missing_vec_by_model_key") or {}).values()):
        # Deliberately the doctor check and not `ingest reindex-vectors`:
        # that verb is XID-attributed and needs a booked launch id, and an
        # emitted action carrying a `<placeholder>` is not a command anyone
        # can run. The check names the key and the counts, and its own
        # message names the repair verb.
        actions.append(
            next_action(
                ["trialerror", "doctor", "--only", "vector_index_stale"],
                "some embedded chunks are missing from a vector index: the check names the key and "
                "the repair (`ingest reindex-vectors`, which needs a booked launch id)",
            )
        )
    return actions


def _run_stats(args: argparse.Namespace) -> dict:
    store, err = _open(args, "query.stats")
    if err is not None:
        return err
    try:
        result = engine.corpus_stats(store)
        return ok_envelope("query.stats", result=result, next_actions=_stats_next_actions(result))
    finally:
        store.close()
