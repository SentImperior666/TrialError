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
"""

from __future__ import annotations

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
        posts = [dict(r) for r in ops_conn.execute("SELECT post_id, author, launch_id FROM feed_post").fetchall()]
        known_sessions = {r["session_id"] for r in ops_conn.execute("SELECT session_id FROM session").fetchall()}
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
    for post in posts:
        author = post["author"]
        launch_id = post["launch_id"]
        if launch_id is not None:
            agent_kind = agent_kind_by_launch.get(launch_id)
            expected = f"{agent_kind}:{launch_id}" if agent_kind is not None else None
            if expected is None or author != expected:
                offenders[post["post_id"]] = f"author={author!r} does not match launch-derived {expected!r}"
        else:
            if not author.startswith(_ORCHESTRATOR_PREFIX):
                offenders[post["post_id"]] = (
                    f"author={author!r} has no launch_id but does not match 'orchestrator:<session_id>'"
                )
            else:
                sid = author[len(_ORCHESTRATOR_PREFIX) :]
                if sid not in known_sessions:
                    offenders[post["post_id"]] = f"author={author!r} references unknown session_id {sid!r}"

    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} feed_post row(s) have an author string that doesn't match "
        "trialerror.events.post_feed's derivation contract"
        if offenders
        else "every feed_post.author matches the launch- or orchestrator-derived contract"
    )
    return CheckResult(
        name="feed_author_integrity",
        category="events",
        status=status,
        message=message,
        details={"offenders": offenders},
    )
