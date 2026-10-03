"""``trialerror units`` -- the CLI surface over :mod:`trialerror.units` (design
Section 2.3). Every ``unit``/``unit_msg`` row lives in ``platform.db``
(machine-wide, like ``quota_snapshot`` -- design Section 0: a unit's cost is
a fact about this host's own Claude Code install, not about any one
program), so this group opens ONLY the platform connection -- never a
program's ops/knowledge/jobs stores, which ``trialerror.stores.store.open_store``
would otherwise create on disk for no reason this group has any use for.

Registration rule (design Section 5.2 / ``trialerror/cli/__init__.py``): this
module lives at ``trialerror/cli/units.py`` and is auto-discovered; adding it
never touches ``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from trialerror.stores import paths as store_paths
from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform as platform_schema
from trialerror.stores.store import Store
from trialerror.units.scan import scan as run_scan
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "units"
HELP = "Unit cost from Claude Code transcripts: scan, list, show, cost (design F1)."

_USAGE_COLUMNS = ("usage_input", "usage_cache_write", "usage_cache_read", "usage_output")
_COST_BY_CHOICES = ("session", "project", "agent_type", "model", "day")


def _add_platform_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--platform-root", default=argparse.SUPPRESS,
        help="override the platform root (default: TRIALERROR_PLATFORM_ROOT or ~/.trialerror)",
    )


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_scan = actions.add_parser("scan", help="scan a host's Claude Code transcripts into platform.db")
    _add_platform_root_arg(p_scan)
    p_scan.add_argument("--projects", required=True, help="the projects root to scan (e.g. ~/.claude/projects)")
    p_scan.add_argument("--host", required=True, help="a label for this host (e.g. dev, sandbox)")
    p_scan.add_argument("--statusline-dir", default=None, help="the statusLine quota dir holding sessions.json")
    p_scan.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    p_scan.set_defaults(handler=_run_scan_cmd)

    p_list = actions.add_parser("list", help="list scanned units")
    _add_platform_root_arg(p_list)
    p_list.add_argument("--host", default=None)
    p_list.add_argument("--session", default=None, dest="session_id")
    p_list.add_argument("--source", default=None, dest="usage_source")
    p_list.add_argument("--since", default=None, help="ISO timestamp; filters on scanned_ts")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(handler=_run_list)

    p_show = actions.add_parser("show", help="show one unit row by its unit_key")
    _add_platform_root_arg(p_show)
    p_show.add_argument("unit_key")
    p_show.set_defaults(handler=_run_show)

    p_cost = actions.add_parser("cost", help="sum token usage, grouped by session/project/agent_type/model/day")
    _add_platform_root_arg(p_cost)
    p_cost.add_argument("--host", default=None)
    p_cost.add_argument("--by", default="project", choices=_COST_BY_CHOICES, dest="group_by")
    p_cost.add_argument("--since", default=None, help="ISO timestamp; filters on first_ts")
    p_cost.add_argument("--until", default=None, help="ISO timestamp; filters on first_ts")
    p_cost.set_defaults(handler=_run_cost)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "units", "no_action", "specify an action: scan|list|show|cost",
        next_actions=[next_action(["trialerror", "units", "--help"], "list units actions")],
    )


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _open_platform_only(args: argparse.Namespace) -> Store:
    """A :class:`Store` whose ``platform`` connection is the real (or
    scratch-test) platform.db, migrated, and whose ``ops``/``knowledge``/
    ``jobs`` connections are throwaway in-memory databases -- ``unit``/
    ``unit_msg``/``probe_run`` are the only tables this group ever touches
    (all three route to ``platform`` via ``trialerror.stores.store.TABLE_DB``),
    so nothing under a program root is ever created just to satisfy
    ``open_store``'s four-connection contract."""
    platform_root = Path(getattr(args, "platform_root", None)) if getattr(args, "platform_root", None) else store_paths.platform_root()
    platform_conn = connect(store_paths.platform_db_path(root=platform_root))
    apply_migrations(platform_conn, platform_schema.MIGRATIONS)
    return Store(
        platform=platform_conn,
        ops=_memory_conn(),
        knowledge=_memory_conn(),
        jobs=_memory_conn(),
        program_root=Path("."),
        platform_root=platform_root,
    )


def _run_scan_cmd(args: argparse.Namespace) -> dict:
    store = _open_platform_only(args)
    try:
        report = run_scan(
            args.projects,
            host=args.host,
            store=store,
            statusline_dir=args.statusline_dir,
            dry_run=args.dry_run,
        )
        return ok_envelope("units.scan", result=report.to_dict())
    finally:
        store.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("models"):
        try:
            d["models"] = json.loads(d["models"])
        except (TypeError, ValueError):
            pass
    if d.get("statusline_cost"):
        try:
            d["statusline_cost"] = json.loads(d["statusline_cost"])
        except (TypeError, ValueError):
            pass
    return d


def _run_list(args: argparse.Namespace) -> dict:
    store = _open_platform_only(args)
    try:
        clauses, params = [], []
        if args.host:
            clauses.append("host = ?")
            params.append(args.host)
        if args.session_id:
            clauses.append("session_id = ?")
            params.append(args.session_id)
        if args.usage_source:
            clauses.append("usage_source = ?")
            params.append(args.usage_source)
        if args.since:
            clauses.append("scanned_ts >= ?")
            params.append(args.since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(args.limit)
        rows = store.platform.execute(
            f"SELECT * FROM unit {where} ORDER BY scanned_ts DESC LIMIT ?", params
        ).fetchall()
        return ok_envelope("units.list", result={"units": [_row_to_dict(r) for r in rows], "count": len(rows)})
    finally:
        store.close()


def _run_show(args: argparse.Namespace) -> dict:
    store = _open_platform_only(args)
    try:
        row = store.platform.execute("SELECT * FROM unit WHERE unit_key = ?", (args.unit_key,)).fetchone()
        if row is None:
            return error_envelope("units.show", "not_found", f"no unit with unit_key={args.unit_key!r}")
        return ok_envelope("units.show", result=_row_to_dict(row))
    finally:
        store.close()


def _run_cost(args: argparse.Namespace) -> dict:
    store = _open_platform_only(args)
    try:
        clauses, params = [], []
        if args.host:
            clauses.append("host = ?")
            params.append(args.host)
        if args.since:
            clauses.append("first_ts >= ?")
            params.append(args.since)
        if args.until:
            clauses.append("first_ts <= ?")
            params.append(args.until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        group_col = {
            "session": "session_id",
            "project": "project_slug",
            "agent_type": "agent_type",
            "model": "models",
            "day": "substr(first_ts, 1, 10)",
        }[args.group_by]

        sums_sql = ", ".join(f"SUM({c}) AS {c}" for c in _USAGE_COLUMNS)
        rows = store.platform.execute(
            f"SELECT {group_col} AS key, COUNT(*) AS n_units, {sums_sql} FROM unit {where} "
            f"GROUP BY {group_col} ORDER BY key",
            params,
        ).fetchall()
        by_group = [dict(r) for r in rows]

        source_rows = store.platform.execute(
            f"SELECT usage_source, COUNT(*) AS n FROM unit {where} GROUP BY usage_source", params
        ).fetchall()
        by_source = {r["usage_source"]: r["n"] for r in source_rows}
        no_transcript = by_source.get("none", 0) + by_source.get("statusline_total", 0)

        totals = store.platform.execute(f"SELECT {sums_sql} FROM unit {where}", params).fetchone()

        footer = (
            f"{no_transcript} unit(s) have no transcript on disk (usage_source in "
            "{'none','statusline_total'}); their tokens are NOT included in any sum above."
            if no_transcript
            else "every unit in this window has a transcript-derived usage figure."
        )
        result = {
            "group_by": args.group_by,
            "by_group": by_group,
            "totals": dict(totals) if totals else {c: 0 for c in _USAGE_COLUMNS},
            "units_by_source": by_source,
            "footer": footer,
        }
        return ok_envelope("units.cost", result=result)
    finally:
        store.close()
