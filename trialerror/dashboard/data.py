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
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from trialerror.artifacts.registry import list_artifacts
from trialerror.budget.pools import budget_status, list_pools
from trialerror.dashboard.store_ro import RoStore
from trialerror.events.api import list_threads, read_inbox
from trialerror.ingest.extract import EXTRACT_REGISTER_KEY, list_pending
from trialerror.ingest.requests import TRANSITIONS as REQUEST_TRANSITIONS
from trialerror.jobs.ledger import list_jobs
from trialerror.memory.merge import list_conflicts as list_memory_conflicts
from trialerror.retrieve import engine as retrieve_engine
from trialerror.retrieve.errors import InvalidSearchModeError
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
    "build_lexicon_panel",
    "build_course_panel",
    "build_since_you_left_panel",
    "run_search",
    "MAX_SEARCH_K",
    "build_all_panels",
    "PANEL_BUILDERS",
]


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
def build_session_panel(rostore: RoStore) -> dict[str, Any]:
    if not rostore.is_available("ops"):
        return {"status": "not_initialized", "message": "ops.db not found"}

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
                "queue": session_row.get("queue"),
            },
            "close_readiness": status.get("readiness"),
            "unread_inbox_count": status.get("unread_inbox_count"),
            "hook_alive_count": status.get("hook_alive_count"),
            "active_jobs_count": len(status.get("active_jobs") or []),
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

    return {
        "status": "ok",
        "state_counts": state_counts,
        "live_jobs": live_jobs,
        "stale_leases": stale_leases,
        "recent_jobs": jobs[:50],
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
    pending_edits = [dict(r) for r in pending_edit_rows]

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


def _load_current_translation(conn: sqlite3.Connection, post_id: str) -> dict[str, Any] | None:
    """The one ``status='current'`` translation row for ``post_id``, or
    ``None`` -- either because the ``feed_post_translation`` table doesn't
    exist yet on this program (pre-v4 ``ops.db``) or because this post has
    never been translated. Never raises on a missing table (see
    :func:`_table_exists`)."""
    if not _table_exists(conn, "feed_post_translation"):
        return None
    row = conn.execute(
        "SELECT * FROM feed_post_translation WHERE post_id = ? AND status = 'current' "
        "ORDER BY created_ts DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def build_feed_panel(rostore: RoStore, *, thread_id: str | None = None) -> dict[str, Any]:
    """Threads, one thread's full-text post stream, unread operator
    directives, and a per-post ``translation`` slot reading the AISPEAK
    sidecar table (``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md``) IF it
    exists on this program -- ``null`` otherwise (this build creates the
    TABLE seam only, never the translator itself, per that design doc's own
    step ordering).

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
    if active_thread_id is not None:
        rows = conn.execute(
            "SELECT *, rowid AS _rowid FROM feed_post WHERE thread_id = ? ORDER BY ts ASC, _rowid ASC",
            (active_thread_id,),
        ).fetchall()
        for r in rows:
            d = {k: v for k, v in dict(r).items() if k != "_rowid"}
            d["kind"] = _derive_post_kind(d["author"])
            translation_row = _load_current_translation(conn, d["post_id"])
            d["translation"] = (
                {
                    "translation_id": translation_row["translation_id"],
                    "body": translation_row["body"],
                    "style_mode": translation_row["style_mode"],
                    "translator_version": translation_row["translator_version"],
                    "faithfulness_score": translation_row["faithfulness_score"],
                    "created_ts": translation_row["created_ts"],
                }
                if translation_row is not None
                else None
            )
            posts.append(d)

    unread_directives = read_inbox(rostore, mark_read=False)

    return {
        "status": "ok",
        "threads": threads,
        "active_thread_id": active_thread_id,
        "posts": posts,
        "unread_directives": unread_directives,
        "translator_table_available": _table_exists(conn, "feed_post_translation"),
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
    rows = conn_ops.execute(
        "SELECT * FROM prereg WHERE status = 'committed' ORDER BY committed_ts"
    ).fetchall()
    return [
        {
            "kind": "prereg_reveal",
            "id": r["prereg_id"],
            "title": r["title"],
            "committed_ts": r["committed_ts"],
            "blocking": False,
            "consequence": (
                "Revealing unseals the committed procedure/params hash so the pre-registered "
                "result can be checked against them."
            ),
        }
        for r in rows
    ]


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
    groups = list_memory_conflicts(rostore)
    return [
        {
            "kind": "memory_conflict",
            "id": g["group_id"],
            "key": g["key"],
            "version_count": len(g["versions"]),
            "blocking": False,
            "consequence": f"Resolving keeps one version of {g['key']!r} active and marks the other superseded.",
        }
        for g in groups
    ]


def build_determinations_panel(rostore: RoStore) -> dict[str, Any]:
    """The one determination queue -- REDESIGN S20 (``build_review_panel``
    unioning three existing reads, no new tables) plus S21 (a
    ``consequence`` field per item, "what happens if you verify") and S26
    (memory-merge conflicts, "queue kind, not drawn"). Six kinds today:
    gate edits awaiting verification, KG merge proposals, acquisition
    requests, pre-registration reveals, room freeze-and-escalate events,
    and memory-sync conflicts."""
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
    "lexicon": build_lexicon_panel,
    "course": build_course_panel,
    "since_you_left": build_since_you_left_panel,
}


def build_all_panels(rostore: RoStore, *, doctor_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every panel in one dict -- what ``trialerror dashboard export`` embeds
    into its static snapshot, and what a fresh live-page load can fetch in
    one request rather than six."""
    panels = {name: builder(rostore) for name, builder in PANEL_BUILDERS.items()}
    panels["doctor"] = build_doctor_panel(doctor_state)
    return panels
