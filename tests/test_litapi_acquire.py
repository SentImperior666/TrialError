"""Tests for :mod:`trialerror.ingest.acquire` -- the acquisition->ingest seam.
Every test here injects a :class:`~trialerror.litapi.transport.FakeTransport`
AND a fake ``fetch_fn`` (never the real :func:`trialerror.ingest.acquire.fetch_bytes`)
so nothing in this file touches a real socket, matching this whole
package's offline-testability discipline. The one real-network exception
(``TRIALERROR_LITAPI_LIVE_TESTS=1``) lives in ``tests/test_litapi_live_smoke.py``
alongside ``trialerror.litapi``'s own live-smoke tests."""

from __future__ import annotations

from urllib.parse import quote, urlencode

import pytest

from trialerror.ingest import acquire as acquire_mod
from trialerror.litapi.config import load_litapi_config
from trialerror.litapi.transport import FakeTransport, TransportResponse
from tests._ingest_fixtures import bootstrap_launch, build_minimal_pdf
from tests._litapi_fixtures import load_fixture, load_text_fixture

ARXIV_BASE = "http://export.arxiv.org/api"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


def _litapi_config(*, unpaywall_mailto: str | None = "me@example.org"):
    raw = {
        "litapi": {
            "openalex": {"min_interval_s": 0.0},
            "semanticscholar": {"min_interval_s": 0.0},
            "arxiv": {"min_interval_s": 0.0},
            "unpaywall": {"min_interval_s": 0.0, **({"mailto": unpaywall_mailto} if unpaywall_mailto else {})},
        }
    }
    return load_litapi_config(raw)


def _arxiv_id_list_url(arxiv_id: str) -> str:
    return f"{ARXIV_BASE}/query?{urlencode({'id_list': arxiv_id})}"


def _unpaywall_doi_url(doi: str, *, email: str = "me@example.org") -> str:
    return f"{UNPAYWALL_BASE}/{quote(doi, safe='')}?{urlencode({'email': email})}"


def _pdf_bytes() -> bytes:
    return build_minimal_pdf(["Acquired fixture page one.", "Acquired fixture page two."])


def _fake_fetch(data: bytes):
    calls: list[str] = []

    def _fetch(url: str) -> bytes:
        calls.append(url)
        return data

    _fetch.calls = calls  # type: ignore[attr-defined]
    return _fetch


# ---------------------------------------------------------------------------
# acquired via arXiv's own PDF link
# ---------------------------------------------------------------------------


def test_acquire_by_arxiv_id_downloads_and_registers(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    fetch = _fake_fetch(_pdf_bytes())

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=fetch,
    )

    assert result.outcome == "acquired"
    assert result.oa_provider == "arxiv"
    assert result.source["title"] == "A Fixture Paper About Distributed Systems Metadata Reconciliation"
    assert result.source["license_tier"] == "open"
    assert result.source["acquisition_route"] == "author_posted"
    assert result.source["arxiv_id"] == "2101.00001"
    assert result.document is not None
    assert result.document["doc_id"].startswith("DOC-")
    assert result.job is not None
    assert fetch.calls == ["http://arxiv.org/pdf/2101.00001v2"]

    # the downloaded file actually landed under the program's raw/ root.
    raw_dir = program_root / "raw"
    assert list(raw_dir.glob("*.pdf"))


def test_acquire_metadata_providers_and_failures_are_reported(store, program_root):
    """openalex/semanticscholar have no registered routes and no keys --
    both fail gracefully (TransportNotConfiguredError/caught) and are
    recorded in metadata_failures, while arxiv (the only provider actually
    wired up in this test) succeeds and is recorded in metadata_providers."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(unpaywall_mailto=None), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert "arxiv" in result.metadata_providers
    failed_names = {f["provider"] for f in result.metadata_failures}
    assert {"openalex", "semanticscholar", "unpaywall"} <= failed_names


# ---------------------------------------------------------------------------
# acquired via Unpaywall (doi-only identifier)
# ---------------------------------------------------------------------------


def test_acquire_by_doi_downloads_via_unpaywall_publisher_location(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(_unpaywall_doi_url("10.1234/fixture.5678"), json_body=load_fixture("unpaywall_doi_hit.json"))
    fetch = _fake_fetch(_pdf_bytes())

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1234/fixture.5678", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=fetch,
    )

    assert result.outcome == "acquired"
    assert result.oa_provider == "unpaywall"
    assert result.source["license_tier"] == "open"  # cc-by
    assert result.source["acquisition_route"] == "publisher_oa"
    assert result.source["doi"] == "10.1234/fixture.5678"
    assert fetch.calls == ["https://example.org/fixture.pdf"]


def test_acquire_by_doi_via_unpaywall_repository_location_maps_to_academic_oa(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(_unpaywall_doi_url("10.1234/repo.0002"), json_body=load_fixture("unpaywall_doi_hit_repository.json"))

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1234/repo.0002", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.outcome == "acquired"
    assert result.source["license_tier"] == "academic_oa"  # repository host, no license string
    assert result.source["acquisition_route"] == "institutional"


# ---------------------------------------------------------------------------
# not openly available -- queued (`wanted`) instead
# ---------------------------------------------------------------------------


def test_acquire_not_open_anywhere_files_wanted_request_row(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(_unpaywall_doi_url("10.1234/paywalled.0001"), json_body=load_fixture("unpaywall_doi_not_oa.json"))

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1234/paywalled.0001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(b"unused"),
    )

    assert result.outcome == "queued"
    assert result.document is None
    assert result.job is None
    assert result.source["request_state"] == "wanted"
    assert result.source["acquisition_route"] == "user_delivered"
    assert result.source["license_tier"] == "unknown"
    assert result.source["doi"] == "10.1234/paywalled.0001"

    # requests/REQUESTS.md was refreshed with the new wanted row (rendered
    # columns are source_id/title/license_tier/acquisition_route -- see
    # trialerror.ingest.requests.render_requests_md).
    requests_md = (program_root / "requests" / "REQUESTS.md").read_text(encoding="utf-8")
    assert result.source["source_id"] in requests_md


def test_acquire_never_registers_a_downloaded_non_pdf(store, program_root):
    """An OA url resolves and 'downloads' successfully, but the bytes
    don't start with the %PDF- magic header (a very likely HTML paywall
    or error page) -- refused, falls through to the `wanted` queue rather
    than registering a mislabeled source (paper-search-mcp's own
    content-sniffing pattern, see trialerror.ingest.acquire's module docstring)."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    not_a_pdf = b"<html><body>this is actually an error page, not a pdf</body></html>"

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(not_a_pdf),
    )

    assert result.outcome == "queued"
    assert result.source["request_state"] == "wanted"
    # nothing was ever written to raw/ -- the sniff failure happens before
    # any file write is attempted.
    raw_dir = program_root / "raw"
    assert not raw_dir.exists() or not list(raw_dir.glob("*.pdf"))


# ---------------------------------------------------------------------------
# dedup: acquiring the identical content twice does not double-enqueue
# ---------------------------------------------------------------------------


def test_acquire_twice_with_identical_content_does_not_reingest(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    data = _pdf_bytes()

    first = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(data),
    )
    second = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(data),
    )

    assert first.outcome == "acquired" and first.document is not None
    assert second.outcome == "acquired"
    assert second.source["source_id"] == first.source["source_id"]
    assert second.source["dedup_of"] == first.source["source_id"]
    assert second.document is None and second.job is None  # no second pipeline run

    doc_count = store.knowledge.execute(
        "SELECT COUNT(*) FROM document WHERE source_id = ?", (first.source["source_id"],)
    ).fetchone()[0]
    assert doc_count == 1


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_acquire_requires_doi_or_arxiv_id(store, program_root):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError):
        acquire_mod.acquire(
            store, program_root=program_root, created_by_launch=launch_id,
            litapi_config=_litapi_config(), transport=FakeTransport(), fetch_fn=_fake_fetch(b""),
        )


# ---------------------------------------------------------------------------
# fetch_bytes -- the real downloader's own request-shape, offline
# (urllib.request.urlopen monkeypatched, never a real socket).
# ---------------------------------------------------------------------------


def test_fetch_bytes_sends_expected_headers_and_returns_body(monkeypatch):
    calls = {}

    class _FakeResponse:
        def read(self):
            return b"%PDF-fake-body"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout):
        calls["url"] = request.full_url
        calls["headers"] = dict(request.header_items())
        calls["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    body = acquire_mod.fetch_bytes("https://example.org/fixture.pdf", timeout_s=12.0)

    assert body == b"%PDF-fake-body"
    assert calls["url"] == "https://example.org/fixture.pdf"
    assert calls["timeout"] == 12.0
    assert calls["headers"].get("User-agent") == "trialerror-litapi/0.1"
