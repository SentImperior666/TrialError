"""M2's doctor checks: a stale-lease scan and a heartbeat-age check. Auto-
discovered by ``trialerror.util.doctor.discover_and_register_checks`` exactly
like M1's ``trialerror/stores/checks.py`` -- dropping this file is the entire
registration step, no shared file touched (design Section 5.2 doctor row:
"framework + license-audit in M0; each module registers its own checks").
Design Section 10: "Jobs ledger = the background-worker dashboard ... no
transcript reading required" / "Watchdog-as-a-table: staleness is a
query, not a loop that can die silently (P7)."

``DoctorContext.program_root`` (an M0-owned field, used as-is -- not
extended) supplies jobs.db's location, same convention
``trialerror/stores/checks.py`` established. A jobs.db that doesn't exist yet is
reported ``skip`` (a program that hasn't been initialized is not a doctor
failure), matching that same precedent.
"""

from __future__ import annotations

from trialerror.jobs.ledger import HEARTBEAT_INTERVAL_S
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now, parse

__all__ = ["check_stale_lease", "check_heartbeat_age"]

#: Early-warning threshold for :func:`check_heartbeat_age`: an active job
#: (lease not yet expired) whose heartbeat is already this old is a worker
#: whose heartbeats are slowing down, worth flagging before its lease
#: actually lapses into :func:`check_stale_lease` territory.
_HEARTBEAT_AGE_WARN_S = HEARTBEAT_INTERVAL_S * 3


def _jobs_db_path(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    return paths.jobs_db_path(ctx.program_root)


@register_check("stale_lease", category="jobs")
def check_stale_lease(ctx: DoctorContext) -> CheckResult:
    """Jobs still marked ``claimed``/``running`` whose lease has already
    expired -- the crashed-worker signature ``trialerror jobs tick``
    (``trialerror.jobs.ledger.sweep_expired_leases``) reclaims. Reported
    ``warn`` (never ``fail``, matching ``anchors_dangling``'s precedent in
    ``trialerror/stores/checks.py``): this is the EXPECTED, self-healing state
    between a crash and the next scheduled tick, not a structural
    integrity violation."""
    path = _jobs_db_path(ctx)
    if path is None or not path.exists():
        return CheckResult(
            name="stale_lease",
            category="jobs",
            status="skip",
            message="jobs.db not found (program_root not configured, or program not yet initialized)",
        )
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT job_id, kind, claimed_by, lease_expires_ts FROM job "
            "WHERE state IN ('claimed','running') AND lease_expires_ts IS NOT NULL AND lease_expires_ts < ?",
            (now(),),
        ).fetchall()
    finally:
        conn.close()
    count = len(rows)
    status = "warn" if count else "pass"
    message = (
        f"{count} job(s) with an expired lease awaiting reclaim (run `trialerror jobs tick`)"
        if count
        else "no expired leases"
    )
    return CheckResult(
        name="stale_lease", category="jobs", status=status, message=message, details={"jobs": [dict(r) for r in rows]}
    )


@register_check("heartbeat_age", category="jobs")
def check_heartbeat_age(ctx: DoctorContext) -> CheckResult:
    """Distinct from ``stale_lease``: active jobs (``claimed``/``running``,
    lease NOT yet expired) whose heartbeat is already older than 3x the
    heartbeat interval -- flags a worker whose heartbeats are slowing down
    BEFORE its lease actually lapses, rather than duplicating
    ``stale_lease``'s already-lapsed signal."""
    path = _jobs_db_path(ctx)
    if path is None or not path.exists():
        return CheckResult(
            name="heartbeat_age",
            category="jobs",
            status="skip",
            message="jobs.db not found (program_root not configured, or program not yet initialized)",
        )
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT job_id, kind, claimed_by, heartbeat_ts, lease_expires_ts FROM job "
            "WHERE state IN ('claimed','running') AND heartbeat_ts IS NOT NULL "
            "AND (lease_expires_ts IS NULL OR lease_expires_ts >= ?)",
            (now(),),
        ).fetchall()
    finally:
        conn.close()

    now_dt = parse(now())
    stale: list[dict] = []
    for r in rows:
        age_s = (now_dt - parse(r["heartbeat_ts"])).total_seconds()
        if age_s > _HEARTBEAT_AGE_WARN_S:
            stale.append({**dict(r), "heartbeat_age_s": age_s})

    status = "warn" if stale else "pass"
    message = (
        f"{len(stale)} active job(s) with a heartbeat older than {_HEARTBEAT_AGE_WARN_S}s (lease not yet expired)"
        if stale
        else "no active job has a stale heartbeat"
    )
    return CheckResult(name="heartbeat_age", category="jobs", status=status, message=message, details={"jobs": stale})
