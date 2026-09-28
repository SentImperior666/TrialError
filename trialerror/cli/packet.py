"""``trialerror packet`` — the weekly decision packet. Items waiting for the
operator go in with ``add``; ``build`` gathers them (plus the store's blocking
determinations and the transcript archive's audit failures) into one packet of
at most ``[packet] max_minutes`` of reading, leaving out a determination an
item's ``--ref`` already covers; ``push`` announces it with one
notification; ``answer`` records a decision; ``list --answered-since`` hands the
answers back to the next session; ``remind`` nudges once, mid-week.

Everything is plain files under the program root (``packet/``), so there is no
store migration. The notifier is ``[packet] notify_cmd`` in ``trialerror.toml``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.events.cli_support import program_root_argument
from trialerror.packet import build as build_mod
from trialerror.packet import store as pstore
from trialerror.packet.store import PacketError, PacketSettings
from trialerror.util.config import ConfigError, find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "packet"
HELP = "The weekly decision packet: `packet add | list | build | push | answer | withdraw | remind`."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_add = actions.add_parser("add", help="add one decision that is waiting for the operator")
    program_root_argument(p_add)
    p_add.add_argument("--what", help="what is being decided (at most 300 characters)")
    p_add.add_argument("--why", help="why it matters and what it unblocks (at most 500 characters)")
    p_add.add_argument(
        "--option", action="append", default=[], metavar="KEY=LABEL::CONSEQUENCE",
        help="one option (repeat at least twice); the consequence is what choosing it leads to",
    )
    p_add.add_argument("--recommend", help="the key of the option you recommend")
    p_add.add_argument("--if-undecided", dest="if_undecided", help="what happens by default if nobody decides")
    p_add.add_argument("--needed-by", dest="needed_by", help="'next-session' or a date YYYY-MM-DD")
    p_add.add_argument("--asked-by", dest="asked_by", help="who is asking, in words (a unit, lane or session)")
    p_add.add_argument("--est-minutes", dest="est_minutes", type=float, default=None, help="reading and deciding time (default 3)")
    p_add.add_argument("--priority", choices=list(pstore.PRIORITIES), default=None, help="default: normal")
    p_add.add_argument(
        "--ref", action="append", default=[], metavar="LABEL::REF",
        help="something to look at: a label in words, then a path, id or link (repeatable); a ref naming a "
             "determinations-queue entry (DECIDE:<id>, or the bare <id>) covers it, and `build` then leaves the "
             "raw entry out",
    )
    p_add.add_argument("--file", help="read the item from a JSON file instead of flags")
    p_add.add_argument("--strict", action="store_true", help="refuse an item whose wording the plain-words lint warns about")
    p_add.set_defaults(handler=run_add)

    p_list = actions.add_parser("list", help="open items, and/or answers recorded since a time")
    program_root_argument(p_list)
    p_list.add_argument("--open", action="store_true", help="the open items (the default when no other flag is given)")
    p_list.add_argument("--answered-since", dest="answered_since", metavar="ISO", help="answers recorded at or after this time")
    p_list.set_defaults(handler=run_list)

    p_build = actions.add_parser("build", help="gather everything waiting into one packet")
    program_root_argument(p_build)
    p_build.add_argument("--trigger", required=True, choices=list(build_mod.TRIGGERS))
    p_build.add_argument("--dry-run", action="store_true", help="show the packet; write nothing")
    p_build.add_argument(
        "--when-weekly-pct", dest="when_weekly_pct", type=float, default=None, metavar="N",
        help="build only when the captured weekly limit is at least N%% (and none was announced in the last 5 days; an unannounced one is announced again with --push)",
    )
    p_build.add_argument("--push", action="store_true", help="also announce the packet just built")
    p_build.set_defaults(handler=run_build)

    p_push = actions.add_parser("push", help="announce a packet with one notification")
    program_root_argument(p_push)
    p_push.add_argument("--packet", help="the packet id (default: the newest)")
    p_push.add_argument("--force", action="store_true", help="send even if this packet, or any packet within 24 h, was announced")
    p_push.set_defaults(handler=run_push)

    p_answer = actions.add_parser("answer", help="record the operator's decision on one item")
    program_root_argument(p_answer)
    p_answer.add_argument("item")
    p_answer.add_argument("--choice", required=True, help="the key of the chosen option")
    p_answer.add_argument("--note", default="")
    p_answer.add_argument("--by", default="operator")
    p_answer.set_defaults(handler=run_answer)

    p_withdraw = actions.add_parser("withdraw", help="take an item out, with a reason")
    program_root_argument(p_withdraw)
    p_withdraw.add_argument("item")
    p_withdraw.add_argument("--reason", required=True)
    p_withdraw.set_defaults(handler=run_withdraw)

    p_remind = actions.add_parser("remind", help="one reminder, a few days after a push, if items are still open")
    program_root_argument(p_remind)
    p_remind.set_defaults(handler=run_remind)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(GROUP_NAME, "no_action", "specify one of: add, list, build, push, answer, withdraw, remind")


def _settings(args: argparse.Namespace, command: str) -> tuple[PacketSettings | None, dict | None]:
    root = getattr(args, "program_root", None)
    root_path = Path(root) if root else find_program_root()
    if root_path is None:
        return None, error_envelope(
            command, "program_root_not_found",
            "no trialerror.toml found walking up from the current directory, and --program-root not given",
        )
    try:
        return pstore.packet_settings(root_path), None
    except ConfigError as exc:
        return None, error_envelope(command, "bad_config", str(exc))


def _refusal(command: str, exc: PacketError) -> dict:
    return error_envelope(command, exc.code, exc.message, details=exc.details)


def _split_pair(text: str, what: str) -> tuple[str, str]:
    left, sep, right = text.partition("::")
    if not sep:
        raise PacketError("bad_input", f"{what} '{text}' must be written LEFT::RIGHT")
    return left.strip(), right.strip()


def _item_from_flags(args: argparse.Namespace) -> dict:
    options = []
    for text in args.option:
        head, sep, rest = text.partition("=")
        if not sep:
            raise PacketError("bad_input", f"--option '{text}' must be written KEY=LABEL::CONSEQUENCE")
        label, sep2, consequence = rest.partition("::")
        option = {"key": head.strip(), "label": label.strip()}
        if sep2:
            option["consequence"] = consequence.strip()
        options.append(option)
    refs = []
    for text in args.ref:
        label, ref = _split_pair(text, "--ref")
        refs.append({"label": label, "ref": ref})
    raw = {
        "what": args.what, "why": args.why, "options": options, "recommended": args.recommend,
        "if_undecided": args.if_undecided, "needed_by": args.needed_by, "asked_by": args.asked_by,
        "priority": args.priority, "refs": refs,
    }
    if args.est_minutes is not None:
        raw["est_minutes"] = args.est_minutes
    return raw


def run_add(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet add")
    if err:
        return err
    try:
        if args.file:
            try:
                raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PacketError("bad_input", f"--file {args.file} cannot be read as JSON: {exc}") from exc
            if not isinstance(raw, dict):
                raise PacketError("bad_input", "--file must hold one JSON object (an item)")
        else:
            raw = _item_from_flags(args)
        item, warnings = pstore.add_item(settings, raw, strict=args.strict)
    except PacketError as exc:
        return _refusal("packet add", exc)
    return ok_envelope(
        "packet add",
        result={"item": item, "lint": warnings},
        warnings=[{"code": "plain_words", "message": w} for w in warnings] or None,
    )


def run_list(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet list")
    if err:
        return err
    try:
        result = pstore.list_items(
            settings, open_only=bool(args.open) or not args.answered_since, answered_since=args.answered_since
        )
    except PacketError as exc:
        return _refusal("packet list", exc)
    return ok_envelope("packet list", result=result)


def run_build(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet build")
    if err:
        return err
    try:
        result = build_mod.build_packet(
            settings, args.trigger, dry_run=args.dry_run, platform_root=getattr(args, "platform_root", None),
            when_weekly_pct=args.when_weekly_pct,
        )
        actions = []
        if result.get("unsent_packet") and args.push and not args.dry_run:
            # the weekly trigger's earlier push failed: announce that packet again, build nothing new
            try:
                result["push"] = build_mod.push_packet(settings, packet_id=result["unsent_packet"])
            except PacketError as exc:
                result["push_error"] = {"code": exc.code, "message": exc.message}
        if result.get("built") and not args.dry_run:
            if args.push:
                try:
                    result["push"] = build_mod.push_packet(settings, packet_id=result["packet"]["packet_id"])
                except PacketError as exc:
                    result["push_error"] = {"code": exc.code, "message": exc.message}
            elif result["packet"]["items"]:
                argv = ["trialerror", "packet", "push", "--packet", result["packet"]["packet_id"]]
                if getattr(args, "program_root", None):
                    argv += ["--program-root", str(args.program_root)]
                actions.append(next_action(argv, "announce this packet with one notification"))
    except PacketError as exc:
        return _refusal("packet build", exc)
    warnings = [{"code": "packet_note", "message": n} for n in (result.get("packet") or {}).get("notes", [])]
    if "push_error" in result:
        warnings.append({"code": result["push_error"]["code"], "message": result["push_error"]["message"]})
    return ok_envelope("packet build", result=result, next_actions=actions, warnings=warnings or None)


def run_push(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet push")
    if err:
        return err
    try:
        result = build_mod.push_packet(settings, packet_id=args.packet, force=args.force)
    except PacketError as exc:
        return _refusal("packet push", exc)
    return ok_envelope("packet push", result=result)


def run_answer(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet answer")
    if err:
        return err
    try:
        result = pstore.answer_item(settings, args.item, args.choice, note=args.note, by=args.by)
    except PacketError as exc:
        return _refusal("packet answer", exc)
    return ok_envelope("packet answer", result=result)


def run_withdraw(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet withdraw")
    if err:
        return err
    try:
        result = pstore.withdraw_item(settings, args.item, args.reason)
    except PacketError as exc:
        return _refusal("packet withdraw", exc)
    return ok_envelope("packet withdraw", result={"item": result})


def run_remind(args: argparse.Namespace) -> dict:
    settings, err = _settings(args, "packet remind")
    if err:
        return err
    try:
        result = build_mod.remind(settings)
    except PacketError as exc:
        return _refusal("packet remind", exc)
    return ok_envelope("packet remind", result=result)
