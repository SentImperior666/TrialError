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
from trialerror.litapi.errors import AllProvidersFailedError, LitApiError, ProviderNotFoundError
from trialerror.litapi.models import CitationsPage, WorkRecord
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "providers_succeeded": list(self.providers_succeeded),
            "providers_failed": list(self.providers_failed),
        }


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
                failures.append({"provider": provider.name, "error": str(exc)})
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
        all_records: list[WorkRecord] = []
        succeeded: list[str] = []
        failures: list[dict[str, Any]] = []
        for provider in self.providers:
            try:
                records = provider.search(query, limit=limit)
            except LitApiError as exc:
                failures.append({"provider": provider.name, "error": str(exc)})
                continue
            succeeded.append(provider.name)
            for r in records:
                r.providers = [provider.name]
            all_records.extend(records)
        if not all_records and not succeeded:
            raise AllProvidersFailedError(
                f"no provider could search for query={query!r}", details={"failures": failures}
            )
        merged = reconcile.reconcile_many(all_records)
        return SearchResult(records=merged[:limit], providers_succeeded=succeeded, providers_failed=failures)

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
                failures.append({"provider": provider.name, "error": str(exc)})
                continue
            except LitApiError as exc:
                failures.append({"provider": provider.name, "error": str(exc)})
                continue
        raise AllProvidersFailedError(
            f"no provider could fetch citations for identifier={identifier!r}", details={"failures": failures}
        )
