"""Panel data-builders: one pure function per dashboard panel, each taking
an :class:`~trialerror.dashboard.store_ro.RoStore` and returning a JSON-
serializable ``dict``. These are the SAME functions both
``trialerror.dashboard.serve`` (live, per-request) and ``trialerror.dashboard.export``
(one static snapshot) call -- the live and static paths can never
independently drift on what a panel means, the same discipline the
earlier dashboard's data layer follows for its own single build path.

Scope (v1-honest, per the M1 build brief): an OPS COCKPIT over a TrialError
program's operational state, not a research-content viz. Six panels
shipped at M1:

- :func:`build_session_panel` -- open session, its boot-time bundle stats
  (recorded on the session row at boot), and a LIVE close-readiness
  recomputation (dangling launches / law-pin drift), reusing
  ``trialerror.sessions.lifecycle.session_status`` verbatim.
- :func:`build_budget_panel` -- pools + headroom per account (reusing
  ``trialerror.budget.pools.budget_status``), a booking-state histogram, and
  TTL-expired dangling bookings (the same predicate
  ``trialerror.budget.checks.check_budget_dangling_launches`` uses).
- :func:`build_jobs_panel` -- ledger state histogram, the claimed/running
  set with heartbeat age and lease-expiry status computed against wall
  clock (reusing ``trialerror.jobs.ledger.list_jobs``).
- :func:`build_gates_panel` -- gate state/verdict/reproduction-status
  histograms, plus gates carrying unapplied edits.
- :func:`build_corpus_panel` -- source/document/chunk/anchor counts,
  license-tier split, ingest request-queue states
  (``source.request_state``), and summary coverage.
- :func:`build_doctor_panel` -- reports the LAST on-demand doctor run (see
  ``trialerror.dashboard.doctor_run``); does not itself run doctor (see that
  module's docstring for why running doctor is a distinct, explicit
  action rather than part of the passive panel-refresh loop).

Every panel tolerates a missing DB file (a fresh/partially-initialized
program) by reporting ``{"status": "not_initialized", ...}`` rather than
raising -- the same "visible, not refused" spirit
``trialerror.util.doctor``'s ``skip`` status uses.

build-v2dash-data (the V2 dashboard redesign's backend stage) adds seven
more, over the SAME ``RoStore -> dict`` contract: :func:`build_feed_panel`,
:func:`build_rooms_panel`, :func:`build_determinations_panel`,
:func:`build_dossier_panel`, :func:`build_lexicon_panel`,
:func:`build_course_panel`, :func:`build_since_you_left_panel` -- plus
:func:`run_search`, a dedicated (non-``PANEL_BUILDERS``) wrapper around
``trialerror.retrieve.engine.search``. See ``docs/DASHBOARD_V2_API.md`` for the
full contract (every endpoint, exact payload shape, real captured JSON
examples) -- that document, not this docstring, is the frontend-facing
source of truth for the V2 build.

Lane C (C6) adds the fourteenth: :func:`build_evidence_panel`, one claim
traced to what it stands on. It is the first builder here that takes THREE
alternative selectors (``claim_id``/``anchor_id``/``chunk_id``) and decides
between them itself -- which is why ``serve.PANEL_QUERY_PARAMS`` is a tuple
of ``(query param, keyword)`` pairs rather than a single pair.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from trialerror.artifacts.registry import list_artifacts
from trialerror.budget.pools import budget_status, list_pools
from trialerror.dashboard.store_ro import RoStore
from trialerror.events.api import list_threads, read_inbox
from trialerror.ingest.extract import EXTRACT_REGISTER_KEY, list_pending
from trialerror.ingest.requests import TRANSITIONS as REQUEST_TRANSITIONS
from trialerror.jobs.ledger import list_jobs
from trialerror.memory.merge import list_conflicts as list_memory_conflicts
from trialerror.offload import protocol as offload_protocol
from trialerror.offload.dashboard_items import offload_backlog_items
from trialerror.webfetch.dashboard_items import webfetch_items
from trialerror.retrieve import engine as retrieve_engine
from trialerror.retrieve.errors import InvalidSearchModeError
from trialerror.retrieve.fence import citation_quote, is_fenced_license
from trialerror.retrieve.wrap import untrusted_wrap
from trialerror.rooms.api import CONVERGENCE_BAR_PCT, check_room_converged, get_freeze_reason, list_room_turns
from trialerror.sessions.lifecycle import session_status
from trialerror.util.timeutil import now, now_dt, parse

__all__ = [
    "build_session_panel",
    "build_budget_panel",
    "build_jobs_panel",
    "build_gates_panel",
    "build_corpus_panel",
    "build_doctor_panel",
    "build_feed_panel",
    "build_rooms_panel",
    "build_determinations_panel",
    "build_dossier_panel",
    "build_evidence_panel",
    "build_lexicon_panel",
    "build_course_panel",
    "build_since_you_left_panel",
    "run_search",
    "MAX_SEARCH_K",
    "build_all_panels",
    "isolated_panel",
    "PANEL_BUILDERS",
]


def _decode_json_text(value: Any) -> Any:
    """A JSON-text column, decoded for the wire -- or handed back exactly
    as stored when it is not JSON after all (sweep §3.10 item 1's rule:
    "unparseable text stays a string").

    A panel payload is read by three consumers -- the live page, the static
    export bundle, and the tests -- and every one of them had to know,
    per field, whether a value was already an object or still a string
    needing ``JSON.parse``. That is a convention, and conventions drift.
    Decoding here makes the shape a property of the builder instead. The
    non-JSON fallback matters: a column holding a plain note must survive
    this untouched, not become ``None``."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return value


def _group_count(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    """``SELECT <column>, COUNT(*) FROM <table> GROUP BY <column>``, NULLs
    reported under the JSON-friendly key ``"__null__"`` (a bare Python
    ``None`` key round-trips through ``json.dumps`` as the string
    ``"null"``, which is easy to misread as a real value name -- an
    explicit sentinel string is clearer in the rendered JSON)."""
    rows = conn.execute(f"SELECT {column}, COUNT(*) AS n FROM {table} GROUP BY {column}").fetchall()
    out: dict[str, int] = {}
    for r in rows:
        key = r[column]
        out[key if key is not None else "__null__"] = r["n"]
    return out


def _elapsed_s(ts: str | None, *, reference: Any) -> float | None:
    if not ts:
        return None
    return (reference - parse(ts)).total_seconds()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Same ``sqlite_master`` check ``trialerror.retrieve.vecsearch._table_exists``
    / ``trialerror.ingest.checks`` already use elsewhere. New v4 seam tables
    (``criterion``, ``feed_post_translation``) may not exist yet on a
    program whose ``ops.db`` was last migrated by a write path (``trialerror
    dashboard`` never migrates -- see ``store_ro.py``'s module docstring)
    before this build landed; every builder below checks this FIRST rather
    than letting a bare ``sqlite3.OperationalError: no such table`` escape."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _short_id(value: Any, n: int = 8) -> str:
    """``LNCH-01M1R8J31R24P6781WT1G05PZC`` -> ``LNCH-…5PZC``. A console packs
    twenty of these onto one screen; the last characters are the ones that
    differ, and the full id always travels beside the short one (``title=``
    on the client) so nothing is actually lost."""
    text = "" if value is None else str(value)
    if len(text) <= n:
        return text
    prefix, sep, _rest = text.partition("-")
    if sep and len(prefix) <= 6:
        return f"{prefix}-…{text[-n:]}"
    return f"…{text[-n:]}"


def _truncate(text: str | None, n: int = 140) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _iso(dt: Any) -> str:
    """Format a ``datetime`` the same way ``trialerror.util.timeutil.now()``
    formats the CURRENT time -- needed here only for the ``since_you_left``
    24h fallback, which formats a PAST datetime, something ``timeutil``
    itself has no function for (by design: :func:`~trialerror.util.timeutil.now`
    is deliberately the only wall-clock read in the codebase)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# session panel
# ---------------------------------------------------------------------------
#: Caps for :func:`_session_timeline`. A session that ran a bulk ingest has
#: thousands of job rows; the timeline is a SHAPE, not a log, and 200 bars is
#: already more than the 24 lanes the canvas draws. Truncation reports itself
#: (house rule) via ``timeline.truncated``.
MAX_TIMELINE_SPANS = 200
MAX_TIMELINE_INSTANTS = 200

#: One session's ops.event scan bound. Above this the timeline would be
#: reading a log rather than a session; the cap is reported the same way.
_TIMELINE_EVENT_SCAN_LIMIT = 5000

#: platform.launch.state -> the timeline's own span vocabulary (sweep §3.4).
#: DEFERRED joins ABANDONED/REFUSED: all three are bookings that will never
#: run, and the bar says so rather than implying work in flight.
_LAUNCH_SPAN_STATUS = {
    "PROVISIONAL": "booked",
    "RUNNING": "running",
    "RECONCILED": "complete",
    "ABANDONED": "abandoned",
    "REFUSED": "abandoned",
    "DEFERRED": "abandoned",
}

#: jobs.job.state -> span status. ``retried`` is decided separately (attempts
#: > 1, or a ``reclaimed`` job_event), because it is a fact ABOUT a completed
#: run rather than a state the ledger holds.
_JOB_SPAN_STATUS = {
    "pending": "booked",
    "paused": "booked",
    "claimed": "running",
    "running": "running",
    "complete": "complete",
    "failed": "failed",
    "abandoned": "abandoned",
}


def _within(ts: str | None, start: str | None, end: str | None) -> bool:
    """Is this ISO stamp inside the session window? Stamps are all written by
    ``trialerror.util.timeutil.now()`` in one fixed format, so a lexicographic
    compare IS a chronological compare -- no parse per row."""
    if not ts:
        return False
    if start and ts < start:
        return False
    if end and ts > end:
        return False
    return True


def _job_subject(payload: Any) -> str | None:
    """What a job is ABOUT, in one short string (sweep §3.5's KIND AND SUBJECT
    column). ``kind`` alone says ``custom`` for every handler-dispatched job,
    which is the least informative column on the busiest table in the page.

    Order is most-specific-first: the handler name, then the document or
    source it names, then the file it was handed, and finally -- when the
    payload says nothing recognisable -- how many fields it has, which is a
    reading rather than a blank."""
    if not isinstance(payload, dict):
        return None
    for key in ("handler", "doc_id", "source_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("zip_path", "path", "file"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or value
    if payload:
        return f"{len(payload)} field(s)"
    return None


def _offload_summary(rostore: RoStore, jobs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The GPU offload queue as the JOBS card reads it (sweep §3.10 item 1).

    Two independent sources, deliberately: the queue directory
    (``<program_root>/offload/``, the only place that says "this is waiting
    for a machine that is switched off") and the ledger rows themselves,
    whose ``last_error`` starts with ``awaiting DEV GPU`` from the moment the
    stage parks them. ``awaiting`` is the UNION by job id, so the count is
    right both before the queue directory exists and after it does."""
    program_root = getattr(rostore, "program_root", None)
    counts = {"pending": 0, "claimed": 0, "done": 0, "failed": 0}
    per_job: dict[str, dict[str, Any]] = {}
    available = False

    if program_root is not None:
        root = offload_protocol.offload_root(program_root)
        available = root.is_dir()
        if available:
            for job_id in offload_protocol.list_pending(root):
                counts["pending"] += 1
                entry: dict[str, Any] = {"state": "pending", "worker_id": None, "heartbeat_ts": None}
                try:
                    manifest = offload_protocol.read_json(
                        offload_protocol.pending_dir(root) / f"{job_id}.json"
                    )
                except (OSError, ValueError):
                    manifest = {}
                entry["offload_attempts"] = manifest.get("offload_attempts")
                per_job[job_id] = entry
            for claim in offload_protocol.list_claims(root):
                counts["claimed"] += 1
                per_job[claim["job_id"]] = {
                    "state": "claimed",
                    "worker_id": claim.get("worker_id"),
                    "heartbeat_ts": claim.get("heartbeat_ts"),
                    "offload_attempts": None,
                }
            for job_id in offload_protocol.list_done(root):
                counts["done"] += 1
                per_job.setdefault(
                    job_id, {"state": "done", "worker_id": None, "heartbeat_ts": None, "offload_attempts": None}
                )
            for job_id in offload_protocol.list_failed(root):
                counts["failed"] += 1
                per_job[job_id] = {
                    "state": "failed", "worker_id": None, "heartbeat_ts": None, "offload_attempts": None,
                }

    awaiting_ids = {
        job_id for job_id, entry in per_job.items() if entry["state"] in ("pending", "claimed")
    }
    for job in jobs:
        last_error = job.get("last_error")
        if isinstance(last_error, str) and last_error.startswith(OFFLOAD_PARK_PREFIX):
            awaiting_ids.add(job["job_id"])

    return {
        "available": available,
        "counts": counts,
        "awaiting": len(awaiting_ids),
        "jobs": per_job,
    }


def _session_timeline(rostore: RoStore, session_row: dict[str, Any]) -> dict[str, Any]:
    """THIS SESSION, END TO END (sweep §3.4) -- a pure derivation over rows
    that already exist: the launches booked under this session, the jobs
    created inside its window, the rooms it opened, and the gate transitions
    it recorded. No new table, no new writer.

    The client compresses the idle stretches (``TEConsole.compressIdleGaps``);
    the server's job is only to say what happened and when. Spans with no end
    are still running -- ``end_ts: null`` is the reading, not a missing
    value."""
    session_id = session_row["session_id"]
    start_ts = session_row.get("opened_ts")
    end_ts = session_row.get("closed_ts")
    window = {"start_ts": start_ts, "end_ts": end_ts}

    spans: list[dict[str, Any]] = []
    instants: list[dict[str, Any]] = []

    # ---- launches (platform.launch) --------------------------------------
    for row in rostore.platform.execute(
        "SELECT launch_id, agent_kind, purpose, state, booked_ts, reconciled_ts "
        "FROM launch WHERE session_id = ? ORDER BY booked_ts",
        (session_id,),
    ).fetchall():
        launch = dict(row)
        spans.append(
            {
                "id": launch["launch_id"],
                "kind": "launch",
                "lane": launch.get("agent_kind") or "launch",
                "label": launch.get("purpose") or launch["launch_id"],
                "start_ts": launch.get("booked_ts"),
                "end_ts": launch.get("reconciled_ts"),
                "status": _LAUNCH_SPAN_STATUS.get(launch.get("state") or "", "booked"),
                "ref": {"launch_id": launch["launch_id"]},
            }
        )

    # ---- jobs (jobs.job + job_event) -------------------------------------
    if rostore.is_available("jobs"):
        claimed_at: dict[str, str] = {}
        reclaimed: set[str] = set()
        for ev in rostore.jobs.execute(
            "SELECT job_id, type, MIN(ts) AS ts FROM job_event "
            "WHERE type IN ('claimed', 'reclaimed') GROUP BY job_id, type"
        ).fetchall():
            if ev["type"] == "claimed":
                claimed_at[ev["job_id"]] = ev["ts"]
            else:
                reclaimed.add(ev["job_id"])
        for row in rostore.jobs.execute(
            "SELECT job_id, kind, payload, state, attempts, created_ts, settled_ts "
            "FROM job ORDER BY created_ts"
        ).fetchall():
            job = dict(row)
            if not _within(job.get("created_ts"), start_ts, end_ts):
                continue
            subject = _job_subject(_decode_json_text(job.get("payload")))
            status = _JOB_SPAN_STATUS.get(job.get("state") or "", "booked")
            if status == "complete" and ((job.get("attempts") or 0) > 1 or job["job_id"] in reclaimed):
                status = "retried"
            spans.append(
                {
                    "id": job["job_id"],
                    "kind": "job",
                    "lane": f"{job.get('kind')} · {subject}" if subject else str(job.get("kind")),
                    "label": subject or job["job_id"],
                    "start_ts": claimed_at.get(job["job_id"]) or job.get("created_ts"),
                    "end_ts": job.get("settled_ts"),
                    "status": status,
                    "ref": {"job_id": job["job_id"]},
                }
            )

    # ---- rooms + hook_alive + dp scores (ops.event) ----------------------
    room_open: dict[str, dict[str, Any]] = {}
    events_scanned = 0
    for row in rostore.ops.execute(
        "SELECT ts, type, payload FROM event WHERE session_id = ? ORDER BY ts LIMIT ?",
        (session_id, _TIMELINE_EVENT_SCAN_LIMIT),
    ).fetchall():
        events_scanned += 1
        etype = row["type"]
        payload = _decode_json_text(row["payload"])
        payload = payload if isinstance(payload, dict) else {}
        room_id = payload.get("room_id")
        if etype == "room_created" and room_id:
            span = {
                "id": room_id,
                "kind": "room",
                "lane": f"room {_short_id(room_id)}",
                "label": payload.get("question") or payload.get("title") or room_id,
                "start_ts": row["ts"],
                "end_ts": None,
                "status": "running",
                "ref": {"room_id": room_id},
            }
            room_open[room_id] = span
            spans.append(span)
        elif etype in ("room_frozen", "room_converged") and room_id:
            span = room_open.get(room_id)
            if span is not None:
                span["end_ts"] = row["ts"]
                span["status"] = "frozen" if etype == "room_frozen" else "complete"
        elif etype in ("room_dp_scored", "hook_alive"):
            instants.append(
                {
                    "ts": row["ts"],
                    "kind": etype,
                    "lane": f"room {_short_id(room_id)}" if room_id else "session",
                    "label": etype.replace("_", " "),
                    "ref": {"room_id": room_id} if room_id else {},
                }
            )

    # ---- gate transitions (ops.gate_transition) --------------------------
    for row in rostore.ops.execute(
        "SELECT gate_id, from_state, to_state, ts FROM gate_transition ORDER BY ts"
    ).fetchall():
        if not _within(row["ts"], start_ts, end_ts):
            continue
        instants.append(
            {
                "ts": row["ts"],
                "kind": "gate_transition",
                "lane": f"gate {_short_id(row['gate_id'])}",
                "label": f"{row['from_state']} → {row['to_state']}",
                "ref": {"gate_id": row["gate_id"]},
            }
        )

    spans.sort(key=lambda s: (s.get("start_ts") or "", s.get("id") or ""))
    instants.sort(key=lambda i: (i.get("ts") or "", i.get("kind") or ""))
    spans_dropped = max(0, len(spans) - MAX_TIMELINE_SPANS)
    instants_dropped = max(0, len(instants) - MAX_TIMELINE_INSTANTS)
    if spans_dropped:
        spans = spans[-MAX_TIMELINE_SPANS:]
    if instants_dropped:
        instants = instants[-MAX_TIMELINE_INSTANTS:]

    return {
        "window": window,
        "spans": spans,
        "instants": instants,
        "truncated": {
            "spans_dropped": spans_dropped,
            "instants_dropped": instants_dropped,
            "events_scan_limited": events_scanned >= _TIMELINE_EVENT_SCAN_LIMIT,
        },
    }


def build_session_panel(rostore: RoStore) -> dict[str, Any]:
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}
    if not rostore.is_available("platform"):
        # M-LU-2's live case: close-readiness and the active-launch count
        # come out of platform.db (trialerror.sessions.lifecycle's
        # _launches_for_session reaches straight for store.platform), so a
        # program pointed at a platform root that was never initialized
        # crashed this builder -- and, through the bundle, /dashboard/api/all
        # with it. An "error" reading names the missing file instead. This
        # is NOT "not_initialized": ops.db exists, so the program IS real;
        # what is missing is the platform store the session hangs off.
        return {"status": "error", "message": "platform.db not found"}

    try:
        status = session_status(rostore, session_id=None)
    except RuntimeError as exc:
        # design invariant: at most one OPEN session per program (see
        # trialerror.budget.gate.resolve_open_session's own docstring) -- a
        # dashboard must report a violation, never crash over it.
        return {"status": "invariant_violation", "message": str(exc)}

    open_session = None
    if status.get("open"):
        session_row = status["session"]
        open_session = {
            "session_id": session_row["session_id"],
            "account_id": session_row["account_id"],
            "opened_ts": session_row["opened_ts"],
            "status": session_row["status"],
            "boot_bundle_stats": {
                "boot_pin_version": session_row.get("boot_pin_version"),
                "boot_bundle_sha": session_row.get("boot_bundle_sha"),
                # sweep §3.10 item 2 / M-CON-3: `queue` is a JSON-text column,
                # and the Console prints its LENGTH ("0 queued"). Decoded here
                # so no renderer has to know which side of the wire parses it.
                "queue": _decode_json_text(session_row.get("queue")),
            },
            "close_readiness": status.get("readiness"),
            "unread_inbox_count": status.get("unread_inbox_count"),
            "hook_alive_count": status.get("hook_alive_count"),
            "active_jobs_count": len(status.get("active_jobs") or []),
            "timeline": _session_timeline(rostore, session_row),
        }

    recent_rows = rostore.ops.execute(
        "SELECT session_id, account_id, status, opened_ts, closed_ts FROM session "
        "ORDER BY opened_ts DESC LIMIT 20"
    ).fetchall()
    recent_sessions = [dict(r) for r in recent_rows]

    return {
        "status": "ok",
        "open_session": open_session,
        "recent_sessions": recent_sessions,
    }


# ---------------------------------------------------------------------------
# budget panel
# ---------------------------------------------------------------------------
def _dangling_bookings(rostore: RoStore) -> list[dict[str, Any]]:
    """Same predicate ``trialerror.budget.checks.check_budget_dangling_launches``
    uses: a PROVISIONAL/RUNNING booking whose TTL has elapsed -- an
    orphaned booking, most often left by a session that crashed before
    reconciling/abandoning it."""
    rows = rostore.platform.execute(
        "SELECT launch_id, account_id, session_id, agent_kind, model_class, purpose, state, "
        "booked_ts, booking_ttl_s FROM launch "
        "WHERE state IN ('PROVISIONAL','RUNNING') "
        "AND julianday(?) > julianday(booked_ts) + (booking_ttl_s / 86400.0)",
        (now(),),
    ).fetchall()
    return [dict(r) for r in rows]


def build_budget_panel(rostore: RoStore) -> dict[str, Any]:
    if not rostore.is_available("platform"):
        return {"status": "not_initialized", "message": "platform.db not found"}

    accounts = [dict(r) for r in rostore.platform.execute("SELECT * FROM account ORDER BY created_ts").fetchall()]
    launch_state_counts = _group_count(rostore.platform, "launch", "state")

    per_account: list[dict[str, Any]] = []
    for acc in accounts:
        account_id = acc["account_id"]
        pools = list_pools(rostore, account_id=account_id)
        classes = sorted({p["model_class"] for p in pools})
        budget = budget_status(rostore, account_id=account_id, model_class=None) if classes else {
            "account_id": account_id, "pools": [], "defer_advisories": [],
        }
        account_launch_states = {
            r["state"]: r["n"]
            for r in rostore.platform.execute(
                "SELECT state, COUNT(*) AS n FROM launch WHERE account_id = ? GROUP BY state", (account_id,)
            ).fetchall()
        }
        per_account.append(
            {
                "account": acc,
                "budget_status": budget,
                "launch_state_counts": account_launch_states,
            }
        )

    from trialerror.budget.quota import quota_status

    return {
        "status": "ok",
        "accounts": per_account,
        "launch_state_counts_total": launch_state_counts,
        "dangling_bookings": _dangling_bookings(rostore),
        "plan_quota": quota_status(),
    }


# ---------------------------------------------------------------------------
# jobs panel
# ---------------------------------------------------------------------------
#: The prefix ``trialerror.offload.stage`` puts on the EnvironmentalFailure it
#: raises when a stage parks work for the GPU worker. It is the only marker a
#: ledger row carries that distinguishes "waiting for a machine that is
#: switched off" from any other deferred job, which is why it is matched here
#: rather than inferred from ``failure_class``.
OFFLOAD_PARK_PREFIX = "awaiting DEV GPU"


def build_jobs_panel(rostore: RoStore) -> dict[str, Any]:
    if not rostore.is_available("jobs"):
        return {"status": "not_initialized", "message": "jobs.db not found"}

    state_counts = _group_count(rostore.jobs, "job", "state")
    jobs = list_jobs(rostore, state=None, kind=None, limit=200)

    reference = now_dt()
    live_jobs: list[dict[str, Any]] = []
    stale_leases: list[dict[str, Any]] = []
    for job in jobs:
        heartbeat_age_s = _elapsed_s(job.get("heartbeat_ts"), reference=reference)
        lease_expires_ts = job.get("lease_expires_ts")
        lease_expired = bool(lease_expires_ts) and parse(lease_expires_ts) < reference
        entry = {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "state": job["state"],
            "claimed_by": job.get("claimed_by"),
            "heartbeat_ts": job.get("heartbeat_ts"),
            "heartbeat_age_s": heartbeat_age_s,
            "lease_expires_ts": lease_expires_ts,
            "lease_expired": lease_expired,
            "attempts": job.get("attempts"),
            "max_attempts": job.get("max_attempts"),
        }
        if job["state"] in ("claimed", "running"):
            live_jobs.append(entry)
            if lease_expired:
                stale_leases.append(entry)

    # Sweep §3.10 item 1 / console-3 / M-CON-3. `payload` and `checkpoint`
    # are JSON-text columns, and the old Console printed them into a table
    # cell as the raw string -- the ingest checkpoint (3.5M rows, a dozen
    # keys) was the single least readable thing on the page. Decoded here so
    # the renderer can pick the two numbers it wants; `subject` and
    # `duration_s` are the other two facts the k9s-style table needs and the
    # row does not carry, both derivable and neither worth a client
    # convention. Unparseable text stays a string (`_decode_json_text`).
    recent_jobs: list[dict[str, Any]] = []
    for job in jobs[:50]:
        row = dict(job)
        row["payload"] = _decode_json_text(row.get("payload"))
        row["checkpoint"] = _decode_json_text(row.get("checkpoint"))
        row["subject"] = _job_subject(row["payload"])
        settled_ts = row.get("settled_ts")
        row["duration_s"] = (
            (parse(settled_ts) - parse(row["created_ts"])).total_seconds()
            if settled_ts and row.get("created_ts")
            else None
        )
        recent_jobs.append(row)

    return {
        "status": "ok",
        "state_counts": state_counts,
        "live_jobs": live_jobs,
        "stale_leases": stale_leases,
        "recent_jobs": recent_jobs,
        "offload": _offload_summary(rostore, jobs),
    }


# ---------------------------------------------------------------------------
# gates panel
# ---------------------------------------------------------------------------
def build_gates_panel(rostore: RoStore) -> dict[str, Any]:
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    state_counts = _group_count(rostore.ops, "gate", "state")
    verdict_counts = _group_count(rostore.ops, "gate", "verdict")
    reproduction_counts = _group_count(rostore.ops, "gate", "reproduction_status")

    pending_edit_rows = rostore.ops.execute(
        "SELECT g.gate_id, g.artifact_id, g.state, g.verdict, g.edits, a.title, a.type "
        "FROM gate g JOIN artifact a ON g.artifact_id = a.artifact_id "
        "WHERE g.edits IS NOT NULL AND g.edits != '' AND g.edits != '[]' "
        "AND g.state NOT IN ('union_applied', 'registered')"
    ).fetchall()
    # Sweep §3.10 item 3, pulled forward by spec §5.1: `edits` reaches the
    # client DECODED. It was the last panel field that made every renderer
    # (and every test) call JSON.parse on a value the server had just
    # serialized -- a shape the export bundle and the live route had to
    # agree on by convention rather than by construction. `unverified_count`
    # comes with it: the number the Console's GATES card and the rail badge
    # both want is a property of the array, computed once, here.
    pending_edits = []
    for row in pending_edit_rows:
        entry = dict(row)
        entry["edits"] = _decode_json_text(entry.get("edits"))
        decoded = entry["edits"] if isinstance(entry["edits"], list) else []
        entry["unverified_count"] = sum(1 for e in decoded if isinstance(e, dict) and not e.get("verified"))
        pending_edits.append(entry)

    recent_transitions = [
        dict(r)
        for r in rostore.ops.execute(
            "SELECT * FROM gate_transition ORDER BY id DESC LIMIT 50"
        ).fetchall()
    ]

    artifact_status_counts = _group_count(rostore.ops, "artifact", "status")
    recent_artifacts = list_artifacts(rostore, limit=25)

    return {
        "status": "ok",
        "gate_state_counts": state_counts,
        "gate_verdict_counts": verdict_counts,
        "reproduction_status_counts": reproduction_counts,
        "pending_edits": pending_edits,
        "recent_transitions": recent_transitions,
        "artifact_status_counts": artifact_status_counts,
        "recent_artifacts": recent_artifacts,
    }


# ---------------------------------------------------------------------------
# corpus panel
# ---------------------------------------------------------------------------
def build_corpus_panel(rostore: RoStore) -> dict[str, Any]:
    if not rostore.is_available("knowledge"):
        return {"status": "not_initialized", "message": "knowledge.db not found"}

    conn = rostore.knowledge
    counts = {
        "sources": conn.execute("SELECT COUNT(*) FROM source").fetchone()[0],
        "documents": conn.execute("SELECT COUNT(*) FROM document").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0],
        "quote_anchors": conn.execute("SELECT COUNT(*) FROM quote_anchor").fetchone()[0],
    }

    license_tier_counts = _group_count(conn, "source", "license_tier")
    request_state_counts = _group_count(conn, "source", "request_state")
    document_status_counts = _group_count(conn, "document", "status")

    documents_with_current_summary = conn.execute(
        "SELECT COUNT(DISTINCT subject_id) FROM summary WHERE subject_kind = 'document' AND status = 'current'"
    ).fetchone()[0]
    summary_coverage = {
        "documents_with_current_summary": documents_with_current_summary,
        "total_documents": counts["documents"],
    }

    # same class of signal as trialerror.stores.checks.check_anchors_dangling
    # (doc_sha256 mismatch = anchor stale vs. current document) -- computed
    # directly here (read-only aggregate) rather than re-running the doctor
    # check, since the panel only needs the count, not a full CheckResult.
    stale_anchors = conn.execute(
        "SELECT COUNT(*) FROM quote_anchor qa JOIN document d ON qa.doc_id = d.doc_id "
        "WHERE qa.doc_sha256 != d.sha256"
    ).fetchone()[0]

    # KG extraction stage (trialerror.ingest.extract, design Section 6 stage 8):
    # candidates land as pending `record` rows (register_key=
    # EXTRACT_REGISTER_KEY) until `trialerror extract accept/reject` resolves
    # them into real entity/relation/claim rows -- the backlog-vs-resolved
    # split IS "extract coverage" for an ops cockpit (how much extraction
    # work is queued vs already landed), not a per-document fraction.
    extract_backlog = conn.execute(
        "SELECT COUNT(*) FROM record WHERE register_key = ?", (EXTRACT_REGISTER_KEY,)
    ).fetchone()[0]
    extract_coverage = {
        "pending_records": extract_backlog,
        "confirmed_entities": conn.execute(
            "SELECT COUNT(*) FROM entity WHERE resolution = 'confirmed'"
        ).fetchone()[0],
        "live_relations": conn.execute(
            "SELECT COUNT(*) FROM relation WHERE expired_at IS NULL"
        ).fetchone()[0],
        "live_claims": conn.execute("SELECT COUNT(*) FROM claim WHERE expired_at IS NULL").fetchone()[0],
    }

    return {
        "status": "ok",
        "counts": counts,
        "license_tier_counts": license_tier_counts,
        "request_state_counts": request_state_counts,
        "document_status_counts": document_status_counts,
        "summary_coverage": summary_coverage,
        "extract_coverage": extract_coverage,
        "stale_anchors": stale_anchors,
    }


# ---------------------------------------------------------------------------
# feed panel
# ---------------------------------------------------------------------------
def _derive_post_kind(author: str) -> str:
    """``feed_post.author`` is always ``"<agent_kind>:<launch_id>"`` or
    ``"orchestrator:<session_id>"`` -- server-derived, never caller-supplied
    (``trialerror.events.api._derive_author``). The kind a dashboard wants to
    badge a post with (LENS / CRITIC / ORCHESTRATOR / ...) is exactly the
    text before that first colon; no second source of truth is invented."""
    return author.split(":", 1)[0] if author else "unknown"


def _load_translations_for_posts(
    conn: sqlite3.Connection, post_ids: Sequence[str]
) -> dict[str, dict[str, Any] | None]:
    """The one ``status='current'`` translation row per post in
    ``post_ids``, or ``None`` for a post with none -- either because the
    ``feed_post_translation`` table doesn't exist yet on this program
    (pre-v4 ``ops.db``) or because that post has never been translated.
    Never raises on a missing table (see :func:`_table_exists`).

    Batched (n2, fix pass): one ``sqlite_master`` probe and one query for
    the whole thread, rather than a per-post query (each of which re-probed
    ``sqlite_master`` too) -- a 100-post thread went through roughly 200
    queries to do what 2 can. When more than one ``status='current'`` row
    exists for a post (should not happen, given the supersede invariant,
    but this keeps the same ``ORDER BY created_ts DESC`` tie-break a
    per-post query would use rather than assuming it away), the most
    recent one wins."""
    result: dict[str, dict[str, Any] | None] = {pid: None for pid in post_ids}
    if not post_ids or not _table_exists(conn, "feed_post_translation"):
        return result
    placeholders = ",".join("?" for _ in post_ids)
    rows = conn.execute(
        f"SELECT * FROM feed_post_translation WHERE post_id IN ({placeholders}) AND status = 'current' "
        "ORDER BY post_id, created_ts DESC",
        list(post_ids),
    ).fetchall()
    for r in rows:
        d = dict(r)
        result.setdefault(d["post_id"], None)
        if result[d["post_id"]] is None:  # first row per post_id wins (created_ts DESC)
            result[d["post_id"]] = d
    return result


#: ``job.kind`` + ``payload["handler"]`` the translator enqueues under
#: (``trialerror.cli.feed.run_translate`` / the ``feed-translate`` write
#: action). Kept here rather than imported so this read-only panel module
#: does not pull the whole ``trialerror.feed_translate`` package (and its
#: ``trialerror.eval`` / ``trialerror.verify`` imports) into the dashboard
#: process just to spell one string.
_TRANSLATE_HANDLER = "feed_translate"
_UNSETTLED_JOB_STATES = ("pending", "claimed", "running", "failed")


def _pending_translation_post_ids(rostore: RoStore, post_ids: Sequence[str]) -> set[str]:
    """Which of ``post_ids`` have a translation job in flight -- the
    dashboard's third right-column state ("translation pending", design
    Section 4.4's cache-miss line, made honest: the operator clicked
    Translate, a job exists, no row has landed yet).

    Read from the jobs ledger rather than from a flag on the post, because
    the ledger is already the durable record of "work asked for, not yet
    done" (``trialerror.jobs.ledger``'s own state machine) and a second
    per-post flag would be a thing to keep in sync for no gain. A job
    naming no ``post_ids`` is a thread-wide or program-wide sweep, so
    every candidate post counts as pending under it.
    """
    if not post_ids or not rostore.is_available("jobs"):
        return set()
    wanted = set(post_ids)
    placeholders = ",".join("?" for _ in _UNSETTLED_JOB_STATES)
    rows = rostore.jobs.execute(
        f"SELECT payload FROM job WHERE kind = 'custom' AND state IN ({placeholders})",
        list(_UNSETTLED_JOB_STATES),
    ).fetchall()
    pending: set[str] = set()
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("handler") != _TRANSLATE_HANDLER:
            continue
        targets = payload.get("post_ids")
        if targets:
            pending |= wanted & set(targets)
        else:
            # a sweep: no explicit target list, so every post in view that
            # has no translation yet is covered by it.
            pending |= wanted
    return pending


def _translation_slot(row: dict[str, Any] | None, *, job_pending: bool) -> tuple[dict[str, Any] | None, str]:
    """``(translation, state)`` for one post's right-hand column.

    ``state`` is the UI contract, and it is the whole point of the
    fail-closed gate being visible rather than silent
    (``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 4.4's three
    states, plus the two this build's job path adds):

    - ``"translated"`` -- a gated, passing translation. ``translation`` is
      the row.
    - ``"ungated"`` -- a translation stored without a gate verdict (a
      pre-v5 row, or one hand-inserted straight through
      ``trialerror.stores.insert``). Served, but flagged: the operator is
      told it was never checked rather than being quietly shown
      unverified text as if it had passed.
    - ``"withheld"`` -- the gate FAILED this translation. ``translation``
      is ``None``: the body is never sent to the browser at all, so no
      amount of client-side cleverness can render it. ``gate_reasons``
      travels instead (with its ``style`` block stripped -- FT-4, fix
      pass, see below), so the operator can see WHY.
    - ``"pending"`` -- a translation job is in flight for this post.
    - ``"absent"`` -- nothing asked for yet.

    FT-4 (fix pass): for a ``"withheld"`` row, ``gate_reasons.style`` is
    dropped before it reaches this slot's caller. ``style.violations[].
    detail`` for ``r1_sentence_length`` quotes up to 80 characters of the
    very translation the gate just rejected
    (:mod:`trialerror.feed_translate.style`'s own rule text), which is
    exactly the text this docstring's ``"withheld"`` bullet promises never
    crosses the wire. The UI's own ``gateReasonLines()`` only ever reads
    ``gate_reasons.reasons`` (never ``.style``), so nothing the panel
    renders is lost by dropping it.
    """
    if row is None:
        return None, ("pending" if job_pending else "absent")

    gate_status = row.get("gate_status") or "ungated"
    try:
        gate_reasons = json.loads(row["gate_reasons"]) if row.get("gate_reasons") else None
    except (TypeError, ValueError):
        gate_reasons = None

    common = {
        "translation_id": row["translation_id"],
        "style_mode": row["style_mode"],
        "translator_version": row["translator_version"],
        "faithfulness_score": row["faithfulness_score"],
        "created_ts": row["created_ts"],
        "gate_status": gate_status,
    }
    if gate_status == "fail":
        redacted = (
            {k: v for k, v in gate_reasons.items() if k != "style"}
            if isinstance(gate_reasons, dict)
            else gate_reasons
        )
        return {**common, "gate_reasons": redacted, "body": None}, "withheld"
    return {**common, "gate_reasons": gate_reasons, "body": row["body"]}, (
        "ungated" if gate_status == "ungated" else "translated"
    )


def _thread_shape(posts: list[dict[str, Any]]) -> list[str]:
    """Annotate one thread's posts with their reply structure IN PLACE and
    return ``order_threaded`` -- the DFS pre-order the THREADED reading
    order renders (lane C spec §2.1; ruling L-C4 makes THREADED the
    default the operator sees).

    ``posts`` arrives in ARRIVAL order (``ts ASC, rowid ASC``) and stays
    that way: it is the append-only truth, and the AS IT ARRIVED view
    renders it verbatim. The threading is a second, derived reading of the
    same list -- an id sequence beside it, never a reshuffle of it.

    Added per post:

    ``reply_to``          the parent id (an alias of ``in_reply_to``; the
                          client reads ONE name for the relation, and the
                          raw column keeps its own name for anyone
                          round-tripping the row).
    ``reply_to_missing``  the parent is not in this thread -- cross-thread,
                          deleted, or part of a cycle. Such a post renders
                          at depth 0 with the flag visible; it is NEVER
                          dropped (L-C4: "never dropped" is the ruling's
                          own word).
    ``depth``             0 for a root, +1 per real parent.
    ``root_post_id``      the top of this post's chain (itself, for a root)
                          -- what the ``N REPLIES`` collapse toggles on.
    ``reply_count``       DIRECT children inside this thread. Not the
                          subtree: a card's own head says how many posts
                          answer *it*, and the collapse control (which
                          hides a whole subtree) counts what it hides
                          client-side, where the visible set is known.

    **Cycle guard.** ``feed_post.in_reply_to`` is a plain self-FK: SQLite
    enforces that the parent EXISTS, not that the graph is acyclic, and a
    hand-written row (or a restore that renumbered ids) can close a loop.
    A post whose ancestor chain revisits any id -- including itself -- is
    cut loose and treated as a root with ``reply_to_missing`` set, exactly
    like a post whose parent is absent. Cutting every member of a cycle
    (the walk below revisits an id from any node in or leading into one)
    is what guarantees the parent map is a forest, so the traversal that
    follows terminates by construction rather than by a depth cap.

    The walk is O(n·chain) in the worst case, which for a feed thread is
    O(n²) with a small n -- the alternative (a proper SCC pass) buys
    nothing at these sizes and costs a reader's afternoon.
    """
    by_id = {p["post_id"]: p for p in posts}
    parent_of: dict[str, str | None] = {}

    for p in posts:
        pid = p["post_id"]
        raw_parent = p.get("in_reply_to")
        p["reply_to"] = raw_parent
        parent = raw_parent if (raw_parent is not None and raw_parent in by_id) else None
        missing = raw_parent is not None and parent is None
        if parent is not None:
            seen = {pid}
            cur: str | None = parent
            while cur is not None:
                if cur in seen:
                    parent, missing = None, True
                    break
                seen.add(cur)
                nxt = by_id[cur].get("in_reply_to")
                cur = nxt if (nxt is not None and nxt in by_id) else None
        parent_of[pid] = parent
        p["reply_to_missing"] = missing

    # children lists inherit `posts`' own (ts, rowid) ordering, which is
    # exactly the sibling order the spec asks for -- no second sort.
    children: dict[str | None, list[str]] = {}
    for p in posts:
        children.setdefault(parent_of[p["post_id"]], []).append(p["post_id"])

    depth: dict[str, int] = {}
    root: dict[str, str] = {}
    order: list[str] = []
    stack: list[tuple[str, int, str]] = [
        (pid, 0, pid) for pid in reversed(children.get(None, []))
    ]
    while stack:
        pid, d, r = stack.pop()
        depth[pid], root[pid] = d, r
        order.append(pid)
        for kid in reversed(children.get(pid, [])):
            stack.append((kid, d + 1, r))

    for p in posts:
        pid = p["post_id"]
        p["depth"] = depth.get(pid, 0)
        p["root_post_id"] = root.get(pid, pid)
        p["reply_count"] = len(children.get(pid, []))
    return order


def build_feed_panel(rostore: RoStore, *, thread_id: str | None = None) -> dict[str, Any]:
    """Threads, one thread's full-text post stream, unread operator
    directives, and a per-post ``translation`` slot reading the AISPEAK
    sidecar table (``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md``) IF it
    exists on this program -- ``null`` otherwise.

    Every post also carries its place in the thread's reply structure --
    ``reply_to``, ``root_post_id``, ``depth``, ``reply_count``,
    ``reply_to_missing`` -- and the panel carries ``order_threaded``, the
    id sequence the THREADED reading order renders (see
    :func:`_thread_shape`). ``posts`` itself stays in ARRIVAL order.

    Each post also carries ``translation_state`` -- one of ``translated``,
    ``ungated``, ``withheld``, ``pending``, ``absent`` (see
    :func:`_translation_slot`). A ``withheld`` post's translation BODY is
    never included in the payload: the fail-closed gate
    (:mod:`trialerror.feed_translate.gate`) refused it, and a body the UI is
    forbidden to render has no business crossing the wire.

    ``inbox_item`` (the operator directive channel) carries NO
    ``thread_id`` column in the M1-built schema -- it is a program-wide
    channel, not a per-thread one (unlike what ``design/dashboard-v2/
    Feed.dc.html``'s mockup renders inline in the post stream). This
    builder reports unread directives honestly as their own top-level list
    rather than fabricating a thread association the schema doesn't carry.
    """
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    conn = rostore.ops
    threads = list_threads(rostore, limit=100)

    active_thread_id = thread_id
    if active_thread_id is None:
        # default: the thread with the most recent post (falls back to the
        # most recently created thread if no thread has any posts yet).
        row = conn.execute(
            "SELECT thread_id FROM feed_post ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            active_thread_id = row["thread_id"]
        elif threads:
            active_thread_id = threads[0]["thread_id"]

    posts: list[dict[str, Any]] = []
    order_threaded: list[str] = []
    if active_thread_id is not None:
        rows = conn.execute(
            "SELECT *, rowid AS _rowid FROM feed_post WHERE thread_id = ? ORDER BY ts ASC, _rowid ASC",
            (active_thread_id,),
        ).fetchall()
        raw = [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]
        translations = _load_translations_for_posts(conn, [d["post_id"] for d in raw])
        untranslated = [pid for pid, t in translations.items() if t is None]
        job_pending = _pending_translation_post_ids(rostore, untranslated)
        for d in raw:
            d["kind"] = _derive_post_kind(d["author"])
            d["translation"], d["translation_state"] = _translation_slot(
                translations[d["post_id"]], job_pending=d["post_id"] in job_pending
            )
            posts.append(d)
        order_threaded = _thread_shape(posts)

    # One GROUP BY for the whole rail, not one COUNT per thread row: the
    # rail lists up to 100 threads and the number beside each is decoration
    # on a list, not a reason to issue 100 queries. Counts RAW replies
    # (`in_reply_to IS NOT NULL`) per thread, so a cross-thread or cycle
    # parent -- which the ACTIVE thread reports as `reply_to_missing` --
    # still counts as a reply here. The rail's number is "posts written as
    # answers", which is the honest reading of an un-opened thread.
    thread_reply_counts = {
        r["thread_id"]: r["n"]
        for r in conn.execute(
            "SELECT thread_id, COUNT(*) AS n FROM feed_post "
            "WHERE in_reply_to IS NOT NULL GROUP BY thread_id"
        ).fetchall()
    }
    for t in threads:
        t["thread_reply_count"] = thread_reply_counts.get(t["thread_id"], 0)

    unread_directives = read_inbox(rostore, mark_read=False)

    return {
        "status": "ok",
        "threads": threads,
        "active_thread_id": active_thread_id,
        "posts": posts,
        "order_threaded": order_threaded,
        "unread_directives": unread_directives,
        "translator_table_available": _table_exists(conn, "feed_post_translation"),
        "translation_withheld_count": sum(1 for p in posts if p["translation_state"] == "withheld"),
    }


# ---------------------------------------------------------------------------
# rooms panel
# ---------------------------------------------------------------------------
_ROOM_EVENT_TYPES = (
    "room_created", "room_turn", "room_dp_scored", "room_converged",
    "room_frozen", "room_deliverable_registered",
)


def _list_rooms(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """No ``list_rooms()`` exists in ``trialerror.rooms.api`` (REDESIGN Section
    5.3 seam #3) -- a plain, read-only ``room`` scan, newest first
    (``created_ts`` with ``rowid`` as the tiebreak for pre-v3 rows whose
    ``created_ts`` is NULL, same convention every other list reader in this
    module already uses)."""
    rows = conn.execute(
        "SELECT *, rowid AS _rowid FROM room ORDER BY (created_ts IS NULL), created_ts DESC, _rowid DESC"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: v for k, v in dict(r).items() if k != "_rowid"}
        try:
            parsed = json.loads(d["dps"])
            config = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            config = {}
        d["participant_count"] = len(config.get("participants") or [])
        d["discussion_point_count"] = len(config.get("discussion_points") or [])
        out.append(d)
    return out


def _room_events_for(conn: sqlite3.Connection, room_id: str) -> list[dict[str, Any]]:
    """Every room-lifecycle event for ``room_id``, oldest first -- filtered
    in Python on ``payload.room_id`` (the ``event`` table has no indexed
    JSON column to push that predicate into SQL; see
    ``trialerror.rooms.api._emit_room_event``, the ONE place every one of
    :data:`_ROOM_EVENT_TYPES` is written and always stamps
    ``payload.room_id``)."""
    placeholders = ",".join("?" for _ in _ROOM_EVENT_TYPES)
    rows = conn.execute(
        f"SELECT *, rowid AS _rowid FROM event WHERE type IN ({placeholders}) ORDER BY ts ASC, _rowid ASC",
        _ROOM_EVENT_TYPES,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("room_id") != room_id:
            continue
        out.append(
            {
                "event_id": r["event_id"], "ts": r["ts"], "type": r["type"],
                "launch_id": r["launch_id"], "payload": payload,
            }
        )
    return out


def build_rooms_panel(rostore: RoStore, *, room_id: str | None = None) -> dict[str, Any]:
    """Room index, one active room's transcript, its current per-DP
    convergence status, its per-DP agreement TRAJECTORY (not just the
    latest score -- the V2 Rooms board draws convergence as a series;
    ``room_score`` itself only ever holds the LATEST value per DP (an
    upsert -- ``trialerror.rooms.api.score_dp``), so the series is reconstructed
    from the append-only ``room_dp_scored`` event trail, the only place
    every past scoring round's ``agreement_pct`` still lives), and
    moderator/lifecycle events."""
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    conn = rostore.ops
    rooms = _list_rooms(conn)

    active_room_id = room_id
    if active_room_id is None:
        open_rooms = [r for r in rooms if r["state"] == "open"]
        if open_rooms:
            active_room_id = open_rooms[0]["room_id"]
        elif rooms:
            active_room_id = rooms[0]["room_id"]

    active_room = None
    turns: list[dict[str, Any]] = []
    convergence: dict[str, Any] | None = None
    dp_agreement_series: dict[str, list[dict[str, Any]]] = {}
    moderator_events: list[dict[str, Any]] = []
    freeze_reason = None
    detail_error = None

    if active_room_id is not None:
        active_room = next((r for r in rooms if r["room_id"] == active_room_id), None)
        try:
            # trialerror.rooms.api's readers (list_room_turns/check_room_converged)
            # assume room.dps holds the {"discussion_points": [...], ...}
            # shape only trialerror.rooms.api.create_room ever writes -- a room
            # row seeded/migrated outside that path (e.g. a bare "[]"
            # placeholder) can violate that shape. This builder degrades to
            # a reported detail_error rather than a 500, the same "visible,
            # not refused" posture every other panel in this module uses.
            turns = list_room_turns(rostore, room_id=active_room_id)
            convergence = check_room_converged(rostore, active_room_id)
            events = _room_events_for(conn, active_room_id)
            moderator_events = events
            for ev in events:
                if ev["type"] != "room_dp_scored":
                    continue
                dp_id = ev["payload"].get("dp_id")
                if dp_id is None:
                    continue
                dp_agreement_series.setdefault(dp_id, []).append(
                    {
                        "ts": ev["ts"],
                        "agreement_pct": ev["payload"].get("agreement_pct"),
                        "note": ev["payload"].get("note"),
                        "converged": ev["payload"].get("converged"),
                    }
                )
            if active_room is not None and active_room["state"] == "frozen":
                freeze_reason = get_freeze_reason(rostore, active_room_id)
        except (TypeError, KeyError, ValueError) as exc:
            detail_error = f"{type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "rooms": rooms,
        "active_room_id": active_room_id,
        "active_room": active_room,
        "freeze_reason": freeze_reason,
        "turns": turns,
        "convergence": convergence,
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
        "dp_agreement_series": dp_agreement_series,
        "moderator_events": moderator_events,
        "detail_error": detail_error,
    }


# ---------------------------------------------------------------------------
# determinations panel
# ---------------------------------------------------------------------------
def _gate_edit_items(conn_ops: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row PER unverified blocking edit (not per gate -- REDESIGN S20/
    S21: the determination queue's unit of work is the edit an operator can
    individually verify or send back), each with a ``consequence`` string
    naming what unblocks if it is resolved."""
    rows = conn_ops.execute(
        "SELECT g.gate_id, g.artifact_id, g.state, g.verdict, g.edits, g.reproduction_status, "
        "g.critic_launch, g.verdict_ts, a.title, a.type "
        "FROM gate g JOIN artifact a ON g.artifact_id = a.artifact_id "
        "WHERE g.edits IS NOT NULL AND g.edits != '' AND g.edits != '[]' "
        "AND g.state NOT IN ('union_applied', 'registered')"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            edits = json.loads(d["edits"]) or []
        except (TypeError, ValueError):
            edits = []
        blocking_unverified = [e for e in edits if e.get("blocking") and not e.get("verified")]
        for e in blocking_unverified:
            remaining_after = len(blocking_unverified) - 1
            if remaining_after > 0:
                consequence = (
                    f"{remaining_after} more blocking edit(s) would remain on {d['gate_id']}; "
                    "the gate cannot enter union_applied until every blocking edit is verified."
                )
            elif d.get("reproduction_status") == "mismatch":
                consequence = (
                    f"This is the last blocking edit on {d['gate_id']}, but its reproduction_status "
                    "is 'mismatch' -- union_applied is refused until that is resolved too."
                )
            else:
                consequence = (
                    f"This is the last blocking edit on {d['gate_id']}. Verifying it clears the way "
                    f"for union_applied, and then registration of {d['artifact_id']} ({d['title']!r})."
                )
            items.append(
                {
                    "kind": "gate_edit",
                    "id": f"{d['gate_id']}::{e['edit_id']}",
                    "gate_id": d["gate_id"],
                    "edit_id": e["edit_id"],
                    "artifact_id": d["artifact_id"],
                    "artifact_title": d["title"],
                    "artifact_type": d["type"],
                    "text": e.get("text"),
                    "blocking": True,
                    "raised_by_launch": d.get("critic_launch"),
                    "raised_ts": d.get("verdict_ts"),
                    # lane C C7: an edit that was SENT BACK is still an
                    # unverified blocking edit -- it stays in this queue, and
                    # must, because the union is still refused. What changes is
                    # that somebody already objected and said why, and the next
                    # operator to reach this row needs to see that rather than
                    # verify it blind. `_normalize_edits` carries these keys
                    # through a later record_verdict (C3), so the objection
                    # survives.
                    "sent_back": bool(e.get("sent_back")),
                    "sent_back_note": e.get("sent_back_note"),
                    "sent_back_by_launch": e.get("sent_back_by_launch"),
                    "sent_back_ts": e.get("sent_back_ts"),
                    "consequence": consequence,
                }
            )
    return items


def _kg_merge_items(rostore: RoStore) -> list[dict[str, Any]]:
    if not rostore.is_available("knowledge"):
        return []
    pending = list_pending(rostore)
    items: list[dict[str, Any]] = []
    for prop in pending["merge_proposals"]:
        try:
            members = json.loads(prop["members"])
        except (TypeError, ValueError):
            members = []
        items.append(
            {
                "kind": "kg_merge",
                "id": prop["prop_id"],
                "canonical_entity": prop["canonical_entity"],
                "members": members,
                "reason": prop["reason"],
                "proposed_by_launch": prop["proposed_by_launch"],
                "blocking": False,
                "consequence": (
                    f"Accepting merges {len(members)} entity row(s) into {prop['canonical_entity']}; "
                    "rejecting leaves every member entity as its own row, unchanged."
                ),
            }
        )
    return items


def _acquisition_items(rostore: RoStore) -> list[dict[str, Any]]:
    if not rostore.is_available("knowledge"):
        return []
    rows = rostore.knowledge.execute(
        "SELECT * FROM source WHERE request_state IN ('wanted','requested','delivered','verifying') "
        "ORDER BY request_state, registered_ts"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        next_states = sorted(REQUEST_TRANSITIONS.get(d["request_state"], frozenset()))
        items.append(
            {
                "kind": "acquisition",
                "id": d["source_id"],
                "title": d["title"],
                "request_state": d["request_state"],
                "source_kind": d["kind"],
                "blocking": False,
                "consequence": (
                    f"Transitioning this source unblocks: {', '.join(next_states)}."
                    if next_states
                    else "This request state is terminal."
                ),
            }
        )
    return items


def _prereg_reveal_items(conn_ops: sqlite3.Connection) -> list[dict[str, Any]]:
    """Committed pre-registrations, and the HASHES ONLY (REDESIGN section 5.4;
    lane C C7): the whole point of a blind commitment is that the procedure
    stays unread until somebody deliberately un-blinds it, so the queue item
    must not carry the content -- not even to a page that promises not to draw
    it. What it carries is what an operator needs in order to decide: the two
    committed hashes, and whether the escrow file is still on disk.

    ``escrow_present`` is a stat, not a hash check: a reveal recomputes both
    hashes and voids the row if either has moved
    (``verify.prereg.reveal_prereg``), and doing that work here -- for every
    committed prereg, on every panel refresh -- would read the escrowed
    content on a passive page load. Missing file means the reveal will refuse;
    present says nothing more than that it can be attempted."""
    rows = conn_ops.execute(
        "SELECT * FROM prereg WHERE status = 'committed' ORDER BY committed_ts"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        escrow_path = d.get("escrow_path")
        escrow_present = False
        if escrow_path:
            try:
                escrow_present = Path(escrow_path).is_file()
            except OSError:
                escrow_present = False
        items.append(
            {
                "kind": "prereg_reveal",
                "id": d["prereg_id"],
                "title": d["title"],
                "committed_ts": d["committed_ts"],
                "procedure_sha256": d.get("procedure_sha256"),
                "params_sha256": d.get("params_sha256"),
                "escrow_present": escrow_present,
                "blocking": False,
                "consequence": (
                    "Revealing unseals the committed procedure/params hash so the pre-registered "
                    "result can be checked against them. It cannot be undone, and it is recorded "
                    "as a prereg_revealed event."
                    if escrow_present
                    else (
                        "The escrow file is not on disk at the path this commitment recorded. A "
                        "reveal will refuse and VOID this pre-registration -- a missing escrow is "
                        "a tamper finding, not a retryable error."
                    )
                ),
            }
        )
    return items


def _room_escalation_items(rostore: RoStore, conn_ops: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn_ops.execute("SELECT * FROM room WHERE state = 'frozen'").fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        reason = get_freeze_reason(rostore, d["room_id"])
        items.append(
            {
                "kind": "room_escalation",
                "id": d["room_id"],
                "topic": d["topic"],
                "reason": reason,
                "blocking": True,
                "consequence": "This room stays frozen until an operator turn resolves it (freeze-and-escalate).",
            }
        )
    return items


def _memory_conflict_items(rostore: RoStore) -> list[dict[str, Any]]:
    """Open conflict groups, each carrying BOTH SIDES (lane C C7).

    ``version_count`` alone made this the one determination kind an operator
    could not actually decide from the page: "2 versions of X disagree" is not
    something you can choose between. ``versions`` is the two rows, side
    labelled, with the bodies -- which is the whole content of the decision
    KEEP LEFT / KEEP RIGHT / KEEP BOTH asks for."""
    groups = list_memory_conflicts(rostore)
    items: list[dict[str, Any]] = []
    for g in groups:
        versions = [
            {
                "side": v["side"],
                "memory_item_id": v["memory_item_id"],
                "tier": v.get("tier"),
                "kind": v.get("kind"),
                "account_id": v.get("account_id"),
                "updated_ts": v.get("updated_ts"),
                "l0_abstract": v.get("l0_abstract"),
                "body": v.get("body"),
            }
            for v in sorted(g["versions"], key=lambda v: v["side"])
        ]
        items.append(
            {
                "kind": "memory_conflict",
                "id": g["group_id"],
                "key": g["key"],
                "version_count": len(versions),
                "versions": versions,
                "blocking": False,
                "consequence": (
                    f"Resolving keeps one version of {g['key']!r} active and marks the other "
                    "superseded (or KEEP BOTH, when the two were never one fact). It is one-shot "
                    "per group -- a second answer is refused."
                ),
            }
        )
    return items


def _memory_stale_items(rostore: RoStore) -> list[dict[str, Any]]:
    """MINING ADOPTION engram-F5: memory items whose type-keyed review
    clock has run out. Non-blocking, and the ``consequence`` says plainly
    that reviewing changes nothing but the clock -- the panel must not
    read as "this item is about to expire", because nothing here expires
    on a timer (review section 5.7).

    Tolerant of a store predating ops v7 exactly as its engram-F4 sibling
    :func:`_memory_conflict_candidate_items` is (no
    ``memory_item.reviewed_ts`` column -> no items). The guard matters more
    here than there: this panel only ever holds a READ-ONLY connection
    (``open_store_ro`` never migrates), so an un-migrated program cannot
    heal itself on this path, and :func:`build_all_panels` has no
    per-panel ``try``/``except`` -- one missing column would take down the
    ENTIRE dashboard payload rather than this one list."""
    import sqlite3 as _sqlite3

    from trialerror.memory.staleness import stale_items as _stale

    try:
        stale = _stale(rostore.ops)
    except _sqlite3.OperationalError:
        return []
    return [
        {
            "kind": "memory_stale",
            "id": s["memory_item_id"],
            "key": s["key"],
            "item_kind": s["kind"],
            "overdue_days": s["overdue_days"],
            "half_life_days": s["half_life_days"],
            "blocking": False,
            "consequence": (
                f"Reviewing {s['key']!r} resets its review clock and changes nothing else "
                "-- decay never expires, unpins, or downgrades anything."
            ),
        }
        for s in stale
    ]


def _memory_conflict_candidate_items(rostore: RoStore) -> list[dict[str, Any]]:
    """MINING ADOPTION engram-F4: save-time conflict candidates nobody has
    judged yet. Tolerant of a store predating ops v7 (no table -> no
    items), so an un-migrated program still renders the panel instead of
    erroring on it."""
    import sqlite3 as _sqlite3

    from trialerror.memory.conflicts import list_candidates as _list_candidates

    try:
        rows = _list_candidates(rostore.ops, status="pending")
    except _sqlite3.OperationalError:
        return []
    return [
        {
            "kind": "memory_conflict_candidate",
            "id": r["relation_id"],
            "key": r["source_key"],
            "target_key": r["target_key"],
            "score": r["score"],
            "blocking": False,
            "consequence": (
                f"Judging records one verb about {r['source_key']!r} vs {r['target_key']!r} with your name on it; "
                "nothing about either item changes."
            ),
        }
        for r in rows
    ]


def build_determinations_panel(rostore: RoStore) -> dict[str, Any]:
    """The one determination queue -- REDESIGN S20 (``build_review_panel``
    unioning three existing reads, no new tables) plus S21 (a
    ``consequence`` field per item, "what happens if you verify") and S26
    (memory-merge conflicts, "queue kind, not drawn"). Nine kinds today:
    gate edits awaiting verification, KG merge proposals, acquisition
    requests, pre-registration reveals, room freeze-and-escalate events,
    memory-sync conflicts, -- from the 2026-09 mining adoptions --
    unjudged save-time memory conflict candidates (engram-F4) and memory
    items past their type-keyed review half-life (engram-F5), and (lane
    L0-C) documents waiting for the DEV GPU worker, and -- lane a --
    web-ingestion hosts awaiting a human's approval and a stopped fetch
    process with URLs queued behind it. Most of the newer kinds are
    non-blocking by construction: they are prompts to LOOK at something,
    never gates on anything. ``webfetch_sidecar_down`` is the exception
    and says so -- a queued fetch does not move at all while the process
    that drains it is gone."""
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    conn = rostore.ops
    items: list[dict[str, Any]] = []
    items.extend(_gate_edit_items(conn))
    items.extend(_kg_merge_items(rostore))
    items.extend(_acquisition_items(rostore))
    items.extend(_prereg_reveal_items(conn))
    items.extend(_room_escalation_items(rostore, conn))
    items.extend(_memory_conflict_items(rostore))
    items.extend(_memory_conflict_candidate_items(rostore))
    items.extend(_memory_stale_items(rostore))
    items.extend(offload_backlog_items(rostore))
    items.extend(webfetch_items(rostore))

    counts_by_kind: dict[str, int] = {}
    for item in items:
        counts_by_kind[item["kind"]] = counts_by_kind.get(item["kind"], 0) + 1

    return {
        "status": "ok",
        "items": items,
        "counts_by_kind": counts_by_kind,
        "blocking_count": sum(1 for i in items if i.get("blocking")),
        "total": len(items),
    }


# ---------------------------------------------------------------------------
# dossier panel
# ---------------------------------------------------------------------------
def _version_chain(conn_ops: sqlite3.Connection, artifact_id: str) -> list[dict[str, Any]]:
    """Every artifact linked to ``artifact_id`` by the ``supersedes`` chain,
    in EITHER direction (older versions this one supersedes, and any newer
    version that later superseded it), oldest first. Built by walking
    ``artifact.supersedes`` -- the only version-chain data this schema
    carries (REDESIGN R12's "honest lineage": no separate version-chain
    table exists, or is needed, since ``supersedes`` already forms one)."""
    chain: dict[str, dict[str, Any]] = {}
    frontier = [artifact_id]
    seen: set[str] = set()
    while frontier:
        aid = frontier.pop()
        if aid in seen:
            continue
        seen.add(aid)
        row = conn_ops.execute(
            "SELECT artifact_id, title, status, registered_ts, supersedes FROM artifact WHERE artifact_id = ?",
            (aid,),
        ).fetchone()
        if row is None:
            continue
        chain[aid] = dict(row)
        if row["supersedes"]:
            frontier.append(row["supersedes"])
        newer = conn_ops.execute("SELECT artifact_id FROM artifact WHERE supersedes = ?", (aid,)).fetchall()
        frontier.extend(r["artifact_id"] for r in newer)
    return sorted(chain.values(), key=lambda r: r.get("registered_ts") or "")


def build_dossier_panel(rostore: RoStore, *, artifact_id: str | None = None) -> dict[str, Any]:
    """Registry rail (every artifact + its type filter chips), and one
    artifact's full detail: purpose, version chain, gate history with edit
    states, verdicts, and lineage assembled honestly from the launch
    ledger, gate history and supersession/record/criterion links.

    ``knowledge.prov_edge`` has ZERO writers anywhere in this codebase
    (REDESIGN R12 finding, confirmed again in this build) -- this builder
    does NOT read it, and says so in ``lineage.note`` rather than drawing
    an empty provenance graph as if it meant something.
    """
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    conn = rostore.ops
    registry = list_artifacts(rostore, limit=200)
    type_filters = [dict(r) for r in conn.execute("SELECT type_key, title, gated FROM template ORDER BY type_key").fetchall()]

    active_artifact_id = artifact_id
    if active_artifact_id is None and registry:
        active_artifact_id = registry[0]["artifact_id"]

    artifact = None
    gate = None
    gate_history: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    version_chain: list[dict[str, Any]] = []
    lineage: dict[str, Any] | None = None
    context_frame = None

    if active_artifact_id is not None:
        artifact_row = conn.execute("SELECT * FROM artifact WHERE artifact_id = ?", (active_artifact_id,)).fetchone()
        if artifact_row is not None:
            artifact = dict(artifact_row)
            attrs = json.loads(artifact["attrs"]) if artifact.get("attrs") else {}
            # R10/seam item 9: artifact.context_frame does not exist as a
            # column yet (REDESIGN Section 5.3 item 9) -- read it
            # best-effort from attrs if a producer already chose to stash
            # one there, else null, never fabricated.
            context_frame = attrs.get("context_frame") if isinstance(attrs, dict) else None

            if artifact.get("gate_id"):
                gate_row = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (artifact["gate_id"],)).fetchone()
                if gate_row is not None:
                    gate = dict(gate_row)
                    gate["edits_parsed"] = json.loads(gate["edits"]) if gate.get("edits") else []
                    gate_history = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT * FROM gate_transition WHERE gate_id = ? ORDER BY id ASC", (gate["gate_id"],)
                        ).fetchall()
                    ]

            if rostore.is_available("knowledge"):
                verdicts = [
                    dict(r)
                    for r in rostore.knowledge.execute(
                        "SELECT * FROM verdict WHERE subject_kind = 'artifact' AND subject_id = ? ORDER BY ts DESC",
                        (active_artifact_id,),
                    ).fetchall()
                ]

            version_chain = _version_chain(conn, active_artifact_id)

            produced_by_launch = None
            in_session = None
            if artifact.get("registered_by_launch") and rostore.is_available("platform"):
                launch_row = rostore.platform.execute(
                    "SELECT launch_id, agent_kind, purpose, session_id FROM launch WHERE launch_id = ?",
                    (artifact["registered_by_launch"],),
                ).fetchone()
                if launch_row is not None:
                    produced_by_launch = dict(launch_row)
                    in_session = launch_row["session_id"]

            registers_records = 0
            if rostore.is_available("knowledge"):
                registers_records = rostore.knowledge.execute(
                    "SELECT COUNT(*) FROM record WHERE artifact_id = ?", (active_artifact_id,)
                ).fetchone()[0]

            discharges_criteria: list[dict[str, Any]] = []
            if _table_exists(conn, "criterion"):
                discharges_criteria = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT criterion_id, label, phase FROM criterion WHERE discharged_by_artifact = ?",
                        (active_artifact_id,),
                    ).fetchall()
                ]

            superseded_by = [
                r["artifact_id"]
                for r in conn.execute("SELECT artifact_id FROM artifact WHERE supersedes = ?", (active_artifact_id,)).fetchall()
            ]

            lineage = {
                "produced_by_launch": produced_by_launch,
                "in_session": in_session,
                "supersedes": artifact.get("supersedes"),
                "superseded_by": superseded_by,
                "registers_records": registers_records,
                "discharges_criteria": discharges_criteria,
                "note": (
                    "Assembled only from the launch ledger, gate history, artifact.supersedes and "
                    "record/criterion links. knowledge.prov_edge has zero writers in this codebase, "
                    "so no general consumed-source provenance graph is drawn here."
                ),
            }

    return {
        "status": "ok",
        "registry": registry,
        "type_filters": type_filters,
        "active_artifact_id": active_artifact_id,
        "artifact": artifact,
        "context_frame": context_frame,
        "gate": gate,
        "gate_history": gate_history,
        "verdicts": verdicts,
        "version_chain": version_chain,
        "lineage": lineage,
    }


# ---------------------------------------------------------------------------
# evidence panel (lane C item A, spec section 1)
# ---------------------------------------------------------------------------

#: Live claims listed in the Evidence rail. Past this the rail says how many
#: it is not showing rather than growing without bound (``index_truncated``).
EVIDENCE_INDEX_LIMIT = 100

#: Seed entities the neighbourhood expands from -- spec section 1.2's cap. One
#: ``k_hop_neighbors`` call per seed, so this multiplies the graph query cost
#: directly; the count NOT expanded is reported as ``seeds_dropped`` rather
#: than dropped in silence.
EVIDENCE_MAX_SEEDS = 5

#: Edges written into the payload. The union may be larger -- ``edge_count``
#: is the true size and the table footer prints the difference.
EVIDENCE_MAX_EDGES_LISTED = 100

#: How many co-anchored claim candidates are examined. A claim's document may
#: carry thousands of claims; this is the read that would otherwise scale with
#: the corpus rather than with the claim.
EVIDENCE_CO_ANCHORED_SCAN_LIMIT = 200

#: Ids bound into ONE ``IN (...)`` list. The neighbourhood's anchor pool and
#: its entity candidates are sized by the corpus, not by any cap this module
#: sets: every anchor sharing a chunk with the claim joins the pool, and every
#: entity endpoint of a live relation on one of those anchors joins the
#: candidates. Past ``SQLITE_LIMIT_VARIABLE_NUMBER`` the query raises
#: ``sqlite3.OperationalError: too many SQL variables`` -- 32766 on SQLite
#: 3.32+, but 999 on older builds, which one chunk's anchors can reach.
#: ``isolated_panel`` fences it, so the symptom is an Evidence panel
#: permanently reading ``{"status": "error"}`` rather than a 500: visible, and
#: undiagnosable from the page. :func:`_rows_in_batches` SPLITS the list
#: rather than capping it, so no anchor is dropped from the region and the
#: payload keeps meaning exactly what it says (lane C, finding F3).
_SQL_MAX_IN_PARAMS = 400


def _rows_in_batches(
    conn: sqlite3.Connection, sql: str, ids: Sequence[str], *, batch_size: int | None = None
) -> list[sqlite3.Row]:
    """Run ``sql`` -- which carries exactly one ``{placeholders}`` slot for an
    ``IN`` list and no other braces -- once per batch of ``ids``, returning
    the concatenated rows.

    The batches are independent queries, so any ORDER BY inside ``sql`` orders
    within a batch and not across them: a caller that needs a global order
    must select its sort columns and re-sort the result (as
    :func:`_evidence_neighbourhood` does). Rows are NOT de-duplicated -- every
    caller here queries a primary key or a column the ids partition, so a row
    cannot match two batches."""
    size = batch_size or _SQL_MAX_IN_PARAMS
    out: list[sqlite3.Row] = []
    ordered = list(ids)
    for start in range(0, len(ordered), size):
        chunk = ordered[start : start + size]
        placeholders = ",".join("?" for _ in chunk)
        out.extend(conn.execute(sql.format(placeholders=placeholders), chunk).fetchall())
    return out


def _extra_anchor_ids(value: Any) -> list[str]:
    """``claim.extra_anchors`` / ``relation.extra_anchors`` decoded to a list
    of anchor ids. The column is nullable JSON text; anything that is not a
    JSON array of strings yields ``[]`` rather than raising -- a malformed
    column must cost this panel one anchor row, never the page."""
    decoded = _decode_json_text(value)
    if not isinstance(decoded, list):
        return []
    return [a for a in decoded if isinstance(a, str) and a]


def _evidence_claim_anchor_ids(claim_row: dict[str, Any]) -> list[str]:
    """Primary first, then extras, de-duplicated, order preserved -- the
    ``role`` a rendered anchor row carries is exactly "is it index 0"."""
    out: list[str] = []
    for aid in [claim_row.get("anchor_id"), *_extra_anchor_ids(claim_row.get("extra_anchors"))]:
        if aid and aid not in out:
            out.append(aid)
    return out


def _evidence_anchor_context(conn: sqlite3.Connection, anchor_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``anchor_id -> {anchor row, doc row, source row}`` in three queries,
    the same anchor -> document -> source license-provenance walk
    :func:`trialerror.retrieve.engine._fence_relation_edges` does (and for the
    same reason: the license tier that decides the fence is two joins away
    from the text being fenced)."""
    ids = [a for a in dict.fromkeys(anchor_ids) if a]
    if not ids:
        return {}
    aph = ",".join("?" for _ in ids)
    anchors = {r["anchor_id"]: dict(r) for r in conn.execute(f"SELECT * FROM quote_anchor WHERE anchor_id IN ({aph})", ids)}
    doc_ids = sorted({a["doc_id"] for a in anchors.values() if a.get("doc_id")})
    documents: dict[str, dict[str, Any]] = {}
    if doc_ids:
        dph = ",".join("?" for _ in doc_ids)
        documents = {r["doc_id"]: dict(r) for r in conn.execute(f"SELECT * FROM document WHERE doc_id IN ({dph})", doc_ids)}
    source_ids = sorted({d["source_id"] for d in documents.values() if d.get("source_id")})
    sources: dict[str, dict[str, Any]] = {}
    if source_ids:
        sph = ",".join("?" for _ in source_ids)
        sources = {r["source_id"]: dict(r) for r in conn.execute(f"SELECT * FROM source WHERE source_id IN ({sph})", source_ids)}

    out: dict[str, dict[str, Any]] = {}
    for aid, anchor in anchors.items():
        doc = documents.get(anchor.get("doc_id"))
        source = sources.get(doc["source_id"]) if doc else None
        out[aid] = {"anchor": anchor, "document": doc, "source": source}
    return out


def _evidence_anchor_rows(conn: sqlite3.Connection, claim_row: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """WHAT IT STANDS ON, plus whether the PRIMARY anchor's source is fenced
    (which is what decides the fence on ``claim.text`` -- one claim, one
    license posture, taken from the anchor the schema calls primary).

    Two hash chips per row, and they answer different questions:

    - ``doc_sha_matches`` -- ``quote_anchor.doc_sha256`` against the
      ``document.sha256`` the document carries NOW. False means the document
      was re-ingested under this anchor's feet, so the char offsets may point
      at different bytes (the corpus panel's own stale-anchor predicate).
    - ``quote_sha_matches`` -- the stored ``quote_text`` re-hashed against
      ``quote_sha256``. ``None``, not ``False``, when no ``quote_text`` was
      stored: "not re-checkable here" is a third reading, and collapsing it
      into False would accuse an anchor that is merely terse.
    """
    from trialerror.ingest.anchors import sha256_hex

    anchor_ids = _evidence_claim_anchor_ids(claim_row)
    context = _evidence_anchor_context(conn, anchor_ids)
    rows: list[dict[str, Any]] = []
    primary_fenced = False
    for position, aid in enumerate(anchor_ids):
        entry = context.get(aid)
        role = "primary" if position == 0 else "extra"
        if entry is None:
            # A claim naming an anchor row that is not there is a broken FK,
            # not a reason to render nothing -- say which anchor is missing.
            rows.append(
                {
                    "anchor_id": aid, "role": role, "doc_id": None, "chunk_id": None, "source_id": None,
                    "source_title": None, "license_tier": None, "page": None,
                    "char_start": None, "char_end": None, "quote": "", "fenced": False,
                    "doc_sha_matches": False, "quote_sha_matches": None, "missing": True,
                }
            )
            continue
        anchor, doc, source = entry["anchor"], entry["document"], entry["source"]
        license_tier = source.get("license_tier") if source else None
        fenced = is_fenced_license(license_tier)
        if role == "primary":
            primary_fenced = fenced
        quote_text = anchor.get("quote_text")
        quote_sha_matches = None
        if quote_text:
            quote_sha_matches = sha256_hex(quote_text) == anchor.get("quote_sha256")
        rows.append(
            {
                "anchor_id": aid,
                "role": role,
                "doc_id": anchor.get("doc_id"),
                "chunk_id": anchor.get("chunk_id"),
                "source_id": doc.get("source_id") if doc else None,
                "source_title": source.get("title") if source else None,
                "license_tier": license_tier,
                "page": anchor.get("page_number"),
                "char_start": anchor.get("char_start"),
                "char_end": anchor.get("char_end"),
                # Per-anchor ``quote`` follows the engine's own precedent for
                # this exact field (``get_chunk``/``resolve_quote``): capped by
                # ``citation_quote``, NOT untrusted-wrapped. The wrapper marks a
                # whole free-text BODY (``claim.text``, ``fact_text``), and it
                # is those two that spec section 1.2 names.
                "quote": citation_quote(quote_text, fenced=fenced),
                "fenced": fenced,
                "doc_sha_matches": bool(doc and anchor.get("doc_sha256") == doc.get("sha256")),
                "quote_sha_matches": quote_sha_matches,
                "missing": False,
            }
        )
    return rows, primary_fenced


def _evidence_index(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], int, bool]:
    """The claim rail: live claims newest first, each already carrying the
    source it hangs off, so the rail can be filtered on a title without a
    second round trip per row."""
    total = conn.execute("SELECT COUNT(*) FROM claim WHERE expired_at IS NULL AND invalid_at IS NULL").fetchone()[0]
    rows = conn.execute(
        "SELECT c.claim_id, c.kind, c.text, c.confidence, c.created_at, c.superseded_by, "
        "c.anchor_id, c.extra_anchors, d.source_id AS source_id, s.title AS source_title, "
        "s.license_tier AS license_tier "
        "FROM claim c "
        "LEFT JOIN quote_anchor qa ON qa.anchor_id = c.anchor_id "
        "LEFT JOIN document d ON d.doc_id = qa.doc_id "
        "LEFT JOIN source s ON s.source_id = d.source_id "
        "WHERE c.expired_at IS NULL AND c.invalid_at IS NULL "
        "ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
        (EVIDENCE_INDEX_LIMIT,),
    ).fetchall()
    index: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        fenced = is_fenced_license(d.get("license_tier"))
        # The rail label is fence-capped but NOT untrusted-wrapped: the
        # wrapper delimits a whole body, and truncating a wrapped string can
        # cut its closing delimiter off -- precisely the forged-close failure
        # ``untrusted_wrap`` exists to make impossible. The full, wrapped text
        # is one click away in ``claim.text``.
        index.append(
            {
                "claim_id": d["claim_id"],
                "kind": d["kind"],
                "text_short": _truncate(citation_quote(d.get("text"), fenced=fenced)),
                "confidence": d.get("confidence"),
                "created_at": d.get("created_at"),
                "source_id": d.get("source_id"),
                "source_title": d.get("source_title"),
                "anchor_count": len(_evidence_claim_anchor_ids(d)),
                "superseded": bool(d.get("superseded_by")),
            }
        )
    return index, total, total > len(index)


def _evidence_resolve_claim(
    conn: sqlite3.Connection, *, claim_id: str | None, anchor_id: str | None, chunk_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Spec section 1.2's resolution order, returning ``(claim row, not_found)``.

    ``claim_id`` -> ``anchor_id`` -> ``chunk_id`` -> the newest live claim.
    An id that resolves to nothing is a READING (``not_found``), never a 404:
    a TRACE from a search result whose anchor carries no claim yet is an
    ordinary state of a young corpus, and the panel still renders its rail.

    A claim named EXPLICITLY is returned even when it is expired/invalidated
    -- the rail lists the live view, but an operator who followed a link to a
    retired claim must be shown the claim they asked for, with its own
    ``expired_at``/``invalid_at`` on the row saying so."""
    if claim_id:
        row = conn.execute("SELECT * FROM claim WHERE claim_id = ?", (claim_id,)).fetchone()
        return (dict(row), None) if row is not None else (None, {"kind": "claim_id", "id": claim_id})

    if anchor_id:
        rows = conn.execute(
            "SELECT * FROM claim WHERE expired_at IS NULL AND invalid_at IS NULL "
            "AND (anchor_id = ? OR extra_anchors LIKE ?) ORDER BY created_at DESC, rowid DESC",
            (anchor_id, f"%{anchor_id}%"),
        ).fetchall()
        # LIKE is a prefilter only -- the authoritative membership test is the
        # decoded JSON list, so an anchor id that merely appears as a
        # substring inside another id can never claim a row.
        matches = [dict(r) for r in rows if anchor_id in _evidence_claim_anchor_ids(dict(r))]
        if matches:
            return matches[0], None
        return None, {"kind": "anchor_id", "id": anchor_id}

    if chunk_id:
        chunk_anchor_ids = [
            r["anchor_id"] for r in conn.execute("SELECT anchor_id FROM quote_anchor WHERE chunk_id = ?", (chunk_id,)).fetchall()
        ]
        if chunk_anchor_ids:
            # Batched, and re-sorted across the batches: one chunk's anchor
            # count is corpus-sized (F3), and the newest row is the answer.
            rows = sorted(
                _rows_in_batches(
                    conn,
                    "SELECT *, rowid AS _rowid FROM claim WHERE expired_at IS NULL AND invalid_at IS NULL "
                    "AND anchor_id IN ({placeholders})",
                    chunk_anchor_ids,
                ),
                key=lambda r: ((r["created_at"] or ""), r["_rowid"]),
                reverse=True,
            )
            if rows:
                found = dict(rows[0])
                found.pop("_rowid", None)
                return found, None
            # A claim reaching this chunk only through an EXTRA anchor counts too.
            for aid in chunk_anchor_ids:
                for r in conn.execute(
                    "SELECT * FROM claim WHERE expired_at IS NULL AND invalid_at IS NULL "
                    "AND extra_anchors LIKE ? ORDER BY created_at DESC, rowid DESC",
                    (f"%{aid}%",),
                ).fetchall():
                    d = dict(r)
                    if aid in _evidence_claim_anchor_ids(d):
                        return d, None
        return None, {"kind": "chunk_id", "id": chunk_id}

    row = conn.execute(
        "SELECT * FROM claim WHERE expired_at IS NULL AND invalid_at IS NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return (dict(row) if row is not None else None), None


def _evidence_argues(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    """WHAT ARGUES WITH IT. Two sources, and the panel is explicit that only
    one of them has a writer today: ``verdict(subject_kind='claim')`` is the
    live signal (``procedure='contracrow'`` is the contradiction check), while
    ``prov_edge`` -- the general provenance graph a "contradicts" edge would
    live on -- has ZERO writers anywhere in this codebase (the same finding
    :func:`build_dossier_panel` records in its own ``lineage.note``). Reading
    it and reporting empty is the honest form; drawing an empty graph as
    though it meant "nothing contradicts this" is not."""
    edges = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM prov_edge WHERE role IN ('contradicts','supports') "
            "AND ((src_kind = 'claim' AND src_id = ?) OR (dst_kind = 'claim' AND dst_id = ?)) "
            "ORDER BY ts DESC",
            (claim_id, claim_id),
        ).fetchall()
    ]
    verdicts = [
        {
            "verdict_id": r["verdict_id"],
            "procedure": r["procedure"],
            "procedure_version": r["procedure_version"],
            "label": r["label"],
            "ts": r["ts"],
            "issued_by_launch": r["issued_by_launch"],
            "prereg_compliant": r["prereg_compliant"],
        }
        for r in conn.execute(
            "SELECT * FROM verdict WHERE subject_kind = 'claim' AND subject_id = ? ORDER BY ts DESC",
            (claim_id,),
        ).fetchall()
    ]
    return {
        "contradicts": [e for e in edges if e["role"] == "contradicts"],
        "supports": [e for e in edges if e["role"] == "supports"],
        "verdicts": verdicts,
        "note": (
            "prov_edge has zero writers; contradiction verdicts (procedure='contracrow') "
            "are the live signal"
        ),
    }


def _evidence_co_anchored(
    conn: sqlite3.Connection, *, claim_id: str, anchor_ids: Sequence[str], context: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Other live claims standing on the SAME evidence, strongest sharing
    first: the same anchor, then the same chunk, then merely the same
    document. Bounded by :data:`EVIDENCE_CO_ANCHORED_SCAN_LIMIT` candidates --
    a heavily-claimed document would otherwise make this read scale with the
    corpus instead of with the claim.

    Known bound, stated rather than hidden: candidates are found through
    their PRIMARY anchor's document, plus a direct extras match on THIS
    claim's own anchors. A claim that shares only a document with this one,
    and only through one of its own extra anchors, is not listed."""
    own = set(anchor_ids)
    own_chunks = {e["anchor"].get("chunk_id") for e in context.values() if e["anchor"].get("chunk_id")}
    own_docs = {e["anchor"].get("doc_id") for e in context.values() if e["anchor"].get("doc_id")}
    if not own_docs:
        return []

    doc_list = sorted(own_docs)
    dph = ",".join("?" for _ in doc_list)
    candidates: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        f"SELECT c.* FROM claim c JOIN quote_anchor qa ON qa.anchor_id = c.anchor_id "
        f"WHERE c.expired_at IS NULL AND c.invalid_at IS NULL AND c.claim_id != ? "
        f"AND qa.doc_id IN ({dph}) ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
        [claim_id, *doc_list, EVIDENCE_CO_ANCHORED_SCAN_LIMIT],
    ).fetchall():
        candidates[r["claim_id"]] = dict(r)
    for aid in sorted(own):
        for r in conn.execute(
            "SELECT * FROM claim WHERE expired_at IS NULL AND invalid_at IS NULL AND claim_id != ? "
            "AND extra_anchors LIKE ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (claim_id, f"%{aid}%", EVIDENCE_CO_ANCHORED_SCAN_LIMIT),
        ).fetchall():
            candidates.setdefault(r["claim_id"], dict(r))

    if not candidates:
        return []
    all_anchor_ids = {a for row in candidates.values() for a in _evidence_claim_anchor_ids(row)}
    cand_context = _evidence_anchor_context(conn, sorted(all_anchor_ids))

    out: list[dict[str, Any]] = []
    for row in candidates.values():
        their = _evidence_claim_anchor_ids(row)
        shared: str | None = None
        if own.intersection(their):
            shared = "anchor"
        else:
            their_chunks = {cand_context[a]["anchor"].get("chunk_id") for a in their if a in cand_context} - {None}
            their_docs = {cand_context[a]["anchor"].get("doc_id") for a in their if a in cand_context} - {None}
            if own_chunks.intersection(their_chunks):
                shared = "chunk"
            elif own_docs.intersection(their_docs):
                shared = "document"
        if shared is None:
            continue
        primary = cand_context.get(row.get("anchor_id"))
        fenced = is_fenced_license((primary["source"] or {}).get("license_tier")) if primary else False
        out.append(
            {
                "claim_id": row["claim_id"],
                "kind": row["kind"],
                "text_short": _truncate(citation_quote(row.get("text"), fenced=fenced)),
                "shared": shared,
            }
        )
    rank = {"anchor": 0, "chunk": 1, "document": 2}
    out.sort(key=lambda c: (rank[c["shared"]], c["claim_id"]))
    return out


def _evidence_neighbourhood(
    rostore: RoStore, *, claim_row: dict[str, Any], anchor_ids: Sequence[str], context: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The bounded knowledge-graph view around this claim's evidence.

    Seeds are the entity endpoints of LIVE relations whose ``evidence_anchor``
    is one of this claim's anchors, or an anchor on one of the same chunks --
    the chunk widening is what makes the region non-empty for a corpus whose
    KG writer anchors relations at chunk granularity while the claim writer
    anchors at quote granularity.

    Expansion is :func:`trialerror.retrieve.engine.k_hop_neighbors` per seed,
    unioned by ``rel_id``. That function is used unmodified, which is what
    buys this panel the engine's own caps, its bi-temporal live filter, and
    -- load-bearing here -- ``_fence_relation_edges``: every ``fact_text``
    arriving from it is already license-fenced and untrusted-wrapped.

    The anchor pool and the entity candidates are sized by the corpus (every
    anchor on the claim's chunks; every endpoint of a live relation on one of
    them), so every ``IN`` list below goes through :func:`_rows_in_batches`
    rather than binding one parameter per id -- see ``_SQL_MAX_IN_PARAMS``."""
    conn = rostore.knowledge
    chunk_ids = sorted({e["anchor"].get("chunk_id") for e in context.values() if e["anchor"].get("chunk_id")})
    anchor_pool = set(anchor_ids)
    if chunk_ids:
        # chunk_ids is bounded by the claim's own anchors, but batched with
        # the rest so one convention covers every IN list in this function.
        anchor_pool |= {
            r["anchor_id"]
            for r in _rows_in_batches(
                conn,
                "SELECT anchor_id FROM quote_anchor WHERE chunk_id IN ({placeholders})",
                chunk_ids,
            )
        }

    seed_rows: list[dict[str, Any]] = []
    if anchor_pool:
        pool = sorted(anchor_pool)
        # Batched (F3): the pool is corpus-sized. `created_at`/`rowid` are
        # selected so the newest-first order survives being reassembled from
        # several queries -- the order decides which anchor is recorded as a
        # seed's `via_anchor` and which five entities get expanded, so it is
        # not cosmetic.
        seed_rows = [
            dict(r)
            for r in sorted(
                _rows_in_batches(
                    conn,
                    "SELECT rel_id, src_entity, dst_entity, evidence_anchor, created_at, rowid AS _rowid "
                    "FROM relation WHERE evidence_anchor IN ({placeholders}) "
                    "AND expired_at IS NULL AND invalid_at IS NULL",
                    pool,
                ),
                key=lambda r: ((r["created_at"] or ""), r["_rowid"]),
                reverse=True,
            )
        ]

    seen: dict[str, str] = {}
    for r in seed_rows:
        for eid in (r["src_entity"], r["dst_entity"]):
            if eid and eid not in seen:
                seen[eid] = r["evidence_anchor"]
    candidate_ids = list(seen)
    # An entity id with no row cannot be expanded; filter here rather than let
    # k_hop_neighbors raise EntityNotFoundError from inside a panel builder.
    known: dict[str, dict[str, Any]] = {}
    if candidate_ids:
        known = {
            r["entity_id"]: dict(r)
            for r in _rows_in_batches(
                conn,
                "SELECT entity_id, name, entity_type FROM entity WHERE entity_id IN ({placeholders})",
                candidate_ids,
            )
        }
    resolvable = [eid for eid in candidate_ids if eid in known]
    seeds = resolvable[:EVIDENCE_MAX_SEEDS]
    seeds_dropped = len(resolvable) - len(seeds)

    edges_by_id: dict[str, dict[str, Any]] = {}
    node_ids: set[str] = set()
    truncated = False
    hops_reached = 0
    max_hops = retrieve_engine.DEFAULT_MAX_HOPS
    hop_limit = retrieve_engine.DEFAULT_HOP_LIMIT
    for eid in seeds:
        result = retrieve_engine.k_hop_neighbors(rostore, eid, max_hops=retrieve_engine.DEFAULT_MAX_HOPS)
        max_hops = result["max_hops"]
        hop_limit = result["hop_limit"]
        hops_reached = max(hops_reached, result["hops_reached"])
        truncated = truncated or bool(result["truncated"])
        node_ids |= set(result["nodes"])
        for e in result["edges"]:
            edges_by_id.setdefault(e["rel_id"], e)

    label_ids = sorted(node_ids - set(known))
    if label_ids:
        for r in _rows_in_batches(
            conn,
            "SELECT entity_id, name, entity_type FROM entity WHERE entity_id IN ({placeholders})",
            label_ids,
        ):
            known[r["entity_id"]] = dict(r)

    nodes: list[dict[str, Any]] = [{"id": claim_row["claim_id"], "kind": "claim", "label": claim_row["claim_id"]}]
    for eid in sorted(node_ids):
        nodes.append({"id": eid, "kind": "entity", "label": (known.get(eid) or {}).get("name") or eid})

    ordered = sorted(edges_by_id.values(), key=lambda e: ((e.get("created_at") or ""), e["rel_id"]), reverse=True)
    edges = [
        {
            "rel_id": e["rel_id"],
            "src": e["src_entity"],
            "dst": e["dst_entity"],
            "rel_type": e["rel_type"],
            "fact_text": e["fact_text"],
            "fenced": e.get("fenced", False),
            "evidence_anchor": e.get("evidence_anchor"),
        }
        for e in ordered[:EVIDENCE_MAX_EDGES_LISTED]
    ]
    return {
        "seed_entities": [
            {
                "entity_id": eid,
                "name": known[eid].get("name"),
                "entity_type": known[eid].get("entity_type"),
                "via_anchor": seen[eid],
            }
            for eid in seeds
        ],
        "nodes": nodes,
        "edges": edges,
        "max_hops": max_hops,
        "hops_reached": hops_reached,
        "hop_limit": hop_limit,
        "truncated": truncated,
        "node_count": len(nodes),
        "edge_count": len(edges_by_id),
        "edges_listed": len(edges),
        "seeds_dropped": seeds_dropped,
    }


def _evidence_lexicon_conflicts(rostore: RoStore, claim_id: str) -> dict[str, Any] | None:
    """Ruling L-C5's import-guarded hook, and nothing more.

    Lane e (E4) is what ADDS ``trialerror.lexicon.api.conflicts_for_claim`` --
    the disjoint-source sense conflict that belongs under WHAT ARGUES WITH IT.
    Until it lands, the module is absent and this returns ``None``, which the
    builder renders as the ``awaiting_migration`` reading the convention calls
    for: the region is OMITTED with a stated reason, never drawn as an empty
    box that reads "no term conflicts" when what is true is "nothing can
    answer that yet".

    ``sqlite3.OperationalError`` is caught beside ``ImportError`` for the
    middle state a two-step ruling creates -- lane e's module present on a
    store that has not run its migration -- exactly the shape
    :func:`_memory_conflict_candidate_items` already handles for the mining
    lane's own tables."""
    try:  # pragma: no cover - the module does not exist until lane e (E4)
        from trialerror.lexicon.api import conflicts_for_claim  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:  # pragma: no cover - same
        return {"status": "ok", "conflicts": conflicts_for_claim(rostore, claim_id)}
    except sqlite3.OperationalError:
        return None


def build_evidence_panel(
    rostore: RoStore,
    *,
    claim_id: str | None = None,
    anchor_id: str | None = None,
    chunk_id: str | None = None,
) -> dict[str, Any]:
    """The Evidence backing route -- lane C item A, spec section 1; the
    operator's 2026-09-05 walkthrough complaint ("Evidence page: no backing
    route yet"), answered.

    One claim, traced: WHAT IT STANDS ON (its anchors, with the two hash chips
    that say whether the ground has moved under them), WHAT ARGUES WITH IT
    (claim verdicts, and ``prov_edge`` reported honestly empty), the other
    claims standing on the same evidence, and the bounded KG neighbourhood
    around that evidence.

    Three entry points, resolved in this order (spec section 1.2):
    ``claim_id`` names the claim; ``anchor_id`` is the TRACE from a search
    result row (``citation.anchor.anchor_id``, which every result already
    carries); ``chunk_id`` is that same trace one level coarser. With none of
    them, the newest live claim -- the default :func:`build_dossier_panel`
    uses for its own rail.

    Every read here already exists and is read-only (spec section 1.1): no new
    table, no new column, no migration.

    The lexicon term-conflict region is deliberately NOT implemented (ruling
    L-C5): the hook is :func:`_evidence_lexicon_conflicts`, and while lane e's
    module is absent the region is omitted with its reason stated rather than
    drawn empty."""
    if not rostore.is_available("knowledge"):
        return {"status": "not_initialized", "message": "knowledge.db not found"}

    conn = rostore.knowledge
    index, index_total, index_truncated = _evidence_index(conn)
    claim_row, not_found = _evidence_resolve_claim(conn, claim_id=claim_id, anchor_id=anchor_id, chunk_id=chunk_id)

    panel: dict[str, Any] = {
        "status": "ok",
        "index": index,
        "index_total": index_total,
        "index_truncated": index_truncated,
        "active_claim_id": None,
        "claim": None,
        "anchors": [],
        "argues": None,
        "co_anchored_claims": [],
        "neighbourhood": None,
        "lineage": None,
    }
    if not_found is not None:
        panel["not_found"] = not_found
    if claim_row is None:
        return panel

    anchors, primary_fenced = _evidence_anchor_rows(conn, claim_row)
    anchor_ids = [a["anchor_id"] for a in anchors]
    context = _evidence_anchor_context(conn, anchor_ids)
    supersedes = [
        r["claim_id"]
        for r in conn.execute("SELECT claim_id FROM claim WHERE superseded_by = ?", (claim_row["claim_id"],)).fetchall()
    ]

    panel["active_claim_id"] = claim_row["claim_id"]
    panel["claim"] = {
        "claim_id": claim_row["claim_id"],
        "kind": claim_row["kind"],
        # Spec section 1.2: the claim BODY is fence-capped and then
        # untrusted-wrapped. The client strips the wrapper and never injects
        # it as HTML (evidence_render.js builds text nodes only).
        "text": untrusted_wrap(citation_quote(claim_row.get("text"), fenced=primary_fenced)),
        "fenced": primary_fenced,
        "confidence": claim_row.get("confidence"),
        "created_at": claim_row.get("created_at"),
        "valid_at": claim_row.get("valid_at"),
        "expired_at": claim_row.get("expired_at"),
        "invalid_at": claim_row.get("invalid_at"),
        "superseded_by": claim_row.get("superseded_by"),
        "created_by_launch": claim_row.get("created_by_launch"),
    }
    panel["anchors"] = anchors
    panel["argues"] = _evidence_argues(conn, claim_row["claim_id"])
    panel["co_anchored_claims"] = _evidence_co_anchored(
        conn, claim_id=claim_row["claim_id"], anchor_ids=anchor_ids, context=context
    )
    panel["neighbourhood"] = _evidence_neighbourhood(
        rostore, claim_row=claim_row, anchor_ids=anchor_ids, context=context
    )
    panel["lineage"] = {"superseded_by": claim_row.get("superseded_by"), "supersedes": supersedes}

    term_conflicts = _evidence_lexicon_conflicts(rostore, claim_row["claim_id"])
    if term_conflicts is not None:  # pragma: no cover - lane e (E4) lands the module
        panel["term_conflicts"] = term_conflicts
    else:
        panel["term_conflicts_omitted"] = {
            "reason": "awaiting_migration",
            "message": (
                "the per-claim term-sense conflict read (lexicon.api.conflicts_for_claim) is not in "
                "this program yet -- this region is omitted rather than drawn empty"
            ),
        }
    return panel


# ---------------------------------------------------------------------------
# lexicon panel
# ---------------------------------------------------------------------------
def build_lexicon_panel(rostore: RoStore) -> dict[str, Any]:
    """Honest v1 over what exists today -- REDESIGN R15, "the largest
    seam": no dedicated ``term``/``term_sense``/``term_sense_evidence``
    store exists. This reads ``knowledge.claim WHERE kind='definition'``
    (term-ish quote-grounded definitions) and ``knowledge.entity`` (with
    its ``aliases`` column, the closest thing to dedup today) plus draft
    ``merge_proposal`` rows as a possible-duplicate signal. Contradiction
    flags would come from ``knowledge.prov_edge WHERE role='contradicts'``
    -- that table has zero writers anywhere in this codebase, so this
    always returns empty today; documented in ``seam_note``, never silently
    treated as "no conflicts exist"."""
    if not rostore.is_available("knowledge"):
        return {"status": "not_initialized", "message": "knowledge.db not found"}

    conn = rostore.knowledge
    entities: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT entity_id, name, entity_type, aliases, summary, resolution, merge_group FROM entity ORDER BY name"
    ).fetchall():
        d = dict(r)
        d["relation_count"] = conn.execute(
            "SELECT COUNT(*) FROM relation WHERE (src_entity = ? OR dst_entity = ?) AND expired_at IS NULL",
            (d["entity_id"], d["entity_id"]),
        ).fetchone()[0]
        entities.append(d)

    definition_claims = [
        dict(r)
        for r in conn.execute(
            "SELECT c.claim_id, c.text, c.confidence, c.created_at, c.created_by_launch, "
            "qa.quote_text, qa.page_number, qa.doc_id "
            "FROM claim c JOIN quote_anchor qa ON c.anchor_id = qa.anchor_id "
            "WHERE c.kind = 'definition' AND c.expired_at IS NULL ORDER BY c.created_at DESC"
        ).fetchall()
    ]

    claim_kind_counts = _group_count(conn, "claim", "kind")
    merge_proposals_draft = [dict(r) for r in conn.execute("SELECT * FROM merge_proposal WHERE status = 'draft'").fetchall()]
    contradiction_edges = [dict(r) for r in conn.execute("SELECT * FROM prov_edge WHERE role = 'contradicts'").fetchall()]

    return {
        "status": "ok",
        "entities": entities,
        "definition_claims": definition_claims,
        "claim_kind_counts": claim_kind_counts,
        "merge_proposals_draft": merge_proposals_draft,
        "contradiction_edges": contradiction_edges,
        "seam_note": (
            "No dedicated term/term_sense/term_sense_evidence store exists yet "
            "(REDESIGN_V2_RATIONALE.md Section 5.3 item 7). Entities and definition-kind claims "
            "are read as a v1 proxy -- they give deduplication signal (entity.aliases, draft "
            "merge_proposal rows), not senses. contradiction_edges is always empty today: "
            "knowledge.prov_edge has zero writers anywhere in this codebase."
        ),
    }


# ---------------------------------------------------------------------------
# course panel
# ---------------------------------------------------------------------------
def build_course_panel(rostore: RoStore) -> dict[str, Any]:
    """Mission phases (derived from grouping ``criterion.phase``), the
    criterion ladder, and a drift log quoted verbatim from
    ``session.course_check`` (written at session close; a close refuses
    without one -- CLAUDE.md's "Boot protocol") plus any ``course_check``
    -type events, if a producer ever emits one (the v4 migration adds the
    ``criterion`` table only -- no event producer is required to exist for
    this builder to work). Per-dimension rollups are reported only where
    they are honestly computable from ``criterion.phase`` groupings; finer
    coverage/theory/validation splits (as sketched on ``Course.dc.html``)
    would need census/hole-register tables this build does not add."""
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    conn = rostore.ops
    if not _table_exists(conn, "criterion"):
        return {
            "status": "awaiting_migration",
            "message": (
                "ops.db has not been migrated to schema v4 yet (the criterion table doesn't "
                "exist) -- any write path that opens this program's store (e.g. a CLI command) "
                "picks up the migration automatically; trialerror dashboard never migrates a store "
                "itself (read-only, see trialerror/dashboard/store_ro.py)."
            ),
        }

    criteria: list[dict[str, Any]] = []
    phase_order: list[str] = []
    phase_stats: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT *, rowid AS _rowid FROM criterion ORDER BY _rowid ASC").fetchall():
        d = {k: v for k, v in dict(r).items() if k != "_rowid"}
        d["discharged_by_artifact_title"] = None
        if d.get("discharged_by_artifact"):
            art = conn.execute("SELECT title FROM artifact WHERE artifact_id = ?", (d["discharged_by_artifact"],)).fetchone()
            if art is not None:
                d["discharged_by_artifact_title"] = art["title"]
        criteria.append(d)

        phase = d["phase"]
        if phase not in phase_stats:
            phase_order.append(phase)
            phase_stats[phase] = {"total": 0, "open": 0, "blocked": 0, "discharged": 0}
        phase_stats[phase]["total"] += 1
        phase_stats[phase][d["state"]] += 1

    phases = [{"phase": p, **phase_stats[p]} for p in phase_order]

    drift_log: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT session_id, closed_ts, course_check FROM session WHERE course_check IS NOT NULL ORDER BY closed_ts DESC"
    ).fetchall():
        try:
            parsed = json.loads(r["course_check"])
        except (TypeError, ValueError):
            parsed = r["course_check"]
        drift_log.append({"source": "session_close", "ts": r["closed_ts"], "session_id": r["session_id"], "course_check": parsed})
    for r in conn.execute("SELECT * FROM event WHERE type = 'course_check' ORDER BY ts DESC").fetchall():
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            payload = r["payload"]
        drift_log.append({"source": "event", "ts": r["ts"], "session_id": r["session_id"], "course_check": payload})
    drift_log.sort(key=lambda d: d["ts"] or "", reverse=True)

    return {"status": "ok", "criteria": criteria, "phases": phases, "drift_log": drift_log}


# ---------------------------------------------------------------------------
# since_you_left panel
# ---------------------------------------------------------------------------
_INGEST_JOB_KINDS = ("ocr", "embed", "index", "extract", "ingest_batch", "normalize", "chunk")


def _default_since(rostore: RoStore) -> tuple[str, str]:
    """``(since, source)`` -- the last CLOSED session's ``closed_ts`` if one
    exists, else 24 hours before now. Never crashes on an empty/absent
    session table (falls through to the 24h fallback)."""
    if rostore.is_available("ops"):
        row = rostore.ops.execute(
            "SELECT closed_ts FROM session WHERE closed_ts IS NOT NULL ORDER BY closed_ts DESC LIMIT 1"
        ).fetchone()
        if row is not None and row["closed_ts"]:
            return row["closed_ts"], "last_session_close"
    return _iso(now_dt() - timedelta(hours=24)), "24h_fallback"


def _room_event_summary(event_type: str, payload: dict[str, Any]) -> str:
    room_id = payload.get("room_id", "?")
    if event_type == "room_dp_scored":
        pct = payload.get("agreement_pct")
        dp = payload.get("dp_id", "?")
        verb = "converged" if payload.get("converged") else "scored"
        return f"Room {room_id} DP {dp} {verb} at {pct}%."
    if event_type == "room_converged":
        return f"Room {room_id} converged on every discussion point."
    if event_type == "room_frozen":
        return f"Room {room_id} was frozen: {payload.get('reason') or '(no reason recorded)'}"
    if event_type == "room_created":
        return f"Room {room_id} opened: {payload.get('topic', '')}"
    if event_type == "room_deliverable_registered":
        return f"Room {room_id} registered its deliverable {payload.get('artifact_id', '?')}."
    return f"Room {room_id}: {event_type}"


def build_since_you_left_panel(rostore: RoStore, *, since: str | None = None) -> dict[str, Any]:
    """Delta builder (REDESIGN Phase 2 "SINCE YOU LEFT"): everything that
    happened after ``since`` (default: the last session close, else 24h),
    newest first, as typed items with a plain factual one-line ``summary``
    -- template sentences built from row data, never an LLM call."""
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

    since_source = "given"
    if since is None:
        since, since_source = _default_since(rostore)

    conn = rostore.ops
    items: list[dict[str, Any]] = []

    for r in conn.execute("SELECT * FROM feed_post WHERE ts > ? ORDER BY ts ASC", (since,)).fetchall():
        d = dict(r)
        items.append(
            {
                "kind": "feed_post",
                "ts": d["ts"],
                "summary": f"{d['author']} posted in thread {d['thread_id']}: \"{_truncate(d['body'])}\"",
                "ref": {"post_id": d["post_id"], "thread_id": d["thread_id"]},
            }
        )

    for r in conn.execute("SELECT * FROM gate_transition WHERE ts > ? ORDER BY ts ASC", (since,)).fetchall():
        d = dict(r)
        items.append(
            {
                "kind": "gate_transition",
                "ts": d["ts"],
                "summary": f"Gate {d['gate_id']} moved {d['from_state']} -> {d['to_state']} (by {d['by_launch']}).",
                "ref": {"gate_id": d["gate_id"]},
            }
        )

    room_type_placeholders = ",".join("?" for _ in _ROOM_EVENT_TYPES)
    for r in conn.execute(
        f"SELECT * FROM event WHERE type IN ({room_type_placeholders}) AND ts > ? ORDER BY ts ASC",
        (*_ROOM_EVENT_TYPES, since),
    ).fetchall():
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            payload = {}
        items.append(
            {
                "kind": r["type"],
                "ts": r["ts"],
                "summary": _room_event_summary(r["type"], payload),
                "ref": {"room_id": payload.get("room_id")},
            }
        )

    for r in conn.execute("SELECT * FROM artifact WHERE registered_ts > ? ORDER BY registered_ts ASC", (since,)).fetchall():
        d = dict(r)
        items.append(
            {
                "kind": "artifact_registered",
                "ts": d["registered_ts"],
                "summary": f"{d['artifact_id']} ({d['type']}) registered: {d['title']}",
                "ref": {"artifact_id": d["artifact_id"]},
            }
        )

    if rostore.is_available("jobs"):
        job_kind_placeholders = ",".join("?" for _ in _INGEST_JOB_KINDS)
        for r in rostore.jobs.execute(
            f"SELECT * FROM job WHERE state = 'complete' AND kind IN ({job_kind_placeholders}) "
            "AND settled_ts IS NOT NULL AND settled_ts > ? ORDER BY settled_ts ASC",
            (*_INGEST_JOB_KINDS, since),
        ).fetchall():
            d = dict(r)
            items.append(
                {
                    "kind": "ingest_complete",
                    "ts": d["settled_ts"],
                    "summary": f"Job {d['job_id']} ({d['kind']}) completed.",
                    "ref": {"job_id": d["job_id"]},
                }
            )

    items.sort(key=lambda i: i["ts"] or "", reverse=True)

    return {"status": "ok", "since": since, "since_source": since_source, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# search (dedicated endpoint, NOT a PANEL_BUILDERS entry -- see run_search's
# own docstring for why: every builder above is a pure ``RoStore -> dict``
# with no required argument, so it can appear in build_all_panels' one
# aggregate fetch; a search has no meaningful no-argument default beyond
# "empty query, empty results", which run_search already degrades to
# gracefully, but it is wired as its own HTTP route in serve.py rather than
# folded into the aggregate endpoint every page load would otherwise pay for)
# ---------------------------------------------------------------------------
#: Hard cap on ``k`` regardless of what a caller (the
#: ``/dashboard/api/search`` HTTP route) requests -- this build's brief:
#: "Search must be read-only and fast; cap k at 50".
MAX_SEARCH_K = 50


def run_search(
    rostore: RoStore,
    *,
    query: str,
    k: int | None = None,
    mode: str = "auto",
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wire ``trialerror.retrieve.engine.search`` (built, tested, never surfaced
    per REDESIGN R1) over the RoStore's knowledge db. Read-only: ``search``
    itself never writes except its own "unfenced bypass" audit-log path,
    which only fires when ``unfenced=True`` -- never passed here, so this
    function issues no writes even though ``RoStore``'s connections are
    already ``mode=ro`` and would refuse one at the driver level regardless.
    ``k`` is clamped to :data:`MAX_SEARCH_K`; an invalid ``mode`` is
    reported as a clean ``"invalid_mode"`` status rather than letting
    :class:`~trialerror.retrieve.errors.InvalidSearchModeError` escape as a 500.
    """
    if not rostore.is_available("knowledge"):
        return {"status": "not_initialized", "message": "knowledge.db not found"}

    resolved_k = retrieve_engine.DEFAULT_K if k is None else max(0, min(int(k), MAX_SEARCH_K))
    try:
        result = retrieve_engine.search(rostore, query=query, k=resolved_k, mode=mode, filters=filters)
    except InvalidSearchModeError as exc:
        return {"status": "invalid_mode", "message": str(exc)}
    result["status"] = "ok"
    return result


# ---------------------------------------------------------------------------
# doctor panel
# ---------------------------------------------------------------------------
def build_doctor_panel(doctor_state: dict[str, Any] | None) -> dict[str, Any]:
    """Reports the LAST on-demand doctor run -- ``doctor_state`` is whatever
    ``trialerror.dashboard.doctor_run.read_doctor_state`` returned (``None`` if
    doctor has never been run this session/from this dashboard). This
    function does not itself invoke doctor; see
    ``trialerror.dashboard.doctor_run`` for why that is a distinct action."""
    if doctor_state is None:
        return {"status": "never_run", "message": "doctor has not been run from this dashboard yet"}
    return {"status": "ok", "last_run": doctor_state}


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
#: panel name -> builder taking (rostore) -- doctor is excluded (it needs
#: the sidecar state dict, not an RoStore); serve.py and export.py each
#: wire it up separately alongside this map.
PANEL_BUILDERS = {
    "session": build_session_panel,
    "budget": build_budget_panel,
    "jobs": build_jobs_panel,
    "gates": build_gates_panel,
    "corpus": build_corpus_panel,
    "feed": build_feed_panel,
    "rooms": build_rooms_panel,
    "determinations": build_determinations_panel,
    "dossier": build_dossier_panel,
    "evidence": build_evidence_panel,
    "lexicon": build_lexicon_panel,
    "course": build_course_panel,
    "since_you_left": build_since_you_left_panel,
}


def isolated_panel(name: str, builder: Any, rostore: RoStore, **kwargs: Any) -> dict[str, Any]:
    """One builder call, fenced (M-LU-2).

    A builder that raises is that panel's own error and nothing else's:
    the caller gets ``{"status": "error", "message": "<ExcType>: <msg>",
    "panel": <name>}`` -- the same "visible, not refused" shape every other
    non-``ok`` panel status already uses -- and the traceback goes to
    stderr, where the serve log keeps it.

    Before this, one raising builder took ``/dashboard/api/all`` down with a
    500 while the same panel's own route still answered 200 (live case: a
    program whose ``platform.db`` does not exist, ``build_session_panel``
    reaching through ``session_status`` into a ``None`` connection). A page
    whose only bulk-load route 500s renders nothing at all -- and the
    client's retry loop then spins against a crash that will never clear."""
    try:
        return builder(rostore, **kwargs)
    except Exception as exc:  # noqa: BLE001 - one panel's failure is not the page's
        traceback.print_exc()
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}", "panel": name}


def build_all_panels(rostore: RoStore, *, doctor_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every panel in one dict -- what ``trialerror dashboard export`` embeds
    into its static snapshot, and what a fresh live-page load can fetch in
    one request rather than six. Each builder is fenced by
    :func:`isolated_panel`, so this dict always has every key."""
    panels = {name: isolated_panel(name, builder, rostore) for name, builder in PANEL_BUILDERS.items()}
    panels["doctor"] = build_doctor_panel(doctor_state)
    return panels
