"""Not a test module -- shared builders for the source investigator's tests
(lane SI part B): real ``OpenAlexProvider``/``SemanticScholarProvider`` over one
``FakeTransport``, and the exact URLs each builds, computed with the same stdlib
primitives the providers use (``quote``/``urlencode``), as
``tests/test_litapi_providers.py`` does.

The fixture works are neutral (``tests/fixtures/litapi/investigate_*.json``):
"A Study of Widgets" (1986, DOI ``10.9999/widgets``), the unrelated work a
mistyped DOI ``10.9999/x1`` points at, and "A Theory of Gadgets" (1969).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlencode

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.providers.openalex import SELECT_FIELDS, OpenAlexProvider
from trialerror.litapi.providers.semanticscholar import FIELDS, SemanticScholarProvider
from trialerror.litapi.transport import FakeTransport, TransportResponse
from tests._litapi_fixtures import load_fixture

OPENALEX_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org"

#: The year every test pins the run to, so the author-works filter's
#: ``from_publication_date`` is known: ``YEAR - author_works_years``.
YEAR = 2026
SINCE_YEAR = YEAR - 10

WIDGETS_DOI = "10.9999/widgets"
WIDGETS_W = "W9000000001"
WIDGETS_AUTHORS = ("A9000000001", "A9000000002")
BOARDS_DOI = "10.9999/x1"
GADGETS_DOI = "10.9999/gadgets"
GADGETS_W = "W9400000001"

WIDGETS_SEED = "C. Writer, D. Other, J. Widget Studies 3(2), 1986, doi:10.9999/widgets"
WRONG_DOI_SEED = "C. Writer, D. Other, E. Third, J. Studies 29(1), 1986, doi:10.9999/x1"
TITLE_ONLY_SEED = 'C. Writer, "A Study of Widgets", 1986'
GADGETS_SEED = 'G. Early, "A Theory of Gadgets", 1969, doi:10.9999/gadgets'


def openalex_cfg() -> ProviderApiConfig:
    return ProviderApiConfig(
        name="openalex", base_url=OPENALEX_BASE, mailto=None, api_key_path=None, api_key_header="x-api-key",
        min_interval_s=0.0, retry_attempts=1, retry_on_status=(429, 500), timeout_s=5.0,
    )


def s2_cfg() -> ProviderApiConfig:
    return ProviderApiConfig(
        name="semanticscholar", base_url=S2_BASE, mailto=None, api_key_path=None, api_key_header="x-api-key",
        min_interval_s=0.0, retry_attempts=1, retry_on_status=(403, 429), timeout_s=5.0,
    )


def providers(transport: FakeTransport) -> list:
    """The default pair, in the client's default order."""
    return [OpenAlexProvider(transport, openalex_cfg()), SemanticScholarProvider(transport, s2_cfg())]


# -- URLs, exactly as the providers build them ---------------------------------


def oa_doi_url(doi: str) -> str:
    return f"{OPENALEX_BASE}/works/https://doi.org/{quote(doi, safe='')}?{urlencode({'select': ','.join(SELECT_FIELDS)})}"


def oa_works_url(extra: dict) -> str:
    return f"{OPENALEX_BASE}/works?{urlencode({'select': ','.join(SELECT_FIELDS), **extra})}"


def oa_search_url(query: str, limit: int = 5) -> str:
    return oa_works_url({"filter": f"title.search:{query}", "per-page": str(limit)})


def oa_citing_url(work_id: str, *, limit: int = 20) -> str:
    return oa_works_url({"filter": f"cites:{work_id}", "per-page": str(limit), "page": "1", "sort": "cited_by_count:desc"})


def oa_reviews_url(work_id: str, *, limit: int = 10) -> str:
    return oa_works_url({"filter": f"cites:{work_id},type:review", "per-page": str(limit), "page": "1"})


def oa_references_url(work_id: str, *, limit: int = 10) -> str:
    return oa_works_url({"filter": f"cited_by:{work_id}", "per-page": str(limit), "sort": "cited_by_count:desc"})


def oa_author_works_url(author_id: str, *, since_year: int = SINCE_YEAR, limit: int = 8) -> str:
    return oa_works_url(
        {"filter": f"author.id:{author_id},from_publication_date:{since_year}-01-01",
         "sort": "cited_by_count:desc", "per-page": str(limit)}
    )


def s2_paper_url(paper_id: str) -> str:
    return f"{S2_BASE}/graph/v1/paper/{quote(paper_id, safe=':')}?{urlencode({'fields': ','.join(FIELDS)})}"


def s2_search_url(query: str, limit: int = 5) -> str:
    return f"{S2_BASE}/graph/v1/paper/search?{urlencode({'query': query, 'limit': str(limit), 'fields': ','.join(FIELDS)})}"


EMPTY_PAGE = {"meta": {"count": 0, "page": 1}, "results": []}


def status(code: int, body: dict | None = None) -> TransportResponse:
    body = body if body is not None else {"error": f"HTTP {code}"}
    return TransportResponse(status_code=code, json_body=body, text=json.dumps(body))


# -- routes --------------------------------------------------------------------


def route_widgets_lookup(transport: FakeTransport) -> None:
    transport.add_json(oa_doi_url(WIDGETS_DOI), json_body=load_fixture("investigate_openalex_widgets.json"))
    transport.add_json(s2_paper_url(f"DOI:{WIDGETS_DOI}"), json_body=load_fixture("investigate_s2_widgets.json"))


def route_widgets_gather(transport: FakeTransport) -> None:
    transport.add_json(oa_citing_url(WIDGETS_W), json_body=load_fixture("investigate_openalex_citing.json"))
    transport.add_json(oa_reviews_url(WIDGETS_W), json_body=load_fixture("investigate_openalex_reviews.json"))
    transport.add_json(oa_references_url(WIDGETS_W), json_body=load_fixture("investigate_openalex_references.json"))
    transport.add_json(oa_author_works_url(WIDGETS_AUTHORS[0]), json_body=load_fixture("investigate_openalex_author_works.json"))
    transport.add_json(oa_author_works_url(WIDGETS_AUTHORS[1]), json_body=EMPTY_PAGE)


def route_widgets(transport: FakeTransport) -> None:
    """Everything a DOI seed for the widgets work asks: the two lookups, then
    the four gather calls (OpenAlex serves all four; its DOI lookup is asked
    again inside each listing to find the work id)."""
    route_widgets_lookup(transport)
    route_widgets_gather(transport)


def route_widgets_search(transport: FakeTransport, query: str = "A Study of Widgets") -> None:
    oa = load_fixture("investigate_openalex_widgets.json")
    s2 = load_fixture("investigate_s2_widgets.json")
    transport.add_json(oa_search_url(query), json_body={"meta": {"count": 1}, "results": [oa]})
    transport.add_json(s2_search_url(query), json_body={"total": 1, "offset": 0, "data": [s2]})


def route_wrong_doi(transport: FakeTransport) -> None:
    transport.add_json(oa_doi_url(BOARDS_DOI), json_body=load_fixture("investigate_openalex_boards.json"))
    transport.add_json(s2_paper_url(f"DOI:{BOARDS_DOI}"), json_body={"error": "Paper not found"}, status_code=404)


def route_gadgets(transport: FakeTransport) -> None:
    """The gadgets work: OpenAlex has it, Semantic Scholar does not; gather
    answers with empty pages and no author works."""
    transport.add_json(oa_doi_url(GADGETS_DOI), json_body=load_fixture("investigate_openalex_gadgets.json"))
    transport.add_json(s2_paper_url(f"DOI:{GADGETS_DOI}"), json_body={"error": "Paper not found"}, status_code=404)
    transport.add_json(oa_citing_url(GADGETS_W), json_body=EMPTY_PAGE)
    transport.add_json(oa_reviews_url(GADGETS_W), json_body=EMPTY_PAGE)
    transport.add_json(oa_references_url(GADGETS_W), json_body=EMPTY_PAGE)
    transport.add_json(oa_author_works_url("A9400000001"), json_body=EMPTY_PAGE)


def write_seeds(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def seed_row(seed_raw: str, *, list_id: str = "list-1", row_id: str = "row-1", question: str | None = None,
             literature=None) -> dict:
    row = {"list_id": list_id, "row_id": row_id, "seed_raw": seed_raw}
    if question is not None:
        row["question"] = question
    if literature is not None:
        row["literature"] = literature
    return row
