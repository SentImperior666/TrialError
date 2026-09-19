"""Mining adoption engram-F5: type-keyed staleness decay driving a
``needs_review`` lifecycle over ``memory_item``.

Source: ``docs/mining/G25-operator-2026-09__engram.md`` finding 4
(``internal/store/store.go:271-284`` -- ``decision``->6mo, ``policy``->12mo,
``preference``->3mo review offsets; ``:3049-3072``
``ObservationsNeedingReview``). Disposition:
``docs/reviews/MINING_2026-09_OPERATOR_LINKS.md`` section 3, orchestrator
verdict **"adopt-now:memory as a DOCTOR CHECK (needs_review surfacing by
type-keyed age); never mutates a pin or a ruling"**.

**The law this had to be built around (review section 5.7).** The
corrections ledger and ``LAW_DIGEST.md`` match in lockstep and a stale pin
is a hard refusal. A decay column that marked a policy row "needs review"
would be one careless commit away from a timer that expires a law. So the
decay here is a **pure function of two timestamps** -- it computes, it
never writes:

- nothing in this module issues an ``UPDATE``;
- no state is stored; ``needs_review`` is derived on every read;
- the only column the adoption added, ``memory_item.reviewed_ts``, is
  written exclusively by an explicit human/agent act
  (``trialerror memory reviewed <id>``), never by age.

An item therefore cannot be expired, unpinned or downgraded by the passage
of time. The worst a fully-decayed law can do is appear in a ``warn`` on a
doctor run and in a dashboard count.

**Half-life, not a hard offset.** The source sets a single ``review_after``
date per type. Keeping a continuous ``freshness`` (``0.5 ** (age /
half_life)``) instead costs nothing, collapses to the same boolean at the
0.5 threshold -- ``needs_review`` is exactly "older than one half-life" --
and additionally gives the doctor check and the dashboard a sane ORDER:
"the six things furthest past due" rather than an unsorted pile.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from trialerror.util.timeutil import now, now_dt, parse

__all__ = [
    "HALF_LIFE_DAYS",
    "DEFAULT_HALF_LIFE_DAYS",
    "REVIEW_THRESHOLD",
    "half_life_days",
    "freshness",
    "review_state",
    "stale_items",
    "mark_reviewed",
    "summarize",
]

#: Days per kind before a memory item has decayed to half-fresh, i.e.
#: before it is due for a look. Mapped from the source's own offsets onto
#: this schema's five kinds (``trialerror.memory.api.KINDS``):
#:
#: - ``rule`` -- the ``policy`` analogue, the source's longest offset (12
#:   months). A standing rule is the thing most likely to be quietly
#:   outlived by the practice it describes, and the least likely to be
#:   noticed, which is why it gets a clock at all rather than none.
#: - ``fact`` / ``lesson`` -- 6 months, the source's ``decision`` offset:
#:   a recorded finding ages with the system it was found in.
#: - ``preference`` -- 3 months, the source's own figure for the same word.
#: - ``index`` -- 3 months: an index is a claim about what EXISTS, which a
#:   growing corpus falsifies faster than anything else here.
#:
#: These are the source's numbers translated, not measured ones. They are
#: a starting point for the operator to move, which is why they live in one
#: dict rather than being scattered through the queries.
HALF_LIFE_DAYS: dict[str, int] = {
    "rule": 365,
    "fact": 180,
    "lesson": 180,
    "preference": 90,
    "index": 90,
}

#: Used for a kind not in the table (a kind added later, or a row imported
#: from an older schema) -- the most conservative of the mapped values, so
#: an unrecognised kind is surfaced sooner rather than never.
DEFAULT_HALF_LIFE_DAYS = 90

#: ``freshness`` below this is ``needs_review``. At 0.5 the predicate is
#: exactly "one half-life has passed", which is what makes the continuous
#: curve and the source's discrete ``review_after`` date agree.
REVIEW_THRESHOLD = 0.5

_SECONDS_PER_DAY = 86400.0


def half_life_days(kind: str | None) -> int:
    return HALF_LIFE_DAYS.get(kind or "", DEFAULT_HALF_LIFE_DAYS)


def freshness(kind: str | None, age_days: float) -> float:
    """``0.5 ** (age / half_life)`` clamped to ``(0, 1]``. A negative age
    (a row timestamped in the future, e.g. a clock-skewed import) reads as
    fully fresh rather than as more-than-fresh, so no row can outrank a
    just-written one."""
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days(kind))


def _age_days(reference_ts: str | None, *, asof: str | None) -> float:
    if not reference_ts:
        return 0.0
    try:
        then = parse(reference_ts)
        current = parse(asof) if asof else now_dt()
    except (ValueError, TypeError):
        # A timestamp this module cannot parse is not this module's
        # problem to raise on: report age 0 (fully fresh, never surfaced)
        # rather than break a doctor run over one malformed row.
        return 0.0
    return (current - then).total_seconds() / _SECONDS_PER_DAY


def review_state(row: Mapping[str, Any], *, asof: str | None = None, threshold: float = REVIEW_THRESHOLD) -> dict[str, Any]:
    """The derived staleness view of one ``memory_item`` row. Pure: takes
    a row, returns a dict, touches nothing.

    The clock runs from ``reviewed_ts`` when set, otherwise ``updated_ts``
    -- "somebody looked at this and left it alone" is as good as "somebody
    rewrote it", and without that the only way to reset the clock would be
    to make a pointless edit to the body.
    """
    kind = row.get("kind")
    reference = row.get("reviewed_ts") or row.get("updated_ts")
    age = _age_days(reference, asof=asof)
    hl = half_life_days(kind)
    f = freshness(kind, age)
    return {
        "memory_item_id": row.get("memory_item_id"),
        "key": row.get("key"),
        "tier": row.get("tier"),
        "kind": kind,
        "account_id": row.get("account_id"),
        "last_touched_ts": reference,
        "last_touched_was_review": bool(row.get("reviewed_ts")),
        "age_days": round(age, 3),
        "half_life_days": hl,
        "freshness": round(f, 6),
        "needs_review": f < threshold,
        "overdue_days": round(max(0.0, age - hl), 3),
    }


def stale_items(
    handle: Any,
    *,
    asof: str | None = None,
    threshold: float = REVIEW_THRESHOLD,
    account_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every ACTIVE item past its half-life, most overdue first.

    ``handle`` may be a :class:`~trialerror.stores.store.Store`, a
    read-only store, or a bare ops :class:`sqlite3.Connection` -- the
    doctor check and the dashboard panel each hold a different one.
    """
    conn: sqlite3.Connection = getattr(handle, "ops", handle)
    sql = "SELECT memory_item_id, key, tier, kind, account_id, updated_ts, reviewed_ts FROM memory_item WHERE status = 'active'"
    params: list[Any] = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    states = [review_state(r, asof=asof, threshold=threshold) for r in rows]
    stale = [s for s in states if s["needs_review"]]
    stale.sort(key=lambda s: (-s["overdue_days"], s["key"] or ""))
    return stale[:limit] if limit is not None else stale


def mark_reviewed(store: Any, memory_item_id: str, *, ts: str | None = None) -> dict[str, Any]:
    """Record that a human or agent LOOKED at this item and left it
    standing -- the only write in this module, and the only thing that
    resets a decay clock.

    Deliberately writes ``reviewed_ts`` and nothing else: it is not an
    edit, it does not touch ``updated_ts`` (which would misreport the
    content as having changed), and it cannot alter a body, a tier, a
    status or a pin.
    """
    from trialerror.stores import update as _update

    row = store.ops.execute(
        "SELECT * FROM memory_item WHERE memory_item_id = ?", (memory_item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"mark_reviewed: no memory_item {memory_item_id!r}")
    ts = ts or now()
    _update(store, "memory_item", pk_column="memory_item_id", pk_value=memory_item_id, changes={"reviewed_ts": ts})
    merged = dict(row)
    merged["reviewed_ts"] = ts
    return merged


def summarize(states: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count + per-kind breakdown, the shape the dashboard panel and the
    doctor check both want."""
    by_kind: dict[str, int] = {}
    for s in states:
        k = s.get("kind") or "unknown"
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"count": len(states), "by_kind": by_kind}
