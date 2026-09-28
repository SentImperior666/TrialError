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
    # a bad-HTTP-status ProviderTransportError (status_code set, host/scheme
    # left at their None default) gets no code/host/scheme -- that enrichment
    # is the transport-unreachable case's alone (see the test below). Lane
    # FB-acq item 2 adds `status_code` to every failure entry that has one: the
    # status used to live only inside the message string, so a caller wanting
    # to branch on 429-vs-5xx had to parse English.
    assert result.providers_failed == [{"provider": "openalex", "error": "boom", "status_code": 500}]


def test_lookup_doi_transport_unreachable_failure_is_enriched_with_code_host_scheme():
    """litapi-arxiv-https build: a GENUINE transport-level unreachability
    (get_with_retry's own wrapping -- host/scheme set) is recorded with
    an extra code/host/scheme a CLI caller can key off, distinct from the
    bad-HTTP-status case above."""
    a = _StubProvider(
        "arxiv",
        doi_error=ProviderTransportError(
            "arxiv: transport unreachable (https://export.arxiv.org): no route to host",
            provider="arxiv", status_code=None, host="export.arxiv.org", scheme="https",
        ),
    )
    b = _StubProvider("openalex", doi_record=WorkRecord(title="T", doi="10.1/x"))
    client = LitApiClient([a, b])

    result = client.lookup_doi("10.1/x")

    assert result.providers_failed == [
        {
            "provider": "arxiv",
            "error": "arxiv: transport unreachable (https://export.arxiv.org): no route to host",
            "code": "transport_unreachable",
            "host": "export.arxiv.org",
            "scheme": "https",
        }
    ]


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


# ---------------------------------------------------------------------------
# lane FB-acq item 2: provider_outcomes -- an entry for EVERY provider asked
# ---------------------------------------------------------------------------


def _rate_limit_error(provider: str, *, retry_after_s: float | None = 30.0) -> ProviderTransportError:
    return ProviderTransportError(
        f"{provider} request failed (lookup): HTTP 429", provider=provider, status_code=429,
        retry_after_s=retry_after_s,
    )


def test_provider_outcomes_names_a_rate_limit_and_a_not_found_separately():
    """FAILS BEFORE this lane: a provider answering "not found" is dropped
    from ``providers_failed`` by design, so "one rate-limited, the other has no
    such record" and "both broken" were the same AllProvidersFailedError with
    the status only inside a message string."""
    a = _StubProvider("openalex", doi_error=_rate_limit_error("openalex"))
    b = _StubProvider("semanticscholar", doi_error=ProviderNotFoundError("nope", provider="semanticscholar"))
    client = LitApiClient([a, b])

    with pytest.raises(AllProvidersFailedError) as caught:
        client.lookup_doi("10.1000/example")

    po = caught.value.details["provider_outcomes"]
    assert po["openalex"]["outcome"] == "rate_limited"
    assert po["openalex"]["status_code"] == 429
    assert po["openalex"]["retry_after_s"] == 30.0
    assert po["semanticscholar"]["outcome"] == "not_found"
    # and the failures list still holds only the real failure
    assert [f["provider"] for f in caught.value.details["failures"]] == ["openalex"]


def test_provider_outcomes_records_a_success_beside_a_rate_limit():
    a = _StubProvider("openalex", doi_error=_rate_limit_error("openalex"))
    b = _StubProvider("semanticscholar", doi_record=WorkRecord(title="T", doi="10.1000/example"))
    client = LitApiClient([a, b])

    result = client.lookup_doi("10.1000/example")

    assert result.record.title == "T"
    assert result.provider_outcomes["openalex"]["outcome"] == "rate_limited"
    assert result.provider_outcomes["semanticscholar"]["outcome"] == "record"
    assert result.to_dict()["provider_outcomes"]["openalex"]["status_code"] == 429


def test_a_provider_returning_none_with_no_exception_reads_as_not_found():
    a = _StubProvider("openalex", doi_record=None)
    b = _StubProvider("semanticscholar", doi_record=WorkRecord(title="T", doi="10.1000/example"))
    client = LitApiClient([a, b])

    result = client.lookup_doi("10.1000/example")

    assert result.provider_outcomes["openalex"]["outcome"] == "not_found"


def test_provider_outcome_keys_are_the_full_documented_set():
    a = _StubProvider("openalex", doi_record=WorkRecord(title="T", doi="10.1000/example"))
    client = LitApiClient([a])

    outcome = client.lookup_doi("10.1000/example").provider_outcomes["openalex"]

    assert set(outcome) == {
        "outcome", "status_code", "retry_after_s", "attempts", "waited_s", "request_sent", "keyed", "error",
    }
    assert outcome["keyed"] is False  # the stub holds no _api_key


def test_provider_outcomes_covers_search_and_citations_too():
    a = _StubProvider("openalex", search_error=_rate_limit_error("openalex"))
    b = _StubProvider("semanticscholar", search_error=ProviderNotFoundError("nope", provider="semanticscholar"))
    client = LitApiClient([a, b])

    with pytest.raises(AllProvidersFailedError) as caught:
        client.search("10.1000/example")  # an identifier: never retried with a normalised query
    assert caught.value.details["provider_outcomes"]["openalex"]["outcome"] == "rate_limited"

    c = _StubProvider("openalex", citations_error=_rate_limit_error("openalex"))
    with pytest.raises(AllProvidersFailedError) as caught_citations:
        LitApiClient([c]).get_citations("10.1000/example")
    assert caught_citations.value.details["provider_outcomes"]["openalex"]["outcome"] == "rate_limited"


def test_a_search_that_succeeded_reports_record_or_not_found_per_provider():
    a = _StubProvider("openalex", search_records=[WorkRecord(title="Example Paper")])
    b = _StubProvider("semanticscholar", search_records=[])
    client = LitApiClient([a, b])

    result = client.search("example paper")

    assert result.provider_outcomes["openalex"]["outcome"] == "record"
    assert result.provider_outcomes["semanticscholar"]["outcome"] == "not_found"
    assert "provider_outcomes" in result.to_dict()


def test_a_provider_reporting_a_real_key_is_marked_keyed(tmp_path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("example-key", encoding="utf-8")
    config = load_litapi_config({"litapi": {"openalex": {"api_key_path": str(key_file), "min_interval_s": 0.0}}})
    transport = FakeTransport()
    provider = OpenAlexProvider(transport, config.openalex)
    client = LitApiClient([provider])

    with pytest.raises(AllProvidersFailedError) as caught:
        client.lookup_doi("10.1000/example")  # no route registered -> TransportNotConfiguredError

    assert caught.value.details["provider_outcomes"]["openalex"]["keyed"] is True


# ---------------------------------------------------------------------------
# lane SI items A1/A2: provider_extra, filtered citations, references, author
# works -- real providers over one FakeTransport where the URL matters.
# ---------------------------------------------------------------------------

from urllib.parse import quote as _quote, urlencode as _urlencode  # noqa: E402

from trialerror.litapi.config import ProviderApiConfig  # noqa: E402
from trialerror.litapi.models import provider_extra  # noqa: E402
from trialerror.litapi.providers.openalex import SELECT_FIELDS as _OA_SELECT  # noqa: E402
from trialerror.litapi.providers.semanticscholar import FIELDS as _S2_FIELDS  # noqa: E402
from trialerror.litapi.transport import TransportResponse  # noqa: E402
from tests._litapi_fixtures import load_fixture  # noqa: E402


def _cfg(name: str, base_url: str) -> ProviderApiConfig:
    return ProviderApiConfig(
        name=name, base_url=base_url, mailto=None, api_key_path=None, api_key_header="x-api-key",
        min_interval_s=0.0, retry_attempts=1, retry_on_status=(500,), timeout_s=5.0,
    )


def _oa_works_url(extra: dict) -> str:
    return f"https://api.openalex.org/works?{_urlencode({'select': ','.join(_OA_SELECT), **extra})}"


def test_provider_extra_reads_flat_and_merged_shapes():
    one = _StubProvider("openalex", doi_record=WorkRecord(title="T", doi="10.1/x", other={"author_ids": ["A1"]}))
    flat = LitApiClient([one]).lookup_doi("10.1/x").record
    assert flat.other == {"author_ids": ["A1"]}  # one provider: extras stay flat
    assert provider_extra(flat, "openalex", "author_ids") == ["A1"]
    assert provider_extra(flat, "semanticscholar", "author_ids") is None

    a = _StubProvider("openalex", doi_record=WorkRecord(title="T", doi="10.1/x", other={"author_ids": ["A1"]}))
    b = _StubProvider("semanticscholar", doi_record=WorkRecord(title="T", doi="10.1/x", other={"author_ids": ["9"]}))
    merged = LitApiClient([a, b]).lookup_doi("10.1/x").record
    assert "author_ids" not in merged.other  # two providers: extras nested per provider
    assert provider_extra(merged, "openalex", "author_ids") == ["A1"]
    assert provider_extra(merged, "semanticscholar", "author_ids") == ["9"]
    assert provider_extra(merged, "openalex", "work_type") is None
    assert provider_extra(merged, "arxiv", "author_ids") is None


def test_client_get_citations_work_type_skips_unsupported_provider():
    transport = FakeTransport()
    url = _oa_works_url({"filter": "cites:W2741809807,type:review", "per-page": "10", "page": "1"})
    transport.add_json(url, json_body=load_fixture("openalex_citations_page.json"))
    s2 = SemanticScholarProvider(transport, _cfg("semanticscholar", "https://api.semanticscholar.org"))
    oa = OpenAlexProvider(transport, _cfg("openalex", "https://api.openalex.org"))
    client = LitApiClient([s2, oa])  # S2 first in order

    page = client.get_citations("W2741809807", limit=10, work_type="review")

    assert page.provider == "openalex"
    assert page.provider_outcomes["semanticscholar"]["outcome"] == "unsupported"
    assert page.provider_outcomes["openalex"]["outcome"] == "record"
    assert [c["url"] for c in transport.calls] == [url]  # S2 sent nothing


def test_client_get_citations_keyword_a_provider_does_not_accept_is_unsupported_not_type_error():
    """A provider written before ``work_type`` existed (the stub's signature
    has no such keyword) is recorded ``unsupported`` instead of raising."""
    old = _StubProvider("old", citations_page=CitationsPage(items=[], provider="old", offset=0, limit=10))
    new = _StubProvider("new", citations_page=CitationsPage(items=[CitationEdge(title="C")], provider="new", offset=0, limit=10))
    new.get_citations = lambda identifier, *, limit=100, offset=0, work_type=None, sort=None: new._citations_page

    page = LitApiClient([old, new]).get_citations("10.1/x", work_type="review")

    assert page.provider == "new"
    assert page.provider_outcomes["old"]["outcome"] == "unsupported"
    assert old.calls == []


def test_client_get_citations_default_call_reaches_providers_without_new_keywords():
    stub = _StubProvider("stub", citations_page=CitationsPage(items=[], provider="stub", offset=0, limit=5))

    page = LitApiClient([stub]).get_citations("10.1/x", limit=5)

    assert stub.calls == ["get_citations:10.1/x"]
    assert page.provider_outcomes == {"stub": page.provider_outcomes["stub"]}
    assert page.provider_outcomes["stub"]["outcome"] == "record"


def test_client_get_references_provider_without_method_is_unsupported():
    no_refs = _StubProvider("norefs")  # the stub has no get_references at all
    has_refs = _StubProvider("refs")
    has_refs.get_references = lambda identifier, *, limit=20: CitationsPage(
        items=[CitationEdge(title="Ref")], provider="refs", offset=0, limit=limit
    )

    page = LitApiClient([no_refs, has_refs]).get_references("10.1/x", limit=5)

    assert page.provider == "refs"
    assert page.provider_outcomes["norefs"]["outcome"] == "unsupported"
    assert page.limit == 5


def test_client_get_references_all_failing_raises_with_outcomes():
    with pytest.raises(AllProvidersFailedError) as excinfo:
        LitApiClient([_StubProvider("norefs")]).get_references("10.1/x")
    assert excinfo.value.details["provider_outcomes"]["norefs"]["outcome"] == "unsupported"


def test_client_get_author_works_uses_each_providers_own_ids():
    asked: list[tuple[str, str, int | None, int]] = []

    def _author_works(name):
        def call(author_id, *, since_year=None, limit=10):
            asked.append((name, author_id, since_year, limit))
            return [WorkRecord(title=f"{name}:{author_id}")]
        return call

    oa = _StubProvider("openalex", doi_record=WorkRecord(
        title="T", doi="10.1/x", authors=["Ann Author", "Ben Nobody"], other={"author_ids": [None, "A2"]}))
    s2 = _StubProvider("semanticscholar", doi_record=WorkRecord(
        title="T", doi="10.1/x", authors=["Ann Author", "Ben Nobody"], other={"author_ids": ["100", "200"]}))
    oa.get_author_works = _author_works("openalex")
    s2.get_author_works = _author_works("semanticscholar")
    client = LitApiClient([oa, s2])
    record = client.lookup_doi("10.1/x").record  # merged: extras nested per provider

    result = client.get_author_works(record, authors=3, since_year=2016, limit=8)

    # position 0: OpenAlex has no id there, so only S2 is asked; position 1:
    # OpenAlex (first in order) serves it; there is no position 2.
    assert asked == [("semanticscholar", "100", 2016, 8), ("openalex", "A2", 2016, 8)]
    assert [(a.position, a.provider, a.author_id) for a in result.authors] == [
        (0, "semanticscholar", "100"), (1, "openalex", "A2"),
    ]
    assert result.authors[0].providers_without_id == ["openalex"]
    assert result.authors[0].records[0].providers == ["semanticscholar"]
    assert result.to_dict()["provider_outcomes"]["openalex"][0]["position"] == 1


def test_client_get_author_works_rate_limited_is_outcome_not_empty():
    """A 429 on the only provider's author-works route comes back as an outcome
    ``rate_limited`` with no serving provider -- never as an empty list that
    reads "this author has no works"."""
    transport = FakeTransport()
    url = _oa_works_url({"filter": "author.id:A5000000001", "sort": "cited_by_count:desc", "per-page": "8"})
    transport.add_sequence(url, [TransportResponse(status_code=429, json_body={"error": "rate limited"})])
    oa = OpenAlexProvider(transport, _cfg("openalex", "https://api.openalex.org"))
    record = WorkRecord(title="T", authors=["Ann Author"], providers=["openalex"],
                        other={"author_ids": ["A5000000001"]})

    result = LitApiClient([oa]).get_author_works(record, authors=3, limit=8)

    assert len(result.authors) == 1
    entry = result.authors[0]
    assert entry.provider is None
    assert entry.records == []
    assert entry.provider_outcomes["openalex"]["outcome"] == "rate_limited"
    assert entry.provider_outcomes["openalex"]["status_code"] == 429
    assert result.provider_outcomes["openalex"][0]["outcome"] == "rate_limited"


def test_client_get_author_works_falls_through_a_rate_limit_to_the_next_provider():
    transport = FakeTransport()
    oa_url = _oa_works_url(
        {"filter": "author.id:A1,from_publication_date:2010-01-01", "sort": "cited_by_count:desc", "per-page": "8"}
    )
    transport.add_sequence(oa_url, [TransportResponse(status_code=429, json_body={})])
    s2_url = (
        "https://api.semanticscholar.org/graph/v1/author/"
        f"{_quote('1000', safe='')}/papers?"
        + _urlencode({"fields": ",".join(_S2_FIELDS), "limit": "8"})
    )
    transport.add_json(s2_url, json_body=load_fixture("semanticscholar_author_papers.json"))
    oa = OpenAlexProvider(transport, _cfg("openalex", "https://api.openalex.org"))
    s2 = SemanticScholarProvider(transport, _cfg("semanticscholar", "https://api.semanticscholar.org"))
    record = WorkRecord(title="T", authors=["Ann Author"], providers=["openalex", "semanticscholar"],
                        other={"openalex": {"author_ids": ["A1"]}, "semanticscholar": {"author_ids": ["1000"]}})

    result = LitApiClient([oa, s2]).get_author_works(record, authors=1, since_year=2010, limit=8)

    entry = result.authors[0]
    assert entry.provider == "semanticscholar"
    assert [r.title for r in entry.records] == ["Widgets Revisited"]
    assert entry.provider_outcomes["openalex"]["outcome"] == "rate_limited"
    assert entry.provider_outcomes["semanticscholar"]["outcome"] == "record"


def test_an_unsupported_outcome_never_repeats_the_previous_requests_stats():
    """S2 answered a DOI lookup first (a request went out); its refusal of a
    ``work_type`` filter right after must say nothing was sent, not echo the
    lookup's numbers. Same for a provider the CLIENT refuses (no method)."""
    transport = FakeTransport()
    s2_doi_url = (
        "https://api.semanticscholar.org/graph/v1/paper/"
        f"{_quote('DOI:10.1234/fixture.5678', safe=':')}?{_urlencode({'fields': ','.join(_S2_FIELDS)})}"
    )
    transport.add_json(s2_doi_url, json_body=load_fixture("semanticscholar_doi_hit.json"))
    url = _oa_works_url({"filter": "cites:W2741809807,type:review", "per-page": "10", "page": "1"})
    transport.add_json(url, json_body=load_fixture("openalex_citations_page.json"))
    s2 = SemanticScholarProvider(transport, _cfg("semanticscholar", "https://api.semanticscholar.org"))
    oa = OpenAlexProvider(transport, _cfg("openalex", "https://api.openalex.org"))
    s2.get_by_doi("10.1234/fixture.5678")
    assert s2.last_request_stats["request_sent"] is True

    page = LitApiClient([s2, oa]).get_citations("W2741809807", limit=10, work_type="review")

    s2_outcome = page.provider_outcomes["semanticscholar"]
    assert s2_outcome["outcome"] == "unsupported"
    assert s2_outcome["request_sent"] is False
    assert s2_outcome["attempts"] == 0
    assert s2_outcome["status_code"] is None

    stale = _StubProvider("stale")
    stale.last_request_stats = {"attempts": 1, "waited_s": 0.0, "last_status": 200, "retry_after_s": None,
                                "request_sent": True}
    with pytest.raises(AllProvidersFailedError) as excinfo:
        LitApiClient([stale]).get_references("10.1/x")
    refused = excinfo.value.details["provider_outcomes"]["stale"]
    assert refused["outcome"] == "unsupported"
    assert refused["request_sent"] is False
    assert refused["status_code"] is None


def test_outcome_word_is_the_provider_outcome_classification():
    """Lane SI part B: the one-word classification on its own (the evidence
    cache records it per cached answer), the same word ``_provider_outcome``
    reports."""
    from trialerror.litapi.client import PROVIDER_OUTCOMES, outcome_word
    from trialerror.litapi.errors import LitApiError, ProviderConfigError, ProviderUnsupportedOperationError

    cases = [
        (None, True, "record"),
        (None, False, "not_found"),
        (ProviderNotFoundError("x", provider="p"), False, "not_found"),
        (ProviderConfigError("x"), False, "not_configured"),
        (ProviderUnsupportedOperationError("x", provider="p"), False, "unsupported"),
        (ProviderTransportError("x", provider="p", host="h", scheme="https"), False, "transport_unreachable"),
        (ProviderTransportError("x", provider="p", status_code=429), False, "rate_limited"),
        (ProviderTransportError("x", provider="p", status_code=503), False, "http_error"),
        (LitApiError("x"), False, "error"),
    ]
    for exc, found, word in cases:
        assert outcome_word(exc, found) == word
        assert word in PROVIDER_OUTCOMES
