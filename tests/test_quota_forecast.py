"""Tests for ``trialerror quota forecast`` (design ``L4_quota-policy.md``
Section 2.4 / Section 3 item 4): :mod:`trialerror.quota.forecast`."""

from __future__ import annotations

import pytest

from trialerror.quota.forecast import (
    PriceMissingError,
    forecast_from_usd,
    parse_tokens_spec,
    price_for_model,
    remaining_points,
    tokens_to_usd,
)

RATE_ROW = {"window": "five_hour", "points_per_usd": 0.4, "ci_low": 0.35, "ci_high": 0.45,
            "fitted_ts": "2026-09-27T00:00:00Z", "method": "ratio"}

FIXTURE_PRICES = {"quota": {"prices": {
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.3},
}}}


def test_forecast_from_usd_applies_the_rate_and_its_interval():
    result = forecast_from_usd(10.0, RATE_ROW)
    assert result["points"] == pytest.approx(4.0)
    assert result["points_ci_low"] == pytest.approx(3.5)
    assert result["points_ci_high"] == pytest.approx(4.5)


def test_forecast_from_usd_without_an_interval_reports_none():
    row = dict(RATE_ROW, ci_low=None, ci_high=None)
    result = forecast_from_usd(5.0, row)
    assert result["points_ci_low"] is None and result["points_ci_high"] is None


def test_parse_tokens_spec_reads_model_type_n_pairs():
    entries = parse_tokens_spec("claude-sonnet-5:input=1000000,claude-sonnet-5:output=200000")
    assert entries == [("claude-sonnet-5", "input", 1_000_000), ("claude-sonnet-5", "output", 200_000)]


def test_parse_tokens_spec_refuses_a_malformed_entry():
    with pytest.raises(ValueError, match="MODEL:TYPE=N"):
        parse_tokens_spec("claude-sonnet-5-input-1000")


def test_parse_tokens_spec_refuses_an_unknown_type():
    with pytest.raises(ValueError, match="TYPE must be one of"):
        parse_tokens_spec("claude-sonnet-5:bogus=1000")


def test_price_for_model_reads_the_fixture_table():
    assert price_for_model(FIXTURE_PRICES, "claude-sonnet-5", "input") == 3.0
    assert price_for_model(FIXTURE_PRICES, "claude-sonnet-5", "output") == 15.0
    assert price_for_model(FIXTURE_PRICES, "unknown-model", "input") is None
    assert price_for_model(None, "claude-sonnet-5", "input") is None


def test_tokens_to_usd_with_a_fixture_price_table():
    entries = parse_tokens_spec("claude-sonnet-5:input=1000000,claude-sonnet-5:output=100000")
    usd = tokens_to_usd(entries, FIXTURE_PRICES)
    assert usd == pytest.approx(3.0 + 1.5)  # 1M input @ $3/M + 0.1M output @ $15/M


def test_tokens_to_usd_refuses_by_name_with_no_price_for_model():
    entries = parse_tokens_spec("no-such-model:input=1000")
    with pytest.raises(PriceMissingError) as excinfo:
        tokens_to_usd(entries, FIXTURE_PRICES)
    assert "no-such-model" in str(excinfo.value)
    assert excinfo.value.model == "no-such-model"


def test_remaining_points_before_yield_and_operator_stop():
    result = remaining_points(60.0)
    assert result["to_yield_80"] == pytest.approx(20.0)
    assert result["to_operator_95"] == pytest.approx(35.0)


def test_remaining_points_is_negative_once_a_band_is_crossed():
    result = remaining_points(90.0)
    assert result["to_yield_80"] < 0
    assert result["to_operator_95"] > 0


def test_remaining_points_with_no_current_reading():
    result = remaining_points(None)
    assert result == {"to_yield_80": None, "to_operator_95": None}
