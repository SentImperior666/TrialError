"""Tests for :class:`trialerror.litapi.providers.arxivxplorer_web.ArxivxplorerWebProvider`
against the canned fixture (``tests/fixtures/litapi/arxivxplorer_search_hit.json``)
via :class:`~trialerror.litapi.transport.FakeTransport` -- NO live network call
anywhere in this file (same offline-testability discipline as every other
``trialerror.litapi`` test module; this module's own docstring is explicit that
the LIVE site was inspected only via an interactive browser session, never
from inside this test suite, and never will be -- C-0069's guardrails are
about what the SHIPPED client does, not about tests touching the real
service). Also covers the C-0069-specific guardrails this provider adds
beyond every other provider in this package: disabled-by-default refusal,
the sqlite response cache (+ TTL expiry), the daily request cap, and
exponential backoff on retry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from trialerror.litapi.config import ArxivxplorerConfig
from trialerror.litapi.errors import ProviderConfigError, ProviderTransportError, ProviderUnsupportedOperationError
from trialerror.litapi.providers.arxivxplorer_web import (
    USER_AGENT,
    ArxivxplorerDailyCapExceededError,
    ArxivxplorerWebProvider,
    ensure_cache_schema,
)
from trialerror.litapi.transport import FakeTransport, TransportResponse
from tests._litapi_fixtures import load_text_fixture

BASE = "https://search.arxivxplorer.com"


def _cfg(**overrides) -> ArxivxplorerConfig:
    kwargs = dict(
        enabled=True, base_url=BASE, min_interval_s=0.0, daily_request_cap=200,
        cache_ttl_s=86400, timeout_s=5.0, retry_attempts=1,
    )
    kwargs.update(overrides)
    return ArxivxplorerConfig(**kwargs)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def _search_url(query: str, *, categories: list[str] | None = None, year: int | None = None) -> str:
    params: list[tuple[str, str]] = [("q", query)]
    for cat in categories or ():
        params.append(("cats", cat))
    if year is not None:
        params.append(("year", str(year)))
    return f"{BASE}/?{urlencode(params)}"


def _json_response(text: str, *, status_code: int = 200) -> TransportResponse:
    return TransportResponse(status_code=status_code, json_body=None, text=text, headers={"content-type": "application/json"})


# ---------------------------------------------------------------------------
# construction: disabled-by-default refusal (C-0069 guardrail 4)
# ---------------------------------------------------------------------------


def test_construction_refuses_when_disabled_with_zero_network_calls():
    transport = FakeTransport()
    with pytest.raises(ProviderConfigError, match="enabled"):
        ArxivxplorerWebProvider(transport, _cfg(enabled=False), cache_conn=_conn())
    assert transport.calls == []


def test_construction_succeeds_when_enabled_and_creates_cache_schema():
    conn = _conn()
    ArxivxplorerWebProvider(FakeTransport(), _cfg(enabled=True), cache_conn=conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"arxivxplorer_cache", "arxivxplorer_request_log"} <= tables


def test_ensure_cache_schema_is_idempotent():
    conn = _conn()
    ensure_cache_schema(conn)
    ensure_cache_schema(conn)  # must not raise on a second call


# ---------------------------------------------------------------------------
# search: field mapping, honest UA, url building with filters
# ---------------------------------------------------------------------------


def test_search_maps_fields_including_non_arxiv_journal_gets_no_guessed_url():
    transport = FakeTransport()
    url = _search_url("ludic rule engines")
    transport.add_response(url, _json_response(load_text_fixture("arxivxplorer_search_hit.json")))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    records = provider.search("ludic rule engines")

    assert len(records) == 2
    arxiv_rec = records[0]
    assert arxiv_rec.title == "A Fixture Paper About Ludic Rule Engines"
    assert arxiv_rec.arxiv_id == "2101.02120"
    assert arxiv_rec.authors == ["Ada Fixture", "Bo Canned", "Cy Third"]
    assert arxiv_rec.year == 2021
    assert arxiv_rec.url == "https://arxiv.org/abs/2101.02120"
    assert arxiv_rec.oa_pdf_url == "https://arxiv.org/pdf/2101.02120"
    assert arxiv_rec.doi is None
    assert arxiv_rec.citation_count is None
    assert arxiv_rec.external_ids == {"arxivxplorer": "2101.02120"}
    assert arxiv_rec.other["journal"] == "arxiv"
    assert arxiv_rec.other["categories"] == ["cs.AI", "cs.LG"]

    biorxiv_rec = records[1]
    assert biorxiv_rec.arxiv_id is None
    assert biorxiv_rec.url is None  # never guessed for a non-arxiv journal (module docstring)
    assert biorxiv_rec.oa_pdf_url is None
    assert biorxiv_rec.authors == ["Dee Fourth"]
    assert biorxiv_rec.other["journal"] == "biorxiv"


def test_search_respects_limit():
    transport = FakeTransport()
    url = _search_url("ludic rule engines")
    transport.add_response(url, _json_response(load_text_fixture("arxivxplorer_search_hit.json")))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    records = provider.search("ludic rule engines", limit=1)

    assert len(records) == 1


def test_search_builds_url_with_repeated_cats_and_year_in_recovered_order():
    transport = FakeTransport()
    url = _search_url("q", categories=["cs.LG", "cs.AI"], year=2022)
    transport.add_response(url, _json_response("[]"))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    provider.search("q", categories=["cs.LG", "cs.AI"], year=2022)

    assert transport.calls[0]["url"] == url
    assert url.endswith("q=q&cats=cs.LG&cats=cs.AI&year=2022")


def test_search_sends_honest_non_spoofed_user_agent():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("[]"))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    provider.search("q")

    ua = transport.calls[0]["headers"]["User-Agent"]
    assert ua == USER_AGENT
    assert "Mozilla" not in ua and "Chrome" not in ua  # never a spoofed browser UA (guardrail 3)
    assert "trialerror" in ua.lower()


# ---------------------------------------------------------------------------
# sqlite response cache (C-0069 guardrail 2)
# ---------------------------------------------------------------------------


def test_second_identical_search_hits_cache_not_network():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("[]"))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    provider.search("q")
    provider.search("q")
    provider.search("q")

    assert len(transport.calls) == 1


def test_cache_entry_expires_after_ttl_and_refetches():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("[]"))

    clock = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    provider = ArxivxplorerWebProvider(
        transport, _cfg(cache_ttl_s=60), cache_conn=_conn(), _now_fn=lambda: clock["t"]
    )

    provider.search("q")
    assert len(transport.calls) == 1

    clock["t"] += timedelta(seconds=30)  # inside TTL -- still a cache hit
    provider.search("q")
    assert len(transport.calls) == 1

    clock["t"] += timedelta(seconds=61)  # now past the 60s TTL -- must refetch
    provider.search("q")
    assert len(transport.calls) == 2


# ---------------------------------------------------------------------------
# daily request cap (C-0069 guardrail 2) -- cache hits are exempt
# ---------------------------------------------------------------------------


def test_daily_cap_refuses_new_network_requests_once_reached():
    transport = FakeTransport()
    url1, url2 = _search_url("q1"), _search_url("q2")
    transport.add_response(url1, _json_response("[]"))
    transport.add_response(url2, _json_response("[]"))
    provider = ArxivxplorerWebProvider(transport, _cfg(daily_request_cap=1), cache_conn=_conn())

    provider.search("q1")
    with pytest.raises(ArxivxplorerDailyCapExceededError, match="daily request cap"):
        provider.search("q2")


def test_daily_cap_does_not_count_cache_hits():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("[]"))
    provider = ArxivxplorerWebProvider(transport, _cfg(daily_request_cap=1), cache_conn=_conn())

    provider.search("q")  # the 1 real request the cap allows
    for _ in range(5):
        provider.search("q")  # every one of these is a cache hit -- must never raise

    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# exponential backoff on retry (C-0069 guardrail 5)
# ---------------------------------------------------------------------------


class _FlakyTwiceThenOkTransport:
    """In-file fake: 503 on the first two attempts, 200 on the third --
    proves search() SUCCEEDS after backoff-and-retry, not just that it
    eventually gives up."""

    def __init__(self, ok_response: TransportResponse):
        self.ok_response = ok_response
        self.calls: list[dict] = []

    def get(self, url, *, headers=None, timeout_s=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if len(self.calls) < 3:
            return TransportResponse(status_code=503, json_body=None, text="")
        return self.ok_response


def test_exponential_backoff_doubles_and_eventually_succeeds():
    ok = _json_response("[]")
    transport = _FlakyTwiceThenOkTransport(ok)
    sleeps: list[float] = []
    provider = ArxivxplorerWebProvider(
        transport, _cfg(retry_attempts=3), cache_conn=_conn(), _sleep_fn=lambda s: sleeps.append(s)
    )

    records = provider.search("q")

    assert records == []
    assert len(transport.calls) == 3
    assert sleeps == [1.0, 2.0]  # doubling from a 1s base, one sleep between each of the 3 attempts


def test_exhausted_retries_raise_transport_error():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("", status_code=503))
    provider = ArxivxplorerWebProvider(
        transport, _cfg(retry_attempts=2), cache_conn=_conn(), _sleep_fn=lambda s: None
    )

    with pytest.raises(ProviderTransportError, match="503"):
        provider.search("q")


# ---------------------------------------------------------------------------
# malformed response body
# ---------------------------------------------------------------------------


def test_non_json_body_raises_transport_error():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("not json at all"))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    with pytest.raises(ProviderTransportError, match="JSON"):
        provider.search("q")


def test_json_object_instead_of_array_raises_transport_error():
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response('{"not": "an array"}'))
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    with pytest.raises(ProviderTransportError, match="array"):
        provider.search("q")


# ---------------------------------------------------------------------------
# unsupported operations -- honest stubs, zero network calls (module docstring)
# ---------------------------------------------------------------------------


def test_get_by_doi_unsupported_with_zero_network_calls():
    transport = FakeTransport()
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_by_doi("10.1234/whatever")

    assert transport.calls == []


def test_get_by_arxiv_unsupported_with_zero_network_calls():
    transport = FakeTransport()
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_by_arxiv("2101.02120")

    assert transport.calls == []


def test_get_citations_always_unsupported_with_zero_network_calls():
    transport = FakeTransport()
    provider = ArxivxplorerWebProvider(transport, _cfg(), cache_conn=_conn())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_citations("2101.02120")

    assert transport.calls == []


# ---------------------------------------------------------------------------
# rate limiter wiring (>= 3s floor, C-0069 guardrail 2) -- fake-clock, no real sleeping
# ---------------------------------------------------------------------------


def test_rate_limiter_is_wired_at_the_configured_interval():
    from trialerror.litapi.config import load_litapi_config

    cfg = load_litapi_config({})
    assert cfg.arxivxplorer.min_interval_s == 3.0  # C-0069's own ">=3s spacing" floor, as the default


def test_provider_paces_successive_calls_through_its_own_rate_limiter(monkeypatch):
    transport = FakeTransport()
    url = _search_url("q")
    transport.add_response(url, _json_response("[]"))
    provider = ArxivxplorerWebProvider(transport, _cfg(min_interval_s=3.0, cache_ttl_s=0), cache_conn=_conn())

    wait_calls = []
    monkeypatch.setattr(provider._rate_limiter, "wait", lambda: wait_calls.append(1))

    provider.search("q")
    # cache_ttl_s=0 still returns the just-cached entry within the same wall-clock instant on some
    # clocks; force a second real network attempt by clearing the cache row instead of relying on TTL.
    provider.cache_conn.execute("DELETE FROM arxivxplorer_cache")
    provider.search("q")

    assert len(wait_calls) == 2
