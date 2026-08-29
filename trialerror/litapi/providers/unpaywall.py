"""Unpaywall ``Provider`` client (v3-acquisition build, C-0064 flags F1/F2
RESOLVED -- ``docs/EXTERNAL_API_FACTS.md`` quick-confirms table). Unpaywall
is deliberately the NARROWEST of the four providers this package ships: it
is not a general metadata source (no title/keyword search, no
citation-graph) -- it does exactly one thing, given a DOI: report whether a
LEGAL open-access copy exists and, if so, where (``best_oa_location``).
That narrowness is exactly why the acquisition-seam module
(:mod:`trialerror.ingest.acquire`) treats Unpaywall (alongside arXiv's own PDF
link) as the ONLY two sources trusted to resolve a download url -- see
that module's docstring for the full legality-fence rationale.

Auth model: no API key at all -- every call requires an ``email=`` query
parameter (identification only, per the mining table: "not gated auth").
This module reuses :class:`~trialerror.litapi.config.ProviderApiConfig`'s
``mailto`` field for that email (the SAME field OpenAlex's own
now-discontinued polite-pool used) rather than adding a parallel ``email``
field to the shared config dataclass -- the two concepts ("identify
yourself via an email string in every request") are semantically
identical, just against two different providers' own field-naming choice.
A program with no ``[litapi.unpaywall].mailto`` configured gets a hard,
loud :class:`~trialerror.litapi.errors.ProviderConfigError` on every call
(never a silently-omitted param, never a placeholder email sent on the
program's behalf) -- see :meth:`UnpaywallProvider._require_email`.

TRIALERROR-DEV-NOTE (search/get_citations, deliberate scope limits): Unpaywall
genuinely has neither a search endpoint nor a citation-graph endpoint --
both raise :class:`~trialerror.litapi.errors.ProviderUnsupportedOperationError`
immediately, matching :mod:`trialerror.litapi.providers.arxiv`'s equivalent
``get_citations`` refusal.

TRIALERROR-DEV-NOTE (response shape, not independently re-verified this
session): field names below (``is_oa``, ``oa_status``,
``best_oa_location``/``oa_locations`` with their own ``url``/
``url_for_pdf``/``host_type``/``license`` sub-fields, ``z_authors``,
``journal_name``) are Unpaywall's own long-documented, stable public
response schema (unchanged for years per common convention) -- this
module's mapping was not cross-checked against a fresh live response this
session (``docs/EXTERNAL_API_FACTS.md``'s own quick-confirms pass covered
auth/rate-limit facts, not the response body schema). The live-smoke test
(``tests/test_litapi_live_smoke.py``, ``TRIALERROR_LITAPI_LIVE_TESTS=1``) is
the place a real mismatch would surface.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderConfigError, ProviderNotFoundError, ProviderTransportError, ProviderUnsupportedOperationError
from trialerror.litapi.models import CitationsPage, WorkRecord, arxiv_to_doi, normalize_arxiv_id, normalize_doi
from trialerror.litapi.providers.base import RateLimiter, get_with_retry, raise_for_transport_error
from trialerror.litapi.transport import ProviderTransport

__all__ = ["UnpaywallProvider"]


def _authors(data: dict) -> list[str]:
    out: list[str] = []
    for a in data.get("z_authors") or []:
        name = " ".join(part for part in (a.get("given"), a.get("family")) if part)
        if name:
            out.append(name)
    return out


def _record_from_response(data: dict) -> WorkRecord:
    best = data.get("best_oa_location") or {}
    oa_pdf_url = best.get("url_for_pdf") or best.get("url")
    return WorkRecord(
        title=data.get("title"),
        doi=normalize_doi(data.get("doi")),
        arxiv_id=None,  # Unpaywall's response carries no arXiv-id field
        authors=_authors(data),
        year=data.get("year"),
        venue=data.get("journal_name"),
        abstract=None,  # Unpaywall does not serve abstracts
        citation_count=None,  # Unpaywall does not serve citation counts
        oa_pdf_url=oa_pdf_url,
        url=data.get("doi_url") or oa_pdf_url,
        external_ids={},
        other={
            "is_oa": data.get("is_oa"),
            "oa_status": data.get("oa_status"),
            "best_oa_location": best,
            "oa_locations_count": len(data.get("oa_locations") or []),
        },
    )


class UnpaywallProvider:
    name = "unpaywall"

    def __init__(self, transport: ProviderTransport, config: ProviderApiConfig, *, program_root=None):
        # program_root accepted (unused) for constructor-shape parity with
        # every other provider (see trialerror.litapi.providers.arxiv's own
        # identical note) -- Unpaywall has no API-key file to resolve.
        self.transport = transport
        self.config = config
        self._rate_limiter = RateLimiter(config.min_interval_s)

    def _require_email(self) -> str:
        if not self.config.mailto:
            raise ProviderConfigError(
                "Unpaywall requires an email identifier on every call ([litapi.unpaywall].mailto in "
                "trialerror.toml) -- refusing rather than sending a request with none (see this module's "
                "docstring; trialerror.litapi.checks.check_litapi_providers_ready surfaces this as "
                "'needs-email' before any call is attempted)"
            )
        return self.config.mailto

    def _fetch(self, doi: str) -> dict | None:
        email = self._require_email()
        url = f"{self.config.base_url}/{quote(doi, safe='')}?{urlencode({'email': email})}"
        response = get_with_retry(
            self.transport,
            url,
            provider=self.name,
            headers={},
            timeout_s=self.config.timeout_s,
            rate_limiter=self._rate_limiter,
            retry_attempts=self.config.retry_attempts,
            retry_on_status=self.config.retry_on_status,
        )
        if response.status_code == 404:
            return None
        raise_for_transport_error(response, provider=self.name, context=f"get_by_doi({doi!r})")
        if not isinstance(response.json_body, dict):
            raise ProviderTransportError(
                f"Unpaywall get_by_doi({doi!r}): non-JSON-object response body",
                provider=self.name, status_code=response.status_code,
            )
        return response.json_body

    # -- Provider interface --------------------------------------------------

    def get_by_doi(self, doi: str) -> WorkRecord | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        data = self._fetch(normalized)
        if data is None:
            raise ProviderNotFoundError(f"Unpaywall: no record for DOI {doi!r}", provider=self.name)
        return _record_from_response(data)

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None:
        """arXiv preprints ARE frequently DataCite/Crossref-DOI-registered
        under arXiv's own self-assigned ``10.48550/arXiv.<id>`` form (see
        ``trialerror.litapi.models.arxiv_to_doi``) -- this delegates to
        :meth:`get_by_doi` via that synthesized DOI, the same pattern
        :meth:`trialerror.litapi.providers.openalex.OpenAlexProvider.get_by_arxiv`
        already uses. A genuine miss (the synthesized DOI isn't in
        Unpaywall's index) surfaces as a normal not-found, not a crash."""
        normalized = normalize_arxiv_id(arxiv_id)
        synthesized_doi = arxiv_to_doi(normalized)
        if not synthesized_doi:
            return None
        try:
            record = self.get_by_doi(synthesized_doi)
        except ProviderNotFoundError:
            raise ProviderNotFoundError(
                f"Unpaywall: no record for arXiv id {arxiv_id!r} (tried via synthesized DOI {synthesized_doi!r})",
                provider=self.name,
            ) from None
        if record is not None:
            record.arxiv_id = normalized
        return record

    def search(self, query: str, *, limit: int = 10) -> list[WorkRecord]:
        raise ProviderUnsupportedOperationError(
            "Unpaywall has no search endpoint (DOI-lookup + OA-location resolution only)", provider=self.name
        )

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage:
        raise ProviderUnsupportedOperationError(
            f"Unpaywall has no citation-graph endpoint (OA-location data only) -- identifier={identifier!r}",
            provider=self.name,
        )
