"""Tests for the monthly spending cap (design ``L4_quota-policy.md`` Section
2.6 / Section 3 item 5): :mod:`trialerror.quota.monthly`."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from trialerror.quota.monthly import (
    due_levels,
    month_period_label,
    month_start_ts,
    packet_item_payload,
    record_notices,
    spend_this_month,
)
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn, platform.MIGRATIONS)
    return conn


def _insert(conn, *, epoch, session_id, cost, five_pct=50, seven_pct=50, account="a"):
    conn.execute(
        "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id, session_cost_usd, "
        "five_pct, five_resets, seven_pct, seven_resets) VALUES ('dev',?,?,?,?,?,?,?,?,?)",
        (account, epoch, "2026-09-27T00:00:00Z", session_id, cost, five_pct, 9_999_999_999, seven_pct, 9_999_999_999),
    )


MONTH_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
MONTH_START_EPOCH = MONTH_START.timestamp()
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
NOW_EPOCH = NOW.timestamp()


def test_month_start_ts_and_period_label_for_day_1():
    assert month_start_ts(NOW, 1) == "2026-09-01T00:00:00Z"
    assert month_period_label(NOW, 1) == "2026-09"


def test_month_start_ts_rolls_back_when_the_start_day_has_not_happened_yet_this_month():
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert month_start_ts(now, 15) == "2026-08-15T00:00:00Z"
    assert month_period_label(now, 15) == "2026-08"


def test_n4_month_start_day_31_does_not_crash_in_a_shorter_month():
    """Review N4: a configured month_start_day of 29-31 crashed
    datetime(...) outright in a month with fewer days -- February most of
    all. It must clamp to that month's own last day instead."""
    # February 2026 has 28 days: day 31 clamps to the 28th.
    mid_feb = datetime(2026, 2, 15, tzinfo=timezone.utc)
    assert month_start_ts(mid_feb, 31) == "2026-01-31T00:00:00Z"  # still mid-January's period
    assert month_period_label(mid_feb, 31) == "2026-01"

    on_the_clamped_day = datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert month_start_ts(on_the_clamped_day, 31) == "2026-02-28T00:00:00Z"
    assert month_period_label(on_the_clamped_day, 31) == "2026-02"

    early_march = datetime(2026, 3, 5, tzinfo=timezone.utc)
    assert month_start_ts(early_march, 31) == "2026-02-28T00:00:00Z"  # March has a real 31st, but we're not there yet
    assert month_period_label(early_march, 31) == "2026-02"


def test_spend_all_sums_cost_increase_since_the_month_started():
    conn = _conn()
    # a session with a row BEFORE the month started: only the increase counts.
    _insert(conn, epoch=MONTH_START_EPOCH - 3600, session_id="S1", cost=10.0)
    _insert(conn, epoch=MONTH_START_EPOCH + 3600, session_id="S1", cost=25.0)
    # a session that started fresh this month: counts from 0.
    _insert(conn, epoch=MONTH_START_EPOCH + 7200, session_id="S2", cost=5.0)
    spend = spend_this_month(conn, "a", counts="all", month_start_epoch=MONTH_START_EPOCH, now_epoch=NOW_EPOCH)
    assert spend == pytest.approx(15.0 + 5.0)


def test_spend_all_ignores_captures_after_now():
    conn = _conn()
    _insert(conn, epoch=MONTH_START_EPOCH + 100, session_id="S1", cost=1.0)
    _insert(conn, epoch=NOW_EPOCH + 86400, session_id="S1", cost=999.0)  # future: excluded
    spend = spend_this_month(conn, "a", counts="all", month_start_epoch=MONTH_START_EPOCH, now_epoch=NOW_EPOCH)
    assert spend == pytest.approx(1.0)


def test_spend_over_window_only_counts_intervals_at_or_past_100_pct():
    conn = _conn()
    # under the window: not counted
    _insert(conn, epoch=MONTH_START_EPOCH + 100, session_id="S1", cost=1.0, five_pct=50)
    _insert(conn, epoch=MONTH_START_EPOCH + 200, session_id="S1", cost=3.0, five_pct=70)
    # crosses into over-window territory: this interval IS counted
    _insert(conn, epoch=MONTH_START_EPOCH + 300, session_id="S1", cost=10.0, five_pct=100)
    _insert(conn, epoch=MONTH_START_EPOCH + 400, session_id="S1", cost=14.0, five_pct=105)
    spend = spend_this_month(conn, "a", counts="over_window", month_start_epoch=MONTH_START_EPOCH, now_epoch=NOW_EPOCH)
    # (10-3) + (14-10) = 11; the 1->3 rise (under 100) is excluded
    assert spend == pytest.approx(11.0)


def test_spend_over_window_via_seven_pct_too():
    conn = _conn()
    _insert(conn, epoch=MONTH_START_EPOCH + 100, session_id="S1", cost=2.0, five_pct=10, seven_pct=99)
    _insert(conn, epoch=MONTH_START_EPOCH + 200, session_id="S1", cost=6.0, five_pct=10, seven_pct=100)
    spend = spend_this_month(conn, "a", counts="over_window", month_start_epoch=MONTH_START_EPOCH, now_epoch=NOW_EPOCH)
    assert spend == pytest.approx(4.0)


def test_n5_over_window_counts_a_session_that_started_already_past_the_window():
    """Review N5: a session with NO row before the month started, whose
    very first in-month row already shows the window at or past 100%,
    contributed nothing (the "no prev" branch was simply skipped). It
    started already over, so its whole cost-from-0 must count, the same way
    a brand-new session counts from 0 in the "all" branch."""
    conn = _conn()
    _insert(conn, epoch=MONTH_START_EPOCH + 100, session_id="S1", cost=20.0, five_pct=100)
    spend = spend_this_month(conn, "a", counts="over_window", month_start_epoch=MONTH_START_EPOCH, now_epoch=NOW_EPOCH)
    assert spend == pytest.approx(20.0)


def test_n5_a_session_starting_under_the_window_still_counts_nothing_until_it_crosses():
    conn = _conn()
    _insert(conn, epoch=MONTH_START_EPOCH + 100, session_id="S1", cost=5.0, five_pct=40)
    spend = spend_this_month(conn, "a", counts="over_window", month_start_epoch=MONTH_START_EPOCH, now_epoch=NOW_EPOCH)
    assert spend == pytest.approx(0.0)


def test_due_levels_and_record_notices_write_once_per_level():
    conn = _conn()
    period = "2026-09"
    assert due_levels(conn, "a", period, 50.0, 100.0) == [50]
    rows = record_notices(conn, "a", period, 50.0, 100.0, "2026-09-27T00:00:00Z")
    assert [r["level"] for r in rows] == [50]
    # calling again at the same spend records nothing new
    assert record_notices(conn, "a", period, 50.0, 100.0, "2026-09-27T00:01:00Z") == []
    # spend rises to 82%: 80 is newly due, 50 stays recorded
    rows2 = record_notices(conn, "a", period, 82.0, 100.0, "2026-09-27T01:00:00Z")
    assert [r["level"] for r in rows2] == [80]
    stored = conn.execute(
        "SELECT level FROM quota_notice WHERE account_label='a' AND kind='monthly_level' AND period=? ORDER BY level",
        (period,),
    ).fetchall()
    assert [r["level"] for r in stored] == [50, 80]


def test_a_spend_that_crosses_two_levels_at_once_records_both():
    conn = _conn()
    rows = record_notices(conn, "a", "2026-09", 96.0, 100.0, "2026-09-27T00:00:00Z")
    assert [r["level"] for r in rows] == [50, 80, 95]


def test_due_levels_is_empty_with_no_limit_configured():
    conn = _conn()
    assert due_levels(conn, "a", "2026-09", 500.0, 0.0) == []


def test_packet_item_payload_shape_matches_the_design_wording():
    payload = packet_item_payload(85.0, 100.0)
    assert payload["what"] == "Monthly spend is at 80 % of the limit"
    assert "round-0" in payload["why"]
    keys = {o["key"] for o in payload["options"]}
    assert keys == {"raise_limit", "slow_lanes", "accept_stop"}
    assert payload["recommended"] in keys
    assert payload["needed_by"] == "next-session"
