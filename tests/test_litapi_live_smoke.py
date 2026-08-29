"""The ONE test file in this package's suite allowed to touch the real
network -- and even here, only when explicitly opted in. Design brief's
F18 discipline (same shape as ``trialerror.ingest.backends``'s GPU-gated
``Real*`` backend tests): skipped unless ``TRIALERROR_LITAPI_LIVE_TESTS=1`` is
set in the environment, so the default ``pytest`` run (CI, a fresh clone,
an offline dev machine) never makes an HTTP request.

Run explicitly with e.g. (PowerShell)::

    $env:TRIALERROR_LITAPI_LIVE_TESTS = "1"
    pytest tests/test_litapi_live_smoke.py -v

This is deliberately the ONLY place :class:`~trialerror.litapi.transport.UrllibTransport`
is exercised end-to-end against a real server -- everywhere else in this
package's suite uses :class:`~trialerror.litapi.transport.FakeTransport`.
"""

from __future__ import annotations

import os

import pytest

from trialerror.litapi.client import LitApiClient, build_default_providers
from trialerror.litapi.config import load_litapi_config

LIVE = os.environ.get("TRIALERROR_LITAPI_LIVE_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="live network test: set TRIALERROR_LITAPI_LIVE_TESTS=1 to run (see module docstring)"
)


@pytest.fixture()
def client() -> LitApiClient:
    config = load_litapi_config({})
    providers = build_default_providers(config)  # real UrllibTransport, default conservative config
    return LitApiClient(providers)


def test_live_lookup_doi_known_paper(client):
    # arXiv's own well-known "Attention Is All You Need" DOI-registered
    # preprint -- stable, unlikely to ever be retracted/removed.
    result = client.lookup_doi("10.48550/arXiv.1706.03762")
    assert result.record.title
    assert result.providers_succeeded


def test_live_search_returns_something(client):
    result = client.search("attention is all you need", limit=3)
    assert result.records


def test_live_acquire_downloads_real_pdf_from_arxiv(store, program_root):
    """v3-acquisition build. The ONE test in this whole package's suite
    that exercises trialerror.ingest.acquire's REAL defaults end to end
    (real UrllibTransport for metadata, real fetch_bytes for the PDF
    download itself) -- every other acquire test
    (tests/test_litapi_acquire.py) injects fakes for both, per this
    module's own docstring discipline. Uses the same stable, well-known
    arXiv preprint the lookup/search tests above already rely on."""
    from trialerror.ingest import acquire as acquire_mod
    from trialerror.litapi.config import load_litapi_config
    from tests._ingest_fixtures import bootstrap_launch

    launch_id = bootstrap_launch(store)
    litapi_cfg = load_litapi_config({})

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="1706.03762", created_by_launch=launch_id,
        litapi_config=litapi_cfg,
    )

    assert result.outcome == "acquired"
    assert result.oa_provider == "arxiv"
    assert result.document is not None
    assert result.job is not None
    assert (program_root / "raw").glob("*.pdf")
