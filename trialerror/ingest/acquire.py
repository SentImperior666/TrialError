"""The acquisition -> ingest seam (``trialerror.litapi``'s own documented v1
wiring seam -- see ``trialerror/litapi/__init__.py``'s "M7 ingestion" note --
made real). v3-acquisition build, C-0064 flags F1/F2 RESOLVED
(``docs/EXTERNAL_API_FACTS.md``).

Given a DOI or arXiv id, :func:`acquire` does, in order:

1. **Resolve metadata** via ``trialerror.litapi``'s full reconciliation set
   (``trialerror.litapi.client.ALL_CLIENTS`` -- OpenAlex + Semantic Scholar +
   arXiv + Unpaywall) -- best-effort; a total metadata-lookup failure does
   NOT abort acquisition (the identifier itself is still enough to attempt
   OA resolution and, worst case, file a request-queue row).
2. **Resolve a LEGAL open-access PDF url** via a DEDICATED,
   narrower step that trusts exactly two sources: arXiv's own PDF link
   (arXiv IS the origin for its own preprints -- nothing to verify) and
   Unpaywall's ``best_oa_location`` (Unpaywall's entire business is
   verified-legal OA-location aggregation). This is deliberately NOT the
   same as reading ``WorkRecord.oa_pdf_url`` off the RECONCILED metadata
   record from step 1 -- that field could have been filled in by
   OpenAlex's ``open_access.oa_url`` or Semantic Scholar's
   ``openAccessPdf``, neither of which this build treats as an
   independently-verified-legal source (see each provider's own module
   docstring: both are documented AS metadata fields, neither provider's
   OWN documentation makes the "we verified this specific url is legally
   open" claim Unpaywall's product literally exists to make). No paywall
   circumvention is attempted anywhere in this module -- the C-0048/49
   licensing posture (project law: legitimate open sources only,
   otherwise the request queue) applies here exactly as it does to every
   other acquisition path in this harness.
3. **If found**: download it (real bytes, sniffed for the ``%PDF-`` magic
   header before being trusted -- a downloaded HTML paywall/error page is
   refused, not silently registered as a PDF source, per
   ``docs/mining/S2-scilit-2__paper-search-mcp.md``'s own "PDF
   content-sniffing on download" pattern), then
   ``trialerror.ingest.pipeline.register_source`` + ``.add_document`` (called
   as-is -- this module makes NO edits to ``pipeline.py`` itself) with
   full provenance (source url, license tier derived from the OA data,
   retrieval timestamp, sha256) so the normal ingest pipeline (normalize
   -> chunk -> embed -> index) takes over exactly as it would for any
   manually-added document.
4. **If a provider did not ANSWER** (a 429, a 5xx, a 406, an unreachable
   host, a malformed feed, a failed download): write nothing at all and
   return ``outcome="unresolved"`` with one :class:`OALeg` per leg saying
   what happened. F18: all of those used to become the same ``None`` as a
   genuine "no such record" and filed a ``wanted`` row whose notes read
   "no legal open-access location found" -- a provider failure recorded
   as a fact about the literature, in a row the request queue's own
   lifecycle could never correct.
5. **If NOT openly available anywhere**: ``register_source`` with
   ``request_state="wanted"`` instead -- the EXISTING request-queue
   lifecycle (``trialerror.ingest.requests``'s own ``source.request_state``
   state machine; there is no separate "request_item" table, the
   ``source`` row itself IS the queue entry) with every metadata field
   this build could resolve prefilled, for a human to fulfill later via
   the normal ``requests/REQUESTS.md`` flow. Never a paywall-circumvention
   attempt.

LIVE network calls only happen when this module's real defaults
(``UrllibTransport`` for metadata, :func:`fetch_bytes` for the PDF
download itself) are actually used -- every test in this package's suite
injects a :class:`~trialerror.litapi.transport.FakeTransport` AND a fake
``fetch_fn`` (the same "internal seam" pattern
``trialerror.litapi.providers.base.RateLimiter``'s ``_time_fn``/``_sleep_fn``
already uses), so the offline suite never touches a socket; the one
skip-gated exception (``TRIALERROR_LITAPI_LIVE_TESTS=1``) lives alongside
``trialerror.litapi``'s own live-smoke test, per that file's own docstring.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from xml.etree import ElementTree as ET

from trialerror.ingest import pipeline
from trialerror.ingest import requests as ingest_requests
from trialerror.litapi.client import ALL_CLIENTS, LitApiClient, build_default_providers
from trialerror.litapi.config import LitApiConfig
from trialerror.litapi.errors import (
    AllProvidersFailedError,
    LitApiError,
    ProviderConfigError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from trialerror.litapi.transport import ProviderTransport, UrllibTransport
from trialerror.stores.store import Store
from trialerror.util.timeutil import now

__all__ = [
    "OA_LEG_OUTCOMES",
    "OA_UNRESOLVED_OUTCOMES",
    "OALeg",
    "OAAttempt",
    "OAResolution",
    "AcquireResult",
    "fetch_bytes",
    "acquire",
]

#: paper-search-mcp's own defensive pattern (docs/mining/S2-scilit-2__paper-search-mcp.md
#: FEATURES WORTH STEALING #6): "checks content-type header AND %PDF magic
#: bytes AND .pdf extension before accepting a downloaded file as a real
#: PDF" -- this module checks the magic bytes (the one signal available
#: with zero extra transport plumbing; header/extension checks would need
#: a richer download return shape than `bytes` alone).
_PDF_MAGIC = b"%PDF-"

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: license strings (lowercased, substring match) Unpaywall's own
#: ``best_oa_location.license`` field uses that this module maps to the
#: source table's ``license_tier='open'`` value -- a real, unrestricted
#: open license, not merely "free to read via this one specific mirror".
_OPEN_LICENSE_MARKERS = ("cc0", "cc-by", "public-domain", "pd")


#: Every outcome one open-access resolution LEG can have. F18: before this
#: existed, a 429, a 5xx, a 406, an unreachable host and a genuine "no such
#: record" all collapsed into the same ``None``, and the row that got filed
#: said "no legal open-access location found" for all five. Only two of these
#: words are that sentence; the rest are provider failures.
OA_LEG_OUTCOMES = (
    "resolved",
    "not_found",
    "no_oa_location",
    "not_attempted",
    "not_configured",
    "rate_limited",
    "http_error",
    "transport_unreachable",
    "provider_error",
    "download_failed",
    "download_not_pdf",
)

#: The outcomes that mean "this leg did not answer the question". A leg with
#: one of these is why :attr:`OAAttempt.unresolved` refuses to file a row: an
#: unanswered question is not a "no", and the request queue's own lifecycle
#: (``trialerror.ingest.requests``: ``failed`` can only move to ``requested``)
#: has no way to turn a wrongly-filed row back into the ``wanted`` one a
#: later, conclusive run would want.
#:
#: ``download_not_pdf`` is deliberately NOT here: a body that downloaded fine
#: and is not a PDF IS an answer (there is no legal PDF at that url), and that
#: case keeps filing its row exactly as it did before.
OA_UNRESOLVED_OUTCOMES = frozenset(
    {"rate_limited", "http_error", "transport_unreachable", "provider_error", "download_failed"}
)

#: Rendered beside ``unpaywall=not_configured`` in a filed row's
#: ``rights_notes``: a leg that was never asked is not evidence of absence,
#: and the note has to say so where a human reading the queue will see it.
_NOT_CONFIGURED_GLOSS = " (open-access availability was not checked there)"


@dataclass
class OALeg:
    """One provider's contribution to one open-access resolution attempt --
    what was asked, and what came back, in words a caller can branch on."""

    provider: str  # "arxiv" | "unpaywall" | "download"
    outcome: str  # one of OA_LEG_OUTCOMES
    status_code: int | None = None
    retry_after_s: float | None = None
    error: str | None = None  # str(exc), never a traceback

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": self.provider, "outcome": self.outcome}
        if self.status_code is not None:
            out["status_code"] = self.status_code
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class OAAttempt:
    """The whole open-access resolution attempt: the location it resolved (or
    ``None``), and one :class:`OALeg` per provider asked, in the order they
    were asked."""

    resolution: "OAResolution | None"
    legs: list[OALeg] = field(default_factory=list)

    @property
    def unresolved(self) -> bool:
        """Nothing resolved AND at least one leg failed to answer -- the case
        where filing a ``wanted`` row would be recording a provider failure as
        a fact about the literature."""
        if self.resolution is not None:
            return False
        return any(leg.outcome in OA_UNRESOLVED_OUTCOMES for leg in self.legs)

    @property
    def retryable(self) -> bool:
        """Is the same command worth running again unchanged? True for a rate
        limit, an unreachable host, or a server-side 5xx -- not for a 4xx the
        provider will answer identically next time."""
        for leg in self.legs:
            if leg.outcome not in OA_UNRESOLVED_OUTCOMES:
                continue
            if leg.outcome in ("rate_limited", "transport_unreachable"):
                return True
            if leg.outcome == "http_error" and (leg.status_code or 0) >= 500:
                return True
        return False

    @property
    def retry_after_s(self) -> float | None:
        """The largest ``Retry-After`` any unresolved leg carried, or ``None``
        when no provider named a wait."""
        waits = [
            leg.retry_after_s
            for leg in self.legs
            if leg.outcome in OA_UNRESOLVED_OUTCOMES and leg.retry_after_s is not None
        ]
        return max(waits) if waits else None

    def to_dicts(self) -> list[dict[str, Any]]:
        return [leg.to_dict() for leg in self.legs]


@dataclass
class OAResolution:
    """One resolved legal open-access download location, plus enough
    provenance to fill ``source.license_tier``/``acquisition_route``
    honestly (design Section 6 stage 1: "License fields REQUIRED at
    intake")."""

    url: str
    license_tier: str
    acquisition_route: str
    source_provider: str  # "arxiv" | "unpaywall"
    doi: str | None = None
    arxiv_id: str | None = None
    #: F16 (lane FB-acq item 4): the document's OWN licence grant, beside the
    #: route-derived ``license_tier``. ``license_tier`` answers "how did this
    #: harness come to hold the file"; these two answer "what may be done with
    #: it", which nothing recorded before. ``None`` means nobody read a grant
    #: -- never "no licence". Nothing reads them yet: recording the grant and
    #: changing what the tier means are two decisions, and this is the first.
    license_grant: str | None = None
    license_grant_source: str | None = None


@dataclass
class AcquireResult:
    outcome: str  # "acquired" | "queued" | "unresolved"
    source: dict[str, Any]
    document: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    oa_provider: str | None = None
    metadata_providers: list[str] = field(default_factory=list)
    metadata_failures: list[dict[str, Any]] = field(default_factory=list)
    #: F18: one entry per open-access resolution leg (:class:`OALeg`), in the
    #: order the legs were asked. Present on every outcome, including
    #: ``"acquired"`` -- the legs are how a caller tells "Unpaywall said no"
    #: from "Unpaywall was rate-limited", which used to be the same ``None``.
    oa_legs: list[dict[str, Any]] = field(default_factory=list)
    #: FB-1 item F10a: which backend the enqueued OCR stage will run on, when
    #: the enqueued stage is OCR at all. Passed through from
    #: ``pipeline.add_document`` so ``lit acquire`` can warn in exactly the
    #: words ``ingest add`` uses.
    stage_backend: dict[str, Any] | None = None

    @property
    def searchable(self) -> bool:
        """Can a search find this document RIGHT NOW?

        Lane FB-1 item F4. ``outcome == "acquired"`` reads as done, and it is
        not: acquisition registers a source, writes the raw file, and
        ENQUEUES the first pipeline stage. Nothing is normalized, chunked,
        embedded or indexed until a worker runs that job, so a caller who
        searched for the paper it had just "acquired" got nothing back and
        had no way to tell an empty corpus from an unrun queue. False
        whenever a job was enqueued -- and also false when nothing was
        acquired at all (a queued request has no document to find).
        """
        return self.job is None and self.document is not None

    @property
    def pending_stage(self) -> str | None:
        """The pipeline stage standing between this document and a search
        hit, or ``None`` when nothing is pending. Read off the enqueued
        job's own kind, never guessed from the media type -- the job is what
        a worker will actually run."""
        if not self.job:
            return None
        from trialerror.ingest.pipeline_status import job_stage

        return job_stage(self.job.get("kind"), self.job.get("payload"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "source": self.source,
            "document": self.document,
            "job": self.job,
            "oa_provider": self.oa_provider,
            "metadata_providers": list(self.metadata_providers),
            "metadata_failures": list(self.metadata_failures),
            "oa_legs": [dict(leg) for leg in self.oa_legs],
            "searchable": self.searchable,
            "pending_stage": self.pending_stage,
            "stage_backend": self.stage_backend,
        }


def fetch_bytes(url: str, *, timeout_s: float = 30.0, user_agent: str = "trialerror-litapi/0.1") -> bytes:
    """The REAL PDF downloader -- stdlib ``urllib`` only, same
    zero-dependency posture as
    ``trialerror.litapi.transport.UrllibTransport``, deliberately NOT reused
    here: that transport's contract is JSON-shaped
    (``TransportResponse.json_body``/``text``), not suited to raw binary
    PDF bytes. This is the production default for :func:`acquire`'s
    ``fetch_fn`` parameter -- every test in this package's suite passes a
    fake instead (see this module's own docstring); the only test that
    ever calls the real thing is the live-smoke test
    (``TRIALERROR_LITAPI_LIVE_TESTS=1``)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept": "application/pdf,*/*"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310 - deliberate: this IS the downloader
        return resp.read()


def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == _PDF_MAGIC


def _safe_filename(raw: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", raw).strip("._")
    return (cleaned or "acquired")[:120]


def _license_tier_from_unpaywall(location: dict[str, Any]) -> str:
    license_str = (location.get("license") or "").lower()
    if any(marker in license_str for marker in _OPEN_LICENSE_MARKERS):
        return "open"
    if location.get("host_type") in ("repository", "publisher"):
        return "academic_oa"
    return "unknown"


def _acquisition_route_from_unpaywall(location: dict[str, Any]) -> str:
    host_type = location.get("host_type")
    if host_type == "repository":
        return "institutional"
    if host_type == "publisher":
        return "publisher_oa"
    return "web"


def _classify_oa_error(provider: str, exc: BaseException) -> OALeg:
    """F18's classification table, in one place. Every branch is a distinct
    outcome word, because the whole defect was five different failures
    reaching the same row as the same sentence.

    ``xml.etree.ElementTree.ParseError`` is caught here as well as
    :class:`~trialerror.litapi.errors.LitApiError`: arXiv's feed parser
    (``trialerror.litapi.providers.arxiv._parse_feed``) lets a malformed feed's
    ``ParseError`` out raw, and that is a provider failure like any other, not
    an absence of the paper.
    """
    if isinstance(exc, ProviderNotFoundError):
        return OALeg(provider, "not_found", error=str(exc))
    if isinstance(exc, ProviderConfigError):
        return OALeg(provider, "not_configured", error=str(exc))
    if isinstance(exc, ProviderTransportError):
        retry_after_s = getattr(exc, "retry_after_s", None)
        if exc.host is not None:
            return OALeg(provider, "transport_unreachable", error=str(exc))
        if exc.status_code == 429:
            return OALeg(provider, "rate_limited", status_code=429, retry_after_s=retry_after_s, error=str(exc))
        return OALeg(provider, "http_error", status_code=exc.status_code, error=str(exc))
    return OALeg(provider, "provider_error", error=str(exc))


def _resolve_oa(
    providers: Mapping[str, Any],
    *,
    doi: str | None,
    arxiv_id: str | None,
) -> OAAttempt:
    """The legality fence (module docstring, step 2): tries arXiv first
    (when an arxiv id is known -- arXiv's own PDF is unambiguously legal
    for its own preprint), then Unpaywall via DOI.

    ``providers`` are the instances :func:`acquire` already built, keyed by
    ``.name``. This function constructs none of its own: before F18 it built a
    SECOND ``ArxivProvider``, with a second :class:`RateLimiter`, so the
    OA-leg arXiv request fired immediately after the metadata arXiv request
    and the 3-second spacing arXiv's own ToU asks for was not kept inside a
    single process.

    Nothing is tolerated silently any more. Each leg gets exactly one
    :class:`OALeg` whose ``outcome`` says which of these happened:

    ==========================  ============================================
    ``not_attempted``           no identifier for this leg (or an earlier
                                leg already resolved)
    ``resolved``                a record with an ``oa_pdf_url``
    ``no_oa_location``          a record, no ``oa_pdf_url``
    ``not_found``               ``ProviderNotFoundError`` -- a real absence
    ``not_configured``          ``ProviderConfigError`` -- never asked
    ``transport_unreachable``   no HTTP response was received at all
    ``rate_limited``            HTTP 429
    ``http_error``              any other bad status
    ``provider_error``          any other ``LitApiError``, or a feed
                                ``ParseError``
    ==========================  ============================================

    :func:`acquire` decides what each of those means for the source row --
    this function's job is only the attempt and its honest description.
    """
    legs: list[OALeg] = []
    resolution: OAResolution | None = None

    for name, identifier in (("arxiv", arxiv_id), ("unpaywall", doi)):
        if resolution is not None or not identifier:
            legs.append(OALeg(name, "not_attempted"))
            continue
        provider = providers.get(name)
        if provider is None:
            # No instance was built for this leg at all (a caller that passed a
            # narrower provider set than ALL_CLIENTS). Same meaning as a config
            # refusal: the question was never put to this provider.
            legs.append(OALeg(name, "not_configured", error=f"no {name} provider instance was built"))
            continue
        try:
            if name == "arxiv":
                record = provider.get_by_arxiv(identifier)
            else:
                record = provider.get_by_doi(identifier)
        except (LitApiError, ET.ParseError) as exc:
            legs.append(_classify_oa_error(name, exc))
            continue
        if record is None:
            legs.append(OALeg(name, "not_found", error=f"{name}: no record for {identifier!r}"))
            continue
        if not record.oa_pdf_url:
            legs.append(OALeg(name, "no_oa_location"))
            continue
        legs.append(OALeg(name, "resolved"))
        if name == "arxiv":
            # F16: the tier stays exactly what it was -- this READS the grant,
            # it does not change what the route means. A failed grant lookup
            # never blocks or downgrades the acquisition and is never an OALeg.
            grant, grant_source = provider.get_license(record)
            resolution = OAResolution(
                url=record.oa_pdf_url, license_tier="open", acquisition_route="author_posted",
                source_provider="arxiv", doi=record.doi, arxiv_id=record.arxiv_id,
                license_grant=grant, license_grant_source=grant_source,
            )
        else:
            best = (record.other or {}).get("best_oa_location") or {}
            reported = (best.get("license") or "").strip().lower() or None
            resolution = OAResolution(
                url=record.oa_pdf_url,
                license_tier=_license_tier_from_unpaywall(best),
                acquisition_route=_acquisition_route_from_unpaywall(best),
                source_provider="unpaywall", doi=record.doi, arxiv_id=None,
                license_grant=reported,
                license_grant_source="unpaywall_best_oa_location" if reported else "none_reported",
            )

    return OAAttempt(resolution=resolution, legs=legs)


def _request_rights_notes(legs: list[OALeg]) -> str:
    """The ``rights_notes`` of a filed ``wanted`` row, built from what the legs
    actually said. F18: the old sentence -- "no legal open-access location
    found via Unpaywall/arXiv" -- was written for a rate limit as much as for
    a genuine absence, and a human reading the queue could not tell which had
    happened. That phrase now appears ONLY when every leg that was actually
    asked came back ``not_found`` or ``no_oa_location``."""
    rendered = []
    for leg in legs:
        text = f"{leg.provider}={leg.outcome}"
        if leg.outcome == "not_configured":
            text += _NOT_CONFIGURED_GLOSS
        rendered.append(text)
    attempted = [leg for leg in legs if leg.outcome != "not_attempted"]
    conclusive = bool(attempted) and all(leg.outcome in ("not_found", "no_oa_location") for leg in attempted)
    head = "no legal open-access location found; " if conclusive else ""
    return (
        f"{head}queued for user fulfillment; open-access resolution: "
        f"{', '.join(rendered)}; no paywall circumvention attempted"
    )


def _source_already_has_document(store: Store, source_id: str) -> bool:
    row = store.knowledge.execute("SELECT 1 FROM document WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return row is not None


def acquire(
    store: Store,
    *,
    program_root: Path,
    created_by_launch: str,
    litapi_config: LitApiConfig,
    doi: str | None = None,
    arxiv_id: str | None = None,
    transport: ProviderTransport | None = None,
    config: dict[str, Any] | None = None,
    fetch_fn: Callable[[str], bytes] = fetch_bytes,
    yes: bool = False,
    pacing_dir: Path | None = None,
) -> AcquireResult:
    """The full acquisition seam. Exactly one of ``doi``/``arxiv_id`` is
    the caller's own identifier; either may ALSO end up filled in from the
    other via reconciled metadata (e.g. an arXiv preprint's own journal
    DOI, once published) -- both resolved values are used for OA
    resolution and stamped onto the registered ``source`` row.

    ``transport`` defaults to a real :class:`~trialerror.litapi.transport.UrllibTransport`
    (production use); every test in this package's suite passes a
    :class:`~trialerror.litapi.transport.FakeTransport` instead. ``fetch_fn``
    defaults to the real :func:`fetch_bytes`; every test passes a fake.
    Neither default is itself gated by ``TRIALERROR_LITAPI_LIVE_TESTS`` --
    that env var gates which TESTS are allowed to exercise the real
    defaults (mirroring ``tests/test_litapi_live_smoke.py``'s own
    discipline), not this function's production behavior.

    ``pacing_dir`` (lane FB-acq item 2) is forwarded to every provider and
    gives its rate limiter a stamp file there, so pacing survives across CLI
    invocations. ``None`` -- the default every test passes -- writes nothing.
    """
    if not doi and not arxiv_id:
        raise ValueError("acquire() requires doi or arxiv_id")

    real_transport = transport if transport is not None else UrllibTransport()

    # 1. metadata reconciliation -- best-effort, tolerates total failure.
    providers = build_default_providers(
        litapi_config, transport=real_transport, provider_classes=ALL_CLIENTS,
        program_root=program_root, pacing_dir=pacing_dir,
    )
    client = LitApiClient(providers)
    metadata = None
    metadata_providers: list[str] = []
    metadata_failures: list[dict[str, Any]] = []
    try:
        result = client.lookup_doi(doi) if doi else client.lookup_arxiv(arxiv_id)
        metadata = result.record
        metadata_providers = result.providers_succeeded
        metadata_failures = result.providers_failed
    except AllProvidersFailedError as exc:
        metadata_failures = list(exc.details.get("failures", []))

    resolved_doi = doi or (metadata.doi if metadata else None)
    resolved_arxiv_id = arxiv_id or (metadata.arxiv_id if metadata else None)
    title = (metadata.title if metadata else None) or resolved_doi or resolved_arxiv_id or "untitled acquisition"
    authors = ", ".join(metadata.authors) if metadata and metadata.authors else None
    year = metadata.year if metadata else None
    venue = metadata.venue if metadata else None

    # 2. legal-OA-only resolution (see module docstring + _resolve_oa). The
    # SAME provider instances metadata reconciliation just used, so each
    # provider's own rate limiter paces both legs (F18).
    attempt = _resolve_oa(
        {p.name: p for p in providers}, doi=resolved_doi, arxiv_id=resolved_arxiv_id
    )
    oa = attempt.resolution

    if oa is not None:
        try:
            data = fetch_fn(oa.url)
        except OSError as exc:
            # A download URLError/HTTPError/timeout used to propagate out of
            # this function and reach the user as a bare traceback (cli/lit.py
            # catches ValueError and the litapi/ingest/store errors, none of
            # which an OSError is). It is a provider failure like any other.
            attempt.legs.append(OALeg("download", "download_failed", error=str(exc)))
            attempt.resolution = None
            oa = None
        else:
            if not _looks_like_pdf(data):
                # Refuse to register a non-PDF (very likely an HTML paywall or
                # error page) as an acquired source -- fall through to the
                # not-openly-available path below instead. This one IS an
                # answer, so the row is still filed (download_not_pdf is not in
                # OA_UNRESOLVED_OUTCOMES).
                attempt.legs.append(OALeg("download", "download_not_pdf"))
                attempt.resolution = None
                oa = None

    if attempt.unresolved:
        # 2b. Write NOTHING. A provider failure is not "no open-access copy
        # exists", and the request queue's own lifecycle has no state that
        # could later be corrected into the `wanted` row a conclusive run
        # would file (trialerror.ingest.requests: `failed` -> `requested` only).
        return AcquireResult(
            outcome="unresolved", source={}, oa_legs=attempt.to_dicts(),
            metadata_providers=metadata_providers, metadata_failures=metadata_failures,
        )

    if oa is not None:
        roots = pipeline.resolve_ingest_roots(program_root, config)
        raw_dir = roots[0]
        raw_dir.mkdir(parents=True, exist_ok=True)
        filename_stub = _safe_filename(resolved_doi or resolved_arxiv_id or "acquired")
        dest_path = raw_dir / f"{filename_stub}.pdf"
        dest_path.write_bytes(data)
        content_sha256 = hashlib.sha256(data).hexdigest()

        source_row = pipeline.register_source(
            store, kind="paper", title=title, authors=authors, year=year, venue=venue,
            url=oa.url, doi=resolved_doi, arxiv_id=resolved_arxiv_id,
            content_sha256=content_sha256, license_tier=oa.license_tier, acquisition_route=oa.acquisition_route,
            registered_by_launch=created_by_launch,
            license_grant=oa.license_grant, license_grant_source=oa.license_grant_source,
            rights_notes=(
                f"OA acquired via {oa.source_provider}; retrieved_ts={now()}; source_url={oa.url}"
                f"; license_grant={oa.license_grant or 'none'} ({oa.license_grant_source})"
            ),
            request_state="delivered", config=config,
        )

        already_deduped_and_ingested = (
            source_row.get("dedup_of") == source_row.get("source_id")
            and _source_already_has_document(store, source_row["source_id"])
        )
        if already_deduped_and_ingested:
            return AcquireResult(
                outcome="acquired", source=source_row, oa_provider=oa.source_provider,
                metadata_providers=metadata_providers, metadata_failures=metadata_failures,
                oa_legs=attempt.to_dicts(),
            )

        add_result = pipeline.add_document(
            store, program_root=program_root, source_id=source_row["source_id"], raw_path=dest_path,
            created_by_launch=created_by_launch, config=config, yes=yes,
        )
        return AcquireResult(
            outcome="acquired", source=source_row, document=add_result["document"], job=add_result["job"],
            oa_provider=oa.source_provider, metadata_providers=metadata_providers, metadata_failures=metadata_failures,
            oa_legs=attempt.to_dicts(), stage_backend=add_result.get("stage_backend"),
        )

    # 3. conclusively not openly available -- file a `wanted` request-queue row
    # (never a paywall-circumvention attempt). The notes say what each leg
    # actually answered, so "nobody has a legal copy" and "we were throttled"
    # are not the same row (F18) -- and a throttled attempt never gets here at
    # all, it returned `unresolved` above.
    source_row = pipeline.register_source(
        store, kind="paper", title=title, authors=authors, year=year, venue=venue,
        url=(metadata.url if metadata else None), doi=resolved_doi, arxiv_id=resolved_arxiv_id,
        license_tier="unknown", acquisition_route="user_delivered", registered_by_launch=created_by_launch,
        rights_notes=_request_rights_notes(attempt.legs),
        request_state="wanted", config=config,
    )
    try:
        ingest_requests.write_requests_md(store, program_root, config=config)
    except Exception:  # noqa: BLE001 - deliberate: a rendered-view refresh must never fail the acquisition itself
        pass
    return AcquireResult(
        outcome="queued", source=source_row, metadata_providers=metadata_providers,
        metadata_failures=metadata_failures, oa_legs=attempt.to_dicts(),
    )
