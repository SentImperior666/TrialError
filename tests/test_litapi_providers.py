"""Tests for the two provider clients against the canned JSON fixtures
(``tests/fixtures/litapi/*.json``) via :class:`FakeTransport` -- NO live
network call anywhere in this file (design brief's offline-testability
requirement). URLs are computed with the exact same stdlib primitives
(``urllib.parse.quote``/``urlencode``) the providers use, so these tests
assert "does the provider build the URL/query it documents building",
not a re-guessed oracle."""

from __future__ import annotations

from urllib.parse import quote, urlencode

import pytest

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderNotFoundError
from trialerror.litapi.providers.openalex import SELECT_FIELDS, OpenAlexProvider
from trialerror.litapi.providers.semanticscholar import FIELDS, SemanticScholarProvider
from trialerror.litapi.transport import FakeTransport
from tests._litapi_fixtures import load_fixture

OPENALEX_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org"


def _openalex_cfg(**overrides) -> ProviderApiConfig:
    kwargs = dict(
        name="openalex", base_url=OPENALEX_BASE, mailto=None, api_key_path=None,
        api_key_header="x-api-key", min_interval_s=0.0, retry_attempts=1, retry_on_status=(500,), timeout_s=5.0,
    )
    kwargs.update(overrides)
    return ProviderApiConfig(**kwargs)


def _s2_cfg(**overrides) -> ProviderApiConfig:
    kwargs = dict(
        name="semanticscholar", base_url=S2_BASE, mailto=None, api_key_path=None,
        api_key_header="x-api-key", min_interval_s=0.0, retry_attempts=1, retry_on_status=(403,), timeout_s=5.0,
    )
    kwargs.update(overrides)
    return ProviderApiConfig(**kwargs)


def _openalex_doi_url(doi: str, *, mailto: str | None = None) -> str:
    q = {"select": ",".join(SELECT_FIELDS)}
    if mailto:
        q["mailto"] = mailto
    segment = f"https://doi.org/{quote(doi, safe='')}"
    return f"{OPENALEX_BASE}/works/{segment}?{urlencode(q)}"


def _openalex_works_url(extra: dict) -> str:
    q = {"select": ",".join(SELECT_FIELDS), **extra}
    return f"{OPENALEX_BASE}/works?{urlencode(q)}"


def _s2_paper_url(paper_id: str) -> str:
    return f"{S2_BASE}/graph/v1/paper/{quote(paper_id, safe=':')}?{urlencode({'fields': ','.join(FIELDS)})}"


def _s2_search_url(query: str, limit: int) -> str:
    q = {"query": query, "limit": str(limit), "fields": ",".join(FIELDS)}
    return f"{S2_BASE}/graph/v1/paper/search?{urlencode(q)}"


def _s2_citations_url(paper_id: str, *, offset: int, limit: int) -> str:
    from trialerror.litapi.providers.semanticscholar import _CITATION_FIELDS

    q = {"offset": str(offset), "limit": str(limit), "fields": ",".join(_CITATION_FIELDS)}
    return f"{S2_BASE}/graph/v1/paper/{quote(paper_id, safe=':')}/citations?{urlencode(q)}"


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


def test_openalex_get_by_doi_hit():
    transport = FakeTransport()
    transport.add_json(_openalex_doi_url("10.1234/fixture.5678"), json_body=load_fixture("openalex_doi_hit.json"))
    provider = OpenAlexProvider(transport, _openalex_cfg())

    record = provider.get_by_doi("10.1234/fixture.5678")

    assert record.title == "A Fixture Paper About Tabletop Engine Metadata Reconciliation"
    assert record.doi == "10.1234/fixture.5678"
    assert record.authors == ["Ada Fixture", "Bo Canned"]
    assert record.year == 2021
    assert record.citation_count == 42
    assert record.oa_pdf_url == "https://example.org/fixture.pdf"
    assert record.venue == "Journal of Fixture Studies"
    assert record.external_ids["openalex"] == "W2741809807"
    assert record.abstract == "This is a canned abstract."


def test_openalex_sends_mailto_when_configured():
    transport = FakeTransport()
    url = _openalex_doi_url("10.1234/fixture.5678", mailto="me@example.org")
    transport.add_json(url, json_body=load_fixture("openalex_doi_hit.json"))
    provider = OpenAlexProvider(transport, _openalex_cfg(mailto="me@example.org"))

    provider.get_by_doi("10.1234/fixture.5678")

    assert transport.calls[-1]["url"] == url


def test_openalex_sends_api_key_header_when_resolved(tmp_path):
    key_path = tmp_path / "openalex.key"
    key_path.write_text("secret-oa-key", encoding="utf-8")
    transport = FakeTransport()
    transport.add_json(_openalex_doi_url("10.1234/fixture.5678"), json_body=load_fixture("openalex_doi_hit.json"))
    provider = OpenAlexProvider(transport, _openalex_cfg(api_key_path=str(key_path)))

    provider.get_by_doi("10.1234/fixture.5678")

    assert transport.calls[-1]["headers"] == {"x-api-key": "secret-oa-key"}


def test_openalex_get_by_doi_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _openalex_doi_url("10.9999/missing"), json_body=load_fixture("openalex_not_found.json"), status_code=404
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_doi("10.9999/missing")


def test_openalex_get_by_arxiv_resolves_via_synthesized_doi():
    transport = FakeTransport()
    # arXiv 2101.00001 -> synthesized DOI 10.48550/arxiv.2101.00001
    transport.add_json(
        _openalex_doi_url("10.48550/arxiv.2101.00001"), json_body=load_fixture("openalex_doi_hit.json")
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    record = provider.get_by_arxiv("arXiv:2101.00001")

    assert record.arxiv_id == "2101.00001"
    assert record.title == "A Fixture Paper About Tabletop Engine Metadata Reconciliation"


def test_openalex_get_by_arxiv_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _openalex_doi_url("10.48550/arxiv.9999.99999"), json_body=load_fixture("openalex_not_found.json"),
        status_code=404,
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_arxiv("9999.99999")


def test_openalex_search_returns_records_up_to_limit():
    transport = FakeTransport()
    transport.add_json(
        _openalex_works_url({"filter": "title.search:tabletop engines", "per-page": "2"}),
        json_body=load_fixture("openalex_citations_page.json"),
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    records = provider.search("tabletop engines", limit=2)

    assert len(records) == 2
    assert records[0].title == "A Fixture Paper That Cites The Target Work"
    assert records[1].doi is None


def test_openalex_get_citations_by_doi_resolves_then_lists():
    transport = FakeTransport()
    transport.add_json(_openalex_doi_url("10.1234/fixture.5678"), json_body=load_fixture("openalex_doi_hit.json"))
    transport.add_json(
        _openalex_works_url({"filter": "cites:W2741809807", "per-page": "2", "page": "1"}),
        json_body=load_fixture("openalex_citations_page.json"),
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    page = provider.get_citations("10.1234/fixture.5678", limit=2, offset=0)

    assert page.provider == "openalex"
    assert page.total == 2
    assert len(page.items) == 2
    assert page.items[0].doi == "10.1234/citer.0001"
    assert page.items[1].doi is None
    assert page.has_more is False


def test_openalex_get_citations_by_bare_openalex_id_skips_resolve():
    transport = FakeTransport()
    transport.add_json(
        _openalex_works_url({"filter": "cites:W2741809807", "per-page": "2", "page": "1"}),
        json_body=load_fixture("openalex_citations_page.json"),
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    page = provider.get_citations("W2741809807", limit=2, offset=0)

    assert len(page.items) == 2
    # only the citations URL was registered -- a resolve call would have
    # raised TransportNotConfiguredError, so reaching here proves no
    # resolve step happened.


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------


def test_s2_get_by_doi_hit_prefers_native_doi_over_arxiv_derived():
    transport = FakeTransport()
    transport.add_json(_s2_paper_url("DOI:10.1234/fixture.5678"), json_body=load_fixture("semanticscholar_doi_hit.json"))
    provider = SemanticScholarProvider(transport, _s2_cfg())

    record = provider.get_by_doi("10.1234/fixture.5678")

    assert record.doi == "10.1234/fixture.5678"  # native DOI, not the arXiv-derived one -- see module TRIALERROR-DEV-NOTE
    assert record.arxiv_id == "2101.00001"
    assert record.authors == ["Ada Fixture", "Bo Canned"]
    assert record.citation_count == 42
    assert record.oa_pdf_url == "https://example.org/fixture.pdf"
    assert record.abstract == "This is a canned abstract from the Semantic Scholar fixture."
    assert record.other["influentialCitationCount"] == 5


def test_s2_sends_api_key_header_when_resolved(tmp_path):
    key_path = tmp_path / "s2.key"
    key_path.write_text("secret-s2-key", encoding="utf-8")
    transport = FakeTransport()
    transport.add_json(_s2_paper_url("DOI:10.1234/fixture.5678"), json_body=load_fixture("semanticscholar_doi_hit.json"))
    provider = SemanticScholarProvider(transport, _s2_cfg(api_key_path=str(key_path)))

    provider.get_by_doi("10.1234/fixture.5678")

    assert transport.calls[-1]["headers"] == {"x-api-key": "secret-s2-key"}


def test_s2_get_by_doi_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _s2_paper_url("DOI:10.9999/missing"), json_body=load_fixture("semanticscholar_not_found.json"),
        status_code=404,
    )
    provider = SemanticScholarProvider(transport, _s2_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_doi("10.9999/missing")


def test_s2_get_by_arxiv_hit_uses_arxiv_prefixed_path():
    transport = FakeTransport()
    transport.add_json(_s2_paper_url("ARXIV:2101.00001"), json_body=load_fixture("semanticscholar_doi_hit.json"))
    provider = SemanticScholarProvider(transport, _s2_cfg())

    record = provider.get_by_arxiv("arXiv:2101.00001")

    assert record.arxiv_id == "2101.00001"
    assert record.doi == "10.1234/fixture.5678"


def test_s2_get_by_arxiv_fills_arxiv_id_when_response_omits_it():
    transport = FakeTransport()
    body = {"paperId": "abc", "title": "No ArXiv Field In Response", "externalIds": {"DOI": "10.1/x"}, "authors": []}
    transport.add_json(_s2_paper_url("ARXIV:2205.00002"), json_body=body)
    provider = SemanticScholarProvider(transport, _s2_cfg())

    record = provider.get_by_arxiv("2205.00002")

    assert record.arxiv_id == "2205.00002"


def test_s2_get_by_arxiv_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _s2_paper_url("ARXIV:9999.99999"), json_body=load_fixture("semanticscholar_not_found.json"), status_code=404
    )
    provider = SemanticScholarProvider(transport, _s2_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_by_arxiv("9999.99999")


def test_s2_search_returns_records():
    transport = FakeTransport()
    body = {
        "total": 1, "offset": 0,
        "data": [{"paperId": "x", "title": "A Fixture Search Hit", "externalIds": {"DOI": "10.1/search"}, "authors": []}],
    }
    transport.add_json(_s2_search_url("tabletop engines", 5), json_body=body)
    provider = SemanticScholarProvider(transport, _s2_cfg())

    records = provider.search("tabletop engines", limit=5)

    assert len(records) == 1
    assert records[0].title == "A Fixture Search Hit"


def test_s2_get_citations_by_doi_coerces_prefix_and_parses_page():
    transport = FakeTransport()
    transport.add_json(
        _s2_citations_url("DOI:10.1234/fixture.5678", offset=0, limit=10),
        json_body=load_fixture("semanticscholar_citations_page.json"),
    )
    provider = SemanticScholarProvider(transport, _s2_cfg())

    page = provider.get_citations("10.1234/fixture.5678", limit=10, offset=0)

    assert page.provider == "semanticscholar"
    assert len(page.items) == 2
    assert page.items[0].doi == "10.1234/citer.0001"
    assert page.items[1].arxiv_id == "2305.00002"
    assert page.items[1].doi is None
    assert page.has_more is True  # fixture's "next": 2


def test_s2_get_citations_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _s2_citations_url("DOI:10.9999/missing", offset=0, limit=10),
        json_body=load_fixture("semanticscholar_not_found.json"), status_code=404,
    )
    provider = SemanticScholarProvider(transport, _s2_cfg())

    with pytest.raises(ProviderNotFoundError):
        provider.get_citations("10.9999/missing", limit=10, offset=0)
