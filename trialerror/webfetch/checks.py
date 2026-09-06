"""Lane a's doctor checks — the in-container half of design §3.4.

Eight checks, category ``webfetch``, auto-discovered by
``trialerror.util.doctor.discover_and_register_checks``: dropping this file
next to the package IS the registration step, and no shared file is touched
(the convention ``trialerror/jobs/checks.py`` and ``trialerror/offload/
checks.py`` both document).

**Every one of them reads only what the research side can see.** The
authoritative audit trail lives in the fetch process's own ``/audit`` mount,
which this container has no path to at all; what these checks read is the
*copy* the queue carries (``<queue>/audit.jsonl``) plus the ``web_fetch``
rows. That split is deliberate and is the same shape the containment lane's
two mass-deletion flags already use: the host check is the one that counts,
this one is the convenience twin that puts the same fact in front of whoever
is inside. Design §4 T7 says so in as many words — a forged manifest is
*detected*, not prevented, and detection is what this file is.

Severity, and why each one is what it is:

``fail``
    ``webfetch_sidecar_alive`` (nothing is being fetched at all and nobody
    told you) and ``webfetch_unattributed`` (a fetch exists that no booked
    launch asked for — the T7 signal, and the only thing here that means
    "someone may be using this channel").

``warn``
    a backlog older than an hour, refusals from the SSRF/exfil class, an
    orphaned result directory, a queue filling up. Each is a real thing to
    look at and none of them stops the system.

``pass`` with a number in the message
    ``webfetch_thin_backlog`` and ``webfetch_refetch_due`` are informational
    by design (§3.4 marks both "info"): a thin page is a research judgment
    and a 30-day-old fetch is not a fault. Reporting them as ``warn`` would
    train the operator to ignore the category, which is the one failure mode
    a health check cannot recover from.

Skip rule, uniform across all eight (§3.4): a program with ``[webfetch]
enabled = false``, or with no queue directory, is not broken — it is a
program that does not fetch web pages, which is the default. Every check
returns ``skip`` there rather than inventing a verdict about a subsystem
that is switched off.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now_dt, parse as parse_ts
from trialerror.webfetch import SSRF_CLASS_REASONS
from trialerror.webfetch.config import WebFetchConfig, WebFetchConfigError, load_webfetch_config
from trialerror.webfetch.policy import Caps
from trialerror.webfetch.protocol import Queue

__all__ = [
    "SIDECAR_DEAD_S",
    "BACKLOG_WARN_S",
    "REFUSAL_WINDOW_S",
    "REFETCH_DUE_S",
    "QUEUE_DISK_WARN_FRACTION",
    "check_webfetch_sidecar_alive",
    "check_webfetch_backlog",
    "check_webfetch_refused_24h",
    "check_webfetch_unattributed",
    "check_webfetch_orphans",
    "check_webfetch_queue_disk",
    "check_webfetch_thin_backlog",
    "check_webfetch_refetch_due",
]

_CATEGORY = "webfetch"

#: Design §3.4: "heartbeat > 10 min ⇒ FAIL". The loop writes its heartbeat
#: once per poll, and a poll is seconds, so ten minutes is two orders of
#: magnitude of slack — a miss means the process is gone, not busy.
SIDECAR_DEAD_S = 600.0

#: §3.4: "pending > 1 h ⇒ WARN". Below that a pending manifest is simply the
#: jobs window's 300 s cadence plus a polite per-host interval.
BACKLOG_WARN_S = 3600.0

#: The window ``webfetch_refused_24h`` counts over.
REFUSAL_WINDOW_S = 86400.0

#: §3.4: "``webfetch_refetch_due`` (info, > 30 d)".
REFETCH_DUE_S = 30 * 86400.0

#: Fraction of the queue disk cap at which the queue is worth mentioning.
QUEUE_DISK_WARN_FRACTION = 0.8

#: Words under which an extracted page is called thin (the same threshold
#: ``trialerror.webfetch.extract`` applies when it sets ``thin_content``).
_THIN_WORDS = 200

#: A cap on how much of the queue's audit copy one check will read. The file
#: is append-only and nothing rotates it from this side; a check that reads
#: an unbounded file is a check that eventually times out the whole doctor
#: run. Newest lines are the ones that matter, so the tail is what is kept.
_AUDIT_TAIL_LINES = 20000


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ctx:
    """What every check needs, resolved once: the config, the queue, and
    the two database paths. Built by :func:`_resolve`, which returns ``None``
    when the subsystem is off — that ``None`` is the uniform skip."""

    config: WebFetchConfig
    program_root: Path
    queue: Queue
    knowledge_db: Path
    jobs_db: Path


def _skip(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, category=_CATEGORY, status="skip", message=message)


def _resolve(ctx: DoctorContext) -> tuple[_Ctx | None, str]:
    """``(resolved, skip_reason)``. Exactly one of the two is meaningful.

    A ``[webfetch]`` block that is present but *invalid* is deliberately NOT
    a skip: the operator stated an intent the loader refused, and a health
    report that stays silent about that is worse than useless. It comes back
    as a skip reason naming the error so the message says what to fix — the
    check framework has no ``error`` status, and a config that cannot be read
    tells us nothing about the queue either way.
    """
    if ctx.program_root is None:
        return None, "no program root (this check is program-scoped: pass --program-root)"
    program_root = Path(ctx.program_root)

    from trialerror.util.config import CONFIG_FILENAME, load_config

    config_path = program_root / CONFIG_FILENAME
    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            raw = load_config(config_path).raw
        except Exception as exc:  # noqa: BLE001 - any config error is the same story here
            return None, f"trialerror.toml could not be read ({exc})"
    try:
        config = load_webfetch_config(raw)
    except WebFetchConfigError as exc:
        return None, f"[webfetch] is present but refused by the loader: {exc}"

    if not config.enabled:
        return None, "[webfetch] enabled = false (web fetching is off by default, C-0069)"

    queue_root = config.queue_path(program_root)
    if not queue_root.is_dir():
        return None, f"no queue directory at {queue_root} (nothing has been enqueued yet)"

    return (
        _Ctx(
            config=config,
            program_root=program_root,
            queue=Queue(queue_root),
            knowledge_db=paths.knowledge_db_path(program_root, raw),
            jobs_db=paths.jobs_db_path(program_root, raw),
        ),
        "",
    )


def _knowledge(resolved: _Ctx) -> sqlite3.Connection | None:
    """A read-only knowledge connection, or ``None`` when the file or the
    ``web_fetch`` table is not there yet (a program enqueued nothing, or was
    created before the v4 migration ran)."""
    if not resolved.knowledge_db.exists():
        return None
    conn = connect(resolved.knowledge_db, read_only=True)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'web_fetch'"
        ).fetchone()
    except sqlite3.DatabaseError:  # pragma: no cover - defensive
        conn.close()
        return None
    if present is None:
        conn.close()
        return None
    return conn


def _audit_records(resolved: _Ctx) -> Iterator[dict[str, Any]]:
    """The queue's copy of the audit trail, newest ``_AUDIT_TAIL_LINES``.

    Every line is untrusted input in the ordinary sense — it is written by
    the process on the other side of the trust boundary — so a line that does
    not parse is skipped rather than raised on. What the checks do with these
    records is count and compare ids; nothing here is interpreted.
    """
    path = resolved.queue.audit_copy_path
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover - defensive
        return
    for line in lines[-_AUDIT_TAIL_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _age_s(value: object) -> float | None:
    """Seconds since an ISO timestamp, or ``None`` if it will not parse."""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = parse_ts(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:  # pragma: no cover - defensive
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now_dt() - stamp).total_seconds())


def _oldest_pending_age_s(resolved: _Ctx) -> tuple[int, float | None, str | None]:
    """``(pending_count, oldest_age_s, oldest_job_id)``.

    Age comes from the manifest's own ``created_ts`` where it parses, and
    from the file's mtime where it does not — an unreadable manifest still
    ages, and a queue that cannot be aged is a queue that never warns.
    """
    pending = resolved.queue.pending_dir
    if not pending.is_dir():
        return 0, None, None
    oldest_age: float | None = None
    oldest_job: str | None = None
    count = 0
    for path in sorted(pending.glob("*.json")):
        count += 1
        age: float | None = None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            age = _age_s(manifest.get("created_ts"))
        except (OSError, ValueError, AttributeError):
            age = None
        if age is None:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                age = max(0.0, (now_dt() - mtime).total_seconds())
            except OSError:  # pragma: no cover - defensive
                age = None
        if age is not None and (oldest_age is None or age > oldest_age):
            oldest_age = age
            oldest_job = path.stem
    return count, oldest_age, oldest_job


def _hours(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 3600:
        return f"{seconds / 60.0:.0f}m"
    return f"{seconds / 3600.0:.1f}h"


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


@register_check("webfetch_sidecar_alive", category=_CATEGORY)
def check_webfetch_sidecar_alive(ctx: DoctorContext) -> CheckResult:
    """Is anything on the other side of the queue still running?

    ``fail`` past :data:`SIDECAR_DEAD_S`, and ``fail`` too when the heartbeat
    file has never appeared *while work is waiting* — a queue with a pending
    manifest and no fetch process is the state this check exists to name. An
    empty queue that has never been fetched from is a program that has not
    started yet, which is a ``pass`` with the fact stated.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_sidecar_alive", reason)

    age = resolved.queue.sidecar_heartbeat_age_s()
    pending, _oldest, _job = _oldest_pending_age_s(resolved)
    details = {
        "heartbeat_age_s": age,
        "fail_after_s": SIDECAR_DEAD_S,
        "pending": pending,
        "heartbeat_path": str(resolved.queue.heartbeat_path),
    }
    if age is None:
        if pending:
            return CheckResult(
                name="webfetch_sidecar_alive",
                category=_CATEGORY,
                status="fail",
                message=(
                    f"{pending} manifest(s) are waiting and the fetch process has never written a "
                    "heartbeat — it is not running. Start it (the container's own service, or "
                    "`trialerror webfetch sidecar --foreground` on a workstation)"
                ),
                details=details,
            )
        return CheckResult(
            name="webfetch_sidecar_alive",
            category=_CATEGORY,
            status="pass",
            message="no heartbeat yet and nothing waiting — the fetch process has never run "
            "against this queue",
            details=details,
        )
    if age > SIDECAR_DEAD_S:
        return CheckResult(
            name="webfetch_sidecar_alive",
            category=_CATEGORY,
            status="fail",
            message=(
                f"the fetch process last said it was alive {_hours(age)} ago (limit "
                f"{_hours(SIDECAR_DEAD_S)}) — it is down; every queued URL is parked until it "
                "comes back"
            ),
            details=details,
        )
    return CheckResult(
        name="webfetch_sidecar_alive",
        category=_CATEGORY,
        status="pass",
        message=f"the fetch process was alive {_hours(age)} ago",
        details=details,
    )


@register_check("webfetch_backlog", category=_CATEGORY)
def check_webfetch_backlog(ctx: DoctorContext) -> CheckResult:
    """How many URLs are waiting, and for how long. ``warn`` past an hour."""
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_backlog", reason)

    pending, oldest, oldest_job = _oldest_pending_age_s(resolved)
    claimed = sorted(p.stem for p in resolved.queue.claimed_dir.glob("*/*.json")) if (
        resolved.queue.claimed_dir.is_dir()
    ) else []
    details = {
        "pending": pending,
        "claimed": len(claimed),
        "oldest_pending_age_s": oldest,
        "oldest_pending_job_id": oldest_job,
        "warn_after_s": BACKLOG_WARN_S,
    }
    if not pending and not claimed:
        return CheckResult(
            name="webfetch_backlog",
            category=_CATEGORY,
            status="pass",
            message="no URLs waiting to be fetched",
            details=details,
        )
    if oldest is not None and oldest > BACKLOG_WARN_S:
        return CheckResult(
            name="webfetch_backlog",
            category=_CATEGORY,
            status="warn",
            message=(
                f"{pending} URL(s) have waited up to {_hours(oldest)} to be fetched — check that "
                "the fetch process is running and not stuck behind one slow host"
            ),
            details=details,
        )
    return CheckResult(
        name="webfetch_backlog",
        category=_CATEGORY,
        status="pass",
        message=f"{pending} pending, {len(claimed)} in flight (oldest {_hours(oldest)})",
        details=details,
    )


@register_check("webfetch_refused_24h", category=_CATEGORY)
def check_webfetch_refused_24h(ctx: DoctorContext) -> CheckResult:
    """Refusals in the last 24 h, counted by reason.

    Most reasons are ordinary results: a paywall is a paywall. The ones in
    :data:`trialerror.webfetch.SSRF_CLASS_REASONS` are not — each of them
    means something inside this container asked for an address or a URL shape
    it had no business asking for, and one of them is worth reading the audit
    over. Hence the split: any count at all in that class is a ``warn``,
    every other reason is reported and passes.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_refused_24h", reason)

    by_reason: dict[str, int] = {}
    examples: dict[str, dict[str, Any]] = {}
    for record in _audit_records(resolved):
        if record.get("outcome") != "refused":
            continue
        age = _age_s(record.get("ts"))
        if age is None or age > REFUSAL_WINDOW_S:
            continue
        why = str(record.get("reason") or "unknown")
        by_reason[why] = by_reason.get(why, 0) + 1
        examples.setdefault(
            why,
            {
                "job_id": record.get("job_id"),
                "launch_id": record.get("launch_id"),
                "host": record.get("host"),
            },
        )

    ssrf = {why: n for why, n in by_reason.items() if why in SSRF_CLASS_REASONS}
    details = {
        "window_s": REFUSAL_WINDOW_S,
        "by_reason": dict(sorted(by_reason.items())),
        "ssrf_class": dict(sorted(ssrf.items())),
        "examples": examples,
        "audit_copy": str(resolved.queue.audit_copy_path),
        "authoritative_audit": "the fetch process's own audit mount, on the host — this copy is "
        "the in-container twin",
    }
    if ssrf:
        listed = ", ".join(f"{why}×{n}" for why, n in sorted(ssrf.items()))
        return CheckResult(
            name="webfetch_refused_24h",
            category=_CATEGORY,
            status="warn",
            message=(
                f"possible SSRF/exfil attempt — read the audit: {listed} in the last 24h "
                f"(total refusals {sum(by_reason.values())})"
            ),
            details=details,
        )
    total = sum(by_reason.values())
    if not total:
        return CheckResult(
            name="webfetch_refused_24h",
            category=_CATEGORY,
            status="pass",
            message="no refusals in the last 24h",
            details=details,
        )
    listed = ", ".join(f"{why}×{n}" for why, n in sorted(by_reason.items()))
    return CheckResult(
        name="webfetch_refused_24h",
        category=_CATEGORY,
        status="pass",
        message=f"{total} refusal(s) in the last 24h, none of the SSRF/exfil class: {listed}",
        details=details,
    )


@register_check("webfetch_unattributed", category=_CATEGORY)
def check_webfetch_unattributed(ctx: DoctorContext) -> CheckResult:
    """Fetches nobody booked (design §4 T7). ``fail`` on any.

    Two halves, and they answer different questions:

    * a ``web_fetch`` row, or an audit line, whose ``launch_id`` is not in
      ``platform.launch`` — someone wrote a manifest with a launch id that
      was never booked;
    * an audit line whose ``job_id`` is unknown to ``jobs.db`` **and** whose
      ``fetch_id`` has no ``web_fetch`` row — a fetch this side has no record
      of asking for at all.

    The second half deliberately requires BOTH to be missing rather than the
    ``job_id`` alone the design sketch names. ``web_fetch.job_id`` is not a
    registered cross-store id precisely because jobs.db rows are expected to
    be swept one day (``trialerror.webfetch.handlers`` says so); keying the
    verdict on jobs.db alone would turn a routine sweep into a FAIL on a
    healthy program. Requiring the fetch record to be missing too keeps the
    signal — a hand-written manifest has neither — and drops the false
    positive.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_unattributed", reason)

    platform_db = paths.platform_db_path(root=ctx.platform_root)
    if not platform_db.exists():
        return _skip(
            "webfetch_unattributed",
            f"platform.db not found at {platform_db} — nothing to check launch ids against",
        )

    conn_platform = connect(platform_db, read_only=True)
    try:
        booked = {
            str(r["launch_id"])
            for r in conn_platform.execute("SELECT launch_id FROM launch").fetchall()
        }
    except sqlite3.DatabaseError:  # pragma: no cover - defensive
        booked = set()
    finally:
        conn_platform.close()

    known_fetch_ids: set[str] = set()
    row_offenders: list[dict[str, Any]] = []
    conn_knowledge = _knowledge(resolved)
    if conn_knowledge is not None:
        try:
            for row in conn_knowledge.execute(
                "SELECT fetch_id, job_id, launch_id, url_norm FROM web_fetch"
            ).fetchall():
                known_fetch_ids.add(str(row["fetch_id"]))
                if str(row["launch_id"]) not in booked:
                    row_offenders.append(
                        {
                            "where": "web_fetch",
                            "fetch_id": row["fetch_id"],
                            "job_id": row["job_id"],
                            "launch_id": row["launch_id"],
                        }
                    )
        finally:
            conn_knowledge.close()

    known_job_ids: set[str] = set()
    if resolved.jobs_db.exists():
        conn_jobs = connect(resolved.jobs_db, read_only=True)
        try:
            known_job_ids = {
                str(r["job_id"]) for r in conn_jobs.execute("SELECT job_id FROM job").fetchall()
            }
        except sqlite3.DatabaseError:  # pragma: no cover - defensive
            known_job_ids = set()
        finally:
            conn_jobs.close()

    audit_offenders: list[dict[str, Any]] = []
    for record in _audit_records(resolved):
        launch_id = record.get("launch_id")
        job_id = record.get("job_id")
        fetch_id = record.get("fetch_id")
        unbooked_launch = not isinstance(launch_id, str) or launch_id not in booked
        unknown_job = (
            not isinstance(job_id, str) or job_id not in known_job_ids
        ) and (not isinstance(fetch_id, str) or fetch_id not in known_fetch_ids)
        if unbooked_launch or unknown_job:
            audit_offenders.append(
                {
                    "where": "audit.jsonl",
                    "fetch_id": fetch_id,
                    "job_id": job_id,
                    "launch_id": launch_id,
                    "unbooked_launch": unbooked_launch,
                    "unknown_job": unknown_job,
                }
            )

    offenders = row_offenders + audit_offenders
    details = {
        "offenders": offenders[:50],
        "offender_count": len(offenders),
        "booked_launches": len(booked),
        "note": "the host-side check on the fetch process's own audit mount is the "
        "authoritative one; this is its in-container twin (design §3.4)",
    }
    if offenders:
        return CheckResult(
            name="webfetch_unattributed",
            category=_CATEGORY,
            status="fail",
            message=(
                f"{len(offenders)} fetch record(s)/audit line(s) carry a launch or job nobody "
                "booked — read the audit before doing anything else; a fetch with no booked "
                "launch is the one signal that this channel is being used by something other "
                "than the harness"
            ),
            details=details,
        )
    return CheckResult(
        name="webfetch_unattributed",
        category=_CATEGORY,
        status="pass",
        message="every fetch on record names a booked launch",
        details=details,
    )


@register_check("webfetch_orphans", category=_CATEGORY)
def check_webfetch_orphans(ctx: DoctorContext) -> CheckResult:
    """Published results with nothing on this side waiting for them.

    A ``done/<job>/`` directory is swept the moment the fetch handler has
    copied its bytes into the corpus. One that is still there for a job the
    ledger has never heard of is either a crash between publish and settle,
    or bytes this side never asked for. Either way it is disk that nothing
    will ever read, and it belongs in front of a human rather than in a
    silent accumulation.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_orphans", reason)

    known_job_ids: set[str] = set()
    if resolved.jobs_db.exists():
        conn = connect(resolved.jobs_db, read_only=True)
        try:
            known_job_ids = {
                str(r["job_id"]) for r in conn.execute("SELECT job_id FROM job").fetchall()
            }
        except sqlite3.DatabaseError:  # pragma: no cover - defensive
            known_job_ids = set()
        finally:
            conn.close()

    orphans: list[str] = []
    published = 0
    for directory in (resolved.queue.done_dir, resolved.queue.failed_dir):
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            published += 1
            if child.name not in known_job_ids:
                orphans.append(f"{directory.name}/{child.name}")

    details = {
        "orphans": orphans[:50],
        "orphan_count": len(orphans),
        "published_dirs": published,
        "queue_root": str(resolved.queue.root),
    }
    if orphans:
        return CheckResult(
            name="webfetch_orphans",
            category=_CATEGORY,
            status="warn",
            message=(
                f"{len(orphans)} published result director(y/ies) belong to no job in the ledger "
                "— inspect them, then delete them by hand; nothing will collect them"
            ),
            details=details,
        )
    return CheckResult(
        name="webfetch_orphans",
        category=_CATEGORY,
        status="pass",
        message=f"{published} published result director(y/ies), all with a job behind them",
        details=details,
    )


@register_check("webfetch_queue_disk", category=_CATEGORY)
def check_webfetch_queue_disk(ctx: DoctorContext) -> CheckResult:
    """How much disk the shared queue is holding.

    The cap that actually bites is enforced by the fetch process against its
    own policy file, which this container cannot read (ruling L-A2). What
    this check compares against is the shipped default
    (:class:`trialerror.webfetch.policy.Caps`) — right for the deployment,
    and stated as an assumption rather than presented as the live value, so
    an operator who raised the cap on the host reads a warn here as
    information rather than as a contradiction.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_queue_disk", reason)

    used = resolved.queue.disk_usage_bytes()
    cap = Caps().queue_disk_cap
    fraction = (used / cap) if cap else 0.0
    details = {
        "bytes": used,
        "assumed_cap_bytes": cap,
        "fraction_of_cap": round(fraction, 4),
        "warn_at_fraction": QUEUE_DISK_WARN_FRACTION,
        "note": "the enforced cap lives in the host policy file this container cannot read; "
        "the number above is the shipped default",
    }
    gib = used / (1024**3)
    if fraction >= QUEUE_DISK_WARN_FRACTION:
        return CheckResult(
            name="webfetch_queue_disk",
            category=_CATEGORY,
            status="warn",
            message=(
                f"the fetch queue holds {gib:.2f} GiB, {fraction * 100:.0f}% of the default "
                f"{cap / (1024**3):.0f} GiB cap — the fetch process starts refusing with "
                "`disk_cap` at the cap; run the jobs worker so results are ingested and swept"
            ),
            details=details,
        )
    return CheckResult(
        name="webfetch_queue_disk",
        category=_CATEGORY,
        status="pass",
        message=f"the fetch queue holds {gib:.2f} GiB of the default {cap / (1024**3):.0f} GiB cap",
        details=details,
    )


@register_check("webfetch_thin_backlog", category=_CATEGORY)
def check_webfetch_thin_backlog(ctx: DoctorContext) -> CheckResult:
    """How many fetched pages came back thin — informational (§3.4).

    The number matters as a *rate*: design §8 defers headless rendering
    until the thin-content rate over the backlog exceeds roughly 10 %, and
    this is the check that makes that number visible instead of anecdotal.
    It never warns. A thin page is not a fault; it is a page.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_thin_backlog", reason)

    conn = _knowledge(resolved)
    if conn is None:
        return _skip(
            "webfetch_thin_backlog", "knowledge.db has no web_fetch table yet (nothing fetched)"
        )
    try:
        extracted = conn.execute(
            "SELECT count(*) AS n FROM web_fetch WHERE superseded_by IS NULL AND state = 'extracted'"
        ).fetchone()["n"]
        thin = conn.execute(
            "SELECT count(*) AS n FROM web_fetch WHERE superseded_by IS NULL AND thin_content = 1"
        ).fetchone()["n"]
        needs_render = conn.execute(
            "SELECT count(*) AS n FROM web_fetch WHERE superseded_by IS NULL AND reason = 'needs_render'"
        ).fetchone()["n"]
    finally:
        conn.close()

    rate = (thin / extracted) if extracted else 0.0
    details = {
        "extracted": extracted,
        "thin": thin,
        "needs_render": needs_render,
        "thin_rate": round(rate, 4),
        "thin_words_threshold": _THIN_WORDS,
        "deferral_note": "design §8 revisits headless rendering when this rate passes ~10%",
    }
    return CheckResult(
        name="webfetch_thin_backlog",
        category=_CATEGORY,
        status="pass",
        message=(
            f"{thin} of {extracted} extracted page(s) are thin ({rate * 100:.0f}%), "
            f"{needs_render} refused as needs_render — informational"
        ),
        details=details,
    )


@register_check("webfetch_refetch_due", category=_CATEGORY)
def check_webfetch_refetch_due(ctx: DoctorContext) -> CheckResult:
    """Pages last fetched more than 30 days ago — informational (§3.4).

    Re-fetching is never automatic (design §5): this check exists so the
    decision is *offered* rather than forgotten. ``trialerror webfetch
    refresh --all --older-than 30d --launch-id …`` is the whole action, and a
    304 or an unchanged extract costs one request and produces nothing.
    """
    resolved, reason = _resolve(ctx)
    if resolved is None:
        return _skip("webfetch_refetch_due", reason)

    conn = _knowledge(resolved)
    if conn is None:
        return _skip(
            "webfetch_refetch_due", "knowledge.db has no web_fetch table yet (nothing fetched)"
        )
    try:
        rows = conn.execute(
            "SELECT fetch_id, url_norm, fetched_ts FROM web_fetch "
            "WHERE superseded_by IS NULL AND fetched_ts IS NOT NULL AND state = 'extracted' "
            "ORDER BY fetched_ts"
        ).fetchall()
    finally:
        conn.close()

    due: list[dict[str, Any]] = []
    for row in rows:
        age = _age_s(row["fetched_ts"])
        if age is not None and age > REFETCH_DUE_S:
            due.append({"fetch_id": row["fetch_id"], "url": row["url_norm"], "age_s": age})

    details = {
        "due": due[:50],
        "due_count": len(due),
        "live_extracted": len(rows),
        "due_after_s": REFETCH_DUE_S,
    }
    if not due:
        return CheckResult(
            name="webfetch_refetch_due",
            category=_CATEGORY,
            status="pass",
            message=f"none of the {len(rows)} live page(s) is older than 30 days",
            details=details,
        )
    return CheckResult(
        name="webfetch_refetch_due",
        category=_CATEGORY,
        status="pass",
        message=(
            f"{len(due)} of {len(rows)} live page(s) were last fetched over 30 days ago — "
            "`trialerror webfetch refresh --all --older-than 30d --launch-id …` if you want them "
            "re-checked (informational; re-fetching is never automatic)"
        ),
        details=details,
    )
