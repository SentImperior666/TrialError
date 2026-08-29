"""Tests for ``trialerror.litapi.client.LitApiClient``: the redundant-fetch
orchestration itself (design brief: "so a single API's fragility or
rate-limit never blocks a lookup"). Uses small hand-written stub providers
(NOT the real OpenAlex/Semantic Scholar clients, which are covered in
``tests/test_litapi_providers.py``) so this file tests ONLY the
client-level tolerate-partial-failure/reconcile/fall-through logic."""

from __future__ import annotations

import pytest

from trialerror.litapi.client import ALL_CLIENTS, DEFAULT_CLIENTS, LitApiClient, build_default_providers
from trialerror.litapi.config import load_litapi_config
from trialerror.litapi.errors import AllProvidersFailedError, ProviderNotFoundError, ProviderTransportError
from trialerror.litapi.models import CitationEdge, CitationsPage, WorkRecord
from trialerror.litapi.providers.arxiv import ArxivProvider
from trialerror.litapi.providers.openalex import OpenAlexProvider
from trialerror.litapi.providers.semanticscholar import SemanticScholarProvider
from trialerror.litapi.providers.unpaywall import UnpaywallProvider
from trialerror.litapi.transport import FakeTransport


class _StubProvider:
    def __init__(
        self, name, *, doi_record=None, doi_error=None, arxiv_record=None, arxiv_error=None,
        search_records=None, search_error=None, citations_page=None, citations_error=None,
    ):
        self.name = name
        self._doi_record, self._doi_error = doi_record, doi_error
        self._arxiv_record, self._arxiv_error = arxiv_record, arxiv_error
        self._search_records, self._search_error = search_records or [], search_error
        self._citations_page, self._citations_error = citations_page, citations_error
        self.calls: list[str] = []

    def get_by_doi(self, doi):
        self.calls.append(f"get_by_doi:{doi}")
        if self._doi_error:
            raise self._doi_error
        return self._doi_record

    def get_by_arxiv(self, arxiv_id):
        self.calls.append(f"get_by_arxiv:{arxiv_id}")
        if self._arxiv_error:
            raise self._arxiv_error
        return self._arxiv_record

    def search(self, query, *, limit=10):
        self.calls.append(f"search:{query}")
        if self._search_error:
            raise self._search_error
        return self._search_records

    def get_citations(self, identifier, *, limit=100, offset=0):
        self.calls.append(f"get_citations:{identifier}")
        if self._citations_error:
            raise self._citations_error
        return self._citations_page


def test_client_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        LitApiClient([])


def test_lookup_doi_merges_records_from_both_providers():
    a = _StubProvider("openalex", doi_record=WorkRecord(title="T", doi="10.1/x", year=2020))
    b = _StubProvider("semanticscholar", doi_record=WorkRecord(title="T", doi="10.1/x", citation_count=42))
    client = LitApiClient([a, b])

    result = client.lookup_doi("10.1/x")

    assert result.record.year == 2020
    assert result.record.citation_count == 42
    assert sorted(result.providers_succeeded) == ["openalex", "semanticscholar"]
    assert result.providers_failed == []


def test_lookup_doi_not_found_from_one_provider_is_not_a_failure():
    a = _StubProvider("openalex", doi_error=ProviderNotFoundError("nope", provider="openalex"))
    b = _StubProvider("semanticscholar", doi_record=WorkRecord(title="T", doi="10.1/x"))
    client = LitApiClient([a, b])

    result = client.lookup_doi("10.1/x")

    assert result.record.title == "T"
    assert result.providers_succeeded == ["semanticscholar"]
    assert result.providers_failed == []  # not-found is silently skipped, not a recorded failure


def test_lookup_doi_transport_error_from_one_provider_is_recorded_as_a_failure():
    a = _StubProvider("openalex", doi_error=ProviderTransportError("boom", provider="openalex", status_code=500))
    b = _StubProvider("semanticscholar", doi_record=WorkRecord(title="T", doi="10.1/x"))
    client = LitApiClient([a, b])

    result = client.lookup_doi("10.1/x")

    assert result.record.title == "T"
    assert result.providers_succeeded == ["semanticscholar"]
    assert result.providers_failed == [{"provider": "openalex", "error": "boom"}]


def test_lookup_doi_all_providers_failing_raises_with_details():
    a = _StubProvider("openalex", doi_error=ProviderTransportError("boom-a", provider="openalex"))
    b = _StubProvider("semanticscholar", doi_error=ProviderNotFoundError("nope-b", provider="semanticscholar"))
    client = LitApiClient([a, b])

    with pytest.raises(AllProvidersFailedError) as excinfo:
        client.lookup_doi("10.1/missing")

    assert excinfo.value.details["failures"] == [{"provider": "openalex", "error": "boom-a"}]


def test_lookup_arxiv_calls_get_by_arxiv_not_get_by_doi():
    a = _StubProvider("openalex", arxiv_record=WorkRecord(title="Preprint", arxiv_id="2101.00001"))
    client = LitApiClient([a])

    result = client.lookup_arxiv("2101.00001")

    assert result.record.title == "Preprint"
    assert a.calls == ["get_by_arxiv:2101.00001"]


def test_search_merges_and_truncates_to_limit():
    a = _StubProvider("openalex", search_records=[
        WorkRecord(title="One", doi="10.1/one"), WorkRecord(title="Two", doi="10.1/two"),
    ])
    b = _StubProvider("semanticscholar", search_records=[WorkRecord(title="One", doi="10.1/one", year=2019)])
    client = LitApiClient([a, b])

    result = client.search("engines", limit=1)

    assert len(result.records) == 1
    assert result.records[0].year == 2019  # merged from provider b even though a listed it first
    assert sorted(result.providers_succeeded) == ["openalex", "semanticscholar"]


def test_search_tolerates_one_provider_failing():
    a = _StubProvider("openalex", search_error=ProviderTransportError("down", provider="openalex"))
    b = _StubProvider("semanticscholar", search_records=[WorkRecord(title="Hit", doi="10.1/hit")])
    client = LitApiClient([a, b])

    result = client.search("engines")

    assert [r.title for r in result.records] == ["Hit"]
    assert result.providers_failed == [{"provider": "openalex", "error": "down"}]


def test_get_citations_falls_through_to_next_provider_on_not_found():
    a = _StubProvider("openalex", citations_error=ProviderNotFoundError("nope", provider="openalex"))
    page = CitationsPage(items=[CitationEdge(title="Citer")], provider="semanticscholar", offset=0, limit=10)
    b = _StubProvider("semanticscholar", citations_page=page)
    client = LitApiClient([a, b])

    result = client.get_citations("10.1/x")

    assert result.provider == "semanticscholar"
    assert result.items[0].title == "Citer"


def test_get_citations_falls_through_on_transport_error():
    a = _StubProvider("openalex", citations_error=ProviderTransportError("down", provider="openalex"))
    page = CitationsPage(items=[], provider="semanticscholar", offset=0, limit=10)
    b = _StubProvider("semanticscholar", citations_page=page)
    client = LitApiClient([a, b])

    result = client.get_citations("10.1/x")

    assert result.provider == "semanticscholar"


def test_get_citations_all_providers_failing_raises():
    a = _StubProvider("openalex", citations_error=ProviderNotFoundError("nope", provider="openalex"))
    b = _StubProvider("semanticscholar", citations_error=ProviderTransportError("down", provider="semanticscholar"))
    client = LitApiClient([a, b])

    with pytest.raises(AllProvidersFailedError) as excinfo:
        client.get_citations("10.1/missing")

    assert len(excinfo.value.details["failures"]) == 2


def test_build_default_providers_constructs_both_providers_against_given_transport():
    config = load_litapi_config({})
    transport = FakeTransport()  # proves no real network is touched by construction itself

    providers = build_default_providers(config, transport=transport)

    assert [type(p) for p in providers] == [OpenAlexProvider, SemanticScholarProvider]
    assert all(p.transport is transport for p in providers)


def test_build_default_providers_honors_provider_classes_override():
    config = load_litapi_config({})
    transport = FakeTransport()

    providers = build_default_providers(config, transport=transport, provider_classes=(OpenAlexProvider,))

    assert len(providers) == 1
    assert isinstance(providers[0], OpenAlexProvider)


def test_default_clients_is_openalex_then_semanticscholar():
    assert DEFAULT_CLIENTS == (OpenAlexProvider, SemanticScholarProvider)


# ---------------------------------------------------------------------------
# ALL_CLIENTS (v3-acquisition build): diverges from DEFAULT_CLIENTS now --
# adds arXiv + Unpaywall, exercising the seam trialerror.litapi.providers'
# module docstring always documented.
# ---------------------------------------------------------------------------


def test_all_clients_adds_arxiv_and_unpaywall_to_the_original_two():
    assert ALL_CLIENTS == (OpenAlexProvider, SemanticScholarProvider, ArxivProvider, UnpaywallProvider)
    assert ALL_CLIENTS != DEFAULT_CLIENTS


def test_build_default_providers_honors_all_clients_override():
    config = load_litapi_config({})
    transport = FakeTransport()

    providers = build_default_providers(config, transport=transport, provider_classes=ALL_CLIENTS)

    assert [type(p) for p in providers] == [OpenAlexProvider, SemanticScholarProvider, ArxivProvider, UnpaywallProvider]
    assert all(p.transport is transport for p in providers)
