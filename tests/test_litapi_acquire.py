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
from trialerror.litapi.providers.base import RateLimiter
from trialerror.litapi.transport import FakeTransport, TransportResponse
from tests._ingest_fixtures import bootstrap_launch, build_minimal_pdf
from tests._litapi_fixtures import load_fixture, load_text_fixture

ARXIV_BASE = "https://export.arxiv.org/api"  # matches config.py's real (https) default
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


def _litapi_config(*, unpaywall_mailto: str | None = "me@example.org", arxiv: dict | None = None):
    """``retry_attempts = 1`` everywhere, deliberately: every route in this file
    is a single deterministic response, and a retried status would otherwise
    spend real time asleep in ``get_with_retry``'s backoff (lane FB-acq item 2
    made 429 retryable on all four providers). Retry behaviour itself is
    covered, with fake clocks and no real sleeping, in
    ``tests/test_litapi_retry.py``."""
    raw = {
        "litapi": {
            "openalex": {"min_interval_s": 0.0, "retry_attempts": 1},
            "semanticscholar": {"min_interval_s": 0.0, "retry_attempts": 1},
            "arxiv": {"min_interval_s": 0.0, "retry_attempts": 1, **(arxiv or {})},
            "unpaywall": {
                "min_interval_s": 0.0, "retry_attempts": 1,
                **({"mailto": unpaywall_mailto} if unpaywall_mailto else {}),
            },
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


def _source_count(store) -> int:
    return store.knowledge.execute("SELECT COUNT(*) FROM source").fetchone()[0]


def _legs_by_provider(result) -> dict:
    return {leg["provider"]: leg for leg in result.oa_legs}


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


class _UnreachableTransport:
    """litapi-arxiv-https build, task 3 / C-0093(a) incident repro: every
    ``.get`` call raises ``urllib.error.URLError``, exactly what the real
    ``UrllibTransport`` raises for a host an egress policy admitting only
    tcp/443 refuses. Unlike every other test in this file, this is NOT a
    ``FakeTransport`` -- the whole point is exercising the real transport-
    level-failure path (``get_with_retry``'s own wrapping into
    ``ProviderTransportError(host=..., scheme=...)``), not a canned
    JSON/XML response."""

    def get(self, url, *, headers=None, timeout_s=None):
        import urllib.error

        raise urllib.error.URLError("no route to host")


def test_acquire_transport_unreachable_on_every_provider_tolerates_and_reports_transport_unreachable(
    store, program_root
):
    """acquire() still tolerates a total METADATA failure (module docstring:
    'a total metadata-lookup failure does NOT abort acquisition'), and this
    test pins its metadata contract: metadata_providers empty, every
    metadata_failures entry code='transport_unreachable' with host/scheme
    recorded, no traceback.

    The OUTCOME changed with F18. An unreachable host is also what the arXiv
    OA leg hit, and `unresolved` (no row at all) is what that means now -- the
    old `queued` row pinned the defect: it said "no legal open-access location
    found" about a paper nobody could ask about.
    """
    launch_id = bootstrap_launch(store)

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=_UnreachableTransport(), fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.outcome == "unresolved"
    assert result.source == {}
    assert _source_count(store) == 0
    assert result.metadata_providers == []
    assert result.metadata_failures  # non-empty
    for failure in result.metadata_failures:
        assert failure["code"] == "transport_unreachable"
        assert failure["host"]
        assert failure["scheme"]
    assert {"provider": "arxiv", "outcome": "transport_unreachable"} in [
        {k: v for k, v in leg.items() if k in ("provider", "outcome")} for leg in result.oa_legs
    ]


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

    # F18: a conclusive absence STILL files the row, and its notes still carry
    # the sentence -- that is the one case the sentence is true of.
    assert "no legal open-access location found" in result.source["rights_notes"]
    assert "unpaywall=no_oa_location" in result.source["rights_notes"]
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
# F18 (lane FB-acq item 1): a provider that did not ANSWER files no row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code,expected_outcome,expected_status",
    [
        (429, "rate_limited", 429),
        (406, "http_error", 406),
        (503, "http_error", 503),
    ],
    ids=["429-rate-limited", "406-http-error", "503-http-error"],
)
def test_acquire_unpaywall_http_failure_files_no_row(
    store, program_root, status_code, expected_outcome, expected_status
):
    """FAILS BEFORE F18: every one of these came back as a `wanted` row whose
    rights_notes said "no legal open-access location found" -- a provider
    failure recorded as a fact about the literature."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _unpaywall_doi_url("10.1000/example"),
        TransportResponse(status_code=status_code, json_body={"error": True}, text="{}"),
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(b"unused"),
    )

    assert result.outcome == "unresolved"
    assert result.source == {}
    assert _source_count(store) == 0
    leg = _legs_by_provider(result)["unpaywall"]
    assert leg["outcome"] == expected_outcome
    assert leg["status_code"] == expected_status
    # the arXiv leg had no identifier of its own and says so, rather than
    # being silently absent from the record.
    assert _legs_by_provider(result)["arxiv"]["outcome"] == "not_attempted"
    # and no rendered request-queue view was refreshed either.
    assert not (program_root / "requests" / "REQUESTS.md").exists()


def test_acquire_unpaywall_unreachable_transport_files_no_row(store, program_root):
    launch_id = bootstrap_launch(store)

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=_UnreachableTransport(), fetch_fn=_fake_fetch(b"unused"),
    )

    assert result.outcome == "unresolved"
    assert _source_count(store) == 0
    assert _legs_by_provider(result)["unpaywall"]["outcome"] == "transport_unreachable"


def test_acquire_unpaywall_404_is_conclusive_and_still_files_the_row(store, program_root):
    """The other half of the same fix: a provider that ANSWERED "no such
    record" is conclusive, and the row (and the sentence) still get filed."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(
        _unpaywall_doi_url("10.1000/example"), json_body=load_fixture("unpaywall_not_found.json"), status_code=404
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(b"unused"),
    )

    assert result.outcome == "queued"
    assert result.source["request_state"] == "wanted"
    assert result.source["license_tier"] == "unknown"
    assert result.source["acquisition_route"] == "user_delivered"
    assert "no legal open-access location found" in result.source["rights_notes"]
    assert _legs_by_provider(result)["unpaywall"]["outcome"] == "not_found"


def test_acquire_unpaywall_not_configured_says_so_and_claims_no_absence(store, program_root):
    """No `[litapi.unpaywall].mailto` -> the leg was never asked. The row is
    still filed (nothing failed), but it must NOT claim the paper has no legal
    open-access copy -- nobody looked."""
    launch_id = bootstrap_launch(store)

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(unpaywall_mailto=None), transport=FakeTransport(),
        fetch_fn=_fake_fetch(b"unused"),
    )

    assert result.outcome == "queued"
    notes = result.source["rights_notes"]
    assert "unpaywall=not_configured" in notes
    assert "no legal open-access location found" not in notes
    assert _legs_by_provider(result)["unpaywall"]["outcome"] == "not_configured"


def test_acquire_download_failure_is_a_leg_not_a_traceback(store, program_root):
    """FAILS BEFORE F18: fetch_fn's URLError propagated straight out of
    acquire() (cli/lit.py catches ValueError and the litapi/ingest/store
    errors, none of which an OSError is) and reached the user as a traceback."""
    import urllib.error

    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )

    def _boom(url: str) -> bytes:
        raise urllib.error.URLError("connection reset by peer")

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_boom,
    )

    assert result.outcome == "unresolved"
    assert _source_count(store) == 0
    leg = _legs_by_provider(result)["download"]
    assert leg["outcome"] == "download_failed"
    assert "connection reset by peer" in leg["error"]
    assert "Traceback" not in leg["error"]


def test_acquire_shares_one_arxiv_provider_across_metadata_and_oa_legs(store, program_root):
    """FAILS BEFORE F18: `_resolve_oa` built a SECOND ArxivProvider with its
    own RateLimiter, so the OA-leg arXiv request fired immediately after the
    metadata arXiv request and the 3-second spacing arXiv's ToU asks for was
    not kept inside one process. With one shared instance the second request
    waits."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    raw = {
        "litapi": {
            "openalex": {"min_interval_s": 0.0},
            "semanticscholar": {"min_interval_s": 0.0},
            "arxiv": {"min_interval_s": 3.0, "license_lookup": False},
            "unpaywall": {"min_interval_s": 0.0, "mailto": "me@example.org"},
        }
    }
    slept: list[float] = []
    clock = {"t": 0.0}

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    original_build = acquire_mod.build_default_providers

    def _build(*a, **kw):
        providers = original_build(*a, **kw)
        for provider in providers:
            provider._rate_limiter = RateLimiter(
                provider.config.min_interval_s, _time_fn=lambda: clock["t"], _sleep_fn=_sleep
            )
        return providers

    acquire_mod.build_default_providers = _build
    try:
        result = acquire_mod.acquire(
            store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
            litapi_config=load_litapi_config(raw), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
        )
    finally:
        acquire_mod.build_default_providers = original_build

    assert result.outcome == "acquired"
    arxiv_calls = [c for c in transport.calls if c["url"].startswith(ARXIV_BASE)]
    assert len(arxiv_calls) == 2  # metadata leg + OA leg
    assert slept == [3.0]  # exactly one paced wait between them


def test_arxiv_provider_sends_an_atom_accept_header(store, program_root):
    """A provider that sends no Accept header of its own gets
    ``Accept: application/json`` from UrllibTransport -- which an Atom
    endpoint is entitled to answer 406 to."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )

    acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    arxiv_calls = [c for c in transport.calls if c["url"].startswith(ARXIV_BASE)]
    assert arxiv_calls
    for call in arxiv_calls:
        assert call["headers"]["Accept"].startswith("application/atom+xml")


# ---------------------------------------------------------------------------
# contentless dedup: one `wanted` row per identifier, not one per attempt
# ---------------------------------------------------------------------------


def test_acquiring_the_same_paywalled_doi_twice_files_one_row(store, program_root):
    """FAILS BEFORE this lane: `register_source` dedups only on
    content_sha256, which a contentless request row does not have, so a second
    attempt at the same DOI filed a second `wanted` row."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(_unpaywall_doi_url("10.1000/example"), json_body=load_fixture("unpaywall_doi_not_oa.json"))

    first = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(b"unused"),
    )
    second = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(b"unused"),
    )

    assert first.outcome == "queued" and second.outcome == "queued"
    assert _source_count(store) == 1
    assert second.source["source_id"] == first.source["source_id"]
    assert second.source["dedup_of"] == first.source["source_id"]


def test_contentless_request_dedups_onto_a_row_that_already_has_content(store, program_root):
    launch_id = bootstrap_launch(store)
    from trialerror.ingest import pipeline

    with_content = pipeline.register_source(
        store, kind="paper", title="Example Paper", license_tier="open", acquisition_route="author_posted",
        registered_by_launch=launch_id, doi="10.1000/example", content_sha256="a" * 64,
    )
    contentless = pipeline.register_source(
        store, kind="paper", title="Example Paper", license_tier="unknown", acquisition_route="user_delivered",
        registered_by_launch=launch_id, doi="10.1000/example", request_state="wanted",
    )

    assert contentless["source_id"] == with_content["source_id"]
    assert contentless["dedup_of"] == with_content["source_id"]
    assert _source_count(store) == 1


def test_two_different_files_under_one_doi_still_register_twice(store, program_root):
    """The content branch is unchanged: a row WITH content is identified by
    its bytes, and two different files are two different sources."""
    launch_id = bootstrap_launch(store)
    from trialerror.ingest import pipeline

    first = pipeline.register_source(
        store, kind="paper", title="Example Paper", license_tier="open", acquisition_route="author_posted",
        registered_by_launch=launch_id, doi="10.1000/example", content_sha256="a" * 64,
    )
    second = pipeline.register_source(
        store, kind="paper", title="Example Paper (accepted manuscript)", license_tier="open",
        acquisition_route="author_posted", registered_by_launch=launch_id, doi="10.1000/example",
        content_sha256="b" * 64,
    )

    assert second["source_id"] != first["source_id"]
    assert second.get("dedup_of") is None
    assert _source_count(store) == 2


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


# ---------------------------------------------------------------------------
# F16 (lane FB-acq item 4): the document's own licence GRANT is recorded
# beside the route-derived tier. The tier itself is unchanged, on purpose.
# ---------------------------------------------------------------------------


def _oai_url(arxiv_id: str, *, base: str = "https://export.arxiv.org/oai2") -> str:
    return f"{base}?{urlencode({'verb': 'GetRecord', 'identifier': f'oai:arXiv.org:{arxiv_id}', 'metadataPrefix': 'arXiv'})}"


def _arxiv_transport(*, oai: TransportResponse | None = None) -> FakeTransport:
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"),
        TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_feed_hit.xml")),
    )
    if oai is not None:
        transport.add_response(_oai_url("2101.00001"), oai)
    return transport


def test_an_arxiv_acquisition_records_the_licence_the_oai_record_names(store, program_root):
    """FAILS BEFORE F16: the arXiv leg wrote license_tier='open' and the Atom
    parser read no licence at all, so a source registered as `open` might be
    CC-BY or might be arXiv's own non-exclusive distribution licence -- which
    grants the reader nothing -- and the store could not say which."""
    launch_id = bootstrap_launch(store)
    transport = _arxiv_transport(
        oai=TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_oai_license.xml"))
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.outcome == "acquired"
    assert result.source["license_grant"] == "arxiv-nonexclusive-distrib-1.0"
    assert result.source["license_grant_source"] == "arxiv_oai"
    # the UNCHANGED half, pinned on purpose: this item records the grant, it
    # does not change what the tier means.
    assert result.source["license_tier"] == "open"
    assert result.source["acquisition_route"] == "author_posted"
    assert "license_grant=arxiv-nonexclusive-distrib-1.0 (arxiv_oai)" in result.source["rights_notes"]


def test_an_oai_answer_without_a_licence_reads_as_none_reported(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = _arxiv_transport(
        oai=TransportResponse(status_code=200, json_body=None, text=load_text_fixture("arxiv_oai_no_license.xml"))
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.source["license_grant"] is None
    assert result.source["license_grant_source"] == "none_reported"
    assert result.source["license_tier"] == "open"


@pytest.mark.parametrize(
    "oai,expected_source",
    [
        (TransportResponse(status_code=429, json_body={"error": "rate limited"}), "lookup_failed:rate_limited"),
        (TransportResponse(status_code=500, json_body={"error": "boom"}), "lookup_failed:http_error"),
        (TransportResponse(status_code=200, json_body=None, text="<not-xml"), "lookup_failed:provider_error"),
        (None, "lookup_failed:provider_error"),  # no route registered at all
    ],
    ids=["oai-429", "oai-500", "oai-malformed-xml", "oai-no-route"],
)
def test_a_failed_grant_lookup_never_blocks_the_acquisition(store, program_root, oai, expected_source):
    launch_id = bootstrap_launch(store)

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=_arxiv_transport(oai=oai),
        fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.outcome == "acquired"  # the acquisition is untouched
    assert result.source["license_grant"] is None
    assert result.source["license_grant_source"] == expected_source
    # and the failure is NOT an OA leg -- the grant is provenance, not legality
    assert "download" not in _legs_by_provider(result)
    assert _legs_by_provider(result)["arxiv"]["outcome"] == "resolved"


def test_license_lookup_false_makes_no_oai_request_at_all(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = _arxiv_transport()

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(arxiv={"license_lookup": False}), transport=transport,
        fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.outcome == "acquired"
    assert result.source["license_grant_source"] == "not_attempted"
    assert not [c for c in transport.calls if "oai2" in c["url"]]


def test_an_atom_entry_that_carries_a_licence_needs_no_oai_request(store, program_root):
    """The cheap path: a licence already in hand costs nothing. Whether arXiv's
    Atom feed ever carries one could not be established offline -- the parse is
    defensive, and this fixture proves it works if it does."""
    launch_id = bootstrap_launch(store)
    feed = load_text_fixture("arxiv_feed_hit.xml").replace(
        "</entry>",
        '<link rel="license" href="https://creativecommons.org/licenses/by/4.0/"/>\n  </entry>',
    )
    transport = FakeTransport()
    transport.add_response(
        _arxiv_id_list_url("2101.00001"), TransportResponse(status_code=200, json_body=None, text=feed)
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, arxiv_id="2101.00001", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.source["license_grant"] == "cc-by-4.0"
    assert result.source["license_grant_source"] == "arxiv_atom"
    assert not [c for c in transport.calls if "oai2" in c["url"]]


def test_the_unpaywall_leg_keeps_the_licence_string_it_used_to_discard(store, program_root):
    """FAILS BEFORE F16: the Unpaywall leg read best_oa_location.license only
    to CHOOSE a tier, then threw the string away."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(_unpaywall_doi_url("10.1234/fixture.5678"), json_body=load_fixture("unpaywall_doi_hit.json"))

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1234/fixture.5678", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.source["license_grant"] == "cc-by"
    assert result.source["license_grant_source"] == "unpaywall_best_oa_location"
    assert result.source["license_tier"] == "open"  # as before


def test_an_unpaywall_repository_location_without_a_licence_reports_none(store, program_root):
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(
        _unpaywall_doi_url("10.1234/repo.0002"), json_body=load_fixture("unpaywall_doi_hit_repository.json")
    )

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1234/repo.0002", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(_pdf_bytes()),
    )

    assert result.source["license_grant"] is None
    assert result.source["license_grant_source"] == "none_reported"
    assert result.source["license_tier"] == "academic_oa"  # as before


def test_a_queued_request_row_records_no_grant_at_all(store, program_root):
    """NULL means "nobody read a grant for this row" -- which is exactly the
    truth for a request nobody has fulfilled yet."""
    launch_id = bootstrap_launch(store)
    transport = FakeTransport()
    transport.add_json(_unpaywall_doi_url("10.1000/example"), json_body=load_fixture("unpaywall_doi_not_oa.json"))

    result = acquire_mod.acquire(
        store, program_root=program_root, doi="10.1000/example", created_by_launch=launch_id,
        litapi_config=_litapi_config(), transport=transport, fetch_fn=_fake_fetch(b"unused"),
    )

    assert result.source["license_grant"] is None
    assert result.source["license_grant_source"] is None
