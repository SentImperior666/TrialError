"""The weekly decision packet: the items, the answers, and the settings.

Once a week, when a research session ends or the weekly limit is nearly spent,
everything that is waiting for the operator's decision is gathered into ONE
packet the operator can read in half an hour. This module holds the parts that
are plain files under the program root (no store table, so no migration):

    packet/pending.jsonl     one item per line; rewritten atomically on change
    packet/answers.jsonl     append-only: the operator's answers
    packet/sent.jsonl        append-only: every push (built in ``build.py``)
    packet/built/            the packets themselves, as PACKET_<ts>.md and .json

An item says, in plain words, what is being decided, why it matters, the
options and what each one leads to, the recommendation, and what happens if
nobody decides. The lint below warns (never refuses, unless ``--strict``) when
the text leans on IDs, paths or long sentences the reader cannot decode.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from trialerror.archive.store import exclusive_lock
from trialerror.util.config import ConfigError, foreign_absolute_kind, load_config
from trialerror.util.ids import new_id

__all__ = [
    "PacketError",
    "PacketSettings",
    "packet_settings",
    "utc_iso",
    "parse_ts",
    "lint_item",
    "make_item",
    "add_item",
    "list_items",
    "answer_item",
    "withdraw_item",
    "read_jsonl",
    "append_jsonl",
    "locked",
    "PRIORITIES",
    "ITEM_KINDS",
    "answered_since",
]

PRIORITIES = ("blocking", "normal", "low")
#: design §4 item 1b: an item may carry ``kind`` -- ``"decision"`` (the
#: default), ``"sample"`` (L6 D's calibration sample; may skip
#: ``recommended``, because a recommendation would lean the operator's
#: check), or ``"info"``.
ITEM_KINDS = ("decision", "sample", "info")
DEFAULT_MAX_MINUTES = 30
DEFAULT_REMIND_AFTER_DAYS = 3
MAX_WHAT = 300
MAX_WHY = 500
MAX_SENTENCE_WORDS = 40


class PacketError(Exception):
    """A refusal with a stable ``code`` the CLI turns into an error envelope."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


# --------------------------------------------------------------------------- time


def utc_iso(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    value = str(text).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PacketError("bad_input", f"not an ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------- settings


@dataclass(frozen=True)
class PacketSettings:
    program_root: Path
    dir: Path
    max_minutes: int = DEFAULT_MAX_MINUTES
    notify_cmd: list[str] | None = None
    link: str | None = None
    remind_after_days: int = DEFAULT_REMIND_AFTER_DAYS
    course_file: Path | None = None
    archive_dirs: list[Path] = field(default_factory=list)
    #: L10 part D: ``[packet] outbox`` (default off) and its folder, default
    #: ``packet/outbox/`` under the program root -- independent of ``dir``,
    #: which a program may relocate on its own.
    outbox: bool = False
    outbox_dir: Path | None = None

    @property
    def pending(self) -> Path:
        return self.dir / "pending.jsonl"

    @property
    def answers(self) -> Path:
        return self.dir / "answers.jsonl"

    @property
    def sent(self) -> Path:
        return self.dir / "sent.jsonl"

    @property
    def built(self) -> Path:
        return self.dir / "built"

    @property
    def outbox_receipts(self) -> Path:
        return self.outbox_dir / "receipts"


def _path_value(program_root: Path, raw: Any, what: str) -> Path:
    text = str(raw)
    foreign = foreign_absolute_kind(text)
    if foreign is not None:
        raise ConfigError(f"{what} = {text!r} is an absolute {foreign} path, but this is a different platform")
    path = Path(text)
    return path if path.is_absolute() else program_root / path


def _positive_int(table: Mapping[str, Any], key: str, default: int) -> int:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"[packet] {key} must be a whole number of at least 1, got {value!r}")
    return value


def _refuse_batch_notifier(first: str) -> None:
    """A ``.cmd``/``.bat`` notifier runs through ``cmd.exe`` on Windows, whose
    quoting does not protect against ``&``, ``|``, ``>`` or ``"`` in the title
    and body (item text written by any lane). Refuse it; the interpreter must
    be named explicitly."""
    resolved = shutil.which(first) or first
    if Path(resolved).suffix.lower() in (".cmd", ".bat"):
        raise ConfigError(
            f"[packet] notify_cmd starts with a batch file ({first!r}); its arguments would be re-parsed by cmd.exe. "
            "Call the interpreter explicitly instead, for example [\"python\", \"notify.py\"] or [\"powershell\", \"-File\", \"notify.ps1\"]."
        )


def packet_settings(program_root: str | Path, raw: Mapping[str, Any] | None = None) -> PacketSettings:
    """The ``[packet]`` table with its defaults applied and its values checked.
    ``raw`` is a parsed ``trialerror.toml`` (read from ``program_root`` when not
    given); a program with no ``[packet]`` table gets every default."""
    root = Path(program_root)
    if raw is None:
        cfg_path = root / "trialerror.toml"
        raw = load_config(cfg_path).raw if cfg_path.is_file() else {}
    table = raw.get("packet", {}) or {}
    if not isinstance(table, Mapping):
        raise ConfigError("[packet] must be a table")
    notify = table.get("notify_cmd")
    if notify is not None and (
        not isinstance(notify, list) or not notify or not all(isinstance(a, str) and a for a in notify)
    ):
        raise ConfigError("[packet] notify_cmd must be a non-empty list of strings (the title and body are appended)")
    if notify is not None:
        _refuse_batch_notifier(notify[0])
    link = table.get("link")
    course = table.get("course_file")
    archive = raw.get("archive", {}) or {}
    dirs = archive.get("dirs", []) if isinstance(archive, Mapping) else []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise ConfigError("[archive] dirs must be a list of folder paths")
    outbox = table.get("outbox", False)
    if not isinstance(outbox, bool):
        raise ConfigError(f"[packet] outbox must be true or false, got {outbox!r}")
    return PacketSettings(
        program_root=root,
        dir=_path_value(root, table.get("dir", "packet"), "[packet] dir"),
        max_minutes=_positive_int(table, "max_minutes", DEFAULT_MAX_MINUTES),
        notify_cmd=list(notify) if notify else None,
        link=str(link) if link else None,
        remind_after_days=_positive_int(table, "remind_after_days", DEFAULT_REMIND_AFTER_DAYS),
        course_file=_path_value(root, course, "[packet] course_file") if course else None,
        archive_dirs=[_path_value(root, d, "[archive] dirs") for d in dirs],
        outbox=outbox,
        outbox_dir=_path_value(root, table.get("outbox_dir", "packet/outbox"), "[packet] outbox_dir"),
    )


# ------------------------------------------------------------------------- files


@contextlib.contextmanager
def locked(settings: PacketSettings, *, wait_s: float = 10.0) -> Iterator[None]:
    """One writer at a time in the packet folder (an ``add`` and a cron ``remind``
    can meet). Waits up to ``wait_s``, then refuses rather than corrupt a file."""
    settings.dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    while True:
        with exclusive_lock(settings.dir / ".lock") as got:
            if got:
                yield
                return
        if time.monotonic() >= deadline:
            raise PacketError("packet_busy", f"another packet command holds {settings.dir}; try again in a moment")
        time.sleep(0.1)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise PacketError("corrupt_file", f"{path} line {number} is not valid JSON: {exc}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# ------------------------------------------------------------------------- lint

#: L10 part C (design §4 item 3): the old pattern (`\b[A-Z]{2,}-\d+\b`)
#: missed ULID-shaped ids (`ROOM-01J...`, 26 Crockford-base32 characters)
#: entirely -- the exact bug behind the operator's own complaint about a
#: bare room id reaching the packet unexplained. Also matches the two
#: legacy, non-ULID numeric styles (`CR-###`, `C-####`).
_ID_RE = re.compile(r"\b[A-Z]{2,6}-[0-9A-HJKMNP-TV-Z]{26}\b|\bCR-\d+\b|\bC-\d{3,}\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{7,}\b")
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/]\S+"  # C:\x or C:/x
    r"|(?<![\w])(?:\.{1,2}/|/)[\w.\-]+(?:/[\w.\-]+)*"  # /a/b, ./a
    r"|\b[\w.\-]+(?:/[\w.\-]+)+\.\w{1,6}\b"  # a/b.py
)


def _tokens(text: str) -> list[str]:
    found: list[str] = []
    for regex in (_ID_RE, _HEX_RE, _PATH_RE):
        found.extend(m.group(0) for m in regex.finditer(text))
    return found


def lint_item(item: Mapping[str, Any]) -> list[str]:
    """Plain-words warnings for an item: IDs, hex runs or paths in ``what``/``why``
    that no ref label explains, sentences over 40 words, empty consequences."""
    warnings: list[str] = []
    refs = [r for r in item.get("refs") or [] if isinstance(r, Mapping)]
    labels = [str(r.get("label", "")).lower() for r in refs]
    for field_name in ("what", "why"):
        text = str(item.get(field_name, ""))
        for token in dict.fromkeys(_tokens(text)):
            explained = any(token.lower() in label for label in labels) or any(
                str(r.get("ref", "")) == token and str(r.get("label", "")).strip() for r in refs
            )
            if not explained:
                warnings.append(
                    f"'{token}' in {field_name} is an ID or path the reader cannot decode; "
                    "say what it is in words, or add a ref whose label explains it"
                )
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            words = len(sentence.split())
            if words > MAX_SENTENCE_WORDS:
                warnings.append(f"a sentence in {field_name} has {words} words (over {MAX_SENTENCE_WORDS}); split it")
    for option in item.get("options") or []:
        if isinstance(option, Mapping) and not str(option.get("consequence", "")).strip():
            warnings.append(f"option '{option.get('key')}' has an empty consequence; say what choosing it leads to")
    return warnings


# ------------------------------------------------------------------------- items


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def make_item(raw: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Validate ``raw`` and return a new item (fresh id, timestamp and status).
    Raises :class:`PacketError` ``bad_input`` naming every problem found."""
    problems: list[str] = []
    what, why = _clean_text(raw.get("what")), _clean_text(raw.get("why"))
    if not what:
        problems.append("what is required (what is being decided)")
    elif len(what) > MAX_WHAT:
        problems.append(f"what is {len(what)} characters; the limit is {MAX_WHAT}")
    if not why:
        problems.append("why is required (why it matters, what it unblocks)")
    elif len(why) > MAX_WHY:
        problems.append(f"why is {len(why)} characters; the limit is {MAX_WHY}")

    options: list[dict[str, str]] = []
    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or len(raw_options) < 2:
        problems.append("at least two options are required, each with a key, a label and a consequence")
    else:
        for position, option in enumerate(raw_options, start=1):
            if not isinstance(option, Mapping):
                problems.append(f"option {position} must be an object with key, label and consequence")
                continue
            key, label = _clean_text(option.get("key")), _clean_text(option.get("label"))
            if not key or not label:
                problems.append(f"option {position} needs a key and a label")
            if "consequence" not in option or not isinstance(option.get("consequence"), str):
                problems.append(f"option {key or position} needs a consequence (what choosing it leads to)")
            options.append({"key": key, "label": label, "consequence": _clean_text(option.get("consequence"))})
        keys = [o["key"] for o in options if o["key"]]
        if len(set(keys)) != len(keys):
            problems.append("option keys must be different from each other")
    kind = _clean_text(raw.get("kind")) or "decision"
    if kind not in ITEM_KINDS:
        problems.append(f"kind must be one of {list(ITEM_KINDS)}, got {kind!r}")
    recommended = _clean_text(raw.get("recommended"))
    # design §4 item 1b: "validation accepts recommended: null only for
    # sample" -- L6 D's calibration sample, "because a recommendation would
    # lean the operator's check." Every other kind still requires one.
    if not recommended and kind != "sample":
        problems.append("recommended is required (the key of the option you recommend)")
    elif options and recommended and recommended not in [o["key"] for o in options]:
        problems.append(f"recommended '{recommended}' is not one of the option keys {[o['key'] for o in options]}")
    if not _clean_text(raw.get("if_undecided")):
        problems.append("if_undecided is required (what happens by default if nobody decides)")
    needed_by = _clean_text(raw.get("needed_by"))
    if not needed_by:
        problems.append("needed_by is required ('next-session' or a date YYYY-MM-DD)")
    elif needed_by != "next-session":
        try:
            date.fromisoformat(needed_by)
        except ValueError:
            problems.append(f"needed_by '{needed_by}' must be 'next-session' or a date YYYY-MM-DD")
    priority = _clean_text(raw.get("priority")) or "normal"
    if priority not in PRIORITIES:
        problems.append(f"priority must be one of {list(PRIORITIES)}")
    est = raw.get("est_minutes", 3)
    if isinstance(est, bool) or not isinstance(est, (int, float)) or est <= 0:
        problems.append("est_minutes must be a positive number")
    refs: list[dict[str, str]] = []
    for position, ref in enumerate(raw.get("refs") or [], start=1):
        if not isinstance(ref, Mapping) or not _clean_text(ref.get("label")) or not _clean_text(ref.get("ref")):
            problems.append(f"ref {position} needs a label (in words) and a ref (a path, id or link)")
            continue
        refs.append({"label": _clean_text(ref["label"]), "ref": _clean_text(ref["ref"])})
    if problems:
        raise PacketError("bad_input", "; ".join(problems), {"problems": problems})
    return {
        "id": new_id("PKT"),
        "kind": kind,
        "created_ts": utc_iso(now),
        "asked_by": _clean_text(raw.get("asked_by")) or "unspecified",
        "what": what,
        "why": why,
        "options": options,
        "recommended": recommended or None,
        "if_undecided": _clean_text(raw.get("if_undecided")),
        "needed_by": needed_by,
        "est_minutes": est,
        "priority": priority,
        "refs": refs,
        "status": "open",
    }


def add_item(
    settings: PacketSettings, raw: Mapping[str, Any], *, strict: bool = False, now: datetime | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Validate and store one item. Returns ``(item, lint warnings)``; with
    ``strict`` a warning is a refusal (``lint_refused``) and nothing is stored."""
    item = make_item(raw, now=now)
    warnings = lint_item(item)
    if strict and warnings:
        raise PacketError(
            "lint_refused", "the item is not in plain words: " + " | ".join(warnings), {"warnings": warnings}
        )
    with locked(settings):
        rows = read_jsonl(settings.pending)
        rows.append(item)
        _write_jsonl_atomic(settings.pending, rows)
    return item, warnings


def answered_since(program_root: str | Path, ts: str) -> list[dict[str, Any]]:
    """Every item answered at or after ``ts``, with its choice, note and
    ``asked_by`` (design §4 item 5) -- a pure function: it opens its own
    settings from ``program_root`` and only reads. L11's SessionStart digest
    calls this directly; ``packet list --answered-since`` (below) uses it
    too."""
    settings = packet_settings(program_root)
    rows = read_jsonl(settings.pending)
    by_id = {r.get("id"): r for r in rows}
    cutoff = parse_ts(ts)
    out: list[dict[str, Any]] = []
    for ans in read_jsonl(settings.answers):
        if parse_ts(ans["decided_ts"]) < cutoff:
            continue
        item = by_id.get(ans.get("item_id"), {})
        label = next((o["label"] for o in item.get("options", []) if o.get("key") == ans.get("choice")), None)
        out.append({**ans, "what": item.get("what"), "asked_by": item.get("asked_by"), "choice_label": label})
    return out


def list_items(
    settings: PacketSettings, *, open_only: bool = True, since: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Open items, and/or the answers recorded at or after ``since``
    (:func:`answered_since`, each joined to its item's question and the
    chosen option's label). With ``[packet] outbox`` on, also the outbox's
    own status in words (design §5 D1) when there is something to say --
    a queued notification stuck past its 15-minute deadline, or the last
    one's failed receipt."""
    rows = read_jsonl(settings.pending)
    result: dict[str, Any] = {}
    if open_only:
        result["open"] = [r for r in rows if r.get("status") == "open"]
    if since:
        result["answered"] = answered_since(settings.program_root, since)
    if settings.outbox:
        from trialerror.packet.outbox import notification_status

        note = notification_status(settings, now=now)
        if note:
            result["notification"] = note
    return result


def _find(rows: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for row in rows:
        if row.get("id") == item_id:
            return row
    raise PacketError(
        "not_found",
        f"no packet item {item_id}; items pulled from the determinations queue are answered where they live",
    )


def answer_item(
    settings: PacketSettings,
    item_id: str,
    choice: str,
    *,
    note: str = "",
    by: str = "operator",
    now: datetime | None = None,
) -> dict[str, Any]:
    with locked(settings):
        rows = read_jsonl(settings.pending)
        item = _find(rows, item_id)
        if item.get("status") != "open":
            raise PacketError("not_open", f"{item_id} is already {item.get('status')}")
        keys = [o["key"] for o in item.get("options", [])]
        if choice not in keys:
            raise PacketError("bad_choice", f"'{choice}' is not one of this item's options {keys}", {"options": keys})
        answer = {
            "item_id": item_id,
            "choice": choice,
            "note": note,
            "decided_by": by,
            "decided_ts": utc_iso(now),
        }
        append_jsonl(settings.answers, answer)
        item["status"] = "answered"
        _write_jsonl_atomic(settings.pending, rows)
    return answer


def withdraw_item(settings: PacketSettings, item_id: str, reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    if not reason.strip():
        raise PacketError("bad_input", "a withdrawal needs a reason, in words")
    with locked(settings):
        rows = read_jsonl(settings.pending)
        item = _find(rows, item_id)
        if item.get("status") != "open":
            raise PacketError("not_open", f"{item_id} is already {item.get('status')}")
        item["status"] = "withdrawn"
        item["withdrawn_reason"] = reason.strip()
        item["withdrawn_ts"] = utc_iso(now)
        _write_jsonl_atomic(settings.pending, rows)
    return item
