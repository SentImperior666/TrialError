"""M6's doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every
other subsystem's ``checks.py`` (M1's ``store_schema_version``, M2's
``stale_lease``, M3's ``budget_dangling_launches``, M4's
``law_digest_lockstep``) — dropping this file is the entire registration
step, no shared file touched (design Section 5.2 doctor row: "framework +
license-audit in M0; each module registers its own checks").

Both checks are ``warn``-or-``fail`` visibility surfaces over invariants
``trialerror.sessions.lifecycle`` otherwise only enforces AT boot/close/Stop
time — the design's "visible-not-refused" pattern (Section 5.4 mid-flight-
staleness note) applied to the session subsystem: a doctor run between
sessions can catch a problem before the next boot/close attempt does.
"""

from __future__ import annotations

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now

__all__ = ["check_session_multiple_open", "check_session_hook_alive", "check_spawns_vs_bookings"]

#: "recent" window for :func:`check_spawns_vs_bookings` -- wide enough to
#: catch a just-closed session's gap on the next `trialerror doctor` run without
#: scanning a program's entire history every time (matching
#: ``check_budget_dangling_launches``'s own TTL-based, not full-history,
#: scope).
_RECENT_SESSION_WINDOW_DAYS = 7


def _ops_conn_or_none(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


def _platform_conn_or_none(ctx: DoctorContext):
    path = paths.platform_db_path(root=ctx.platform_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


@register_check("session_multiple_open", category="sessions")
def check_session_multiple_open(ctx: DoctorContext) -> CheckResult:
    """Mirrors ``trialerror.budget.gate.resolve_open_session``'s own invariant
    (at most one OPEN session per program) as a doctor-visible check, so a
    bug that left two sessions open (e.g. a crashed process whose
    ``abandoned`` transition never ran) is caught by `trialerror doctor`
    without needing to trigger a spawn attempt first."""
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="session_multiple_open",
            category="sessions",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = conn.execute("SELECT session_id FROM session WHERE status = 'open'").fetchall()
    finally:
        conn.close()
    offenders = [r["session_id"] for r in rows]
    status = "fail" if len(offenders) > 1 else "pass"
    message = (
        f"{len(offenders)} sessions simultaneously OPEN (expected at most 1): {offenders}"
        if len(offenders) > 1
        else f"{len(offenders)} session(s) open"
    )
    return CheckResult(
        name="session_multiple_open", category="sessions", status=status, message=message,
        details={"open_session_ids": offenders},
    )


@register_check("session_hook_alive", category="sessions")
def check_session_hook_alive(ctx: DoctorContext) -> CheckResult:
    """Design Section 5.4 SessionStart row: "a session with no hook events
    = hooks were disabled". Warns (never fails — the same "visible, not a
    structural integrity violation" treatment ``trialerror.jobs.checks``' own
    ``stale_lease`` check gives an expected-but-attention-worthy state) for
    every currently OPEN session with zero recorded ``hook_alive`` events —
    catching a hooks-disabled session BEFORE its close attempt hits the
    same wall."""
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="session_hook_alive",
            category="sessions",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        open_rows = conn.execute("SELECT session_id FROM session WHERE status = 'open'").fetchall()
        offenders = []
        for row in open_rows:
            sid = row["session_id"]
            count = conn.execute(
                "SELECT COUNT(*) FROM event WHERE session_id = ? AND type = 'hook_alive'", (sid,)
            ).fetchone()[0]
            if count == 0:
                offenders.append(sid)
    finally:
        conn.close()
    status = "warn" if offenders else "pass"
    message = (
        f"{len(offenders)} open session(s) with zero hook_alive events (hooks may be disabled): {offenders}"
        if offenders
        else "every open session has recorded at least one hook_alive event"
    )
    return CheckResult(
        name="session_hook_alive", category="sessions", status=status, message=message, details={"offenders": offenders}
    )


@register_check("spawns_vs_bookings", category="sessions")
def check_spawns_vs_bookings(ctx: DoctorContext) -> CheckResult:
    """FX-8 (C-0064 lens B EP-1 Bypass C, "the quiet corner"): a session can
    pass every existing hooks/dangling-launch check while still having run
    spawns that never touched the booking ledger at all -- SessionStart
    armed, PreToolUse:Task off, so an ungated spawn consumes no ``launch``
    row and leaves no positive trace there. This check counts, per
    OPEN-or-recent session, ``subagent_return`` events (``event.type ==
    'subagent_return'``, written by ``plugin/hooks/post_task.py``) against
    CONSUMED bookings (``launch.state IN ('RUNNING','RECONCILED')`` under
    that session) and warns on a mismatch; it separately warns on any
    ``subagent_return`` event carrying a null or dangling (no matching
    ``platform.launch`` row) ``launch_id`` -- the audit-trail gap
    ``post_task.py``'s own module docstring already names as possible
    (``extract_launch_id_token`` returns ``None`` when a subagent's prompt
    carries no ``launch_id:`` token at all).

    "Recent" = every OPEN session, plus every session CLOSED within the
    last 7 days (:data:`_RECENT_SESSION_WINDOW_DAYS`)."""
    ops_conn = _ops_conn_or_none(ctx)
    if ops_conn is None:
        return CheckResult(
            name="spawns_vs_bookings",
            category="sessions",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    platform_conn = _platform_conn_or_none(ctx)
    try:
        sessions = ops_conn.execute(
            "SELECT session_id FROM session WHERE status = 'open' "
            "OR (closed_ts IS NOT NULL AND julianday(?) - julianday(closed_ts) <= ?)",
            (now(), _RECENT_SESSION_WINDOW_DAYS),
        ).fetchall()

        mismatched: list[dict] = []
        bad_launch_id_events: list[dict] = []
        for row in sessions:
            sid = row["session_id"]
            return_rows = ops_conn.execute(
                "SELECT event_id, launch_id FROM event WHERE session_id = ? AND type = 'subagent_return'", (sid,)
            ).fetchall()
            for r in return_rows:
                lid = r["launch_id"]
                if lid is None:
                    bad_launch_id_events.append({"session_id": sid, "event_id": r["event_id"], "reason": "null_launch_id"})
                elif platform_conn is not None:
                    exists = platform_conn.execute("SELECT 1 FROM launch WHERE launch_id = ? LIMIT 1", (lid,)).fetchone()
                    if exists is None:
                        bad_launch_id_events.append(
                            {"session_id": sid, "event_id": r["event_id"], "launch_id": lid, "reason": "unknown_launch_id"}
                        )

            consumed_launch_count = 0
            if platform_conn is not None:
                consumed_launch_count = platform_conn.execute(
                    "SELECT COUNT(*) FROM launch WHERE session_id = ? AND state IN ('RUNNING', 'RECONCILED')", (sid,)
                ).fetchone()[0]
            if len(return_rows) != consumed_launch_count:
                mismatched.append(
                    {"session_id": sid, "subagent_return_count": len(return_rows), "consumed_launch_count": consumed_launch_count}
                )
    finally:
        ops_conn.close()
        if platform_conn is not None:
            platform_conn.close()

    status = "warn" if (mismatched or bad_launch_id_events) else "pass"
    message = (
        f"{len(mismatched)} session(s) where subagent_return count != consumed (RUNNING/RECONCILED) "
        f"booking count; {len(bad_launch_id_events)} subagent_return event(s) with a null/unknown launch_id"
        if status == "warn"
        else "subagent_return counts reconcile with consumed bookings for every open/recent session"
    )
    return CheckResult(
        name="spawns_vs_bookings",
        category="sessions",
        status=status,
        message=message,
        details={"mismatched_sessions": mismatched, "bad_launch_id_events": bad_launch_id_events},
    )
