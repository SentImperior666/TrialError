"""``LitApiClient`` -- the top-level orchestration a caller (the ``trialerror
lit`` CLI group, and later M7/M9) actually uses. Design brief: "given a
DOI/arxiv-id/title, fetch normalized metadata + citations from MULTIPLE
providers with reconciliation, so a single API's fragility or rate-limit
never blocks a lookup."

Every provider is queried independently and failures are tolerated per
provider (a ``ProviderNotFoundError`` or ``ProviderTransportError`` from
one provider never aborts the whole lookup -- it's recorded in
``providers_failed`` and the other provider(s) still get a chance). Only
when EVERY configured provider comes up empty does a lookup raise
:class:`~trialerror.litapi.errors.AllProvidersFailedError`.

``DEFAULT_CLIENTS``/``ALL_CLIENTS`` (paper-qa's own naming, mining report:
"``DEFAULT_CLIENTS = (CrossrefProvider, SemanticScholarProvider,
JournalQualityPostProcessor)``; ``ALL_CLIENTS`` adds OpenAlex, Unpaywall,
retraction checking"): the v1-preview build shipped exactly two providers,
with ``ALL_CLIENTS is DEFAULT_CLIENTS`` at the time -- the v3-acquisition
build (C-0064 flags F1/F2 RESOLVED) exercises the documented third-provider
seam (``trialerror.litapi.providers``'s own module docstring) twice, adding
:class:`~trialerror.litapi.providers.arxiv.ArxivProvider` and
:class:`~trialerror.litapi.providers.unpaywall.UnpaywallProvider` to
``ALL_CLIENTS`` while leaving ``DEFAULT_CLIENTS`` (and every existing
caller that builds against it -- ``trialerror/cli/lit.py``'s ``lookup``/
``citations``/``search`` commands, unchanged this build) exactly as
before. ``trialerror.ingest.acquire`` (the new acquisition-seam module this
same build adds) is ``ALL_CLIENTS``'s first real consumer: it needs the
FULL reconciliation set for metadata (more provider coverage is strictly
better there) plus dedicated, individual access to the arxiv/unpaywall
providers specifically for OA-pdf resolution (see that module's own
docstring for why OpenAlex's/S2's own ``oa_pdf_url`` field is deliberately
never trusted for a download).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence, Type

from trialerror.litapi import reconcile
from trialerror.litapi.config import LitApiConfig
from trialerror.litapi.errors import (
    AllProvidersFailedError,
    LitApiError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from trialerror.litapi.models import CitationsPage, WorkRecord, looks_like_identifier, normalize_title
from trialerror.litapi.providers.arxiv import ArxivProvider
from trialerror.litapi.providers.base import Provider
from trialerror.litapi.providers.openalex import OpenAlexProvider
from trialerror.litapi.providers.semanticscholar import SemanticScholarProvider
from trialerror.litapi.providers.unpaywall import UnpaywallProvider
from trialerror.litapi.transport import ProviderTransport, UrllibTransport

__all__ = [
    "DEFAULT_CLIENTS",
    "ALL_CLIENTS",
    "LookupResult",
    "SearchResult",
    "LitApiClient",
    "build_default_providers",
]

DEFAULT_CLIENTS: tuple[Type[Provider], ...] = (OpenAlexProvider, SemanticScholarProvider)
#: v3-acquisition build: the full reconciliation set -- the original two
#: plus arXiv (keyless, ToU-paced) and Unpaywall (email-identified,
#: OA-location-only). See this module's own docstring for why
#: ``trialerror.ingest.acquire`` builds against THIS tuple, not
#: ``DEFAULT_CLIENTS``.
ALL_CLIENTS: tuple[Type[Provider], ...] = (OpenAlexProvider, SemanticScholarProvider, ArxivProvider, UnpaywallProvider)


@dataclass
class LookupResult:
    """The outcome of one ``lookup_doi``/``lookup_arxiv`` call: the
    reconciled record plus full provenance -- which providers actually
    contributed, and which failed and why (design brief: "provenance =
    which providers contributed")."""

    record: WorkRecord
    providers_succeeded: list[str] = field(default_factory=list)
    providers_failed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "providers_succeeded": list(self.providers_succeeded),
            "providers_failed": list(self.providers_failed),
        }


@dataclass
class SearchResult:
    records: list[WorkRecord]
    providers_succeeded: list[str] = field(default_factory=list)
    providers_failed: list[dict[str, Any]] = field(default_factory=list)
    #: Lane FB-1 item F3: what each provider's search actually matched on
    #: (``{provider: scope}``), reported beside ``providers_succeeded``
    #: because the two belong together -- "openalex returned 0" means
    #: nothing until you know openalex was asked a title-only question. No
    #: query is rewritten anywhere on the strength of this; it is the scope
    #: being STATED, not normalised away.
    provider_query_scope: dict[str, str] = field(default_factory=dict)
    #: Lane FB-1 item F10b: every normalised-fallback retry that fired, as
    #: ``{provider, status_code, first_error, retried_query, outcome}``. A
    #: retry is a different question asked of the same API, so it is recorded
    #: rather than hidden -- a caller comparing two runs' hit counts needs to
    #: know that one of them asked twice.
    provider_retries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "providers_succeeded": list(self.providers_succeeded),
            "providers_failed": list(self.providers_failed),
            "provider_query_scope": dict(self.provider_query_scope),
            "provider_retries": list(self.provider_retries),
        }


def _failure_entry(provider_name: str, exc: LitApiError) -> dict[str, Any]:
    """One ``providers_failed``/``metadata_failures`` row. A genuine
    transport-level unreachability (litapi-arxiv-https build) --
    :func:`~trialerror.litapi.providers.base.get_with_retry` wraps a raw
    ``URLError``/socket timeout/``ConnectionError`` into
    :class:`~trialerror.litapi.errors.ProviderTransportError` with
    ``host``/``scheme`` set (see that class's own docstring for how this
    is distinguished from the pre-existing "bad HTTP status" use of the
    same class) -- is enriched with a ``code``/``host``/``scheme`` a
    caller (``trialerror/cli/lit.py``) can key off to report the more
    actionable ``transport_unreachable`` envelope code instead of the
    generic exception-class-name one, without having to re-parse the
    ``error`` message string. Every other failure (bad HTTP status,
    malformed body) keeps the original, narrower ``{provider, error}``
    shape unchanged."""
    entry: dict[str, Any] = {"provider": provider_name, "error": str(exc)}
    if isinstance(exc, ProviderTransportError) and exc.host is not None:
        entry["code"] = "transport_unreachable"
        entry["host"] = exc.host
        entry["scheme"] = exc.scheme
    return entry


def _normalized_retry_query(query: str, exc: LitApiError) -> str | None:
    """The query to retry ``exc``'s provider with, or ``None`` for "do not
    retry". See :meth:`LitApiClient.search` for why each condition is here.

    ``status_code`` is the discriminator the transport already records: it is
    set for "the provider answered with a bad status" and left ``None`` for
    "no HTTP response was ever received" (see
    :class:`~trialerror.litapi.errors.ProviderTransportError`), which is the
    exact line between "maybe the query offended it" and "nothing was
    reached"."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or not 400 <= status <= 599:
        return None
    if looks_like_identifier(query):
        return None
    normalized = normalize_title(query)
    if not normalized or normalized == query:
        return None
    return normalized


def build_default_providers(
    config: LitApiConfig,
    *,
    transport: ProviderTransport | None = None,
    provider_classes: Sequence[Type[Provider]] = DEFAULT_CLIENTS,
    program_root=None,
) -> list[Provider]:
    """Construct the default provider set against a REAL transport
    (:class:`~trialerror.litapi.transport.UrllibTransport` unless one is
    given). Production/CLI entry point; tests build providers directly
    with a :class:`~trialerror.litapi.transport.FakeTransport` instead and
    never call this function."""
    real_transport = transport if transport is not None else UrllibTransport()
    providers: list[Provider] = []
    for cls in provider_classes:
        provider_cfg = config.provider(cls.name) if hasattr(cls, "name") else None
        if provider_cfg is None:
            continue
        providers.append(cls(real_transport, provider_cfg, program_root=program_root))
    return providers


class LitApiClient:
    def __init__(self, providers: Sequence[Provider]):
        if not providers:
            raise ValueError("LitApiClient requires at least one provider")
        self.providers = list(providers)

    # -- single-record lookups -----------------------------------------------

    def lookup_doi(self, doi: str) -> LookupResult:
        return self._lookup(lambda p: p.get_by_doi(doi), description=f"doi={doi!r}")

    def lookup_arxiv(self, arxiv_id: str) -> LookupResult:
        return self._lookup(lambda p: p.get_by_arxiv(arxiv_id), description=f"arxiv_id={arxiv_id!r}")

    def _lookup(self, call, *, description: str) -> LookupResult:
        records: list[WorkRecord] = []
        failures: list[dict[str, Any]] = []
        for provider in self.providers:
            try:
                record = call(provider)
            except ProviderNotFoundError:
                continue
            except LitApiError as exc:
                failures.append(_failure_entry(provider.name, exc))
                continue
            if record is not None:
                record.providers = [provider.name]
                records.append(record)
        if not records:
            raise AllProvidersFailedError(
                f"no provider returned a record for {description}", details={"failures": failures}
            )
        merged = reconcile.merge_one(records)
        return LookupResult(
            record=merged,
            providers_succeeded=[r.providers[0] for r in records],
            providers_failed=failures,
        )

    # -- search ----------------------------------------------------------------

    def search(self, query: str, *, limit: int = 10) -> SearchResult:
        """Search every provider with the caller's query VERBATIM, and retry a
        provider at most once with a normalised query when that provider
        answered with an HTTP error status (lane FB-1 item F10b).

        The raw query is always the first attempt -- punctuation, case and
        diacritics are signal to a search API, and rewriting up front would
        throw away recall nobody asked to lose. But a title pasted out of a
        PDF (smart quotes, a soft hyphen, a trailing "  (preprint)") is a
        query some providers answer with a 400 rather than an empty result,
        and a lookup that dies on a punctuation mark is indistinguishable to
        the caller from a paper that is not indexed.

        Four bounds, each of them the point:

        - ONLY on that provider's own 4xx/5xx. A provider that answered
          successfully is never asked twice: its empty result is an answer,
          and a second query would be inventing recall the provider did not
          report. A transport-level failure (no HTTP response at all, so no
          status code) is not retried either -- normalization cannot reach
          an unreachable host.
        - ONCE, and only when the normalised form actually DIFFERS from what
          was already sent.
        - NEVER for a DOI or an arXiv id (:func:`looks_like_identifier`):
          title normalization collapses every non-alphanumeric run, which is
          exactly the part of an identifier that identifies it.
        - RECORDED in ``provider_retries``, always -- including when the
          retry fails too.
        """
        all_records: list[WorkRecord] = []
        succeeded: list[str] = []
        failures: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        for provider in self.providers:
            try:
                records = provider.search(query, limit=limit)
            except LitApiError as exc:
                retry_query = _normalized_retry_query(query, exc)
                if retry_query is None:
                    failures.append(_failure_entry(provider.name, exc))
                    continue
                entry = {
                    "provider": provider.name,
                    "status_code": getattr(exc, "status_code", None),
                    "first_error": str(exc),
                    "retried_query": retry_query,
                }
                try:
                    records = provider.search(retry_query, limit=limit)
                except LitApiError as retry_exc:
                    entry["outcome"] = "failed"
                    retries.append(entry)
                    failures.append(_failure_entry(provider.name, retry_exc))
                    continue
                entry["outcome"] = "succeeded"
                retries.append(entry)
            succeeded.append(provider.name)
            for r in records:
                r.providers = [provider.name]
            all_records.extend(records)
        if not all_records and not succeeded:
            raise AllProvidersFailedError(
                f"no provider could search for query={query!r}", details={"failures": failures}
            )
        merged = reconcile.reconcile_many(all_records)
        return SearchResult(
            records=merged[:limit],
            providers_succeeded=succeeded,
            providers_failed=failures,
            provider_query_scope={
                p.name: getattr(p, "search_scope", "unspecified") for p in self.providers
            },
            provider_retries=retries,
        )

    # -- citations --------------------------------------------------------------

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage:
        """Tries providers IN ORDER, returning the first success (design
        brief: "so a single API's fragility or rate-limit never blocks a
        lookup"). Citations pages are NOT reconciled/merged across
        providers in this v1-preview build -- unlike a single-paper
        lookup, two providers' citation listings are two different sets
        of citing papers with only partial overlap, and de-duplicating a
        PAGE of results across providers (rather than a single record)
        needs pagination-aware reconciliation this bounded scope does not
        attempt. The returned page's own ``provider`` field names which
        one actually served it."""
        failures: list[dict[str, Any]] = []
        for provider in self.providers:
            try:
                return provider.get_citations(identifier, limit=limit, offset=offset)
            except ProviderNotFoundError as exc:
                failures.append(_failure_entry(provider.name, exc))
                continue
            except LitApiError as exc:
                failures.append(_failure_entry(provider.name, exc))
                continue
        raise AllProvidersFailedError(
            f"no provider could fetch citations for identifier={identifier!r}", details={"failures": failures}
        )
