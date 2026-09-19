"""Past-TTL bookings, and the one question the TTL alone cannot answer.

A booking whose ``booking_ttl_s`` has elapsed while it is still
PROVISIONAL/RUNNING has always been reported as "orphaned - likely a
crashed session". It is not: the TTL is a guess made at booking time, and
an elapsed TTL is equally consistent with a launch that is still running
perfectly well under a booking whose TTL was simply too short. Reporting
the first reading as fact taught operators to ignore the check
(operator feedback, disposition D-FB-3).

So this module holds two things, both shared rather than duplicated:

1. **The TTL arithmetic itself** (:func:`past_ttl_rows`) - one SQL
   predicate, read by the ``budget_dangling_launches`` doctor check and by
   the dashboard's budget card. They used to carry a copy each, which is a
   standing invitation for the card and the doctor to disagree about the
   same launch.
2. **The liveness split** (:func:`split_by_liveness`): a past-TTL booking
   whose own session is still OPEN *and* has recorded ``hook_alive`` events
   is evidence of a short TTL, not of a dead session. Everything else has
   no evidence either way, and the message says exactly that instead of
   guessing.

The split needs ops.db (sessions and events are program-scoped) while the
TTL needs platform.db (bookings are cross-program). A doctor run with no
program root therefore cannot split, and degrades to the single
undifferentiated list it has always reported - the same
"absent-config-is-not-an-error" posture ``_program_model_classes`` takes in
:mod:`trialerror.budget.checks`.

**Liveness sources.** Any ``hook_alive`` event counts, whichever hook wrote
it: ``plugin/hooks/session_start.py`` writes one unconditionally at boot,
the spawn gate and post-task hooks write one the first time they fire, and
``trialerror.hooks.stop_check`` writes ``hook_alive{hook=stop_check}`` once
per session (reconciliation follow-up (f)) so that a session which has
spawned nothing at all still shows alive after its first turn.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from trialerror.util.timeutil import now

__all__ = [
    "LIVE_STATES",
    "DEFAULT_COLUMNS",
    "PAST_TTL_WHERE",
    "past_ttl_rows",
    "sessions_with_hook_liveness",
    "sessions_known_here",
    "foreign_session_rows",
    "split_by_liveness",
    "dangling_message",
    "past_ttl_status",
]

#: The booking states a TTL can still expire under.
LIVE_STATES = ("PROVISIONAL", "RUNNING")

#: What the doctor check reports per offender. The dashboard asks for more.
#: ``session_id`` is in here because :func:`split_by_liveness` reads it: a
#: default that could not be split would hand a caller who took it an empty
#: "alive" list and every row in "offenders" -- silently, which is the one
#: outcome this module exists to prevent (fix-accept, V-6).
DEFAULT_COLUMNS = ("launch_id", "account_id", "session_id", "state", "booked_ts", "booking_ttl_s")

#: The one copy of the arithmetic: booked_ts + booking_ttl_s is in the past.
PAST_TTL_WHERE = (
    "state IN ('PROVISIONAL','RUNNING') "
    "AND julianday(?) > julianday(booked_ts) + (booking_ttl_s / 86400.0)"
)


def past_ttl_rows(
    platform_conn: sqlite3.Connection,
    *,
    columns: Sequence[str] = DEFAULT_COLUMNS,
    now_ts: str | None = None,
) -> list[dict[str, Any]]:
    """Every live booking whose booking TTL has elapsed, newest-first-free
    (the caller orders if it cares). ``columns`` are ``launch`` column names
    - callers pass their own list because the doctor reports five fields and
    the dashboard card renders nine, and neither should drag the other's
    payload around."""
    sql = f"SELECT {', '.join(columns)} FROM launch WHERE {PAST_TTL_WHERE}"
    rows = platform_conn.execute(sql, (now_ts or now(),)).fetchall()
    return [dict(r) for r in rows]


def sessions_with_hook_liveness(ops_conn: sqlite3.Connection) -> set[str]:
    """Session ids that are OPEN *and* have at least one ``hook_alive``
    event - the same pair of facts ``trialerror.sessions.checks``'
    ``session_hook_alive`` check reads, asked the other way round. A CLOSED
    session is deliberately not "alive": whatever it did while open, it is
    not running now, so a booking of its that is past TTL really is
    unreconciled work nobody is holding."""
    rows = ops_conn.execute(
        "SELECT DISTINCT s.session_id FROM session s "
        "JOIN event e ON e.session_id = s.session_id AND e.type = 'hook_alive' "
        "WHERE s.status = 'open'"
    ).fetchall()
    return {r["session_id"] for r in rows}


def sessions_known_here(ops_conn: sqlite3.Connection) -> set[str]:
    """Every session id in THIS program's ops.db, open or closed.

    Bookings are cross-program (platform.db) and sessions are per-program
    (ops.db), so a past-TTL row may have been booked by a session this
    ops.db has never heard of. Such a row is not evidence of a dead session;
    it is a launch whose evidence lives in another program's store. The
    caller needs this set to say that out loud, because "no evidence HERE"
    and "no evidence" are different statements and the empty ``alive`` list
    cannot tell them apart (fix-accept, V-5)."""
    rows = ops_conn.execute("SELECT session_id FROM session").fetchall()
    return {r["session_id"] for r in rows}


def foreign_session_rows(
    rows: Iterable[Mapping[str, Any]], known_session_ids: set[str]
) -> list[dict[str, Any]]:
    """The subset of ``rows`` whose booking session is not one this ops.db
    knows -- i.e. the rows whose liveness this program cannot speak to at
    all, however complete its own session table is."""
    return [dict(r) for r in rows if r.get("session_id") not in known_session_ids]


def split_by_liveness(
    rows: Iterable[Mapping[str, Any]], alive_session_ids: set[str] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Split past-TTL bookings into ``(no_evidence_of_life, session_alive)``.

    ``alive_session_ids is None`` means the split could not be evaluated (no
    program root, or no ops.db yet): every row lands in the first list and
    the second is ``None`` - "could not tell", which is a different
    statement from the empty list's "checked, none alive"."""
    rows = [dict(r) for r in rows]
    if alive_session_ids is None:
        return rows, None
    offenders: list[dict[str, Any]] = []
    alive: list[dict[str, Any]] = []
    for row in rows:
        if "session_id" not in row:
            raise ValueError(
                "split_by_liveness needs each row's session_id -- a row without it would be "
                "counted as 'no evidence of life' on the strength of a column the caller simply "
                "did not select (ask past_ttl_rows for DEFAULT_COLUMNS or add session_id to your "
                "own column list)"
            )
        (alive if row["session_id"] in alive_session_ids else offenders).append(row)
    return offenders, alive


def past_ttl_status(rows: Sequence[Mapping[str, Any]]) -> str:
    """The one SEVERITY WORD for a set of past-TTL bookings: ``"warn"`` if
    there are any, ``"pass"`` if there are none.

    FB-1's verify pass, finding V-11: the doctor check reported ``warn`` for a
    fixture whose dashboard card showed ``DANGLING 0`` under a *settled* chip,
    and the two words were computed in two places. The reading recorded then
    stands -- a booking past a TTL whose session is demonstrably alive is a
    TTL that was guessed too short, which is a fact about this program's
    bookings worth surfacing, so it counts toward the warn even though it is
    not an offender -- and the word is now computed HERE, beside the two lists
    and the one sentence, so the card can print the doctor's own severity
    instead of a reader inferring one from a list length. ``DANGLING`` still
    counts ``offenders``: that number is a true reading of the doctor's own
    offender list, not a verdict about the past-TTL question, and the card
    says which is which (see ``static/console_render.js``'s ledger card and
    the guide's own note).

    Deliberately takes the UNSPLIT rows: the severity does not depend on
    whether the liveness split could be evaluated, and a check that dropped to
    ``pass`` on a program with no ops.db would be reporting the absence of
    evidence as the absence of a finding."""
    return "warn" if rows else "pass"


def dangling_message(
    offenders: Sequence[Mapping[str, Any]], alive: Sequence[Mapping[str, Any]] | None
) -> str:
    """The one sentence both surfaces print, so neither can describe the
    same two lists differently."""
    total = len(offenders) + len(alive or ())
    if total == 0:
        return "no launches past their booking TTL"
    if alive is None:
        return (
            f"{total} launch(es) past their booking TTL — either the TTL was too short or the "
            "session died; this check cannot tell which"
        )
    alive_note = (
        f"; {len(alive)} more past TTL under a session that is still open and recording hook "
        "liveness (a TTL that was too short, not a session that died)"
        if alive
        else ""
    )
    if not offenders:
        return (
            f"{len(alive)} launch(es) past their booking TTL, all under a session that is still "
            "open and recording hook liveness (a TTL that was too short, not a session that died)"
        )
    return (
        f"{len(offenders)} launch(es) past their booking TTL with no evidence of life — either "
        "the TTL was too short or the session died; this check cannot tell which" + alive_note
    )
