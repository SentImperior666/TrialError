"""``trialerror vastai ...``: rent a vast.ai GPU for the embed offload queue.

Design: ``docs/VASTAI_EMBED_DESIGN.md``. Commands:

    plan         read-only: one offer search, print selection/TTL/worst-case $
    run          create -> embed the pending markers -> destroy (always)
    reap         destroy TrialError instances past deadline or orphaned
    approve-high OPERATOR ONLY, interactive terminal: sign a short-lived
                 high-tier approval into keys/

There is intentionally no command to keep, reuse or extend an instance.
Auto-discovered by ``trialerror.cli.discover_groups``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.util.config import CONFIG_FILENAME, find_program_root, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "vastai"
HELP = "Rent a vast.ai GPU for pending embed jobs (plan/run/reap); create -> run -> destroy, never kept alive."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="vastai_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--program-root", default=argparse.SUPPRESS, help="program root (default: discovered from CWD)")
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root")

    p_plan = sub.add_parser("plan", help="read-only: search offers and show what a run would rent and cost")
    _common(p_plan)
    p_plan.add_argument("--max-jobs", type=int, default=None)
    p_plan.set_defaults(handler=_cmd_plan)

    p_run = sub.add_parser("run", help="rent one GPU, embed pending markers, destroy it")
    _common(p_run)
    p_run.add_argument("--max-jobs", type=int, default=None)
    p_run.set_defaults(handler=_cmd_run)

    p_reap = sub.add_parser("reap", help="destroy TrialError-tagged instances past their deadline or orphaned")
    _common(p_reap)
    p_reap.add_argument("--dry-run", action="store_true")
    p_reap.set_defaults(handler=_cmd_reap)

    p_appr = sub.add_parser("approve-high", help="OPERATOR ONLY (interactive terminal): approve the high tier for <= 24 h")
    _common(p_appr)
    p_appr.add_argument("--hours", type=float, required=True)
    p_appr.add_argument("--max-job-usd", type=float, required=True)
    p_appr.set_defaults(handler=_cmd_approve_high)
    return parser


def _load(args: argparse.Namespace, action: str):
    root = Path(args.program_root) if getattr(args, "program_root", None) else find_program_root()
    if root is None:
        return None, None, error_envelope(action, "no_program_root", "no --program-root and no trialerror.toml above CWD")
    cfg_path = root / CONFIG_FILENAME
    raw = load_config(cfg_path).raw if cfg_path.is_file() else {}
    return root, raw, None


def _refusal(action: str, exc: Exception) -> dict:
    return error_envelope(action, "refused", str(exc))


def _cmd_plan(args: argparse.Namespace) -> dict:
    from trialerror.vastai.api import VastApiError
    from trialerror.vastai.guard import HighTierRefused
    from trialerror.vastai.runner import VastRunRefused, prepare_run
    from trialerror.vastai.tiers import PlanRefused, VastConfigError

    root, raw, err = _load(args, "vastai.plan")
    if err:
        return err
    try:
        prep = prepare_run(root, raw, max_jobs=args.max_jobs)
    except (VastRunRefused, PlanRefused, HighTierRefused, VastConfigError, VastApiError) as exc:
        return _refusal("vastai.plan", exc)
    return ok_envelope(
        "vastai.plan",
        result={"plan": prep["plan"].as_dict(), "jobs": [j for j, _ in prep["jobs"]], "notes": prep["cfg"].notes,
                "note": "prices are live offers; throughput/TTL are estimates (docs/VASTAI_EMBED_DESIGN.md 3)"},
        next_actions=[next_action(["trialerror", "vastai", "run"], "rent, embed, destroy")],
    )


def _cmd_run(args: argparse.Namespace) -> dict:
    from trialerror.stores.store import open_store
    from trialerror.vastai.api import VastApiError
    from trialerror.vastai.guard import HighTierRefused
    from trialerror.vastai.runner import VastRunRefused, run_vastai
    from trialerror.vastai.tiers import PlanRefused, VastConfigError

    root, raw, err = _load(args, "vastai.run")
    if err:
        return err
    store = open_store(root, platform_root=getattr(args, "platform_root", None))
    try:
        summary = run_vastai(root, raw, store=store, max_jobs=args.max_jobs)
    except (VastRunRefused, PlanRefused, HighTierRefused, VastConfigError, VastApiError) as exc:
        return _refusal("vastai.run", exc)
    finally:
        store.close()
    return ok_envelope(
        "vastai.run",
        result=summary,
        next_actions=[next_action(["trialerror", "offload", "kick"], "let the parked embed jobs pick up the published vectors")],
    )


def _cmd_reap(args: argparse.Namespace) -> dict:
    from trialerror.vastai.api import VastApiError, VastClient
    from trialerror.vastai.reaper import reap
    from trialerror.vastai.tiers import VastConfigError, load_vast_config

    root, raw, err = _load(args, "vastai.reap")
    if err:
        return err
    try:
        cfg = load_vast_config(raw, root)
        result = reap(VastClient(cfg.api_key_path), root, dry_run=args.dry_run)
    except (VastApiError, VastConfigError) as exc:
        return error_envelope("vastai.reap", "vastai_error", str(exc))
    return ok_envelope("vastai.reap", result={"reaped": result, "count": len(result), "dry_run": args.dry_run})


def _cmd_approve_high(args: argparse.Namespace) -> dict:
    from trialerror.vastai.guard import HighTierRefused, mint_high_tier_approval
    from trialerror.vastai.tiers import load_vast_config

    root, raw, err = _load(args, "vastai.approve-high")
    if err:
        return err
    try:
        cfg = load_vast_config(raw, root)
        path = mint_high_tier_approval(root, cfg.api_key_path, hours=args.hours, max_job_usd=args.max_job_usd)
    except HighTierRefused as exc:
        return _refusal("vastai.approve-high", exc)
    return ok_envelope("vastai.approve-high", result={"approval": str(path)})
