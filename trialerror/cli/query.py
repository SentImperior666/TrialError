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
        return ok_envelope("query.search", result=result)
    except RetrievalError as exc:
        return error_envelope("query.search", type(exc).__name__, str(exc))
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
        return error_envelope("query.similar", type(exc).__name__, str(exc))
    finally:
        store.close()


def _run_stats(args: argparse.Namespace) -> dict:
    store, err = _open(args, "query.stats")
    if err is not None:
        return err
    try:
        result = engine.corpus_stats(store)
        return ok_envelope("query.stats", result=result)
    finally:
        store.close()
