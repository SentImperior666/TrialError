"""``trialerror artifact`` — the typed-artifact registry. Design Section 5.2
(artifact row): "register, list, show" (table explicitly headed "Commands
(abridged)" — see ``trialerror/artifacts/registry.py`` module docstring for why
``create`` is added here as the abridged table's missing artifact-row
creation step). Thin CLI wrapper over ``trialerror.artifacts.registry`` — all
logic lives there; this module only parses argv and shapes the
AgentEnvelope.

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by M10.

FX-9 (docs/reviews/IMPL_REVIEW_VERDICT.md NB-5/SD-1 v1 ticket): the
``templates`` action below is the "``trialerror artifact templates`` CLI
listing" the ticket names — a thin wrapper over
``trialerror.artifacts.template_seed``, listing/seeding the 12 bundled,
ported built-in templates. Registered as a fifth ``artifact``
subaction alongside create/register/list/show, not a new top-level group
(it operates on the same ``template`` table this group's other actions
already read via ``get_template``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.artifacts.errors import RegistrationRefusedError
from trialerror.artifacts.gates import register_failed, register_with_deviation
from trialerror.artifacts.registry import create_artifact, get_artifact, list_artifacts, register_artifact
from trialerror.artifacts.template_seed import list_builtin_templates, seed_builtin_templates
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "artifact"
HELP = "Typed-artifact registry: create, register, list, show, templates."

_PROGRAM_ROOT_HELP = "override the program root (default: discover trialerror.toml upward from CWD)"


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # See trialerror/cli/law.py for why this is registered on both the parent
    # and every action subparser. FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE):
    # default=SUPPRESS so an unset value here never overwrites the global
    # --program-root the top-level parser resolved.
    p.add_argument("--program-root", default=argparse.SUPPRESS, help=_PROGRAM_ROOT_HELP)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_create = actions.add_parser("create", help="create a new artifact row (status='draft')")
    _add_program_root_arg(p_create)
    p_create.add_argument("--type", required=True, dest="type_key", help="template.type_key")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--path", required=True)
    p_create.add_argument("--sha256", required=True)
    p_create.add_argument("--by-launch", required=True, dest="by_launch")
    p_create.add_argument("--purpose", default=None)
    p_create.add_argument("--domain", action="append", default=None, dest="domains", metavar="DOMAIN")
    p_create.add_argument("--attrs", default=None, help="JSON object string")
    p_create.set_defaults(handler=_run_create)

    p_register = actions.add_parser(
        "register",
        help="register an existing artifact — refused for a gated type unless its gate is union_applied "
        "(or --with-deviation / --as-failed, by an operator decision)",
    )
    _add_program_root_arg(p_register)
    p_register.add_argument("--id", required=True, dest="artifact_id")
    p_register.add_argument("--by-launch", required=True, dest="by_launch")
    p_register.add_argument("--supersedes", default=None, help="artifact_id of an existing 'registered' artifact")
    mode = p_register.add_mutually_exclusive_group()
    mode.add_argument(
        "--with-deviation", action="store_true", dest="with_deviation",
        help="register a gated artifact whose gate suite failed on something the artifact itself discloses "
        "(needs --deviation, once per failing check, and --decided-by)",
    )
    mode.add_argument(
        "--as-failed", action="store_true", dest="as_failed",
        help="register an artifact as a failed result: its critic verdict was FAIL (needs --failure-ref), or "
        "its gate was failed on a mismatched reproduction by `gate fail-reproduction` (--failure-ref is then "
        "optional: the gate's own record states the failure). Needs --decided-by",
    )
    dev = p_register.add_mutually_exclusive_group()
    dev.add_argument(
        "--deviation", action="append", default=None, metavar="CHECK=REASON@REPORT_REF",
        help="with --with-deviation: a failing gate-suite check, the reason, and a string that appears in the "
        "artifact, separated by '=' and one '@' (a reference or reason that needs an '@' goes in --deviations-file)",
    )
    dev.add_argument(
        "--deviations-file", default=None, dest="deviations_file", metavar="PATH",
        help="with --with-deviation: a UTF-8 JSON list of {check, reason, report_ref}; instead of --deviation",
    )
    p_register.add_argument(
        "--failure-ref", default=None, dest="failure_ref", metavar="REPORT_REF",
        help="with --as-failed: a string that appears in the artifact and states what failed",
    )
    p_register.add_argument(
        "--decided-by", default=None, dest="decided_by", metavar="REF",
        help="the operator decision (or request) id behind --with-deviation / --as-failed",
    )
    p_register.add_argument(
        "--file", default=None, dest="file", metavar="PATH",
        help="with --with-deviation / --as-failed: register the bytes of this file instead of the artifact's own "
        "path. Its sha256 must be one the gate holds: the submitted hash, or the corrected hash the gate recorded "
        "when its last blocking edit was verified",
    )
    p_register.add_argument(
        "--note", default=None,
        help="with --with-deviation / --as-failed: a plain-words note kept in the registration evidence",
    )
    p_register.set_defaults(handler=_run_register)

    p_list = actions.add_parser("list", help="filtered read over the artifact registry")
    _add_program_root_arg(p_list)
    p_list.add_argument("--type", default=None, dest="type_key")
    p_list.add_argument("--status", default=None, choices=["draft", "in_gate", "registered", "superseded"])
    p_list.add_argument("--limit", type=int, default=100)
    p_list.set_defaults(handler=_run_list)

    p_show = actions.add_parser("show", help="show one artifact by id")
    _add_program_root_arg(p_show)
    p_show.add_argument("--id", required=True, dest="artifact_id")
    p_show.set_defaults(handler=_run_show)

    p_templates = actions.add_parser(
        "templates",
        help="list the 12 bundled built-in templates (FX-9); --seed inserts any missing rows first",
    )
    _add_program_root_arg(p_templates)
    p_templates.add_argument("--seed", action="store_true", help="insert any bundled template not yet in this program's template table")
    p_templates.set_defaults(handler=_run_templates)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = Path(args.program_root) if args.program_root else find_program_root()
    if root is None:
        return None, error_envelope(
            "artifact",
            "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(root), None


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(
        "artifact",
        "no_action",
        "specify an action: create|register|list|show",
        next_actions=[next_action(["trialerror", "artifact", "--help"], "list artifact actions")],
    )


def _run_create(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        attrs = json.loads(args.attrs) if args.attrs else None
        row = create_artifact(
            store,
            type_key=args.type_key,
            title=args.title,
            path=args.path,
            sha256=args.sha256,
            by_launch=args.by_launch,
            purpose=args.purpose,
            domains=args.domains,
            attrs=attrs,
        )
    except (StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("artifact create", "create_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "artifact create",
        result=row,
        next_actions=[next_action(["trialerror", "gate", "open", "--artifact-id", row["artifact_id"]], "open a review gate")],
    )


def _parse_deviation(entry: str) -> dict[str, str]:
    """``CHECK=REASON@REPORT_REF`` -> the deviation mapping. The check ends at
    the first ``=``; the rest holds exactly one ``@``. A second ``@`` would make
    the split ambiguous (a reason quietly cut short, or a reference quietly
    truncated so the verbatim check passes on less than was meant): refused."""
    check, eq, rest = entry.partition("=")
    if rest.count("@") > 1:
        raise ValueError(
            f"--deviation {entry!r} holds more than one '@', so where the reason ends and the report reference "
            "starts is ambiguous; put the deviations in a UTF-8 JSON file and pass --deviations-file"
        )
    reason, at, report_ref = rest.partition("@")
    if not (eq and at and check.strip() and reason.strip() and report_ref.strip()):
        raise ValueError(f"--deviation {entry!r} is not CHECK=REASON@REPORT_REF")
    return {"check": check.strip(), "reason": reason.strip(), "report_ref": report_ref.strip()}


def _load_deviations_file(path: str) -> list[dict[str, str]]:
    """A JSON list of ``{check, reason, report_ref}``. Raises ``ValueError`` /
    ``OSError`` for anything else."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # tolerate an editor's BOM
    if not isinstance(data, list) or not data:
        raise ValueError(f"--deviations-file {path!r} must hold a non-empty JSON list of deviations")
    out: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) and entry[k].strip() for k in ("check", "reason", "report_ref")
        ):
            raise ValueError(
                f"--deviations-file {path!r}: every entry needs a non-empty check, reason and report_ref; got {entry!r}"
            )
        out.append({k: entry[k] for k in ("check", "reason", "report_ref")})
    return out


def _register_mode_problem(args: argparse.Namespace) -> str | None:
    if args.with_deviation:
        if not args.deviation and not args.deviations_file:
            return "--with-deviation needs at least one --deviation CHECK=REASON@REPORT_REF, or --deviations-file"
        if not args.decided_by:
            return "--with-deviation needs --decided-by (the operator decision that accepts the deviation)"
        if args.failure_ref:
            return "--failure-ref is only for --as-failed"
    elif args.as_failed:
        # --failure-ref may be left out when the gate itself records the failure
        # (an operator-decided failure); register_failed decides, and names what is missing.
        if not args.decided_by:
            return "--as-failed needs --decided-by (the operator decision that puts the failure on the record)"
        if args.deviation or args.deviations_file:
            return "--deviation and --deviations-file are only for --with-deviation"
    else:
        stray = [
            flag
            for flag, value in (("--deviation", args.deviation), ("--deviations-file", args.deviations_file), ("--failure-ref", args.failure_ref), ("--decided-by", args.decided_by), ("--file", args.file), ("--note", args.note))
            if value
        ]
        if stray:
            return f"{', '.join(stray)} only apply with --with-deviation or --as-failed"
    return None


def _run_register(args: argparse.Namespace) -> dict:
    problem = _register_mode_problem(args)
    if problem is not None:
        return error_envelope("artifact register", "bad_input", problem)
    try:
        deviations = None
        if args.with_deviation:
            deviations = (
                _load_deviations_file(args.deviations_file)
                if args.deviations_file
                else [_parse_deviation(d) for d in args.deviation]
            )
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        return error_envelope("artifact register", "bad_input", str(exc))
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.with_deviation or args.as_failed:
            artifact = get_artifact(store, args.artifact_id)
            if artifact is None:
                raise ValueError(f"no such artifact: {args.artifact_id!r}")
            if not artifact.get("gate_id"):
                raise RegistrationRefusedError(
                    f"artifact {args.artifact_id!r} has no gate; only a gated artifact can be registered this way"
                )
            if args.with_deviation:
                row = register_with_deviation(
                    store, gate_id=artifact["gate_id"], deviations=deviations,
                    decided_by=args.decided_by, by_launch=args.by_launch, supersedes=args.supersedes,
                    file=args.file, note=args.note,
                )
            else:
                row = register_failed(
                    store, gate_id=artifact["gate_id"], failure_ref=args.failure_ref,
                    decided_by=args.decided_by, by_launch=args.by_launch, supersedes=args.supersedes,
                    file=args.file, note=args.note,
                )
        else:
            row = register_artifact(
                store, artifact_id=args.artifact_id, by_launch=args.by_launch, supersedes=args.supersedes
            )
    except RegistrationRefusedError as exc:
        return error_envelope("artifact register", "registration_refused", str(exc))
    except (StoreError, ValueError) as exc:
        return error_envelope("artifact register", "register_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("artifact register", result=row)


def _run_list(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = list_artifacts(store, type_key=args.type_key, status=args.status, limit=args.limit)
    finally:
        store.close()
    return ok_envelope("artifact list", result={"artifacts": rows, "count": len(rows)})


def _run_show(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        row = get_artifact(store, args.artifact_id)
        registration = _registered_bytes(store, row) if row is not None else None
    finally:
        store.close()
    if row is None:
        return error_envelope("artifact show", "not_found", f"no such artifact: {args.artifact_id!r}")
    if registration is not None:
        row = {**row, **registration}
    return ok_envelope("artifact show", result=row)


def _registered_bytes(store: Store, artifact: dict) -> dict | None:
    """The bytes a registration bound to (hash, which version, where), read from
    the evidence of the gate's ``-> registered`` transition. ``None`` for an
    artifact that was not registered through a path that records them."""
    gate_id = artifact.get("gate_id")
    if artifact.get("status") != "registered" or not gate_id:
        return None
    transition = store.ops.execute(
        "SELECT evidence FROM gate_transition WHERE gate_id = ? AND to_state = 'registered' "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (gate_id,),
    ).fetchone()
    try:
        evidence = json.loads(transition["evidence"]) if transition and transition["evidence"] else None
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(evidence, dict) or "registered_sha256" not in evidence:
        return None
    shown = {k: evidence[k] for k in ("registered_sha256", "registered_bytes", "registered_path", "note") if k in evidence}
    return {"registered_sha256": shown.get("registered_sha256"), "registered_bytes": shown.get("registered_bytes"),
            "registered_path": shown.get("registered_path"),
            **({"registered_note": shown["note"]} if "note" in shown else {})}


def _run_templates(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        seeded: list[dict] = []
        if args.seed:
            seeded = seed_builtin_templates(store)
        rows = list_builtin_templates(store)
    finally:
        store.close()
    return ok_envelope(
        "artifact templates",
        result={"templates": rows, "count": len(rows), "seeded_count": len(seeded)},
        next_actions=[next_action(["trialerror", "artifact", "templates", "--seed"], "insert any missing built-in template rows")] if not args.seed else None,
    )
