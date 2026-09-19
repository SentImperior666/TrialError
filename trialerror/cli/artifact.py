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
        help="register an existing artifact — refused for a gated type unless its gate is union_applied",
    )
    _add_program_root_arg(p_register)
    p_register.add_argument("--id", required=True, dest="artifact_id")
    p_register.add_argument("--by-launch", required=True, dest="by_launch")
    p_register.add_argument("--supersedes", default=None, help="artifact_id of an existing 'registered' artifact")
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


def _run_register(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
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
    finally:
        store.close()
    if row is None:
        return error_envelope("artifact show", "not_found", f"no such artifact: {args.artifact_id!r}")
    return ok_envelope("artifact show", result=row)


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
