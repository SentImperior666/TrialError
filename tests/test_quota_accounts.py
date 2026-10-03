"""Tests for the new-account guard's pure decision function (design
``L4_quota-policy.md`` Section 2.5 / Section 3 item 7):
:func:`trialerror.quota.accounts.classify_account`.

Covers: new by age, new by window count, established, unlabelled, and the
stricter band edges (YIELD 60 / STOP 75)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trialerror.quota.accounts import NEW_STOP_AT, NEW_YIELD_AT, classify_account

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_unlabelled_turns_the_guard_off():
    result = classify_account({}, "", now=NOW)
    assert result["standing"] == "unlabelled"
    assert result["yield_at"] is None and result["stop_at"] is None
    assert result["message"] == "account unlabelled: new-account guard off"


def test_new_by_age_under_7_days_with_plenty_of_windows():
    doc = {"a": {"first_seen_ts": _iso(NOW - timedelta(days=2)), "windows_seen": [1, 2, 3, 4, 5]}}
    result = classify_account(doc, "a", now=NOW)
    assert result["standing"] == "new"
    assert result["yield_at"] == NEW_YIELD_AT and result["stop_at"] == NEW_STOP_AT
    assert "new account a" in result["message"]


def test_new_by_window_count_even_though_old_enough():
    doc = {"a": {"first_seen_ts": _iso(NOW - timedelta(days=30)), "windows_seen": [1, 2]}}
    result = classify_account(doc, "a", now=NOW)
    assert result["standing"] == "new"
    assert result["n_windows"] == 2


def test_established_needs_both_old_enough_and_enough_windows():
    doc = {"a": {"first_seen_ts": _iso(NOW - timedelta(days=30)), "windows_seen": [1, 2, 3, 4]}}
    result = classify_account(doc, "a", now=NOW)
    assert result["standing"] == "established"
    assert result["yield_at"] is None and result["stop_at"] is None
    assert result["message"] == "established account a"


def test_never_seen_label_is_treated_as_new():
    result = classify_account({}, "brand-new", now=NOW)
    assert result["standing"] == "new"
    assert result["age_days"] is None


def test_the_band_edge_at_exactly_7_days_is_established_not_new():
    doc = {"a": {"first_seen_ts": _iso(NOW - timedelta(days=7)), "windows_seen": [1, 2, 3]}}
    result = classify_account(doc, "a", now=NOW)
    assert result["standing"] == "established"


def test_the_band_edge_just_under_7_days_is_new():
    doc = {"a": {"first_seen_ts": _iso(NOW - timedelta(days=7) + timedelta(seconds=1)), "windows_seen": [1, 2, 3]}}
    result = classify_account(doc, "a", now=NOW)
    assert result["standing"] == "new"


def test_the_window_count_edge_at_exactly_3_is_enough():
    doc = {"a": {"first_seen_ts": _iso(NOW - timedelta(days=30)), "windows_seen": [1, 2, 3]}}
    result = classify_account(doc, "a", now=NOW)
    assert result["standing"] == "established"


def test_stricter_bands_are_the_documented_60_and_75():
    doc = {"a": {"first_seen_ts": _iso(NOW), "windows_seen": []}}
    result = classify_account(doc, "a", now=NOW)
    assert result["yield_at"] == 60.0
    assert result["stop_at"] == 75.0


# --- N9: a pure band(pct, classification) -> GO/YIELD/STOP -----------------------------------------------------------


def test_band_edges_for_a_new_account_59_9_60_74_9_75():
    from trialerror.quota.accounts import band

    new_account = {"standing": "new", "yield_at": 60.0, "stop_at": 75.0}
    assert band(59.9, new_account) == "GO"
    assert band(60.0, new_account) == "YIELD"
    assert band(74.9, new_account) == "YIELD"
    assert band(75.0, new_account) == "STOP"


def test_band_is_none_for_established_and_unlabelled_accounts():
    from trialerror.quota.accounts import band

    established = {"standing": "established", "yield_at": None, "stop_at": None}
    unlabelled = {"standing": "unlabelled", "yield_at": None, "stop_at": None}
    assert band(99.0, established) is None
    assert band(99.0, unlabelled) is None


def test_band_composes_directly_with_classify_account():
    from trialerror.quota.accounts import band

    doc = {"a": {"first_seen_ts": _iso(NOW), "windows_seen": []}}
    classification = classify_account(doc, "a", now=NOW)
    assert band(60.0, classification) == "YIELD"
    assert band(10.0, classification) == "GO"
