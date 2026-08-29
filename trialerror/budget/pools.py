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
from typing import Any, Mapping

from trialerror.budget.errors import ModelPolicyViolationError, NoOpenSessionError, UnknownOverrideRulingError
from trialerror.budget.policy import meets_minimum, required_class_for_purpose
from trialerror.stores import get, insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "DEFAULT_BOOKING_TTL_S",
    "BookResult",
    "book_launch",
    "reconcile_launch",
    "tree_rollup",
    "create_pool",
    "list_pools",
    "budget_status",
    "snapshot_ingest",
    "calibrate",
]

#: Design Section 4.3: ``booking_ttl_s INTEGER NOT NULL DEFAULT 3600``.
DEFAULT_BOOKING_TTL_S = 3600

_LIVE_STATES = ("PROVISIONAL", "RUNNING")


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


def _current_pool(store: Store, account_id: str, model_class: str) -> dict | None:
    """The pool for ``(account_id, model_class)`` with the latest
    ``period_start`` - v0's reading of "the current pool" (design Section 4.3
    doesn't specify pool-rollover mechanics; new-period pools are created
    explicitly via :func:`create_pool`, and this always picks the newest
    one on file)."""
    row = store.platform.execute(
        "SELECT * FROM budget_pool WHERE account_id = ? AND model_class = ? "
        "ORDER BY period_start DESC LIMIT 1",
        (account_id, model_class),
    ).fetchone()
    return dict(row) if row is not None else None


def _committed_visible_tokens(store: Store, account_id: str, model_class: str) -> int:
    """Sum of ``est_tokens`` over every live (PROVISIONAL/RUNNING) booking
    for this account+model_class - the not-yet-reconciled commitment a new
    booking's cap check must account for."""
    row = store.platform.execute(
        "SELECT COALESCE(SUM(est_tokens), 0) FROM launch "
        "WHERE account_id = ? AND model_class = ? AND state IN ('PROVISIONAL','RUNNING')",
        (account_id, model_class),
    ).fetchone()
    return int(row[0] or 0)


def _projected_billed_tokens(pool: Mapping[str, Any], committed_visible: int, new_est: int) -> float:
    return (float(pool["spent_visible_tokens"] or 0) + committed_visible + new_est) * float(
        pool["billed_multiplier"]
    )


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
    """
    session = _require_open_session(store, session_id)
    account_id = session["account_id"]

    _check_model_policy(
        store, purpose=purpose, model_class=model_class, policy=policy, override_ruling_id=override_ruling_id
    )

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
    }
    insert(store, "launch", row)

    return BookResult(
        ok=(state == "PROVISIONAL"),
        launch_id=launch_id,
        state=state,
        account_id=account_id,
        reason=reason,
        defer_advisory=defer_advisory,
        details={"pool_configured": pool is not None},
    )


def reconcile_launch(
    store: Store,
    *,
    launch_id: str,
    actual_tokens: int,
    reconcile_source: str = "manual",
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Settle actuals by launch_id (design Section 5.1 ``reconcile_launch``
    tool). Feeds the settled ``actual_tokens`` into the owning pool's
    ``spent_visible_tokens`` running total so subsequent ``book_launch``
    cap checks see it."""
    from trialerror.budget.errors import BudgetError

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    if row is None:
        raise BudgetError(f"unknown launch_id {launch_id!r}")
    if row["state"] not in ("RUNNING", "PROVISIONAL"):
        raise BudgetError(
            f"launch {launch_id!r} is already in terminal state {row['state']!r}; cannot reconcile twice"
        )

    ts = now_ts or now()
    update(
        store,
        "launch",
        pk_column="launch_id",
        pk_value=launch_id,
        changes={
            "state": "RECONCILED",
            "actual_tokens": actual_tokens,
            "reconciled_ts": ts,
            "reconcile_source": reconcile_source,
        },
    )

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
        "pool_updated": pool is not None,
    }


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

    pools_out: list[dict[str, Any]] = []
    defer_advisories: list[dict[str, Any]] = []
    for mclass in classes:
        pool = _current_pool(store, account_id, mclass)
        if pool is None:
            continue
        committed = _committed_visible_tokens(store, account_id, mclass)
        projected = _projected_billed_tokens(pool, committed, 0)
        hard_cap = float(pool["cap_tokens"]) * float(pool["hard_pct"]) / 100.0
        soft_cap = float(pool["cap_tokens"]) * float(pool["soft_pct"]) / 100.0
        entry = {
            "pool_id": pool["pool_id"],
            "model_class": mclass,
            "period": pool["period"],
            "period_start": pool["period_start"],
            "cap_tokens": pool["cap_tokens"],
            "spent_visible_tokens": pool["spent_visible_tokens"],
            "committed_visible_tokens": committed,
            "billed_multiplier": pool["billed_multiplier"],
            "projected_billed_tokens": projected,
            "hard_cap": hard_cap,
            "soft_cap": soft_cap,
            "headroom_tokens": max(hard_cap - projected, 0.0),
            "over_soft": projected > soft_cap,
            "over_hard": projected > hard_cap,
        }
        pools_out.append(entry)
        if entry["over_soft"]:
            defer_advisories.append(
                {
                    "model_class": mclass,
                    "pool_id": pool["pool_id"],
                    "reason": "projected spend over soft_pct" + (" (over hard_pct)" if entry["over_hard"] else ""),
                }
            )

    return {"account_id": account_id, "pools": pools_out, "defer_advisories": defer_advisories}


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
