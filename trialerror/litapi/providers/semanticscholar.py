"""Semantic Scholar ``Provider`` client. Shapes grounded in
``docs/mining/S1-scilit-1__semantic-scholar-api.md`` (corroborated against
paper-qa's real, tested ``src/paperqa/clients/semantic_scholar.py``
client) -- every URL-building/field convention below cites the doc line
it came from.

Confirmed conventions this module implements (mining report table +
"Confirmed from paper-qa's live client code" prose):

- Paper by DOI: ``GET /graph/v1/paper/DOI:{doi}`` (explicit DOI-prefixed
  path segment).
- Paper by ID (S2 id / DOI / ArXiv, etc.): ``GET /graph/v1/paper/{paperId}``.
- Relevance search: ``GET /graph/v1/paper/search`` (``query``, ``offset``,
  ``limit`` params).
- Citations: ``GET /graph/v1/paper/{paperId}/citations`` (paginated via
  ``limit``).
- Field selection via ``fields=<comma-joined>`` (see :data:`FIELDS`, taken
  near-verbatim from the doc's own confirmed field-mapping list).
- Auth: API key as a HEADER (not query param/Bearer) -- the standard S2
  header name is ``x-api-key``; this module makes it config-overridable
  (``[litapi.semanticscholar].api_key_header``) rather than hardcoding it,
  since the mining report itself only confirmed "a dedicated header name,
  not a query param or Bearer token", not the literal header string.

TRIALERROR-DEV-NOTE / TODO (get_by_arxiv path prefix, flagged): the mining
report's endpoint table confirms "ID can be S2 ID, DOI (``DOI:<doi>``),
ArXiv, etc." for the generic by-id endpoint, but does not spell out the
exact ArXiv prefix casing. This module uses ``ARXIV:<id>`` (Semantic
Scholar's documented external-id-type convention family --
DOI/ARXIV/MAG/ACL/PMID/PMCID/CorpusID) -- not independently re-confirmed
against a live response in this session; the live-smoke test
(``TRIALERROR_LITAPI_LIVE_TESTS=1``) is the place a mismatch would surface.

TRIALERROR-DEV-NOTE (citations response envelope, flagged): the mining report
documents the citations endpoint's PATH but not its response envelope
shape. This module assumes ``{"offset": int, "next": int|null, "data":
[{"citingPaper": {...fields}}]}`` -- Semantic Scholar's documented
convention for paginated relationship endpoints generally, mirrored in
``tests/fixtures/litapi/semanticscholar_citations_page.json`` -- not
verbatim-confirmed by the mining pass itself. Same follow-up applies.

TRIALERROR-DEV-NOTE (deliberate deviation from paper-qa's own behavior,
disclosed): the mining report notes paper-qa "gives ArXiv-derived DOI
precedence over a native DOI field when both exist" in ``externalIds``.
This module does NOT mirror that specific precedence -- it prefers the
NATIVE DOI when Semantic Scholar returns one (matching the mission
brief's plain "DOI-preferred identity" instruction and
``trialerror.litapi.models.WorkRecord.identity_key``'s own fallback order),
and always captures the arXiv id separately regardless. Reasoning: the
mission's own reconciliation rule already falls back to the
arXiv-synthesized DOI only when NO native DOI exists (see
``models.WorkRecord.identity_key``), so mirroring paper-qa's inverted
preference here would fight that rule rather than match it. Flagged since
it is a knowing divergence from an explicitly-cited upstream pattern, not
an oversight.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from trialerror.litapi.config import ProviderApiConfig, resolve_api_key
from trialerror.litapi.errors import ProviderNotFoundError, ProviderTransportError
from trialerror.litapi.models import CitationEdge, CitationsPage, WorkRecord, normalize_arxiv_id, normalize_doi
from trialerror.litapi.providers.base import RateLimiter, build_headers, get_with_retry, raise_for_transport_error
from trialerror.litapi.transport import ProviderTransport, TransportResponse

__all__ = ["SemanticScholarProvider", "FIELDS"]

#: Near-verbatim from the mining report's confirmed field-mapping list,
#: plus ``abstract`` (a standard S2 field, added beyond the report's own
#: literal example list -- see this module's docstring).
FIELDS: tuple[str, ...] = (
    "title",
    "externalIds",
    "authors",
    "publicationDate",
    "year",
    "journal",
    "url",
    "openAccessPdf",
    "citationStyles",
    "isOpenAccess",
    "influentialCitationCount",
    "publicationTypes",
    "venue",
    "citationCount",
    "abstract",
)

_CITATION_FIELDS: tuple[str, ...] = ("title", "externalIds", "authors", "year")
_KNOWN_ID_PREFIXES = {"DOI", "ARXIV", "CORPUSID", "MAG", "ACL", "PMID", "PMCID"}


def _coerce_paper_id(identifier: str) -> str:
    """Accept a bare S2 paper id, an already-prefixed id
    (``"DOI:10.1234/x"``, ``"ARXIV:2101.00001"``, ...), or a plain DOI
    string -- normalizing only the last case, since S2's by-id endpoint
    family (mining report: "ID can be S2 ID, DOI, ArXiv, etc.") expects
    the caller to supply the right prefix itself for anything but a bare
    S2 id."""
    if ":" in identifier and identifier.split(":", 1)[0].upper() in _KNOWN_ID_PREFIXES:
        return identifier
    normalized = normalize_doi(identifier)
    if normalized and "/" in normalized:
        return f"DOI:{normalized}"
    return identifier


def _extract_doi_and_arxiv(external_ids: dict) -> tuple[str | None, str | None]:
    """See this module's docstring TRIALERROR-DEV-NOTE: native DOI preferred,
    arXiv id captured separately (not folded into ``doi`` here even when
    present -- reconciliation's own fallback handles the arxiv->DOI
    normalization when no native DOI exists)."""
    doi = normalize_doi(external_ids.get("DOI"))
    arxiv_id = normalize_arxiv_id(external_ids.get("ArXiv"))
    return doi, arxiv_id


def _paper_to_record(data: dict) -> WorkRecord:
    external_ids = data.get("externalIds") or {}
    doi, arxiv_id = _extract_doi_and_arxiv(external_ids)
    authors = [a.get("name") for a in (data.get("authors") or []) if a.get("name")]
    journal = data.get("journal") or {}
    venue = data.get("venue") or journal.get("name")
    oa_pdf = data.get("openAccessPdf") or {}
    other_external: dict[str, str] = {}
    if external_ids.get("CorpusId") is not None:
        other_external["corpus_id"] = str(external_ids["CorpusId"])
    if data.get("paperId"):
        other_external["semanticscholar"] = data["paperId"]

    return WorkRecord(
        title=data.get("title"),
        doi=doi,
        arxiv_id=arxiv_id,
        authors=authors,
        year=data.get("year"),
        venue=venue,
        abstract=data.get("abstract"),
        citation_count=data.get("citationCount"),
        oa_pdf_url=oa_pdf.get("url"),
        url=data.get("url"),
        external_ids=other_external,
        other={
            "influentialCitationCount": data.get("influentialCitationCount"),
            "isOpenAccess": data.get("isOpenAccess"),
            "publicationTypes": data.get("publicationTypes"),
            "bibtex": (data.get("citationStyles") or {}).get("bibtex"),
        },
    )


class SemanticScholarProvider:
    name = "semanticscholar"

    def __init__(self, transport: ProviderTransport, config: ProviderApiConfig, *, program_root=None):
        self.transport = transport
        self.config = config
        self._api_key = resolve_api_key(config, program_root=program_root)
        self._rate_limiter = RateLimiter(config.min_interval_s)

    # -- URL building --------------------------------------------------------

    def _get(self, path: str, query: dict[str, str]) -> TransportResponse:
        url = f"{self.config.base_url}{path}?{urlencode(query)}"
        headers = build_headers(self.config, self._api_key)
        return get_with_retry(
            self.transport,
            url,
            provider=self.name,
            headers=headers,
            timeout_s=self.config.timeout_s,
            rate_limiter=self._rate_limiter,
            retry_attempts=self.config.retry_attempts,
            retry_on_status=self.config.retry_on_status,
        )

    def _get_by_paper_id(self, paper_id: str) -> dict | None:
        response = self._get(f"/graph/v1/paper/{quote(paper_id, safe=':')}", {"fields": ",".join(FIELDS)})
        if response.status_code == 404:
            return None
        raise_for_transport_error(response, provider=self.name, context=f"get_by_id({paper_id!r})")
        if not isinstance(response.json_body, dict):
            raise ProviderTransportError(
                f"Semantic Scholar get_by_id({paper_id!r}): non-JSON-object response body",
                provider=self.name, status_code=response.status_code,
            )
        return response.json_body

    # -- Provider interface --------------------------------------------------

    def get_by_doi(self, doi: str) -> WorkRecord | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        data = self._get_by_paper_id(f"DOI:{normalized}")
        if data is None:
            raise ProviderNotFoundError(f"Semantic Scholar: no paper found for DOI {doi!r}", provider=self.name)
        return _paper_to_record(data)

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None:
        normalized = normalize_arxiv_id(arxiv_id)
        if not normalized:
            return None
        data = self._get_by_paper_id(f"ARXIV:{normalized}")
        if data is None:
            raise ProviderNotFoundError(
                f"Semantic Scholar: no paper found for arXiv id {arxiv_id!r}", provider=self.name
            )
        record = _paper_to_record(data)
        if not record.arxiv_id:
            record.arxiv_id = normalized
        return record

    def search(self, query: str, *, limit: int = 10) -> list[WorkRecord]:
        response = self._get(
            "/graph/v1/paper/search",
            {"query": query, "limit": str(max(1, min(limit, 100))), "fields": ",".join(FIELDS)},
        )
        raise_for_transport_error(response, provider=self.name, context=f"search({query!r})")
        body = response.json_body or {}
        results = body.get("data", []) if isinstance(body, dict) else []
        return [_paper_to_record(r) for r in results[:limit]]

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage:
        """``identifier`` may be a bare Semantic Scholar paper id, or a
        DOI/arXiv id (passed straight through with the matching prefix --
        S2's by-id endpoint family accepts these directly, per the mining
        report's "ID can be S2 ID, DOI, ArXiv, etc." confirmation, so no
        separate resolve step is needed here, unlike OpenAlex)."""
        paper_id = _coerce_paper_id(identifier)
        limit = max(1, min(limit, 1000))
        response = self._get(
            f"/graph/v1/paper/{quote(paper_id, safe=':')}/citations",
            {"offset": str(offset), "limit": str(limit), "fields": ",".join(_CITATION_FIELDS)},
        )
        if response.status_code == 404:
            raise ProviderNotFoundError(
                f"Semantic Scholar: no paper found for citations lookup {identifier!r}", provider=self.name
            )
        raise_for_transport_error(response, provider=self.name, context=f"get_citations({identifier!r})")
        body = response.json_body or {}
        rows = body.get("data", []) if isinstance(body, dict) else []
        items: list[CitationEdge] = []
        for row in rows:
            paper = row.get("citingPaper") or {}
            external_ids = paper.get("externalIds") or {}
            doi, arxiv_id = _extract_doi_and_arxiv(external_ids)
            items.append(
                CitationEdge(
                    title=paper.get("title"),
                    doi=doi,
                    arxiv_id=arxiv_id,
                    year=paper.get("year"),
                    authors=[a.get("name") for a in (paper.get("authors") or []) if a.get("name")],
                    external_ids={"semanticscholar": paper["paperId"]} if paper.get("paperId") else {},
                )
            )
        next_offset = body.get("next") if isinstance(body, dict) else None
        return CitationsPage(
            items=items, provider=self.name, offset=offset, limit=limit,
            total=body.get("total") if isinstance(body, dict) else None,
            has_more=next_offset is not None,
        )
