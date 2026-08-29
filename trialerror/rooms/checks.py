"""``trialerror.rooms``'s doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's ``checks.py`` (design Section 5.2 doctor row: "framework +
license-audit in M0; each module registers its own checks") — dropping
this file is the entire registration step, no shared file touched.

Two checks, both scoped to ``DoctorContext.program_root`` (ops.db is
per-program), both reading the ``event`` table rather than a ``room``/
``room_turn`` timestamp column — neither exists (``trialerror/rooms/api.py``'s
own module TRIALERROR-DEV-NOTE item 2), so the companion events
``trialerror.rooms.api`` emits alongside every room mutation are the only
timestamped trail available:

- ``rooms_stuck``: an ``open`` room with no ``room_created``/``room_turn``
  event in the last :data:`STUCK_MAX_AGE_HOURS` hours — mirrors
  ``trialerror.jobs.checks.check_heartbeat_age``'s "warn, not fail" precedent
  (staleness is advisory: a room can legitimately sit open during a slow
  human review pass; this just makes that visible, the way the job ledger
  makes a slow worker visible without treating it as broken).
- ``rooms_unregistered_deliverables``: a ``converged`` room with no
  ``room_deliverable_registered`` event — a REAL invariant violation
  (``trialerror.rooms.api.register_room_deliverable`` is the only path that
  emits one, and design/REQUIREMENTS Section 1.8 states plainly "a
  converged room owes its theory-doc artifact"), so this is reported
  ``fail``, matching ``trialerror.artifacts.checks.check_gated_type_without_gate``'s
  precedent for a real, write-API-enforced invariant re-checked offline.
"""

from __future__ import annotations

import json

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now, parse

__all__ = ["check_rooms_stuck", "check_rooms_unregistered_deliverables"]

#: How long an ``open`` room may go with no ``room_created``/``room_turn``
#: event before :func:`check_rooms_stuck` flags it — MN-033's "keep rooms
#: as the cheap filter" framing argues rooms should resolve quickly; this
#: is a generous ceiling on top of that, not a tight SLA (mirrors
#: ``trialerror.jobs.checks._HEARTBEAT_AGE_WARN_S``'s role as an early-warning
#: constant, not a hard failure threshold).
STUCK_MAX_AGE_HOURS = 48.0


def _ops_conn_or_none(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


@register_check("rooms_stuck", category="rooms")
def check_rooms_stuck(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="rooms_stuck",
            category="rooms",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        open_rooms = conn.execute("SELECT room_id, topic FROM room WHERE state = 'open'").fetchall()
        if not open_rooms:
            return CheckResult(name="rooms_stuck", category="rooms", status="pass", message="no open rooms")

        activity_rows = conn.execute(
            "SELECT payload, ts FROM event WHERE type IN ('room_created', 'room_turn')"
        ).fetchall()
        last_activity_by_room: dict[str, str] = {}
        for r in activity_rows:
            try:
                room_id = json.loads(r["payload"]).get("room_id")
            except (TypeError, ValueError):
                continue
            if room_id is None:
                continue
            ts = r["ts"]
            if room_id not in last_activity_by_room or ts > last_activity_by_room[room_id]:
                last_activity_by_room[room_id] = ts

        now_dt = parse(now())
        stuck: list[dict] = []
        for room in open_rooms:
            room_id = room["room_id"]
            last_ts = last_activity_by_room.get(room_id)
            if last_ts is None:
                # A room with no activity event at all (should only happen
                # via a direct write bypassing trialerror.rooms.api.create_room,
                # which always emits 'room_created') -- treat as maximally
                # stale rather than silently skipped.
                stuck.append({"room_id": room_id, "topic": room["topic"], "last_activity_ts": None, "age_hours": None})
                continue
            age_hours = (now_dt - parse(last_ts)).total_seconds() / 3600.0
            if age_hours > STUCK_MAX_AGE_HOURS:
                stuck.append({"room_id": room_id, "topic": room["topic"], "last_activity_ts": last_ts, "age_hours": age_hours})

        status = "warn" if stuck else "pass"
        message = (
            f"{len(stuck)} open room(s) with no activity in the last {STUCK_MAX_AGE_HOURS}h "
            "(or no activity event at all)"
            if stuck
            else "no open room is stuck"
        )
        return CheckResult(name="rooms_stuck", category="rooms", status=status, message=message, details={"rooms": stuck})
    finally:
        conn.close()


@register_check("rooms_unregistered_deliverables", category="rooms")
def check_rooms_unregistered_deliverables(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="rooms_unregistered_deliverables",
            category="rooms",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        converged_rooms = conn.execute("SELECT room_id, topic FROM room WHERE state = 'converged'").fetchall()
        if not converged_rooms:
            return CheckResult(
                name="rooms_unregistered_deliverables", category="rooms", status="pass", message="no converged rooms"
            )

        registered_rows = conn.execute(
            "SELECT payload FROM event WHERE type = 'room_deliverable_registered'"
        ).fetchall()
        registered_room_ids: set[str] = set()
        for r in registered_rows:
            try:
                room_id = json.loads(r["payload"]).get("room_id")
            except (TypeError, ValueError):
                continue
            if room_id is not None:
                registered_room_ids.add(room_id)

        offenders = [
            {"room_id": room["room_id"], "topic": room["topic"]}
            for room in converged_rooms
            if room["room_id"] not in registered_room_ids
        ]
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} converged room(s) with no registered deliverable artifact "
            "(trialerror.rooms.api.register_room_deliverable was never called)"
            if offenders
            else "every converged room has a registered deliverable artifact"
        )
        return CheckResult(
            name="rooms_unregistered_deliverables", category="rooms", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()
