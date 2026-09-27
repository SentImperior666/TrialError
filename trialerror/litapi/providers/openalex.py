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

from pathlib import Path
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
    # lane SI item A1: the work's type (``article``, ``review``, ``book``, ...)
    # and its per-year citation counts.
    "type",
    "counts_by_year",
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


def _named_authorships(data: dict) -> list[dict]:
    """The authorships that carry a display name, in authorship order -- the
    one filter both ``authors`` and ``author_ids`` are built from, so position
    ``i`` in one is position ``i`` in the other."""
    return [
        (a.get("author") or {})
        for a in (data.get("authorships") or [])
        if (a.get("author") or {}).get("display_name")
    ]


def _counts_by_year(data: dict) -> list[dict]:
    """``counts_by_year`` as ``[{year, cited_by_count}]`` verbatim (only the two
    keys kept; OpenAlex also ships ``works_count`` for author entities)."""
    rows = data.get("counts_by_year") or []
    return [
        {"year": row.get("year"), "cited_by_count": row.get("cited_by_count")}
        for row in rows
        if isinstance(row, dict)
    ]


def _work_to_edge(r: dict) -> CitationEdge:
    """One listing row (citing work, referenced work) as a :class:`CitationEdge`."""
    return CitationEdge(
        title=r.get("title"),
        doi=normalize_doi(r.get("doi")),
        arxiv_id=None,
        year=r.get("publication_year"),
        authors=[a.get("display_name") for a in _named_authorships(r)],
        external_ids={"openalex": _short_openalex_id(r.get("id"))} if r.get("id") else {},
        work_type=r.get("type"),
        citation_count=r.get("cited_by_count"),
    )


def _work_to_record(data: dict) -> WorkRecord:
    named = _named_authorships(data)
    authors = [a.get("display_name") for a in named]
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
        other={
            "referenced_works": data.get("referenced_works", []),
            # lane SI item A1
            "work_type": data.get("type"),
            "counts_by_year": _counts_by_year(data),
            "author_ids": [_short_openalex_id(a.get("id")) for a in named],
        },
    )


class OpenAlexProvider:
    name = "openalex"
    #: FB-1 item F3: what this provider's `search` matches on.
    search_scope = "title only (filter=title.search)"

    def __init__(
        self, transport: ProviderTransport, config: ProviderApiConfig, *,
        program_root=None, pacing_dir=None,
    ):
        self.transport = transport
        self.config = config
        self._api_key = resolve_api_key(config, program_root=program_root)
        # lane FB-acq item 2: ``pacing_dir`` turns the in-memory rate limiter
        # into a cross-INVOCATION one (every CLI call is a new process, so the
        # in-memory gate alone spaced nothing across a shell loop). ``None``
        # -- the default every test and library caller gets -- keeps exactly
        # today's in-process behaviour and writes no files anywhere.
        self._rate_limiter = RateLimiter(
            config.min_interval_s,
            stamp_path=(Path(pacing_dir) / f"{self.name}.json") if pacing_dir else None,
        )
        #: The last request's :func:`get_with_retry` stats (attempts, total
        #: backoff waited, last status, Retry-After, whether a request went out
        #: at all), reset per request and read by
        #: ``trialerror.litapi.client._provider_outcome``.
        self.last_request_stats: dict = {}

    # -- URL building ------------------------------------------------------

    def _query(self, extra: dict[str, str]) -> dict[str, str]:
        q = {"select": ",".join(SELECT_FIELDS), **extra}
        if self.config.mailto:
            q["mailto"] = self.config.mailto
        return q

    def _get(self, path: str, extra_query: dict[str, str]) -> TransportResponse:
        url = f"{self.config.base_url}{path}?{urlencode(self._query(extra_query))}"
        headers = build_headers(self.config, self._api_key)
        self.last_request_stats = {}
        return get_with_retry(
            self.transport,
            url,
            provider=self.name,
            headers=headers,
            timeout_s=self.config.timeout_s,
            rate_limiter=self._rate_limiter,
            retry_attempts=self.config.retry_attempts,
            retry_on_status=self.config.retry_on_status,
            max_total_wait_s=self.config.max_total_wait_s,
            stats=self.last_request_stats,
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

    def _resolve_work_id(self, identifier: str, *, purpose: str) -> str:
        """A DOI is resolved to its OpenAlex work id first (one extra request);
        a bare ``W123`` is used as is. The test is the one ``get_citations``
        has always used -- anything holding a ``/`` or a ``.`` is resolved as a
        DOI -- so the full ``https://openalex.org/W123`` form goes through the
        DOI lookup too (kept unchanged here; lane SI names it in its report)."""
        if "/" in identifier or identifier.count(".") >= 1:
            # looks like a DOI, not a bare/URL-form OpenAlex id -- resolve first.
            resolved = self.get_by_doi(identifier)
            if resolved is None or "openalex" not in resolved.external_ids:
                raise ProviderNotFoundError(
                    f"OpenAlex: could not resolve {identifier!r} to a work id for {purpose} lookup",
                    provider=self.name,
                )
            return resolved.external_ids["openalex"]
        return _short_openalex_id(identifier) or identifier

    def _list_edges(self, query: dict[str, str], *, context: str, offset: int, limit: int) -> CitationsPage:
        response = self._get("/works", query)
        raise_for_transport_error(response, provider=self.name, context=context)
        body = response.json_body or {}
        results = body.get("results", []) if isinstance(body, dict) else []
        meta = body.get("meta", {}) if isinstance(body, dict) else {}
        total = meta.get("count")
        items = [_work_to_edge(r) for r in results]
        has_more = isinstance(total, int) and (offset + len(items)) < total
        return CitationsPage(items=items, provider=self.name, offset=offset, limit=limit, total=total, has_more=has_more)

    def get_citations(
        self, identifier: str, *, limit: int = 100, offset: int = 0,
        work_type: str | None = None, sort: str | None = None,
    ) -> CitationsPage:
        """``identifier`` may be a DOI or an OpenAlex work id (short
        ``W123`` or the full ``https://openalex.org/W123`` form).

        Lane SI item A2: ``work_type`` narrows the citing works to one OpenAlex
        type (``filter=cites:<W>,type:<work_type>``) and ``sort`` is passed
        through as ``sort=<sort>`` (e.g. ``cited_by_count:desc``). With both
        left at ``None`` the request URL is byte-identical to the one built
        before these keywords existed.

        TRIALERROR-DEV-NOTE (scope limitation, disclosed): OpenAlex paginates
        listing endpoints via ``page``/``per-page``, not a raw byte
        offset; this maps ``offset`` to a page number assuming ``offset``
        is page-aligned (``offset == (page - 1) * limit``), which holds
        for straightforward sequential paging (page 1, then page 2 at
        ``offset=limit``, ...) but not for an arbitrary offset a caller
        might otherwise expect an offset-based API to support."""
        limit = max(1, min(limit, 100))
        page = (offset // limit) + 1
        openalex_id = self._resolve_work_id(identifier, purpose="citations")
        work_filter = f"cites:{openalex_id}"
        if work_type is not None:
            work_filter += f",type:{work_type}"
        query = {"filter": work_filter, "per-page": str(limit), "page": str(page)}
        if sort is not None:
            query["sort"] = sort
        return self._list_edges(query, context=f"get_citations({identifier!r})", offset=offset, limit=limit)

    def get_references(self, identifier: str, *, limit: int = 20) -> CitationsPage:
        """Lane SI item A2: the works ``identifier`` cites, most-cited first
        (``filter=cited_by:<W>&sort=cited_by_count:desc``). Resolves a DOI to a
        work id exactly as :meth:`get_citations` does. One page only."""
        limit = max(1, min(limit, 100))
        openalex_id = self._resolve_work_id(identifier, purpose="references")
        query = {"filter": f"cited_by:{openalex_id}", "per-page": str(limit), "sort": "cited_by_count:desc"}
        return self._list_edges(query, context=f"get_references({identifier!r})", offset=0, limit=limit)

    def get_author_works(
        self, author_id: str, *, since_year: int | None = None, limit: int = 10,
    ) -> list[WorkRecord]:
        """Lane SI item A2: one OpenAlex author's works, most-cited first,
        optionally only those published from ``since_year`` on
        (``filter=author.id:<A>[,from_publication_date:<since_year>-01-01]``).
        ``author_id`` is an OpenAlex author id (``A123`` or its URL form)."""
        limit = max(1, min(limit, 100))
        short_id = _short_openalex_id(author_id) or author_id
        author_filter = f"author.id:{short_id}"
        if since_year is not None:
            author_filter += f",from_publication_date:{int(since_year)}-01-01"
        response = self._get(
            "/works", {"filter": author_filter, "sort": "cited_by_count:desc", "per-page": str(limit)},
        )
        raise_for_transport_error(response, provider=self.name, context=f"get_author_works({author_id!r})")
        body = response.json_body or {}
        results = body.get("results", []) if isinstance(body, dict) else []
        return [_work_to_record(r) for r in results[:limit]]
