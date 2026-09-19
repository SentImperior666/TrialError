"""``trialerror summarize`` -- the L1 summary tier's CLI surface. Build brief:
"CLI: trialerror summarize {run,show,list}." Thin wrapper over
``trialerror.summarize.api`` -- all logic lives there; this module only parses
argv and shapes the AgentEnvelope (same convention as ``trialerror/cli/
verify.py``/``trialerror/cli/lens.py``).

**Judgment, from the CLI (the LLM-judgment boundary -- ``trialerror/cli/
verify.py``'s own note, applied here identically):** this process never
calls an LLM. ``run``'s ``--body``/``--judgments-file`` are how the
CALLER (an agent that already authored the overview out-of-band, or a
test) supplies the summary text -- omitting both builds the envelope and
returns it PENDING (``result.status == "pending_judgment"``), ready for a
caller with an agent handy to fill and resubmit.

Registration rule (design Section 5.2 / lane safety): this module lives
at ``trialerror/cli/summarize.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touches
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.jobs.ledger import enqueue as enqueue_job
from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.summarize.api import DEFAULT_WORD_CAP, build_summary_envelope, get_summary, get_summary_by_id, list_summaries, store_summary
from trialerror.summarize.errors import SummarizeError
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "summarize"
HELP = "L1 summary tier: build/store overview summaries per document or collection, look them up, batch-generate per corpus."


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
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_run = actions.add_parser("run", help="build a summary envelope for one subject; store it if a body/judgment is supplied, or enqueue a batch job")
    _add_program_root_arg(p_run)
    p_run.add_argument("--subject-kind", choices=["document", "collection"], default="document", dest="subject_kind")
    p_run.add_argument("--subject-id", dest="subject_id", help="a doc_id (subject_kind=document) or a collection key/source_id (subject_kind=collection)")
    p_run.add_argument("--doc-id", action="append", default=None, dest="doc_ids", metavar="DOC_ID", help="collection member doc_id (repeatable; collection only)")
    p_run.add_argument("--word-cap", type=int, default=DEFAULT_WORD_CAP, dest="word_cap")
    p_run.add_argument("--by-launch", dest="issued_by_launch", help="required unless --batch (batch jobs carry created_by_launch in the job payload instead)")
    p_run.add_argument("--procedure-version", default="1", dest="procedure_version")
    p_run.add_argument("--body", default=None, help="the summary text (an agent that already authored it out-of-band)")
    p_run.add_argument("--judgments-file", default=None, dest="judgments_file", help="JSON {subject_id: body_text} -- an entry for --subject-id, or the whole map for --batch")
    p_run.add_argument("--batch", action="store_true", help="auto-discover every subject_kind='document' missing/stale a summary and enqueue a 'summarize' job (design Section 6: rides the M2 ledger) instead of running one subject synchronously")
    p_run.set_defaults(handler=_run_run)

    p_show = actions.add_parser("show", help="show one summary (by --id, or the current one for --subject-kind/--subject-id)")
    _add_program_root_arg(p_show)
    p_show.add_argument("--id", default=None, dest="summary_id")
    p_show.add_argument("--subject-kind", choices=["document", "collection"], default=None, dest="subject_kind")
    p_show.add_argument("--subject-id", default=None, dest="subject_id")
    p_show.set_defaults(handler=_run_show)

    p_list = actions.add_parser("list", help="list summary rows, newest first")
    _add_program_root_arg(p_list)
    p_list.add_argument("--subject-kind", choices=["document", "collection"], default=None, dest="subject_kind")
    p_list.add_argument("--subject-id", default=None, dest="subject_id")
    p_list.add_argument("--status", choices=["current", "superseded"], default=None)
    p_list.add_argument("--limit", type=int, default=100)
    p_list.set_defaults(handler=_run_list)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    root = getattr(args, "program_root", None)
    if root:
        return Path(root)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(program_root, platform_root=getattr(args, "platform_root", None)), None


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "summarize", "no_action", "specify an action: run|show|list",
        next_actions=[next_action(["trialerror", "summarize", "--help"], "list summarize actions")],
    )


def _load_judgments_file(path: str | None) -> dict[str, str] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_batch(args: argparse.Namespace, store: Store) -> dict:
    if not args.issued_by_launch:
        return error_envelope("summarize.run", "missing_by_launch", "--batch requires --by-launch (recorded as the job payload's created_by_launch)")
    if args.subject_kind == "collection":
        return error_envelope(
            "summarize.run", "batch_collection_unsupported",
            "--batch auto-discovery only supports --subject-kind document -- a 'collection' target set is "
            "caller-defined and has no natural auto-discovery; enqueue explicit targets via the job payload instead",
        )
    judgments = _load_judgments_file(args.judgments_file) or {}
    payload = {
        "handler": "summarize",
        "subject_kind": args.subject_kind,
        "created_by_launch": args.issued_by_launch,
        "word_cap": args.word_cap,
        "procedure_version": args.procedure_version,
        "judgments": judgments,
    }
    job = enqueue_job(store, kind="custom", payload=payload)
    return ok_envelope(
        "summarize.run", result={"job": job, "status": "enqueued"},
        next_actions=[
            next_action(["trialerror", "jobs", "start-worker", "--job-id", job["job_id"], "--mode", "once"], "run the enqueued batch job")
        ],
    )


def _run_run(args: argparse.Namespace) -> dict:
    store, err = _open(args, "summarize.run")
    if err is not None:
        return err
    try:
        if args.batch:
            return _run_batch(args, store)

        if not args.subject_id:
            return error_envelope("summarize.run", "missing_subject_id", "--subject-id is required unless --batch is given")
        if not args.issued_by_launch:
            return error_envelope("summarize.run", "missing_by_launch", "--by-launch is required")

        envelope = build_summary_envelope(
            store, subject_kind=args.subject_kind, subject_id=args.subject_id, doc_ids=args.doc_ids, word_cap=args.word_cap
        )

        body = args.body
        if body is None:
            judgments = _load_judgments_file(args.judgments_file)
            if judgments is not None:
                body = judgments.get(args.subject_id)

        if body is None:
            return ok_envelope(
                "summarize.run",
                result={"status": "pending_judgment", "envelope": envelope},
                next_actions=[
                    next_action(
                        ["trialerror", "summarize", "run", "--subject-kind", args.subject_kind, "--subject-id", args.subject_id, "--by-launch", args.issued_by_launch, "--body", "<authored overview text>"],
                        "re-run supplying the authored body once an agent has filled the envelope",
                    )
                ],
            )

        row = store_summary(
            store, envelope=envelope, body=body, issued_by_launch=args.issued_by_launch, procedure_version=args.procedure_version
        )
    except SummarizeError as exc:
        return error_envelope("summarize.run", "summarize_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("summarize.run", "record_refused", str(exc))
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("summarize.run", "judgments_file_error", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "summarize.run", result={"status": "stored", "summary": row},
        next_actions=[next_action(["trialerror", "summarize", "show", "--id", row["summary_id"]], "show the stored summary")],
    )


def _run_show(args: argparse.Namespace) -> dict:
    store, err = _open(args, "summarize.show")
    if err is not None:
        return err
    try:
        if args.summary_id:
            row = get_summary_by_id(store, args.summary_id)
        elif args.subject_kind and args.subject_id:
            row = get_summary(store, subject_kind=args.subject_kind, subject_id=args.subject_id)
        else:
            return error_envelope("summarize.show", "missing_lookup_key", "give --id, or both --subject-kind and --subject-id")
    finally:
        store.close()
    if row is None:
        return error_envelope("summarize.show", "not_found", "no matching summary row")
    return ok_envelope("summarize.show", result={"summary": row})


def _run_list(args: argparse.Namespace) -> dict:
    store, err = _open(args, "summarize.list")
    if err is not None:
        return err
    try:
        rows = list_summaries(store, subject_kind=args.subject_kind, subject_id=args.subject_id, status=args.status, limit=args.limit)
    finally:
        store.close()
    return ok_envelope("summarize.list", result={"summaries": rows, "count": len(rows)})
