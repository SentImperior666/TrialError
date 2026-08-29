"""``trialerror.litapi`` -- the redundant scientific-literature metadata client
layer (C-0064 litapi-preview build, session 1f478c74b). A v1-PREVIEW
module built early (budget headroom) against the design's own scheduling:
``docs/DESIGN_v0.md`` D5 / F1 / F2 rows and
``docs/FEATURE_MATRIX.md``'s "(d) Literature acquisition & citation-graph
APIs" all schedule this to v1, out of v0's critical path -- this package
is fully standalone and makes NO edits to any landed v0 module.

Given a DOI/arxiv-id/title, fetches normalized metadata (+ citations) from
MULTIPLE providers (OpenAlex, Semantic Scholar, and -- as of the
v3-acquisition build, C-0064 flags F1/F2 RESOLVED -- arXiv and Unpaywall)
with reconciliation, so a single API's fragility or rate-limit never
blocks a lookup -- the paper-qa provider/post-processor pattern
(``docs/mining/S1-scilit-1__paper-qa.md``: "automatic redundant fetching
of paper metadata"), reimplemented natively (no paper-qa dependency).

Submodules:

- :mod:`trialerror.litapi.errors` -- this package's exception hierarchy.
- :mod:`trialerror.litapi.transport` -- the ``ProviderTransport`` abstraction
  (``FakeTransport`` for offline tests, ``UrllibTransport`` for real
  network -- see that module's docstring for the offline-testability
  contract).
- :mod:`trialerror.litapi.models` -- ``WorkRecord``/``CitationEdge``/
  ``CitationsPage`` + DOI/arXiv identity normalization.
- :mod:`trialerror.litapi.config` -- ``trialerror.toml`` ``[litapi]`` section
  loader (rate-limit/retry knobs, config-pathed API keys, now for all
  four providers).
- :mod:`trialerror.litapi.providers` -- ``OpenAlexProvider``/
  ``SemanticScholarProvider``/``ArxivProvider``/``UnpaywallProvider``,
  each implementing the common ``Provider`` interface.
- :mod:`trialerror.litapi.reconcile` -- the post-processor: merges redundant
  provider results into one record (DOI-preferred identity, provenance).
- :mod:`trialerror.litapi.client` -- ``LitApiClient``, the top-level
  orchestration (``lookup_doi``/``lookup_arxiv``/``search``/
  ``get_citations``) + the ``DEFAULT_CLIENTS``/``ALL_CLIENTS`` pattern.
- :mod:`trialerror.litapi.checks` -- doctor checks (auto-discovered; imported
  for side effects by ``trialerror.util.doctor.discover_and_register_checks``,
  same convention as every other subsystem's ``checks.py``) -- including
  ``litapi_providers_ready`` (v3-acquisition build): per-provider
  key-gated readiness across all four.
- :mod:`trialerror.ingest.acquire` (v3-acquisition build, lives under
  ``trialerror.ingest`` not this package -- the acquisition->ingest seam, see
  the "M7 ingestion" bullet below) -- ``LitApiClient`` results feed into
  ``trialerror.ingest.pipeline``'s existing ``register_source``/``add_document``.
- CLI: ``trialerror/cli/lit.py`` (``trialerror lit lookup|citations|search|acquire``),
  auto-discovered by ``trialerror.cli.discover_groups`` -- not part of this
  package proper (the CLI-group convention lives under ``trialerror/cli/``
  repo-wide), listed here for discoverability.

**v1 wiring seams:**

- **M7 ingestion -- NOW REAL** (v3-acquisition build, C-0064 flags F1/F2
  RESOLVED, ``docs/EXTERNAL_API_FACTS.md``): :mod:`trialerror.ingest.acquire`
  (a NEW module -- ``trialerror.ingest.pipeline`` itself received zero edits)
  implements exactly the flow this note used to only describe:
  ``LitApiClient.lookup_doi``/``lookup_arxiv`` (now against
  :data:`~trialerror.litapi.client.ALL_CLIENTS` -- OpenAlex + Semantic Scholar +
  the two providers this same build adds, arXiv + Unpaywall) resolves
  metadata BEFORE ``trialerror.ingest.pipeline.register_source``/
  ``add_document``, pre-filling ``title``/``authors``/``year``/``venue``/
  ``url``/``doi``/``arxiv_id`` (all already-existing ``source`` columns --
  no schema change needed after all) plus a legal-OA-only download step
  (arXiv's own PDF link / Unpaywall's ``best_oa_location`` -- never
  OpenAlex's/S2's own ``oa_pdf_url`` field, see that module's docstring for
  why) and a `wanted`-queue fallback when no legal OA copy exists. Reachable
  via ``trialerror lit acquire --doi <doi>|--arxiv <id> --launch-id <launch>``
  (``trialerror/cli/lit.py``).
- **M9 verification** (``trialerror.verify.hypothesis``): still a documented,
  NOT-implemented seam (out of this build's own lane -- ``trialerror.verify``
  received zero edits). That module's
  "judgment request" envelope contract (evidence text + anchor + fixed
  label vocabulary, filled by an external judge callable -- see
  ``trialerror/verify/__init__.py``'s "LLM-judgment boundary" note) is
  corpus-internal today (``trialerror.retrieve.engine`` over already-ingested
  chunks). A v1 extension could feed
  ``LitApiClient.get_citations``/``search`` results into the SAME
  envelope shape as external evidence candidates for a hypothesis not yet
  backed by an ingested source -- again, a seam described here, not
  implemented (would require ``trialerror.verify.hypothesis`` to accept an
  external-evidence source, which is that package's lane).
"""

from __future__ import annotations

from trialerror.litapi.client import ALL_CLIENTS, DEFAULT_CLIENTS, LitApiClient, LookupResult, SearchResult
from trialerror.litapi.errors import (
    AllProvidersFailedError,
    LitApiError,
    ProviderConfigError,
    ProviderNotFoundError,
    ProviderTransportError,
    ProviderUnsupportedOperationError,
    TransportNotConfiguredError,
)
from trialerror.litapi.models import CitationEdge, CitationsPage, WorkRecord

__all__ = [
    "LitApiClient",
    "LookupResult",
    "SearchResult",
    "DEFAULT_CLIENTS",
    "ALL_CLIENTS",
    "WorkRecord",
    "CitationEdge",
    "CitationsPage",
    "LitApiError",
    "ProviderConfigError",
    "ProviderTransportError",
    "ProviderNotFoundError",
    "ProviderUnsupportedOperationError",
    "AllProvidersFailedError",
    "TransportNotConfiguredError",
]
