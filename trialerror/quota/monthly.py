"""The monthly spending cap (design ``L4_quota-policy.md`` Section 2.6):
``trialerror quota month``, the 50/80/95 % notices, and the operator packet
item at 80 %.

"On round-0 day, a monthly spend limit stopped the sandbox orchestrator for
about two hours without warning" (design Section 0 item 2) -- this module
is watched, never enforced: nothing here refuses a booking or a session; it
only computes the figure and records/pushes the warnings."""

from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "NOTICE_LEVELS",
    "month_period_label",
    "month_start_ts",
    "spend_this_month",
    "due_levels",
    "record_notices",
    "packet_item_payload",
]

#: design Section 2.6: "At 50, 80 and 95 % of limit_usd".
NOTICE_LEVELS = (50, 80, 95)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamped_start_day(year: int, month: int, month_start_day: int) -> int:
    """``month_start_day``, clamped to the last real day of ``(year, month)``
    -- review N4: a configured day of 29-31 crashed ``datetime(...)`` outright
    in a shorter month (February most of all). A start day past the end of
    the month means "the month's last day", not an error."""
    return min(month_start_day, calendar.monthrange(year, month)[1])


def month_start_ts(now: datetime, month_start_day: int = 1) -> str:
    """The ISO timestamp the current billing month started at. A
    ``month_start_day`` past the current day rolls back to last month (e.g.
    day 28 asked on the 27th means the month that started on the 28th of
    the PRIOR month)."""
    year, month = now.year, now.month
    if now.day < _clamped_start_day(year, month, month_start_day):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return _iso(datetime(year, month, _clamped_start_day(year, month, month_start_day), tzinfo=timezone.utc))


def month_period_label(now: datetime, month_start_day: int = 1) -> str:
    """The period key ``quota_notice`` dedupes on: ``YYYY-MM`` of the month
    that STARTED at or before ``now`` (matches :func:`month_start_ts`)."""
    if now.day < _clamped_start_day(now.year, now.month, month_start_day):
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    else:
        year, month = now.year, now.month
    return f"{year:04d}-{month:02d}"


def _sessions(conn: sqlite3.Connection, account_label: str) -> dict[str, list[tuple[float, float, float | None, float | None]]]:
    """session_id -> [(epoch, cost, five_pct, seven_pct), ...] ascending, for
    every capture with a usable cost, across both hosts."""
    cur = conn.execute(
        "SELECT session_id, epoch, session_cost_usd, five_pct, seven_pct FROM quota_capture "
        "WHERE account_label = ? AND session_id IS NOT NULL AND session_cost_usd IS NOT NULL "
        "ORDER BY epoch ASC",
        (account_label,),
    )
    out: dict[str, list[tuple[float, float, float | None, float | None]]] = {}
    for sid, epoch, cost, five_pct, seven_pct in cur.fetchall():
        out.setdefault(sid, []).append((epoch, cost, five_pct, seven_pct))
    return out


def spend_this_month(
    conn: sqlite3.Connection,
    account_label: str,
    *,
    counts: str,
    month_start_epoch: float,
    now_epoch: float,
) -> float:
    """Total spend since ``month_start_epoch`` (design Section 2.6).

    ``counts="all"``: for every session, its cost increase since the month
    started (a session with no capture before the month began counts from
    0 -- its cumulative cost already starts at 0 for a brand-new session).

    ``counts="over_window"``: only the cost increase across consecutive
    captures where the LATER capture's ``five_pct``/``seven_pct`` reading
    was already >= 100 -- the "extra usage past the windows" a plan's cap
    is actually watching."""
    if counts not in ("all", "over_window"):
        raise ValueError(f"counts must be 'all' or 'over_window', got {counts!r}")
    sessions = _sessions(conn, account_label)
    total = 0.0
    for sid, points in sessions.items():
        in_range = [(e, c, f, s) for e, c, f, s in points if e <= now_epoch]
        if not in_range:
            continue
        before = [p for p in in_range if p[0] < month_start_epoch]
        within = [p for p in in_range if p[0] >= month_start_epoch]
        if not within:
            continue
        if counts == "all":
            baseline = before[-1][1] if before else 0.0
            delta = within[-1][1] - baseline
            if delta > 0:
                total += delta
        else:
            prev = before[-1] if before else None
            for point in within:
                over = (point[2] is not None and point[2] >= 100) or (point[3] is not None and point[3] >= 100)
                if prev is not None:
                    if over:
                        delta = point[1] - prev[1]
                        if delta > 0:
                            total += delta
                elif over:
                    # review N5: this session's FIRST in-month point (no
                    # "before" row at all) already shows the window at or
                    # past 100% -- it started already over, so its whole
                    # cost-from-0 counts, not nothing. Mirrors the "all"
                    # branch's own from-0 baseline for a brand-new session.
                    if point[1] > 0:
                        total += point[1]
                prev = point
    return total


def due_levels(conn: sqlite3.Connection, account_label: str, period: str, spend: float, limit_usd: float) -> list[int]:
    """Which of :data:`NOTICE_LEVELS` ``spend`` has crossed for
    ``(account_label, period)`` that has no ``monthly_level`` row yet."""
    if limit_usd <= 0:
        return []
    pct = (spend / limit_usd) * 100.0
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT level FROM quota_notice WHERE account_label = ? AND kind = 'monthly_level' AND period = ?",
            (account_label, period),
        ).fetchall()
    }
    return [level for level in NOTICE_LEVELS if pct >= level and level not in existing]


def record_notices(
    conn: sqlite3.Connection,
    account_label: str,
    period: str,
    spend: float,
    limit_usd: float,
    now_ts: str,
) -> list[dict[str, Any]]:
    """Insert one ``quota_notice`` row per newly-crossed level (never a
    second one for a level already recorded this period). Returns the rows
    inserted this call, so a caller (the CLI, or the packet item below)
    knows which levels are NEW rather than re-deriving it from a query."""
    levels = due_levels(conn, account_label, period, spend, limit_usd)
    rows = []
    for level in levels:
        detail = f"spend ${spend:.2f} of ${limit_usd:.2f} limit ({period})"
        conn.execute(
            "INSERT INTO quota_notice (account_label, kind, period, level, created_ts, detail) "
            "VALUES (?, 'monthly_level', ?, ?, ?, ?)",
            (account_label, period, level, now_ts, detail),
        )
        rows.append({"account_label": account_label, "period": period, "level": level, "created_ts": now_ts, "detail": detail})
    if rows:
        conn.commit()
    return rows


def packet_item_payload(spend: float, limit_usd: float) -> dict[str, Any]:
    """The ``raw`` dict for :func:`trialerror.packet.store.add_item` at the
    80 % level (design Section 2.6, verbatim what/why/options)."""
    return {
        "what": "Monthly spend is at 80 % of the limit",
        "why": "at 100 % sessions stop mid-work, as on round-0 day",
        "options": [
            {"key": "raise_limit", "label": "raise the limit", "consequence": "more headroom this month; the cap moves, not the spending"},
            {"key": "slow_lanes", "label": "slow the lanes", "consequence": "fewer or smaller waves this month, spend growth slows"},
            {"key": "accept_stop", "label": "accept a stop", "consequence": "sessions may stop mid-work once the limit is reached"},
        ],
        "recommended": "raise_limit",
        "if_undecided": "the limit stays as configured; a session may stop mid-work at 100%",
        "needed_by": "next-session",
        "refs": [{"label": "spend so far", "ref": f"${spend:.2f} of ${limit_usd:.2f}"}],
    }
