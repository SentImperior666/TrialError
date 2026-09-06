"""Acknowledgements: the operator's answer to a permanent ``webfetch_
unattributed`` FAIL.

The problem this closes, exactly. ``check_webfetch_unattributed`` reads the
research-visible COPY of the fetch trail (``<queue>/audit.jsonl``), and that
file is **append-only** — nothing on this side of the boundary may edit or
truncate it, because a trail a suspect can rewrite is not a trail. So the
one deliberate forgery the acceptance runbook asks the operator to perform
(item H-attrib: hand-write a manifest naming a launch nobody booked, prove
both surfaces go red) leaves a line that is red *forever*. Deleting the
result directories, which is all the runbook's cleanup step can do, changes
nothing the check reads.

A check that cannot go green after a test the runbook itself prescribes
trains the operator to ignore the category — which is the one failure mode a
health check cannot survive. Three ways out, and only one of them is honest:

1. let the check skip audit lines older than some age — the signal decays
   for real offences too, which is exactly backwards;
2. let something delete or rewrite the audit line — the property the whole
   split-container design exists to guarantee, thrown away to tidy a
   dashboard;
3. leave the audit line exactly where it is and record, separately and
   attributably, that a named human looked at this id and says it is
   accounted for.

This module is (3), and the shape follows from it: an acknowledgement is an
**additional** record, never a subtraction. It names the launch that the
acknowledging session was itself booked under (so the acknowledgement is as
attributable as the fetch it excuses), it carries a free-text note the
operator has to write (an acknowledgement with nothing to say is a click,
not a judgment), and the check reports acknowledged offenders in their own
list with the note attached rather than dropping them from the report.

Storage: one ``ops.meta`` row per id, ``key = 'webfetch.ack.<id>'``, value a
JSON ``{launch_id, note, ts, kind}``. Why ``meta`` and not a table of its
own: ops v8 belongs to another lane, and a key/value row is the seam FU-14
already established for exactly this — "one durable fact about this program"
— with the key namespace saying who owns it. Why keyed on the id and not on
a row of some join: the ids are what the audit line carries, and the audit
line is the only thing the check can see.

The stored ``ts`` is not decoration. An acknowledgement is a **boundary, not
a switch** — the property the same key/value seam's other tenant states for
its own grandfathering: a row written one second later is not exempt, so the
exemption can never silently widen. ``check_webfetch_unattributed`` covers
only records whose own timestamp is at or before this one, and a record with
no readable timestamp is never covered. Without that bound an acknowledged
id would be an unbounded, permanent allowlist token for anything that later
reused it — and this feature *publishes* its tokens, since the check's
passing message names the acknowledged ids and ``webfetch acks`` lists them.

Both id kinds are accepted because both appear in the trail: a forged
manifest names a ``fetch_id`` and a ``job_id``, and an operator reading
``te-webfetch.sh audit`` has whichever one the line put in front of them.
The check treats a hit on EITHER as covering the offender — each against its
own recorded ``kind``, so ``--job-id X`` does not quietly cover a *fetch* id
that happens to also be X.

Nothing here is a way to make a fetch look booked. The ``web_fetch`` row's
``launch_id`` is untouched, the audit copy is untouched, the host's own
authoritative trail is untouched and unreachable from this side, and
``webfetch acks`` prints the current acknowledgement for every id with the
name of the launch that made it — plus how many times that id has been
acknowledged, because re-acknowledging UPDATES the single row and a second
signer would otherwise replace the first in that surface without saying so.
The superseded records themselves are in the ``webfetch_ack`` event log,
which nothing here rewrites.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert, update
from trialerror.util.timeutil import now

__all__ = [
    "ACK_KEY_PREFIX",
    "ACK_EVENT_TYPE",
    "ACK_KINDS",
    "AckError",
    "ack_key",
    "id_from_ack_key",
    "acknowledge",
    "ack_revision_counts",
    "list_acks",
    "load_acknowledged",
]

#: ``ops.meta`` key namespace. One row per acknowledged id.
ACK_KEY_PREFIX = "webfetch.ack."

#: The type-keyed event every acknowledgement also appends, so the act shows
#: up in the same timeline as the fetch it is about (the convention every
#: other webfetch write verb follows via ``handlers._event``).
ACK_EVENT_TYPE = "webfetch_ack"

#: What an id can be. ``fetch`` = a ``WF-…`` fetch id, ``job`` = a
#: ``JOB-webfetch-…`` ledger job id. Both appear on an audit line.
ACK_KINDS = ("fetch", "job")

#: An id is an argv token that becomes part of a database key, so it is held
#: to a conservative shape rather than trusted. Every id this system mints
#: (``WF-…``, ``JOB-webfetch-WF-…``) is inside it.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")

#: A note is a judgment, not an essay. Long enough for a runbook item id and
#: a sentence; short enough that nobody pastes a page into it.
_NOTE_MAX = 500


class AckError(Exception):
    """A refusal the CLI turns into an error envelope. Never raised for
    "this id was already acknowledged" — that is a normal, reportable
    outcome, not a failure."""


@dataclass(frozen=True)
class AckResult:
    """What one id's acknowledgement did."""

    target_id: str
    kind: str
    launch_id: str
    note: str
    ts: str
    already: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.target_id,
            "kind": self.kind,
            "launchId": self.launch_id,
            "note": self.note,
            "ts": self.ts,
            "already": self.already,
        }


def ack_key(target_id: str) -> str:
    """The ``ops.meta`` key one id's acknowledgement lives under."""
    return f"{ACK_KEY_PREFIX}{target_id}"


def id_from_ack_key(key: str) -> str | None:
    """The id an ack key names, or ``None`` if the key is not one of ours."""
    if not key.startswith(ACK_KEY_PREFIX):
        return None
    rest = key[len(ACK_KEY_PREFIX) :]
    return rest or None


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _validate_note(note: str | None) -> str:
    text = (note or "").strip()
    if not text:
        raise AckError(
            "--note is required: an acknowledgement is a judgment about why an "
            "unattributed fetch is accounted for, and a judgment nobody wrote "
            "down is indistinguishable from a dashboard being silenced"
        )
    if len(text) > _NOTE_MAX:
        raise AckError(f"--note is longer than {_NOTE_MAX} characters; say it shorter")
    return text


def _validate_launch(store: Store, launch_id: str) -> str:
    """The acknowledgement is itself attributed (design §4 T7 applied to the
    operator's act, not only to the fetch). An unbooked launch here would
    make the record that excuses an unattributed fetch itself unattributed."""
    text = (launch_id or "").strip()
    if not text:
        raise AckError("--launch-id is required: an acknowledgement is an attributed act")
    found = store.platform.execute(
        "SELECT 1 FROM launch WHERE launch_id = ? LIMIT 1", (text,)
    ).fetchone()
    if found is None:
        raise AckError(
            f"launch {text!r} is not booked in platform.launch — book the launch you are "
            "acknowledging under before you acknowledge anything (the record that excuses "
            "an unattributed fetch cannot itself be unattributable)"
        )
    return text


def _validate_ids(fetch_ids: Iterable[str], job_ids: Iterable[str]) -> list[tuple[str, str]]:
    """``[(id, kind), …]`` in the order given, duplicates collapsed."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, values in (("fetch", fetch_ids), ("job", job_ids)):
        for raw in values:
            target = (raw or "").strip()
            if not target:
                continue
            if not _ID_RE.match(target):
                raise AckError(
                    f"{target!r} is not an id shape this accepts (letters, digits and "
                    "'.', '_', ':', '-', up to 120 characters) — pass the fetch id or the "
                    "job id exactly as the audit line prints it"
                )
            if target in seen:
                continue
            seen.add(target)
            pairs.append((target, kind))
    if not pairs:
        raise AckError(
            "name what you are acknowledging: --fetch-id and/or --job-id (an "
            "acknowledgement is per-id on purpose — there is no --all)"
        )
    return pairs


def acknowledge(
    store: Store,
    *,
    fetch_ids: Iterable[str] = (),
    job_ids: Iterable[str] = (),
    launch_id: str,
    note: str,
    ts: str | None = None,
) -> list[AckResult]:
    """Record that a named human has accounted for these ids.

    One ``ops.meta`` row per id and one ``webfetch_ack`` event per id — the
    event so the act is in the timeline, the row so the doctor check can find
    it with the read-only handle it has. Re-acknowledging an id UPDATES its
    note, launch and timestamp and reports ``already=True``: the operator who
    re-runs a runbook line should get "already acknowledged", not a second
    row and not an error.

    Re-acknowledging therefore MOVES the boundary forward, which is the only
    way to cover a record that has arrived since — and it is a deliberate,
    attributed, event-logged act each time rather than something the first
    acknowledgement grants in advance. The superseded record stays in the
    ``webfetch_ack`` log; :func:`list_acks` reports how many there are.

    Raises :class:`AckError` for an unbooked launch, an empty note, or an id
    that is not an id. Writes nothing at all when it raises — every argument
    is validated before the first row is touched.
    """
    checked_note = _validate_note(note)
    checked_launch = _validate_launch(store, launch_id)
    pairs = _validate_ids(fetch_ids, job_ids)

    from trialerror.events.api import append_event

    stamp = ts or now()
    results: list[AckResult] = []
    for target, kind in pairs:
        key = ack_key(target)
        existing = get(store, "meta", pk_column="key", pk_value=key)
        payload = {
            "launch_id": checked_launch,
            "note": checked_note,
            "ts": stamp,
            "kind": kind,
        }
        value = json.dumps(payload, sort_keys=True)
        if existing is None:
            insert(store, "meta", {"key": key, "value": value, "updated_ts": stamp})
        else:
            update(
                store,
                "meta",
                pk_column="key",
                pk_value=key,
                changes={"value": value, "updated_ts": stamp},
            )
        append_event(
            store,
            event_type=ACK_EVENT_TYPE,
            payload={
                "id": target,
                "kind": kind,
                "note": checked_note,
                "reacknowledged": existing is not None,
            },
            launch_id=checked_launch,
            ts=stamp,
        )
        results.append(
            AckResult(
                target_id=target,
                kind=kind,
                launch_id=checked_launch,
                note=checked_note,
                ts=stamp,
                already=existing is not None,
            )
        )
    return results


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _decode(target: str, raw: object) -> dict[str, Any]:
    """One stored value → a record with every field present.

    A value that does not parse is not dropped: an unreadable
    acknowledgement is still an acknowledgement someone wrote, and hiding it
    would turn a corrupt row into a silently un-acknowledged offender. It
    comes back with an empty note and ``kind = "unknown"`` so the surface
    that prints it says so.
    """
    parsed: Any = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    if not isinstance(parsed, Mapping):
        return {"id": target, "kind": "unknown", "launch_id": None, "note": "", "ts": None}
    kind = parsed.get("kind")
    return {
        "id": target,
        "kind": kind if kind in ACK_KINDS else "unknown",
        "launch_id": parsed.get("launch_id"),
        "note": parsed.get("note") if isinstance(parsed.get("note"), str) else "",
        "ts": parsed.get("ts"),
    }


def load_acknowledged(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{id: record}`` from a bare (typically read-only) ops.db connection —
    what a doctor check has.

    Tolerates an ops.db with no ``meta`` table at all (returns ``{}``): a
    program that predates the FU-14 migration has no acknowledgements, which
    is the same answer as a program that has made none.
    """
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            (ACK_KEY_PREFIX + "%",),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row[0] if not isinstance(row, sqlite3.Row) else row["key"]
        raw = row[1] if not isinstance(row, sqlite3.Row) else row["value"]
        target = id_from_ack_key(str(key))
        if target is None:
            continue
        out[target] = _decode(target, raw)
    return out


def ack_revision_counts(store: Store) -> dict[str, int]:
    """``{id: how many times it has been acknowledged}``, from the
    ``webfetch_ack`` event log.

    The ``ops.meta`` row is current-state only: :func:`acknowledge` UPDATEs
    it, so a second signer replaces the first signer's launch, note and
    timestamp there and no read of that row alone can tell you a decision was
    revised. The events are the append-only half — one per act, each carrying
    its own launch — so counting them is what lets ``acks`` say "this one has
    been written twice, the earlier signature is in the event log" instead of
    presenting the newest as though it were the only one.
    """
    counts: dict[str, int] = {}
    try:
        rows = store.ops.execute(
            "SELECT payload FROM event WHERE type = ?", (ACK_EVENT_TYPE,)
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - defensive (no event table at all)
        return {}
    for row in rows:
        raw = row["payload"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if isinstance(payload, Mapping):
            target = payload.get("id")
            if isinstance(target, str) and target:
                counts[target] = counts.get(target, 0) + 1
    return counts


def list_acks(store: Store) -> list[dict[str, Any]]:
    """The CURRENT acknowledgement for each id, oldest key first — what
    ``trialerror webfetch acks`` prints.

    ``revisions`` is how many times that id has been acknowledged in total
    (1 = never revised, 0 = the event log no longer holds it) and
    ``superseded`` is ``revisions - 1``. They are here because the row this
    reads is overwritten on a re-ack: without them the surface whose whole
    justification is "an acknowledgement that could not be listed back would
    be a way to make a finding disappear" would silently show one signer's
    launch and note in place of another's.
    """
    records = load_acknowledged(store.ops)
    revisions = ack_revision_counts(store)
    out: list[dict[str, Any]] = []
    for target in sorted(records):
        record = dict(records[target])
        count = revisions.get(target, 0)
        record["revisions"] = count
        record["superseded"] = max(0, count - 1)
        out.append(record)
    return out
