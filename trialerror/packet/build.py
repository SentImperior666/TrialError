"""Building, pushing and reminding: the packet's outward side.

``build`` gathers the open items, the blocking determinations still waiting in
the store, and the transcript archive's audit failures into one packet capped
at ``max_minutes`` of reading. A determination that an open item already
explains (the item carries a ref to it) is left out, so the operator reads it
once, in plain words; the packet lists what it left out and which item covers
each. ``push`` announces it through the configured
notifier (one push per packet, one per 24 hours). ``remind`` nudges once, a few
days later, if items are still open.

The notifier is whatever ``[packet] notify_cmd`` says: a command list to which
the title and the body are appended as the last two arguments. Nothing here
knows a channel, a host or a secret; the sender keeps its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from trialerror.packet.store import (
    PacketError,
    PacketSettings,
    append_jsonl,
    locked,
    parse_ts,
    read_jsonl,
    utc_iso,
)

__all__ = [
    "TRIGGERS",
    "build_packet",
    "push_packet",
    "remind",
    "notify_argv",
    "order_open",
    "covering_items",
    "BODY_LIMIT",
    "PUSH_INTERVAL_HOURS",
]

TRIGGERS = ("session_close", "weekly_limit", "manual")
BODY_LIMIT = 600
FIRST_LIMIT = 200
PUSH_INTERVAL_HOURS = 24
DECIDE_MINUTES = 3
AUDIT_MINUTES = 2
DECIDE_LABEL = "from DECIDE: not yet in plain words"
#: The weekly-limit trigger builds at most one packet per this many days.
WEEKLY_COOLDOWN_DAYS = 5
#: A capture older than this is not read for the weekly-limit trigger.
WEEKLY_CAPTURE_MAX_AGE_S = 24 * 3600


# ---------------------------------------------------------------------- ordering


def _needed_key(item: Mapping[str, Any]) -> tuple[int, str]:
    needed = str(item.get("needed_by", ""))
    return (0, "") if needed == "next-session" else (1, needed)


_PRIORITY_RANK = {"blocking": 0, "normal": 1, "low": 2}


def order_open(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocking first, then by when it is needed, then priority, then age."""
    return sorted(
        items,
        key=lambda i: (
            0 if i.get("priority") == "blocking" else 1,
            _needed_key(i),
            _PRIORITY_RANK.get(str(i.get("priority")), 1),
            str(i.get("created_ts", "")),
        ),
    )


def _own_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    entry = dict(item)
    entry["source"] = "own"
    entry["answerable"] = True
    return entry


# ------------------------------------------------------------ other sources


def _decide_entries(settings: PacketSettings, platform_root: str | Path | None) -> tuple[list[dict[str, Any]], str | None]:
    """The store's blocking determinations, until each DECIDE builder carries the
    plain-words fields itself. Missing store: no entries and a note."""
    from trialerror.dashboard.data import build_determinations_panel
    from trialerror.dashboard.store_ro import open_store_ro

    try:
        ro = open_store_ro(settings.program_root, platform_root=platform_root)
    except Exception as exc:  # noqa: BLE001 - an unreadable store must not stop the packet
        return [], f"the determinations queue could not be read: {type(exc).__name__}"
    try:
        panel = build_determinations_panel(ro)
    except Exception as exc:  # noqa: BLE001
        return [], f"the determinations queue could not be built: {type(exc).__name__}"
    finally:
        ro.close()
    if panel.get("status") != "ok":
        return [], "the program has no operations store yet, so no determinations were added"
    entries = []
    for item in panel.get("items", []):
        if not item.get("blocking"):
            continue
        entries.append(
            {
                "id": f"DECIDE:{item.get('id')}",
                "source": "decide",
                "label": DECIDE_LABEL,
                "what": str(item.get("text") or item.get("artifact_title") or item.get("id")),
                "why": "",
                "consequence": str(item.get("consequence") or ""),
                "options": [],
                "recommended": None,
                "if_undecided": "",
                "needed_by": "next-session",
                "est_minutes": DECIDE_MINUTES,
                "priority": "blocking",
                "refs": [{"label": str(item.get("kind", "item")), "ref": str(item.get("id"))}],
                "answerable": False,
            }
        )
    return entries, None


def _decide_key(ref: str) -> str:
    """The DECIDE entry id a ref names: ``DECIDE:<id>`` as written, or a bare
    ``<id>`` (the determination's own id, as the queue shows it)."""
    ref = ref.strip()
    return ref if ref.startswith("DECIDE:") else f"DECIDE:{ref}"


def covering_items(own: list[dict[str, Any]]) -> dict[str, str]:
    """``{DECIDE entry id: id of the own item that covers it}``. An own item
    covers an entry when one of its refs names the entry's id exactly (see
    :func:`_decide_key`); when two do, the first in packet order wins."""
    covered: dict[str, str] = {}
    for entry in own:
        for ref in entry.get("refs") or []:
            value = ref.get("ref") if isinstance(ref, Mapping) else None
            if isinstance(value, str) and value.strip():
                covered.setdefault(_decide_key(value), str(entry["id"]))
    return covered


def _cap(
    entries: list[dict[str, Any]], max_minutes: float
) -> tuple[list[dict[str, Any]], list[dict[str, str]], float]:
    """Fill the packet in order up to ``max_minutes``: an entry that does not fit,
    and everything after it, waits (a first entry longer than the cap still opens
    the packet)."""
    included: list[dict[str, Any]] = []
    waiting: list[dict[str, str]] = []
    total = 0.0
    for entry in entries:
        est = float(entry.get("est_minutes") or DECIDE_MINUTES)
        if not waiting and (not included or total + est <= max_minutes):
            included.append(entry)
            total += est
        else:
            waiting.append({"id": entry["id"], "what": entry["what"]})
    return included, waiting, total


def _fill(
    own_blocking: list[dict[str, Any]],
    audit_items: list[dict[str, Any]],
    decide: list[dict[str, Any]],
    own_rest: list[dict[str, Any]],
    own: list[dict[str, Any]],
    max_minutes: float,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], float, list[dict[str, str]]]:
    """The capped packet, with a DECIDE entry left out only while the own item
    that covers it is itself in the packet.

    A covering item keeps its own place in the order, so under the cap it can
    be cut to *waiting* while the entry it covers would have fitted. Such an
    entry is put back in its own place and the packet is filled again. Putting
    entries back only pushes later entries further out, never earlier ones in,
    so the set put back only grows and the loop ends. Returns ``(included,
    waiting, total, [{id, covered_by}] for each entry left out)``."""
    covers = covering_items(own)
    left_out = {e["id"] for e in decide if e["id"] in covers}
    while True:
        entries = own_blocking + audit_items + [e for e in decide if e["id"] not in left_out] + own_rest
        included, waiting, total = _cap(entries, max_minutes)
        shown = {e["id"] for e in included}
        restore = {i for i in left_out if covers[i] not in shown}
        if not restore:
            break
        left_out -= restore
    covered = [{"id": e["id"], "covered_by": covers[e["id"]]} for e in decide if e["id"] in left_out]
    return included, waiting, total, covered


def _audit_entries(settings: PacketSettings) -> tuple[list[dict[str, Any]], list[str]]:
    """Failed checks of each configured archive's latest audit, as items; and one
    course line for each clean audit."""
    from trialerror.archive.audit import latest_audit

    entries: list[dict[str, Any]] = []
    lines: list[str] = []
    for directory in settings.archive_dirs:
        report = latest_audit(directory)
        if report is None:
            continue
        host = report.get("host", "?")
        day = str(report.get("generated_ts", ""))[:10]
        if report.get("clean"):
            lines.append(f"Transcript archive ({host}): clean audit on {day}.")
            continue
        for check in report.get("checks", []):
            if check.get("status") != "fail":
                continue
            entries.append(
                {
                    "id": f"AUDIT:{host}:{day}:{check.get('n')}",
                    "source": "audit",
                    "label": "from the transcript archive audit",
                    "what": f"The transcript archive on {host} failed a check ({check.get('name')}): {check.get('sentence')}",
                    "why": (
                        "The archive is the only complete copy of what the agents did once the host deletes its "
                        "own transcripts, and the audit found it is not doing that job."
                    ),
                    "options": [
                        {
                            "key": "a",
                            "label": "Have the custodian fix it before the next session",
                            "consequence": "The archive keeps its guarantee; the next audit should pass.",
                        },
                        {
                            "key": "b",
                            "label": "Leave it for now",
                            "consequence": "The problem stays open and the next audit reports it again.",
                        },
                    ],
                    "recommended": "a",
                    "if_undecided": "The custodian treats it as option a.",
                    "needed_by": "next-session",
                    "est_minutes": AUDIT_MINUTES,
                    "priority": "blocking",
                    "refs": [{"label": "the audit report", "ref": str(report.get("report_md", ""))}],
                    "answerable": False,
                }
            )
    return entries, lines


def _course_lines(settings: PacketSettings) -> list[str]:
    if settings.course_file is None or not settings.course_file.is_file():
        return []
    text = settings.course_file.read_text(encoding="utf-8", errors="replace")
    return [ln.strip() for ln in text.splitlines()[:3] if ln.strip()]


# --------------------------------------------------------------------- rendering


def _minutes(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _answer_line(entry: Mapping[str, Any], settings: PacketSettings) -> str:
    if not entry.get("answerable"):
        return "Answer: tell the custodian, or resolve it where it lives (the determinations queue)."
    if settings.link:
        return f"Answer: {settings.link}"
    return f"Answer: `trialerror packet answer {entry['id']} --choice {entry.get('recommended')}` (pick your own key)"


def _render_markdown(packet: Mapping[str, Any], settings: PacketSettings) -> str:
    items = packet["items"]
    lines = [
        f"# Decisions for you: {len(items)} (about {_minutes(packet['minutes'])} min)",
        "",
        f"Built {packet['built_ts']} ({packet['trigger']}). "
        f"Read in about {_minutes(packet['minutes'])} minutes; answer before the next session.",
        "",
    ]
    if packet["course"]:
        lines += ["## Where things stand", ""] + [f"- {c}" for c in packet["course"]] + [""]
    for number, e in enumerate(items, start=1):
        lines.append(f"## {number}. {e['what']}")
        lines.append("")
        lines.append(
            f"*{e['priority']} · about {_minutes(e['est_minutes'])} min · needed by {e['needed_by']}"
            + (f" · {e['label']}" if e.get("label") else "")
            + "*"
        )
        lines.append("")
        if e.get("why"):
            lines += [f"**Why it matters.** {e['why']}", ""]
        if e.get("consequence"):
            lines += [f"**What happens if you verify.** {e['consequence']}", ""]
        if e.get("options"):
            lines.append("**Options.**")
            for o in e["options"]:
                mark = " **(recommended)**" if o["key"] == e.get("recommended") else ""
                lines.append(f"- `{o['key']}`: {o['label']}{mark}. {o['consequence']}".rstrip())
            lines.append("")
        if e.get("if_undecided"):
            lines += [f"**If nobody decides.** {e['if_undecided']}", ""]
        refs = [r for r in e.get("refs", []) if r.get("ref")]
        if refs:
            lines += ["**Related.** " + "; ".join(f"{r['label']} ({r['ref']})" for r in refs), ""]
        settles = [c["id"] for c in packet.get("decide_covered", []) if c["covered_by"] == e.get("id")]
        if settles:
            lines += [
                "**Also answers.** "
                + ", ".join(settles)
                + " in the determinations queue; it is not listed again in this packet.",
                "",
            ]
        lines += [_answer_line(e, settings), ""]
    if packet["waiting"]:
        lines += ["## Waiting for the next packet", ""] + [f"- {w['what']}" for w in packet["waiting"]] + [""]
    for note in packet.get("notes", []):
        lines.append(f"_{note}_")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------- weekly gate


def _weekly_gate(
    settings: PacketSettings, pct: float, now: datetime, quota_dir: str | None
) -> tuple[str | None, str | None]:
    """``(reason, unsent_packet_id)``: why a ``--when-weekly-pct`` build should not happen
    now (``None`` to go on), and, when a weekly packet was built in the cooldown window but
    never announced, its id, so the caller can announce it again instead of building.
    The cooldown counts announcements (a ``sent.jsonl`` row), not builds: a failed push
    must not silence the trigger for five days."""
    from trialerror.budget.quota import quota_status

    status = quota_status(quota_dir, now_epoch=now.timestamp(), fresh_within_s=WEEKLY_CAPTURE_MAX_AGE_S)
    if not status.get("available"):
        return "no quota capture on this machine", None
    if not status.get("fresh"):
        return "the quota capture is more than a day old", None
    window = (status.get("windows") or {}).get("seven_day") or {}
    used = window.get("used_percentage")
    if not isinstance(used, (int, float)):
        return "the capture carries no seven-day figure", None
    resets = window.get("resets_at")
    try:
        reset_epoch = float(resets) if isinstance(resets, (int, float)) else parse_ts(str(resets)).timestamp()
    except (PacketError, TypeError, ValueError):
        reset_epoch = None
    if reset_epoch is not None and reset_epoch < now.timestamp():
        return "the capture is from before the last weekly reset", None
    if used < pct:
        return f"the weekly limit is at {used:g}%, below {pct:g}%", None
    cutoff = now - timedelta(days=WEEKLY_COOLDOWN_DAYS)
    sent = read_jsonl(settings.sent)
    for row in sent:
        try:
            if row.get("trigger") == "weekly_limit" and not row.get("reminder") and parse_ts(row["pushed_ts"]) > cutoff:
                return f"a weekly-limit packet was already announced on {str(row['pushed_ts'])[:10]}", None
        except (KeyError, PacketError):
            continue
    announced = {r.get("packet_id") for r in sent if not r.get("reminder")}
    newest: tuple[str, str] | None = None
    for path in sorted(settings.built.glob("PACKET_*.json")) if settings.built.is_dir() else []:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            built = parse_ts(meta["built_ts"])
            if meta.get("trigger") == "weekly_limit" and built > cutoff and meta["packet_id"] not in announced:
                if newest is None or meta["built_ts"] > newest[0]:
                    newest = (meta["built_ts"], meta["packet_id"])
        except (OSError, ValueError, KeyError, PacketError):
            continue
    if newest is not None:
        return f"weekly-limit packet {newest[1]} was built on {newest[0][:10]} but never announced", newest[1]
    return None, None


# ------------------------------------------------------------------------- build


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def build_packet(
    settings: PacketSettings,
    trigger: str,
    *,
    dry_run: bool = False,
    platform_root: str | Path | None = None,
    now: datetime | None = None,
    when_weekly_pct: float | None = None,
    quota_dir: str | None = None,
) -> dict[str, Any]:
    if trigger not in TRIGGERS:
        raise PacketError("bad_input", f"--trigger must be one of {list(TRIGGERS)}")
    now = now or datetime.now(timezone.utc)
    if when_weekly_pct is not None:
        reason, unsent = _weekly_gate(settings, when_weekly_pct, now, quota_dir)
        if reason is not None:
            skipped: dict[str, Any] = {"built": False, "skipped": reason}
            if unsent:
                skipped["unsent_packet"] = unsent
            return skipped
    notes: list[str] = []
    pending = [r for r in read_jsonl(settings.pending) if r.get("status") == "open"]
    own = [_own_entry(i) for i in order_open(pending)]
    own_blocking = [e for e in own if e.get("priority") == "blocking"]
    own_rest = [e for e in own if e.get("priority") != "blocking"]
    decide, note = _decide_entries(settings, platform_root)
    if note:
        notes.append(note)
    audit_items, audit_lines = _audit_entries(settings)
    included, waiting, total, decide_covered = _fill(
        own_blocking, audit_items, decide, own_rest, own, settings.max_minutes
    )
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    packet: dict[str, Any] = {
        "packet_id": f"PACKET_{stamp}",
        "trigger": trigger,
        "built_ts": utc_iso(now),
        "max_minutes": settings.max_minutes,
        "minutes": total if not float(total).is_integer() else int(total),
        "course": _course_lines(settings) + audit_lines,
        "items": included,
        "waiting": waiting,
        "decide_covered": decide_covered,
        "notes": notes,
        "link": settings.link,
    }
    markdown = _render_markdown(packet, settings)
    result: dict[str, Any] = {"built": True, "dry_run": dry_run, "packet": packet}
    if dry_run:
        result["markdown"] = markdown
        return result
    settings.built.mkdir(parents=True, exist_ok=True)
    base, suffix = packet["packet_id"], 1
    while (settings.built / f"{packet['packet_id']}.json").exists():
        suffix += 1
        packet["packet_id"] = f"{base}_{suffix}"
    markdown = _render_markdown(packet, settings)
    md_path = settings.built / f"{packet['packet_id']}.md"
    json_path = settings.built / f"{packet['packet_id']}.json"
    # temp-and-replace, .json last: a push or remind racing this build sees either no packet
    # or a whole one, never a half-written file
    _write_atomic(md_path, markdown)
    _write_atomic(json_path, json.dumps(packet, indent=2, ensure_ascii=False))
    result.update({"path_md": str(md_path), "path_json": str(json_path)})
    return result


# -------------------------------------------------------------------------- push


def notify_argv(cmd: list[str], title: str, body: str) -> list[str]:
    """The command to run: ``cmd`` plus the title and body as the last two
    arguments. When the notifier is reached through ``ssh``, the remote shell
    re-splits its arguments, so those two are quoted for it."""
    exe = Path(cmd[0]).name.lower()
    if exe in ("ssh", "ssh.exe"):
        return [*cmd, shlex.quote(title), shlex.quote(body)]
    return [*cmd, title, body]


def _load_packet(settings: PacketSettings, packet_id: str | None) -> tuple[dict[str, Any], Path]:
    if packet_id:
        name = packet_id if packet_id.startswith("PACKET_") else f"PACKET_{packet_id}"
        path = settings.built / f"{name}.json"
    else:
        found = sorted(settings.built.glob("PACKET_*.json")) if settings.built.is_dir() else []
        if not found:
            raise PacketError("no_packet", "no packet has been built yet; run `packet build` first")
        path = found[-1]
    if not path.is_file():
        raise PacketError("no_packet", f"no built packet {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8")), path.with_suffix(".md")
    except (OSError, ValueError) as exc:
        raise PacketError("corrupt_file", f"{path} cannot be read: {exc}") from exc


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _compose(count: int, minutes: float, first: str, link: str, *, reminder: bool) -> tuple[str, str]:
    plural = "s" if count != 1 else ""
    mins = _minutes(minutes)
    if reminder:
        title = f"Reminder: {count} decision{plural} still open (about {mins} min)"
        lead = f"{count} decision{plural} from the last packet {'are' if count != 1 else 'is'} still open (about {mins} minutes)."
    else:
        title = f"Decisions for you: {count} (about {mins} min)"
        lead = f"{count} decision{plural} {'need' if count != 1 else 'needs'} you before the next session (about {mins} minutes)."
    tail = f" Read: {link}."
    first_part = _cut(first, FIRST_LIMIT)
    body = f"{lead} First: {first_part}.{tail}"
    if len(body) > BODY_LIMIT:
        room = BODY_LIMIT - len(f"{lead} First: .{tail}")
        body = f"{lead} First: {_cut(first, max(room, 10))}.{tail}"
    return title, body[:BODY_LIMIT]


def _run_notifier(
    settings: PacketSettings, title: str, body: str, runner: Callable[..., Any]
) -> None:
    if not settings.notify_cmd:
        raise PacketError(
            "no_notify_cmd",
            "no notifier is configured; set [packet] notify_cmd in trialerror.toml "
            "(a command list; the title and body are appended as its last two arguments)",
        )
    argv = notify_argv(settings.notify_cmd, title, body)
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PacketError("push_failed", f"the notifier could not be run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        detail = _cut((proc.stderr or proc.stdout or "").strip(), 200)
        raise PacketError("push_failed", f"the notifier exited {proc.returncode}: {detail}".rstrip(": "))


def push_packet(
    settings: PacketSettings,
    *,
    packet_id: str | None = None,
    force: bool = False,
    now: datetime | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    packet, md_path = _load_packet(settings, packet_id)
    items = packet.get("items", [])
    if not items:
        raise PacketError("empty_packet", "the packet has no decisions in it, so nothing was sent")
    with locked(settings):
        sent = read_jsonl(settings.sent)
        if not force:
            if any(r.get("packet_id") == packet["packet_id"] and not r.get("reminder") for r in sent):
                raise PacketError("already_pushed", f"{packet['packet_id']} was already announced; --force sends it again")
            recent = [
                parse_ts(r["pushed_ts"])
                for r in sent
                if r.get("pushed_ts") and now - parse_ts(r["pushed_ts"]) < timedelta(hours=PUSH_INTERVAL_HOURS)
            ]
            if recent:
                when = max(recent) + timedelta(hours=PUSH_INTERVAL_HOURS)
                raise PacketError(
                    "push_limit_24h",
                    f"a notification went out less than {PUSH_INTERVAL_HOURS} hours ago; the next is allowed after "
                    f"{utc_iso(when)} (--force overrides)",
                    {"next_allowed_ts": utc_iso(when)},
                )
        link = packet.get("link") or settings.link or str(md_path)
        title, body = _compose(len(items), packet["minutes"], str(items[0]["what"]), str(link), reminder=False)
        _run_notifier(settings, title, body, runner)
        row = {
            "packet_id": packet["packet_id"],
            "trigger": packet.get("trigger"),
            "pushed_ts": utc_iso(now),
            "reminder": False,
            "force": bool(force),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
        append_jsonl(settings.sent, row)
    return {"packet_id": packet["packet_id"], "title": title, "body": body, "pushed_ts": row["pushed_ts"], "forced": bool(force)}


# ----------------------------------------------------------------------- remind


def remind(
    settings: PacketSettings,
    *,
    now: datetime | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """One reminder per pushed packet, once it is ``remind_after_days`` old and some
    of its own items are still open."""
    now = now or datetime.now(timezone.utc)
    with locked(settings):
        sent = read_jsonl(settings.sent)
        pushes = [r for r in sent if not r.get("reminder") and r.get("pushed_ts")]
        if not pushes:
            return {"reminded": False, "reason": "no packet has been pushed yet"}
        last = max(pushes, key=lambda r: parse_ts(r["pushed_ts"]))
        if any(r.get("reminder") and r.get("packet_id") == last["packet_id"] for r in sent):
            return {"reminded": False, "reason": f"{last['packet_id']} was already reminded once"}
        age = now - parse_ts(last["pushed_ts"])
        if age < timedelta(days=settings.remind_after_days):
            return {
                "reminded": False,
                "reason": f"the push is {age.days} day(s) old; a reminder waits {settings.remind_after_days}",
            }
        packet, md_path = _load_packet(settings, last["packet_id"])
        open_ids = {r.get("id") for r in read_jsonl(settings.pending) if r.get("status") == "open"}
        still = [i for i in packet.get("items", []) if i.get("source") == "own" and i.get("id") in open_ids]
        if not still:
            return {"reminded": False, "reason": "none of the packet's items are still open"}
        minutes = sum(float(i.get("est_minutes") or 0) for i in still)
        link = packet.get("link") or settings.link or str(md_path)
        title, body = _compose(len(still), minutes, str(still[0]["what"]), str(link), reminder=True)
        _run_notifier(settings, title, body, runner)
        append_jsonl(
            settings.sent,
            {
                "packet_id": packet["packet_id"],
                "trigger": packet.get("trigger"),
                "pushed_ts": utc_iso(now),
                "reminder": True,
                "force": False,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            },
        )
    return {"reminded": True, "packet_id": packet["packet_id"], "title": title, "body": body, "open_items": len(still)}
