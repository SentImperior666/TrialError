"""``trialerror events`` — type-keyed event append/tail/export. Design Section
5.2 CLI table: "events | append, tail, export | export renders per-
workpackage/session jsonl views." Thin argv/envelope shell over
``trialerror.events.api`` (design Section 5.2's own registration rule: "each
CLI group lives in its own module ``trialerror/cli/<group>.py``, auto-discovered
at load — no implementation lane ever edits a shared ``cli/__init__.py``");
this file changes nothing outside itself to register.
"""

from __future__ import annotations

import argparse
import json

from trialerror.events.api import append_event, export_jsonl, tail_events
from trialerror.events.cli_support import ProgramRootNotFoundError, open_program_store, program_root_argument
from trialerror.stores.errors import StoreError
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "events"
HELP = "Type-keyed event append/tail/export (trialerror.events)."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_append = actions.add_parser("append", help="append one type-keyed event")
    program_root_argument(p_append)
    p_append.add_argument("--type", required=True, dest="event_type", help="the event's type key")
    p_append.add_argument("--payload", required=True, help="JSON object/array/string (NOT NULL)")
    p_append.add_argument("--session-id", default=None)
    p_append.add_argument("--launch-id", default=None)
    p_append.add_argument("--workpackage", default=None)
    p_append.set_defaults(handler=run_append)

    p_tail = actions.add_parser("tail", help="show the most recent matching events, oldest-first")
    program_root_argument(p_tail)
    p_tail.add_argument("--workpackage", default=None)
    p_tail.add_argument("--session-id", default=None)
    p_tail.add_argument("--type", default=None, dest="event_type")
    p_tail.add_argument("--limit", type=int, default=20)
    p_tail.set_defaults(handler=run_tail)

    p_export = actions.add_parser("export", help="render matching events as jsonl (byte-stable)")
    program_root_argument(p_export)
    p_export.add_argument("--out", required=True, help="output file path (or directory with --split-by-workpackage)")
    p_export.add_argument("--workpackage", default=None)
    p_export.add_argument("--session-id", default=None)
    p_export.add_argument("--type", default=None, dest="event_type")
    p_export.add_argument(
        "--split-by-workpackage",
        action="store_true",
        help="write one <workpackage>.jsonl file per distinct workpackage into --out (a directory); "
        "only applies with no --workpackage/--session-id filter",
    )
    p_export.set_defaults(handler=run_export)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(GROUP_NAME, "no_action", "specify one of: append, tail, export")


def run_append(args: argparse.Namespace) -> dict:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        return error_envelope("events append", "bad_payload_json", f"--payload is not valid JSON: {exc}")

    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("events append", "program_root_not_found", str(exc))

    try:
        row = append_event(
            store,
            event_type=args.event_type,
            payload=payload,
            session_id=args.session_id,
            launch_id=args.launch_id,
            workpackage=args.workpackage,
        )
    except StoreError as exc:
        return error_envelope("events append", "append_refused", str(exc))
    finally:
        store.close()

    return ok_envelope(
        "events append",
        result={"event_id": row["event_id"], "ts": row["ts"], "redactions": row["redactions"]},
    )


def _tail_argv(args: argparse.Namespace, *, limit: int, drop_type: bool = False) -> list[str]:
    """One ``events tail`` argv, carrying forward exactly the filters this
    call was given (FB-1 item F10c: a suggested command must parse today AND
    mean the same thing as the one that produced it)."""
    argv = ["trialerror", "events", "tail", "--limit", str(limit)]
    if getattr(args, "program_root", None):
        argv += ["--program-root", str(args.program_root)]
    if args.workpackage:
        argv += ["--workpackage", args.workpackage]
    if args.session_id:
        argv += ["--session-id", args.session_id]
    if args.event_type and not drop_type:
        argv += ["--type", args.event_type]
    return argv


def _tail_next_actions(args: argparse.Namespace, rows: list) -> list:
    """State-conditional, never decorative.

    A tail that filled its window is the one case where "there are 20
    events" and "there are at least 20 events" are different facts, and the
    caller cannot tell which it got. A tail that matched NOTHING under a
    ``--type`` filter is the other: the type may be spelled differently in
    this ledger, and the same query without it is the cheapest way to find
    out. Every other outcome gets no action at all -- a next action nobody
    needs is noise the next reader learns to skip."""
    actions = []
    if len(rows) >= args.limit > 0:
        actions.append(
            next_action(
                _tail_argv(args, limit=args.limit * 2),
                f"the window was full at {args.limit}: there may be older matching events",
            )
        )
    elif not rows and args.event_type:
        actions.append(
            next_action(
                _tail_argv(args, limit=args.limit, drop_type=True),
                f"nothing of type {args.event_type!r} matched: the same tail without the type filter",
            )
        )
    return actions


def run_tail(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("events tail", "program_root_not_found", str(exc))
    try:
        rows = tail_events(
            store,
            workpackage=args.workpackage,
            session_id=args.session_id,
            event_type=args.event_type,
            limit=args.limit,
        )
    finally:
        store.close()
    return ok_envelope(
        "events tail",
        result={"events": rows, "count": len(rows)},
        next_actions=_tail_next_actions(args, rows),
    )


def run_export(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("events export", "program_root_not_found", str(exc))
    try:
        result = export_jsonl(
            store,
            out_path=args.out,
            workpackage=args.workpackage,
            session_id=args.session_id,
            event_type=args.event_type,
            split_by_workpackage=args.split_by_workpackage,
        )
    finally:
        store.close()
    return ok_envelope("events export", result=result)
