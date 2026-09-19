"""``trialerror inbox`` — the user's inbox. Design Section 4.2: "The user's
write path is ``trialerror inbox post`` (the one API-backed inbox writer — no
hand-appended files, per P2)." Design Section 5.2 CLI table: "inbox |
post, read | ``inbox post`` = the user's one API-backed write path (P2)."
"""

from __future__ import annotations

import argparse

from trialerror.events.api import post_inbox, read_inbox
from trialerror.events.cli_support import ProgramRootNotFoundError, open_program_store, program_root_argument
from trialerror.stores.errors import StoreError
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "inbox"
HELP = "The user's inbox: `inbox post` (the one API-backed write path) + `inbox read`."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_post = actions.add_parser("post", help="the user's one API-backed inbox write path")
    program_root_argument(p_post)
    p_post.add_argument("--body", required=True)
    p_post.set_defaults(handler=run_post)

    p_read = actions.add_parser("read", help="unread inbox items (marks them read unless --no-mark-read)")
    program_root_argument(p_read)
    p_read.add_argument("--session-id", default=None, help="stamped on read_by_session when marking items read")
    p_read.add_argument("--no-mark-read", action="store_true", help="peek without marking items read")
    p_read.set_defaults(handler=run_read)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(GROUP_NAME, "no_action", "specify one of: post, read")


def run_post(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("inbox post", "program_root_not_found", str(exc))
    try:
        row = post_inbox(store, body=args.body)
    except StoreError as exc:
        return error_envelope("inbox post", "post_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("inbox post", result={"item_id": row["item_id"], "ts": row["ts"]})


def run_read(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("inbox read", "program_root_not_found", str(exc))
    try:
        items = read_inbox(store, session_id=args.session_id, mark_read=not args.no_mark_read)
    finally:
        store.close()
    # FB-1 item F10c: exactly one state earns an action here. `--no-mark-read`
    # PEEKED at items that are still unread, and unread items are what
    # `session close` refuses on -- so the caller is one command away from a
    # refusal it has just been shown the cause of. Items that were marked
    # read need nothing, and neither does an empty inbox.
    actions = []
    if items and args.no_mark_read:
        argv = ["trialerror", "inbox", "read"]
        if getattr(args, "program_root", None):
            argv += ["--program-root", str(args.program_root)]
        if args.session_id:
            argv += ["--session-id", args.session_id]
        actions.append(
            next_action(
                argv,
                f"{len(items)} item(s) are still unread after this peek -- `session close` refuses "
                "while any are",
            )
        )
    return ok_envelope(
        "inbox read", result={"items": items, "count": len(items)}, next_actions=actions
    )
