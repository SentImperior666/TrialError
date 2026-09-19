"""Budget pools, bookings, reconciliation, calibration. Design Section 4.3
(platform.db DDL, binding rules) + Section 5.2 (``budget`` CLI group:
"book, reconcile, status, pools, snapshot-ingest, calibrate") + Section 5.1
(``trialerror-ops`` tools ``budget_status``/``book_launch``/``reconcile_launch``).

This module owns the BUSINESS LOGIC around ``platform.launch``/
``budget_pool``/``quota_snapshot``/``calibration``; the ATOMIC spawn-time
claim (PROVISIONAL -> RUNNING) is a separate, narrower concern that lives
in :mod:`trialerror.budget.gate` (review finding F2 - kept in its own module
because it is the one operation that must be a single conditional
``UPDATE``, not because the logic differs in kind).

TRIALERROR-DEV-NOTE (over-cap math): the design names the fields
(``cap_tokens``, ``spent_visible_tokens``, ``billed_multiplier``,
``soft_pct``, ``hard_pct``) but not the formula relating them. This module
reads them the only way that is internally consistent with their own
docstrings: ``spent_visible_tokens`` + currently-live (PROVISIONAL/RUNNING)
bookings' ``est_tokens``, all converted to real (billed) cost via
``billed_multiplier``, compared against ``cap_tokens * pct/100``.
``cap_tokens`` is therefore a REAL-usage ceiling; ``spent_visible_tokens``
is a raw visible-token counter (what ``reconcile_launch`` bookkeeps
directly); the multiplier bridges the two. Documented here since v0's
design text states the fields without their arithmetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trialerror.budget.errors import (
    LaunchNotOwnedError,
    ModelPolicyViolationError,
    NoOpenSessionError,
    UnknownAssignmentError,
    UnknownOverrideRulingError,
)
from trialerror.budget.policy import meets_minimum, required_class_for_purpose
from trialerror.stores import get, insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "DEFAULT_BOOKING_TTL_S",
    "ASSERTABLE_RECONCILE_SOURCES",
    "EVENT_RECONCILE_SOURCE",
    "USAGE_COLUMNS",
    "BookResult",
    "book_launch",
    "link_launch_to_assignments",
    "resolve_assignment_ids",
    "check_booking_preconditions",
    "heartbeat_launch",
    "reconcile_launch",
    "reconcile_launch_from_event",
    "latest_subagent_return",
    "tree_rollup",
    "usage_split_totals",
    "create_pool",
    "list_pools",
    "current_pool_row",
    "committed_visible_tokens",
    "evaluate_pool",
    "pool_report",
    "budget_status",
    "snapshot_ingest",
    "calibrate",
]

#: Design Section 4.3: ``booking_ttl_s INTEGER NOT NULL DEFAULT 3600``.
DEFAULT_BOOKING_TTL_S = 3600

_LIVE_STATES = ("PROVISIONAL", "RUNNING")

#: The ``reconcile_source`` values a CALLER may assert (design Section 4.3:
#: "caller-asserted label"). Each is a claim about where a number came from
#: that nothing can check, which is exactly why the fourth value is not here.
ASSERTABLE_RECONCILE_SOURCES = ("transcript", "estimate", "manual")

#: platform-v2's fourth ``reconcile_source``: the number came off a recorded
#: ``subagent_return`` event's own ``usage`` object. Set ONLY by
#: :func:`reconcile_launch_from_event`, which has read that event; every
#: other caller is refused, so this value is never an assertion.
EVENT_RECONCILE_SOURCE = "event"

#: ``launch`` column <- ``subagent_return`` payload ``usage`` key. platform-v2
#: (D-FB-13 (c)): nullable columns, filled only by
#: :func:`reconcile_launch_from_event`, left null by ``--actual-tokens``.
USAGE_COLUMNS: dict[str, str] = {
    "usage_input_tokens": "input_tokens",
    "usage_cache_creation_tokens": "cache_creation_input_tokens",
    "usage_cache_read_tokens": "cache_read_input_tokens",
    "usage_output_tokens": "output_tokens",
}


@dataclass
class BookResult:
    """The result of :func:`book_launch`. Never raised as an exception for
    the "can't afford it" outcomes (REFUSED/DEFERRED) - design Section 5.1
    cross-cutting rule: "structured errors, never exceptions". ``ok`` is
    True only for ``state == "PROVISIONAL"`` (a token a spawn can actually
    consume)."""

    ok: bool
    launch_id: str
    state: str  # PROVISIONAL | DEFERRED | REFUSED
    account_id: str
    reason: str | None = None
    defer_advisory: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "launch_id": self.launch_id,
            "state": self.state,
            "account_id": self.account_id,
            "reason": self.reason,
            "defer_advisory": self.defer_advisory,
            "details": self.details,
        }


def _require_open_session(store: Store, session_id: str) -> dict:
    """Review finding F13: "book_launch is not stated to require an open
    session ... Require an open session ... at booking." Also supplies F14
    ("account bound at session boot, read by book_launch") - the caller of
    :func:`book_launch` never states an ``account_id``; it is always read
    off this row."""
    session = get(store, "session", pk_column="session_id", pk_value=session_id)
    if session is None or session.get("status") != "open":
        raise NoOpenSessionError(
            f"session {session_id!r} is not OPEN in this program's ops.db "
            "(book_launch refuses unless the calling session is OPEN - design "
            "Section 4.3 / review F13)"
        )
    return session


def _check_model_policy(
    store: Store,
    *,
    purpose: str,
    model_class: str,
    policy: Mapping[str, str] | None,
    override_ruling_id: str | None,
) -> None:
    minimum = required_class_for_purpose(dict(policy) if policy else None, purpose)
    if meets_minimum(model_class, minimum):
        return
    if not override_ruling_id:
        raise ModelPolicyViolationError(
            f"purpose {purpose!r} requires model_class >= {minimum!r}, got {model_class!r} "
            "(no override_ruling_id supplied)"
        )
    ruling = get(store, "ruling", pk_column="ruling_id", pk_value=override_ruling_id)
    if ruling is None:
        raise UnknownOverrideRulingError(
            f"override_ruling_id {override_ruling_id!r} does not name an existing ops.ruling row"
        )


def check_booking_preconditions(
    store: Store,
    *,
    session_id: str,
    purpose: str,
    model_class: str,
    policy: Mapping[str, str] | None = None,
    override_ruling_id: str | None = None,
) -> dict:
    """Rungs 1 and 2 of :func:`book_launch`'s refusal ladder -- the session
    is OPEN, and the purpose's required model class is met -- returning the
    session row. Writes nothing.

    Lane FB-3 fix pass (V-2). ``book_launch`` still runs this itself, so
    nothing can book past it; it is public so that the two surfaces which
    gate a booking on something OUTSIDE the store (the stale plan-quota
    capture, item 8) can put their gate in the right place in the ladder.
    A booking made from a closed session, or with a malformed argument, was
    being reported as ``stale_quota_capture`` -- which sends an operator to
    the statusLine to fix a fault that is in their own command. The
    operator's command is judged first; the environment's reading second."""
    session = _require_open_session(store, session_id)
    _check_model_policy(
        store, purpose=purpose, model_class=model_class, policy=policy, override_ruling_id=override_ruling_id
    )
    return session


def current_pool_row(conn: Any, account_id: str, model_class: str) -> dict | None:
    """The pool for ``(account_id, model_class)`` with the latest
    ``period_start`` - v0's reading of "the current pool" (design Section 4.3
    doesn't specify pool-rollover mechanics; new-period pools are created
    explicitly via :func:`create_pool`, and this always picks the newest
    one on file).

    Takes a platform DB-API CONNECTION rather than a :class:`Store` so the
    doctor -- which opens platform.db read-only and has no Store at all --
    asks the same question through the same statement that ``book_launch``
    targets its booking with. D-FB-14 (1): a check that decided "current" its
    own way would judge a pool nothing can book against."""
    row = conn.execute(
        "SELECT * FROM budget_pool WHERE account_id = ? AND model_class = ? "
        "ORDER BY period_start DESC LIMIT 1",
        (account_id, model_class),
    ).fetchone()
    return dict(row) if row is not None else None


def _current_pool(store: Store, account_id: str, model_class: str) -> dict | None:
    return current_pool_row(store.platform, account_id, model_class)


def _launch_has_pool_id(conn: Any) -> bool:
    """Does this platform.db's ``launch`` table carry ``pool_id`` yet?

    Fix pass V-4. Asked of the table rather than of ``PRAGMA user_version``:
    the column is the thing the statement below needs, and a read-only
    caller on a v1 file cannot migrate it. One PRAGMA per call is the same
    order of cost as the query it guards."""
    try:
        return any(row[1] == "pool_id" for row in conn.execute("PRAGMA table_info(launch)").fetchall())
    except Exception:  # noqa: BLE001 - no table_info means no usable launch table either
        return False


def committed_visible_tokens(
    conn: Any, *, account_id: str, model_class: str, pool_id: str | None, is_current: bool
) -> dict[str, Any]:
    """The live (PROVISIONAL/RUNNING) commitment held against one pool, with
    the per-state breakdown.

    **Which launches count.** Since platform-v2 a booking records the
    ``pool_id`` it was judged against, so a superseded pool's commitment is
    exactly its own rows -- no inference from (account_id, model_class),
    which silently swept a previous period's live bookings into the new
    period's number the moment a pool rolled over. Rows booked BEFORE
    platform-v2 carry no ``pool_id`` at all; they count toward the CURRENT
    pool, which is where they were charged and where the pre-v2 arithmetic
    put them. That is the whole of "exact from now on": old rows keep their
    old (only possible) reading, new rows are exact.

    **The breakdown** (custodian observation (a), 2026-09-11/15). The total
    is the same number ``book_launch``'s cap check has always used, computed
    from the booking rows themselves -- no hook, no state the spawn gate has
    to advance. ``by_state`` is what makes that visible on the surfaces: a
    commitment that is entirely PROVISIONAL is a set of bookings nothing has
    spawned against yet, and reading one number could not tell that from a
    set of running agents.

    TRIALERROR-DEV-NOTE (hookless PROVISIONAL bookings, lane FB-3 item 7 --
    the recorded cause). The observation was that a PROVISIONAL booking made
    from a session without hooks (2026-09-11, launch 0604; 2026-09-15, 0.5M)
    did not appear in ``committed_visible_tokens``. The brief's first
    hypothesis -- that ``committed`` was computed from a state only the hook
    advances -- is NOT what the code did: the sum has always been over
    ``state IN ('PROVISIONAL','RUNNING')`` on the booking rows, so a booking
    that never reaches RUNNING because no spawn gate fired is counted from
    the moment it is written. That is pinned by a regression test
    (``tests/test_budget_pool_truth.py``). Three code-level readings DO
    produce the reported symptom, and all three are addressed here or
    reported rather than left to be rediscovered:

    1. **Cross-period leakage, now fixed.** Before ``pool_id``, a live
       booking was attributed by (account_id, model_class) alone, so the
       moment a weekly pool rolled over, last period's live bookings were
       counted against the NEW pool and the old pool's own commitment could
       not be read at all. A reader comparing a booking against the pool it
       was actually judged against saw numbers that did not add up.
    2. **``spent`` and ``committed`` are different numbers.** Only
       ``reconcile`` moves ``spent_visible_tokens`` (and only a screenshot or
       the statusline feed moves the plan meter), so a PROVISIONAL booking is
       invisible in BOTH by design -- it is a commitment, not a spend. A
       reading taken off the pool's ``spent`` or off ``budget quota`` will
       never show it. ``by_state`` and ``budget check``'s commitment line
       exist so the difference is stated rather than inferred.
    3. **The platform root is per-process.** Bookings live in
       ``platform.db``, resolved from ``--platform-root`` /
       ``TRIALERROR_PLATFORM_ROOT`` / ``~/.trialerror`` at call time, so a
       session whose environment was not set up the way the launcher sets it
       up books into a different file, and a ``budget status`` reading the
       other one cannot see it. Nothing in this module can detect that from
       inside; it is named here because it is the remaining way to get this
       symptom with everything above correct."""
    if not _launch_has_pool_id(conn):
        # Fix pass V-4: a platform.db still on v1 has no `pool_id` column.
        # Both read-only callers -- the `budget_pool_overspend` doctor check
        # and the dashboard's budget card -- open the file read-only and so
        # cannot migrate it themselves, and the first doctor run after a
        # deploy (the boot ritual runs doctor first) happens before anything
        # has opened a writable Store. Raising `no such column: pool_id`
        # there is an error about the reader, not about the budget. Fall
        # back to the pre-v2 reading, which is the only one those rows can
        # support, and say so rather than pretending the number is per-pool.
        rows = conn.execute(
            "SELECT state, COALESCE(SUM(est_tokens), 0) AS est FROM launch "
            "WHERE state IN ('PROVISIONAL','RUNNING') AND account_id = ? AND model_class = ? "
            "GROUP BY state",
            (account_id, model_class),
        ).fetchall()
        by_state = {r["state"]: int(r["est"] or 0) for r in rows}
        return {
            "total": sum(by_state.values()) if is_current else 0,
            "by_state": by_state if is_current else {},
            "attribution": "account+class (platform.db is on v1: open a store once to migrate it)",
        }
    if is_current:
        where = "(pool_id = ? OR (pool_id IS NULL AND account_id = ? AND model_class = ?))"
        params: tuple[Any, ...] = (pool_id, account_id, model_class)
    else:
        where = "pool_id = ?"
        params = (pool_id,)
    rows = conn.execute(
        f"SELECT state, COALESCE(SUM(est_tokens), 0) AS est FROM launch "
        f"WHERE state IN ('PROVISIONAL','RUNNING') AND {where} GROUP BY state",
        params,
    ).fetchall()
    by_state = {r["state"]: int(r["est"] or 0) for r in rows}
    return {"total": sum(by_state.values()), "by_state": by_state, "attribution": "pool_id"}


def _committed_visible_tokens(store: Store, account_id: str, model_class: str) -> int:
    """The current pool's live commitment, as :func:`book_launch`'s cap check
    reads it."""
    pool = _current_pool(store, account_id, model_class)
    return committed_visible_tokens(
        store.platform,
        account_id=account_id,
        model_class=model_class,
        pool_id=pool["pool_id"] if pool else None,
        is_current=True,
    )["total"]


def _projected_billed_tokens(pool: Mapping[str, Any], committed_visible: int, new_est: int) -> float:
    return (float(pool["spent_visible_tokens"] or 0) + committed_visible + new_est) * float(
        pool["billed_multiplier"]
    )


def evaluate_pool(pool: Mapping[str, Any], committed: Mapping[str, Any], *, judged: bool) -> dict[str, Any]:
    """One pool's numbers: projected billed spend against its own soft/hard
    caps, plus the headroom conversions. THE one place this arithmetic lives
    -- ``budget status``, ``budget pools`` and the ``budget_pool_overspend``
    doctor check all call it, so a named offender can be checked from the
    CLI and get the same numbers back (D-FB-14 (2)).

    ``judged=False`` is for a SUPERSEDED pool: every number is still
    reported, because a frozen ``spent_visible_tokens`` is exactly what an
    operator reconstructing a period wants, but ``over_soft``/``over_hard``
    come back ``None`` rather than ``False``. ``book_launch`` only ever
    targets the current pool, so a superseded pool cannot move and a verdict
    about it would be a verdict about a number nobody can change."""
    multiplier = float(pool["billed_multiplier"]) or 1.0
    projected = _projected_billed_tokens(pool, int(committed["total"]), 0)
    hard_cap = float(pool["cap_tokens"]) * float(pool["hard_pct"]) / 100.0
    soft_cap = float(pool["cap_tokens"]) * float(pool["soft_pct"]) / 100.0
    entry: dict[str, Any] = {
        "pool_id": pool["pool_id"],
        "account_id": pool["account_id"],
        "model_class": pool["model_class"],
        "period": pool["period"],
        "period_start": pool["period_start"],
        "cap_tokens": pool["cap_tokens"],
        # Fix pass V-8: the pool's own stored percentages and its last write,
        # which `list_pools` used to return and `budget pools` stopped
        # printing when it moved to this function. soft_cap/hard_cap make the
        # percentages recoverable by division, which is not the same as
        # showing what the row says.
        "soft_pct": pool["soft_pct"],
        "hard_pct": pool["hard_pct"],
        "updated_ts": pool["updated_ts"],
        "spent_visible_tokens": pool["spent_visible_tokens"],
        "committed_visible_tokens": int(committed["total"]),
        "committed_by_state": dict(committed["by_state"]),
        # Fix pass V-4: how the commitment above was attributed -- `pool_id`
        # (exact, platform-v2) or the pre-v2 account+class sum, which is what
        # a read-only reader on an unmigrated store can still answer.
        "committed_attribution": committed.get("attribution", "pool_id"),
        "billed_multiplier": pool["billed_multiplier"],
        "projected_billed_tokens": projected,
        "hard_cap": hard_cap,
        "soft_cap": soft_cap,
        "headroom_tokens": max(hard_cap - projected, 0.0),
        "standing": "current" if judged else "superseded",
        "judged": judged,
        "over_soft": (projected > soft_cap) if judged else None,
        "over_hard": (projected > hard_cap) if judged else None,
        # Lane FB-1 item F7. Every number above is in BILLED tokens --
        # the plan meter's unit. What a caller about to book is holding
        # is an estimate in VISIBLE tokens (what the host reports for a
        # launch), and dividing one by the other in your head, against
        # the wrong cap, is how a session walks through its own soft
        # line. So: the headroom to the limit that actually binds next,
        # already converted into the unit the booking is written in.
        "visible_headroom_to_soft": max(soft_cap - projected, 0.0) / multiplier,
        "binding_limit": "hard" if projected > soft_cap else "soft",
    }
    entry["visible_headroom_to_binding_limit"] = (
        max(hard_cap - projected, 0.0) / multiplier
        if entry["binding_limit"] == "hard"
        else entry["visible_headroom_to_soft"]
    )
    return entry


def pool_report(conn: Any, *, account_id: str | None = None, model_class: str | None = None) -> list[dict[str, Any]]:
    """Every pool on file (optionally filtered), each evaluated by
    :func:`evaluate_pool` and labelled ``current`` or ``superseded``.

    D-FB-14. The current pool per (account_id, model_class) is decided by
    :func:`current_pool_row` -- the very statement ``book_launch`` targets --
    so nothing that reads this can judge a pool a booking could not land in.
    Takes a platform CONNECTION, so the doctor's read-only handle and a
    Store's writable one produce the same list."""
    sql = "SELECT * FROM budget_pool"
    clauses: list[str] = []
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if model_class:
        clauses.append("model_class = ?")
        params.append(model_class)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY account_id, model_class, period_start DESC"
    pools = [dict(r) for r in conn.execute(sql, params).fetchall()]

    current_ids: set[str] = set()
    for key in {(p["account_id"], p["model_class"]) for p in pools}:
        current = current_pool_row(conn, key[0], key[1])
        if current is not None:
            current_ids.add(current["pool_id"])

    out: list[dict[str, Any]] = []
    for pool in pools:
        judged = pool["pool_id"] in current_ids
        committed = committed_visible_tokens(
            conn,
            account_id=pool["account_id"],
            model_class=pool["model_class"],
            pool_id=pool["pool_id"],
            is_current=judged,
        )
        out.append(evaluate_pool(pool, committed, judged=judged))
    return out


def resolve_assignment_ids(store: Store, assign_ids: Sequence[str] | None) -> list[str]:
    """Validate ``assign_ids`` against ``lens_assignment`` and return them.

    Reads nothing else and writes nothing, so a caller can run it BEFORE it
    creates anything (fix pass B-1: :func:`book_launch` used to insert the
    ``launch`` row first and validate second, so one mistyped ``--assign-id``
    left a PROVISIONAL launch holding pool headroom that then refused
    ``session close`` with ``dangling_launches`` -- and the caller never saw
    the launch id to reconcile it with, because the refusal is an exception,
    not a :class:`BookResult`). A refusal has to be a refusal.

    Refuses the whole set if any ``assign_id`` names no row: a link that
    silently covered two of three slices would put the third outside the
    launch's own scope, and "outside the slice" is what the citation audit
    reports as a crossed barrier. A bare string is refused rather than
    iterated into one id per character.
    """
    if assign_ids is None:
        return []
    if isinstance(assign_ids, (str, bytes)):
        raise UnknownAssignmentError(
            "book_launch: assign_ids must be a sequence of assign ids, not a single string -- "
            f"{assign_ids!r} would read as one id per character. Pass a list (CLI: repeat "
            "`--assign-id`)"
        )
    ids = [str(a) for a in assign_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    found = {
        row["assign_id"]
        for row in store.ops.execute(
            f"SELECT assign_id FROM lens_assignment WHERE assign_id IN ({placeholders})", ids
        ).fetchall()
    }
    missing = [a for a in ids if a not in found]
    if missing:
        raise UnknownAssignmentError(
            f"book_launch: assign_id(s) {missing!r} name no row in lens_assignment. A booking linked to "
            "assignments that do not exist declares a slice nothing can resolve -- check `trialerror lens "
            "export --round-id <round>` for this round's assign_ids"
        )
    return ids


def link_launch_to_assignments(store: Store, *, launch_id: str, assign_ids: Sequence[str]) -> list[str]:
    """Record ``launch_id`` as the lens launch of each named assignment row.

    Validates through :func:`resolve_assignment_ids` first, so a direct
    caller gets the same refusal :func:`book_launch` takes before it writes
    anything."""
    ids = resolve_assignment_ids(store, assign_ids)
    if not ids:
        return []
    for assign_id in ids:
        update(
            store, "lens_assignment", pk_column="assign_id", pk_value=assign_id,
            changes={"lens_launch_id": launch_id},
        )
    return ids


def book_launch(
    store: Store,
    *,
    session_id: str,
    program_id: str,
    agent_kind: str,
    model_class: str,
    model: str,
    purpose: str,
    est_tokens: int,
    booking_ttl_s: int = DEFAULT_BOOKING_TTL_S,
    parent_launch: str | None = None,
    workpackage: str | None = None,
    attrs: Mapping[str, Any] | None = None,
    assign_ids: Sequence[str] | None = None,
    policy: Mapping[str, str] | None = None,
    override_ruling_id: str | None = None,
    now_ts: str | None = None,
) -> BookResult:
    """Create a booking. Design Section 5.2: "book returns launch_id token
    for the spawn gate."

    Refusal ladder (each one either raises a :mod:`trialerror.budget.errors`
    exception for a structural problem, or returns a non-``PROVISIONAL``
    :class:`BookResult` for an "affordability" outcome):

    1. session not OPEN -> :class:`NoOpenSessionError` (F13).
    2. purpose's policy-required model class not met, no/bad override ->
       :class:`ModelPolicyViolationError` / :class:`UnknownOverrideRulingError`.
    3. projected spend would cross the pool's ``hard_pct`` cap ->
       ``state="DEFERRED"`` (purpose requires top-tier AND caller correctly
       requested ``model_class="top"`` - "idle beats shallow", design
       Section 5.4) or ``state="REFUSED"`` (every other over-cap case) - 
       "over-cap book refused" acceptance criterion, either way ``ok=False``
       and the returned token is NOT a spawnable (PROVISIONAL) one.
    4. otherwise -> ``state="PROVISIONAL"``, ``ok=True``, spawnable.

    ``assign_ids`` links this booking to the ``lens_assignment`` rows it was
    booked for (lane FB-4 item 5), by writing ``lens_assignment.lens_launch_id``.
    ``lens_assignment.launch_id`` is the launch that WROTE the row -- the
    orchestrator running ``lens assign`` -- and until this link existed
    nothing recorded which assignment rows the LENS's own launch covered:
    the per-launch retrieval scope and ``lens_citations_within_slice`` both
    resolved a slice from ``launch.attrs``, so a booking made without those
    attrs read as "not a lens launch" and the barrier was off for exactly
    the launches it exists for. An ``assign_id`` naming no row raises
    :class:`UnknownAssignmentError` rather than linking nothing -- raised
    BEFORE the ``launch`` row is written (fix pass B-1), so a refused
    booking leaves no PROVISIONAL row holding pool headroom -- and the
    link is written only for a booking that was actually created
    (PROVISIONAL): a REFUSED booking never runs, and a slice pointing at one
    would claim it did.
    """
    session = check_booking_preconditions(
        store,
        session_id=session_id,
        purpose=purpose,
        model_class=model_class,
        policy=policy,
        override_ruling_id=override_ruling_id,
    )
    account_id = session["account_id"]

    # Rung 2c (fix pass B-1): the assign ids are resolved before anything is
    # created. Validating them after the insert made a refusal a booking.
    resolved_assign_ids = resolve_assignment_ids(store, assign_ids)

    ts = now_ts or now()
    launch_id = new_id("LNCH")

    pool = _current_pool(store, account_id, model_class)
    defer_advisory = False
    state = "PROVISIONAL"
    reason: str | None = None

    if pool is not None:
        committed = _committed_visible_tokens(store, account_id, model_class)
        projected = _projected_billed_tokens(pool, committed, est_tokens)
        hard_cap = float(pool["cap_tokens"]) * float(pool["hard_pct"]) / 100.0
        soft_cap = float(pool["cap_tokens"]) * float(pool["soft_pct"]) / 100.0
        if projected > hard_cap:
            required = required_class_for_purpose(dict(policy) if policy else None, purpose)
            if required == "top" and model_class == "top":
                state = "DEFERRED"
                reason = "pool cannot afford top-tier for this top-tier-required purpose"
            else:
                state = "REFUSED"
                reason = "projected spend would exceed the pool's hard cap"
        elif projected > soft_cap:
            defer_advisory = True

    attrs_dict: dict[str, Any] = dict(attrs) if attrs else {}
    if override_ruling_id:
        attrs_dict["override_ruling_id"] = override_ruling_id

    row = {
        "launch_id": launch_id,
        "account_id": account_id,
        "program_id": program_id,
        "session_id": session_id,
        "parent_launch": parent_launch,
        "agent_kind": agent_kind,
        "model_class": model_class,
        "model": model,
        "purpose": purpose,
        "est_tokens": est_tokens,
        "booked_ts": ts,
        "booking_ttl_s": booking_ttl_s,
        "state": state,
        "workpackage": workpackage,
        "attrs": json.dumps(attrs_dict, ensure_ascii=False) if attrs_dict else None,
        # D-FB-14 (1), platform-v2: the pool this booking was JUDGED against,
        # written whether or not it was allowed through -- a REFUSED booking
        # is evidence about a particular pool's cap, and losing which pool
        # would make it evidence about nothing. Null only when the account
        # has no pool for this class at all, in which case nothing judged it.
        "pool_id": pool["pool_id"] if pool is not None else None,
    }
    insert(store, "launch", row)
    if resolved_assign_ids and state == "PROVISIONAL":
        link_launch_to_assignments(store, launch_id=launch_id, assign_ids=resolved_assign_ids)

    return BookResult(
        ok=(state == "PROVISIONAL"),
        launch_id=launch_id,
        state=state,
        account_id=account_id,
        reason=reason,
        defer_advisory=defer_advisory,
        details={"pool_configured": pool is not None},
    )


def heartbeat_launch(
    store: Store,
    *,
    launch_id: str,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Push a live booking's ``booked_ts`` forward, and change nothing else.

    Lane FB-1 item F2. ``booking_ttl_s`` is a guess made at booking time;
    a launch that outlives its guess is reported past-TTL by
    ``budget_dangling_launches`` and by the dashboard's budget card, and
    until now the only ways to clear that reading were to reconcile early
    (a lie about the actuals) or to let a real, running launch sit in the
    doctor's offender list. This is the third way: the launch says "still
    here", the TTL clock restarts, and nothing about the booking's
    accounting moves -- not ``est_tokens``, not ``booking_ttl_s``, not the
    state.

    Three bounds, all deliberate:

    1. **An open session must own the launch.** The refusal posture is
       ``book_launch``'s (:class:`NoOpenSessionError` when no session is
       open at all; :class:`LaunchNotOwnedError` when the open session is
       not the one that booked it). A booking's TTL is a claim about work
       running under a session, so a heartbeat from anywhere else is a
       claim nobody is in a position to make.
    2. **Live states only.** A RECONCILED/ABANDONED/REFUSED/DEFERRED
       booking has no TTL left to extend; refreshing one would rewrite the
       booked_ts of settled history.
    3. **Every refresh is an event** (``launch_heartbeat``, carrying both
       timestamps), because a TTL that keeps moving is exactly the kind of
       thing an auditor needs to be able to see having happened.
    """
    from trialerror.budget.errors import BudgetError
    from trialerror.budget.gate import resolve_open_session
    from trialerror.events.api import append_event

    session = resolve_open_session(store)
    if session is None:
        raise NoOpenSessionError(
            "no OPEN session in this program's ops.db -- `budget heartbeat` refuses to refresh a "
            "booking's TTL on behalf of a session that is not running (run `trialerror session boot`)"
        )

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    if row is None:
        raise BudgetError(f"unknown launch_id {launch_id!r}")
    if row["session_id"] != session["session_id"]:
        raise LaunchNotOwnedError(
            f"launch {launch_id!r} was booked by session {row['session_id']!r}, but the OPEN "
            f"session is {session['session_id']!r} -- a heartbeat may only be sent by the session "
            "that owns the booking"
        )
    if row["state"] not in _LIVE_STATES:
        raise BudgetError(
            f"launch {launch_id!r} is in state {row['state']!r}, not one of {_LIVE_STATES!r} -- "
            "there is no live booking TTL to refresh"
        )

    ts = now_ts or now()
    booked_ts_before = row["booked_ts"]
    update(store, "launch", pk_column="launch_id", pk_value=launch_id, changes={"booked_ts": ts})
    event = append_event(
        store,
        event_type="launch_heartbeat",
        session_id=session["session_id"],
        launch_id=launch_id,
        payload={
            "booked_ts_before": booked_ts_before,
            "booked_ts_after": ts,
            "booking_ttl_s": row["booking_ttl_s"],
            "state": row["state"],
        },
    )
    return {
        "launch_id": launch_id,
        "session_id": session["session_id"],
        "state": row["state"],
        "booked_ts_before": booked_ts_before,
        "booked_ts": ts,
        "booking_ttl_s": row["booking_ttl_s"],
        "event_id": event["event_id"],
    }


def reconcile_launch(
    store: Store,
    *,
    launch_id: str,
    actual_tokens: int,
    reconcile_source: str = "manual",
    spawned_model: str | None = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Settle actuals by launch_id (design Section 5.1 ``reconcile_launch``
    tool). Feeds the settled ``actual_tokens`` into the owning pool's
    ``spent_visible_tokens`` running total so subsequent ``book_launch``
    cap checks see it.

    ``spawned_model`` records the model this launch ACTUALLY ran on, in
    ``launch.attrs.spawned_model``. The spawn gate refuses a live mismatch
    at spawn time; this is the post-hoc half of the same question, and it is
    what the ``agent_model_matches_booking`` doctor check reads to answer
    "did anything book one class and run on a cheaper one" across a whole
    program's history. Left unset it changes nothing — a launch reconciled
    without it simply makes no claim about the model it ran on, and the
    check skips it rather than inventing one.

    ``reconcile_source`` must be one of :data:`ASSERTABLE_RECONCILE_SOURCES`.
    :data:`EVENT_RECONCILE_SOURCE` is refused here by name: it means "this
    number was read off a recorded event", and the only code in a position
    to say that is the code that read the event
    (:func:`reconcile_launch_from_event`). A provenance label a caller can
    simply assert is a label the ``reconcile_provenance`` doctor check
    cannot use."""
    from trialerror.budget.errors import BudgetError

    if reconcile_source not in ASSERTABLE_RECONCILE_SOURCES:
        detail = (
            f" -- {EVENT_RECONCILE_SOURCE!r} is set only by `budget reconcile --from-event`, which "
            "reads the launch's own subagent_return event; it cannot be asserted about a launch "
            "nobody measured"
            if reconcile_source == EVENT_RECONCILE_SOURCE
            else ""
        )
        raise BudgetError(
            f"reconcile_source {reconcile_source!r} is not one of {ASSERTABLE_RECONCILE_SOURCES!r}{detail}"
        )
    return _settle_launch(
        store,
        launch_id=launch_id,
        actual_tokens=actual_tokens,
        reconcile_source=reconcile_source,
        spawned_model=spawned_model,
        now_ts=now_ts,
    )


def _settle_launch(
    store: Store,
    *,
    launch_id: str,
    actual_tokens: int,
    reconcile_source: str,
    spawned_model: str | None = None,
    usage: Mapping[str, Any] | None = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """The one write path for a reconciliation, shared by
    :func:`reconcile_launch` (caller-asserted provenance) and
    :func:`reconcile_launch_from_event` (measured provenance). ``usage``, when
    given, fills platform-v2's four split columns; absent, they stay null,
    which is what "``--actual-tokens`` makes no claim about composition"
    looks like in the schema."""
    from trialerror.budget.errors import BudgetError

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    if row is None:
        raise BudgetError(f"unknown launch_id {launch_id!r}")
    if row["state"] not in ("RUNNING", "PROVISIONAL"):
        raise BudgetError(
            f"launch {launch_id!r} is already in terminal state {row['state']!r}; cannot reconcile twice"
        )

    ts = now_ts or now()
    changes: dict[str, Any] = {
        "state": "RECONCILED",
        "actual_tokens": actual_tokens,
        "reconciled_ts": ts,
        "reconcile_source": reconcile_source,
    }
    if usage is not None:
        for column, key in USAGE_COLUMNS.items():
            value = usage.get(key)
            changes[column] = int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    if spawned_model is not None:
        attrs_raw = row.get("attrs")
        attrs = json.loads(attrs_raw) if attrs_raw else {}
        attrs["spawned_model"] = spawned_model
        changes["attrs"] = json.dumps(attrs, ensure_ascii=False)
    update(store, "launch", pk_column="launch_id", pk_value=launch_id, changes=changes)

    pool = _current_pool(store, row["account_id"], row["model_class"])
    if pool is not None:
        new_spent = int(pool["spent_visible_tokens"] or 0) + int(actual_tokens)
        update(
            store,
            "budget_pool",
            pk_column="pool_id",
            pk_value=pool["pool_id"],
            changes={"spent_visible_tokens": new_spent, "updated_ts": ts},
        )

    return {
        "launch_id": launch_id,
        "state": "RECONCILED",
        "actual_tokens": actual_tokens,
        "reconciled_ts": ts,
        "reconcile_source": reconcile_source,
        "spawned_model": spawned_model,
        "pool_updated": pool is not None,
        "usage": {column: changes.get(column) for column in USAGE_COLUMNS} if usage is not None else None,
    }


def latest_subagent_return(store: Store, launch_id: str) -> dict[str, Any] | None:
    """The newest ``subagent_return`` event for ``launch_id`` (the row, with
    its ``payload`` already decoded into ``payload_obj``), or ``None``.

    Newest, not first: a launch that returned more than once -- a resumed
    workflow, a retried spawn under the same booking -- has more than one
    event, and the last one is the one whose usage describes the run that
    actually finished. The events live in ops.db (program-scoped) while the
    booking lives in platform.db (cross-program), so this can only be
    answered from the program the launch was booked in."""
    row = store.ops.execute(
        "SELECT * FROM event WHERE type = 'subagent_return' AND launch_id = ? "
        "ORDER BY ts DESC, event_id DESC LIMIT 1",
        (launch_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["payload_obj"] = json.loads(out["payload"]) if out["payload"] else {}
    except (TypeError, ValueError):
        out["payload_obj"] = {}
    return out


def reconcile_launch_from_event(
    store: Store,
    *,
    launch_id: str,
    spawned_model: str | None = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Settle a launch from the host's OWN usage reading (D-FB-13 (b)):
    reads the latest ``subagent_return`` event for ``launch_id``, takes its
    ``usage.total_tokens``, and records ``reconcile_source = 'event'``.

    Three named refusals, because each one is a different thing to go and
    fix:

    - **no event at all** -- the PostToolUse hook never fired for this
      launch (the Workflow tool does not fire hooks; a session may have run
      with hooks disabled). ``--actual-tokens`` is the path.
    - **``usage: null``** -- the hook fired and the host sent no usage
      object. Nothing here can invent one; ``--actual-tokens`` is the path.
    - **a usage object with no total** -- a shape the reader could not
      reduce to a number, which is a host-contract change worth seeing
      rather than reconciling around.

    The returned envelope names the event row that was read (``event_id``,
    ``ts``), so a reconciliation's provenance is followable to the row it
    came from rather than resting on a label."""
    from trialerror.budget.errors import BudgetError

    event = latest_subagent_return(store, launch_id)
    if event is None:
        raise BudgetError(
            f"no subagent_return event on file for launch {launch_id!r} -- the PostToolUse hook never "
            "recorded a return for it (the Workflow tool fires no hooks, and a session may have run "
            "with hooks disabled). Reconcile with --actual-tokens instead"
        )
    usage = event["payload_obj"].get("usage") if isinstance(event["payload_obj"], dict) else None
    if not isinstance(usage, Mapping):
        raise BudgetError(
            f"the subagent_return event {event['event_id']!r} for launch {launch_id!r} carries "
            "usage: null -- the hook fired but the host sent no usage object, so there is no measured "
            "number to reconcile from. Reconcile with --actual-tokens instead"
        )
    total = usage.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise BudgetError(
            f"the subagent_return event {event['event_id']!r} for launch {launch_id!r} carries a usage "
            f"object with no usable total (total_tokens={total!r}) -- reconcile with --actual-tokens, "
            "and treat this as a change in what the host reports"
        )

    result = _settle_launch(
        store,
        launch_id=launch_id,
        actual_tokens=total,
        reconcile_source=EVENT_RECONCILE_SOURCE,
        spawned_model=spawned_model,
        usage=usage,
        now_ts=now_ts,
    )
    result["event"] = {
        "event_id": event["event_id"],
        "ts": event["ts"],
        "session_id": event["session_id"],
        "total_source": usage.get("total_source"),
    }
    return result


def tree_rollup(store: Store, root_launch_id: str) -> dict[str, Any]:
    """Sum ``est_tokens``/``actual_tokens`` over a launch and every
    descendant reachable via ``parent_launch`` (design Section 4.3:
    "``parent_launch?`` FK (tree-inherited rollups; omnigent pattern)";
    Section 9.2: "tree rollups via parent_launch"). BFS over children - 
    correct for any tree depth/fan-out, not just one level."""
    from trialerror.budget.errors import BudgetError

    root = get(store, "launch", pk_column="launch_id", pk_value=root_launch_id)
    if root is None:
        raise BudgetError(f"unknown launch_id {root_launch_id!r}")

    members = [root]
    frontier = [root_launch_id]
    while frontier:
        parent_id = frontier.pop()
        children = store.platform.execute(
            "SELECT * FROM launch WHERE parent_launch = ?", (parent_id,)
        ).fetchall()
        for c in children:
            child = dict(c)
            members.append(child)
            frontier.append(child["launch_id"])

    est_total = sum(int(m["est_tokens"] or 0) for m in members)
    actual_total = sum(int(m["actual_tokens"] or 0) for m in members)
    states: dict[str, int] = {}
    for m in members:
        states[m["state"]] = states.get(m["state"], 0) + 1

    return {
        "root_launch_id": root_launch_id,
        "member_count": len(members),
        "descendant_count": len(members) - 1,
        "est_tokens_total": est_total,
        "actual_tokens_total": actual_total,
        "states": states,
        # D-FB-13 (c). A tree's actual_tokens total says how much; the split
        # says of what -- and a rollup over a tree where only some members
        # were reconciled from their own return events would otherwise read
        # as if the whole tree's composition were known. ``attested`` is how
        # many members carry a split at all, so the totals are never mistaken
        # for the tree's.
        "usage_split": usage_split_totals(members),
    }


def usage_split_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum the four platform-v2 usage columns over ``rows``, plus how many of
    them carried a split at all.

    ``totals`` is ``None`` when nothing did: a set of zeros would read as
    "measured, and it was nothing", which is the one statement the split
    columns exist to keep apart from "nobody measured this". The shape is
    shared by ``budget rollup`` and the dashboard's budget card so the two
    surfaces cannot compute the same sum differently."""
    attested = 0
    totals = {column: 0 for column in USAGE_COLUMNS}
    for row in rows:
        values = {
            column: row.get(column)
            for column in USAGE_COLUMNS
            if isinstance(row.get(column), int) and not isinstance(row.get(column), bool)
        }
        if not values:
            continue
        attested += 1
        for column, value in values.items():
            totals[column] += value
    return {
        "attested": attested,
        "of_rows": len(rows),
        "totals": totals if attested else None,
    }


def create_pool(
    store: Store,
    *,
    account_id: str,
    model_class: str,
    period: str,
    cap_tokens: int,
    period_start: str | None = None,
    billed_multiplier: float = 2.75,
    soft_pct: float = 95,
    hard_pct: float = 100,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Create a new budget pool row (design Section 4.3 DDL). ``trialerror budget
    pools --create`` is the CLI path to this."""
    ts = now_ts or now()
    row = {
        "pool_id": new_id("POOL"),
        "account_id": account_id,
        "model_class": model_class,
        "period": period,
        "period_start": period_start or ts,
        "cap_tokens": cap_tokens,
        "spent_visible_tokens": 0,
        "billed_multiplier": billed_multiplier,
        "soft_pct": soft_pct,
        "hard_pct": hard_pct,
        "updated_ts": ts,
    }
    insert(store, "budget_pool", row)
    return row


def list_pools(store: Store, *, account_id: str | None = None) -> list[dict[str, Any]]:
    if account_id:
        rows = store.platform.execute(
            "SELECT * FROM budget_pool WHERE account_id = ? ORDER BY model_class, period_start DESC",
            (account_id,),
        ).fetchall()
    else:
        rows = store.platform.execute(
            "SELECT * FROM budget_pool ORDER BY account_id, model_class, period_start DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def budget_status(store: Store, *, account_id: str, model_class: str | None = None) -> dict[str, Any]:
    """Design Section 5.1 ``budget_status`` tool: "pools, headroom,
    multiplier, DEFER advisories." Reports the CURRENT pool (latest
    ``period_start``) per model class, each with projected headroom against
    its own hard/soft caps."""
    classes = [model_class] if model_class else list(dict.fromkeys(
        r["model_class"] for r in list_pools(store, account_id=account_id)
    ))

    # D-FB-14 (2): the SAME evaluation `budget pools` prints and the
    # `budget_pool_overspend` doctor check judges, filtered to the current
    # pool per class -- which is what this verb has always reported.
    evaluated = {
        entry["pool_id"]: entry
        for entry in pool_report(store.platform, account_id=account_id)
        if entry["standing"] == "current"
    }

    pools_out: list[dict[str, Any]] = []
    defer_advisories: list[dict[str, Any]] = []
    for mclass in classes:
        pool = _current_pool(store, account_id, mclass)
        if pool is None:
            continue
        entry = dict(evaluated[pool["pool_id"]])
        pools_out.append(entry)
        if entry["over_soft"]:
            defer_advisories.append(
                {
                    "model_class": mclass,
                    "pool_id": pool["pool_id"],
                    "reason": "projected spend over soft_pct" + (" (over hard_pct)" if entry["over_hard"] else ""),
                }
            )

    return {
        "account_id": account_id,
        "pools": pools_out,
        "defer_advisories": defer_advisories,
        "binding_limit": _binding_limit(pools_out),
    }


def _binding_limit(pools_out: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one limit a caller reading this envelope is actually up against:
    the pool with the least visible headroom left to whichever of its own
    soft/hard caps binds next. ``None`` when the account has no pool at all
    -- which is not "unlimited" but "uncapped and unmeasured", and the
    absence says so more honestly than a fabricated number would."""
    if not pools_out:
        return None
    tightest = min(pools_out, key=lambda p: p["visible_headroom_to_binding_limit"])
    return {
        "model_class": tightest["model_class"],
        "pool_id": tightest["pool_id"],
        "limit": tightest["binding_limit"],
        "visible_headroom_tokens": tightest["visible_headroom_to_binding_limit"],
        "visible_headroom_to_soft": tightest["visible_headroom_to_soft"],
        "billed_multiplier": tightest["billed_multiplier"],
        "over_soft": tightest["over_soft"],
        "over_hard": tightest["over_hard"],
    }


def snapshot_ingest(
    store: Store,
    *,
    account_id: str,
    source: str,
    payload: Mapping[str, Any] | str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Design Section 4.3: "ground truth rule preserved:
    ``quota_snapshot(source=screenshot)`` rows override all estimates."
    ``payload`` (design's own convention, documented here since v0 doesn't
    pin a schema beyond "JSON"): ``{"model_class": "top", "used_tokens": N}``
   - ``used_tokens`` is the REAL cumulative usage the user read off a
    screenshot at ``ts``; :func:`calibrate` consumes pairs of these."""
    row = {
        "snap_id": new_id("QSNAP"),
        "account_id": account_id,
        "ts": ts or now(),
        "source": source,
        "payload": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
    }
    insert(store, "quota_snapshot", row)
    return row


def _reconciled_visible_tokens_between(
    store: Store, account_id: str, model_class: str, ts_start: str, ts_end: str
) -> int:
    row = store.platform.execute(
        "SELECT COALESCE(SUM(actual_tokens), 0) FROM launch "
        "WHERE account_id = ? AND model_class = ? AND state = 'RECONCILED' "
        "AND reconciled_ts >= ? AND reconciled_ts <= ?",
        (account_id, model_class, ts_start, ts_end),
    ).fetchone()
    return int(row[0] or 0)


def calibrate(
    store: Store,
    *,
    account_id: str,
    model_class: str,
    window: str = "7d",
    now_ts: str | None = None,
) -> dict[str, Any]:
    """``trialerror budget calibrate derives multipliers from snapshot pairs``
    (design Section 4.3). Takes the EARLIEST and LATEST ``screenshot``
    :class:`quota_snapshot` on file for ``(account_id, model_class)``,
    divides their real-usage delta by the visible-token spend
    :func:`reconcile_launch` recorded for this account+model_class in that
    same window, and writes both a ``calibration`` row and the derived
    ``billed_multiplier`` back onto the current pool (closing the loop the
    over-cap check in :func:`book_launch`/:func:`budget_status` reads)."""
    from trialerror.budget.errors import BudgetError

    rows = store.platform.execute(
        "SELECT * FROM quota_snapshot WHERE account_id = ? AND source = 'screenshot' ORDER BY ts ASC",
        (account_id,),
    ).fetchall()
    relevant = []
    for r in rows:
        payload = json.loads(r["payload"])
        if payload.get("model_class", model_class) == model_class and "used_tokens" in payload:
            relevant.append((dict(r), payload))

    if len(relevant) < 2:
        raise BudgetError(
            f"calibrate needs >=2 screenshot snapshots for account={account_id!r} "
            f"model_class={model_class!r}; found {len(relevant)}"
        )

    (first_row, first_payload), (last_row, last_payload) = relevant[0], relevant[-1]
    delta_real = int(last_payload["used_tokens"]) - int(first_payload["used_tokens"])
    if delta_real < 0:
        raise BudgetError(
            "snapshot pair shows a negative real-usage delta (a quota reset inside the "
            "window?) - cannot calibrate across a reset"
        )
    delta_visible = _reconciled_visible_tokens_between(
        store, account_id, model_class, first_row["ts"], last_row["ts"]
    )
    if delta_visible <= 0:
        raise BudgetError(
            "no reconciled visible-token spend between the snapshot pair; cannot derive a "
            "multiplier (division by zero)"
        )

    multiplier = delta_real / delta_visible
    ts = now_ts or now()
    calib_row = {
        "calib_id": new_id("CALIB"),
        "account_id": account_id,
        "model_class": model_class,
        "window": window,
        "multiplier": multiplier,
        "derived_from": json.dumps(
            {
                "snap_ids": [first_row["snap_id"], last_row["snap_id"]],
                "delta_real": delta_real,
                "delta_visible": delta_visible,
            },
            ensure_ascii=False,
        ),
        "ts": ts,
    }
    insert(store, "calibration", calib_row)

    pool = _current_pool(store, account_id, model_class)
    if pool is not None:
        update(
            store,
            "budget_pool",
            pk_column="pool_id",
            pk_value=pool["pool_id"],
            changes={"billed_multiplier": multiplier, "updated_ts": ts},
        )

    return calib_row
