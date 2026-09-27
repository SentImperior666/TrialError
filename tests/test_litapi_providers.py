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

    assert record.title == "A Fixture Paper About Distributed Systems Metadata Reconciliation"
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
    assert record.title == "A Fixture Paper About Distributed Systems Metadata Reconciliation"


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
        _openalex_works_url({"filter": "title.search:distributed systems", "per-page": "2"}),
        json_body=load_fixture("openalex_citations_page.json"),
    )
    provider = OpenAlexProvider(transport, _openalex_cfg())

    records = provider.search("distributed systems", limit=2)

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
    transport.add_json(_s2_search_url("distributed systems", 5), json_body=body)
    provider = SemanticScholarProvider(transport, _s2_cfg())

    records = provider.search("distributed systems", limit=5)

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


# ---------------------------------------------------------------------------
# lane FB-acq item 2: a 429 through a REAL provider, against FakeTransport's
# new add_sequence -- "429 then 200" is a fixture neither add_response half
# can express on its own.
# ---------------------------------------------------------------------------


def test_fake_transport_add_sequence_returns_successive_responses_then_repeats():
    from trialerror.litapi.transport import TransportResponse

    transport = FakeTransport()
    transport.add_sequence(
        "http://x",
        [TransportResponse(status_code=429), TransportResponse(status_code=200, json_body={"n": 2})],
    )

    assert [transport.get("http://x").status_code for _ in range(4)] == [429, 200, 200, 200]
    assert len(transport.calls) == 4


def test_fake_transport_add_sequence_refuses_an_empty_sequence():
    transport = FakeTransport()
    with pytest.raises(ValueError):
        transport.add_sequence("http://x", [])


def test_openalex_retries_a_429_then_succeeds_and_reports_its_stats():
    """FAILS BEFORE this lane: 429 was not in openalex's retry_on_status, so
    the first response was returned as-is and the lookup failed."""
    from trialerror.litapi.providers.base import RateLimiter
    from trialerror.litapi.transport import TransportResponse

    url = _openalex_doi_url("10.1000/example")
    transport = FakeTransport()
    body = load_fixture("openalex_doi_hit.json")
    transport.add_sequence(
        url,
        [
            TransportResponse(status_code=429, json_body={"error": "rate limited"}, headers={"Retry-After": "2"}),
            TransportResponse(status_code=200, json_body=body, text=""),
        ],
    )
    provider = OpenAlexProvider(transport, _openalex_cfg(retry_attempts=3, retry_on_status=(429, 500)))
    slept: list[float] = []
    provider._rate_limiter = RateLimiter(0.0, _sleep_fn=slept.append)

    record = provider.get_by_doi("10.1000/example")

    assert record is not None
    assert slept == [2.0]
    assert provider.last_request_stats["attempts"] == 2
    assert provider.last_request_stats["waited_s"] == 2.0
    assert provider.last_request_stats["request_sent"] is True


def test_a_provider_given_a_pacing_dir_writes_its_own_stamp_file(tmp_path):
    """The stamp is per PROVIDER: two providers sharing one pacing dir never
    pace against each other's requests."""
    transport = FakeTransport()
    openalex = OpenAlexProvider(transport, _openalex_cfg(min_interval_s=1.0), pacing_dir=tmp_path)
    s2 = SemanticScholarProvider(transport, _s2_cfg(min_interval_s=1.0), pacing_dir=tmp_path)

    assert openalex._rate_limiter.stamp_path == tmp_path / "openalex.json"
    assert s2._rate_limiter.stamp_path == tmp_path / "semanticscholar.json"

    # and the default -- what every other test and library caller gets -- is no
    # stamp at all, so nothing is written anywhere.
    assert OpenAlexProvider(transport, _openalex_cfg())._rate_limiter.stamp_path is None


# ---------------------------------------------------------------------------
# lane SI items A1/A2: work type, author ids, citation curve; references,
# author works, filtered/sorted citing works.
# ---------------------------------------------------------------------------


def _s2_references_url(paper_id: str, *, limit: int) -> str:
    from trialerror.litapi.providers.semanticscholar import _CITATION_FIELDS

    q = {"offset": "0", "limit": str(limit), "fields": ",".join(_CITATION_FIELDS)}
    return f"{S2_BASE}/graph/v1/paper/{quote(paper_id, safe=':')}/references?{urlencode(q)}"


def _s2_author_papers_url(author_id: str, *, limit: int) -> str:
    q = {"fields": ",".join(FIELDS), "limit": str(limit)}
    return f"{S2_BASE}/graph/v1/author/{quote(author_id, safe='')}/papers?{urlencode(q)}"


def test_openalex_select_fields_ask_for_type_and_counts_by_year():
    assert "type" in SELECT_FIELDS
    assert "counts_by_year" in SELECT_FIELDS


def test_openalex_record_carries_work_type_author_ids_counts_by_year():
    """FAILS BEFORE lane SI: the record dropped ``type``, ``counts_by_year`` and
    every ``authorships[].author.id``."""
    transport = FakeTransport()
    transport.add_json(_openalex_doi_url("10.1234/widgets.review"), json_body=load_fixture("openalex_work_review.json"))
    provider = OpenAlexProvider(transport, _openalex_cfg())

    record = provider.get_by_doi("10.1234/widgets.review")

    assert record.other["work_type"] == "review"
    assert record.other["counts_by_year"] == [
        {"year": 2017, "cited_by_count": 5},
        {"year": 2016, "cited_by_count": 9},
        {"year": 2015, "cited_by_count": 3},
    ]
    # authorship order; None for the authorship that carries no id; aligned
    # position-for-position with ``authors``.
    assert record.other["author_ids"] == ["A5000000001", None, "A5000000003"]
    assert record.authors == ["Ann Author", "Ben Nobody", "Cat Writer"]
    assert record.other["referenced_works"] == ["https://openalex.org/W1"]


def test_s2_record_carries_author_ids_and_work_type():
    transport = FakeTransport()
    transport.add_json(_s2_paper_url("DOI:10.1234/fixture.5678"), json_body=load_fixture("semanticscholar_doi_hit.json"))
    provider = SemanticScholarProvider(transport, _s2_cfg())

    record = provider.get_by_doi("10.1234/fixture.5678")

    assert record.other["author_ids"] == ["1000", "1001"]
    assert record.other["work_type"] == "journalarticle"
    assert record.other["publicationTypes"] == ["JournalArticle"]  # the raw list is still kept


def test_s2_record_with_no_publication_types_has_work_type_none():
    transport = FakeTransport()
    body = {"paperId": "p1", "title": "T", "externalIds": {"DOI": "10.1/t"}, "publicationTypes": None,
            "authors": [{"authorId": None, "name": "No Id"}]}
    transport.add_json(_s2_paper_url("DOI:10.1/t"), json_body=body)

    record = SemanticScholarProvider(transport, _s2_cfg()).get_by_doi("10.1/t")

    assert record.other["work_type"] is None
    assert record.other["author_ids"] == [None]


def test_openalex_citation_edges_carry_work_type_and_citation_count():
    transport = FakeTransport()
    transport.add_json(
        _openalex_works_url({"filter": "cites:W2741809807", "per-page": "2", "page": "1"}),
        json_body=load_fixture("openalex_citations_page.json"),
    )
    page = OpenAlexProvider(transport, _openalex_cfg()).get_citations("W2741809807", limit=2)

    assert [e.citation_count for e in page.items] == [3, 0]
    assert [e.work_type for e in page.items] == [None, None]  # the fixture carries no ``type``
    assert set(page.items[0].to_dict()) >= {"work_type", "citation_count"}


def test_s2_citation_fields_ask_for_citation_count_and_publication_types():
    from trialerror.litapi.providers.semanticscholar import _CITATION_FIELDS

    assert "citationCount" in _CITATION_FIELDS
    assert "publicationTypes" in _CITATION_FIELDS


def test_openalex_get_citations_default_url_unchanged():
    """Pins the default URL as a literal string, so the new ``work_type``/``sort``
    keywords cannot drift it: no ``type:`` filter clause, no ``sort`` param,
    parameter order select, filter, per-page, page. (The ``select`` value itself
    grew by ``type,counts_by_year`` in item A1.)"""
    transport = FakeTransport()
    url = (
        "https://api.openalex.org/works?select=id%2Cdoi%2Cids%2Ctitle%2Cpublication_year%2Cauthorships"
        "%2Cprimary_location%2Copen_access%2Ccited_by_count%2Cabstract_inverted_index%2Creferenced_works"
        "%2Ctype%2Ccounts_by_year&filter=cites%3AW2741809807&per-page=2&page=1"
    )
    transport.add_json(url, json_body=load_fixture("openalex_citations_page.json"))

    OpenAlexProvider(transport, _openalex_cfg()).get_citations("W2741809807", limit=2, offset=0)

    assert [c["url"] for c in transport.calls] == [url]


def test_openalex_get_citations_review_filter_and_sort():
    transport = FakeTransport()
    url = _openalex_works_url(
        {"filter": "cites:W2741809807,type:review", "per-page": "10", "page": "1", "sort": "cited_by_count:desc"}
    )
    transport.add_json(url, json_body=load_fixture("openalex_citations_page.json"))

    page = OpenAlexProvider(transport, _openalex_cfg()).get_citations(
        "W2741809807", limit=10, work_type="review", sort="cited_by_count:desc"
    )

    assert transport.calls[-1]["url"] == url
    assert page.provider == "openalex"


def test_openalex_get_references_uses_cited_by_filter_sorted():
    """FAILS BEFORE lane SI: OpenAlexProvider had no get_references."""
    transport = FakeTransport()
    transport.add_json(_openalex_doi_url("10.1234/fixture.5678"), json_body=load_fixture("openalex_doi_hit.json"))
    url = _openalex_works_url({"filter": "cited_by:W2741809807", "per-page": "2", "sort": "cited_by_count:desc"})
    transport.add_json(url, json_body=load_fixture("openalex_references_page.json"))
    provider = OpenAlexProvider(transport, _openalex_cfg())

    page = provider.get_references("10.1234/fixture.5678", limit=2)

    assert transport.calls[-1]["url"] == url  # resolved DOI -> W id first, exactly as get_citations does
    assert page.provider == "openalex"
    assert [e.title for e in page.items] == ["A Study of Widgets", "Further Notes on Widgets"]
    assert [e.work_type for e in page.items] == ["book", "article"]
    assert [e.citation_count for e in page.items] == [250, 40]
    assert page.total == 3
    assert page.has_more is True


def test_openalex_get_author_works_since_year():
    transport = FakeTransport()
    url = _openalex_works_url(
        {"filter": "author.id:A5000000001,from_publication_date:2016-01-01", "sort": "cited_by_count:desc",
         "per-page": "5"}
    )
    transport.add_json(url, json_body=load_fixture("openalex_author_works.json"))
    provider = OpenAlexProvider(transport, _openalex_cfg())

    records = provider.get_author_works("https://openalex.org/A5000000001", since_year=2016, limit=5)

    assert transport.calls[-1]["url"] == url
    assert [r.title for r in records] == ["Widgets Revisited", "A Handbook of Widgets"]
    assert records[1].other["work_type"] == "book"


def test_openalex_get_author_works_without_since_year_has_no_date_clause():
    transport = FakeTransport()
    url = _openalex_works_url({"filter": "author.id:A5000000001", "sort": "cited_by_count:desc", "per-page": "10"})
    transport.add_json(url, json_body=load_fixture("openalex_author_works.json"))

    records = OpenAlexProvider(transport, _openalex_cfg()).get_author_works("A5000000001")

    assert len(records) == 2


def test_s2_get_references_reads_cited_paper_rows():
    transport = FakeTransport()
    transport.add_json(
        _s2_references_url("DOI:10.1234/fixture.5678", limit=20),
        json_body=load_fixture("semanticscholar_references_page.json"),
    )

    page = SemanticScholarProvider(transport, _s2_cfg()).get_references("10.1234/fixture.5678")

    assert page.provider == "semanticscholar"
    assert [e.title for e in page.items] == ["A Study of Widgets", "Further Notes on Widgets"]
    assert [e.work_type for e in page.items] == ["book", None]
    assert [e.citation_count for e in page.items] == [250, 40]
    assert page.has_more is False


def test_s2_get_references_not_found_raises():
    transport = FakeTransport()
    transport.add_json(
        _s2_references_url("DOI:10.9999/missing", limit=20),
        json_body=load_fixture("semanticscholar_not_found.json"), status_code=404,
    )
    with pytest.raises(ProviderNotFoundError):
        SemanticScholarProvider(transport, _s2_cfg()).get_references("10.9999/missing")


def test_s2_get_author_works_filters_by_year():
    transport = FakeTransport()
    transport.add_json(_s2_author_papers_url("1000", limit=8), json_body=load_fixture("semanticscholar_author_papers.json"))

    provider = SemanticScholarProvider(transport, _s2_cfg())
    recent = provider.get_author_works("1000", since_year=2010, limit=8)
    everything = provider.get_author_works("1000", limit=8)

    # the endpoint has no year filter: the same URL both times, filtered here.
    assert {c["url"] for c in transport.calls} == {_s2_author_papers_url("1000", limit=8)}
    assert [r.title for r in recent] == ["Widgets Revisited"]  # 2004 and the undated paper dropped
    assert [r.title for r in everything] == ["Widgets Revisited", "Early Widget Notes", "An Undated Widget Note"]


def test_s2_get_citations_refuses_work_type_and_sort_before_any_request():
    from trialerror.litapi.errors import ProviderUnsupportedOperationError

    transport = FakeTransport()
    provider = SemanticScholarProvider(transport, _s2_cfg())

    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_citations("10.1234/fixture.5678", work_type="review")
    with pytest.raises(ProviderUnsupportedOperationError):
        provider.get_citations("10.1234/fixture.5678", sort="cited_by_count:desc")
    assert transport.calls == []


def test_s2_get_citations_edges_carry_work_type_and_citation_count():
    transport = FakeTransport()
    body = {"offset": 0, "next": None, "data": [{"citingPaper": {
        "paperId": "d1", "title": "A Survey of Widgets", "externalIds": {}, "year": 2020,
        "citationCount": 7, "publicationTypes": ["Review", "JournalArticle"], "authors": []}}]}
    transport.add_json(_s2_citations_url("DOI:10.1234/fixture.5678", offset=0, limit=10), json_body=body)

    page = SemanticScholarProvider(transport, _s2_cfg()).get_citations("10.1234/fixture.5678", limit=10, sort=None)

    assert page.items[0].work_type == "review"
    assert page.items[0].citation_count == 7
