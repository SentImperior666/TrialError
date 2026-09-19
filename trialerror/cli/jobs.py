"""``trialerror jobs`` -- the durable execution ledger's CLI surface. Design
Section 5.2 jobs row: "list, start-worker, tick, pause, resume, logs |
detached worker mgmt."

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/jobs.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touched
``trialerror/cli/__init__.py``.

``kick`` is the seventh verb, added by mining adoption rowboat-F8: it
writes the wake token :func:`trialerror.jobs.worker.run_loop` naps on, so
"a job was just enqueued, stop waiting" costs one command instead of a
poll interval. It is deliberately NOT a spawn -- it wakes workers that
already exist and does nothing at all when none are running (which is why
it needs no launch booking).

``abandon`` (lane FB-3) and ``retry`` (lane FB-8a) are the two ends of the
settled-unsuccessful state: one puts a job there on purpose, the other is
the sanctioned way back out of it once the thing that broke has been fixed.
Between them there was no way back at all -- ``resume`` covers ``paused``
only -- and the alternative an operator actually reached for was editing
``jobs.db`` by hand.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from trialerror.jobs import ledger
from trialerror.jobs.errors import JobError
from trialerror.jobs.registry import discover_and_register_handlers
from trialerror.jobs.worker import DEFAULT_JITTER_FRACTION, kick, run_loop, run_one, spawn_worker
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "jobs"
HELP = (
    "Durable execution ledger: list/claim/pause/resume/retry/kick jobs; launch detached workers."
)


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
        "--jitter-frac",
        type=float,
        default=None,
        dest="jitter_frac",
        help=f"poll-window jitter as a +/- fraction of --poll-interval-s (default {DEFAULT_JITTER_FRACTION}; 0 disables)",
    )
    p_start.add_argument(
        "--no-wake-signal",
        action="store_true",
        dest="no_wake_signal",
        help="ignore the wake-signal file ('trialerror jobs kick' will not shorten this worker's naps)",
    )
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

    p_kick = sub.add_parser(
        "kick", help="wake any napping worker immediately (writes the wake-signal token; does not spawn)"
    )
    _common(p_kick)
    p_kick.set_defaults(handler=_cmd_kick)

    p_pause = sub.add_parser("pause", help="cooperatively pause a job (its worker stops at its next heartbeat)")
    _common(p_pause)
    p_pause.add_argument("job_id")
    p_pause.set_defaults(handler=_cmd_pause)

    p_resume = sub.add_parser("resume", help="make a paused job claimable again (does not itself spawn a worker)")
    _common(p_resume)
    p_resume.add_argument("job_id")
    p_resume.set_defaults(handler=_cmd_resume)

    p_abandon = sub.add_parser(
        "abandon",
        help="settle a job as abandoned with a stated reason (refused while a worker holds it)",
    )
    _common(p_abandon)
    p_abandon.add_argument("job_id")
    p_abandon.add_argument(
        "--reason",
        required=True,
        help="why this job is being cancelled -- recorded in last_error and on the ledger event, "
             "because a terminal row with no stated cause is the kind the next person re-runs",
    )
    p_abandon.set_defaults(handler=_cmd_abandon)

    p_retry = sub.add_parser(
        "retry",
        help="return a failed/abandoned job to the queue with its retry budget restored "
        "(and its offload marker with it)",
    )
    _common(p_retry)
    p_retry.add_argument("job_id")
    p_retry.add_argument(
        "--reason",
        required=True,
        help="why this settled job deserves another run -- recorded on the ledger event and, "
        "for an offloaded job, in the evidence directory the terminal attempt is kept in. "
        "A row that came back from terminal with no stated cause is one nobody can audit",
    )
    p_retry.add_argument(
        "--by-launch",
        default=None,
        dest="by_launch",
        help="the launch this retry is booked under (recorded on the ledger event)",
    )
    p_retry.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        dest="max_attempts",
        help=f"give the job a new attempt budget ({ledger.RETRY_MAX_ATTEMPTS_RANGE[0]}-"
        f"{ledger.RETRY_MAX_ATTEMPTS_RANGE[1]}); unchanged when omitted",
    )
    p_retry.add_argument(
        "--clear-checkpoint",
        action="store_true",
        dest="clear_checkpoint",
        help="drop the job's checkpoint instead of keeping it (use when the cursor itself is "
        "what was wrong -- by default a retried stage resumes from where it got to)",
    )
    p_retry.set_defaults(handler=_cmd_retry)

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


def _cmd_kick(args: argparse.Namespace) -> dict:
    """rowboat-F8's immediate-trigger escape hatch. Opens no store: the
    wake signal is a file next to ``jobs.db``, and a kick must stay usable
    (and cheap) even while a worker holds the DB busy -- which is exactly
    when someone reaches for it."""
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "jobs.kick", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )
    try:
        result = kick(program_root)
    except OSError as exc:
        return error_envelope("jobs.kick", "wake_signal_unwritable", f"{type(exc).__name__}: {exc}")
    return ok_envelope(
        "jobs.kick",
        result=result,
        next_actions=[
            next_action(
                ["trialerror", "jobs", "start-worker", "--mode", "loop"],
                "no worker is napping? a kick wakes existing workers only -- launch one",
            )
        ],
    )


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


def _cmd_abandon(args: argparse.Namespace) -> dict:
    """Lane FB-3 item 9. Until now the only route to ``abandoned`` was
    exhausting ``max_attempts``, so cancelling a job meant letting it run and
    fail three times, or pausing it and leaving a paused row in the queue
    forever. ``ingest retract`` uses the same ledger call for the pending
    work of a document it has just withdrawn."""
    store, err = _open(args)
    if err is not None:
        return err
    try:
        row = ledger.abandon(store, args.job_id, reason=args.reason)
        return ok_envelope("jobs.abandon", result=row)
    except JobError as exc:
        return error_envelope(
            "jobs.abandon",
            type(exc).__name__,
            str(exc),
            next_actions=[
                next_action(
                    ["trialerror", "jobs", "pause", args.job_id],
                    "a worker holds this job: pause it first, then abandon the paused row",
                )
            ],
        )
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


def _cmd_retry(args: argparse.Namespace) -> dict:
    """Lane FB-8a. A tool-side defect was fixed and the documents that met
    it first were the only ones the fix could not reach: their jobs were
    ``failed`` (offload attempts spent) or ``abandoned``, ``jobs resume``
    covers ``paused`` only, and the only other route was editing the jobs
    store by hand.

    **The order of the two halves is the design.** The state refusals are
    checked FIRST and read nothing but the ledger row, so a retry aimed at a
    job a worker is holding is refused before any directory moves. Then the
    offload queue entry goes back (idempotent, detectable). Then, and only
    then, the store transaction. A crash in between leaves a queued marker
    for a still-``failed`` row -- recoverable by running this command again,
    which finds the queue half done and completes the store half -- rather
    than a ``pending`` row whose marker is still terminal, which would look
    like nothing needed doing and would simply re-fail three times."""
    from trialerror.offload import protocol
    from trialerror.offload.retry import retry_offload_entry
    from trialerror.offload.stage import MissingStageInputError

    store, err = _open(args)
    if err is not None:
        return err
    program_root = _resolve_program_root(args)
    try:
        # -- half zero: refuse on the row's state, before anything moves ---
        try:
            ledger.retry_refusal(store, args.job_id, max_attempts=args.max_attempts)
        except ValueError as exc:
            return error_envelope("jobs.retry", "bad_max_attempts", str(exc))
        except JobError as exc:
            found = ledger.get_job(store, args.job_id)
            actions = []
            if found is not None and found["state"] == "paused":
                actions.append(
                    next_action(
                        ["trialerror", "jobs", "resume", args.job_id],
                        "a paused job is lifted with `jobs resume`, not retried",
                    )
                )
            if found is not None and found["state"] in ledger.HELD_STATES:
                actions.append(
                    next_action(
                        ["trialerror", "jobs", "pause", args.job_id],
                        "a worker holds this job: pause it, let it stop, then retry the settled row",
                    )
                )
            return error_envelope("jobs.retry", type(exc).__name__, str(exc), next_actions=actions)

        # -- half one: the offload queue entry ----------------------------
        root = protocol.offload_root(program_root)
        try:
            offload = retry_offload_entry(
                store, root, args.job_id, reason=args.reason, by_launch=args.by_launch
            )
        except MissingStageInputError as exc:
            return error_envelope("jobs.retry", "offload_input_missing", str(exc))
        except protocol.OffloadError as exc:
            return error_envelope("jobs.retry", "offload_retry_refused", str(exc))

        # -- half two: the jobs store -------------------------------------
        before = ledger.get_job(store, args.job_id)
        try:
            row = ledger.retry(
                store,
                args.job_id,
                reason=args.reason,
                by_launch=args.by_launch,
                max_attempts=args.max_attempts,
                clear_checkpoint=args.clear_checkpoint,
            )
        except JobError as exc:
            return error_envelope("jobs.retry", type(exc).__name__, str(exc))

        warnings = list(offload["warnings"])
        actions = [
            next_action(
                ["trialerror", "jobs", "kick"],
                "wake a napping worker so the retried job is claimed now rather than at the "
                "next poll",
            )
        ]
        if offload["moved"] or offload["already_retried"]:
            actions.append(
                next_action(
                    ["trialerror", "offload", "status"],
                    "the marker is back in the queue -- the GPU worker claims it on its next poll",
                )
            )
        return ok_envelope(
            "jobs.retry",
            result={
                "job_id": row["job_id"],
                "kind": row["kind"],
                "previous_state": before["state"],
                "previous_attempts": before["attempts"],
                "state": row["state"],
                "max_attempts": row["max_attempts"],
                "last_error": row["last_error"],
                "checkpoint_cleared": bool(args.clear_checkpoint),
                "offload": {
                    "moved": offload["moved"],
                    "from": offload["from"],
                    "to": offload["to"],
                    "attempts_reset": offload["attempts_reset"],
                    "state": offload["state"],
                    "already_retried": offload["already_retried"],
                },
                "warnings": warnings,
            },
            next_actions=actions,
            warnings=warnings or None,
        )
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
            jitter_kwargs = {"jitter_frac": args.jitter_frac} if args.jitter_frac is not None else {}
            results = run_loop(
                store,
                kinds=kinds,
                poll_interval_s=args.poll_interval_s,
                max_idle_polls=args.max_idle_polls,
                max_iterations=args.max_iterations,
                wake_signal=not args.no_wake_signal,
                **jitter_kwargs,
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
        jitter_frac=args.jitter_frac,
        wake_signal=not args.no_wake_signal,
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
