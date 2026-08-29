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
4. **If NOT openly available anywhere**: ``register_source`` with
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
from typing import Any, Callable

from trialerror.ingest import pipeline
from trialerror.ingest import requests as ingest_requests
from trialerror.litapi.client import ALL_CLIENTS, LitApiClient, build_default_providers
from trialerror.litapi.config import LitApiConfig
from trialerror.litapi.errors import AllProvidersFailedError, LitApiError
from trialerror.litapi.providers.arxiv import ArxivProvider
from trialerror.litapi.providers.unpaywall import UnpaywallProvider
from trialerror.litapi.transport import ProviderTransport, UrllibTransport
from trialerror.stores.store import Store
from trialerror.util.timeutil import now

__all__ = [
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


@dataclass
class AcquireResult:
    outcome: str  # "acquired" | "queued"
    source: dict[str, Any]
    document: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    oa_provider: str | None = None
    metadata_providers: list[str] = field(default_factory=list)
    metadata_failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "source": self.source,
            "document": self.document,
            "job": self.job,
            "oa_provider": self.oa_provider,
            "metadata_providers": list(self.metadata_providers),
            "metadata_failures": list(self.metadata_failures),
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


def _resolve_oa(
    transport: ProviderTransport,
    litapi_config: LitApiConfig,
    *,
    program_root: Path | None,
    doi: str | None,
    arxiv_id: str | None,
) -> OAResolution | None:
    """The legality fence (module docstring, step 2): tries arXiv first
    (when an arxiv id is known -- arXiv's own PDF is unambiguously legal
    for its own preprint), then Unpaywall via DOI. Either provider being
    unusable (no record, refused for missing config, a transport hiccup)
    is tolerated silently here -- ``acquire`` decides what "no legal OA
    location resolved" means for the source row, this function's job is
    only the resolution attempt itself."""
    if arxiv_id:
        try:
            provider = ArxivProvider(transport, litapi_config.arxiv, program_root=program_root)
            record = provider.get_by_arxiv(arxiv_id)
        except LitApiError:
            record = None
        if record is not None and record.oa_pdf_url:
            return OAResolution(
                url=record.oa_pdf_url, license_tier="open", acquisition_route="author_posted",
                source_provider="arxiv", doi=record.doi, arxiv_id=record.arxiv_id,
            )

    if doi:
        try:
            provider = UnpaywallProvider(transport, litapi_config.unpaywall, program_root=program_root)
            record = provider.get_by_doi(doi)
        except LitApiError:
            record = None
        if record is not None and record.oa_pdf_url:
            best = (record.other or {}).get("best_oa_location") or {}
            return OAResolution(
                url=record.oa_pdf_url,
                license_tier=_license_tier_from_unpaywall(best),
                acquisition_route=_acquisition_route_from_unpaywall(best),
                source_provider="unpaywall", doi=record.doi, arxiv_id=None,
            )

    return None


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
    """
    if not doi and not arxiv_id:
        raise ValueError("acquire() requires doi or arxiv_id")

    real_transport = transport if transport is not None else UrllibTransport()

    # 1. metadata reconciliation -- best-effort, tolerates total failure.
    providers = build_default_providers(
        litapi_config, transport=real_transport, provider_classes=ALL_CLIENTS, program_root=program_root
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

    # 2. legal-OA-only resolution (see module docstring + _resolve_oa).
    oa = _resolve_oa(
        real_transport, litapi_config, program_root=program_root, doi=resolved_doi, arxiv_id=resolved_arxiv_id
    )

    if oa is not None:
        data = fetch_fn(oa.url)
        if not _looks_like_pdf(data):
            # Refuse to register a non-PDF (very likely an HTML paywall or
            # error page) as an acquired source -- fall through to the
            # not-openly-available path below instead.
            oa = None

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
            rights_notes=f"OA acquired via {oa.source_provider}; retrieved_ts={now()}; source_url={oa.url}",
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
            )

        add_result = pipeline.add_document(
            store, program_root=program_root, source_id=source_row["source_id"], raw_path=dest_path,
            created_by_launch=created_by_launch, config=config, yes=yes,
        )
        return AcquireResult(
            outcome="acquired", source=source_row, document=add_result["document"], job=add_result["job"],
            oa_provider=oa.source_provider, metadata_providers=metadata_providers, metadata_failures=metadata_failures,
        )

    # 3. not openly available anywhere -- file a `wanted` request-queue row
    # (never a paywall-circumvention attempt: C-0048/49 posture).
    source_row = pipeline.register_source(
        store, kind="paper", title=title, authors=authors, year=year, venue=venue,
        url=(metadata.url if metadata else None), doi=resolved_doi, arxiv_id=resolved_arxiv_id,
        license_tier="unknown", acquisition_route="user_delivered", registered_by_launch=created_by_launch,
        rights_notes="no legal open-access location found via Unpaywall/arXiv -- queued for user "
                      "fulfillment (C-0048/49 posture: no paywall circumvention attempted)",
        request_state="wanted", config=config,
    )
    try:
        ingest_requests.write_requests_md(store, program_root, config=config)
    except Exception:  # noqa: BLE001 - deliberate: a rendered-view refresh must never fail the acquisition itself
        pass
    return AcquireResult(
        outcome="queued", source=source_row, metadata_providers=metadata_providers, metadata_failures=metadata_failures
    )
