"""Lane FB-acq item 2: what ``trialerror.litapi.providers.base`` does under HTTP
429 -- ``Retry-After`` parsing, the bounded backoff, and the cross-invocation
pacing stamp.

No real sleeps anywhere: every limiter is built with a recording ``_sleep_fn``
and every clock is a fake, so the numbers asserted below are the numbers the
code would have slept rather than time this suite actually spends.
"""

from __future__ import annotations

import json

import pytest

from trialerror.litapi.errors import ProviderTransportError
from trialerror.litapi.providers.base import (
    DEFAULT_MAX_TOTAL_WAIT_S,
    MAX_BLOCK_S,
    RateLimiter,
    get_with_retry,
    parse_retry_after,
)
from trialerror.litapi.transport import TransportResponse


class _ScriptedTransport:
    """One response per call from a fixed script, repeating the last entry."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, *, headers=None, timeout_s=None):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def _rate_limited(retry_after: str | None = None) -> TransportResponse:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return TransportResponse(status_code=429, json_body={"error": "rate limited"}, headers=headers)


def _limiter(min_interval_s: float = 0.0, **kw) -> tuple[RateLimiter, list[float]]:
    slept: list[float] = []
    return RateLimiter(min_interval_s, _sleep_fn=slept.append, **kw), slept


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Retry-After": "2"}, 2.0),
        ({"retry-after": "2"}, 2.0),  # header names are case-insensitive
        ({"RETRY-AFTER": " 0.5 "}, 0.5),
        ({"Retry-After": "-5"}, 0.0),  # a negative wait is "you may retry now"
        ({"Retry-After": "soon"}, None),
        ({"Retry-After": ""}, None),
        ({}, None),
        (None, None),
        ({"X-Other": "3"}, None),
    ],
)
def test_parse_retry_after_forms(headers, expected):
    assert parse_retry_after(headers) == expected


def test_parse_retry_after_accepts_an_http_date():
    # 30 seconds after the fake "now" this is parsed against.
    now = 1_600_000_000.0
    assert parse_retry_after({"Retry-After": "Sun, 13 Sep 2020 12:27:10 GMT"}, _now_fn=lambda: now) == pytest.approx(
        30.0, abs=1.0
    )


def test_parse_retry_after_http_date_in_the_past_is_zero_not_negative():
    assert parse_retry_after(
        {"Retry-After": "Sun, 13 Sep 2020 12:26:40 GMT"}, _now_fn=lambda: 1_900_000_000.0
    ) == 0.0


# ---------------------------------------------------------------------------
# get_with_retry under 429
# ---------------------------------------------------------------------------


def test_a_429_with_a_retry_after_is_retried_once_and_succeeds():
    """FAILS BEFORE this lane: 429 was in no provider's ``retry_on_status``,
    and ``Retry-After`` was never read although the header was right there on
    the response."""
    transport = _ScriptedTransport([_rate_limited("2"), TransportResponse(status_code=200, json_body={"ok": True})])
    limiter, slept = _limiter()
    stats: dict = {}

    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=limiter, retry_attempts=3, retry_on_status=(429, 500), stats=stats,
    )

    assert response.status_code == 200
    assert slept == [2.0]  # the provider's own number, not a guess
    assert stats["attempts"] == 2
    assert stats["waited_s"] == 2.0
    assert stats["last_status"] == 200
    assert stats["request_sent"] is True


def test_a_retry_after_larger_than_the_budget_is_never_slept():
    """The stated hard cap. A provider asking for two minutes gets an answer
    now, not a two-minute sleep -- and the caller is told how long it asked
    for."""
    transport = _ScriptedTransport([_rate_limited("120")])
    limiter, slept = _limiter()
    stats: dict = {}

    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=limiter, retry_attempts=3, retry_on_status=(429,), stats=stats,
    )

    assert slept == []
    assert transport.calls == 1
    assert stats["retry_after_s"] == 120.0

    # and the caller's own raise_for_transport_error carries it onward.
    from trialerror.litapi.providers.base import raise_for_transport_error

    with pytest.raises(ProviderTransportError) as caught:
        raise_for_transport_error(response, provider="test", context="lookup")
    assert caught.value.status_code == 429
    assert caught.value.retry_after_s == 120.0


def test_a_garbage_retry_after_falls_back_to_exponential_backoff():
    transport = _ScriptedTransport([_rate_limited("whenever")])
    limiter, slept = _limiter()

    get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=limiter, retry_attempts=4, retry_on_status=(429,),
    )

    assert slept == [1.0, 2.0, 4.0]
    assert transport.calls == 4


@pytest.mark.parametrize("attempts", [2, 5, 10, 40])
def test_total_backoff_never_exceeds_the_budget_however_many_429s(attempts):
    """The property, not one example: for ANY sequence of 429s the recorded
    backoff sleeps sum to at most the cap."""
    transport = _ScriptedTransport([_rate_limited("whenever")])
    limiter, slept = _limiter()

    get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=limiter, retry_attempts=attempts, retry_on_status=(429,),
    )

    assert sum(slept) <= DEFAULT_MAX_TOTAL_WAIT_S


def test_an_explicit_retry_on_status_still_wins_verbatim():
    """A program that wrote ``retry_on_status = [500]`` in its own
    trialerror.toml asked for 429 NOT to be retried, and gets that."""
    transport = _ScriptedTransport([_rate_limited("2")])
    limiter, slept = _limiter()

    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=limiter, retry_attempts=3, retry_on_status=(500,),
    )

    assert response.status_code == 429
    assert transport.calls == 1
    assert slept == []


# ---------------------------------------------------------------------------
# the cross-invocation pacing stamp
# ---------------------------------------------------------------------------


def test_two_limiters_sharing_a_stamp_path_pace_across_processes(tmp_path):
    """The defect this closes: each limiter is one per provider INSTANCE, and
    every CLI invocation is a new process, so a shell loop of lookups hit a
    provider as fast as processes started."""
    stamp = tmp_path / "pacing" / "test.json"
    wall = {"t": 1000.0}
    first, first_slept = _limiter(3.0, stamp_path=stamp, _wall_fn=lambda: wall["t"])
    first.wait()
    assert first_slept == []
    assert stamp.is_file()

    wall["t"] = 1001.0  # one second later, a NEW process (a fresh limiter)
    second, second_slept = _limiter(3.0, stamp_path=stamp, _wall_fn=lambda: wall["t"])
    second.wait()

    assert second_slept == [2.0]  # the remainder of the 3-second interval


def test_a_corrupt_or_absent_stamp_file_is_treated_as_empty(tmp_path):
    stamp = tmp_path / "test.json"
    stamp.write_text("{not json at all", encoding="utf-8")
    limiter, slept = _limiter(3.0, stamp_path=stamp, _wall_fn=lambda: 1000.0)

    limiter.wait()  # must not raise, must not sleep

    assert slept == []
    assert json.loads(stamp.read_text(encoding="utf-8"))["last_request_ts"] == 1000.0


def test_a_recorded_block_refuses_without_sending_a_request(tmp_path):
    stamp = tmp_path / "test.json"
    wall = {"t": 1000.0}
    recorder, _ = _limiter(0.0, stamp_path=stamp, _wall_fn=lambda: wall["t"])
    recorder.note_blocked(120.0)

    transport = _ScriptedTransport([TransportResponse(status_code=200)])
    fresh, slept = _limiter(0.0, stamp_path=stamp, _wall_fn=lambda: wall["t"])
    stats: dict = {}

    with pytest.raises(ProviderTransportError) as caught:
        get_with_retry(
            transport, "http://x", provider="test", headers={}, timeout_s=1.0,
            rate_limiter=fresh, retry_attempts=3, retry_on_status=(429,), stats=stats,
        )

    assert "no request sent" in str(caught.value)
    assert caught.value.status_code == 429
    assert caught.value.retry_after_s == pytest.approx(120.0)
    assert transport.calls == 0
    assert slept == []
    assert stats["request_sent"] is False


def test_a_short_recorded_block_is_slept_out_and_the_request_goes_through(tmp_path):
    stamp = tmp_path / "test.json"
    wall = {"t": 1000.0}
    recorder, _ = _limiter(0.0, stamp_path=stamp, _wall_fn=lambda: wall["t"])
    recorder.note_blocked(5.0)

    transport = _ScriptedTransport([TransportResponse(status_code=200, json_body={"ok": True})])
    fresh, slept = _limiter(0.0, stamp_path=stamp, _wall_fn=lambda: wall["t"])

    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=fresh, retry_attempts=3, retry_on_status=(429,),
    )

    assert response.status_code == 200
    assert slept == [pytest.approx(5.0)]
    assert transport.calls == 1


def test_note_blocked_clamps_to_the_maximum(tmp_path):
    stamp = tmp_path / "test.json"
    limiter, _ = _limiter(0.0, stamp_path=stamp, _wall_fn=lambda: 1000.0)

    limiter.note_blocked(86_400.0)  # a provider asking for a whole day

    assert json.loads(stamp.read_text(encoding="utf-8"))["blocked_until_ts"] == 1000.0 + MAX_BLOCK_S
    assert limiter.blocked_remaining_s() == MAX_BLOCK_S


def test_giving_up_on_a_429_records_the_block_for_the_next_invocation(tmp_path):
    stamp = tmp_path / "test.json"
    transport = _ScriptedTransport([_rate_limited("120")])
    limiter, _ = _limiter(0.0, stamp_path=stamp, _wall_fn=lambda: 1000.0)

    get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=limiter, retry_attempts=3, retry_on_status=(429,),
    )

    assert json.loads(stamp.read_text(encoding="utf-8"))["blocked_until_ts"] == 1120.0


def test_without_a_stamp_path_nothing_is_written_and_nothing_blocks(tmp_path):
    limiter, _ = _limiter(0.0)
    limiter.note_blocked(120.0)

    assert limiter.blocked_remaining_s() == 0.0
    assert list(tmp_path.iterdir()) == []
