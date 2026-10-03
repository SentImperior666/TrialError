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
    lint_item,
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
    "answered_covers",
    "ANSWERED_COVER_DAYS",
    "BODY_LIMIT",
    "PUSH_INTERVAL_HOURS",
]

TRIGGERS = ("session_close", "weekly_limit", "manual")
BODY_LIMIT = 600
FIRST_LIMIT = 200
PUSH_INTERVAL_HOURS = 24
DECIDE_MINUTES = 3
AUDIT_MINUTES = 2
#: L10 part C: an entry that reaches the packet is fully explained (design
#: §4 item 1) -- the raw "not yet in plain words" wording this label used
#: to carry is no longer true of anything `_decide_entries` returns.
DECIDE_LABEL = "from the determinations queue"
#: How many days an answered own item keeps covering the DECIDE entry it
#: named, before the entry can reach the packet again as a fresh decision
#: (design §4 item 4, the review's N-1 finding) -- long enough that the action the
#: answer promised has had a chance to actually happen.
ANSWERED_COVER_DAYS = 7
#: The operator's own plain-words fields (design §3 B1) an operator-owned
#: DECIDE item must carry in full before it may reach the packet as a
#: decision; anything short of this goes to ``needs_explaining`` instead
#: (design §4 item 1).
_REQUIRED_OPERATOR_FIELDS = ("what", "why", "options", "if_undecided", "needed_by")
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


def _resolved_ref(ro, label: str | None, ref_id: str) -> dict[str, str]:
    """One ref, with its resolved sentence attached (design §4 item 2:
    "each ref is rendered through describe() as '<label>: <kind_words>
    ...(<state_words>) [<id>]'"). ``describe()`` never raises and never
    guesses; an id this build's resolver does not recognise still gets a
    ref, just without a ``resolved`` sentence (the renderer falls back to
    the plain ``label (ref)`` form)."""
    from trialerror.resolve import describe
    from trialerror.resolve.base import describe_line

    entry = {"label": label or "related", "ref": str(ref_id)}
    try:
        desc = describe(str(ref_id), ro)
        if desc.found:
            # a label that only repeats the resolver's own kind_words (S-5's "a
            # source" refs, one per underlying source) would read "a source: a
            # source '...'" -- redundant, so it is omitted from the sentence
            # itself; the plain label(ref) fallback below still uses it.
            entry["resolved"] = describe_line(desc, label=None if label == desc.kind_words else label)
    except Exception:  # noqa: BLE001 - a ref that cannot be resolved must not stop the packet
        pass
    return entry


def _missing_operator_fields(item: Mapping[str, Any]) -> list[str]:
    """The plain-words fields (design §3 B1) this operator item is still
    missing. ``recommended`` may be ``None`` only for a ``"sample"``-kind
    packet item (design §4 item 1b) -- L6 D's calibration sample, built
    after this lane; no DECIDE builder this harness has today produces one,
    so a missing recommendation is always reported here."""
    missing = [f for f in _REQUIRED_OPERATOR_FIELDS if not item.get(f)]
    if item.get("recommended") is None:
        missing.append("recommended")
    return missing


def _operator_entry(item: Mapping[str, Any], ro) -> dict[str, Any]:
    # S-5: a combined acquisition entry carries the underlying source ids
    # separately (its own "id" is the stable "ACQ:open") -- one resolvable
    # ref per source, capped the same way as the heading, never the raw
    # comma-joined id list describe() cannot read.
    source_ids = item.get("_source_ids")
    if source_ids:
        refs = [_resolved_ref(ro, "a source", sid) for sid in source_ids]
    else:
        refs = [_resolved_ref(ro, str(item.get("kind", "item")), str(item["id"]))]
    return {
        "id": f"DECIDE:{item['id']}",
        "source": "decide",
        "label": DECIDE_LABEL,
        "what": str(item["what"]),
        "why": str(item["why"]),
        "consequence": "",  # each option carries its own consequence (design §4 item 2)
        "options": item["options"],
        "recommended": item.get("recommended"),
        "if_undecided": str(item["if_undecided"]),
        "needed_by": str(item["needed_by"]),
        "est_minutes": DECIDE_MINUTES,
        "priority": "blocking" if item.get("blocking") else "normal",
        "refs": refs,
        "answerable": False,
        "kind": "decision",
    }


#: Review S-5: an uncapped heading over thousands of open sources produced a
#: single multi-thousand-character line (probe Q3: 5,000 sources -> a
#: 58,937-character heading). Show a handful, then say how many more.
_ACQ_TITLES_SHOWN = 5


def _combine_acquisitions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Design §4 item 1: "non-blocking items of one kind become one item...
    many small items then cannot push [other decisions] past the cap." Only
    ``acquisition`` can produce more than one row per build (each other
    operator kind's own builder already emits at most one).

    Review S-5 fixed four things in the combined entry: the heading is
    capped (above) instead of joining every open title; the ref is one
    resolvable id per source (capped the same way) instead of a raw
    ``ACQ:<id>,<id>,...`` list ``describe()`` cannot read; ``why`` and
    ``options`` are written in the plural, for the group, not copied from
    the first item alone; and the id is the stable ``ACQ:open`` rather than
    one that changes shape every time the open set does, so a ``--ref
    "...::DECIDE:ACQ:open"`` cover stays attached across builds."""
    if len(items) <= 1:
        return items
    shown = items[:_ACQ_TITLES_SHOWN]
    titles = ", ".join(str(i.get("title")) for i in shown)
    if len(items) > len(shown):
        titles += f", and {len(items) - len(shown)} more"
    combined = dict(items[0])
    combined["id"] = "ACQ:open"
    combined["what"] = f"{len(items)} sources are waiting for you to obtain them: {titles}"
    combined["why"] = "Requested; only you can obtain them."
    combined["options"] = [
        {"key": "deliver", "label": "Deliver them",
         "consequence": "They are marked delivered, and ingestion can proceed."},
        {"key": "not_now", "label": "Not now",
         "consequence": "They stay requested and return in a later packet."},
        {"key": "drop", "label": "Drop the requests",
         "consequence": "Nothing further happens unless they are requested again."},
    ]
    combined["if_undecided"] = "They stay requested and reappear in a later packet."
    combined["_source_ids"] = [str(i["id"]) for i in shown]
    return [combined]


def _decide_entries(
    settings: PacketSettings, platform_root: str | Path | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """The determinations queue's operator-owned items, kept ONLY when fully
    explained (design §4 item 1: "the packet carries only explained operator
    decisions"). Returns ``(entries, needs_explaining, note)``:
    ``needs_explaining`` is ``[{id, kind, reason}]`` for an operator item the
    builder has not (yet) given every plain-words field -- it never reaches
    the packet as a raw entry; ``build`` reports the list so the custodian or
    the orchestrator can add a covering item (``--ref
    "<label>::DECIDE:<id>"``). Missing store: no entries and a note."""
    from trialerror.dashboard.data import build_determinations_panel
    from trialerror.dashboard.store_ro import open_store_ro

    try:
        ro = open_store_ro(settings.program_root, platform_root=platform_root)
    except Exception as exc:  # noqa: BLE001 - an unreadable store must not stop the packet
        return [], [], f"the determinations queue could not be read: {type(exc).__name__}"
    try:
        panel = build_determinations_panel(ro)
        if panel.get("status") != "ok":
            return [], [], "the program has no operations store yet, so no determinations were added"

        operator_items = [i for i in panel.get("items", []) if i.get("owner") == "operator"]
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in operator_items:
            by_kind.setdefault(str(item.get("kind")), []).append(item)
        if "acquisition" in by_kind:
            by_kind["acquisition"] = _combine_acquisitions(by_kind["acquisition"])

        entries: list[dict[str, Any]] = []
        needs_explaining: list[dict[str, Any]] = []
        for kind, items in by_kind.items():
            for item in items:
                missing = _missing_operator_fields(item)
                if missing:
                    needs_explaining.append(
                        {"id": str(item["id"]), "kind": kind, "reason": f"missing {', '.join(missing)}"}
                    )
                    continue
                entries.append(_operator_entry(item, ro))
        entries.sort(key=lambda e: e.get("priority") != "blocking")  # blocking first (design §4 item 1)
        return entries, needs_explaining, None
    except Exception as exc:  # noqa: BLE001
        return [], [], f"the determinations queue could not be built: {type(exc).__name__}"
    finally:
        ro.close()


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


def answered_covers(settings: PacketSettings, now: datetime) -> dict[str, dict[str, Any]]:
    """``{DECIDE entry id: {item_id, what, choice_label, decided_ts}}`` for
    every own item answered within :data:`ANSWERED_COVER_DAYS` whose refs
    name a DECIDE entry (design §4 item 4, the review's N-1 finding): "an answered
    item keeps covering its DECIDE entry for 7 days after its answer... The
    packet lists such items under 'Already answered, being carried out'."
    The FIRST answer to cover a given entry wins, same as
    :func:`covering_items`."""
    pending_by_id = {r.get("id"): r for r in read_jsonl(settings.pending)}
    cutoff = now - timedelta(days=ANSWERED_COVER_DAYS)
    covers: dict[str, dict[str, Any]] = {}
    for ans in read_jsonl(settings.answers):
        try:
            decided_ts = parse_ts(ans["decided_ts"])
        except (KeyError, PacketError):
            continue
        if not (cutoff <= decided_ts <= now):  # too old, or answered after this very build (a clock oddity)
            continue
        item = pending_by_id.get(ans.get("item_id"))
        if not item:
            continue
        label = next((o["label"] for o in item.get("options", []) if o.get("key") == ans.get("choice")), None)
        for ref in item.get("refs") or []:
            value = ref.get("ref") if isinstance(ref, Mapping) else None
            if isinstance(value, str) and value.strip():
                covers.setdefault(
                    _decide_key(value),
                    {
                        "item_id": item["id"], "what": item.get("what"), "choice": ans.get("choice"),
                        "choice_label": label, "decided_ts": ans["decided_ts"],
                    },
                )
    return covers


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


def _render_ref(r: Mapping[str, Any]) -> str:
    """Design §4 item 2: a ref this build could resolve reads as
    ``"<label>: <kind_words> '<title>' (<state_words>) [<id>]"`` (the id
    last, in brackets); one it could not (an "own" item's ``--ref`` can name
    a path or a link, never necessarily a store id) falls back to the
    original ``label (ref)`` form."""
    resolved = r.get("resolved")
    return str(resolved) if resolved else f"{r['label']} ({r['ref']})"


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
            lines += ["**Related.** " + "; ".join(_render_ref(r) for r in refs), ""]
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
    if packet.get("needs_explaining"):
        n = len(packet["needs_explaining"])
        lines += [
            f"_{n} more decision{'s' if n != 1 else ''} {'are' if n != 1 else 'is'} waiting for an explanation "
            "before they can reach you; the custodian has been told._",
            "",
        ]
    if packet.get("already_answered"):
        lines += ["## Already answered, being carried out", ""]
        lines += [
            f"- {a['what']} → {a.get('choice_label') or a['choice']} (on {str(a['decided_ts'])[:10]})"
            for a in packet["already_answered"]
        ]
        lines.append("")
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
    decide, needs_explaining, note = _decide_entries(settings, platform_root)
    if note:
        notes.append(note)
    # design §4 item 4: an answered own item keeps covering its DECIDE entry
    # for ANSWERED_COVER_DAYS -- the entry never reaches the packet again as
    # a fresh decision in that window; it is listed once, as already done.
    covers = answered_covers(settings, now)
    already_answered: list[dict[str, Any]] = []
    if covers:
        still_open_decide = []
        for entry in decide:
            covered_by = covers.get(entry["id"])
            if covered_by:
                already_answered.append(covered_by)
            else:
                still_open_decide.append(entry)
        decide = still_open_decide
    audit_items, audit_lines = _audit_entries(settings)
    included, waiting, total, decide_covered = _fill(
        own_blocking, audit_items, decide, own_rest, own, settings.max_minutes
    )
    # design §4 item 3: the plain-words lint now also runs over every item
    # `build` assembles, never refusing (only `add --strict` does that) --
    # it reports what it finds so the custodian/orchestrator can fix wording
    # before the operator ever sees it.
    lint_warnings = [{"id": e["id"], "warnings": w} for e in included if (w := lint_item(e))]
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
        "needs_explaining": needs_explaining,
        "already_answered": already_answered,
        "lint_warnings": lint_warnings,
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


def _compose(count: int, minutes: float, link: str, *, reminder: bool) -> tuple[str, str]:
    """Design §5 D1: "the body carries no content (a change from L5 C3). A
    push sends only counts and the link... item titles and topics never go
    to the notification service." Whatever channel finally delivers this
    (``notify_cmd`` directly, or the outbox and a host job) sees the same
    two counts-and-a-link strings -- never a decision's own wording."""
    plural = "s" if count != 1 else ""
    mins = _minutes(minutes)
    if reminder:
        title = f"Reminder: {count} decision{plural} still open (about {mins} min)"
        lead = f"{count} decision{plural} from the last packet {'are' if count != 1 else 'is'} still open (about {mins} minutes)."
    else:
        title = f"Decisions for you: {count} (about {mins} min)"
        lead = f"{count} decision{plural} {'need' if count != 1 else 'needs'} you before the next session (about {mins} minutes)."
    body = f"{lead} Read: {link}."
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


def _pending_outbox(settings: PacketSettings, kind: str) -> list[dict[str, Any]]:
    if not settings.outbox:
        return []
    from trialerror.packet import outbox as outbox_mod

    return [e for e in outbox_mod.pending_entries(settings) if e.get("kind") == kind]


def push_packet(
    settings: PacketSettings,
    *,
    packet_id: str | None = None,
    force: bool = False,
    now: datetime | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if settings.outbox:
        from trialerror.packet import outbox as outbox_mod

        outbox_mod.reconcile_receipts(settings, now=now)
    packet, md_path = _load_packet(settings, packet_id)
    items = packet.get("items", [])
    if not items:
        raise PacketError("empty_packet", "the packet has no decisions in it, so nothing was sent")
    with locked(settings):
        sent = read_jsonl(settings.sent)
        pending_push = _pending_outbox(settings, "push")
        if not force:
            already = any(r.get("packet_id") == packet["packet_id"] and not r.get("reminder") for r in sent)
            already = already or any(e.get("packet_id") == packet["packet_id"] for e in pending_push)
            if already:
                raise PacketError("already_pushed", f"{packet['packet_id']} was already announced; --force sends it again")
            recent_times = [parse_ts(r["pushed_ts"]) for r in sent if r.get("pushed_ts")]
            recent_times += [parse_ts(e["created_ts"]) for e in pending_push if e.get("created_ts")]
            recent = [t for t in recent_times if now - t < timedelta(hours=PUSH_INTERVAL_HOURS)]
            if recent:
                when = max(recent) + timedelta(hours=PUSH_INTERVAL_HOURS)
                raise PacketError(
                    "push_limit_24h",
                    f"a notification went out less than {PUSH_INTERVAL_HOURS} hours ago; the next is allowed after "
                    f"{utc_iso(when)} (--force overrides)",
                    {"next_allowed_ts": utc_iso(when)},
                )
        link = packet.get("link") or settings.link or str(md_path)
        title, body = _compose(len(items), packet["minutes"], str(link), reminder=False)
        if settings.outbox and not settings.notify_cmd:
            from trialerror.packet import outbox as outbox_mod

            entry = outbox_mod.queue_notification(
                settings, kind="push", packet_id=packet["packet_id"], title=title, body=body,
                trigger=packet.get("trigger"), now=now,
            )
            return {
                "packet_id": packet["packet_id"], "title": title, "body": body, "queued": True,
                "outbox_id": entry["id"], "forced": bool(force),
            }
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
    of its own items are still open. Only a DELIVERED push starts this clock
    (``sent.jsonl``, unchanged by the outbox); a still-queued push has no
    receipt yet and so is not "pushed" for this purpose."""
    now = now or datetime.now(timezone.utc)
    if settings.outbox:
        from trialerror.packet import outbox as outbox_mod

        outbox_mod.reconcile_receipts(settings, now=now)
    with locked(settings):
        sent = read_jsonl(settings.sent)
        pushes = [r for r in sent if not r.get("reminder") and r.get("pushed_ts")]
        if not pushes:
            return {"reminded": False, "reason": "no packet has been pushed yet"}
        last = max(pushes, key=lambda r: parse_ts(r["pushed_ts"]))
        if any(r.get("reminder") and r.get("packet_id") == last["packet_id"] for r in sent):
            return {"reminded": False, "reason": f"{last['packet_id']} was already reminded once"}
        if any(e.get("packet_id") == last["packet_id"] for e in _pending_outbox(settings, "reminder")):
            return {"reminded": False, "reason": f"a reminder for {last['packet_id']} is already queued"}
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
        title, body = _compose(len(still), minutes, str(link), reminder=True)
        if settings.outbox and not settings.notify_cmd:
            from trialerror.packet import outbox as outbox_mod

            entry = outbox_mod.queue_notification(
                settings, kind="reminder", packet_id=packet["packet_id"], title=title, body=body,
                trigger=packet.get("trigger"), now=now,
            )
            return {
                "reminded": True, "packet_id": packet["packet_id"], "title": title, "body": body,
                "open_items": len(still), "queued": True, "outbox_id": entry["id"],
            }
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
