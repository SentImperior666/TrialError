"""OpenAlex ``Provider`` client. Shapes grounded in
``docs/mining/S1-scilit-1__openalex-api.md`` (itself corroborated against
paper-qa's real, tested ``src/paperqa/clients/openalex.py`` client) --
every URL-building/field-selection convention below cites the doc line it
came from.

Confirmed conventions this module implements (mining report, "Confirmed
from paper-qa's live client code" section):

- DOI lookup: ``GET /works/https://doi.org/<url-encoded-doi>`` -- the DOI
  embedded as a full URL *inside* the path (unusual but the doc calls this
  "confirmed exact shape from working code, not just docs").
- Title search: ``GET /works?filter=title.search:<title>``.
- Field selection: ``select=<comma-joined-fields>``.
- Auth is optional and additive: ``mailto=<email>`` query param for the
  "polite pool"; a separate ``api_key`` HEADER (not query param) for
  premium features.
- Response envelope for listing endpoints: ``{"meta": {count, page,
  per_page, cost_usd}, "results": [...] }``; a single-entity GET (the DOI
  lookup) returns the Work object directly, unwrapped.

TRIALERROR-DEV-NOTE (unconfirmed, flagged per the mining report's own
caveats): the full Work object field list was never independently
fetched (404s on the schema sub-pages the mining session tried) --
``abstract_inverted_index``/``authorships``/``primary_location``/
``open_access``/``cited_by_count``/``referenced_works`` are the mining
brief's *expected* fields, not independently confirmed against a live
response in that session. This module's field mapping (:data:`SELECT_FIELDS`,
:func:`_work_to_record`) is built against those expected field names; a
live-smoke-test run (``tests/test_litapi_live_smoke.py``,
``TRIALERROR_LITAPI_LIVE_TESTS=1``) is the place a real mismatch would surface.

TRIALERROR-DEV-NOTE (get_by_arxiv, a deliberate design choice not literally
in the mining report): OpenAlex does not document a dedicated
arXiv-ID lookup path. arXiv has self-assigned DOIs to its preprints since
2022 (``10.48550/arXiv.<id>``, see ``trialerror.litapi.models.arxiv_to_doi``),
and OpenAlex indexes works by DOI -- so :meth:`OpenAlexProvider.get_by_arxiv`
resolves via that synthesized DOI through the same DOI-lookup path rather
than a separate endpoint. Not independently verified against a live
OpenAlex record in this session; flagged for the live-smoke follow-up.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from trialerror.litapi.config import ProviderApiConfig, resolve_api_key
from trialerror.litapi.errors import ProviderNotFoundError, ProviderTransportError
from trialerror.litapi.models import CitationEdge, CitationsPage, WorkRecord, arxiv_to_doi, normalize_arxiv_id, normalize_doi
from trialerror.litapi.providers.base import RateLimiter, build_headers, get_with_retry, raise_for_transport_error
from trialerror.litapi.transport import ProviderTransport, TransportResponse

__all__ = ["OpenAlexProvider", "SELECT_FIELDS"]

#: The mining report's "expected fields" (see this module's docstring
#: TRIALERROR-DEV-NOTE) plus the always-present ``id``/``doi``/``title``/
#: ``publication_year``/``ids`` identity fields.
SELECT_FIELDS: tuple[str, ...] = (
    "id",
    "doi",
    "ids",
    "title",
    "publication_year",
    "authorships",
    "primary_location",
    "open_access",
    "cited_by_count",
    "abstract_inverted_index",
    "referenced_works",
)


def _short_openalex_id(full_id: str | None) -> str | None:
    """``"https://openalex.org/W123"`` -> ``"W123"``; already-short ids
    pass through unchanged."""
    if not full_id:
        return None
    return full_id.rsplit("/", 1)[-1]


def _reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """OpenAlex ships abstracts as an ``abstract_inverted_index``
    (``{word: [position, ...]}``, a copyright-avoidance encoding OpenAlex
    itself uses) rather than plain text. Reconstructs the plain-text
    abstract by placing each word at its position(s) -- the standard,
    documented way to read this field."""
    if not inverted_index:
        return None
    slots: dict[int, str] = {}
    max_pos = -1
    for word, positions in inverted_index.items():
        for pos in positions:
            slots[pos] = word
            max_pos = max(max_pos, pos)
    if max_pos < 0:
        return None
    return " ".join(slots.get(i, "") for i in range(max_pos + 1)).strip() or None


def _work_to_record(data: dict) -> WorkRecord:
    authorships = data.get("authorships") or []
    authors = [
        a.get("author", {}).get("display_name")
        for a in authorships
        if a.get("author", {}).get("display_name")
    ]
    primary_location = data.get("primary_location") or {}
    open_access = data.get("open_access") or {}
    oa_pdf_url = primary_location.get("pdf_url") or open_access.get("oa_url")
    venue = (primary_location.get("source") or {}).get("display_name")
    openalex_id = _short_openalex_id(data.get("id"))
    external_ids: dict[str, str] = {}
    if openalex_id:
        external_ids["openalex"] = openalex_id
    ids_block = data.get("ids") or {}
    if ids_block.get("mag"):
        external_ids["mag"] = str(ids_block["mag"])

    return WorkRecord(
        title=data.get("title"),
        doi=normalize_doi(data.get("doi")),
        arxiv_id=None,  # OpenAlex responses don't carry a native arxiv_id field; see module docstring
        authors=authors,
        year=data.get("publication_year"),
        venue=venue,
        abstract=_reconstruct_abstract(data.get("abstract_inverted_index")),
        citation_count=data.get("cited_by_count"),
        oa_pdf_url=oa_pdf_url,
        url=data.get("id"),
        external_ids=external_ids,
        other={"referenced_works": data.get("referenced_works", [])},
    )


class OpenAlexProvider:
    name = "openalex"

    def __init__(self, transport: ProviderTransport, config: ProviderApiConfig, *, program_root=None):
        self.transport = transport
        self.config = config
        self._api_key = resolve_api_key(config, program_root=program_root)
        self._rate_limiter = RateLimiter(config.min_interval_s)

    # -- URL building ------------------------------------------------------

    def _query(self, extra: dict[str, str]) -> dict[str, str]:
        q = {"select": ",".join(SELECT_FIELDS), **extra}
        if self.config.mailto:
            q["mailto"] = self.config.mailto
        return q

    def _get(self, path: str, extra_query: dict[str, str]) -> TransportResponse:
        url = f"{self.config.base_url}{path}?{urlencode(self._query(extra_query))}"
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

    # -- Provider interface --------------------------------------------------

    def get_by_doi(self, doi: str) -> WorkRecord | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        doi_path_segment = f"https://doi.org/{quote(normalized, safe='')}"
        response = self._get(f"/works/{doi_path_segment}", {})
        if response.status_code == 404:
            raise ProviderNotFoundError(f"OpenAlex: no work found for DOI {doi!r}", provider=self.name)
        raise_for_transport_error(response, provider=self.name, context=f"get_by_doi({doi!r})")
        if not isinstance(response.json_body, dict):
            raise ProviderTransportError(
                f"OpenAlex get_by_doi({doi!r}): non-JSON-object response body", provider=self.name,
                status_code=response.status_code,
            )
        return _work_to_record(response.json_body)

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None:
        """See this module's docstring TRIALERROR-DEV-NOTE: resolved via the
        arXiv-self-assigned DOI, not a dedicated arXiv endpoint."""
        normalized = normalize_arxiv_id(arxiv_id)
        synthesized_doi = arxiv_to_doi(normalized)
        if not synthesized_doi:
            return None
        try:
            record = self.get_by_doi(synthesized_doi)
        except ProviderNotFoundError:
            raise ProviderNotFoundError(
                f"OpenAlex: no work found for arXiv id {arxiv_id!r} "
                f"(tried via synthesized DOI {synthesized_doi!r})",
                provider=self.name,
            ) from None
        if record is not None:
            record.arxiv_id = normalized
        return record

    def search(self, query: str, *, limit: int = 10) -> list[WorkRecord]:
        response = self._get("/works", {"filter": f"title.search:{query}", "per-page": str(max(1, min(limit, 100)))})
        raise_for_transport_error(response, provider=self.name, context=f"search({query!r})")
        body = response.json_body or {}
        results = body.get("results", []) if isinstance(body, dict) else []
        return [_work_to_record(r) for r in results[:limit]]

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage:
        """``identifier`` may be a DOI or an OpenAlex work id (short
        ``W123`` or the full ``https://openalex.org/W123`` form).
        TRIALERROR-DEV-NOTE (scope limitation, disclosed): OpenAlex paginates
        listing endpoints via ``page``/``per-page``, not a raw byte
        offset; this maps ``offset`` to a page number assuming ``offset``
        is page-aligned (``offset == (page - 1) * limit``), which holds
        for straightforward sequential paging (page 1, then page 2 at
        ``offset=limit``, ...) but not for an arbitrary offset a caller
        might otherwise expect an offset-based API to support."""
        limit = max(1, min(limit, 100))
        page = (offset // limit) + 1
        openalex_id = identifier
        if "/" in identifier or identifier.count(".") >= 1:
            # looks like a DOI, not a bare/URL-form OpenAlex id -- resolve first.
            resolved = self.get_by_doi(identifier)
            if resolved is None or "openalex" not in resolved.external_ids:
                raise ProviderNotFoundError(
                    f"OpenAlex: could not resolve {identifier!r} to a work id for citations lookup",
                    provider=self.name,
                )
            openalex_id = resolved.external_ids["openalex"]
        else:
            openalex_id = _short_openalex_id(identifier) or identifier

        response = self._get("/works", {"filter": f"cites:{openalex_id}", "per-page": str(limit), "page": str(page)})
        raise_for_transport_error(response, provider=self.name, context=f"get_citations({identifier!r})")
        body = response.json_body or {}
        results = body.get("results", []) if isinstance(body, dict) else []
        meta = body.get("meta", {}) if isinstance(body, dict) else {}
        total = meta.get("count")
        items = [
            CitationEdge(
                title=r.get("title"),
                doi=normalize_doi(r.get("doi")),
                arxiv_id=None,
                year=r.get("publication_year"),
                authors=[
                    a.get("author", {}).get("display_name")
                    for a in (r.get("authorships") or [])
                    if a.get("author", {}).get("display_name")
                ],
                external_ids={"openalex": _short_openalex_id(r.get("id"))} if r.get("id") else {},
            )
            for r in results
        ]
        has_more = isinstance(total, int) and (offset + len(items)) < total
        return CitationsPage(items=items, provider=self.name, offset=offset, limit=limit, total=total, has_more=has_more)
