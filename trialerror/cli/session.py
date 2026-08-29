"""``trialerror session`` — the session lifecycle's CLI surface. Design Section
5.2 CLI table: "session | boot, close, render-handoff, status | close
REFUSES on dangling launches / stale digest / unread close checklist."
Thin argv/envelope shell over ``trialerror.sessions.lifecycle``/``.handoff`` —
all logic lives there (design Section 5.2 registration rule: "each CLI
group lives in its own module ``trialerror/cli/<group>.py`` ... no
implementation lane ever edits a shared ``cli/__init__.py``"; this file is
that drop-in).

``abandon`` is an extra action not named in design Section 5.2's table —
see the TRIALERROR-DEV-NOTE in ``trialerror/sessions/lifecycle.py``'s module
docstring for why M6 adds it anyway.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.sessions.handoff import rerender_handoff
from trialerror.sessions.lifecycle import abandon_session, boot_session, close_session, session_status
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "session"
HELP = "Session lifecycle: boot, close (refuses on dangling launches / stale digest / unread inbox), render-handoff, status."


def _common(p: argparse.ArgumentParser) -> None:
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root/
    # --platform-root the top-level parser resolved.
    p.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="program scaffold root (default: discovered from CWD via trialerror.toml)"
    )
    p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="session_cmd", metavar="<command>", required=True)

    p_boot = sub.add_parser("boot", help="open (or, by default, reuse) the program's one open session")
    _common(p_boot)
    p_boot.add_argument("--account", default=None, dest="account_id", help="mandatory if >1 account is registered (F14)")
    p_boot.add_argument(
        "--create-account", default=None, dest="create_account_label", metavar="LABEL",
        help="bootstrap convenience: create and use a brand-new account (first-ever boot on a fresh program)",
    )
    p_boot.add_argument("--queue", action="append", default=None, metavar="ITEM", help="seed a desk-item queue entry (repeatable)")
    p_boot.add_argument(
        "--fresh", action="store_true",
        help="refuse (rather than reuse) if a session is already open -- default is to reuse it idempotently",
    )
    p_boot.add_argument("--ts", default=None, help="override the boot timestamp (tests only)")
    p_boot.set_defaults(handler=_cmd_boot)

    p_close = sub.add_parser("close", help="close the open session; refuses on dangling launches / stale digest / unread inbox")
    _common(p_close)
    p_close.add_argument("--session-id", default=None, help="default: the currently open session")
    p_close.add_argument(
        "--course-check", required=True,
        help='JSON object, REQUIRED (design Sec 9.3), e.g. \'{"rungs":"...","build_vs_theory":"...","drift_flag":false}\'',
    )
    p_close.add_argument("--notes", default=None)
    p_close.add_argument(
        "--override-ruling-id", default=None, dest="hook_alive_override_ruling_id",
        help="cite an existing ruling to bypass the hook_alive check (design Sec 5.4: override-only path)",
    )
    p_close.add_argument("--ts", default=None, help="override the close timestamp (tests only)")
    p_close.set_defaults(handler=_cmd_close)

    p_render = sub.add_parser(
        "render-handoff", help="re-flush an already-closed session's handoff file from ops.db truth (no new supersession)"
    )
    _common(p_render)
    p_render.add_argument("--session-id", required=True)
    p_render.set_defaults(handler=_cmd_render_handoff)

    p_status = sub.add_parser("status", help="read-only snapshot of the open session (no side effects; inbox is peeked)")
    _common(p_status)
    p_status.add_argument("--session-id", default=None)
    p_status.set_defaults(handler=_cmd_status)

    p_abandon = sub.add_parser("abandon", help="mark a crashed/never-closed OPEN session 'abandoned' (not in design Sec 5.2's table; see TRIALERROR-DEV-NOTE)")
    _common(p_abandon)
    p_abandon.add_argument("--session-id", required=True)
    p_abandon.add_argument("--reason", default=None)
    p_abandon.add_argument("--ts", default=None)
    p_abandon.set_defaults(handler=_cmd_abandon)

    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace, *, command: str) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            command,
            "program_root_not_found",
            "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    store = open_store(program_root, platform_root=args.platform_root)
    return store, None


def _load_program_config(program_root: Path) -> dict:
    # Same best-effort load every other CLI group's own private copy of
    # this helper already uses (trialerror.cli.ingest._load_program_config,
    # trialerror.cli.law._load_program_config, ...) -- see the build report's
    # deviations for why this isn't consolidated in this pass.
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def _cmd_boot(args: argparse.Namespace) -> dict:
    store, err = _open(args, command="session boot")
    if err is not None:
        return err
    try:
        result = boot_session(
            store,
            account_id=args.account_id,
            create_account_label=args.create_account_label,
            queue=args.queue,
            reuse_open=not args.fresh,
            now_ts=args.ts,
            config=_load_program_config(store.program_root),
        )
    finally:
        store.close()

    if not result.ok:
        next_actions = []
        if result.code == "no_accounts":
            next_actions = [next_action(["trialerror", "session", "boot", "--create-account", "<label>"], "bootstrap the first account")]
        elif result.code == "account_required":
            next_actions = [next_action(["trialerror", "session", "boot", "--account", "<id>"], "boot under a specific account")]
        elif result.code == "session_already_open":
            next_actions = [
                next_action(["trialerror", "session", "close", "--course-check", "<json>"], "close the open session first"),
                next_action(["trialerror", "session", "abandon", "--session-id", result.session_id or "<id>"], "or abandon it if it crashed"),
            ]
        return error_envelope("session boot", result.code, result.message, details=result.to_dict(), next_actions=next_actions)

    return ok_envelope(
        "session boot",
        result=result.to_dict(),
        next_actions=[next_action(["trialerror", "session", "status"], "review the boot bundle again later")],
    )


def _cmd_close(args: argparse.Namespace) -> dict:
    try:
        course_check = json.loads(args.course_check)
    except json.JSONDecodeError as exc:
        return error_envelope("session close", "bad_course_check_json", f"--course-check is not valid JSON: {exc}")

    store, err = _open(args, command="session close")
    if err is not None:
        return err
    try:
        result = close_session(
            store,
            session_id=args.session_id,
            course_check=course_check,
            notes=args.notes,
            hook_alive_override_ruling_id=args.hook_alive_override_ruling_id,
            now_ts=args.ts,
            config=_load_program_config(store.program_root),
        )
    finally:
        store.close()

    if not result.ok:
        next_actions = []
        if result.code == "dangling_launches":
            next_actions = [next_action(["trialerror", "budget", "reconcile", "--launch-id", "<id>", "--actual-tokens", "<n>"], "reconcile each dangling launch")]
        elif result.code == "unread_checklist":
            next_actions = [next_action(["trialerror", "inbox", "read"], "read the unread inbox items")]
        elif result.code == "stale_digest":
            next_actions = [next_action(["trialerror", "law", "diff-foreign", "--pin", "<your boot_pin_version>"], "see what was appended since boot")]
        return error_envelope("session close", result.code, result.message, details=result.to_dict(), next_actions=next_actions)

    return ok_envelope(
        "session close",
        result=result.to_dict(),
        next_actions=[next_action(["trialerror", "session", "boot"], "boot the next session")],
    )


def _cmd_render_handoff(args: argparse.Namespace) -> dict:
    store, err = _open(args, command="session render-handoff")
    if err is not None:
        return err
    try:
        result = rerender_handoff(
            store, session_id=args.session_id, config=_load_program_config(store.program_root)
        )
    except ValueError as exc:
        return error_envelope("session render-handoff", "rerender_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("session render-handoff", result=result.to_dict())


def _cmd_status(args: argparse.Namespace) -> dict:
    store, err = _open(args, command="session status")
    if err is not None:
        return err
    try:
        result = session_status(store, session_id=args.session_id)
    finally:
        store.close()
    return ok_envelope("session status", result=result)


def _cmd_abandon(args: argparse.Namespace) -> dict:
    store, err = _open(args, command="session abandon")
    if err is not None:
        return err
    try:
        result = abandon_session(store, session_id=args.session_id, reason=args.reason, now_ts=args.ts)
    finally:
        store.close()
    if not result.ok:
        return error_envelope("session abandon", result.code, result.message, details=result.to_dict())
    return ok_envelope("session abandon", result=result.to_dict())
