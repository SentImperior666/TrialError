"""``trialerror eval`` -- DeepEval-pattern gate acceptance suites. Thin wrapper
over ``trialerror.eval.gate_suites`` -- all logic lives there; this module only
parses argv and shapes the AgentEnvelope (same convention as
``trialerror/cli/verify.py``/``trialerror/cli/gate.py``).

TRIALERROR-DEV-NOTE (CLI surface deviates from the literal brief, restated at the
CLI layer): this build's brief names the verb ``trialerror gate eval
<gate_id>``. ``trialerror/cli/gate.py`` is the CLI surface for
``trialerror/artifacts/gates.py``, a different subsystem/lane this build's
pathspec-limited commit does not touch -- this build owns
``trialerror/verify/``/``trialerror/eval/`` (new) only. The shipped verb is
``trialerror eval gate --gate-id <gate_id> --suite <suite_id> ...`` instead, in
this NEW module -- auto-discovered by ``trialerror.cli.discover_groups``, so
adding it never touches ``trialerror/cli/__init__.py`` or ``trialerror/cli/gate.py``
(the design's own registration rule, restated verbatim in
``trialerror/cli/verify.py``'s own module docstring).

**Judgment boundary:** N/A -- this module (and everything under
``trialerror.eval``) never calls an LLM; a metric function is a pure Python
callable over an already-assembled ``subject`` dict (see
``trialerror/eval/gate_suites.py``'s own module docstring).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.eval.errors import EvalError
from trialerror.eval.gate_suites import list_suites, run_gate_suite_for_gate
from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "eval"
HELP = "Gate acceptance suites: DeepEval-pattern metric checks over an artifact under review, run as real pytest test cases."


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # FX-12 convention (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE, restated by
    # every group's own CLI module): default=SUPPRESS so an unset value
    # here never overwrites the global --program-root the top-level parser
    # resolved.
    p.add_argument("--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_gate = actions.add_parser(
        "gate", help="run a registered gate suite against a gate's artifact; results written onto gate.reproduction_ref/reproduction_status"
    )
    _add_program_root_arg(p_gate)
    p_gate.add_argument("--gate-id", required=True, dest="gate_id")
    p_gate.add_argument("--suite", required=True, dest="suite_id", help="a registered gate suite id (see 'trialerror eval list-suites')")
    p_gate.add_argument("--subject-file", required=True, dest="subject_file", help="JSON file: the artifact-under-review data the suite's metric functions read")
    p_gate.add_argument("--by-launch", required=True, dest="issued_by_launch")
    p_gate.add_argument("--timeout", type=float, default=60.0)
    p_gate.add_argument("--procedure-version", default="1", dest="procedure_version")
    p_gate.set_defaults(handler=_run_gate)

    p_list = actions.add_parser("list-suites", help="list registered gate suite ids and their check names")
    p_list.set_defaults(handler=_run_list_suites)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, dict | None]:
    program_root = Path(args.program_root) if args.program_root else find_program_root()
    if program_root is None:
        return None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")],
        )
    return open_store(program_root), None


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "eval", "no_action", "specify an action: gate|list-suites",
        next_actions=[next_action(["trialerror", "eval", "--help"], "list eval actions")],
    )


def _run_list_suites(_args: argparse.Namespace) -> dict:
    return ok_envelope("eval.list-suites", result={"suites": list_suites()})


def _run_gate(args: argparse.Namespace) -> dict:
    store, err = _open(args, "eval.gate")
    if err is not None:
        return err
    try:
        subject = json.loads(Path(args.subject_file).read_text(encoding="utf-8"))
        result = run_gate_suite_for_gate(
            store, gate_id=args.gate_id, suite_id=args.suite_id, subject=subject,
            issued_by_launch=args.issued_by_launch, timeout=args.timeout, procedure_version=args.procedure_version,
        )
    except EvalError as exc:
        return error_envelope("eval.gate", "gate_suite_refused", str(exc))
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("eval.gate", "subject_file_unreadable", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("eval.gate", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "eval.gate", result=result,
        next_actions=(
            [next_action(["trialerror", "gate", "apply-union", "--id", args.gate_id, "--by-launch", args.issued_by_launch], "apply the gate union if the suite passed")]
            if result.get("overall") == "PASS" else []
        ),
    )
