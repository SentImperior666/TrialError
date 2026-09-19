"""``trialerror extract`` -- the KG extraction + merge-review CLI surface.
Design Section 11 v1 deliverable 3: "CLI: ``trialerror extract
{run,review,accept,reject,status}``." Thin wrapper over
``trialerror.ingest.extract`` -- all logic lives there; this module only parses
argv and shapes the AgentEnvelope (the ``trialerror/cli/verify.py`` convention).

**Judgment, from the CLI (the LLM-judgment boundary, stated once in
``trialerror.ingest.extract``'s own module docstring, applied here concretely):**
this process never calls an LLM. ``run``'s ``--judgments-file`` is a JSON
file the CALLER (an agent that already ran the real per-chunk extraction
judgment out-of-band, or a test) supplies -- ``{"<chunk_id>": {"entities":
[...], "relations": [...], "claims": [...]}}`` -- turned into a plain
dict-lookup ``judge`` callable, the exact ``trialerror.cli.verify._judge_from_table``
pattern.

Design Section 5.2 registration rule: this module lives at
``trialerror/cli/extract.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touches
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from trialerror.ingest import extract as extract_api
from trialerror.ingest.errors import ExtractError
from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "extract"
HELP = "KG extraction + merge review: run extraction, review/accept/reject candidates, status."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    parser.add_argument("--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)")
    parser.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)")
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")

    p_run = actions.add_parser("run", help="extract every not-yet-processed chunk of a document from a pre-computed judgments file")
    _common(p_run)
    p_run.add_argument("--doc-id", required=True, dest="doc_id")
    p_run.add_argument("--judgments-file", required=True, dest="judgments_file", help="JSON {chunk_id: {entities,relations,claims}} covering every chunk to be extracted")
    p_run.add_argument("--by-launch", required=True, dest="created_by_launch")
    p_run.set_defaults(handler=_run_run)

    p_review = actions.add_parser("review", help="list pending extraction candidates + draft merge proposals")
    _common(p_review)
    p_review.add_argument("--kind", default=None, choices=["entity", "relation", "claim"])
    p_review.add_argument("--doc-id", default=None, dest="doc_id")
    p_review.set_defaults(handler=_run_review)

    p_accept = actions.add_parser("accept", help="accept one candidate (RCD-...) or merge proposal (PROP-...)")
    _common(p_accept)
    p_accept.add_argument("--id", required=True, dest="item_id")
    p_accept.add_argument("--by-launch", required=True, dest="by_launch")
    p_accept.set_defaults(handler=_run_accept)

    p_reject = actions.add_parser("reject", help="reject one candidate (RCD-...) or merge proposal (PROP-...)")
    _common(p_reject)
    p_reject.add_argument("--id", required=True, dest="item_id")
    p_reject.add_argument("--by-launch", required=True, dest="by_launch")
    p_reject.add_argument("--reason", default=None)
    p_reject.set_defaults(handler=_run_reject)

    p_status = actions.add_parser("status", help="summary counts of extraction candidates + merge proposals by status")
    _common(p_status)
    p_status.set_defaults(handler=_run_status)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if getattr(args, "program_root", None):
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")],
        )
    return open_store(program_root, platform_root=getattr(args, "platform_root", None)), None


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "extract", "no_action", "specify an action: run|review|accept|reject|status",
        next_actions=[next_action(["trialerror", "extract", "--help"], "list extract actions")],
    )


def _load_judgments_file(path: str) -> Mapping[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_run(args: argparse.Namespace) -> dict:
    store, err = _open(args, "extract.run")
    if err is not None:
        return err
    try:
        judgments = _load_judgments_file(args.judgments_file)

        def judge(envelope: Mapping[str, Any]) -> Any:
            chunk_id = envelope["chunk_id"]
            if chunk_id not in judgments:
                raise ExtractError(f"no judgment supplied for chunk_id={chunk_id!r} in --judgments-file")
            return judgments[chunk_id]

        result = extract_api.run_extract_document(store, args.doc_id, judge=judge, created_by_launch=args.created_by_launch)
    except (ExtractError, OSError, json.JSONDecodeError) as exc:
        return error_envelope("extract.run", "extract_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("extract.run", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "extract.run", result=result,
        next_actions=[next_action(["trialerror", "extract", "review", "--doc-id", args.doc_id], "review the queued candidates")],
    )


def _run_review(args: argparse.Namespace) -> dict:
    store, err = _open(args, "extract.review")
    if err is not None:
        return err
    try:
        result = extract_api.list_pending(store, kind=args.kind, doc_id=args.doc_id)
    finally:
        store.close()
    return ok_envelope("extract.review", result=result)


def _run_accept(args: argparse.Namespace) -> dict:
    store, err = _open(args, "extract.accept")
    if err is not None:
        return err
    try:
        result = extract_api.accept(store, args.item_id, by_launch=args.by_launch)
    except ExtractError as exc:
        return error_envelope("extract.accept", "accept_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("extract.accept", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("extract.accept", result=result)


def _run_reject(args: argparse.Namespace) -> dict:
    store, err = _open(args, "extract.reject")
    if err is not None:
        return err
    try:
        result = extract_api.reject(store, args.item_id, by_launch=args.by_launch, reason=args.reason)
    except ExtractError as exc:
        return error_envelope("extract.reject", "reject_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("extract.reject", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("extract.reject", result=result)


def _run_status(args: argparse.Namespace) -> dict:
    store, err = _open(args, "extract.status")
    if err is not None:
        return err
    try:
        result = extract_api.status(store)
    finally:
        store.close()
    return ok_envelope("extract.status", result=result)
