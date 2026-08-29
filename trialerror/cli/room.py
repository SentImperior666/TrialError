"""``trialerror room`` — the brainstorm-rooms runtime CLI surface. Mission brief
(v1-rooms lane): "CLI: trialerror room {create, status, post, score, freeze,
converge-check, export}." Thin wrapper over ``trialerror.rooms.api`` — all logic
lives there; this module only parses argv and shapes the AgentEnvelope
(same split ``trialerror/cli/gate.py``/``trialerror/cli/artifact.py`` document for
M10, and ``trialerror/cli/verify.py`` documents for the judge-callable boundary
this group's ``score`` action reuses).

**Judgment, from the CLI (the LLM-judgment boundary — ``trialerror/rooms/
api.py``'s own module docstring, applied here concretely, same as
``trialerror/cli/verify.py``'s own note for ``hypothesis``):** this process
never calls an LLM. ``score``'s ``--agreement-pct``/``--note`` are values
the CALLER already produced (a moderator agent that already read
:func:`~trialerror.rooms.api.build_moderator_scoring_envelope`'s output
out-of-band, or a test) — wrapped here into a trivial ``judge`` callable
that just returns them, mirroring ``trialerror verify hypothesis``'s
``--judgments-file`` in spirit but scalar (one discussion point's score IS
one number, unlike hypothesis's per-evidence-chunk table).

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by this build.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.rooms.api import (
    check_room_converged,
    converge_room,
    create_room,
    export_room,
    freeze_room,
    get_room,
    list_room_turns,
    post_message,
    score_dp,
)
from trialerror.rooms.errors import RoomsError
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "room"
HELP = "Brainstorm-rooms runtime: create, status, post, score, freeze, converge-check, export."

_PROGRAM_ROOT_HELP = "override the program root (default: discover trialerror.toml upward from CWD)"


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root the
    # top-level parser resolved.
    p.add_argument("--program-root", default=argparse.SUPPRESS, help=_PROGRAM_ROOT_HELP)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_create = actions.add_parser("create", help="open a new room (state='open')")
    _add_program_root_arg(p_create)
    p_create.add_argument("--topic", required=True)
    p_create.add_argument(
        "--dps", required=True, help="JSON array: [{\"dp_id\"?, \"prompt\", \"idea_id\"?}, ...]"
    )
    p_create.add_argument(
        "--participants", required=True, help="comma-separated participant labels, e.g. 'lens_A,lens_B'"
    )
    p_create.add_argument("--rounds-per-dp", type=int, default=None, dest="rounds_per_dp")
    p_create.add_argument(
        "--no-enforce-participant-range", action="store_false", dest="enforce_participant_range",
        help="override the MN-033 2-3 participant soft-enforcement",
    )
    p_create.add_argument("--by-launch", default=None, dest="by_launch")
    p_create.set_defaults(handler=_run_create, enforce_participant_range=True)

    p_status = actions.add_parser("status", help="room row + discussion-point scores + turn counts")
    _add_program_root_arg(p_status)
    p_status.add_argument("--id", required=True, dest="room_id")
    p_status.set_defaults(handler=_run_status)

    p_post = actions.add_parser("post", help="append one turn to a discussion point")
    _add_program_root_arg(p_post)
    p_post.add_argument("--id", required=True, dest="room_id")
    p_post.add_argument("--launch-id", required=True, dest="launch_id")
    p_post.add_argument("--dp", required=True, dest="dp_id")
    body_group = p_post.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", default=None)
    body_group.add_argument("--body-file", default=None, dest="body_file")
    p_post.set_defaults(handler=_run_post)

    p_score = actions.add_parser("score", help="record a moderator's agreement score for one discussion point")
    _add_program_root_arg(p_score)
    p_score.add_argument("--id", required=True, dest="room_id")
    p_score.add_argument("--dp", required=True, dest="dp_id")
    p_score.add_argument("--agreement-pct", required=True, type=float, dest="agreement_pct")
    p_score.add_argument("--note", default=None)
    p_score.add_argument("--by-launch", required=True, dest="by_launch")
    p_score.set_defaults(handler=_run_score)

    p_freeze = actions.add_parser("freeze", help="open -> frozen: moderator escalation with a required reason")
    _add_program_root_arg(p_freeze)
    p_freeze.add_argument("--id", required=True, dest="room_id")
    p_freeze.add_argument("--reason", required=True)
    p_freeze.add_argument("--by-launch", required=True, dest="by_launch")
    p_freeze.set_defaults(handler=_run_freeze)

    p_converge = actions.add_parser(
        "converge-check", help="open -> converged if every discussion point is at/above the bar, else report what's missing"
    )
    _add_program_root_arg(p_converge)
    p_converge.add_argument("--id", required=True, dest="room_id")
    p_converge.add_argument(
        "--apply", action="store_true", help="actually transition the room (default: dry-run report only)"
    )
    p_converge.add_argument("--by-launch", default=None, dest="by_launch", help="required with --apply")
    p_converge.set_defaults(handler=_run_converge_check)

    p_export = actions.add_parser("export", help="render the append-only room doc as markdown (atomic write)")
    _add_program_root_arg(p_export)
    p_export.add_argument("--id", required=True, dest="room_id")
    p_export.add_argument("--out", required=True, dest="out_path")
    p_export.set_defaults(handler=_run_export)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = Path(args.program_root) if args.program_root else find_program_root()
    if root is None:
        return None, error_envelope(
            "room",
            "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(root), None


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(
        "room",
        "no_action",
        "specify an action: create|status|post|score|freeze|converge-check|export",
        next_actions=[next_action(["trialerror", "room", "--help"], "list room actions")],
    )


def _run_create(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        dps = json.loads(args.dps)
        participants = [p.strip() for p in args.participants.split(",") if p.strip()]
        kwargs = {}
        if args.rounds_per_dp is not None:
            kwargs["rounds_per_dp"] = args.rounds_per_dp
        row = create_room(
            store,
            topic=args.topic,
            discussion_points=dps,
            participants=participants,
            enforce_participant_range=args.enforce_participant_range,
            by_launch=args.by_launch,
            **kwargs,
        )
    except (RoomsError, StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("room create", "create_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "room create", result=row,
        next_actions=[next_action(["trialerror", "room", "post", "--id", row["room_id"], "--dp", "DP1", "..."], "post the first turn")],
    )


def _run_status(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        room = get_room(store, args.room_id)
        if room is None:
            return error_envelope("room status", "not_found", f"no such room: {args.room_id!r}")
        convergence = check_room_converged(store, args.room_id)
        turns = list_room_turns(store, room_id=args.room_id)
        turn_counts: dict[str, int] = {}
        for t in turns:
            turn_counts[t["dp_ref"]] = turn_counts.get(t["dp_ref"], 0) + 1
    except (RoomsError, StoreError, ValueError) as exc:
        return error_envelope("room status", "status_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "room status",
        result={"room": room, "convergence": convergence, "turn_count": len(turns), "turn_counts_by_dp_ref": turn_counts},
    )


def _run_post(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        body = args.body if args.body is not None else Path(args.body_file).read_text(encoding="utf-8")
        row = post_message(store, room_id=args.room_id, launch_id=args.launch_id, dp_id=args.dp_id, body=body)
    except (RoomsError, StoreError, ValueError) as exc:
        return error_envelope("room post", "post_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("room post", result=row)


def _run_score(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        # The CLI never calls an LLM (module docstring) -- the caller
        # already produced this number; `judge` just hands it through.
        judge = lambda _envelope: {"agreement_pct": args.agreement_pct, "note": args.note}  # noqa: E731
        row = score_dp(store, room_id=args.room_id, dp_id=args.dp_id, judge=judge, by_launch=args.by_launch)
    except (RoomsError, StoreError, ValueError) as exc:
        return error_envelope("room score", "score_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "room score", result=row,
        next_actions=[next_action(["trialerror", "room", "converge-check", "--id", args.room_id], "check overall convergence")],
    )


def _run_freeze(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        row = freeze_room(store, room_id=args.room_id, by_launch=args.by_launch, reason=args.reason)
    except (RoomsError, StoreError, ValueError) as exc:
        return error_envelope("room freeze", "freeze_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("room freeze", result=row)


def _run_converge_check(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.apply:
            if not args.by_launch:
                return error_envelope("room converge-check", "by_launch_required", "--by-launch is required with --apply")
            row = converge_room(store, room_id=args.room_id, by_launch=args.by_launch)
            result = {"applied": True, "room": row}
        else:
            status = check_room_converged(store, args.room_id)
            result = {"applied": False, "convergence": status}
    except (RoomsError, StoreError, ValueError) as exc:
        return error_envelope("room converge-check", "converge_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("room converge-check", result=result)


def _run_export(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        result = export_room(store, args.room_id, out_path=args.out_path)
    except (RoomsError, StoreError, ValueError) as exc:
        return error_envelope("room export", "export_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("room export", result=result)
