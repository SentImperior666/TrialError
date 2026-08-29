"""``trialerror jobs`` -- the durable execution ledger's CLI surface. Design
Section 5.2 jobs row: "list, start-worker, tick, pause, resume, logs |
detached worker mgmt."

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/jobs.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touched
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from trialerror.jobs import ledger
from trialerror.jobs.errors import JobError
from trialerror.jobs.registry import discover_and_register_handlers
from trialerror.jobs.worker import run_loop, run_one, spawn_worker
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "jobs"
HELP = "Durable execution ledger: list/claim/pause/resume jobs; launch detached workers."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="jobs_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so
        # an unset value here never overwrites the global --program-root/
        # --platform-root the top-level parser resolved.
        p.add_argument(
            "--program-root", default=argparse.SUPPRESS, help="program scaffold root (default: discovered from CWD via trialerror.toml)"
        )
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")

    p_list = sub.add_parser("list", help="list jobs, optionally filtered by state/kind")
    _common(p_list)
    p_list.add_argument("--state", default=None)
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--limit", type=int, default=100)
    p_list.set_defaults(handler=_cmd_list)

    p_start = sub.add_parser("start-worker", help="launch a worker (detached by default; --foreground to run inline)")
    _common(p_start)
    p_start.add_argument("--job-id", default=None, help="claim this specific job id (create-if-missing with --kind/--payload)")
    p_start.add_argument("--kind", default=None, help="kind for --job-id when the job doesn't exist yet")
    p_start.add_argument("--payload", default=None, help="JSON payload for --job-id when the job doesn't exist yet")
    p_start.add_argument("--kinds", default=None, help="comma-separated kind filter for open-queue polling")
    p_start.add_argument("--mode", choices=["once", "loop"], default="once")
    p_start.add_argument("--lease-s", type=int, default=None, help="override the lease duration in seconds (default: 900)")
    p_start.add_argument("--poll-interval-s", type=float, default=2.0)
    p_start.add_argument("--max-idle-polls", type=int, default=3)
    p_start.add_argument("--max-iterations", type=int, default=None)
    p_start.add_argument(
        "--handler-module", action="append", default=None, help="extra module to import before running (repeatable)"
    )
    p_start.add_argument(
        "--foreground",
        action="store_true",
        help="run inline in THIS process instead of spawning a detached one "
        "(this is what the detached child itself invokes)",
    )
    p_start.set_defaults(handler=_cmd_start_worker)

    p_tick = sub.add_parser("tick", help="reclaim jobs whose lease has expired (crashed-worker recovery)")
    _common(p_tick)
    p_tick.set_defaults(handler=_cmd_tick)

    p_pause = sub.add_parser("pause", help="cooperatively pause a job (its worker stops at its next heartbeat)")
    _common(p_pause)
    p_pause.add_argument("job_id")
    p_pause.set_defaults(handler=_cmd_pause)

    p_resume = sub.add_parser("resume", help="make a paused job claimable again (does not itself spawn a worker)")
    _common(p_resume)
    p_resume.add_argument("job_id")
    p_resume.set_defaults(handler=_cmd_resume)

    p_logs = sub.add_parser("logs", help="show a job's ledger event history")
    _common(p_logs)
    p_logs.add_argument("job_id")
    p_logs.add_argument("--limit", type=int, default=100)
    p_logs.set_defaults(handler=_cmd_logs)

    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            f"jobs.{args.jobs_cmd}",
            "no_program_root",
            "no --program-root given and no trialerror.toml found walking up from CWD",
        )
    store = open_store(program_root, platform_root=args.platform_root)
    return store, None


def _cmd_list(args: argparse.Namespace) -> dict:
    store, err = _open(args)
    if err is not None:
        return err
    try:
        jobs = ledger.list_jobs(store, state=args.state, kind=args.kind, limit=args.limit)
        return ok_envelope("jobs.list", result={"jobs": jobs, "count": len(jobs)})
    finally:
        store.close()


def _cmd_tick(args: argparse.Namespace) -> dict:
    store, err = _open(args)
    if err is not None:
        return err
    try:
        reclaimed = ledger.sweep_expired_leases(store)
        return ok_envelope(
            "jobs.tick",
            result={"reclaimed": reclaimed, "count": len(reclaimed)},
            next_actions=(
                [next_action(["trialerror", "jobs", "start-worker"], "relaunch a worker to pick up reclaimed jobs")]
                if reclaimed
                else []
            ),
        )
    finally:
        store.close()


def _cmd_pause(args: argparse.Namespace) -> dict:
    store, err = _open(args)
    if err is not None:
        return err
    try:
        row = ledger.pause(store, args.job_id)
        return ok_envelope("jobs.pause", result=row)
    except JobError as exc:
        return error_envelope("jobs.pause", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_resume(args: argparse.Namespace) -> dict:
    store, err = _open(args)
    if err is not None:
        return err
    try:
        row = ledger.resume(store, args.job_id)
        return ok_envelope(
            "jobs.resume",
            result=row,
            next_actions=[
                next_action(
                    ["trialerror", "jobs", "start-worker", "--job-id", args.job_id],
                    "relaunch a worker for the resumed job",
                )
            ],
        )
    except JobError as exc:
        return error_envelope("jobs.resume", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_logs(args: argparse.Namespace) -> dict:
    store, err = _open(args)
    if err is not None:
        return err
    try:
        events = ledger.list_events(store, args.job_id, limit=args.limit)
        return ok_envelope("jobs.logs", result={"job_id": args.job_id, "events": events})
    finally:
        store.close()


def _cmd_start_worker(args: argparse.Namespace) -> dict:
    kinds = args.kinds.split(",") if args.kinds else None
    payload = json.loads(args.payload) if args.payload else None

    if args.foreground:
        store, err = _open(args)
        if err is not None:
            return err
        try:
            discover_and_register_handlers()
            for mod in args.handler_module or []:
                importlib.import_module(mod)
            lease_kwargs = {"lease_s": args.lease_s} if args.lease_s is not None else {}
            if args.mode == "once":
                result = run_one(store, job_id=args.job_id, kind=args.kind, payload=payload, kinds=kinds, **lease_kwargs)
                return ok_envelope("jobs.start-worker", result=result)
            results = run_loop(
                store,
                kinds=kinds,
                poll_interval_s=args.poll_interval_s,
                max_idle_polls=args.max_idle_polls,
                max_iterations=args.max_iterations,
                **lease_kwargs,
            )
            return ok_envelope("jobs.start-worker", result={"results": results, "count": len(results)})
        except JobError as exc:
            return error_envelope("jobs.start-worker", type(exc).__name__, str(exc))
        finally:
            store.close()

    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "jobs.start-worker", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )
    handle = spawn_worker(
        program_root=program_root,
        platform_root=args.platform_root,
        kinds=kinds,
        job_id=args.job_id,
        kind=args.kind,
        payload=payload,
        mode=args.mode,
        lease_s=args.lease_s,
        poll_interval_s=args.poll_interval_s,
        max_idle_polls=args.max_idle_polls,
        max_iterations=args.max_iterations,
        extra_handler_modules=args.handler_module,
    )
    return ok_envelope(
        "jobs.start-worker",
        result={"pid": handle.pid, "argv": handle.argv, "log_path": str(handle.log_path)},
        next_actions=[
            next_action(["trialerror", "jobs", "list"], "check claimed job state"),
            next_action(["trialerror", "jobs", "logs", args.job_id or "<job-id>"], "tail the ledger's event history for a job"),
        ],
    )
