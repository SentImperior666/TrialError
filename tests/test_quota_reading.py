"""``trialerror.quota.reading``: the reading that cannot read low, and its bound for quiet periods.

Fixtures are rows with exactly the keys the statusLine capture writes (``epoch``, ``captured_ts``, ``session_id``,
``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}``, ``model``, ``cc_version``, ``account_hint``, and,
since the quota-policy work, ``account_label`` and ``session_cost_usd``), with and without the last two."""

from __future__ import annotations

import time

import pytest

from trialerror.quota import reading as R

T0 = 1_790_500_000.0  # a Monday-ish epoch; only differences matter
FIVE_R = int(T0 + 3 * 3600)  # the live five-hour window ends 3 h after T0
SEVEN_R = int(T0 + 5 * 86400)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def raw(t, sid="s1", five=None, seven=None, five_r=FIVE_R, seven_r=SEVEN_R, cost=None, label=None, iso_resets=False):
    """A capture row with the real keys. ``cost``/``label`` are left out (a pre-policy row) when ``None``."""
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five, "resets_at": _iso(five_r) if iso_resets else five_r}
    if seven is not None:
        rl["seven_day"] = {"used_percentage": seven, "resets_at": _iso(seven_r) if iso_resets else seven_r}
    row = {
        "epoch": t,
        "captured_ts": _iso(t),
        "rate_limits": rl,
        "model": "Opus 5.5",
        "session_id": sid,
        "cc_version": "2.1.283",
        "account_hint": "",
    }
    if label is not None:
        row["account_label"] = label
    if cost is not None:
        row["session_cost_usd"] = cost
    return row


def rows(*raws, host="h1"):
    parsed = [R.parse_row(r, host) for r in raws]
    assert all(p is not None for p in parsed)
    return parsed


def five(rs, now, **kw):
    return R.reading(rs, now, **kw).five


def seven(rs, now, **kw):
    return R.reading(rs, now, **kw).seven


# --- the window maximum beats the latest row ---------------------------------------------------------------------


def test_the_window_maximum_beats_the_latest_row_and_a_rerender_is_not_fresh():
    """Rows 69 then 44 from two sessions: one session's real call showed 69; a quiet session then re-rendered its own
    old 44 with an unchanged cost. The reading is 69, and the 44 is not the fresh sighting."""
    rs = rows(
        raw(T0 + 0, "quiet", five=44, cost=5.0),
        raw(T0 + 60, "busy", five=60, cost=1.0),
        raw(T0 + 120, "busy", five=69, cost=2.0),
        raw(T0 + 180, "quiet", five=44, cost=5.0),  # the stale re-render, latest in the file
    )
    w = five(rs, T0 + 200)
    assert w.basis == "reading"
    assert w.value == 69 and w.upper == 69
    assert w.t_fresh == T0 + 120  # the busy session's call, not the re-render at T0 + 180


def test_no_rows_at_all_is_unknown():
    assert R.reading([], T0).five.basis == "unknown"
    assert R.reading([], T0).seven.basis == "unknown"


# --- freshness ---------------------------------------------------------------------------------------------------


def test_with_cost_fields_a_row_is_fresh_only_when_its_cost_rose():
    rs = rows(
        raw(T0, "s", five=30, cost=1.0),
        raw(T0 + 100, "s", five=31, cost=1.0),  # figure moved but cost did not: not a call by this session
    )
    assert five(rs, T0 + 150).t_fresh == T0  # only the first row shows a call
    rs = rows(raw(T0, "s", five=30, cost=1.0), raw(T0 + 100, "s", five=31, cost=1.5))
    assert five(rs, T0 + 150).t_fresh == T0 + 100


def test_without_cost_fields_a_row_is_fresh_only_when_its_figure_changed_or_it_is_the_first():
    rs = rows(raw(T0, "s", five=30), raw(T0 + 100, "s", five=30), raw(T0 + 200, "s", five=30))
    assert five(rs, T0 + 250).t_fresh == T0  # the first row; unchanged re-renders are not fresh
    rs = rows(raw(T0, "s", five=30), raw(T0 + 100, "s", five=31), raw(T0 + 200, "s", five=31))
    assert five(rs, T0 + 250).t_fresh == T0 + 100


def test_a_new_sessions_first_render_with_zero_cost_is_not_a_call():
    """A session's first render can show stale numbers before any call (44 % where the truth was 69 %)."""
    rs = rows(raw(T0, "old", five=69, cost=10.0), raw(T0 + 100, "new", five=44, cost=0.0))
    w = five(rs, T0 + 150)
    assert w.value == 69 and w.t_fresh == T0


def test_the_last_fresh_sighting_ages_into_a_reading_then_a_bound_then_unknown():
    """D2: fresh_s shortened to 120 s so a bound's growth term applies from 2 minutes, not 10."""
    rs = rows(raw(T0, "s", five=30, cost=1.0))
    assert five(rs, T0 + 119).basis == "reading"
    assert five(rs, T0 + 121).basis == "bound"
    assert five(rs, T0 + 3599).basis == "bound"
    assert five(rs, T0 + 3601).basis == "unknown"


# --- the bound ---------------------------------------------------------------------------------------------------


def test_the_bound_grows_from_the_window_maximum_by_margin_plus_slope():
    rs = rows(raw(T0, "s", five=30, cost=1.0))
    b = R.DEFAULT_BOUNDS.five
    w = five(rs, T0 + 20 * 60)
    assert w.basis == "bound" and w.value == 30
    assert w.upper == pytest.approx(30 + b.margin + b.slope * 20, abs=0.05)


def test_the_bound_grows_from_the_window_maximum_and_counts_from_the_last_real_call_not_a_stale_first_render():
    """A session's stale first render (44) arrives after another session's real 69. It neither lowers the base (growing
    from 44 would put the bound under the truth) nor restarts the clock: the age counts from the 69."""
    rs = rows(raw(T0, "busy", five=69), raw(T0 + 100, "new", five=44))  # legacy rows: the first row counts as fresh
    w = five(rs, T0 + 100 + 12 * 60)
    b = R.DEFAULT_BOUNDS.five
    assert w.basis == "bound" and w.value == 69 and w.t_fresh == T0
    assert w.upper == pytest.approx(69 + b.margin + b.slope * (100 + 12 * 60) / 60, abs=0.05)


def test_a_bound_never_reads_below_the_window_maximum_and_never_above_100():
    rs = rows(raw(T0, "s", five=90, cost=1.0))
    w = five(rs, T0 + 50 * 60)
    assert w.basis == "bound" and w.upper == 100.0
    rs = rows(raw(T0, "a", five=50, cost=1.0), raw(T0 + 10, "b", five=40, cost=1.0))
    assert five(rs, T0 + 700).upper >= 50


def test_the_maximum_age_is_per_window_kind():
    rs = rows(raw(T0, "s", five=30, seven=40, cost=1.0))
    assert seven(rs, T0 + 3 * 3600).basis == "bound"  # a five-hour figure this old is unknown, a weekly one is not
    assert five(rs, T0 + 3 * 3600).basis == "unknown"
    assert seven(rs, T0 + 11.9 * 3600).basis == "bound"
    assert seven(rs, T0 + 12.1 * 3600).basis == "unknown"


def test_the_seven_day_reading_uses_its_own_freshness():
    """D2: fresh_s shortened to 600 s for the seven-day figure."""
    rs = rows(raw(T0, "s", five=30, seven=40, cost=1.0))
    assert seven(rs, T0 + 599).basis == "reading"
    assert seven(rs, T0 + 601).basis == "bound"


# --- a reset seen, and a reset nobody saw ------------------------------------------------------------------------


def _free_reset_rows():
    """The shape of the 09-27 free weekly reset: one session reads seven-day 97 % and, 28 minutes later, 2 % with the
    SAME weekly reset time; the five-hour figure falls too and its reset time moves."""
    old_five_r, new_five_r = FIVE_R - 5 * 3600, FIVE_R
    return rows(
        raw(T0 - 5400, "a", five=55, seven=95, five_r=old_five_r),
        raw(T0 - 1800, "a", five=63, seven=97, five_r=old_five_r),  # the last pre-reset sighting: B
        raw(T0 - 1700, "b", five=61, seven=96, five_r=old_five_r),
        raw(T0, "a", five=7, seven=2, five_r=new_five_r),
        raw(T0 + 60, "a", five=8, seven=2, five_r=new_five_r),
    )


def test_the_free_reset_is_recognised_and_the_pre_reset_rows_stop_counting():
    w = seven(_free_reset_rows(), T0 + 120)
    assert w.basis == "reading"
    assert w.value == 2 and w.upper == 2  # not 97
    assert w.rows_dropped_early_reset >= 2


def test_a_later_stale_pre_reset_rerender_from_another_session_is_dropped_as_implausible():
    rs = _free_reset_rows() + rows(raw(T0 + 300, "b", five=61, seven=96, five_r=FIVE_R - 5 * 3600))
    # b's own figure is still the old window's, unchanged: a re-render (and on the old five-hour reset time)
    rs += rows(raw(T0 + 400, "c", five=9, seven=97))  # a new session's first render carrying the pre-reset 97
    w = seven(rs, T0 + 500)
    assert w.value == 2 and w.upper == 2
    assert w.rows_dropped_implausible >= 1


def test_an_early_reset_cannot_leave_a_pre_reset_figure_standing_on_a_bound_either():
    w = seven(_free_reset_rows(), T0 + 3 * 3600)  # past the fresh limit: a bound, computed on the post-reset rows
    assert w.basis == "bound" and w.value == 2
    assert w.upper < 60  # 2 + margin + weekly slope x 3 h: a bound of the post-reset window, nowhere near 97


def test_a_one_point_fall_is_rounding_not_a_reset():
    rs = rows(raw(T0, "s", five=20, cost=1.0), raw(T0 + 60, "s", five=19, cost=1.2))
    w = five(rs, T0 + 100)
    assert w.value == 20 and w.rows_dropped_early_reset == 0


def test_after_a_five_hour_reset_the_bound_counts_from_the_old_windows_last_sighting_not_from_its_end():
    """No live window: the old five-hour window's reset time has passed and nobody has written a new figure. The old
    window was last seen 40 minutes before now, 30 minutes before its end: counting from its end would say 10 minutes."""
    old_r = int(T0 + 1800)
    rs = rows(raw(T0, "s", five=42, five_r=old_r, cost=1.0))
    now = T0 + 2400
    w = five(rs, now)
    assert w.basis == "bound_after_reset"
    b = R.DEFAULT_BOUNDS.five
    assert w.age_min == pytest.approx(40.0)
    assert w.upper == pytest.approx(b.after_reset(40), abs=0.05)
    assert w.upper > b.after_reset(10)
    assert w.value is None


def test_a_stale_rerender_after_the_old_window_ended_does_not_restart_the_clock():
    old_r = int(T0 + 1800)
    rs = rows(
        raw(T0, "s", five=42, five_r=old_r, cost=1.0),
        raw(T0 + 3000, "s", five=42, five_r=old_r, cost=1.0),  # re-render, unchanged cost, after the reset time
    )
    w = five(rs, T0 + 3300)
    assert w.age_min == pytest.approx((3300 - 0) / 60)  # counted from the real call, not the re-render


def test_the_after_reset_bound_is_unknown_past_the_maximum_age():
    old_r = int(T0 + 1800)
    rs = rows(raw(T0, "s", five=42, five_r=old_r, cost=1.0))
    assert five(rs, T0 + 3500).basis == "bound_after_reset"
    assert five(rs, T0 + 3700).basis == "unknown"


def test_a_rerender_alone_after_the_window_ended_is_unknown_not_a_bound():
    old_r = int(T0 + 1800)
    rs = rows(raw(T0, "s", five=42, five_r=old_r), raw(T0 + 60, "s", five=42, five_r=old_r))
    assert five(rs, T0 + 2000).basis == "bound_after_reset"  # the first row was a real call
    rs = rows(raw(T0, "s", five=42, five_r=old_r, cost=1.0), raw(T0 + 60, "s", five=42, five_r=old_r, cost=1.0))
    assert five(rs, T0 + 2000).t_fresh == T0


# --- accounts ----------------------------------------------------------------------------------------------------


def test_labelled_accounts_are_kept_apart():
    rs = rows(
        raw(T0, "a", five=80, seven=70, label="acct-a", cost=1.0),
        raw(T0 + 10, "b", five=10, seven=5, label="acct-b", cost=1.0, five_r=FIVE_R + 7200, seven_r=SEVEN_R + 86400),
    )
    only_a = R.account_rows(rs, "acct-a")
    assert [r.session_id for r in only_a] == ["a"]
    assert R.reading(only_a, T0 + 60).five.value == 80
    assert [r.session_id for r in R.account_rows(rs, "acct-b")] == ["b"]


def test_unlabelled_rows_join_a_labelled_account_by_their_reset_times():
    rs = rows(
        raw(T0, "a", five=50, label="acct-a", cost=1.0),
        raw(T0 + 5, "old", five=52),  # unlabelled, same reset times within 120 s
        raw(T0 + 6, "other", five=9, five_r=FIVE_R + 7200, seven_r=SEVEN_R + 86400),  # unlabelled, another account's
    )
    kept = R.account_rows(rs, "acct-a")
    assert sorted(r.session_id for r in kept) == ["a", "old"]


def test_without_a_caller_label_the_hosts_are_one_account_when_their_reset_times_agree():
    h1 = rows(raw(T0, "a", five=50, cost=1.0), host="h1")
    h2 = rows(raw(T0 + 1, "b", five=51, seven_r=SEVEN_R + 60, cost=1.0), host="h2")
    assert {r.host for r in R.account_rows(h1 + h2, "")} == {"h1", "h2"}


def test_without_a_caller_label_disagreeing_hosts_leave_the_callers_host_only():
    h1 = rows(raw(T0, "a", five=50, cost=1.0), host="h1")
    h2 = rows(raw(T0 + 1, "b", five=9, five_r=FIVE_R + 7200, seven_r=SEVEN_R + 86400, cost=1.0), host="h2")
    kept = R.account_rows(h1 + h2, "", caller_host="h1")
    assert {r.host for r in kept} == {"h1"}


def test_a_host_whose_last_capture_belongs_to_an_ended_window_is_not_a_different_account():
    """With ``now``, only a live pair decides: the laptop's old capture (its weekly window ended) says nothing."""
    old = dict(five_r=FIVE_R - 86400 * 9, seven_r=SEVEN_R - 86400 * 9)
    h1 = rows(raw(T0 - 86400 * 9, "a", five=50, seven=40, five_r=old["five_r"], seven_r=old["seven_r"], cost=1.0), host="h1")
    h2 = rows(raw(T0, "b", five=9, seven=30, cost=1.0), host="h2")
    assert {r.host for r in R.account_rows(h1 + h2, "", caller_host="h1")} == {"h1"}  # no ``now``: the old test
    assert {r.host for r in R.account_rows(h1 + h2, "", caller_host="h1", now=T0 + 30)} == {"h1", "h2"}


# --- parsing -----------------------------------------------------------------------------------------------------


def test_float_noise_in_a_percentage_is_rounded_to_a_tenth():
    row = R.parse_row(raw(T0, five=28.999999999999996, seven=56.00000000000001), "h")
    assert row.five_pct == 29.0 and row.seven_pct == 56.0


def test_iso_and_epoch_reset_times_parse_to_the_same_row():
    a = R.parse_row(raw(T0, five=30, seven=40, iso_resets=True), "h")
    b = R.parse_row(raw(T0, five=30, seven=40), "h")
    assert (a.five_resets, a.seven_resets) == (b.five_resets, b.seven_resets) == (FIVE_R, SEVEN_R)


def test_a_stamp_more_than_120_s_in_the_future_is_dropped():
    now = T0
    assert R.parse_row(raw(now + 121, five=30), "h", now) is None
    assert R.parse_row(raw(now + 119, five=30), "h", now) is not None
    rs = rows(raw(now + 500, "s", five=99, cost=1.0), raw(now - 100, "s", five=30, cost=1.0))
    assert five(rs, now).value == 30  # reading() drops the future row too


def test_malformed_rows_are_none_never_an_exception():
    for bad in (None, [], {}, {"epoch": "soon"}, {"epoch": T0, "rate_limits": "x"}):
        assert R.parse_row(bad, "h") is None or isinstance(R.parse_row(bad, "h"), R.Row)
    row = R.parse_row({"epoch": T0, "rate_limits": {"five_hour": {"used_percentage": True, "resets_at": "x"}}}, "h")
    assert row is not None and row.five_pct is None and row.five_resets is None


def test_a_latest_json_row_and_its_history_twin_are_one_row():
    twin = raw(T0, "s", five=30, seven=40, cost=1.0)
    w = R.reading(rows(twin, twin), T0 + 30)
    assert w.five.basis == "reading" and w.five.value == 30


def test_the_seven_day_figure_alone_stamped_after_a_five_hour_reset_still_counts_for_the_week():
    rs = rows(raw(T0, "s", seven=40, cost=1.0))  # no five-hour figure at all
    r = R.reading(rs, T0 + 30)
    assert r.seven.basis == "reading" and r.five.basis == "unknown"


# --- Bounds ------------------------------------------------------------------------------------------------------


def test_config_may_raise_a_bound_but_never_lower_it():
    d = R.DEFAULT_BOUNDS
    raised = R.bounds_from_config({"five_hour": {"margin": 9.0, "slope": 3.0, "start": 8.0, "reset_drop": 6.0}})
    assert raised.five.margin == 9.0 and raised.five.slope == 3.0 and raised.five.start == 8.0
    assert raised.five.reset_drop == 6.0
    assert raised.seven == d.seven  # untouched
    lowered = R.bounds_from_config({"five_hour": {"margin": 0.0, "slope": 0.1, "start": 0.0, "after_slope": 0.0,
                                                  "reset_drop": 0.5}})
    assert lowered.five == d.five
    junk = R.bounds_from_config({"five_hour": {"margin": "big", "slope": True, "start": float("nan")}})
    assert junk == d
    assert R.bounds_from_config(None) == d


def test_config_may_shorten_the_ages_but_never_lengthen_them():
    d = R.DEFAULT_BOUNDS
    b = R.bounds_from_config({"seven_day": {"fresh_s": 60, "max_age_s": 7200}, "five_hour": {"max_age_s": 999999}})
    assert b.seven.fresh_s == 60 and b.seven.max_age_s == 7200
    assert b.five.max_age_s == d.five.max_age_s


def test_a_raised_bound_reads_higher():
    rs = rows(raw(T0, "s", five=30, cost=1.0))
    base = five(rs, T0 + 1200).upper
    wider = five(rs, T0 + 1200, bounds=R.bounds_from_config({"five_hour": {"slope": 5.0}})).upper
    assert wider > base


# --- a row that changed but is not a real call cannot restart the clock -------------------------------------------


def test_a_new_sessions_stale_first_render_does_not_turn_a_bound_back_into_a_reading():
    """A figure of 70 seen at the start, 50 minutes ago; the answer is a bound. A new session's first render then
    arrives showing 44 (cost-less, or with a cost above zero as a resumed session carries): it is not a call, and the
    answer must stay a bound of the 70, not become a fresh reading of 70."""
    old = rows(raw(T0, "a", five=69), raw(T0 + 60, "a", five=70))
    now = T0 + 50 * 60
    before = five(old, now)
    assert before.basis == "bound" and before.upper == 100.0
    for stale in (raw(now - 30, "b", five=44), raw(now - 30, "b", five=44, cost=0.4)):
        w = five(old + rows(stale), now)
        assert w.basis == "bound", w
        assert w.value == 70 and w.upper == 100.0 and w.t_fresh == T0 + 60


def test_a_lagging_sessions_changing_figures_below_the_window_maximum_are_not_sightings():
    """The shape of the sandbox session whose figures lagged the account's (0, 5, 8, 10 while the window stood at 23):
    each row differs from that session's previous one, and none is a real call."""
    busy = rows(raw(T0, "busy", five=14), raw(T0 + 600, "busy", five=23))
    lag = rows(raw(T0 + 2400, "lag", five=0), raw(T0 + 2500, "lag", five=5), raw(T0 + 2600, "lag", five=8),
               raw(T0 + 2700, "lag", five=10))
    w = five(busy + lag, T0 + 2800)
    assert w.basis == "bound"
    assert w.value == 23 and w.t_fresh == T0 + 600


def test_a_figure_one_point_under_the_window_maximum_is_still_a_sighting_rounding_allowed():
    rs = rows(raw(T0, "a", five=70), raw(T0 + 100, "b", five=69), raw(T0 + 200, "b", five=69))
    w = five(rs, T0 + 1500)
    assert w.t_fresh == T0 + 100  # b's first row, one under 70: rounding, not a lag
    rs = rows(raw(T0, "a", five=70), raw(T0 + 100, "b", five=68))
    assert five(rs, T0 + 1500).t_fresh == T0  # two under: not a sighting


def test_the_same_rule_holds_for_the_seven_day_figure():
    old = rows(raw(T0, "a", seven=60), raw(T0 + 60, "a", seven=62))
    now = T0 + 3 * 3600
    assert seven(old, now).basis == "bound"
    w = seven(old + rows(raw(now - 30, "b", seven=41)), now)
    assert w.basis == "bound" and w.t_fresh == T0 + 60


# --- after an early reset, the sessions that have shown it keep their genuine rise --------------------------------


def test_the_session_that_showed_an_early_reset_keeps_its_later_genuine_rise():
    """Five-hour early reset with the reset time unchanged: one session goes 60 -> 64, falls to 2 (the reset), shows 4,
    then 17 eight minutes later (a measured burst; the after-reset line allows 10.6 there) and 19. The truth is at
    least 19. The plausibility line exists for OTHER sessions' stale pre-reset re-renders, not for this one."""
    rs = rows(
        raw(T0, "a", five=60, cost=1.0),
        raw(T0 + 60, "a", five=64, cost=1.2),
        raw(T0 + 120, "a", five=2, cost=1.3),  # the reset: 64 -> 2
        raw(T0 + 240, "a", five=4, cost=1.4),
        raw(T0 + 600, "a", five=17, cost=2.0),
        raw(T0 + 640, "a", five=19, cost=2.1),
    )
    w = five(rs, T0 + 660)
    assert w.basis == "reading" and w.value == 19 and w.upper == 19
    assert w.rows_dropped_early_reset == 2 and w.rows_dropped_implausible == 0


def test_a_session_with_a_kept_post_reset_row_keeps_its_later_rise_too():
    """Another session's first row after the reset is plausible (3 %), so it has shown the reset; its later 30 % is a
    real rise, not a stale pre-reset re-render."""
    rs = rows(
        raw(T0, "a", five=60, cost=1.0),
        raw(T0 + 60, "a", five=64, cost=1.2),
        raw(T0 + 120, "a", five=2, cost=1.3),
        raw(T0 + 300, "b", five=3, cost=0.1),
        raw(T0 + 700, "b", five=30, cost=1.0),
    )
    w = five(rs, T0 + 720)
    assert w.value == 30 and w.rows_dropped_implausible == 0


def test_a_session_that_has_not_shown_the_reset_still_has_its_implausible_row_dropped():
    """A session whose first row after the reset carries the pre-reset 64 has not shown the reset: dropped."""
    rs = rows(
        raw(T0, "a", five=60, cost=1.0),
        raw(T0 + 60, "a", five=64, cost=1.2),
        raw(T0 + 120, "a", five=2, cost=1.3),
        raw(T0 + 400, "c", five=64, cost=9.0),  # a new session's first render: the old window's number
    )
    w = five(rs, T0 + 420)
    assert w.value == 2 and w.rows_dropped_implausible == 1


# --- account joining -----------------------------------------------------------------------------------------------


def test_an_unlabelled_row_joins_a_labelled_account_only_when_every_shared_reset_pair_agrees():
    """Another account's unlabelled rows can share the weekly reset time (weekly resets fall on the hour) while their
    five-hour window is a different one. One agreeing pair is not enough: both accounts' figures would be mixed and the
    live five-hour window would be read from the wrong account (11 % where the caller's account stood at 70 %)."""
    mine = rows(raw(T0 - 300, "m", five=70, seven=40, label="main", cost=1.0), host="dev")
    other = rows(raw(T0 - 120, "o", five=10, seven=20, five_r=FIVE_R + 3600, cost=0.5),
                 raw(T0 - 60, "o", five=11, seven=20, five_r=FIVE_R + 3600, cost=0.6), host="sandbox")
    kept = R.account_rows(mine + other, "main")
    assert [r.session_id for r in kept] == ["m"]
    assert R.reading(kept, T0).five.value == 70


def test_a_row_with_only_one_reset_pair_joins_when_that_pair_agrees():
    mine = rows(raw(T0 - 300, "m", five=70, seven=40, label="main", cost=1.0), host="dev")
    weekly_only = rows(raw(T0 - 100, "w", seven=41, cost=0.2), host="sandbox")  # no five-hour figure at all
    assert sorted(r.session_id for r in R.account_rows(mine + weekly_only, "main")) == ["m", "w"]
    no_pair = rows(raw(T0 - 100, "n", seven=41, seven_r=SEVEN_R + 86400, cost=0.2), host="sandbox")
    assert [r.session_id for r in R.account_rows(mine + no_pair, "main")] == ["m"]


def test_account_rows_has_no_host_default_so_no_host_label_lives_in_harness_code():
    import inspect

    default = inspect.signature(R.account_rows).parameters["caller_host"].default
    assert default in (inspect.Parameter.empty, "")
    # With hosts that disagree and no caller host named, nothing is kept (UNKNOWN), never a guessed host's rows.
    h1 = rows(raw(T0, "a", five=50, cost=1.0), host="dev")
    h2 = rows(raw(T0 + 1, "b", five=9, five_r=FIVE_R + 7200, seven_r=SEVEN_R + 86400, seven=5, cost=1.0), host="h2")
    h1 = rows(raw(T0, "a", five=50, seven=40, cost=1.0), host="dev")
    assert R.account_rows(h1 + h2, "") == []
    assert {r.host for r in R.account_rows(h1 + h2, "", caller_host="h2")} == {"h2"}


def test_the_same_accounts_unlabelled_rows_from_a_new_five_hour_window_join_a_labelled_caller():
    """F3: the laptop is labelled 'main' and was last seen in a five-hour window that has now ended; the same account's
    other host (unlabelled, not yet exporting a label) is in the NEW window at 38 and 40 %. The weekly time agrees, and
    the labelled account has no live five-hour window that this one could contradict, so the rows join and the reading
    is 40, not a bound counted from the old window."""
    r1 = int(T0 - 25 * 60)  # the labelled host's window, ended 25 minutes ago
    dev = rows(raw(r1 - 300, "d", five=60, seven=30, five_r=r1, cost=2.0, label="main"), host="dev")
    sb = rows(raw(T0 - 120, "s", five=38, seven=33, five_r=FIVE_R, cost=1.0),
              raw(T0 - 60, "s", five=40, seven=33, five_r=FIVE_R, cost=1.2), host="sandbox")
    for now in (T0, None):
        kept = R.account_rows(dev + sb, "main", now=now)
        assert sorted(r.host for r in kept) == ["dev", "sandbox", "sandbox"]
        w = R.reading(kept, T0).five
        assert w.basis == "reading" and w.value == 40


# --- D1: one clock for every host -----------------------------------------------------------------------------


def test_a_hosts_clock_two_hours_ahead_gives_the_same_reading_once_corrected():
    """The sandbox's clock reads 2 h ahead of the caller's. Its row is stamped 2 h into the caller's future;
    clock_offset (read in the same call as the rows) corrects it before anything else, and the reading matches the
    equal-clocks case exactly."""
    now = T0 + 300
    equal = [R.parse_row(raw(T0, "s", five=30, cost=1.0), "sandbox", now)]
    ahead = [R.parse_row(raw(T0 + 7200, "s", five=30, cost=1.0), "sandbox", now, clock_offset=7200.0)]
    assert equal[0] is not None and ahead[0] is not None and equal[0].epoch == ahead[0].epoch
    assert R.reading(equal, now).five == R.reading(ahead, now).five


def test_a_writer_ahead_of_its_own_hosts_clock_is_still_dropped():
    """The host's clock itself is 2 h ahead (offset 7200); a row whose OWN stamp is a further 10 minutes ahead of
    THAT clock is still unusable -- the future-stamp rule applies AFTER the D1 correction, so it only catches a
    writer whose stamp is off from its host's own clock."""
    now, offset = T0, 7200.0
    assert R.parse_row(raw(T0 + offset + 600, "s", five=30, cost=1.0), "sandbox", now, clock_offset=offset) is None
    assert R.parse_row(raw(T0 + offset + 100, "s", five=30, cost=1.0), "sandbox", now, clock_offset=offset) is not None


# --- D3: a reset seen by a newer session ------------------------------------------------------------------------


def test_a_reset_seen_only_by_a_newer_session_drops_the_old_high_figure():
    """A session that started AFTER the reset never shows its own fall; its second row (a real call), 2 % following
    another session's 97 %, still marks and drops the old rows."""
    rs = rows(
        raw(T0, "old", seven=97, cost=5.0),
        raw(T0 + 300, "new", seven=2, cost=0.0),  # the new session's first row: never evidence
        raw(T0 + 500, "new", seven=2, cost=1.0),  # its second row, a real call: evidence
    )
    w = seven(rs, T0 + 520)
    assert w.value == 2 and w.rows_dropped_early_reset >= 1


def test_a_new_sessions_first_row_alone_does_not_mark_a_reset():
    rs = rows(raw(T0, "old", seven=97, cost=5.0), raw(T0 + 300, "new", seven=2, cost=0.0))
    assert seven(rs, T0 + 320).value == 97


def test_a_stale_rerender_never_marks_a_reset_only_a_real_call_does():
    stale = rows(
        raw(T0, "old", seven=97, cost=5.0),
        raw(T0 + 300, "new", seven=2, cost=0.0),  # first row, zero cost: not evidence
        raw(T0 + 500, "new", seven=2, cost=0.0),  # repeats, unchanged cost: a re-render, not evidence
    )
    assert seven(stale, T0 + 520).value == 97  # nothing here is evidence; no reset seen
    real_call = stale + rows(raw(T0 + 700, "new", seven=2, cost=1.0))  # the same session's real call, still 2 %
    assert seven(real_call, T0 + 720).value == 2  # now it counts


def test_s1_a_lagging_sessions_flat_or_one_point_wobble_is_still_not_reset_evidence():
    """The review's S1 (part D fix round): the rise-check guard alone still admits a lagging session's row that is
    flat against its own previous one, or wobbles down by rounding. L1: 0, 5, 8, 10, 9 (the 9 is flat-or-falling
    from 10, so the rise check alone lets it through) must not mark a reset at the busy session's 23. L2: the same
    shape with cost fields, where the last row (10, a real call) is exactly flat."""
    busy = rows(raw(T0, "busy", five=14), raw(T0 + 600, "busy", five=23))
    l1 = busy + rows(*(raw(T0 + 2400 + 100 * i, "lag", five=v) for i, v in enumerate((0, 5, 8, 10, 9))))
    assert five(l1, T0 + 2900).value == 23
    busyc = rows(raw(T0, "busy", five=14, cost=1.0), raw(T0 + 600, "busy", five=23, cost=2.0))
    l2 = busyc + rows(*(raw(T0 + 2400 + 100 * i, "lag", five=v, cost=0.5 + i) for i, v in enumerate((0, 5, 8, 10, 10))))
    assert five(l2, T0 + 2900).value == 23


def test_s1_a_genuine_reset_by_a_newer_session_still_registers():
    """The span guard must not swallow the real case D3 exists for: a new session whose figures stay within
    reset_drop of each other still marks the reset, flat (2, 2) or slowly climbing (2, 3, 4, 4)."""
    old = rows(raw(T0, "old", seven=97, cost=5.0))
    flat = old + rows(raw(T0 + 300, "new", seven=2, cost=0.5), raw(T0 + 700, "new", seven=2, cost=1.0))
    assert seven(flat, T0 + 720).value == 2
    climb = old + rows(*(raw(T0 + 300 + 400 * i, "new", seven=v, cost=0.5 + i) for i, v in enumerate((2, 3, 4, 4))))
    assert seven(climb, T0 + 1600).value == 4


def test_s2_the_window_end_uses_the_earliest_available_clock_not_just_the_callers_own():
    """The review's S2: D1 corrects another host's clock, but nothing corrects the caller's own -- a caller whose
    own clock runs ahead still read a live window as already ended, and read low from a bound_after_reset that
    starts near zero. A window resetting in 30 true minutes, the caller 40 minutes ahead: without window_now the
    bug reproduces (not a 'reading'); with it (the sandbox's own, true clock, the earliest one read) it stays 70."""
    live_five_r = int(T0 + 1800)  # 30 minutes from true T0
    caller_now = T0 + 2400  # the caller's own clock is 40 minutes ahead of true time
    rs = [R.parse_row(raw(T0 - 60, "a", five=70, cost=1.0, five_r=live_five_r), "sandbox", caller_now,
                      clock_offset=-2400.0)]  # sandbox's own true clock is T0 (offset = T0 - caller_now)
    without_fix = R.reading(rs, caller_now).five
    assert without_fix.basis != "reading"  # the bug: prematurely read as already ended
    with_fix = R.reading(rs, caller_now, window_now=T0).five
    assert with_fix.basis == "reading" and with_fix.value == 70


def test_a_reset_across_two_hosts_with_differing_clocks_matches_equal_clocks_after_d1():
    now = T0 + 900

    def new_session(offset):
        return [
            R.parse_row(raw(T0 + 300 + offset, "new", seven=2, cost=0.0), "sandbox", now, clock_offset=offset),
            R.parse_row(raw(T0 + 700 + offset, "new", seven=2, cost=1.0), "sandbox", now, clock_offset=offset),
        ]

    old = [R.parse_row(raw(T0, "old", seven=97, cost=5.0), "dev", now)]
    equal = R.reading(old + new_session(0.0), now).seven
    shifted = R.reading(old + new_session(7200.0), now).seven
    assert equal == shifted and equal.value == 2


def test_another_accounts_row_is_excluded_only_when_both_five_hour_windows_are_live_and_differ():
    """P7 and its neighbours. Two live five-hour windows at once cannot be one account; a window that has ended, or a
    row with no five-hour figure, cannot contradict one."""
    mine = rows(raw(T0 - 300, "m", five=70, seven=40, label="main", cost=1.0), host="dev")  # live (ends at FIVE_R)
    other_live = rows(raw(T0 - 60, "o", five=11, seven=20, five_r=FIVE_R + 3600, cost=0.6), host="sandbox")
    assert [r.session_id for r in R.account_rows(mine + other_live, "main", now=T0)] == ["m"]
    assert R.reading(R.account_rows(mine + other_live, "main", now=T0), T0).five.value == 70
    weekly_only = rows(raw(T0 - 60, "w", seven=41, cost=0.2), host="sandbox")
    assert sorted(r.session_id for r in R.account_rows(mine + weekly_only, "main", now=T0)) == ["m", "w"]
    other_ended = rows(raw(T0 - 60, "e", five=11, seven=20, five_r=int(T0 - 600), cost=0.6), host="sandbox")
    assert sorted(r.session_id for r in R.account_rows(mine + other_ended, "main", now=T0)) == ["e", "m"]
    other_week = rows(raw(T0 - 60, "x", five=11, seven=20, five_r=FIVE_R + 3600, seven_r=SEVEN_R + 86400, cost=0.6))
    assert [r.session_id for r in R.account_rows(mine + other_week, "main", now=T0)] == ["m"]  # another week too
