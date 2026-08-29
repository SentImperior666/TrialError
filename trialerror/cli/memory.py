"""``trialerror memory`` — tiered agent memory: search, put, sync-export,
sync-import, merge. Design Section 5.2 (memory row): "search, put,
sync-export, sync-import, merge." Thin CLI wrapper over
``trialerror.memory.*`` — all logic lives there; this module only parses argv
and shapes the AgentEnvelope (same convention as ``trialerror/cli/law.py``).

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by M11.

**``search`` doubles as the L1/L2 full-body fetch** (``--id
<memory_item_id>``): the design names exactly 5 actions for this group,
not 6, so the progressive-disclosure "step 2" (``trialerror.memory.api.
get_item``) is exposed as a mode of ``search`` rather than a new action —
documented here as the interpretation, not silently folded in.

**``merge`` doubles as both "list open conflicts" (no args) and "resolve
one" (``--group``/``--keep``)**: ``sync-import`` is what actually RUNS a
merge pass (design Section 9.7's "export/import ... with MegaMemory merge
port" — the merge happens AT import, not as a separate manual step); the
``merge`` action is the conflict-management surface for whatever
``sync-import`` (or another account's own import) left open.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.memory.api import boot_bundle, get_item, put_item, search_items
from trialerror.memory.merge import list_conflicts, resolve_conflict
from trialerror.memory.render import export_memory, import_memory
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "memory"
HELP = "Tiered agent memory: search, put, sync-export, sync-import, merge (conflict list/resolve)."

_PROGRAM_ROOT_HELP = "override the program root (default: discover trialerror.toml upward from CWD)"


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # Same duplication-on-every-subparser rationale as trialerror/cli/law.py:
    # argparse only recognizes a parent-only optional BEFORE the
    # subcommand token, and `trialerror memory put --program-root X ...` is the
    # natural ordering. FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE):
    # default=SUPPRESS so an unset value here never overwrites the global
    # --program-root the top-level parser resolved.
    p.add_argument("--program-root", default=argparse.SUPPRESS, help=_PROGRAM_ROOT_HELP)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_search = actions.add_parser(
        "search", help="progressive-disclosure index search, or --id for one item's full body"
    )
    _add_program_root_arg(p_search)
    p_search.add_argument("--id", default=None, dest="memory_item_id", help="fetch ONE full item by id (skips filters)")
    p_search.add_argument("--query", default=None, help="substring match against key/l0_abstract/body")
    p_search.add_argument("--tier", default=None, choices=["L0", "L1", "L2"])
    p_search.add_argument("--kind", default=None, choices=["rule", "fact", "lesson", "preference", "index"])
    p_search.add_argument("--account", default=None, dest="account_id")
    p_search.add_argument(
        "--status", default="active", help="filter by status (default: active); pass '' for no filter"
    )
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument(
        "--boot-bundle", action="store_true", dest="boot_bundle_mode",
        help="return the M6 boot payload (L0 index + targeted abstracts) instead of a plain search",
    )
    p_search.add_argument("--token-budget", type=int, default=None, dest="token_budget")
    p_search.set_defaults(handler=_run_search)

    p_put = actions.add_parser("put", help="upsert one memory item by (key, account)")
    _add_program_root_arg(p_put)
    p_put.add_argument("--key", required=True)
    p_put.add_argument("--tier", required=True, choices=["L0", "L1", "L2"])
    p_put.add_argument("--kind", required=True, choices=["rule", "fact", "lesson", "preference", "index"])
    p_put.add_argument("--body", required=True)
    p_put.add_argument("--account", required=True, dest="account_id")
    p_put.add_argument("--l0-abstract", default=None, dest="l0_abstract")
    p_put.add_argument("--ts", default=None, help="override the write timestamp (tests only)")
    p_put.set_defaults(handler=_run_put)

    p_export = actions.add_parser("sync-export", help="render active items to memory/*.md (one file per item + index)")
    _add_program_root_arg(p_export)
    p_export.add_argument("--out-dir", default=None, help="default: <program-root>/memory")
    p_export.add_argument("--account", default=None, dest="account_id")
    p_export.set_defaults(handler=_run_sync_export)

    p_import = actions.add_parser(
        "sync-import", help="parse memory/*.md and two-way-merge into this store (dedup by content hash; conflicts surfaced, never dropped)"
    )
    _add_program_root_arg(p_import)
    p_import.add_argument("--in-dir", default=None, help="default: <program-root>/memory")
    p_import.set_defaults(handler=_run_sync_import)

    p_merge = actions.add_parser(
        "merge", help="no args: list open conflict groups; --group/--keep: resolve one"
    )
    _add_program_root_arg(p_merge)
    p_merge.add_argument("--group", default=None, dest="group_id")
    p_merge.add_argument("--keep", default=None, choices=["left", "right", "both"])
    p_merge.add_argument("--account", default=None, dest="account_id")
    p_merge.set_defaults(handler=_run_merge)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = Path(args.program_root) if args.program_root else find_program_root()
    if root is None:
        return None, error_envelope(
            "memory",
            "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(root), None


def _load_program_config(program_root: Path) -> dict:
    # Same "read generically, tolerate absence" best-effort load every
    # other CLI group's own private copy of this helper already uses
    # (trialerror.cli.ingest._load_program_config, trialerror.cli.lit._load_config,
    # ...) -- not refactored into one shared helper here (out of this
    # build's lane; see the build report's deviations).
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
        "memory",
        "no_action",
        "specify an action: search|put|sync-export|sync-import|merge",
        next_actions=[next_action(["trialerror", "memory", "--help"], "list memory actions")],
    )


def _run_search(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.memory_item_id:
            row = get_item(store, args.memory_item_id)
            if row is None:
                return error_envelope("memory search", "not_found", f"no memory_item {args.memory_item_id!r}")
            return ok_envelope("memory search", result={"item": row})

        if args.boot_bundle_mode:
            budget = args.token_budget if args.token_budget is not None else None
            kwargs = {"account_id": args.account_id}
            if budget is not None:
                kwargs["token_budget"] = budget
            bundle = boot_bundle(store, **kwargs)
            return ok_envelope("memory search", result=bundle)

        status = args.status if args.status != "" else None
        rows = search_items(
            store,
            query=args.query,
            tier=args.tier,
            kind=args.kind,
            account_id=args.account_id,
            status=status,
            limit=args.limit,
        )
        return ok_envelope("memory search", result={"items": rows, "count": len(rows)})
    finally:
        store.close()


def _run_put(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        row = put_item(
            store,
            key=args.key,
            tier=args.tier,
            kind=args.kind,
            body=args.body,
            account_id=args.account_id,
            l0_abstract=args.l0_abstract,
            ts=args.ts,
        )
    except (StoreError, ValueError) as exc:
        return error_envelope("memory put", "put_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "memory put",
        result={"item": row},
        next_actions=[next_action(["trialerror", "memory", "search", "--id", row["memory_item_id"]], "confirm the write")],
    )


def _run_sync_export(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        # the import-design notes (internal, not in this export) Sec 5 knob #6: [paths].memory_dir overrides
        # the "<program-root>/memory" default -- --out-dir, when given,
        # still wins outright (design's own "still overridable per-call").
        if args.out_dir:
            out_dir = Path(args.out_dir)
        else:
            from trialerror.util.config import resolve_configured_path

            out_dir = resolve_configured_path(
                store.program_root, _load_program_config(store.program_root), "memory_dir", "memory"
            )
        result = export_memory(store, out_dir=out_dir, account_id=args.account_id)
    finally:
        store.close()
    return ok_envelope(
        "memory sync-export",
        result=result,
        next_actions=[next_action(["trialerror", "memory", "sync-import", "--in-dir", result["out_dir"]], "round-trip check")],
    )


def _run_sync_import(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.in_dir:
            in_dir = Path(args.in_dir)
        else:
            from trialerror.util.config import resolve_configured_path

            in_dir = resolve_configured_path(
                store.program_root, _load_program_config(store.program_root), "memory_dir", "memory"
            )
        if not in_dir.is_dir():
            return error_envelope("memory sync-import", "in_dir_not_found", f"no such directory: {in_dir}")
        try:
            result = import_memory(store, in_dir=in_dir)
        except ValueError as exc:
            return error_envelope("memory sync-import", "malformed_export", str(exc))
    finally:
        store.close()
    envelope_result = result.to_dict()
    next_actions = []
    if envelope_result["conflicts"]:
        next_actions.append(
            next_action(["trialerror", "memory", "merge"], "list the conflict group(s) this import surfaced")
        )
    return ok_envelope("memory sync-import", result=envelope_result, next_actions=next_actions)


def _run_merge(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.group_id or args.keep:
            if not (args.group_id and args.keep):
                return error_envelope(
                    "memory merge", "incomplete_resolution", "--group and --keep must be given together"
                )
            try:
                result = resolve_conflict(store, group_id=args.group_id, keep=args.keep)
            except ValueError as exc:
                return error_envelope("memory merge", "resolve_refused", str(exc))
            return ok_envelope("memory merge", result=result)

        conflicts = list_conflicts(store, account_id=args.account_id)
        return ok_envelope("memory merge", result={"conflicts": conflicts, "count": len(conflicts)})
    finally:
        store.close()
