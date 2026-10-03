"""Tests for the rate fit (design ``L4_quota-policy.md`` Section 2.3 /
Section 3 item 3): a synthetic history of 12 five-hour windows with a known
rate of 0.4 points per dollar, three of them deliberately excluded (too few
points, a partial start, headless activity), plus the seven-day fallback."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.quota.rate import fit_rate
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform

BASE = 2_000_000_000
FAR_SEVEN_RESETS = BASE + 40 * 86400  # never reached: forces the seven-day fallback
WINDOW_S = 5 * 3600
TRUE_RATE = 0.4

# per-window (index -> five_pct at the end of the window); 9 "good" windows
# average exactly 40 (dollars=100 each => ratio 0.4), 3 are excluded.
_POINTS = {0: 38, 1: 42, 2: 40, 4: 40, 6: 40, 8: 40, 9: 40, 10: 40, 11: 40}
_TOO_FEW_POINTS_IDX = 3
_PARTIAL_START_IDX = 5
_HEADLESS_IDX = 7


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn, platform.MIGRATIONS)
    return conn


def _insert(conn, *, host, epoch, session_id, cost, five_pct, five_resets, seven_pct):
    conn.execute(
        "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id, session_cost_usd, "
        "five_pct, five_resets, seven_pct, seven_resets, cc_version, model) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (host, "a", epoch, "2026-09-27T12:00:00Z", session_id, cost, five_pct, five_resets,
         seven_pct, FAR_SEVEN_RESETS, "2.1.141", "Fable 5"),
    )


def _build_fixture(conn) -> dict:
    baseline = 0.0
    resets = {}
    for i in range(12):
        five_resets = BASE + (i + 1) * WINDOW_S
        window_start = five_resets - WINDOW_S
        resets[i] = five_resets
        seven_start = 50 + i * 3

        end_points = _POINTS.get(i, 40)
        if i == _TOO_FEW_POINTS_IDX:
            end_points = 3

        _insert(conn, host="dev", epoch=window_start + 60, session_id="S", cost=baseline,
                five_pct=1, five_resets=five_resets, seven_pct=seven_start)
        _insert(conn, host="dev", epoch=five_resets - 100, session_id="S", cost=baseline + 100,
                five_pct=end_points, five_resets=five_resets, seven_pct=seven_start + 3)
        baseline += 100

        if i == _PARTIAL_START_IDX:
            # S2's FIRST EVER row lands inside this window with cost >= 0.50:
            # design Section 2.3 step 3's "otherwise the window is marked
            # partial_start" -- excludes the whole window, not just S2.
            _insert(conn, host="dev", epoch=window_start + 500, session_id="S2", cost=5.0,
                    five_pct=end_points, five_resets=five_resets, seven_pct=seven_start + 1)
    return resets


def test_the_rate_fit_lands_within_10_percent_and_the_interval_covers_the_true_rate():
    conn = _conn()
    resets = _build_fixture(conn)
    headless_reset = resets[_HEADLESS_IDX]

    def headless_active(five_resets, _start, _end):
        return five_resets == headless_reset

    result = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z", headless_active=headless_active)
    five = result["five_hour"]

    assert abs(five["points_per_usd"] - TRUE_RATE) / TRUE_RATE <= 0.10
    assert five["ci_low"] <= TRUE_RATE <= five["ci_high"]


def test_excluded_windows_are_counted_with_their_reasons():
    conn = _conn()
    resets = _build_fixture(conn)
    headless_reset = resets[_HEADLESS_IDX]

    def headless_active(five_resets, _start, _end):
        return five_resets == headless_reset

    result = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z", headless_active=headless_active)
    five = result["five_hour"]

    assert five["n_windows"] == 9
    assert five["excluded_windows"] == 3
    assert len(five["windows"]) == 12
    reasons = five["detail"]["exclusion_reasons"]
    assert reasons == {"too_few_points": 1, "partial_start": 1, "headless_active": 1}


def test_windows_are_stored_in_quota_rate():
    conn = _conn()
    resets = _build_fixture(conn)
    fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z", headless_active=lambda *_: False)
    rows = conn.execute("SELECT window, method FROM quota_rate WHERE account_label = 'a' ORDER BY window").fetchall()
    assert {r["window"] for r in rows} == {"five_hour", "seven_day"}


def test_the_seven_day_fallback_method_is_used_with_fewer_than_2_complete_seven_day_windows():
    conn = _conn()
    _build_fixture(conn)
    result = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z", headless_active=lambda *_: False)
    seven = result["seven_day"]
    assert seven["method"] == "five_hour_fallback"
    assert seven["points_per_usd"] > 0  # derived from the ratio, not left at the zero default


def test_no_history_at_all_yields_no_estimate_not_a_fabricated_zero_rate():
    """Review B1: a fit with zero usable windows must never be stored (and
    later shown) as a real rate of 0.0 points/dollar. ``method`` is
    ``no_estimate`` for both window kinds; ``points_per_usd`` stays the
    schema's NOT NULL placeholder, which no reader may treat as real without
    checking ``method`` first (test_quota_cli.py's rate-show/forecast tests
    check exactly that they don't)."""
    conn = _conn()
    result = fit_rate(conn, "nobody", now_ts="2026-09-28T00:00:00Z")
    assert result["five_hour"]["method"] == "no_estimate"
    assert result["five_hour"]["n_windows"] == 0
    assert result["seven_day"]["method"] == "no_estimate"


def test_a_five_hour_window_spanning_a_seven_day_reset_is_excluded_from_the_fallback_multiplier():
    """Review S3, scenario 1: the real DEV history has a five-hour window
    where seven_pct goes 86 -> 9 because a seven-day reset landed inside it
    -- `last - first` swung to -77, and with few windows the stored
    seven-day rate went negative. A window whose seven_pct-bearing rows
    carry more than one `seven_resets` value crossed a reset mid-window and
    must contribute no delta at all, not a nonsense swing."""
    conn = _conn()
    good_reset = BASE + WINDOW_S

    def ins(epoch, cost, five_pct, five_resets, seven_pct, seven_resets):
        conn.execute(
            "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id, "
            "session_cost_usd, five_pct, five_resets, seven_pct, seven_resets) VALUES "
            "('dev','a',?,?,'S',?,?,?,?,?)",
            (epoch, "2026-09-27T12:00:00Z", cost, five_pct, five_resets, seven_pct, seven_resets),
        )

    # window 0: ordinary, no reset crossing -- delta_seven = +3 over points=40
    ins(BASE + 60, 0.0, 1, good_reset, 50, 9_999_999_999)
    ins(good_reset - 100, 100.0, 40, good_reset, 53, 9_999_999_999)
    # window 1: a seven-day reset lands inside it (86 -> 9): must be skipped
    spanning_reset = good_reset + WINDOW_S
    ins(good_reset + 60, 100.0, 1, spanning_reset, 86, 9_999_999_999)
    ins(spanning_reset - 100, 200.0, 40, spanning_reset, 9, 9_999_999_998)

    result = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z")
    seven = result["seven_day"]
    assert seven["method"] == "five_hour_fallback"
    assert seven["points_per_usd"] > 0, "the spanning window's -77 delta must not drag the rate negative"
    assert seven["detail"]["n_five_hour_deltas_used"] == 1


def test_a_zero_seven_day_delta_is_kept_not_dropped_as_falsy():
    """Review S3, scenario 2: `if r.delta_seven_pct and r.points > 0` treated
    a delta of exactly 0.0 as falsy and dropped it, biasing the median
    multiplier upward when the true 7d/5h ratio is small."""
    conn = _conn()

    def ins(epoch, cost, five_pct, five_resets, seven_pct):
        conn.execute(
            "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id, "
            "session_cost_usd, five_pct, five_resets, seven_pct, seven_resets) VALUES "
            "('dev','a',?,?,'S',?,?,?,?,9999999999)",
            (epoch, "2026-09-27T12:00:00Z", cost, five_pct, five_resets, seven_pct),
        )

    r0, r1 = BASE + WINDOW_S, BASE + 2 * WINDOW_S
    ins(BASE + 60, 0.0, 1, r0, 30)
    ins(r0 - 100, 100.0, 40, r0, 30)  # delta_seven = 0 -- must still count
    ins(r0 + 60, 100.0, 1, r1, 30)
    ins(r1 - 100, 200.0, 40, r1, 31)  # delta_seven = 1

    result = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z")
    seven = result["seven_day"]
    assert seven["detail"]["n_five_hour_deltas_used"] == 2
    # median(0/40, 1/40) = 0.0125; five_ratio = 80/200 = 0.4 -> 0.005
    assert seven["points_per_usd"] == pytest.approx(0.4 * 0.0125)


def test_the_seven_day_ratio_path_gets_the_same_mixed_account_and_headless_exclusions():
    """Review S3: the seven-day ratio path (2+ complete seven-day windows)
    must apply the same mixed-account/headless exclusions as five-hour and
    count its own reasons, instead of using every complete window blindly."""
    conn = _conn()

    def ins(epoch, cost, five_pct, five_resets, seven_pct, seven_resets, session="S", host="dev"):
        conn.execute(
            "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id, "
            "session_cost_usd, five_pct, five_resets, seven_pct, seven_resets) VALUES "
            "(?,'a',?,?,?,?,?,?,?,?)",
            (host, epoch, "2026-09-27T12:00:00Z", session, cost, five_pct, five_resets, seven_pct, seven_resets),
        )

    # a separate, ordinary five-hour window (own session, realistic
    # epoch/five_resets alignment) purely so a five_ratio exists at all --
    # the seven-day ratio path is only attempted when one does. Its own
    # five_pct (1) never reaches MIN_POINTS, so it plays no other part.
    five_reset = BASE - 10 * WINDOW_S
    ins(five_reset - WINDOW_S + 60, 0.0, 1, five_reset, None, None, session="S5")
    ins(five_reset - 100, 100.0, 40, five_reset, None, None, session="S5")

    day = 86400
    seven_a, seven_b, seven_c = BASE + 7 * day, BASE + 14 * day, BASE + 21 * day
    # three complete seven-day windows so the ratio path (not the fallback)
    # is attempted, each with points=40 (>= MIN_POINTS) and dollars=100 (one
    # session, cost rising by 100 per window); five_pct stays low so these
    # rows are excluded from the (irrelevant, here) five-hour fit instead of
    # forming spurious five-hour windows of their own.
    baseline = 0.0
    for reset in (seven_a, seven_b, seven_c):
        ins(reset - day + 60, baseline, 1, BASE, 1, reset)
        ins(reset - 100, baseline + 100.0, 1, BASE, 40, reset)
        baseline += 100

    def headless_active(_reset, _start, _end):
        return _reset == seven_b

    result = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z", headless_active=headless_active)
    seven = result["seven_day"]
    assert seven["method"] == "ratio"
    assert seven["excluded_windows"] == 1
    assert seven["detail"]["exclusion_reasons"] == {"headless_active": 1}


def test_prior_ratio_is_reported_when_a_prior_is_given_and_skipped_otherwise():
    conn = _conn()
    _build_fixture(conn)
    with_prior = fit_rate(conn, "a", now_ts="2026-09-28T00:00:00Z", prior_points_per_usd=0.5)
    assert with_prior["five_hour"]["detail"]["ratio_to_prior"] == with_prior["five_hour"]["points_per_usd"] / 0.5

    conn2 = _conn()
    _build_fixture(conn2)
    without_prior = fit_rate(conn2, "a", now_ts="2026-09-28T00:00:00Z")
    assert "no price table configured" in without_prior["five_hour"]["detail"]["prior_check"]
