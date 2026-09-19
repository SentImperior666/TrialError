"""``trialerror offload`` -- the GPU offload queue's CLI surface (lane
L0-C, design section 4).

Verbs, split by which machine runs them:

- ``worker``   runs on DEV. Claims the sandbox's queue over SSH, runs the
  real marker/Qwen3 backends on the local GPU, publishes the results, and
  exits when the queue is empty. This is what the DEV "GPU Worker"
  launcher double-clicks. Its ``--backend-config-root`` (D-FB-6) names the
  root whose ``trialerror.toml`` says where marker/Qwen3 live -- the queue
  comes from ``--remote``/``--queue-root``, never from that root, which is
  what the old name ``--program-root`` kept implying. The old spelling is
  still accepted (a suppressed alias, so the DEV launcher and
  ``supervise.sh`` keep working unchanged) and says so in the envelope's
  ``meta``.
- ``doctor``   runs anywhere. This subsystem's own health checks in one
  place, modelled on ``ingest doctor``: the queue's five, the record's
  ``fake_backend_rows``, and ``offload_backend_root_resolved`` -- which
  root was read, what each stage names, and whether it resolves here.
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
- ``worker-control`` runs on the SANDBOX (ruling C-0097, D1/D5). Leaves a
  pause/resume/stop request in the queue for one worker to read off its next
  heartbeat reply. Every act carries a launch (L-E4: no launch, no control);
  there is no preemptive kill anywhere in this harness (D8), so this verb
  ASKS and the worker complies at its next cooperative checkpoint.
- ``worker-status`` runs anywhere (D4). What each worker last said it was
  doing: state, job, progress, pace, ETA, heartbeat age, pending control,
  settings. On the sandbox it reads the queue directly; on DEV, against a
  queue reached over the key, it reads the same files through ``--queue-root``.

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


#: What ``--batch-size`` says in its HELP text. The value that actually
#: takes effect is ``trialerror.offload.worker.DEFAULT_EMBED_BATCH_SIZE``,
#: resolved in :func:`_cmd_worker` -- ``register()`` runs for EVERY
#: ``trialerror`` invocation (the CLI's group auto-discovery), and importing
#: the worker there pulls in ~32 modules (jobs ledger/registry/worker, the
#: offload lock/protocol/shell/stage/transport stack, the whole stores
#: schema package) at ~96 ms, on every command including the ones that never
#: touch offload. A helper with a local import does NOT avoid that if the
#: parser calls it, which is what this used to do -- so the parser now names
#: no worker symbol at all, and this string is pinned to the real constant
#: by a test rather than by an import.
_BATCH_SIZE_HELP_DEFAULT = 4

#: D-FB-6: what ``--program-root`` is called on the ``worker`` verb now, and
#: the alias that keeps the DEV launcher working. Both write the SAME
#: namespace attribute (``program_root``), so every code path below reads one
#: value and there is no second resolution order to get wrong.
BACKEND_CONFIG_ROOT_FLAG = "--backend-config-root"
BACKEND_CONFIG_ROOT_ALIAS = "--program-root"

#: The sentence the envelope's ``meta`` carries when the worker was invoked
#: under the old spelling. A deprecation nobody is told about is a rename
#: that never finishes.
BACKEND_CONFIG_ROOT_DEPRECATION = (
    f"{BACKEND_CONFIG_ROOT_ALIAS} is a deprecated alias for {BACKEND_CONFIG_ROOT_FLAG} on this verb "
    "(D-FB-6): the worker reads that root for [ingest.ocr]/[ingest.embed] and nothing else -- the "
    "queue comes from --remote or --queue-root. The alias still resolves to the same value and is "
    "not going away while the DEV launcher spells it that way."
)


class _BackendConfigRootAction(argparse.Action):
    """Store the value under ``program_root`` and remember WHICH spelling
    said so.

    argparse cannot answer "was this the alias?" on its own once two option
    strings share a ``dest``, and the deprecation note has to be able to
    tell -- an envelope that carried the note unconditionally would nag
    every operator who already moved to the new name."""

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: D102 - argparse contract
        setattr(namespace, self.dest, values)
        if option_string == BACKEND_CONFIG_ROOT_ALIAS:
            setattr(namespace, "backend_config_root_alias", option_string)


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

    def _backend_config_root(p: argparse.ArgumentParser) -> None:
        """``worker``'s own version of ``_common``'s program-root flag,
        under the name that says what the root is FOR (D-FB-6).

        Same ``dest`` and same ``default=SUPPRESS`` on both spellings (FX-12,
        above), so an unset value here still never overwrites the global
        ``--program-root`` the top-level parser resolved, and the alias is
        indistinguishable from the new name everywhere except the
        deprecation note."""
        p.add_argument(
            BACKEND_CONFIG_ROOT_FLAG,
            dest="program_root",
            action=_BackendConfigRootAction,
            default=argparse.SUPPRESS,
            # V-4: argparse derives the metavar from the ``dest``, and the
            # dest is the OLD name (it has to be: every code path below reads
            # one attribute). Without this the renamed flag's own usage line
            # reads ``--backend-config-root PROGRAM_ROOT``, i.e. a rename
            # that looks half-done in --help.
            metavar="ROOT",
            help="root whose trialerror.toml names the LOCAL marker/Qwen3 backends this worker runs "
            "([ingest.ocr]/[ingest.embed]); the queue itself comes from --remote/--queue-root "
            "(default: discovered from CWD via trialerror.toml)",
        )
        p.add_argument(
            BACKEND_CONFIG_ROOT_ALIAS,
            dest="program_root",
            action=_BackendConfigRootAction,
            default=argparse.SUPPRESS,
            metavar="ROOT",
            help=argparse.SUPPRESS,  # deprecated alias, kept for the DEV launcher
        )
        p.add_argument(
            "--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)"
        )

    p_worker = sub.add_parser(
        "worker",
        help="DEV only: claim the sandbox's offload queue and run the real GPU backends locally",
    )
    _backend_config_root(p_worker)
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
    # FX-S1: the default is a GPU batch size now, not a "how much work is
    # one model load worth" compromise -- the driver stays resident for the
    # whole run (trialerror.offload.worker.DEFAULT_EMBED_BATCH_SIZE's note).
    p_worker.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=f"embed batch size on the GPU (default: {_BATCH_SIZE_HELP_DEFAULT}) -- lower it if the card "
        "runs out of VRAM; it no longer decides how often the model is loaded",
    )
    # C-0097 D9: the old behaviour, for a machine with the memory to hold both
    # stages' models at once. The default now unloads the idle one on a kind
    # switch, because the measured alternative was 0.9 GB of free RAM.
    p_worker.add_argument(
        "--keep-resident",
        action="store_true",
        help="keep both stages' backends loaded for the whole run instead of unloading the idle "
        "one when the job kind switches (needs the memory for both at once)",
    )
    p_worker.set_defaults(handler=_cmd_worker)

    # --- C-0097 D1/D5: ask a worker to pause, resume or stop ---------------
    p_control = sub.add_parser(
        "worker-control",
        help="SANDBOX: ask a worker to pause, resume or stop at its next cooperative checkpoint",
    )
    _common(p_control)
    p_control.add_argument("--worker-id", default="dev", help="claim directory name under offload/claimed/ (default: dev)")
    group = p_control.add_mutually_exclusive_group(required=True)
    group.add_argument("--pause", action="store_const", const="pause", dest="request",
                       help="finish the unit in flight, keep the claim, keep heartbeating")
    group.add_argument("--resume", action="store_const", const="resume", dest="request",
                       help="continue from the next unit (nothing repeated, nothing skipped)")
    group.add_argument("--stop", action="store_const", const="stop", dest="request",
                       help="finish the unit in flight, hand an incomplete claim back, exit")
    # FIX V-3: the operator's explicit way out. A spent request is reaped
    # automatically by this verb, `worker-status` and `kick`, but a worker that
    # was restarted before any of those ran -- or one that never reported at all
    # -- leaves a request nothing can end except the TTL. This is that hour back.
    group.add_argument("--clear", action="store_const", const="clear", dest="request",
                       help="drop this worker's pending request without asking for anything "
                            "(a request a worker never read, or one an operator changed their "
                            "mind about)")
    p_control.add_argument(
        "--by-launch",
        default=None,
        dest="by_launch",
        help="the launch this control act is booked under (REQUIRED -- ruling L-E4: an existing "
        "platform.launch row; no launch, no control)",
    )
    p_control.add_argument("--job-id", default=None, dest="job_id", help="record which job the request was aimed at")
    p_control.add_argument(
        "--even-if-absent",
        action="store_true",
        help="leave the request waiting for a worker that has not reported yet (default: refuse, "
        "because a request nothing is listening for is a request that will surprise somebody)",
    )
    p_control.set_defaults(handler=_cmd_worker_control)

    p_wstatus = sub.add_parser(
        "worker-status", help="what each worker last said it was doing (state, progress, pace, ETA, beat age)"
    )
    _common(p_wstatus)
    p_wstatus.add_argument("--worker-id", default=None, help="one worker instead of all of them")
    p_wstatus.add_argument(
        "--queue-root",
        default=None,
        help="read a queue at this path instead of <program-root>/offload (the DEV side of the "
        "split, and what the tests use)",
    )
    p_wstatus.add_argument(
        "--heartbeat-interval-s",
        type=float,
        default=None,
        help="the worker's beat interval, which sets the lost window (2x + 60s). Default: the "
        "worker's own default.",
    )
    p_wstatus.set_defaults(handler=_cmd_worker_status)

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

    p_doctor = sub.add_parser(
        "doctor",
        help="offload-specific health checks (queue backlog/claims/failures, worker heartbeat and "
        "control requests, the backend config root, fake-backend rows)",
    )
    _common(p_doctor)
    p_doctor.set_defaults(handler=_cmd_doctor)

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


#: What ``offload doctor`` runs, in the order it reports them: the queue's
#: own checks (``trialerror/offload/checks.py``'s ``offload`` category, the
#: two C-0097 control ones included -- a subsystem doctor that hid the
#: control law's own witness would be the wrong five), the backend-root
#: reading D-FB-6 adds, and the record-side ``fake_backend_rows``, which is
#: registered under ``ingest`` but is this subsystem's D13 backstop and the
#: one an operator running THIS verb is looking for.
_OFFLOAD_CHECK_NAMES = (
    "offload_backlog",
    "offload_stale_claims",
    "offload_failed",
    "worker_heartbeat_stale",
    "offload_control_orphaned",
    "offload_backend_root_resolved",
    "fake_backend_rows",
)


def _cmd_doctor(args: argparse.Namespace) -> dict:
    """``offload doctor`` -- this subsystem's checks in one verb, shaped
    exactly like ``ingest doctor`` (``trialerror/cli/ingest.py``): discover,
    run a named list, summarise, and return a NON-ok envelope when anything
    failed so a script can read the exit code instead of the prose.

    It registers no check of its own: every one of them is also reachable
    as ``trialerror doctor --only <name>``, which is what keeps this verb a
    convenience rather than a second source of truth."""
    from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

    program_root = _resolve_program_root(args)
    discover_and_register_checks()
    ctx = DoctorContext(
        program_root=program_root, platform_root=getattr(args, "platform_root", None)
    )
    results = run_checks(ctx, only=list(_OFFLOAD_CHECK_NAMES))
    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    result = {
        "backend_config_root": str(program_root) if program_root else None,
        "checks": [r.to_dict() for r in results],
        "summary": {
            "total": len(results),
            "warned": len(warned),
            "failed": len(failed),
        },
    }
    if failed:
        return error_envelope(
            "offload.doctor",
            "offload_doctor_checks_failed",
            f"{len(failed)} check(s) failed",
            details=result,
        )
    return ok_envelope("offload.doctor", result=result)


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


def _cmd_worker_control(args: argparse.Namespace) -> dict:
    """Ask one worker to pause, resume or stop (C-0097 D1/D5).

    Goes through ``trialerror.offload.control.request_control_for_launch`` --
    the SAME function the dashboard's ``worker-control`` write action calls --
    so the refusals are identical on both surfaces by construction rather than
    by two lists that agree today. The launch is checked against
    ``platform.launch`` before anything is written, and the act lands in the
    event log as ``offload_worker_control``."""
    from trialerror.offload import control as control_api
    from trialerror.stores.errors import StoreError

    root, err = _root_or_error(args, "offload.worker_control")
    if err is not None:
        return err
    if not args.by_launch:
        return error_envelope(
            "offload.worker_control",
            "launch_required",
            "offload worker-control requires --by-launch (ruling L-E4: an existing platform.launch "
            "row) -- no launch, no control",
        )
    program_root = _resolve_program_root(args)
    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    try:
        if args.request == "clear":
            record = control_api.clear_control_for_launch(
                store, root, worker_id=args.worker_id, by_launch=args.by_launch
            )
            return ok_envelope("offload.worker_control", result=record)
        record = control_api.request_control_for_launch(
            store,
            root,
            worker_id=args.worker_id,
            request=args.request,
            by_launch=args.by_launch,
            job_id=args.job_id,
            require_worker=not args.even_if_absent,
        )
    except control_api.ControlError as exc:
        return error_envelope("offload.worker_control", "control_refused", str(exc))
    except StoreError as exc:
        return error_envelope("offload.worker_control", "launch_missing", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "offload.worker_control",
        result=record,
        next_actions=[
            next_action(
                ["trialerror", "offload", "worker-status", "--worker-id", args.worker_id],
                "watch the worker pick the request up on its next heartbeat",
            )
        ],
    )


def _cmd_worker_status(args: argparse.Namespace) -> dict:
    """What each worker last said it was doing (C-0097 D4).

    Reads the progress files and nothing else: this verb never talks to a
    worker, so it is safe to run on either machine and at any time, and it
    reports ``lost`` rather than pretending a silent worker is busy.

    One write, disclosed because a status verb that changes anything is worth
    disclosing: it REAPS control requests the worker has already acted on and
    finished with (FIX V-3, ``reap_spent_controls``), and names them in
    ``reaped``. Nothing else in this harness polls the queue often enough to be
    the janitor for this, and a stop that outlives its worker stops the next
    run the operator starts."""
    from trialerror.offload import control as control_api

    if getattr(args, "queue_root", None):
        root = Path(args.queue_root)
    else:
        root, err = _root_or_error(args, "offload.worker_status")
        if err is not None:
            return err
    interval = (
        control_api.DEFAULT_HEARTBEAT_INTERVAL_S
        if args.heartbeat_interval_s is None
        else float(args.heartbeat_interval_s)
    )
    reaped = control_api.reap_spent_controls(
        root, worker_id=args.worker_id, heartbeat_interval_s=interval
    )
    rows = control_api.worker_rows(root, heartbeat_interval_s=interval)
    if args.worker_id:
        rows = [r for r in rows if r["worker_id"] == args.worker_id]
    actions = []
    for row in rows:
        if row["lost"] and row.get("pending_control"):
            actions.append(
                next_action(
                    ["trialerror", "doctor", "--only", "worker_heartbeat_stale"],
                    f"worker {row['worker_id']} has a pending {row['pending_control']} and has gone quiet",
                )
            )
            break
    return ok_envelope(
        "offload.worker_status",
        result={
            "root": str(root),
            "exists": Path(root).is_dir(),
            "workers": rows,
            "count": len(rows),
            "heartbeat_interval_s": interval,
            "lost_after_s": control_api.lost_after_s(interval),
            "controls": control_api.list_controls(root),
            "reaped": reaped,
        },
        next_actions=actions,
    )


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
    from trialerror.offload import control as control_api

    root = protocol.offload_root(program_root)
    adopted = protocol.adopt_orphaned_partials(root)
    rejected = protocol.rejected_partials(root)
    # FIX V-3: the janitor's newest chore. The jobs loop runs `kick` every
    # cycle, so a control request whose worker has acted on it and gone is
    # cleared without anybody asking -- which is what makes a RESTART inside the
    # one-hour TTL start clean instead of stopping itself.
    reaped = control_api.reap_spent_controls(root) if root.is_dir() else []

    kicked: list[str] = []
    swept: list[str] = []
    unknown: list[str] = []
    if not root.is_dir():  # V2: nothing to kick, and nothing to create
        return ok_envelope(
            "offload.kick",
            result={
                "adopted": [], "kicked": [], "swept": [], "unknown_job_ids": [],
                "rejected": [], "reaped": [],
            },
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
            "reaped": reaped,
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


def _worker_meta(args: argparse.Namespace) -> dict | None:
    """The ``meta`` block every ``offload.worker`` envelope carries when the
    operator spelled the backend root the old way (D-FB-6), and ``None``
    when they did not. Passed to EVERY return below, including the
    refusals -- a deprecation note that only appears on success is one the
    operator reading a refusal never sees."""
    if getattr(args, "backend_config_root_alias", None) is None:
        return None
    return {
        "deprecated_flags": {
            BACKEND_CONFIG_ROOT_ALIAS: BACKEND_CONFIG_ROOT_DEPRECATION,
        }
    }


def _worker_warnings(args: argparse.Namespace) -> list[dict] | None:
    """The same deprecation, on the channel ``--format text`` actually
    prints (V-2).

    ``meta`` is where the note belongs for a JSON reader, and
    :func:`trialerror.util.envelope.render_text` renders status,
    result/error, warnings and nextActions -- never ``meta``. The one caller
    in this repo that still spells the alias is the DEV launcher, which runs
    ``--format text``: the note reached exactly the operators who had
    already moved. Additive by construction: the ``warnings`` key is emitted
    only for a non-empty list, so a run under the new name is byte-identical
    to before."""
    if getattr(args, "backend_config_root_alias", None) is None:
        return None
    return [{"code": "deprecated_flag", "message": BACKEND_CONFIG_ROOT_DEPRECATION}]


def _cmd_worker(args: argparse.Namespace) -> dict:
    from trialerror.offload.lock import WorkerAlreadyRunning, single_instance_lock
    from trialerror.offload.transport import LocalTransport, SshTransport
    from trialerror.offload.worker import (
        DEFAULT_EMBED_BATCH_SIZE,
        ConfigDevBackends,
        WorkerConfigError,
        run_worker,
    )

    # The real default, resolved HERE (see _BATCH_SIZE_HELP_DEFAULT): the
    # worker import belongs on the one code path that actually runs a
    # worker, not on the parser construction every trialerror command pays.
    batch_size = DEFAULT_EMBED_BATCH_SIZE if args.batch_size is None else args.batch_size
    meta = _worker_meta(args)
    warnings = _worker_warnings(args)

    if bool(args.remote) == bool(args.queue_root):
        return error_envelope(
            "offload.worker",
            "bad_arguments",
            "pass exactly one of --remote <ssh alias> (the normal DEV case) or --queue-root <path> "
            "(a queue on this same machine)",
            meta=meta,
            warnings=warnings,
        )

    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "offload.worker",
            "no_program_root",
            f"no {BACKEND_CONFIG_ROOT_FLAG} given and no trialerror.toml found walking up from "
            "CWD -- the DEV worker needs the DEV program's trialerror.toml to know where "
            "marker/Qwen3 live",
            meta=meta,
            warnings=warnings,
        )
    cfg_path = Path(program_root) / CONFIG_FILENAME
    try:
        config = load_config(cfg_path).raw
    except Exception as exc:  # noqa: BLE001 - surfaced as an envelope, never a traceback
        return error_envelope(
            "offload.worker",
            "bad_config",
            f"could not read {cfg_path}: {exc}",
            meta=meta,
            warnings=warnings,
        )

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
                batch_size=batch_size,
                keep_resident=args.keep_resident,
                log=lines.append,
            )
    except WorkerAlreadyRunning as exc:
        return error_envelope(
            "offload.worker", "already_running", str(exc), meta=meta, warnings=warnings
        )
    except WorkerConfigError as exc:
        return error_envelope(
            "offload.worker", "fake_backend_refused", str(exc), meta=meta, warnings=warnings
        )
    except KeyboardInterrupt:  # pragma: no cover - operator Ctrl+C
        return error_envelope(
            "offload.worker",
            "interrupted",
            "interrupted; the claim was returned",
            meta=meta,
            warnings=warnings,
        )

    return ok_envelope(
        "offload.worker", result={**summary, "log": lines}, meta=meta, warnings=warnings
    )
