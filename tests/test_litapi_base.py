"""Tests for ``trialerror.litapi.providers.base``: :class:`RateLimiter` pacing
and :func:`get_with_retry`'s mining-report-grounded retry policy. Uses a
tiny in-file stub transport (NOT ``FakeTransport``, which is
single-response-per-URL) so a call can return a different status on each
attempt -- still fully offline, no network anywhere."""

from __future__ import annotations

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.providers.base import RateLimiter, build_headers, get_with_retry
from trialerror.litapi.transport import TransportResponse


class _ScriptedTransport:
    """Returns one ``TransportResponse`` per call from a fixed script,
    repeating the last entry once the script is exhausted."""

    def __init__(self, responses: list[TransportResponse]):
        self._responses = responses
        self.calls = 0

    def get(self, url, *, headers=None, timeout_s=None):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def _limiter(min_interval_s: float = 0.0) -> RateLimiter:
    return RateLimiter(min_interval_s)


def test_rate_limiter_first_call_never_sleeps():
    sleeps: list[float] = []
    limiter = RateLimiter(1.0, _time_fn=lambda: 100.0, _sleep_fn=sleeps.append)
    limiter.wait()
    assert sleeps == []


def test_rate_limiter_sleeps_for_the_remaining_interval():
    times = [0.0]

    def time_fn():
        return times[0]

    def sleep_fn(seconds):
        times[0] += seconds

    limiter = RateLimiter(1.0, _time_fn=time_fn, _sleep_fn=sleep_fn)
    limiter.wait()  # t=0, no previous call
    times[0] = 0.2  # only 0.2s elapsed before the next call
    limiter.wait()
    assert times[0] == 1.0  # slept the remaining 0.8s to reach the 1.0s floor


def test_rate_limiter_no_sleep_once_interval_already_elapsed():
    times = [0.0]
    sleeps: list[float] = []
    limiter = RateLimiter(1.0, _time_fn=lambda: times[0], _sleep_fn=sleeps.append)
    limiter.wait()
    times[0] = 5.0  # plenty of time has passed
    limiter.wait()
    assert sleeps == []


def test_rate_limiter_zero_interval_never_sleeps():
    sleeps: list[float] = []
    limiter = RateLimiter(0.0, _time_fn=lambda: 0.0, _sleep_fn=sleeps.append)
    limiter.wait()
    limiter.wait()
    assert sleeps == []


def test_get_with_retry_retries_on_configured_status_then_succeeds():
    transport = _ScriptedTransport(
        [TransportResponse(status_code=500), TransportResponse(status_code=200, json_body={"ok": True})]
    )
    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=_limiter(), retry_attempts=3, retry_on_status=(500,),
    )
    assert response.status_code == 200
    assert transport.calls == 2


def test_get_with_retry_stops_at_retry_budget():
    transport = _ScriptedTransport([TransportResponse(status_code=500)])
    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=_limiter(), retry_attempts=3, retry_on_status=(500,),
    )
    assert response.status_code == 500
    assert transport.calls == 3


def test_get_with_retry_does_not_retry_non_configured_status():
    transport = _ScriptedTransport([TransportResponse(status_code=404)])
    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=_limiter(), retry_attempts=3, retry_on_status=(500,),
    )
    assert response.status_code == 404
    assert transport.calls == 1


def test_get_with_retry_returns_immediately_on_first_success():
    transport = _ScriptedTransport([TransportResponse(status_code=200)])
    response = get_with_retry(
        transport, "http://x", provider="test", headers={}, timeout_s=1.0,
        rate_limiter=_limiter(), retry_attempts=5, retry_on_status=(500,),
    )
    assert response.status_code == 200
    assert transport.calls == 1


def test_build_headers_includes_api_key_under_configured_header_name():
    cfg = ProviderApiConfig(name="semanticscholar", base_url="https://x", api_key_header="x-my-key")
    headers = build_headers(cfg, "secret")
    assert headers == {"x-my-key": "secret"}


def test_build_headers_empty_when_no_api_key():
    cfg = ProviderApiConfig(name="openalex", base_url="https://x")
    assert build_headers(cfg, None) == {}
