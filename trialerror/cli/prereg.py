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
from trialerror.verify.errors import (
    InvalidProcedureError,
    PlanCheckFailedError,
    PreregNotFoundError,
    PreregTamperedError,
    PreregVoidedError,
    UnknownPlanSuiteError,
)
from trialerror.verify.plan_check import PLAN_SUITE_ADMISSION, PLAN_SUITES
from trialerror.verify.prereg import check_prereg_plan, commit_prereg, prereg_status, reveal_prereg

GROUP_NAME = "prereg"
HELP = (
    "Blind pre-registration: commit, check, reveal, status (escrow in the platform tree, outside the program repo)."
)


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


def _add_params_args(p: argparse.ArgumentParser, *, required: bool = False) -> None:
    group = p.add_mutually_exclusive_group(required=required)
    group.add_argument("--params", default=None, help="JSON object string, hashed as canonical (sorted-key) JSON")
    group.add_argument("--params-file", default=None, dest="params_file", help="read the params JSON object from this UTF-8 file")


def _add_plan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--plan-suite", default=None, dest="plan_suite", metavar="SUITE",
        help=f"check the plan against this suite before anything is written: {' | '.join(sorted(PLAN_SUITES))}",
    )
    p.add_argument("--round-id", default=None, dest="round_id", help="the round this prereg is for (required with --plan-suite)")
    p.add_argument(
        "--parent-prereg", default=None, dest="parent_prereg", metavar="PREREG_ID",
        help=f"the round's first prereg (required for, and only for, the {PLAN_SUITE_ADMISSION} suite)",
    )


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
    _add_params_args(p_commit)
    _add_plan_args(p_commit)
    p_commit.add_argument(
        "--accept-deviation", action="append", default=None, dest="accept_deviation", metavar="CHECK_ID=REASON",
        help="let one failing required plan check through, with the reason (repeatable; needs --decided-by; the "
        "check must be one that actually failed)",
    )
    p_commit.add_argument(
        "--decided-by", default=None, dest="decided_by", metavar="REF",
        help="the operator decision (or request) id that accepts the deviations; required with --accept-deviation",
    )
    p_commit.set_defaults(handler=_run_commit)

    p_check = actions.add_parser(
        "check", help="dry run of the plan-time check of `commit`: writes no escrow and no row, returns the result"
    )
    _add_program_root_arg(p_check)
    _add_params_args(p_check, required=True)
    _add_plan_args(p_check)
    p_check.set_defaults(handler=_run_check)

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
        "prereg", "no_action", "specify an action: commit|check|reveal|status",
        next_actions=[next_action(["trialerror", "prereg", "--help"], "list prereg actions")],
    )


def _load_params(args: argparse.Namespace) -> dict | None:
    """The params object from ``--params`` or ``--params-file`` (``None`` when
    neither is given). Raises ``ValueError`` for anything that is not a JSON
    object; ``OSError``/``json.JSONDecodeError`` pass through."""
    if args.params_file is not None:
        raw = Path(args.params_file).read_text(encoding="utf-8-sig")  # tolerate an editor's BOM
    elif args.params:
        raw = args.params
    else:
        return None
    params = json.loads(raw)
    if not isinstance(params, dict):
        raise ValueError("the params must be a JSON object")
    return params


def _plan_flag_problem(args: argparse.Namespace, *, committing: bool) -> str | None:
    """A misuse of the plan flags, in plain words, or ``None``."""
    suite = args.plan_suite
    if suite is None:
        stray = [
            flag
            for flag, value in (
                ("--round-id", args.round_id),
                ("--parent-prereg", args.parent_prereg),
                ("--accept-deviation", getattr(args, "accept_deviation", None)),
                ("--decided-by", getattr(args, "decided_by", None)),
            )
            if value
        ]
        if stray:
            return f"{', '.join(stray)} only apply together with --plan-suite"
        if not committing:
            return "prereg check needs --plan-suite"
        return None
    if not args.round_id:
        return "--plan-suite needs --round-id"
    if suite == PLAN_SUITE_ADMISSION and not args.parent_prereg:
        return f"the {PLAN_SUITE_ADMISSION} suite needs --parent-prereg (the round's first prereg)"
    if suite != PLAN_SUITE_ADMISSION and args.parent_prereg:
        return f"--parent-prereg is only for the {PLAN_SUITE_ADMISSION} suite"
    if getattr(args, "accept_deviation", None) and not getattr(args, "decided_by", None):
        return "--accept-deviation needs --decided-by (the operator decision that accepts it)"
    if getattr(args, "decided_by", None) and not getattr(args, "accept_deviation", None):
        return "--decided-by only applies together with --accept-deviation"
    return None


def _parse_deviations(entries: list[str] | None) -> dict[str, str]:
    deviations: dict[str, str] = {}
    for entry in entries or []:
        check_id, sep, reason = entry.partition("=")
        if not sep or not check_id.strip() or not reason.strip():
            raise ValueError(f"--accept-deviation {entry!r} is not CHECK_ID=REASON")
        deviations[check_id.strip()] = reason.strip()
    return deviations


def _unknown_suite_envelope(cmd: str, suite: str) -> dict:
    return error_envelope(
        cmd, "unknown_plan_suite", f"unknown plan suite {suite!r}; known: {sorted(PLAN_SUITES)}",
        next_actions=[next_action(["trialerror", "prereg", cmd.split(".")[-1], "--help"], "list the plan suites")],
    )


def _invocation_argv(args: argparse.Namespace, suite_id: str) -> list[str]:
    """The flags of this invocation that a follow-up ``prereg commit`` needs to
    be the same commit: the title, the procedure, the plan flags, the params and
    the roots, in the way they were given."""
    argv = ["trialerror", "prereg", "commit", "--title", args.title]
    if args.procedure is not None:
        argv += ["--procedure", args.procedure]
    else:
        argv += ["--procedure-file", args.procedure_file]
    argv += ["--plan-suite", suite_id, "--round-id", args.round_id]
    if args.parent_prereg:
        argv += ["--parent-prereg", args.parent_prereg]
    if args.params_file is not None:
        argv += ["--params-file", args.params_file]
    elif args.params:
        argv += ["--params", args.params]
    for flag, value in (("--program-root", getattr(args, "program_root", None)), ("--platform-root", getattr(args, "platform_root", None))):
        if value:
            argv += [flag, str(value)]
    return argv


def _plan_failed_envelope(cmd: str, exc: PlanCheckFailedError, args: argparse.Namespace) -> dict:
    result = exc.result
    recheck = ["trialerror", "prereg", "check", "--plan-suite", result.suite_id, "--round-id", args.round_id or "<round>"]
    if args.parent_prereg:
        recheck += ["--parent-prereg", args.parent_prereg]
    recheck += ["--params-file", "<fixed params file>"]
    accept = _invocation_argv(args, result.suite_id)
    for check_id in result.must_failures:
        accept += ["--accept-deviation", f"{check_id}=<reason>"]
    accept += ["--decided-by", "<operator decision id>"]
    return error_envelope(
        cmd, "plan_check_failed",
        f"{exc} Two ways forward: fix the params (for `admission_escrow_planned`, add `\"rooms\": false`, or "
        "`\"admission_escrow\": {\"by\": \"second_prereg_commit\"}` with a room seed) and commit again; or, "
        "if the operator decides the round goes ahead as it is, commit again naming each failing check with "
        "--accept-deviation CHECK_ID=REASON and the operator decision with --decided-by.",
        details=result.to_dict(),
        next_actions=[
            next_action(recheck, "after fixing the params: dry-run the check again (writes nothing)"),
            next_action(
                accept,
                "or accept the deviation(s) on the record: runs as printed once each <reason> and the "
                "<operator decision id> are filled in (needs an operator decision)",
            ),
        ],
    )


def _run_commit(args: argparse.Namespace) -> dict:
    problem = _plan_flag_problem(args, committing=True)
    if problem is not None:
        return error_envelope("prereg.commit", "bad_input", problem)
    if args.plan_suite is not None and args.plan_suite not in PLAN_SUITES:
        return _unknown_suite_envelope("prereg.commit", args.plan_suite)
    store, err = _open(args, "prereg.commit")
    if err is not None:
        return err
    try:
        procedure = args.procedure
        if procedure is None:
            procedure = Path(args.procedure_file).read_text(encoding="utf-8")
        params = _load_params(args)
        deviations = _parse_deviations(args.accept_deviation)
        row = commit_prereg(
            store, title=args.title, procedure=procedure, params=params,
            plan_suite=args.plan_suite, round_id=args.round_id, parent_prereg_id=args.parent_prereg,
            accepted_deviations=deviations or None, decided_by=args.decided_by,
        )
    except PlanCheckFailedError as exc:
        return _plan_failed_envelope("prereg.commit", exc, args)
    except UnknownPlanSuiteError as exc:
        return _unknown_suite_envelope("prereg.commit", args.plan_suite or str(exc))
    except InvalidProcedureError as exc:
        return error_envelope("prereg.commit", "invalid_procedure", str(exc))
    except PreregNotFoundError as exc:
        return error_envelope("prereg.commit", "not_found", str(exc))
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        return error_envelope("prereg.commit", "bad_input", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("prereg.commit", "commit_refused", str(exc))
    finally:
        store.close()
    result = dict(row)
    if row.get("plan_check"):
        record = json.loads(row["plan_check"])
        result["plan_check_items"] = record["items"]
    return ok_envelope(
        "prereg.commit", result=result,
        next_actions=[next_action(["trialerror", "prereg", "status", "--id", row["prereg_id"]], "check status later")],
    )


def _run_check(args: argparse.Namespace) -> dict:
    problem = _plan_flag_problem(args, committing=False)
    if problem is not None:
        return error_envelope("prereg.check", "bad_input", problem)
    if args.plan_suite not in PLAN_SUITES:
        return _unknown_suite_envelope("prereg.check", args.plan_suite)
    store, err = _open(args, "prereg.check")
    if err is not None:
        return err
    try:
        params = _load_params(args)
        result = check_prereg_plan(
            store, plan_suite=args.plan_suite, params=params, round_id=args.round_id, parent_prereg_id=args.parent_prereg
        )
    except PreregNotFoundError as exc:
        return error_envelope("prereg.check", "not_found", str(exc))
    except (OSError, ValueError) as exc:
        return error_envelope("prereg.check", "bad_input", str(exc))
    finally:
        store.close()
    # A finished dry run is a success either way: it exits 0 and the verdict is
    # `result.overall` (the dispatcher would exit 1 for an ok=false envelope,
    # and a failing check is the answer to the question, not a failed command).
    warnings = (
        [{"code": "plan_check_failed", "message": f"required check(s) failing: {result.must_failures}"}]
        if result.must_failures
        else None
    )
    return ok_envelope("prereg.check", result=result.to_dict(), warnings=warnings)


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
