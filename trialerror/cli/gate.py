"""``trialerror gate`` — the gate state machine. Design Section 5.2 (gate row):
"open, submit, verdict, apply-union, verify-edit, advance." Thin CLI
wrapper over ``trialerror.artifacts.gates`` — all logic lives there; this
module only parses argv and shapes the AgentEnvelope.

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by M10.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.artifacts.errors import GateEntryConditionError, IllegalTransitionError
from trialerror.artifacts.gates import advance_gate, apply_union, open_gate, record_verdict, submit_gate, verify_edit
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "gate"
HELP = "Gate state machine: open, submit, verdict, apply-union, verify-edit, advance."

_PROGRAM_ROOT_HELP = "override the program root (default: discover trialerror.toml upward from CWD)"


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root the
    # top-level parser resolved.
    p.add_argument("--program-root", default=argparse.SUPPRESS, help=_PROGRAM_ROOT_HELP)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_open = actions.add_parser("open", help="open a new gate (state='draft') for an existing artifact")
    _add_program_root_arg(p_open)
    p_open.add_argument("--artifact-id", required=True, dest="artifact_id")
    p_open.set_defaults(handler=_run_open)

    p_submit = actions.add_parser("submit", help="draft -> submitted")
    _add_program_root_arg(p_submit)
    p_submit.add_argument("--id", required=True, dest="gate_id")
    p_submit.add_argument("--by-launch", required=True, dest="by_launch")
    p_submit.add_argument("--evidence", default=None, help="JSON value string")
    p_submit.set_defaults(handler=_run_submit)

    p_verdict = actions.add_parser(
        "verdict", help="record a critic verdict + edits/reproduction fields; submitted -> gated|failed"
    )
    _add_program_root_arg(p_verdict)
    p_verdict.add_argument("--id", required=True, dest="gate_id")
    p_verdict.add_argument("--verdict", required=True, choices=["PASS", "PASS_WITH_EDITS", "FAIL"])
    p_verdict.add_argument("--critic-launch", default=None, dest="critic_launch")
    p_verdict.add_argument("--by-launch", default=None, dest="by_launch", help="defaults to --critic-launch")
    p_verdict.add_argument("--edits", default=None, help="JSON array string: [{text, blocking, ...}, ...]")
    p_verdict.add_argument("--reproduction-ref", default=None, dest="reproduction_ref")
    p_verdict.add_argument(
        "--reproduction-status", default=None, dest="reproduction_status", choices=["match", "mismatch", "unrun"]
    )
    p_verdict.add_argument("--evidence", default=None, help="JSON value string")
    p_verdict.set_defaults(handler=_run_verdict)

    p_union = actions.add_parser(
        "apply-union", help="gated -> union_applied (the terminal-pass transition; enforces F10)"
    )
    _add_program_root_arg(p_union)
    p_union.add_argument("--id", required=True, dest="gate_id")
    p_union.add_argument("--by-launch", required=True, dest="by_launch")
    p_union.add_argument("--evidence", default=None, help="JSON value string")
    p_union.set_defaults(handler=_run_apply_union)

    p_verify_edit = actions.add_parser(
        "verify-edit", help="mark one edits[] entry applied+verified (not a state transition)"
    )
    _add_program_root_arg(p_verify_edit)
    p_verify_edit.add_argument("--id", required=True, dest="gate_id")
    p_verify_edit.add_argument("--edit-id", required=True, dest="edit_id")
    p_verify_edit.add_argument("--by-launch", required=True, dest="by_launch")
    p_verify_edit.add_argument("--verified-note", default=None, dest="verified_note")
    p_verify_edit.set_defaults(handler=_run_verify_edit)

    p_advance = actions.add_parser(
        "advance", help="the generic, low-level transition entry point — refuses any illegal edge"
    )
    _add_program_root_arg(p_advance)
    p_advance.add_argument("--id", required=True, dest="gate_id")
    p_advance.add_argument("--to", required=True, dest="to_state")
    p_advance.add_argument("--by-launch", required=True, dest="by_launch")
    p_advance.add_argument("--evidence", default=None, help="JSON value string")
    p_advance.set_defaults(handler=_run_advance)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = Path(args.program_root) if args.program_root else find_program_root()
    if root is None:
        return None, error_envelope(
            "gate",
            "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(root), None


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(
        "gate",
        "no_action",
        "specify an action: open|submit|verdict|apply-union|verify-edit|advance",
        next_actions=[next_action(["trialerror", "gate", "--help"], "list gate actions")],
    )


def _run_open(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        row = open_gate(store, artifact_id=args.artifact_id)
    except (StoreError, ValueError) as exc:
        return error_envelope("gate open", "open_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "gate open", result=row,
        next_actions=[next_action(["trialerror", "gate", "submit", "--id", row["gate_id"]], "submit for review")],
    )


def _run_submit(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        evidence = json.loads(args.evidence) if args.evidence else None
        row = submit_gate(store, gate_id=args.gate_id, by_launch=args.by_launch, evidence=evidence)
    except (IllegalTransitionError, StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("gate submit", "transition_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("gate submit", result=row)


def _run_verdict(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        edits = json.loads(args.edits) if args.edits else None
        evidence = json.loads(args.evidence) if args.evidence else None
        row = record_verdict(
            store,
            gate_id=args.gate_id,
            verdict=args.verdict,
            critic_launch=args.critic_launch,
            by_launch=args.by_launch,
            edits=edits,
            reproduction_ref=args.reproduction_ref,
            reproduction_status=args.reproduction_status,
            evidence=evidence,
        )
    except (IllegalTransitionError, StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("gate verdict", "verdict_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("gate verdict", result=row)


def _run_apply_union(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        evidence = json.loads(args.evidence) if args.evidence else None
        row = apply_union(store, gate_id=args.gate_id, by_launch=args.by_launch, evidence=evidence)
    except GateEntryConditionError as exc:
        return error_envelope("gate apply-union", "entry_condition_failed", str(exc))
    except (IllegalTransitionError, StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("gate apply-union", "transition_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "gate apply-union", result=row,
        next_actions=[next_action(["trialerror", "artifact", "register", "--id", row["artifact_id"]], "register the artifact")],
    )


def _run_verify_edit(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        row = verify_edit(
            store, gate_id=args.gate_id, edit_id=args.edit_id, by_launch=args.by_launch,
            verified_note=args.verified_note,
        )
    except (StoreError, ValueError) as exc:
        return error_envelope("gate verify-edit", "verify_edit_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("gate verify-edit", result=row)


def _run_advance(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        evidence = json.loads(args.evidence) if args.evidence else None
        row = advance_gate(store, gate_id=args.gate_id, to_state=args.to_state, by_launch=args.by_launch, evidence=evidence)
    except GateEntryConditionError as exc:
        return error_envelope("gate advance", "entry_condition_failed", str(exc))
    except (IllegalTransitionError, StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("gate advance", "transition_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("gate advance", result=row)
