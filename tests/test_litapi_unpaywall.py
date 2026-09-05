"""Tests for :class:`trialerror.litapi.providers.unpaywall.UnpaywallProvider`
against the canned JSON fixtures (``tests/fixtures/litapi/unpaywall_*.json``)
via :class:`~trialerror.litapi.transport.FakeTransport` -- NO live network call
anywhere in this file, same offline-testability discipline as
``tests/test_litapi_providers.py``."""

from __future__ import annotations

from urllib.parse import quote, urlencode

import pytest

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderConfigError, ProviderNotFoundError, ProviderUnsupportedOperationError
from trialerror.litapi.providers.unpaywall import UnpaywallProvider
from trialerror.litapi.transport import FakeTransport
from tests._litapi_fixtures import load_fixture

UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


def _unpaywall_cfg(**overrides) -> ProviderApiConfig:
    kwargs = dict(
        name="unpaywall", base_url=UNPAYWALL_BASE, mailto="me@example.org", api_key_path=None,
        api_key_header="x-api-key", min_interval_s=0.0, retry_attempts=1, retry_on_status=(500,), timeout_s=5.0,
    )
    kwargs.update(overrides)
    return ProviderApiConfig(**kwargs)


def _doi_url(doi: str, *, email: str = "me@example.org") -> str:
    return f"{UNPAYWALL_BASE}/{quote(doi, safe='')}?{urlencode({'email': email})}"


# ---------------------------------------------------------------------------
# get_by_doi
# ---------------------------------------------------------------------------


def test_get_by_doi_hit_with_publisher_oa_location():
    transport = FakeTransport()
    transport.add_json(_doi_url("10.1234/fixture.5678"), json_body=load_fixture("unpaywall_doi_hit.json"))
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    record = provider.get_by_doi("10.1234/fixture.5678")

    assert record.title == "A Fixture Paper About Distributed Systems Metadata Reconciliation"
    assert record.doi == "10.1234/fixture.5678"
    assert record.authors == ["Ada Fixture", "Bo Canned"]
    assert record.year == 2021
    assert record.venue == "Journal of Fixture Studies"
    assert record.oa_pdf_url == "https://example.org/fixture.pdf"
    assert record.other["is_oa"] is True
    assert record.other["best_oa_location"]["host_type"] == "publisher"


def test_get_by_doi_sends_email_query_param():
    transport = FakeTransport()
    transport.add_json(
        _doi_url("10.1234/fixture.5678", email="specific@example.org"),
        json_body=load_fixture("unpaywall_doi_hit.json"),
    )
    provider = UnpaywallProvider(transport, _unpaywall_cfg(mailto="specific@example.org"))

    provider.get_by_doi("10.1234/fixture.5678")

    assert "email=specific%40example.org" in transport.calls[-1]["url"]


def test_get_by_doi_not_oa_yields_record_with_no_pdf_url():
    transport = FakeTransport()
    transport.add_json(_doi_url("10.1234/paywalled.0001"), json_body=load_fixture("unpaywall_doi_not_oa.json"))
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    record = provider.get_by_doi("10.1234/paywalled.0001")

    assert record.oa_pdf_url is None
    assert record.other["is_oa"] is False


def test_get_by_doi_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _doi_url("10.9999/missing"), json_body=load_fixture("unpaywall_not_found.json"), status_code=404
    )
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_doi("10.9999/missing")


def test_get_by_doi_empty_doi_returns_none_with_no_network_call():
    transport = FakeTransport()
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    assert provider.get_by_doi("") is None
    assert transport.calls == []


# ---------------------------------------------------------------------------
# missing-email refusal -- the provider itself, independent of the doctor
# check's own "needs-email" readiness reporting
# ---------------------------------------------------------------------------


def test_get_by_doi_without_configured_email_refuses_with_zero_network_calls():
    transport = FakeTransport()
    provider = UnpaywallProvider(transport, _unpaywall_cfg(mailto=None))

    with pytest.raises(ProviderConfigError):
        provider.get_by_doi("10.1234/fixture.5678")

    assert transport.calls == []


# ---------------------------------------------------------------------------
# get_by_arxiv (delegates via the arXiv-derived DOI)
# ---------------------------------------------------------------------------


def test_get_by_arxiv_resolves_via_synthesized_doi():
    transport = FakeTransport()
    transport.add_json(_doi_url("10.48550/arxiv.2101.00001"), json_body=load_fixture("unpaywall_doi_hit.json"))
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    record = provider.get_by_arxiv("arXiv:2101.00001")

    assert record.arxiv_id == "2101.00001"
    assert record.oa_pdf_url == "https://example.org/fixture.pdf"


def test_get_by_arxiv_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _doi_url("10.48550/arxiv.9999.99999"), json_body=load_fixture("unpaywall_not_found.json"), status_code=404
    )
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_arxiv("9999.99999")


# ---------------------------------------------------------------------------
# repository-hosted OA location (host_type/license mapping variety)
# ---------------------------------------------------------------------------


def test_get_by_doi_repository_hosted_oa_location():
    transport = FakeTransport()
    transport.add_json(_doi_url("10.1234/repo.0002"), json_body=load_fixture("unpaywall_doi_hit_repository.json"))
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    record = provider.get_by_doi("10.1234/repo.0002")

    assert record.oa_pdf_url == "https://institution.example.edu/repo/item123.pdf"
    assert record.other["best_oa_location"]["host_type"] == "repository"
    assert record.other["best_oa_location"]["license"] is None


# ---------------------------------------------------------------------------
# search / get_citations -- unconditionally unsupported
# ---------------------------------------------------------------------------


def test_search_always_raises_unsupported():
    transport = FakeTransport()
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.search("distributed systems")


def test_get_citations_always_raises_unsupported():
    transport = FakeTransport()
    provider = UnpaywallProvider(transport, _unpaywall_cfg())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_citations("10.1234/fixture.5678")
