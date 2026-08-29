"""``trialerror law`` — user rulings as versioned law. Design Section 5.2 (law
row): "append, lookup, digest, verify, diff-foreign | append+digest
atomic." Thin CLI wrapper over ``trialerror.law.service`` — all logic lives
there; this module only parses argv and shapes the AgentEnvelope.

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by M4.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.law.service import (
    append_ruling,
    diff_foreign,
    get_current_digest,
    lookup_rulings,
    render_current_digest_to_disk,
    verify_pin,
)
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "law"
HELP = "User rulings as versioned law: append, lookup, digest, verify (spawn-gate pin check), diff-foreign."

_PROGRAM_ROOT_HELP = "override the program root (default: discover trialerror.toml upward from CWD)"


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # Registered on the `law` parser AND on every action subparser (not
    # just the parent): argparse only recognizes a parent-only optional
    # BEFORE the subcommand token (`trialerror law --program-root X append
    # ...`), and the natural agent/human ordering is `trialerror law append
    # --program-root X ...`. Duplicating it here is what makes both work.
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root the
    # top-level parser resolved.
    p.add_argument("--program-root", default=argparse.SUPPRESS, help=_PROGRAM_ROOT_HELP)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_append = actions.add_parser(
        "append", help="append a ruling and regenerate the digest, atomically (the ONE way to add law)"
    )
    _add_program_root_arg(p_append)
    p_append.add_argument("--summary", required=True, help="required; real ledgers may have no verbatim quote")
    p_append.add_argument("--verbatim-quote", default=None, dest="verbatim_quote")
    p_append.add_argument(
        "--standing-clause", action="append", default=None, dest="standing_clauses", metavar="CLAUSE"
    )
    p_append.add_argument("--domain", action="append", default=None, dest="domains", metavar="DOMAIN")
    p_append.add_argument("--supersedes", default=None, help="ruling_id of an existing ACTIVE ruling to supersede")
    p_append.add_argument(
        "--supersedes-note", default=None, dest="supersedes_note", help="prose supersession target (F20(c))"
    )
    p_append.add_argument("--ts", default=None, help="override the append timestamp (tests only)")
    p_append.set_defaults(handler=_run_append)

    p_lookup = actions.add_parser("lookup", help="filtered read over the ruling ledger, in append order")
    _add_program_root_arg(p_lookup)
    p_lookup.add_argument("--id", default=None, dest="ruling_id")
    p_lookup.add_argument("--domain", default=None)
    p_lookup.add_argument("--status", default=None, choices=["active", "superseded"])
    p_lookup.add_argument("--query", default=None, help="substring match against summary/verbatim_quote")
    p_lookup.set_defaults(handler=_run_lookup)

    p_digest = actions.add_parser(
        "digest", help="show the current digest version/pin; --render re-flushes the file from ops.db truth"
    )
    _add_program_root_arg(p_digest)
    p_digest.add_argument(
        "--render", action="store_true", help="re-write law/LAW_DIGEST.md from the current digest row (no version bump)"
    )
    p_digest.set_defaults(handler=_run_digest)

    p_verify = actions.add_parser(
        "verify",
        help="verify a pin: freshness (matches current digest) + chain integrity — the spawn-gate contract",
    )
    _add_program_root_arg(p_verify)
    p_verify.add_argument("--pin", required=True, help="'vNN@YYYY-MM-DD', e.g. the open session's boot_pin_version")
    p_verify.set_defaults(handler=_run_verify)

    p_diff = actions.add_parser("diff-foreign", help="rulings appended (by any session) since a given pin")
    _add_program_root_arg(p_diff)
    p_diff.add_argument("--pin", required=True)
    p_diff.set_defaults(handler=_run_diff_foreign)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = Path(args.program_root) if args.program_root else find_program_root()
    if root is None:
        return None, error_envelope(
            "law",
            "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(root), None


def _load_program_config(program_root: Path) -> dict:
    # Same best-effort load every other CLI group's own private copy of
    # this helper already uses (trialerror.cli.ingest._load_program_config,
    # trialerror.cli.memory._load_program_config, ...) -- see the build
    # report's deviations for why this isn't consolidated in this pass.
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(
        "law",
        "no_action",
        "specify an action: append|lookup|digest|verify|diff-foreign",
        next_actions=[next_action(["trialerror", "law", "--help"], "list law actions")],
    )


def _run_append(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        result = append_ruling(
            store,
            summary=args.summary,
            ts=args.ts,
            verbatim_quote=args.verbatim_quote,
            standing_clauses=args.standing_clauses,
            domains=args.domains,
            supersedes=args.supersedes,
            supersedes_note=args.supersedes_note,
            config=_load_program_config(store.program_root),
        )
    except (StoreError, ValueError) as exc:
        return error_envelope("law append", "append_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "law append",
        result=result.to_dict(),
        next_actions=[
            next_action(["trialerror", "law", "verify", "--pin", result.pin], "confirm the new pin verifies")
        ],
    )


def _run_lookup(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = lookup_rulings(
            store, ruling_id=args.ruling_id, domain=args.domain, status=args.status, query=args.query
        )
    finally:
        store.close()
    return ok_envelope("law lookup", result={"rulings": rows, "count": len(rows)})


def _run_digest(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.render:
            try:
                rendered = render_current_digest_to_disk(store)
            except ValueError as exc:
                return error_envelope("law digest", "no_digest_yet", str(exc))
            return ok_envelope("law digest", result=rendered.to_dict())
        digest = get_current_digest(store)
    finally:
        store.close()
    if digest is None:
        return error_envelope(
            "law digest",
            "no_digest_yet",
            "no ruling has ever been appended in this program",
            next_actions=[next_action(["trialerror", "law", "append", "--summary", "..."], "append the first ruling")],
        )
    return ok_envelope("law digest", result=digest)


def _run_verify(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        result = verify_pin(store, args.pin)
    finally:
        store.close()
    if not result.valid:
        return error_envelope("law verify", "pin_invalid", result.reason, details=result.to_dict())
    return ok_envelope("law verify", result=result.to_dict())


def _run_diff_foreign(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = diff_foreign(store, args.pin)
    except ValueError as exc:
        return error_envelope("law diff-foreign", "bad_pin", str(exc))
    finally:
        store.close()
    return ok_envelope("law diff-foreign", result={"since_pin": args.pin, "foreign_rulings": rows, "count": len(rows)})
