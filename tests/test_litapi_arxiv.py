"""Tests for :class:`trialerror.litapi.providers.arxiv.ArxivProvider` against
the canned Atom XML fixtures (``tests/fixtures/litapi/arxiv_feed_*.xml``)
via :class:`~trialerror.litapi.transport.FakeTransport` -- NO live network call
anywhere in this file (same offline-testability discipline as
``tests/test_litapi_providers.py``). Also covers the throttle behavior
(1 req/3s) via :class:`~trialerror.litapi.providers.base.RateLimiter`'s own
fake-clock seams."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderNotFoundError, ProviderUnsupportedOperationError
from trialerror.litapi.providers.arxiv import ArxivProvider
from trialerror.litapi.transport import FakeTransport, TransportResponse
from tests._litapi_fixtures import load_text_fixture

ARXIV_BASE = "https://export.arxiv.org/api"  # matches config.py's real (https) default


def _arxiv_cfg(**overrides) -> ProviderApiConfig:
    kwargs = dict(
        name="arxiv", base_url=ARXIV_BASE, mailto=None, api_key_path=None,
        api_key_header="x-api-key", min_interval_s=0.0, retry_attempts=1, retry_on_status=(500, 503), timeout_s=5.0,
    )
    kwargs.update(overrides)
    return ProviderApiConfig(**kwargs)


def _id_list_url(arxiv_id: str) -> str:
    return f"{ARXIV_BASE}/query?{urlencode({'id_list': arxiv_id})}"


def _search_url(query: str, *, start: int = 0, max_results: int = 10) -> str:
    q = {"search_query": f"all:{query}", "start": str(start), "max_results": str(max_results)}
    return f"{ARXIV_BASE}/query?{urlencode(q)}"


def _xml_response(text: str) -> TransportResponse:
    return TransportResponse(status_code=200, json_body=None, text=text)


# ---------------------------------------------------------------------------
# get_by_arxiv
# ---------------------------------------------------------------------------


def test_get_by_arxiv_hit():
    transport = FakeTransport()
    transport.add_response(_id_list_url("2101.00001"), _xml_response(load_text_fixture("arxiv_feed_hit.xml")))
    provider = ArxivProvider(transport, _arxiv_cfg())

    record = provider.get_by_arxiv("arXiv:2101.00001")

    assert record.title == "A Fixture Paper About Distributed Systems Metadata Reconciliation"
    assert record.arxiv_id == "2101.00001"
    assert record.doi == "10.1234/fixture.5678"
    assert record.authors == ["Ada Fixture", "Bo Canned"]
    assert record.year == 2021
    assert record.venue == "Journal of Fixture Studies 12, 34 (2021)"
    assert record.oa_pdf_url == "http://arxiv.org/pdf/2101.00001v2"
    assert record.abstract.startswith("This is a canned arXiv abstract")
    assert record.citation_count is None


def test_get_by_arxiv_falls_back_to_stable_pdf_url_when_feed_omits_pdf_link():
    transport = FakeTransport()
    transport.add_response(_id_list_url("2205.00002"), _xml_response(load_text_fixture("arxiv_feed_no_pdf_link.xml")))
    provider = ArxivProvider(transport, _arxiv_cfg())

    record = provider.get_by_arxiv("2205.00002")

    assert record.oa_pdf_url == "https://arxiv.org/pdf/2205.00002"
    assert record.doi is None


def test_get_by_arxiv_not_found_raises_on_arxiv_error_entry_shape():
    """arXiv returns HTTP 200 (not 404) for an unknown id -- a single
    error-shaped <entry> inside an otherwise normal feed. See
    trialerror.litapi.providers.arxiv's own module docstring TRIALERROR-DEV-NOTE."""
    transport = FakeTransport()
    transport.add_response(_id_list_url("9999.99999"), _xml_response(load_text_fixture("arxiv_feed_not_found.xml")))
    provider = ArxivProvider(transport, _arxiv_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_arxiv("9999.99999")


def test_get_by_arxiv_empty_or_invalid_id_returns_none_with_no_network_call():
    transport = FakeTransport()  # no routes registered -- a call would raise TransportNotConfiguredError
    provider = ArxivProvider(transport, _arxiv_cfg())

    assert provider.get_by_arxiv("") is None
    assert transport.calls == []


# ---------------------------------------------------------------------------
# get_by_doi (only succeeds for arXiv's own synthesized DOI form)
# ---------------------------------------------------------------------------


def test_get_by_doi_resolves_via_arxiv_derived_doi():
    transport = FakeTransport()
    transport.add_response(_id_list_url("2101.00001"), _xml_response(load_text_fixture("arxiv_feed_hit.xml")))
    provider = ArxivProvider(transport, _arxiv_cfg())

    record = provider.get_by_doi("10.48550/arXiv.2101.00001")

    assert record.arxiv_id == "2101.00001"


def test_get_by_doi_non_arxiv_doi_raises_not_found_with_zero_network_calls():
    transport = FakeTransport()
    provider = ArxivProvider(transport, _arxiv_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_doi("10.1234/a-real-journal-doi")

    assert transport.calls == []


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_returns_records_skipping_error_entries():
    transport = FakeTransport()
    transport.add_response(
        _search_url("distributed systems", max_results=2), _xml_response(load_text_fixture("arxiv_feed_search.xml"))
    )
    provider = ArxivProvider(transport, _arxiv_cfg())

    records = provider.search("distributed systems", limit=2)

    assert len(records) == 2
    assert records[0].title == "A Fixture Search Hit About Distributed Systems, One"
    assert records[1].arxiv_id == "2301.00002"


def test_search_truncates_to_limit():
    transport = FakeTransport()
    transport.add_response(
        _search_url("distributed systems", max_results=1), _xml_response(load_text_fixture("arxiv_feed_search.xml"))
    )
    provider = ArxivProvider(transport, _arxiv_cfg())

    records = provider.search("distributed systems", limit=1)

    assert len(records) == 1


# ---------------------------------------------------------------------------
# get_citations -- unconditionally unsupported
# ---------------------------------------------------------------------------


def test_get_citations_always_raises_unsupported_with_zero_network_calls():
    transport = FakeTransport()
    provider = ArxivProvider(transport, _arxiv_cfg())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_citations("2101.00001")

    assert transport.calls == []


# ---------------------------------------------------------------------------
# throttle behavior (1 req/3s ToU, fake clock -- no real sleeping in tests)
# ---------------------------------------------------------------------------


def test_rate_limiter_enforces_configured_min_interval_via_fake_clock():
    from trialerror.litapi.providers.base import RateLimiter

    times = iter([100.0, 100.5, 103.0])  # 0.5s apart -- below the 3.0s floor; 3rd value read post-sleep
    sleeps: list[float] = []
    limiter = RateLimiter(3.0, _time_fn=lambda: next(times), _sleep_fn=lambda s: sleeps.append(s))

    limiter.wait()
    limiter.wait()

    assert sleeps == [pytest.approx(2.5)]


def test_arxiv_config_default_min_interval_is_3_seconds():
    from trialerror.litapi.config import load_litapi_config

    cfg = load_litapi_config({})
    assert cfg.arxiv.min_interval_s == 3.0


def test_provider_paces_successive_calls_through_its_own_rate_limiter(monkeypatch):
    """End-to-end (still offline): two get_by_arxiv calls through the SAME
    provider instance actually invoke the configured RateLimiter's wait()
    each time -- proves the throttle is wired in, not just configured."""
    transport = FakeTransport()
    transport.add_response(_id_list_url("2101.00001"), _xml_response(load_text_fixture("arxiv_feed_hit.xml")))
    provider = ArxivProvider(transport, _arxiv_cfg(min_interval_s=3.0))

    wait_calls = []
    monkeypatch.setattr(provider._rate_limiter, "wait", lambda: wait_calls.append(1))

    provider.get_by_arxiv("2101.00001")
    provider.get_by_arxiv("2101.00001")

    assert len(wait_calls) == 2


# ---------------------------------------------------------------------------
# lane FB-acq item 4 (F16): get_license -- the paper's OWN licence grant
# ---------------------------------------------------------------------------

OAI_BASE = "https://export.arxiv.org/oai2"


def _oai_cfg(**overrides) -> ProviderApiConfig:
    kwargs = {"oai_base_url": OAI_BASE, "license_lookup": True}
    kwargs.update(overrides)
    return _arxiv_cfg(**kwargs)


def _oai_url(arxiv_id: str) -> str:
    q = {"verb": "GetRecord", "identifier": f"oai:arXiv.org:{arxiv_id}", "metadataPrefix": "arXiv"}
    return f"{OAI_BASE}?{urlencode(q)}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://arxiv.org/licenses/nonexclusive-distrib/1.0/", "arxiv-nonexclusive-distrib-1.0"),
        ("https://arxiv.org/licenses/assumed-1991-2003", "arxiv-assumed-1991-2003"),
        ("https://creativecommons.org/licenses/by/4.0/", "cc-by-4.0"),
        ("http://creativecommons.org/licenses/by-sa/4.0", "cc-by-sa-4.0"),
        ("https://creativecommons.org/licenses/by-nc-sa/4.0/", "cc-by-nc-sa-4.0"),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "cc-by-nc-nd-4.0"),
        ("https://creativecommons.org/licenses/by/3.0/", "cc-by-3.0"),
        ("https://creativecommons.org/licenses/by-nc-sa/3.0/", "cc-by-nc-sa-3.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0-1.0"),
        # an unknown licence is still a licence: kept as itself, not dropped
        ("https://example.org/licenses/house-terms/2.0/", "example.org/licenses/house-terms/2.0"),
        ("  HTTPS://Creativecommons.ORG/licenses/BY/4.0/  ", "cc-by-4.0"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_license_url_tokens(raw, expected):
    from trialerror.litapi.providers.arxiv import normalize_license_url

    assert normalize_license_url(raw) == expected


def test_get_license_reads_the_oai_record_and_strips_the_version_suffix():
    transport = FakeTransport()
    transport.add_response(
        _id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    transport.add_response(
        _oai_url("2101.00001"),  # the id_list feed reports 2101.00001v2; OAI ids carry no version
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_oai_license.xml")),
    )
    provider = ArxivProvider(transport, _oai_cfg())
    record = provider.get_by_arxiv("2101.00001")

    assert provider.get_license(record) == ("arxiv-nonexclusive-distrib-1.0", "arxiv_oai")
    assert provider.get_license("2101.00001v2") == ("arxiv-nonexclusive-distrib-1.0", "arxiv_oai")
    assert [c["headers"]["Accept"] for c in transport.calls if "oai2" in c["url"]] == [
        "application/xml, text/xml;q=0.9, */*;q=0.8"
    ] * 2


def test_get_license_reports_none_reported_for_a_well_formed_answer_without_one():
    transport = FakeTransport()
    transport.add_response(
        _oai_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_oai_no_license.xml")),
    )
    provider = ArxivProvider(transport, _oai_cfg())

    assert provider.get_license("2101.00001") == (None, "none_reported")


def test_get_license_reports_none_reported_for_an_oai_error_element():
    transport = FakeTransport()
    transport.add_response(
        _oai_url("9999.99999"),
        TransportResponse(
            status_code=200, json_body=None,
            text='<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                 '<error code="idDoesNotExist">no such record</error></OAI-PMH>',
        ),
    )
    provider = ArxivProvider(transport, _oai_cfg())

    assert provider.get_license("9999.99999") == (None, "none_reported")


@pytest.mark.parametrize(
    "response,expected",
    [
        (TransportResponse(status_code=429, json_body=None, text=""), "lookup_failed:rate_limited"),
        (TransportResponse(status_code=500, json_body=None, text=""), "lookup_failed:http_error"),
        (TransportResponse(status_code=404, json_body=None, text=""), "lookup_failed:http_error"),
        (TransportResponse(status_code=200, json_body=None, text="<truncated"), "lookup_failed:provider_error"),
    ],
    ids=["429", "500", "404", "malformed-xml"],
)
def test_get_license_never_raises_and_says_which_failure_it_was(response, expected):
    transport = FakeTransport()
    transport.add_response(_oai_url("2101.00001"), response)
    provider = ArxivProvider(transport, _oai_cfg(retry_on_status=()))

    assert provider.get_license("2101.00001") == (None, expected)


def test_get_license_reports_an_unreachable_transport_without_raising():
    class _Unreachable:
        def get(self, url, *, headers=None, timeout_s=None):
            import urllib.error

            raise urllib.error.URLError("no route to host")

    provider = ArxivProvider(_Unreachable(), _oai_cfg())

    assert provider.get_license("2101.00001") == (None, "lookup_failed:transport_unreachable")


def test_get_license_reports_a_missing_fake_route_as_a_provider_error():
    provider = ArxivProvider(FakeTransport(), _oai_cfg())

    assert provider.get_license("2101.00001") == (None, "lookup_failed:provider_error")


def test_get_license_switched_off_makes_no_request_at_all():
    transport = FakeTransport()
    provider = ArxivProvider(transport, _oai_cfg(license_lookup=False))

    assert provider.get_license("2101.00001") == (None, "not_attempted")
    assert transport.calls == []


def test_get_license_prefers_a_licence_already_in_the_atom_entry():
    feed = load_text_fixture("arxiv_feed_hit.xml").replace(
        "</entry>", '<link rel="license" href="https://creativecommons.org/licenses/by/4.0/"/>\n  </entry>'
    )
    transport = FakeTransport()
    transport.add_response(_id_list_url("2101.00001"), TransportResponse(status_code=200, json_body=None, text=feed))
    provider = ArxivProvider(transport, _oai_cfg())
    record = provider.get_by_arxiv("2101.00001")

    assert record.other["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert provider.get_license(record) == ("cc-by-4.0", "arxiv_atom")
    assert not [c for c in transport.calls if "oai2" in c["url"]]


def test_an_arxiv_namespaced_license_element_is_read_too():
    feed = load_text_fixture("arxiv_feed_hit.xml").replace(
        "</entry>",
        '<arxiv:license xmlns:arxiv="http://arxiv.org/schemas/atom">'
        "http://arxiv.org/licenses/nonexclusive-distrib/1.0/</arxiv:license>\n  </entry>",
    )
    transport = FakeTransport()
    transport.add_response(_id_list_url("2101.00001"), TransportResponse(status_code=200, json_body=None, text=feed))
    provider = ArxivProvider(transport, _oai_cfg())

    record = provider.get_by_arxiv("2101.00001")

    assert provider.get_license(record) == ("arxiv-nonexclusive-distrib-1.0", "arxiv_atom")


def test_an_entry_without_a_licence_carries_no_license_url_key():
    transport = FakeTransport()
    transport.add_response(
        _id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    provider = ArxivProvider(transport, _oai_cfg())

    record = provider.get_by_arxiv("2101.00001")

    assert "license_url" not in record.other
    assert record.other["journal_ref"]  # the pre-existing key is untouched
