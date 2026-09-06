"""``trialerror feed`` — full-text agent voices: threads + posts. Design Section
5.2 CLI table: "feed | post, read, threads |". Authorship is derived by
``trialerror.events.post_feed`` from ``--launch-id`` (never a free-text
``--author`` flag — none exists) or, when omitted, from the open session;
see ``trialerror/events/api.py`` for the binding contract this shell delegates
to entirely.

``feed translate`` / ``feed translations`` extend the same shell over
:mod:`trialerror.feed_translate` (``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md``
Section 5 step 4). ``translate`` ENQUEUES a job and returns — it never
translates inline, and this process never calls an LLM (the house
LLM-judgment boundary, stated in ``trialerror/cli/summarize.py``'s own
docstring and observed identically here). ``--body`` / ``--judgments-file``
are how a caller who ALREADY authored the plain text out-of-band hands it
to the job; without them the job asks its configured backend, and parks a
PENDING envelope when that backend has nothing to give.

``--claim-decomposition-file`` / ``--claim-judgments-file`` (FT-1, fix
pass) turn on the gate's JUDGED (meaning-level) tier for this run — the
same ``{pair_id: judgment}`` table shape ``trialerror verify faithfulness``
already documents for its own ``--decomposition-file``/``--judgments-file``.
Sentence pair ids (``<post_id>::S-<n>``) are deterministic from the
candidate text via ``trialerror.feed_translate.style.split_sentences`` —
the same text given through ``--body``/``--judgments-file`` — so an agent
authoring a translation can compute them without a job round-trip; claim
pair ids (``<post_id>::S-<n>::CLM-<m>``) are then whatever the agent's own
``--claim-decomposition-file`` names, one entry per atomic claim it
chooses to break each sentence into. Both files are authored together, in
one pass, by whoever is already authoring the translation text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.events.api import create_thread, get_thread_posts, list_threads, post_feed
from trialerror.events.cli_support import ProgramRootNotFoundError, open_program_store, program_root_argument
from trialerror.feed_translate.api import (
    CURRENT_TRANSLATOR_VERSION,
    find_untranslated_posts,
    get_translation,
    list_translations,
)
from trialerror.feed_translate.style import DEFAULT_STYLE_MODE
from trialerror.jobs.ledger import enqueue as enqueue_job
from trialerror.stores.errors import StoreError
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "feed"
HELP = "Full-text agent voices: threads + posts, plus plain-English translations of them."


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

    p_translate = actions.add_parser(
        "translate",
        help="enqueue a plain-English translation job for one post, one thread, or every untranslated post",
    )
    program_root_argument(p_translate)
    p_translate.add_argument("--post-id", default=None, help="translate exactly this post")
    p_translate.add_argument("--thread-id", default=None, help="translate every untranslated post in this thread")
    p_translate.add_argument(
        "--pending", action="store_true",
        help="translate every untranslated post program-wide (the backlog sweep)",
    )
    p_translate.add_argument(
        "--body", default=None,
        help="plain-English text you already authored for --post-id (carried into the job payload; still gated)",
    )
    p_translate.add_argument(
        "--judgments-file", default=None, dest="judgments_file",
        help='JSON {post_id: plain_text} of translations authored out-of-band (still gated)',
    )
    p_translate.add_argument(
        "--claim-decomposition-file", default=None, dest="claim_decomposition_file",
        help='JSON {pair_id: {claims: [...]}} -- turns on the gate\'s judged (meaning-level) tier; '
        "pair ids are <post_id>::S-<n>, one per sentence of the candidate translation",
    )
    p_translate.add_argument(
        "--claim-judgments-file", default=None, dest="claim_judgments_file",
        help='JSON {claim_pair_id: {label, note?}} -- one entry per claim in --claim-decomposition-file '
        "(pair_id <post_id>::S-<n>::CLM-<m>); required alongside --claim-decomposition-file, not on its own",
    )
    p_translate.add_argument(
        "--by-launch", default=None, dest="by_launch",
        help="your OWN launch_id, recorded as the translation's created_by_launch "
        "(omit to translate under the orchestrator's no-launch identity)",
    )
    p_translate.add_argument("--style-mode", choices=["flavored", "strict"], default=DEFAULT_STYLE_MODE, dest="style_mode")
    p_translate.add_argument("--translator-version", default=CURRENT_TRANSLATOR_VERSION, dest="translator_version")
    p_translate.set_defaults(handler=run_translate)

    p_translations = actions.add_parser(
        "translations", help="show stored translations (one post's current one, or a filtered list)"
    )
    program_root_argument(p_translations)
    p_translations.add_argument("--post-id", default=None, help="show only this post's CURRENT translation")
    p_translations.add_argument("--status", choices=["current", "superseded"], default=None)
    p_translations.add_argument(
        "--gate-status", choices=["pass", "fail", "ungated"], default=None, dest="gate_status",
        help="filter by the faithfulness gate's verdict ('fail' = withheld from the dashboard)",
    )
    p_translations.add_argument("--limit", type=int, default=50)
    p_translations.set_defaults(handler=run_translations)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(GROUP_NAME, "no_action", "specify one of: post, threads, read, translate, translations")


def run_post(args: argparse.Namespace) -> dict:
    if not args.thread_id and not args.new_thread:
        return error_envelope("feed post", "missing_thread", "give --thread-id or --new-thread")
    if args.thread_id and args.new_thread:
        return error_envelope("feed post", "conflicting_thread_args", "give exactly one of --thread-id / --new-thread")
    # `--new-thread` used to also require `--launch-id`, because
    # thread.created_by_launch was NOT NULL. ops v8 made it nullable and gave
    # `thread` the same derived `created_by` a post already carries, so the
    # orchestrator can now OPEN a thread under its open session exactly as it
    # could always post into one. `create_thread` refuses with its own named
    # error when there is no open session either -- one refusal, in the module
    # that owns the rule, rather than two that can disagree.

    try:
        store = open_program_store(args.program_root)
    except ProgramRootNotFoundError as exc:
        return error_envelope("feed post", "program_root_not_found", str(exc))

    try:
        thread_id = args.thread_id
        if args.new_thread:
            thread = create_thread(
                store, title=args.new_thread, launch_id=args.launch_id, session_id=args.session_id
            )
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


def _load_judgments_file(path: str | None) -> dict[str, str] | None:
    """``{post_id: plain_text}`` — the same ``--judgments-file`` contract
    ``trialerror summarize run`` and ``trialerror verify citecheck`` already
    document, keyed by ``post_id`` instead of ``subject_id``."""
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_translate(args: argparse.Namespace) -> dict:
    """Enqueue one ``feed_translate`` job. Never translates inline (the
    design's option C: lazy, cached, ledger-backed) and never calls a
    model from this process.

    Exactly one target selector is required: ``--post-id``,
    ``--thread-id``, or ``--pending``. ``--body`` is only meaningful with
    ``--post-id`` (it IS that post's translation); a whole-thread or
    backlog run supplies its answers through ``--judgments-file`` instead.
    """
    selectors = [bool(args.post_id), bool(args.thread_id), bool(args.pending)]
    if sum(selectors) != 1:
        return error_envelope(
            "feed translate", "target_required",
            "give exactly one of --post-id, --thread-id, --pending",
        )
    if args.body and not args.post_id:
        return error_envelope(
            "feed translate", "body_needs_post_id",
            "--body is one post's translation -- pair it with --post-id, or use --judgments-file for a batch",
        )
    have_decomp = bool(args.claim_decomposition_file)
    have_claim_judgments = bool(args.claim_judgments_file)
    if have_decomp != have_claim_judgments:
        return error_envelope(
            "feed translate", "claim_files_incomplete",
            "--claim-decomposition-file and --claim-judgments-file turn on the judged tier together -- "
            "give both, or neither",
        )

    try:
        store = open_program_store(getattr(args, "program_root", None))
    except ProgramRootNotFoundError as exc:
        return error_envelope("feed translate", "program_root_not_found", str(exc))

    try:
        judgments = _load_judgments_file(args.judgments_file) or {}
        if args.body:
            judgments[args.post_id] = args.body
        claim_decomposition = _load_judgments_file(args.claim_decomposition_file)
        claim_judgments = _load_judgments_file(args.claim_judgments_file)

        payload = {
            "handler": "feed_translate",
            "translator_version": args.translator_version,
            "style_mode": args.style_mode,
            "created_by_launch": args.by_launch,
            "judgments": judgments,
        }
        if claim_decomposition is not None:
            payload["claim_decomposition"] = claim_decomposition
        if claim_judgments is not None:
            payload["claim_judgments"] = claim_judgments
        if args.post_id:
            payload["post_ids"] = [args.post_id]
            targets = 1
        elif args.thread_id:
            payload["thread_id"] = args.thread_id
            targets = len(
                find_untranslated_posts(
                    store, thread_id=args.thread_id, translator_version=args.translator_version
                )
            )
        else:
            targets = len(find_untranslated_posts(store, translator_version=args.translator_version))

        job = enqueue_job(store, kind="custom", payload=payload)
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("feed translate", "judgments_file_error", str(exc))
    except StoreError as exc:
        return error_envelope("feed translate", "enqueue_refused", str(exc))
    finally:
        store.close()

    return ok_envelope(
        "feed translate",
        result={"status": "enqueued", "job": job, "targets": targets},
        next_actions=[
            next_action(
                ["trialerror", "jobs", "start-worker", "--job-id", job["job_id"], "--mode", "once"],
                "run the enqueued translation job",
            ),
            next_action(
                ["trialerror", "feed", "translations", "--gate-status", "fail"],
                "list translations the faithfulness gate withheld",
            ),
        ],
    )


def run_translations(args: argparse.Namespace) -> dict:
    try:
        store = open_program_store(getattr(args, "program_root", None))
    except ProgramRootNotFoundError as exc:
        return error_envelope("feed translations", "program_root_not_found", str(exc))
    try:
        if args.post_id and not (args.status or args.gate_status):
            row = get_translation(store, post_id=args.post_id)
            if row is None:
                return error_envelope(
                    "feed translations", "not_found", f"no current translation for post {args.post_id!r}"
                )
            return ok_envelope("feed translations", result={"translation": row})
        rows = list_translations(
            store, post_id=args.post_id, status=args.status, gate_status=args.gate_status, limit=args.limit
        )
    finally:
        store.close()
    return ok_envelope("feed translations", result={"translations": rows, "count": len(rows)})
