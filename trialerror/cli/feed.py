"""``trialerror feed`` — full-text agent voices: threads + posts. Design Section
5.2 CLI table: "feed | post, read, threads |". Authorship is derived by
``trialerror.events.post_feed`` from ``--launch-id`` (never a free-text
``--author`` flag — none exists) or, when omitted, from the open session;
see ``trialerror/events/api.py`` for the binding contract this shell delegates
to entirely.
"""

from __future__ import annotations

import argparse

from trialerror.events.api import create_thread, get_thread_posts, list_threads, post_feed
from trialerror.events.cli_support import ProgramRootNotFoundError, open_program_store, program_root_argument
from trialerror.stores.errors import StoreError
from trialerror.util.envelope import error_envelope, ok_envelope

GROUP_NAME = "feed"
HELP = "Full-text agent voices: threads + posts (author derived by the API, never caller-settable)."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_post = actions.add_parser("post", help="post full text into a thread (or open one with --new-thread)")
    program_root_argument(p_post)
    p_post.add_argument("--body", required=True, help="the full post text (never a summary)")
    p_post.add_argument("--thread-id", default=None, help="post into an existing thread")
    p_post.add_argument(
        "--new-thread",
        default=None,
        metavar="TITLE",
        help="open a new thread with this title instead of --thread-id "
        "(requires --launch-id: thread.created_by_launch is NOT NULL)",
    )
    p_post.add_argument(
        "--launch-id",
        default=None,
        help="the caller's OWN launch_id (never another agent's) -- omit to post as the orchestrator",
    )
    p_post.add_argument(
        "--session-id", default=None, help="orchestrator posts only: defaults to the currently open session"
    )
    p_post.add_argument("--in-reply-to", default=None)
    p_post.set_defaults(handler=run_post)

    p_threads = actions.add_parser("threads", help="list threads, newest first")
    program_root_argument(p_threads)
    p_threads.add_argument("--limit", type=int, default=50)
    p_threads.set_defaults(handler=run_threads)

    p_read = actions.add_parser("read", help="read the full-text posts in one thread, oldest first")
    program_root_argument(p_read)
    p_read.add_argument("--thread-id", required=True)
    p_read.set_defaults(handler=run_read)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(GROUP_NAME, "no_action", "specify one of: post, threads, read")


def run_post(args: argparse.Namespace) -> dict:
    if not args.thread_id and not args.new_thread:
        return error_envelope("feed post", "missing_thread", "give --thread-id or --new-thread")
    if args.thread_id and args.new_thread:
        return error_envelope("feed post", "conflicting_thread_args", "give exactly one of --thread-id / --new-thread")
    if args.new_thread and not args.launch_id:
        return error_envelope(
            "feed post",
            "new_thread_needs_launch",
            "opening a new thread requires --launch-id (thread.created_by_launch is NOT NULL)",
        )

    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("feed post", "program_root_not_found", str(exc))

    try:
        thread_id = args.thread_id
        if args.new_thread:
            thread = create_thread(store, title=args.new_thread, launch_id=args.launch_id)
            thread_id = thread["thread_id"]
        post = post_feed(
            store,
            thread_id=thread_id,
            body=args.body,
            launch_id=args.launch_id,
            session_id=args.session_id,
            in_reply_to=args.in_reply_to,
        )
    except StoreError as exc:
        return error_envelope("feed post", "post_refused", str(exc))
    finally:
        store.close()

    return ok_envelope(
        "feed post",
        result={"post_id": post["post_id"], "thread_id": thread_id, "author": post["author"], "ts": post["ts"]},
    )


def run_threads(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("feed threads", "program_root_not_found", str(exc))
    try:
        rows = list_threads(store, limit=args.limit)
    finally:
        store.close()
    return ok_envelope("feed threads", result={"threads": rows, "count": len(rows)})


def run_read(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("feed read", "program_root_not_found", str(exc))
    try:
        rows = get_thread_posts(store, thread_id=args.thread_id)
    finally:
        store.close()
    return ok_envelope("feed read", result={"thread_id": args.thread_id, "posts": rows, "count": len(rows)})
