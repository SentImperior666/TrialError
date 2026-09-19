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

from trialerror.budget.errors import (
    BudgetError,
    LaunchNotOwnedError,
    ModelPolicyViolationError,
    NoOpenSessionError,
    UnknownAssignmentError,
    UnknownOverrideRulingError,
)
from trialerror.budget.gate import resolve_booking_identity
from trialerror.budget.pools import (
    DEFAULT_BOOKING_TTL_S,
    book_launch,
    budget_status,
    calibrate as calibrate_,
    check_booking_preconditions,
    create_pool,
    heartbeat_launch,
    pool_report,
    reconcile_launch,
    reconcile_launch_from_event,
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
    book.add_argument(
        "--session-id", default=None,
        help="default: the program's single OPEN session (bound at `session boot`). Refused, by name, "
             "when none or more than one is open -- booking against a session nobody named is how a "
             "launch ends up unreconcilable",
    )
    book.add_argument(
        "--program-id", default=None,
        help="default: [program] id from this program's trialerror.toml",
    )
    book.add_argument("--agent-kind", required=True)
    book.add_argument("--model-class", required=True, choices=["top", "mid", "small"])
    book.add_argument("--model", required=True)
    book.add_argument("--purpose", required=True)
    book.add_argument("--est-tokens", required=True, type=int)
    book.add_argument("--booking-ttl-s", type=int, default=DEFAULT_BOOKING_TTL_S)
    book.add_argument("--parent-launch", default=None)
    book.add_argument("--workpackage", default=None)
    book.add_argument("--override-ruling-id", default=None)
    book.add_argument(
        "--assign-id", action="append", default=None, dest="assign_ids", metavar="ASSIGN_ID",
        help="for a lens booking: the lens_assignment row(s) this launch covers, repeatable (they are on "
             "each `trialerror lens export` row as attrs.assign_ids). Recorded as lens_assignment."
             "lens_launch_id, which is how the retrieval scope and the citation audit resolve this "
             "launch's slice",
    )
    book.add_argument(
        "--quota-dir", default=None, help="override the plan-quota capture dir (see `budget quota`)"
    )
    book.add_argument(
        "--allow-stale-quota",
        action="store_true",
        help="book even though the plan-quota capture is older than [budget] quota_max_age_s; the "
             "flag and the reading it overrode are recorded on the launch",
    )
    book.set_defaults(handler=_run_book)

    reconcile = sub.add_parser("reconcile", help="settle actuals for a launch_id")
    reconcile.add_argument("--launch-id", required=True)
    # D-FB-13 (b): exactly one of the two, enforced by argparse rather than by
    # a hand-rolled check, so `--actual-tokens 9000 --from-event` cannot reach
    # a handler that has to decide which of two numbers the operator meant.
    source = reconcile.add_mutually_exclusive_group(required=True)
    source.add_argument("--actual-tokens", type=int)
    source.add_argument(
        "--from-event",
        action="store_true",
        help="take the actuals from the launch's own latest subagent_return event (the usage object "
             "the PostToolUse hook captured) and record reconcile_source=event; refuses, naming the "
             "event, when the host sent no usage",
    )
    # Fix pass V-8: default None rather than "manual", so the handler can
    # tell "the operator asked for a label" from "nobody said". The
    # sibling pair --actual-tokens/--from-event is refused by argparse;
    # this one was accepted and the label silently discarded, which is the
    # same mistake with a quieter answer.
    reconcile.add_argument(
        "--reconcile-source",
        default=None,
        choices=["transcript", "estimate", "manual"],
        help="the label for a hand-asserted number (default: manual); refused with --from-event, "
             "which records reconcile_source=event and is the only thing that may",
    )
    reconcile.add_argument(
        "--spawned-model", default=None,
        help="the model this launch ACTUALLY ran on; recorded in launch.attrs.spawned_model and read by "
             "the agent_model_matches_booking doctor check",
    )
    reconcile.set_defaults(handler=_run_reconcile)

    heartbeat = sub.add_parser(
        "heartbeat",
        help="a live launch says 'still here': refresh its booking TTL (booked_ts only, nothing else)",
    )
    heartbeat.add_argument("--launch-id", required=True)
    heartbeat.set_defaults(handler=_run_heartbeat)

    status = sub.add_parser("status", help="pools, headroom, multiplier, DEFER advisories for an account")
    status.add_argument(
        "--account-id",
        default=None,
        help="default: the account the program's OPEN session is bound to (session.account_id). "
             "Required only when no session is open, or when more than one is",
    )
    status.add_argument("--model-class", default=None, choices=["top", "mid", "small"])
    status.set_defaults(handler=_run_status)

    check = sub.add_parser(
        "check",
        help="status + quota in one envelope, with the binding limit named (composes; adds no logic)",
    )
    check.add_argument("--account-id", default=None, help="default: the open session's account")
    check.add_argument("--model-class", default=None, choices=["top", "mid", "small"])
    check.add_argument("--quota-dir", default=None, help="override the capture dir (see `budget quota`)")
    check.add_argument(
        "--fresh-within-s",
        type=int,
        default=None,
        help="freshness bar in seconds for this one reading (default: [budget] quota_max_age_s, else 900)",
    )
    check.set_defaults(handler=_run_check)

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


def _load_raw_config(program_root: Path) -> dict | None:
    """The whole ``trialerror.toml`` as a plain dict, or ``None``. Same
    tolerate-absence posture as :func:`_load_policy`; ``[budget]``'s reader
    (:func:`trialerror.budget.quota.resolve_max_age_s`) takes the raw dict,
    matching every other config consumer in this codebase."""
    try:
        return load_config(program_root / "trialerror.toml").raw
    except ConfigError:
        return None


def _run_book(args: argparse.Namespace) -> dict:
    from trialerror.budget.quota import booking_quota_reading, booking_refusal

    store = _open_store(args)
    try:
        # Lane FB-7 item 6: both ids default from what the harness already
        # knows -- the open session, and [program] id -- before anything
        # else runs, so every rung below judges the booking that will
        # actually be made.
        #
        # Its own `try`, covering the resolver and nothing else (lane FB-7
        # fix pass, V-8). `RuntimeError` here means "more than one open
        # session" and `ValueError` means "no readable [program] id"; the
        # booking body below can raise both for reasons that have nothing to
        # do with either -- a `json.JSONDecodeError` IS a `ValueError` -- and
        # reporting one of those as `program_id_unresolved` sends the
        # operator to fix a config that is fine. The MCP handler has always
        # wrapped only the resolve call; this is the CLI reading the same.
        try:
            session_id, program_id, resolved_from = resolve_booking_identity(
                store, session_id=args.session_id, program_id=args.program_id
            )
        except NoOpenSessionError:
            raise
        except RuntimeError as exc:
            # `resolve_open_session` raises a bare RuntimeError for more than
            # one OPEN session. Two open sessions is exactly the state that
            # produces bookings nothing can reconcile, so this verb refuses
            # by name rather than picking one -- the same answer `budget
            # status` and `budget heartbeat` already give.
            return error_envelope(
                "budget book",
                "multiple_open_sessions",
                f"{exc} -- name the session explicitly with --session-id, or close one",
                next_actions=[
                    next_action(
                        ["trialerror", "doctor", "--only", "session_multiple_open"],
                        "see both sessions",
                    )
                ],
            )
        except ValueError as exc:
            return error_envelope("budget book", "program_id_unresolved", str(exc))
        policy = _load_policy(store.program_root)
        # Fix pass V-2: the operator's own command is judged BEFORE the
        # environment's reading. A booking from a closed session (or one that
        # breaks model policy) used to come back `stale_quota_capture`, which
        # sends the operator to the statusLine to fix a fault in their own
        # argv. `book_launch` runs these same two rungs again, so nothing can
        # book past them; this call is only about the order of the answers.
        check_booking_preconditions(
            store,
            session_id=session_id,
            purpose=args.purpose,
            model_class=args.model_class,
            policy=policy,
            override_ruling_id=args.override_ruling_id,
        )
        # Lane FB-3 item 8 (custodian observation (b)): the plan-quota capture
        # is written by the statusLine on a UI tick, so it goes stale exactly
        # while a session sits idle -- and a booking sized against a reading
        # that WAS true is the failure mode this closes. Absent capture is not
        # stale; see `staleness`.
        quota_reading = booking_quota_reading(store.program_root, quota_dir=args.quota_dir)
        refusal = booking_refusal(quota_reading)
        if refusal and not args.allow_stale_quota:
            return error_envelope(
                "budget book",
                "stale_quota_capture",
                refusal,
                details=quota_reading,
                next_actions=[
                    next_action(
                        ["trialerror", "budget", "quota"],
                        "re-read the capture (it refreshes on the next Claude Code UI tick)",
                    )
                ],
            )
        attrs = (
            # Recorded ON THE LAUNCH, not just logged: a booking made against
            # a reading nobody could trust is a fact about that booking, and
            # the next person reconstructing the week needs it beside the
            # numbers rather than in a terminal scrollback.
            {"allowed_stale_quota": quota_reading}
            if refusal and args.allow_stale_quota
            else None
        )
        result = book_launch(
            store,
            session_id=session_id,
            program_id=program_id,
            agent_kind=args.agent_kind,
            model_class=args.model_class,
            model=args.model,
            purpose=args.purpose,
            est_tokens=args.est_tokens,
            booking_ttl_s=args.booking_ttl_s,
            parent_launch=args.parent_launch,
            workpackage=args.workpackage,
            attrs=attrs,
            assign_ids=getattr(args, "assign_ids", None),
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
    except UnknownAssignmentError as exc:
        return error_envelope(
            "budget book", "unknown_assignment", str(exc),
            next_actions=[next_action(["trialerror", "lens", "export", "--round-id", "<round>"],
                                      "read this round's assign_ids off the bookable rows")],
        )
    finally:
        store.close()

    payload = result.to_dict()
    # Say what was assumed rather than leaving the operator to infer it: a
    # booking that silently picked its own session is indistinguishable in
    # the envelope from one that was told which, and only one of those is
    # reproducible from the command that made it.
    payload["resolved_from"] = resolved_from
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
    if args.from_event and args.reconcile_source is not None:
        # The number and its label come from the same place: --from-event
        # reads the host's own usage and records `event`. Accepting a second
        # label and ignoring it would make the envelope say something the
        # operator did not ask for.
        return error_envelope(
            "budget reconcile",
            "conflicting_arguments",
            "--from-event records reconcile_source=event, so it cannot be combined with "
            f"--reconcile-source {args.reconcile_source!r}: drop one (--actual-tokens takes the "
            "label, --from-event takes the measurement)",
        )
    store = _open_store(args)
    try:
        if args.from_event:
            result = reconcile_launch_from_event(
                store, launch_id=args.launch_id, spawned_model=args.spawned_model
            )
        else:
            result = reconcile_launch(
                store,
                launch_id=args.launch_id,
                actual_tokens=args.actual_tokens,
                reconcile_source=args.reconcile_source or "manual",
                spawned_model=args.spawned_model,
            )
    except BudgetError as exc:
        # An operator who reached for --from-event and got refused has exactly
        # one other path, and it is the documented one: name it rather than
        # leaving them to re-read the help.
        next_actions = (
            [
                next_action(
                    ["trialerror", "budget", "reconcile", "--launch-id", args.launch_id, "--actual-tokens", "0"],
                    "no measured usage on file: reconcile with the count you have (--actual-tokens)",
                )
            ]
            if args.from_event
            else []
        )
        return error_envelope(
            "budget reconcile", "reconcile_refused", str(exc), next_actions=next_actions
        )
    finally:
        store.close()
    return ok_envelope("budget reconcile", result=result)


def _run_heartbeat(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        result = heartbeat_launch(store, launch_id=args.launch_id)
    except NoOpenSessionError as exc:
        return error_envelope(
            "budget heartbeat",
            "no_open_session",
            str(exc),
            next_actions=[next_action(["trialerror", "session", "boot"], "boot a session first")],
        )
    except LaunchNotOwnedError as exc:
        return error_envelope("budget heartbeat", "launch_not_owned", str(exc))
    except BudgetError as exc:
        return error_envelope("budget heartbeat", "heartbeat_refused", str(exc))
    except RuntimeError as exc:
        # `resolve_open_session` raises a bare RuntimeError for more than one
        # OPEN session, and main() does not catch handler exceptions, so this
        # verb used to exit with a traceback instead of an envelope
        # (fix-accept, V-7). Two open sessions is exactly the state that
        # produces orphan-looking bookings -- the state an operator reaches
        # for `heartbeat` in -- and the same lane taught `budget status` to
        # refuse it cleanly, so the two refusals now read the same.
        return error_envelope(
            "budget heartbeat",
            "multiple_open_sessions",
            f"{exc} -- close one, or reconcile this launch from the session that owns it",
            next_actions=[
                next_action(["trialerror", "doctor", "--only", "session_multiple_open"], "see both sessions")
            ],
        )
    finally:
        store.close()
    return ok_envelope(
        "budget heartbeat",
        result=result,
        next_actions=[
            next_action(
                ["trialerror", "doctor", "--only", "budget_dangling_launches"],
                "confirm this launch no longer reads as past its booking TTL",
            )
        ],
    )


def _resolve_account_id(store, args: argparse.Namespace, command: str) -> tuple[str | None, dict | None]:
    """``--account-id``, or the account the OPEN session is bound to.

    Lane FB-1 item F7. The account is bound at session boot
    (``session.account_id``) and every booking already reads it off that row
    -- only the reporting verbs demanded it back from the caller, which in a
    single-account program is a value the harness knows and the operator has
    to go and look up. Three outcomes, no guessing:

    - one open session -> its account, reported in the envelope as
      ``account_resolved_from``.
    - no open session -> a refusal naming BOTH ways out (the flag, or
      ``session boot``). This is not a case to default: an account guessed
      from, say, "the only account on file" would silently become the wrong
      account the first time a second one exists.
    - more than one open session -> ``resolve_open_session``'s own refusal,
      surfaced verbatim rather than swallowed. Picking one would attribute a
      reading to an account nobody named.
    """
    if args.account_id:
        return args.account_id, None
    from trialerror.budget.gate import resolve_open_session

    try:
        session = resolve_open_session(store)
    except RuntimeError as exc:
        return None, error_envelope(
            command,
            "multiple_open_sessions",
            f"{exc} -- name the account explicitly with --account-id",
            next_actions=[next_action(["trialerror", "doctor", "--only", "session_multiple_open"], "see both sessions")],
        )
    if session is None:
        return None, error_envelope(
            command,
            "no_open_session",
            "no --account-id given and no OPEN session in this program to read one from "
            "(session.account_id is where the default comes from): pass --account-id, or run "
            "`trialerror session boot`",
            next_actions=[next_action(["trialerror", "session", "boot"], "boot a session to bind an account")],
        )
    return session["account_id"], None


def _run_status(args: argparse.Namespace) -> dict:
    store = _open_store(args)
    try:
        account_id, err = _resolve_account_id(store, args, "budget status")
        if err is not None:
            return err
        result = budget_status(store, account_id=account_id, model_class=args.model_class)
        result["account_resolved_from"] = "--account-id" if args.account_id else "open session"
    finally:
        store.close()
    return ok_envelope("budget status", result=result, next_actions=_status_next_actions(result))


def _status_next_actions(result: dict) -> list:
    """Only argv that parses today, and only when the state calls for it
    (FB-1 item F10c)."""
    actions = []
    binding = result.get("binding_limit")
    if binding and (binding.get("over_soft") or binding.get("over_hard")):
        actions.append(
            next_action(
                ["trialerror", "budget", "check", "--account-id", result["account_id"]],
                "read the pool against the plan's own quota windows before booking again",
            )
        )
    if not result.get("pools"):
        actions.append(
            next_action(
                ["trialerror", "budget", "pools", "--account-id", result["account_id"]],
                "no pool for this account yet: list what exists (pools --create makes one)",
            )
        )
    return actions


def _run_check(args: argparse.Namespace) -> dict:
    """``status`` and ``quota`` in one envelope, with the binding limit
    named. Composition only -- both halves are computed by the code that
    already owns them, and this verb adds no arithmetic of its own. The
    quota half degrades exactly as `budget quota` does (``available:
    false`` plus its own note) when nothing has been captured; a missing
    capture is a missing reading, not a failed command.

    Fix pass V-3: the freshness bar is ``[budget] quota_max_age_s``, the
    same one `budget quota`, the `quota_capture_stale` doctor check and the
    booking gate read (``--fresh-within-s`` still wins for one reading).
    This verb was left on the hardcoded default, so one program with one
    config and one capture gave three answers -- and this is the envelope an
    operator reads BEFORE booking, i.e. the one most likely to be trusted
    and then contradicted by a refusal a second later."""
    from trialerror.budget.quota import quota_status, resolve_max_age_s, staleness

    store = _open_store(args)
    try:
        account_id, err = _resolve_account_id(store, args, "budget check")
        if err is not None:
            return err
        status = budget_status(store, account_id=account_id, model_class=args.model_class)
        status["account_resolved_from"] = "--account-id" if args.account_id else "open session"
        program_root = store.program_root
    finally:
        store.close()

    fresh_within = resolve_max_age_s(_load_raw_config(program_root), override=args.fresh_within_s)
    quota = quota_status(args.quota_dir, fresh_within_s=fresh_within)
    quota["staleness"] = staleness(quota, max_age_s=fresh_within)
    binding = status.get("binding_limit")
    if binding is None:
        summary = (
            f"account {account_id}: no budget pool configured, so nothing here binds a booking "
            "(uncapped and unmeasured, not unlimited)"
        )
    else:
        summary = (
            f"account {account_id}: the binding limit is the {binding['limit']} cap of the "
            f"{binding['model_class']} pool, with {binding['visible_headroom_tokens']:.0f} visible "
            f"tokens of headroom left to it (x{binding['billed_multiplier']} on the plan meter)"
        )
    # Lane FB-3 item 7. A booking that is still PROVISIONAL has moved neither
    # the pool's spent_visible_tokens nor the plan meter -- it is a
    # commitment, not a spend -- so an operator reading either of those for
    # "what have I got out?" cannot see it at all. It IS in the projected
    # number above, which is the only place it has ever been, and the point
    # of saying it out loud here is that "committed" and "spent" being
    # different numbers is exactly what made a hookless PROVISIONAL booking
    # look missing on 2026-09-11 and 2026-09-15.
    outstanding = _outstanding_commitments(status)
    if outstanding["committed_visible_tokens"]:
        by_state = ", ".join(f"{n:,} {state}" for state, n in sorted(outstanding["by_state"].items()))
        summary += (
            f"; {outstanding['committed_visible_tokens']:,} visible tokens are committed and not yet "
            f"settled ({by_state}) -- counted in the projection above, and in neither the pool's "
            "spent total nor the plan meter until each launch is reconciled"
        )
    if not quota["available"]:
        summary += "; no plan-quota capture to compare it against"
    elif not quota["fresh"]:
        age = quota.get("age_s")
        age_text = f"{age:.0f}s old" if isinstance(age, (int, float)) else "of unknown age"
        summary += (
            f"; the plan-quota capture is stale ({age_text}, past the {fresh_within}s bar) -- "
            "a booking against it is refused unless --allow-stale-quota is passed"
        )
    return ok_envelope(
        "budget check",
        result={
            "summary": summary,
            "status": status,
            "quota": quota,
            "outstanding_commitments": outstanding,
        },
        next_actions=_status_next_actions(status),
    )


def _outstanding_commitments(status: dict) -> dict:
    """The live (PROVISIONAL/RUNNING) commitment across the pools this status
    reports, with the per-state breakdown -- read off the pool entries rather
    than re-queried, so it cannot disagree with the projection beside it."""
    by_state: dict[str, int] = {}
    for pool in status.get("pools") or []:
        for state, tokens in (pool.get("committed_by_state") or {}).items():
            by_state[state] = by_state.get(state, 0) + int(tokens)
    return {"committed_visible_tokens": sum(by_state.values()), "by_state": by_state}


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
        # D-FB-14 (2): the same projected / soft_cap / hard_cap / committed /
        # headroom columns `budget status` computes and the
        # `budget_pool_overspend` doctor check judges, from the same
        # function -- so a pool the doctor names by id can be read here, and
        # the two surfaces cannot report different numbers about it. `status`
        # shows the current pool per class; this shows every pool, each
        # labelled current or superseded.
        rows = pool_report(store.platform, account_id=args.account_id, model_class=args.model_class)
        superseded = [r for r in rows if not r["judged"]]
        return ok_envelope(
            "budget pools",
            result={
                "pools": rows,
                "current": [r["pool_id"] for r in rows if r["judged"]],
                "superseded": [r["pool_id"] for r in superseded],
                "note": (
                    "superseded pools carry their frozen numbers and no verdict: book_launch only "
                    "ever targets the current pool per (account, model class), so nothing can move "
                    "them"
                    if superseded
                    else "every pool listed is the current one for its (account, model class)"
                ),
            },
        )
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
    from trialerror.budget.quota import (
        quota_status,
        resolve_max_age_s,
        stale_capture_message,
        staleness,
    )

    # Lane FB-3 item 8: the bar is [budget] quota_max_age_s, not a hardcoded
    # 900 -- --fresh-within-s still wins, so a one-off reading at another bar
    # stays possible without editing config.
    program_root = Path(args.program_root) if args.program_root else Path.cwd()
    config = _load_raw_config(program_root)
    fresh_within = resolve_max_age_s(config, override=args.fresh_within_s)
    status = quota_status(args.quota_dir, fresh_within_s=fresh_within)
    reading = staleness(status, max_age_s=fresh_within)
    status["staleness"] = reading
    if not args.ingest:
        next_actions = []
        if reading["stale"]:
            status["note"] = stale_capture_message(reading)
            next_actions.append(
                next_action(
                    ["trialerror", "doctor", "--only", "quota_capture_stale"],
                    "the statusLine capture refreshes on the next Claude Code UI tick; this is the "
                    "same reading the doctor reports",
                )
            )
        return ok_envelope("budget quota", result=status, next_actions=next_actions)
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
        "specify a subcommand: book, heartbeat, reconcile, status, check, pools, "
        "snapshot-ingest, calibrate, rollup, quota",
    )
