"""Budget-subsystem doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like M0's
``license_audit`` and M1's ``store_schema_version``/``xid_dangling``
(design Section 5.2 doctor row: "framework + license-audit in M0; each
module registers its own checks") - dropping this file is the entire
registration step.

Two checks, both scoped by ``account_id`` (platform.db is cross-program, so
neither needs ``DoctorContext.program_root``):

- ``budget_dangling_launches``: PROVISIONAL/RUNNING bookings whose TTL has
  expired - the platform-wide, TTL-based cousin of the session-scoped
  dangling-launch check M6's ``session close``/Stop hook perform (design
  Section 5.4 Stop row); this one is visible from `trialerror doctor` without an
  open session at all, and catches bookings orphaned by a crashed session
  (never marked ``abandoned``) that M6's own-session check wouldn't see.
- ``budget_pool_overspend``: pools whose current (spent + committed) *
  billed_multiplier has crossed ``hard_pct`` of ``cap_tokens``.

Both report ``warn`` (design's "visible-not-refused" pattern, Section 5.4
mid-flight-staleness note) - a doctor check flags, it does not itself
refuse anything; refusal is the hook's job.
"""

from __future__ import annotations

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now

__all__ = ["check_budget_dangling_launches", "check_budget_pool_overspend"]


def _platform_db_path(ctx: DoctorContext) -> "object":
    # fix-accept (C-0064): honor an explicit ctx.platform_root (the
    # --platform-root CLI flag / an acceptance journey's own param) before
    # falling back to TRIALERROR_PLATFORM_ROOT/~/.trialerror -- this used to call
    # paths.platform_db_path() with no root at all, ignoring ctx entirely.
    return paths.platform_db_path(root=ctx.platform_root)


@register_check("budget_dangling_launches", category="budget")
def check_budget_dangling_launches(ctx: DoctorContext) -> CheckResult:
    path = _platform_db_path(ctx)
    if not path.exists():
        return CheckResult(
            name="budget_dangling_launches",
            category="budget",
            status="skip",
            message="platform.db not found (no account has booked anything yet)",
        )
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT launch_id, account_id, state, booked_ts, booking_ttl_s FROM launch "
            "WHERE state IN ('PROVISIONAL','RUNNING') "
            "AND julianday(?) > julianday(booked_ts) + (booking_ttl_s / 86400.0)",
            (now(),),
        ).fetchall()
    finally:
        conn.close()

    offenders = [dict(r) for r in rows]
    status = "warn" if offenders else "pass"
    message = (
        f"{len(offenders)} launch(es) PROVISIONAL/RUNNING past their booking TTL "
        "(orphaned booking - likely a crashed session)"
        if offenders
        else "no TTL-expired PROVISIONAL/RUNNING launches"
    )
    return CheckResult(
        name="budget_dangling_launches",
        category="budget",
        status=status,
        message=message,
        details={"offenders": offenders},
    )


@register_check("budget_pool_overspend", category="budget")
def check_budget_pool_overspend(ctx: DoctorContext) -> CheckResult:
    path = _platform_db_path(ctx)
    if not path.exists():
        return CheckResult(
            name="budget_pool_overspend",
            category="budget",
            status="skip",
            message="platform.db not found (no pools configured yet)",
        )
    conn = connect(path, read_only=True)
    try:
        pools = conn.execute("SELECT * FROM budget_pool").fetchall()
        offenders = []
        for pool in pools:
            pool = dict(pool)
            committed = conn.execute(
                "SELECT COALESCE(SUM(est_tokens), 0) FROM launch "
                "WHERE account_id = ? AND model_class = ? AND state IN ('PROVISIONAL','RUNNING')",
                (pool["account_id"], pool["model_class"]),
            ).fetchone()[0]
            projected = (float(pool["spent_visible_tokens"] or 0) + committed) * float(pool["billed_multiplier"])
            hard_cap = float(pool["cap_tokens"]) * float(pool["hard_pct"]) / 100.0
            if projected > hard_cap:
                offenders.append(
                    {
                        "pool_id": pool["pool_id"],
                        "account_id": pool["account_id"],
                        "model_class": pool["model_class"],
                        "projected_billed_tokens": projected,
                        "hard_cap": hard_cap,
                    }
                )
    finally:
        conn.close()

    status = "warn" if offenders else "pass"
    message = (
        f"{len(offenders)} pool(s) over their hard cap (projected spend > hard_pct of cap_tokens)"
        if offenders
        else "no pool over its hard cap"
    )
    return CheckResult(
        name="budget_pool_overspend",
        category="budget",
        status=status,
        message=message,
        details={"offenders": offenders},
    )
