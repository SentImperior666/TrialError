"""``trialerror lens`` — AMENDMENT-3 ideation machinery, generalized (design
Section 5.2: "``lens`` | roster, stratify, assign, log | AMENDMENT-3
machinery generalized"; this build's brief additionally names ``export`` as
a convention example — implemented here as a fifth, separate action). Thin
CLI wrapper over ``trialerror.lens.*`` — all logic lives there; this module only
parses argv and shapes the AgentEnvelope (same split ``trialerror/cli/artifact.py``
documents for M10).

Flat, single-level subcommands throughout (matching ``trialerror/cli/budget.py``'s
own established convention, e.g. its ``pools`` action: "list pools, or
``--create`` a new one" — one action, a flag switches mode — rather than a
second nested subparser level, which no other group in this codebase uses):
``roster`` lists a round's roster by default, or adds one lens when
``--add`` is given alongside the lens fields.

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by M13.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.lens.assign import list_assignments, run_assignment
from trialerror.lens.errors import LensError
from trialerror.lens.export import export_launch_bookable
from trialerror.lens.roster import SEATS, add_lens, list_roster
from trialerror.lens.stratify import score_candidates, stratify
from trialerror.lens.vectors import fetch_doc_vectors
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "lens"
HELP = "Ideation lens tooling: roster, stratify, assign, log, export (AMENDMENT-3 generalized)."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root the
    # top-level parser resolved.
    parser.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="program scaffold root (default: discover trialerror.toml upward from CWD)"
    )
    parser.set_defaults(handler=_run_no_action)
    sub = parser.add_subparsers(dest="action", metavar="<action>")

    roster = sub.add_parser("roster", help="list a round's roster, or --add one lens to it")
    roster.add_argument("--round-id", required=True)
    roster.add_argument("--add", action="store_true")
    roster.add_argument("--lens-name", default=None)
    roster.add_argument("--vantage", default=None)
    roster.add_argument("--model-class", default=None)
    roster.add_argument("--seat", default="standard", choices=list(SEATS))
    roster.set_defaults(handler=_run_roster)

    stratify_p = sub.add_parser("stratify", help="dry-run: score + tercile-cut a candidate pool (no write)")
    _add_stratify_args(stratify_p)
    stratify_p.set_defaults(handler=_run_stratify)

    assign = sub.add_parser("assign", help="stratify + seeded quota draw + write lens_assignment rows")
    _add_stratify_args(assign)
    assign.add_argument("--round-id", required=True)
    assign.add_argument("--roster-id", action="append", default=None, dest="roster_ids", metavar="ROSTER_ID", help="restrict to these lenses (default: every lens in the round's roster)")
    assign.add_argument("--slices-per-lens", type=int, required=True)
    assign.add_argument("--seed", required=True)
    assign.add_argument("--weights", default="40,40,20", help="comma-separated near,moderate,far percentages")
    assign.add_argument("--far-floor", type=int, default=2)
    assign.add_argument("--inter-cluster-mandate", action="store_true")
    assign.add_argument("--home-cluster", default=None)
    assign.add_argument("--launch-id", default=None)
    assign.set_defaults(handler=_run_assign)

    log = sub.add_parser("log", help="list logged lens_assignment rows for a round")
    log.add_argument("--round-id", required=True)
    log.set_defaults(handler=_run_log)

    export = sub.add_parser("export", help="launch-bookable rows for trialerror.budget.book_launch")
    export.add_argument("--round-id", required=True)
    export.set_defaults(handler=_run_export)

    return parser


def _add_stratify_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model-key", required=True, help="embedding model_key (matches vec_chunks__<model_key>)")
    p.add_argument("--home", action="append", default=[], dest="home_doc_ids", metavar="DOC_ID", required=True)
    p.add_argument("--candidate", action="append", default=[], dest="candidate_doc_ids", metavar="DOC_ID", required=True)
    p.add_argument("--cluster-of", default=None, help='JSON object string: {"doc_id": "cluster_id", ...}')


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = getattr(args, "program_root", None) or find_program_root()
    if root is None:
        return None, error_envelope(
            "lens", "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(Path(root)), None


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(
        "lens", "no_action", "specify an action: roster|stratify|assign|log|export",
        next_actions=[next_action(["trialerror", "lens", "--help"], "list lens actions")],
    )


def _run_roster(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.add:
            missing = [f for f in ("lens_name", "vantage", "model_class") if getattr(args, f) is None]
            if missing:
                return error_envelope(
                    "lens roster", "missing_fields",
                    f"--add requires {['--' + m.replace('_', '-') for m in missing]!r}",
                )
            row = add_lens(
                store, round_id=args.round_id, lens_name=args.lens_name, vantage=args.vantage,
                model_class=args.model_class, seat=args.seat,
            )
            return ok_envelope(
                "lens roster", result=row,
                next_actions=[next_action(["trialerror", "lens", "roster", "--round-id", args.round_id], "see the round's roster")],
            )
        rows = list_roster(store, round_id=args.round_id)
        return ok_envelope("lens roster", result={"roster": rows, "count": len(rows)})
    except (StoreError, ValueError) as exc:
        return error_envelope("lens roster", "roster_refused", str(exc))
    finally:
        store.close()


def _fetch_candidates_and_home(store: Store, args: argparse.Namespace):
    home = fetch_doc_vectors(store, model_key=args.model_key, doc_ids=args.home_doc_ids)
    candidates = fetch_doc_vectors(store, model_key=args.model_key, doc_ids=args.candidate_doc_ids)
    cluster_of = json.loads(args.cluster_of) if getattr(args, "cluster_of", None) else None
    return home, candidates, cluster_of


def _run_stratify(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        home, candidates, cluster_of = _fetch_candidates_and_home(store, args)
        scores = score_candidates(candidates, home)
        stratified = stratify(scores, cluster_of=cluster_of)
    except (LensError, json.JSONDecodeError) as exc:
        return error_envelope("lens stratify", "stratify_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "lens stratify",
        result={"candidates": [sc.to_dict() for sc in stratified], "count": len(stratified)},
    )


def _run_assign(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        roster = list_roster(store, round_id=args.round_id)
        if args.roster_ids:
            wanted = set(args.roster_ids)
            roster = [r for r in roster if r["roster_id"] in wanted]
        if not roster:
            return error_envelope(
                "lens assign", "empty_roster",
                f"round_id={args.round_id!r} has no matching roster rows to assign",
                next_actions=[next_action(["trialerror", "lens", "roster", "--add"], "add a lens to this round first")],
            )
        cluster_of = json.loads(args.cluster_of) if args.cluster_of else None
        weights = tuple(int(w) for w in args.weights.split(","))
        result = run_assignment(
            store,
            round_id=args.round_id,
            model_key=args.model_key,
            home_doc_ids=args.home_doc_ids,
            candidate_doc_ids=args.candidate_doc_ids,
            lenses=[{"roster_id": r["roster_id"]} for r in roster],
            slices_per_lens=args.slices_per_lens,
            seed=args.seed,
            weights=weights,
            far_floor=args.far_floor,
            inter_cluster_mandate=args.inter_cluster_mandate,
            cluster_of=cluster_of,
            home_cluster=args.home_cluster,
            launch_id=args.launch_id,
        )
    except LensError as exc:
        return error_envelope("lens assign", "assign_refused", str(exc))
    except (StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("lens assign", "assign_error", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "lens assign",
        result={"rows": result["rows"], "count": len(result["rows"])},
        next_actions=[next_action(["trialerror", "lens", "log", "--round-id", args.round_id], "see the logged assignment")],
    )


def _run_log(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = list_assignments(store, round_id=args.round_id)
    finally:
        store.close()
    return ok_envelope("lens log", result={"assignments": rows, "count": len(rows)})


def _run_export(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = export_launch_bookable(store, round_id=args.round_id)
    finally:
        store.close()
    return ok_envelope(
        "lens export", result={"bookable": rows, "count": len(rows)},
        next_actions=[next_action(["trialerror", "budget", "book"], "book each row via trialerror.budget.book_launch")],
    )
