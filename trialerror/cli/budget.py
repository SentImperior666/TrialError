"""``trialerror budget`` - pools, book/reconcile/status/calibrate, snapshot
ingest (design Section 5.2 CLI table: "book, reconcile, status, pools,
snapshot-ingest, calibrate | book returns launch_id token for the spawn
gate"). Business logic lives in :mod:`trialerror.budget.pools`; this module is
argv parsing + envelope wrapping only, per the M3 build brief's CLI
contract (handlers return envelopes).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.budget.errors import BudgetError, ModelPolicyViolationError, NoOpenSessionError, UnknownOverrideRulingError
from trialerror.budget.pools import (
    DEFAULT_BOOKING_TTL_S,
    book_launch,
    budget_status,
    calibrate as calibrate_,
    create_pool,
    list_pools,
    reconcile_launch,
    snapshot_ingest,
    tree_rollup,
)
from trialerror.stores.store import open_store
from trialerror.util.config import ConfigError, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "budget"
HELP = "Budget pools, bookings, reconciliation, calibration (the spawn gate's data side)."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root/
    # --platform-root the top-level parser resolved.
    parser.add_argument("--program-root", default=argparse.SUPPRESS, help="program scaffold root (default: CWD)")
    parser.add_argument(
        "--platform-root", default=argparse.SUPPRESS, help="override the platform root (default: TRIALERROR_PLATFORM_ROOT or ~/.trialerror)"
    )
    parser.set_defaults(handler=run)
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    book = sub.add_parser("book", help="create a PROVISIONAL booking; returns a launch_id token")
    book.add_argument("--session-id", required=True)
    book.add_argument("--program-id", required=True)
    book.add_argument("--agent-kind", required=True)
    book.add_argument("--model-class", required=True, choices=["top", "mid", "small"])
    book.add_argument("--model", required=True)
    book.add_argument("--purpose", required=True)
    book.add_argument("--est-tokens", required=True, type=int)
    book.add_argument("--booking-ttl-s", type=int, default=DEFAULT_BOOKING_TTL_S)
    book.add_argument("--parent-launch", default=None)
    book.add_argument("--workpackage", default=None)
    book.add_argument("--override-ruling-id", default=None)
    book.set_defaults(handler=_run_book)

    reconcile = sub.add_parser("reconcile", help="settle actuals for a launch_id")
    reconcile.add_argument("--launch-id", required=True)
    reconcile.add_argument("--actual-tokens", required=True, type=int)
    reconcile.add_argument("--reconcile-source", default="manual", choices=["transcript", "estimate", "manual"])
    reconcile.set_defaults(handler=_run_reconcile)

    status = sub.add_parser("status", help="pools, headroom, multiplier, DEFER advisories for an account")
    status.add_argument("--account-id", required=True)
    status.add_argument("--model-class", default=None, choices=["top", "mid", "small"])
    status.set_defaults(handler=_run_status)

    pools = sub.add_parser("pools", help="list pools, or --create a new one")
    pools.add_argument("--account-id", default=None, help="filter (list mode) / owner (create mode)")
    pools.add_argument("--create", action="store_true")
    pools.add_argument("--model-class", default=None, choices=["top", "mid", "small"])
    pools.add_argument("--period", default=None, choices=["weekly", "monthly"])
    pools.add_argument("--cap-tokens", type=int, default=None)
    pools.add_argument("--period-start", default=None)
    pools.add_argument("--billed-multiplier", type=float, default=2.75)
    pools.add_argument("--soft-pct", type=float, default=95)
    pools.add_argument("--hard-pct", type=float, default=100)
    pools.set_defaults(handler=_run_pools)

    snap = sub.add_parser("snapshot-ingest", help="record a quota_snapshot (screenshot = ground truth)")
    snap.add_argument("--account-id", required=True)
    snap.add_argument("--source", required=True, choices=["screenshot", "api", "estimate"])
    snap.add_argument("--payload", required=True, help='JSON, e.g. \'{"model_class":"top","used_tokens":12345}\'')
    snap.set_defaults(handler=_run_snapshot_ingest)

    calib = sub.add_parser("calibrate", help="derive billed_multiplier from a screenshot snapshot pair")
    calib.add_argument("--account-id", required=True)
    calib.add_argument("--model-class", required=True, choices=["top", "mid", "small"])
    calib.add_argument("--window", default="7d")
    calib.set_defaults(handler=_run_calibrate)

    rollup = sub.add_parser("rollup", help="sum est/actual tokens over a launch tree (parent_launch)")
    rollup.add_argument("--launch-id", required=True)
    rollup.set_defaults(handler=_run_rollup)

    quota = sub.add_parser(
        "quota",
        help="plan rate-limit windows captured from the Claude Code statusLine feed (USER_SETUP.md wires it)",
    )
    quota.add_argument("--quota-dir", default=None, help="override the capture dir (default: TRIALERROR_QUOTA_DIR or ~/.trialerror/quota)")
    quota.add_argument("--fresh-within-s", type=int, default=None, help="freshness bar in seconds (default 900)")
    quota.add_argument("--ingest", action="store_true", help="also record the reading as a quota_snapshot(source=api) row")
    quota.add_argument("--account-id", default=None, help="required with --ingest")
    quota.set_defaults(handler=_run_quota)

    return parser


def _open_store(args: argparse.Namespace):
    program_root = Path(args.program_root) if args.program_root else Path.cwd()
    platform_root = Path(args.platform_root) if args.platform_root else None
    return open_store(program_root, platform_root=platform_root)


def _load_policy(program_root: Path) -> dict[str, str] | None:
    """Best-effort ``[models]`` policy load - a missing/invalid
    ``trialerror.toml`` means "no policy configured", not a CLI failure (design
    Section 3.2: ``trialerror.toml`` is per-program and optional at this layer;
    M7's license posture / M1's id-prefix pinning are the same "read
    generically, tolerate absence" convention)."""
    try:
        config = load_config(program_root / "trialerror.toml")
    except ConfigError:
        return None
    return dict(config.models) if config.models else None


def _run_book(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        policy = _load_policy(store.program_root)
        result = book_launch(
            store,
            session_id=args.session_id,
            program_id=args.program_id,
            agent_kind=args.agent_kind,
            model_class=args.model_class,
            model=args.model,
            purpose=args.purpose,
            est_tokens=args.est_tokens,
            booking_ttl_s=args.booking_ttl_s,
            parent_launch=args.parent_launch,
            workpackage=args.workpackage,
            policy=policy,
            override_ruling_id=args.override_ruling_id,
        )
    except NoOpenSessionError as exc:
        return error_envelope(
            "budget book",
            "no_open_session",
            str(exc),
            next_actions=[next_action(["trialerror", "session", "boot"], "boot a session before booking")],
        )
    except (ModelPolicyViolationError, UnknownOverrideRulingError) as exc:
        return error_envelope("budget book", "model_policy_violation", str(exc))
    finally:
        store.close()

    payload = result.to_dict()
    if not result.ok:
        return error_envelope(
            "budget book",
            f"book_{result.state.lower()}",
            result.reason or f"booking not created as PROVISIONAL (state={result.state})",
            details=payload,
        )
    return ok_envelope(
        "budget book",
        result=payload,
        next_actions=[
            next_action(
                ["trialerror", "budget", "status", "--account-id", result.account_id],
                "check pool headroom",
            )
        ],
        meta={"prompt_fragment": f"launch_id: {result.launch_id}"},
    )


def _run_reconcile(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        result = reconcile_launch(
            store,
            launch_id=args.launch_id,
            actual_tokens=args.actual_tokens,
            reconcile_source=args.reconcile_source,
        )
    except BudgetError as exc:
        return error_envelope("budget reconcile", "reconcile_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("budget reconcile", result=result)


def _run_status(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        result = budget_status(store, account_id=args.account_id, model_class=args.model_class)
    finally:
        store.close()
    return ok_envelope("budget status", result=result)


def _run_pools(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        if args.create:
            missing = [
                flag
                for flag, val in (
                    ("--account-id", args.account_id),
                    ("--model-class", args.model_class),
                    ("--period", args.period),
                    ("--cap-tokens", args.cap_tokens),
                )
                if val is None
            ]
            if missing:
                return error_envelope(
                    "budget pools",
                    "missing_arguments",
                    f"--create requires {', '.join(missing)}",
                )
            row = create_pool(
                store,
                account_id=args.account_id,
                model_class=args.model_class,
                period=args.period,
                cap_tokens=args.cap_tokens,
                period_start=args.period_start,
                billed_multiplier=args.billed_multiplier,
                soft_pct=args.soft_pct,
                hard_pct=args.hard_pct,
            )
            return ok_envelope("budget pools", result={"created": row})
        rows = list_pools(store, account_id=args.account_id)
        return ok_envelope("budget pools", result={"pools": rows})
    finally:
        store.close()


def _run_snapshot_ingest(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        store.close()
        return error_envelope("budget snapshot-ingest", "invalid_payload", f"--payload is not valid JSON: {exc}")
    try:
        row = snapshot_ingest(store, account_id=args.account_id, source=args.source, payload=payload)
    finally:
        store.close()
    return ok_envelope("budget snapshot-ingest", result=row)


def _run_calibrate(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        row = calibrate_(store, account_id=args.account_id, model_class=args.model_class, window=args.window)
    except BudgetError as exc:
        return error_envelope("budget calibrate", "calibrate_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("budget calibrate", result=row)


def _run_rollup(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        result = tree_rollup(store, args.launch_id)
    except BudgetError as exc:
        return error_envelope("budget rollup", "unknown_launch_id", str(exc))
    finally:
        store.close()
    return ok_envelope("budget rollup", result=result)


def _run_quota(args: argparse.Namespace) -> dict:
    from trialerror.budget.quota import DEFAULT_FRESH_WITHIN_S, quota_status

    fresh_within = args.fresh_within_s if args.fresh_within_s is not None else DEFAULT_FRESH_WITHIN_S
    status = quota_status(args.quota_dir, fresh_within_s=fresh_within)
    if not args.ingest:
        return ok_envelope("budget quota", result=status)
    if not args.account_id:
        return error_envelope("budget quota", "missing_account", "--ingest requires --account-id")
    if not status["available"]:
        return error_envelope("budget quota", "no_snapshot", "nothing captured yet — nothing to ingest")
    store = _open_store(args)
    try:
        row = snapshot_ingest(
            store,
            account_id=args.account_id,
            source="api",
            payload={"windows": status["windows"], "captured_ts": status["captured_ts"], "via": "statusline"},
            ts=status["captured_ts"],
        )
    finally:
        store.close()
    return ok_envelope("budget quota", result={"quota": status, "ingested": row})


def run(args: argparse.Namespace) -> dict:
    """The ``budget`` group's own default handler - reached only when no
    subcommand was given (each subcommand's ``set_defaults(handler=...)``
    overrides this for its own parse)."""
    return error_envelope(
        "budget",
        "no_subcommand",
        "specify a subcommand: book, reconcile, status, pools, snapshot-ingest, calibrate, rollup, quota",
    )
