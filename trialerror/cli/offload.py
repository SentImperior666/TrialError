"""``trialerror offload`` -- the GPU offload queue's CLI surface (lane
L0-C, design section 4).

Four verbs, split by which machine runs them:

- ``worker``   runs on DEV. Claims the sandbox's queue over SSH, runs the
  real marker/Qwen3 backends on the local GPU, publishes the results, and
  exits when the queue is empty. This is what the DEV "GPU Worker"
  launcher double-clicks.
- ``reclaim``  runs on the SANDBOX, every jobs-loop cycle. Returns claims
  whose heartbeat stopped (a closed laptop) to ``pending/``.
- ``kick``     runs on the SANDBOX, every jobs-loop cycle. Finishes any
  interrupted publish, clears the retry delay on a parked ledger job whose
  result has landed so it completes on this cycle instead of in half an
  hour, and deletes ``done/<job>`` once its ledger row is ``complete``. Its
  ``rejected`` list is the one field worth watching: a staging directory
  that failed the adoption gate (SEC-1) is a manifest the offload key wrote
  for a job the sandbox has no record of queueing.
- ``status``   runs anywhere. The counts ``te-status.sh`` prints.

``deploy/sandbox/supervise.sh``'s jobs window already probes
``trialerror offload --help`` and calls ``reclaim``/``kick`` when the group
is present, so this file landing is the whole deployment step -- no
supervise.sh change (design section 2's jobs-window note).

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/offload.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touched
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.jobs import ledger
from trialerror.offload import protocol
from trialerror.stores.store import open_store
from trialerror.util.config import CONFIG_FILENAME, find_program_root, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "offload"
HELP = "GPU offload queue: run the DEV worker; reclaim/kick/inspect the sandbox-side queue."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="offload_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so
        # an unset value here never overwrites the global --program-root/
        # --platform-root the top-level parser resolved.
        p.add_argument(
            "--program-root",
            default=argparse.SUPPRESS,
            help="program scaffold root (default: discovered from CWD via trialerror.toml)",
        )
        p.add_argument(
            "--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)"
        )

    p_worker = sub.add_parser(
        "worker",
        help="DEV only: claim the sandbox's offload queue and run the real GPU backends locally",
    )
    _common(p_worker)
    p_worker.add_argument(
        "--remote",
        default=None,
        help="ssh alias of the sandbox's restricted offload endpoint (e.g. te-offload). "
        "Mutually exclusive with --queue-root.",
    )
    p_worker.add_argument(
        "--queue-root",
        default=None,
        help="path to an offload/ queue on THIS machine (same-box worker; also what the tests "
        "use). Mutually exclusive with --remote.",
    )
    p_worker.add_argument("--work-root", default=None, help="local scratch root (default: %%LOCALAPPDATA%%/trialerror/offload/work)")
    p_worker.add_argument("--lock-path", default=None, help="single-instance lock file (default: beside the work root)")
    p_worker.add_argument("--worker-id", default="dev", help="claim directory name under offload/claimed/ (default: dev)")
    p_worker.add_argument(
        "--stay", action="store_true", help="keep polling instead of exiting when the queue is empty"
    )
    p_worker.add_argument("--poll-interval-s", type=float, default=30.0)
    p_worker.add_argument("--max-jobs", type=int, default=None, help="stop after this many jobs")
    p_worker.add_argument("--max-polls", type=int, default=None, help="stop after this many polls")
    p_worker.add_argument("--batch-size", type=int, default=8, help="embed batch size on the GPU (default: 8)")
    p_worker.set_defaults(handler=_cmd_worker)

    p_reclaim = sub.add_parser(
        "reclaim", help="SANDBOX: return claims whose heartbeat stopped (default: older than 60 min)"
    )
    _common(p_reclaim)
    p_reclaim.add_argument(
        "--expiry-s", type=float, default=protocol.DEFAULT_CLAIM_EXPIRY_S, help="claim expiry in seconds (default: 3600)"
    )
    p_reclaim.set_defaults(handler=_cmd_reclaim)

    p_kick = sub.add_parser(
        "kick", help="SANDBOX: adopt interrupted publishes, un-delay parked jobs whose result landed, sweep completed ones"
    )
    _common(p_kick)
    p_kick.set_defaults(handler=_cmd_kick)

    p_status = sub.add_parser("status", help="counts for the offload queue (what te-status.sh prints)")
    _common(p_status)
    p_status.set_defaults(handler=_cmd_status)

    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if getattr(args, "program_root", None):
        return Path(args.program_root)
    return find_program_root()


def _root_or_error(args: argparse.Namespace, action: str) -> tuple[Path | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            action,
            "no_program_root",
            "no --program-root given and no trialerror.toml found walking up from CWD",
        )
    return protocol.offload_root(program_root), None


# ---------------------------------------------------------------------------
def _cmd_status(args: argparse.Namespace) -> dict:
    root, err = _root_or_error(args, "offload.status")
    if err is not None:
        return err
    result = protocol.counts(root)
    actions = []
    if result["pending"]:
        actions.append(
            next_action(
                ["trialerror", "offload", "worker", "--remote", "te-offload"],
                "run the DEV GPU worker (the GPU Worker launcher does this for you)",
            )
        )
    return ok_envelope("offload.status", result=result, next_actions=actions)


def _cmd_reclaim(args: argparse.Namespace) -> dict:
    root, err = _root_or_error(args, "offload.reclaim")
    if err is not None:
        return err
    reclaimed = protocol.reclaim_stale(root, expiry_s=args.expiry_s)
    return ok_envelope(
        "offload.reclaim",
        result={"reclaimed": reclaimed, "count": len(reclaimed), "expiry_s": args.expiry_s},
    )


def _cmd_kick(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "offload.kick",
            "no_program_root",
            "no --program-root given and no trialerror.toml found walking up from CWD",
        )
    root = protocol.offload_root(program_root)
    adopted = protocol.adopt_orphaned_partials(root)
    rejected = protocol.rejected_partials(root)

    kicked: list[str] = []
    swept: list[str] = []
    unknown: list[str] = []
    if not root.is_dir():  # V2: nothing to kick, and nothing to create
        return ok_envelope(
            "offload.kick",
            result={"adopted": [], "kicked": [], "swept": [], "unknown_job_ids": [], "rejected": []},
        )
    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    try:
        for job_id in protocol.list_done(root):
            job = ledger.get_job(store, job_id)
            if job is None:
                unknown.append(job_id)
                continue
            if job["state"] == "complete":
                protocol.discard_published(root, job_id)
                swept.append(job_id)
                continue
            if ledger.kick(store, job_id) is not None:
                kicked.append(job_id)
    finally:
        store.close()

    return ok_envelope(
        "offload.kick",
        result={
            "adopted": adopted,
            "kicked": kicked,
            "swept": swept,
            "unknown_job_ids": unknown,
            "rejected": rejected,
        },
        next_actions=(
            [next_action(["trialerror", "jobs", "start-worker", "--mode", "loop"], "let the un-delayed jobs finish")]
            if kicked
            else []
        ),
    )


def _cmd_worker(args: argparse.Namespace) -> dict:
    from trialerror.offload.lock import WorkerAlreadyRunning, single_instance_lock
    from trialerror.offload.transport import LocalTransport, SshTransport
    from trialerror.offload.worker import ConfigDevBackends, WorkerConfigError, run_worker

    if bool(args.remote) == bool(args.queue_root):
        return error_envelope(
            "offload.worker",
            "bad_arguments",
            "pass exactly one of --remote <ssh alias> (the normal DEV case) or --queue-root <path> "
            "(a queue on this same machine)",
        )

    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "offload.worker",
            "no_program_root",
            "no --program-root given and no trialerror.toml found walking up from CWD -- the DEV "
            "worker needs the DEV program's trialerror.toml to know where marker/Qwen3 live",
        )
    cfg_path = Path(program_root) / CONFIG_FILENAME
    try:
        config = load_config(cfg_path).raw
    except Exception as exc:  # noqa: BLE001 - surfaced as an envelope, never a traceback
        return error_envelope("offload.worker", "bad_config", f"could not read {cfg_path}: {exc}")

    transport = (
        SshTransport(args.remote)
        if args.remote
        else LocalTransport(args.queue_root, worker_id=args.worker_id)
    )
    lines: list[str] = []
    try:
        with single_instance_lock(args.lock_path):
            summary = run_worker(
                transport=transport,
                backends=ConfigDevBackends(config),
                work_root=args.work_root,
                worker_id=args.worker_id,
                stay=args.stay,
                poll_interval_s=args.poll_interval_s,
                max_jobs=args.max_jobs,
                max_polls=args.max_polls,
                batch_size=args.batch_size,
                log=lines.append,
            )
    except WorkerAlreadyRunning as exc:
        return error_envelope("offload.worker", "already_running", str(exc))
    except WorkerConfigError as exc:
        return error_envelope("offload.worker", "fake_backend_refused", str(exc))
    except KeyboardInterrupt:  # pragma: no cover - operator Ctrl+C
        return error_envelope("offload.worker", "interrupted", "interrupted; the claim was returned")

    return ok_envelope("offload.worker", result={**summary, "log": lines})
