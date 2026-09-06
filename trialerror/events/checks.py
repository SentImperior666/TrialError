"""M5's doctor checks: ``event_secret_leak`` (defense in depth against a
direct-DB write that bypassed ``trialerror.stores.insert``'s auto-redaction —
the API always redacts, but a check that trusts nothing except the stored
bytes is what a doctor scan is for) and ``feed_author_integrity`` (every
``feed_post.author`` matches the launch-derived or orchestrator-derived
format ``trialerror.events.post_feed`` itself enforces, catching a hand-rolled
``INSERT`` that slipped past the write API). Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like M1's
``trialerror/stores/checks.py`` (design Section 5.2 doctor row: "each module
registers its own checks") — dropping this file is the entire
registration step, no shared file touched.

XID referential integrity for ``event.launch_id`` / ``thread.created_by_
launch`` / ``feed_post.launch_id`` is already covered generically by M1's
``xid_dangling`` check (every ``XID_REGISTRY`` entry, events/feed's columns
included); this module does not duplicate that scan.

FU-14 (imported history): ``feed_author_integrity`` exempts feed posts
older than the origin-project import watermark. The 153 imported posts predate the
author contract entirely -- 73 say plainly ``'orchestrator'`` because no
session id was ever recorded alongside them, 37 say ``'user'``, the rest
carry lens names -- and there is no honest way to resolve which session
covered each one. The exemption is a strict timestamp boundary read from
:mod:`the (excluded) tenant-migration module`, never a relaxation of the rule: a
post one second newer than the watermark is still a failure, a post with no
readable ``ts`` is never exempt, and the exempted count is stated in the
check's own message so a green result never hides them.
"""

from __future__ import annotations

from the (excluded) tenant-migration module import at_or_before, import_ts_from_conn
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.stores.redact import redact_text
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_event_secret_leak", "check_feed_author_integrity"]

_ORCHESTRATOR_PREFIX = "orchestrator:"


@register_check("event_secret_leak", category="events")
def check_event_secret_leak(ctx: DoctorContext) -> CheckResult:
    if ctx.program_root is None:
        return CheckResult(
            name="event_secret_leak",
            category="events",
            status="skip",
            message="program_root not configured; cannot resolve ops.db path",
        )
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return CheckResult(
            name="event_secret_leak",
            category="events",
            status="skip",
            message="ops.db not found (program not yet initialized)",
        )

    conn = connect(path, read_only=True)
    try:
        rows = conn.execute("SELECT event_id, payload FROM event").fetchall()
    finally:
        conn.close()

    offenders: dict[str, int] = {}
    for row in rows:
        _, count = redact_text(row["payload"] or "")
        if count:
            offenders[row["event_id"]] = count

    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} event row(s) contain an unredacted secret-shaped substring "
        "(trialerror.stores.insert redacts at write time -- this indicates a direct DB write)"
        if offenders
        else "no unredacted secrets found in event.payload"
    )
    return CheckResult(
        name="event_secret_leak", category="events", status=status, message=message, details={"offenders": offenders}
    )


@register_check("feed_author_integrity", category="events")
def check_feed_author_integrity(ctx: DoctorContext) -> CheckResult:
    if ctx.program_root is None:
        return CheckResult(
            name="feed_author_integrity",
            category="events",
            status="skip",
            message="program_root not configured; cannot resolve ops.db path",
        )
    ops_path = paths.ops_db_path(ctx.program_root)
    if not ops_path.exists():
        return CheckResult(
            name="feed_author_integrity",
            category="events",
            status="skip",
            message="ops.db not found (program not yet initialized)",
        )

    ops_conn = connect(ops_path, read_only=True)
    try:
        posts = [dict(r) for r in ops_conn.execute("SELECT post_id, author, launch_id, ts FROM feed_post").fetchall()]
        known_sessions = {r["session_id"] for r in ops_conn.execute("SELECT session_id FROM session").fetchall()}
        # FU-14: the boundary, read once. None on a program that was never
        # imported into -- in which case nothing is ever exempt and this
        # check behaves exactly as it always did.
        watermark_ts = import_ts_from_conn(ops_conn)
    finally:
        ops_conn.close()

    needed_launches = {p["launch_id"] for p in posts if p["launch_id"] is not None}
    agent_kind_by_launch: dict[str, str] = {}
    if needed_launches:
        # fix-accept (C-0064): honor ctx.platform_root when supplied,
        # falling back to TRIALERROR_PLATFORM_ROOT/~/.trialerror otherwise -- this
        # used to always re-derive from the env var/default, ignoring ctx.
        plat_path = paths.platform_db_path(root=ctx.platform_root)
        if plat_path.exists():
            plat_conn = connect(plat_path, read_only=True)
            try:
                placeholders = ",".join("?" for _ in needed_launches)
                rows = plat_conn.execute(
                    f"SELECT launch_id, agent_kind FROM launch WHERE launch_id IN ({placeholders})",
                    list(needed_launches),
                ).fetchall()
                agent_kind_by_launch = {r["launch_id"]: r["agent_kind"] for r in rows}
            finally:
                plat_conn.close()

    offenders: dict[str, str] = {}
    grandfathered = 0
    for post in posts:
        author = post["author"]
        launch_id = post["launch_id"]
        problem: str | None = None
        if launch_id is not None:
            agent_kind = agent_kind_by_launch.get(launch_id)
            expected = f"{agent_kind}:{launch_id}" if agent_kind is not None else None
            if expected is None or author != expected:
                problem = f"author={author!r} does not match launch-derived {expected!r}"
        else:
            if not author.startswith(_ORCHESTRATOR_PREFIX):
                problem = f"author={author!r} has no launch_id but does not match 'orchestrator:<session_id>'"
            else:
                sid = author[len(_ORCHESTRATOR_PREFIX) :]
                if sid not in known_sessions:
                    problem = f"author={author!r} references unknown session_id {sid!r}"
        if problem is None:
            continue
        # FU-14: only a row that WOULD have failed is ever exempted, and
        # only if its own ts proves it predates the import. The count below
        # therefore means "failures the watermark suppressed" -- the number
        # an operator actually needs -- not "imported rows skipped".
        if at_or_before(post.get("ts"), watermark_ts):
            grandfathered += 1
            continue
        offenders[post["post_id"]] = problem

    status = "fail" if offenders else "pass"
    exempt_note = (
        f" ({grandfathered} imported row(s) at or before the origin-project import watermark {watermark_ts} "
        f"exempted; they would otherwise fail)"
        if grandfathered
        else ""
    )
    message = (
        f"{len(offenders)} feed_post row(s) have an author string that doesn't match "
        f"trialerror.events.post_feed's derivation contract{exempt_note}"
        if offenders
        else f"every feed_post.author matches the launch- or orchestrator-derived contract{exempt_note}"
    )
    return CheckResult(
        name="feed_author_integrity",
        category="events",
        status=status,
        message=message,
        details={
            "offenders": offenders,
            "imported_grandfathered": grandfathered,
            "import_watermark_ts": watermark_ts,
        },
    )
