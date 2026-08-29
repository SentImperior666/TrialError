"""``trialerror prereg`` — blind pre-registration. Design Section 5.2 (``prereg``
row): "commit, reveal, status | escrow in the platform tree (Section 4.2)."
Thin CLI wrapper over ``trialerror.verify.prereg`` — all logic lives there; this
module only parses argv and shapes the AgentEnvelope (same convention as
``trialerror/cli/gate.py``/``trialerror/cli/query.py``).

Design Section 5.2 registration rule: this module lives at
``trialerror/cli/prereg.py`` and is auto-discovered by ``trialerror.cli.discover_groups``
-- adding it never touches ``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.verify.errors import InvalidProcedureError, PreregNotFoundError, PreregTamperedError, PreregVoidedError
from trialerror.verify.prereg import commit_prereg, prereg_status, reveal_prereg

GROUP_NAME = "prereg"
HELP = "Blind pre-registration: commit, reveal, status (escrow in the platform tree, outside the program repo)."


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # Registered on the `prereg` parser AND on every action subparser (the
    # ``trialerror/cli/law.py`` convention) so both `trialerror prereg --program-root
    # X commit ...` and `trialerror prereg commit ... --program-root X` work.
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

    p_commit = actions.add_parser("commit", help="hash-commit a procedure+params blind; escrows the raw content outside the program repo")
    _add_program_root_arg(p_commit)
    p_commit.add_argument("--title", required=True)
    proc_group = p_commit.add_mutually_exclusive_group(required=True)
    proc_group.add_argument("--procedure", default=None, help="the procedure text/spec, hashed verbatim")
    proc_group.add_argument("--procedure-file", default=None, dest="procedure_file", help="read the procedure text from this file")
    p_commit.add_argument("--params", default=None, help="JSON object string, hashed as canonical (sorted-key) JSON")
    p_commit.set_defaults(handler=_run_commit)

    p_reveal = actions.add_parser("reveal", help="reveal a committed procedure -- tamper-checked, copies content into the program tree")
    _add_program_root_arg(p_reveal)
    p_reveal.add_argument("--id", required=True, dest="prereg_id")
    p_reveal.add_argument("--dest-dir", default=None, dest="dest_dir")
    p_reveal.set_defaults(handler=_run_reveal)

    p_status = actions.add_parser("status", help="fetch one prereg row's status")
    _add_program_root_arg(p_status)
    p_status.add_argument("--id", required=True, dest="prereg_id")
    p_status.set_defaults(handler=_run_status)

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
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(program_root, platform_root=args.platform_root), None


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "prereg", "no_action", "specify an action: commit|reveal|status",
        next_actions=[next_action(["trialerror", "prereg", "--help"], "list prereg actions")],
    )


def _run_commit(args: argparse.Namespace) -> dict:
    store, err = _open(args, "prereg.commit")
    if err is not None:
        return err
    try:
        procedure = args.procedure
        if procedure is None:
            procedure = Path(args.procedure_file).read_text(encoding="utf-8")
        params = json.loads(args.params) if args.params else None
        row = commit_prereg(store, title=args.title, procedure=procedure, params=params)
    except InvalidProcedureError as exc:
        return error_envelope("prereg.commit", "invalid_procedure", str(exc))
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("prereg.commit", "bad_input", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("prereg.commit", "commit_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "prereg.commit", result=row,
        next_actions=[next_action(["trialerror", "prereg", "status", "--id", row["prereg_id"]], "check status later")],
    )


def _run_reveal(args: argparse.Namespace) -> dict:
    store, err = _open(args, "prereg.reveal")
    if err is not None:
        return err
    try:
        row = reveal_prereg(store, prereg_id=args.prereg_id, dest_dir=args.dest_dir)
    except PreregNotFoundError as exc:
        return error_envelope("prereg.reveal", "not_found", str(exc))
    except PreregVoidedError as exc:
        return error_envelope("prereg.reveal", "voided", str(exc))
    except PreregTamperedError as exc:
        return error_envelope("prereg.reveal", "tampered", str(exc))
    finally:
        store.close()
    return ok_envelope("prereg.reveal", result=row)


def _run_status(args: argparse.Namespace) -> dict:
    store, err = _open(args, "prereg.status")
    if err is not None:
        return err
    try:
        row = prereg_status(store, prereg_id=args.prereg_id)
    except PreregNotFoundError as exc:
        return error_envelope("prereg.status", "not_found", str(exc))
    finally:
        store.close()
    return ok_envelope("prereg.status", result=row)
