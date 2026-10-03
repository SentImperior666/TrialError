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

import inspect
from dataclasses import dataclass, field
from typing import Any, Sequence, Type

from trialerror.litapi import reconcile
from trialerror.litapi.config import LitApiConfig
from trialerror.litapi.errors import (
    AllProvidersFailedError,
    LitApiError,
    ProviderConfigError,
    ProviderNotFoundError,
    ProviderTransportError,
    ProviderUnsupportedOperationError,
    UnknownProviderError,
)
from trialerror.litapi.models import CitationsPage, WorkRecord, looks_like_identifier, normalize_title, provider_extra
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
    "AuthorWorks",
    "AuthorWorksResult",
    "LitApiClient",
    "build_default_providers",
    "PROVIDER_OUTCOMES",
    "outcome_word",
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
    #: Lane FB-acq item 2: one entry for EVERY provider asked, whatever it
    #: answered -- ``{provider: {outcome, status_code, retry_after_s, attempts,
    #: waited_s, request_sent, keyed, error}}``. ``providers_failed`` cannot
    #: carry this: a provider that answered "not found" is dropped from it by
    #: design (a redundant lookup's other provider may still have the record),
    #: so "one provider rate-limited, the other has no such record" and "both
    #: broken" were indistinguishable, and a 429 lived only inside a message
    #: string. Neither ``providers_succeeded`` nor ``providers_failed`` changes
    #: meaning.
    provider_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "providers_succeeded": list(self.providers_succeeded),
            "providers_failed": list(self.providers_failed),
            "provider_outcomes": {k: dict(v) for k, v in self.provider_outcomes.items()},
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
    #: Lane FB-acq item 2 -- see :attr:`LookupResult.provider_outcomes`.
    provider_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``search(per_provider=True)`` only, ``None`` otherwise: for each record
    #: (same order as ``records``), the 1-based rank it held in each provider's
    #: own list, ``{provider: rank}``.
    provider_ranks: list[dict[str, int]] | None = None
    #: ``search(per_provider=True)`` only, ``None`` otherwise: how the records
    #: were chosen -- ``{"mode", "limit", "order", "kept"}``, ``kept`` being, for
    #: EVERY provider searched, how many distinct kept records it returned (``0``
    #: when it failed or returned nothing; a record two providers returned counts
    #: for each; a provider's repeated identity counts once).
    selection: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        records = [r.to_dict() for r in self.records]
        if self.provider_ranks is not None:
            for record, ranks in zip(records, self.provider_ranks):
                record["provider_ranks"] = dict(ranks)
        out: dict[str, Any] = {
            "records": records,
            "providers_succeeded": list(self.providers_succeeded),
            "providers_failed": list(self.providers_failed),
            "provider_query_scope": dict(self.provider_query_scope),
            "provider_retries": list(self.provider_retries),
            "provider_outcomes": {k: dict(v) for k, v in self.provider_outcomes.items()},
        }
        if self.selection is not None:
            out["selection"] = dict(self.selection)
        return out


@dataclass
class AuthorWorks:
    """Lane SI item A2: one author position's works, as served by the first
    provider that answered for that position. ``provider``/``author_id`` are
    ``None`` when no provider served it -- then ``provider_outcomes`` says why
    (a ``rate_limited`` there is "could not ask", never "no works"), and
    ``providers_without_id`` names the providers that were not asked because
    the record carries no author id of theirs at this position."""

    position: int
    provider: str | None = None
    author_id: str | None = None
    records: list[WorkRecord] = field(default_factory=list)
    provider_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    providers_without_id: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "provider": self.provider,
            "author_id": self.author_id,
            "records": [r.to_dict() for r in self.records],
            "provider_outcomes": {k: dict(v) for k, v in self.provider_outcomes.items()},
            "providers_without_id": list(self.providers_without_id),
        }


@dataclass
class AuthorWorksResult:
    """Lane SI item A2: :meth:`LitApiClient.get_author_works`' answer -- one
    :class:`AuthorWorks` per author position asked, plus ``provider_outcomes``
    keyed by provider: a LIST of that provider's outcome dicts, one per position
    it was asked about, each carrying its ``position`` (a provider is asked
    once per author, so one dict per provider could not hold them)."""

    authors: list[AuthorWorks] = field(default_factory=list)
    provider_outcomes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "authors": [a.to_dict() for a in self.authors],
            "provider_outcomes": {k: [dict(o) for o in v] for k, v in self.provider_outcomes.items()},
        }


def _accepts_keywords(method, names: Sequence[str]) -> bool:
    """Does ``method`` accept every keyword in ``names``? A provider written
    before a keyword existed would otherwise fail with a ``TypeError`` -- the
    client reads that as "this provider does not support it" instead."""
    if not names:
        return True
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


def _unsupported(provider: Provider, what: str) -> ProviderUnsupportedOperationError:
    return ProviderUnsupportedOperationError(f"{provider.name}: {what} not supported by this provider", provider=provider.name)


#: The request stats of a call the CLIENT refused before reaching the provider
#: (the same "nothing went out" shape ``get_with_retry`` starts from), so the
#: outcome never repeats the provider's previous request's numbers.
_NOTHING_SENT: dict[str, Any] = {
    "attempts": 0, "waited_s": 0.0, "last_status": None, "retry_after_s": None, "request_sent": False,
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
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        # Lane FB-acq item 2: the status lived only inside the message string,
        # so a caller wanting to branch on 429-vs-5xx had to parse English.
        entry["status_code"] = status_code
    if isinstance(exc, ProviderTransportError) and exc.host is not None:
        entry["code"] = "transport_unreachable"
        entry["host"] = exc.host
        entry["scheme"] = exc.scheme
    elif status_code == 429:
        entry["code"] = "rate_limited"
        entry["retry_after_s"] = getattr(exc, "retry_after_s", None)
    return entry


#: What ``_provider_outcome`` can say about one provider's answer.
PROVIDER_OUTCOMES: tuple[str, ...] = (
    "record", "not_found", "rate_limited", "http_error", "transport_unreachable",
    "not_configured", "unsupported", "error",
)


def outcome_word(exc: LitApiError | None, record_found: bool) -> str:
    """The one :data:`PROVIDER_OUTCOMES` word for one provider's answer --
    the classification :func:`_provider_outcome` reports, on its own, for a
    caller that records the word somewhere else (lane SI part B's evidence
    cache keeps it per cached answer). ``None`` with nothing found reads as
    ``not_found``."""
    if exc is None:
        return "record" if record_found else "not_found"
    if isinstance(exc, ProviderNotFoundError):
        return "not_found"
    if isinstance(exc, ProviderConfigError):
        return "not_configured"
    if isinstance(exc, ProviderUnsupportedOperationError):
        return "unsupported"
    if isinstance(exc, ProviderTransportError):
        if exc.host is not None:
            return "transport_unreachable"
        if exc.status_code == 429:
            return "rate_limited"
        return "http_error"
    return "error"


def _provider_outcome(
    provider: Provider, exc: LitApiError | None, record_found: bool, *, stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One provider's answer, in one word plus the numbers behind it.

    The word comes from the same table
    :func:`trialerror.ingest.acquire._classify_oa_error` uses -- one vocabulary
    for "what did this provider do", whether it was asked for metadata or for
    an open-access location. ``None`` returned with no exception reads as
    ``not_found``: a provider that answers nothing has no record.

    ``keyed`` says whether the call actually went out with an API key, which
    nothing in a result said before: a keyless call to a provider that now
    requires a key looks exactly like a provider outage from the outside.
    """
    if stats is None:
        stats = getattr(provider, "last_request_stats", None) or {}
    outcome = outcome_word(exc, record_found)
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = stats.get("last_status")
    retry_after_s = getattr(exc, "retry_after_s", None)
    if retry_after_s is None:
        retry_after_s = stats.get("retry_after_s")
    return {
        "outcome": outcome,
        "status_code": status_code,
        "retry_after_s": retry_after_s,
        "attempts": stats.get("attempts"),
        "waited_s": stats.get("waited_s"),
        "request_sent": stats.get("request_sent"),
        "keyed": bool(getattr(provider, "_api_key", None)),
        "error": str(exc) if exc is not None else None,
    }


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
    pacing_dir=None,
) -> list[Provider]:
    """Construct the default provider set against a REAL transport
    (:class:`~trialerror.litapi.transport.UrllibTransport` unless one is
    given). Production/CLI entry point; tests build providers directly
    with a :class:`~trialerror.litapi.transport.FakeTransport` instead and
    never call this function.

    ``pacing_dir`` (lane FB-acq item 2) is forwarded to every provider, which
    gives its rate limiter a per-provider stamp file there and so paces across
    INVOCATIONS, not only within one process. ``None`` -- the default -- keeps
    today's in-memory-only behaviour and writes nothing."""
    real_transport = transport if transport is not None else UrllibTransport()
    providers: list[Provider] = []
    for cls in provider_classes:
        provider_cfg = config.provider(cls.name) if hasattr(cls, "name") else None
        if provider_cfg is None:
            continue
        providers.append(cls(real_transport, provider_cfg, program_root=program_root, pacing_dir=pacing_dir))
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
        outcomes: dict[str, dict[str, Any]] = {}
        for provider in self.providers:
            record = None
            exc: LitApiError | None = None
            try:
                record = call(provider)
            except ProviderNotFoundError as not_found:
                exc = not_found
            except LitApiError as failure:
                exc = failure
                failures.append(_failure_entry(provider.name, failure))
            outcomes[provider.name] = _provider_outcome(provider, exc, record is not None)
            if exc is None and record is not None:
                record.providers = [provider.name]
                records.append(record)
        if not records:
            raise AllProvidersFailedError(
                f"no provider returned a record for {description}",
                details={"failures": failures, "provider_outcomes": outcomes},
            )
        merged = reconcile.merge_one(records)
        return LookupResult(
            record=merged,
            providers_succeeded=[r.providers[0] for r in records],
            providers_failed=failures,
            provider_outcomes=outcomes,
        )

    # -- search ----------------------------------------------------------------

    def search(
        self, query: str, *, limit: int = 10, per_provider: bool = False, providers: Sequence[str] | None = None,
    ) -> SearchResult:
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

        ``limit`` must be at least 1: a smaller one raises ``ValueError`` before
        any provider is called (it used to cut the merged list with a zero or
        negative slice and return an empty, apparently successful, answer).

        ``providers`` searches only the named providers (``None``: all of
        them, as before). A name this client does not have raises
        :class:`~trialerror.litapi.errors.UnknownProviderError` before any
        provider is called. Nothing else changes: each named provider is still
        asked once, with the same retry rule.

        ``per_provider`` changes what is KEPT, not what is asked. By default the
        merged list is cut to its first ``limit`` records, and because it is in
        provider order a first provider that returns ``limit`` records fills it
        alone. With ``per_provider=True`` each provider that succeeded keeps its
        own first ``limit`` records, in its own order, and the result may hold
        ``limit`` x (providers) records. The merge is the same: a record two
        providers both returned appears once, with both provider names. Order is
        provider order, then each provider's own rank -- the order the default
        already uses, so the first provider's block reads the same either way
        and only a later provider's own hits are added. ``provider_ranks`` gives,
        per record, the rank it held in each provider's list, and ``selection``
        says how the records were chosen.
        """
        if limit < 1:
            raise ValueError(f"search() needs limit >= 1, got {limit}")
        selected = self._select_providers(providers)
        all_records: list[WorkRecord] = []
        succeeded: list[str] = []
        failures: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        outcomes: dict[str, dict[str, Any]] = {}
        by_provider: list[tuple[str, list[WorkRecord]]] = []
        for provider in selected:
            try:
                records = provider.search(query, limit=limit)
            except LitApiError as exc:
                retry_query = _normalized_retry_query(query, exc)
                if retry_query is None:
                    failures.append(_failure_entry(provider.name, exc))
                    outcomes[provider.name] = _provider_outcome(provider, exc, False)
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
                    outcomes[provider.name] = _provider_outcome(provider, retry_exc, False)
                    continue
                entry["outcome"] = "succeeded"
                retries.append(entry)
            outcomes[provider.name] = _provider_outcome(provider, None, bool(records))
            succeeded.append(provider.name)
            for r in records:
                r.providers = [provider.name]
            all_records.extend(records)
            by_provider.append((provider.name, records[:limit]))
        if not all_records and not succeeded:
            raise AllProvidersFailedError(
                f"no provider could search for query={query!r}",
                details={"failures": failures, "provider_outcomes": outcomes},
            )
        provider_ranks: list[dict[str, int]] | None = None
        selection: dict[str, Any] | None = None
        if per_provider:
            ranked = reconcile.reconcile_ranked(by_provider)
            kept = [record for record, _ in ranked]
            provider_ranks = [ranks for _, ranks in ranked]
            selection = {
                "mode": "per_provider",
                "limit": limit,
                "order": "provider order, then each provider's own rank",
                # distinct kept records that provider returned (a merged record counts
                # once for each provider that returned it); 0 when it failed or was empty
                "kept": {p.name: sum(1 for ranks in provider_ranks if p.name in ranks) for p in selected},
            }
        else:
            kept = reconcile.reconcile_many(all_records)[:limit]
        return SearchResult(
            records=kept,
            providers_succeeded=succeeded,
            providers_failed=failures,
            provider_query_scope={
                p.name: getattr(p, "search_scope", "unspecified") for p in selected
            },
            provider_retries=retries,
            provider_outcomes=outcomes,
            provider_ranks=provider_ranks,
            selection=selection,
        )

    def _select_providers(self, names: Sequence[str] | None) -> list[Provider]:
        """The providers a search asks: all of them for ``None``, else the named
        ones, in this client's own provider order (the order they are merged
        in), each once however many times it is named."""
        if names is None:
            return list(self.providers)
        if isinstance(names, str):
            names = [names]
        valid = [p.name for p in self.providers]
        unknown = [n for n in dict.fromkeys(names) if n not in valid]
        if unknown or not names:
            raise UnknownProviderError(
                f"unknown provider(s) {unknown}: valid names are {', '.join(valid)}",
                details={"unknown": unknown, "valid": valid},
            )
        return [p for p in self.providers if p.name in names]

    # -- citations --------------------------------------------------------------

    def get_citations(
        self, identifier: str, *, limit: int = 100, offset: int = 0,
        work_type: str | None = None, sort: str | None = None,
    ) -> CitationsPage:
        """Tries providers IN ORDER, returning the first success (design
        brief: "so a single API's fragility or rate-limit never blocks a
        lookup"). Citations pages are NOT reconciled/merged across
        providers in this v1-preview build -- unlike a single-paper
        lookup, two providers' citation listings are two different sets
        of citing papers with only partial overlap, and de-duplicating a
        PAGE of results across providers (rather than a single record)
        needs pagination-aware reconciliation this bounded scope does not
        attempt. The returned page's own ``provider`` field names which
        one actually served it.

        Lane SI item A2: ``work_type``/``sort`` are passed only when given (so
        a default call reaches every provider exactly as before); a provider
        that cannot honour one is recorded ``unsupported`` and the next is
        asked. The served page carries ``provider_outcomes`` for every
        provider asked, the serving one included."""
        extra = {k: v for k, v in (("work_type", work_type), ("sort", sort)) if v is not None}
        return self._first_page(
            "get_citations", identifier, description=f"citations for identifier={identifier!r}",
            kwargs={"limit": limit, "offset": offset, **extra}, required_keywords=tuple(extra),
        )

    def get_references(self, identifier: str, *, limit: int = 20) -> CitationsPage:
        """Lane SI item A2: the works ``identifier`` cites, first success across
        providers (same rules as :meth:`get_citations`). A provider without a
        ``get_references`` method is recorded ``unsupported``."""
        return self._first_page(
            "get_references", identifier, description=f"references for identifier={identifier!r}",
            kwargs={"limit": limit}, required_keywords=(),
        )

    def _first_page(
        self, method_name: str, identifier: str, *, description: str,
        kwargs: dict[str, Any], required_keywords: Sequence[str],
    ) -> CitationsPage:
        failures: list[dict[str, Any]] = []
        outcomes: dict[str, dict[str, Any]] = {}
        for provider in self.providers:
            method = getattr(provider, method_name, None)
            refused: LitApiError | None = None
            if method is None:
                refused = _unsupported(provider, method_name)
            elif not _accepts_keywords(method, required_keywords):
                refused = _unsupported(provider, f"{method_name}({', '.join(required_keywords)})")
            if refused is not None:
                failures.append(_failure_entry(provider.name, refused))
                outcomes[provider.name] = _provider_outcome(provider, refused, False, stats=_NOTHING_SENT)
                continue
            try:
                page = method(identifier, **kwargs)
            except LitApiError as exc:
                failures.append(_failure_entry(provider.name, exc))
                outcomes[provider.name] = _provider_outcome(provider, exc, False)
                continue
            outcomes[provider.name] = _provider_outcome(provider, None, True)
            if isinstance(page, CitationsPage):
                page.provider_outcomes = outcomes
            return page
        raise AllProvidersFailedError(
            f"no provider could fetch {description}",
            details={"failures": failures, "provider_outcomes": outcomes},
        )

    # -- author works -------------------------------------------------------------

    def get_author_works(
        self, record: WorkRecord, *, authors: int = 3, since_year: int | None = None, limit: int = 10,
    ) -> AuthorWorksResult:
        """Lane SI item A2: the works of ``record``'s first ``authors`` authors.

        An author id is provider-specific, so each provider is asked only with
        an id from its OWN ``author_ids`` (read through
        :func:`~trialerror.litapi.models.provider_extra`, whichever shape the
        record is in); a provider with no id at a position is not asked about
        it and is named in ``providers_without_id``. Per position, providers are
        tried in order and the first that answers without an error serves it
        (an empty list is an answer); a provider lacking ``get_author_works``
        is recorded ``unsupported``. Never raises for a provider failure: a
        position nobody could serve comes back with ``provider=None`` and the
        outcomes that say why."""
        positions = 0
        per_provider_ids: dict[str, list] = {}
        for provider in self.providers:
            ids = provider_extra(record, provider.name, "author_ids")
            ids = list(ids) if isinstance(ids, (list, tuple)) else []
            per_provider_ids[provider.name] = ids
            positions = max(positions, len(ids))
        positions = min(max(0, int(authors)), max(positions, len(record.authors)))

        result = AuthorWorksResult()
        for position in range(positions):
            entry = AuthorWorks(position=position)
            for provider in self.providers:
                ids = per_provider_ids[provider.name]
                author_id = ids[position] if position < len(ids) else None
                if not author_id:
                    entry.providers_without_id.append(provider.name)
                    continue
                method = getattr(provider, "get_author_works", None)
                exc: LitApiError | None = None
                works: list[WorkRecord] | None = None
                if method is None:
                    exc = _unsupported(provider, "get_author_works")
                    outcome = _provider_outcome(provider, exc, False, stats=_NOTHING_SENT)
                else:
                    try:
                        works = method(author_id, since_year=since_year, limit=limit)
                    except LitApiError as failure:
                        exc = failure
                    outcome = _provider_outcome(provider, exc, bool(works))
                entry.provider_outcomes[provider.name] = outcome
                result.provider_outcomes.setdefault(provider.name, []).append(
                    {"position": position, "author_id": author_id, **outcome}
                )
                if exc is None:
                    for w in works or []:
                        w.providers = [provider.name]
                    entry.provider = provider.name
                    entry.author_id = str(author_id)
                    entry.records = list(works or [])
                    break
            result.authors.append(entry)
        return result
