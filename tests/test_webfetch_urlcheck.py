"""Syntactic URL policy — the B-hostile table of the lane-a design §4/§6.

The table below is the design's own list, turned into rows. Its point is not
that each individual string is refused (any one of them could be caught by
accident) but that each is refused *with the reason the design names*: the
reason is what the audit trail records, what the doctor check groups on, and
what tells an operator reading a WARN whether something tried to reach the
metadata service or merely mistyped a hostname.

Every row here runs without a network stack. That is the property being
protected: a refusal from this module costs nothing and tells an attacker
nothing, because nothing left the process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.webfetch import REASONS, WebFetchRefused
from trialerror.webfetch.urlcheck import (
    DEFAULT_MAX_QUERY_LEN,
    DEFAULT_MAX_URL_LEN,
    normalize,
    peek_host,
    same_host,
)

# (url, expected_reason, why-this-row-exists)
HOSTILE_URLS: list[tuple[str, str, str]] = [
    # --- IP literals in every encoding the design enumerates -------------
    ("https://127.0.0.1/", "ip_literal", "plain loopback"),
    ("https://0x7f000001/", "ip_literal", "hex-packed loopback"),
    ("https://2130706433/", "ip_literal", "decimal-packed loopback"),
    ("https://127.1/", "ip_literal", "short-form loopback"),
    ("https://0177.0.0.1/", "ip_literal", "octal loopback"),
    ("https://[::1]/", "ip_literal", "IPv6 loopback"),
    ("https://[::ffff:127.0.0.1]/", "ip_literal", "IPv4-mapped IPv6 loopback"),
    ("https://169.254.169.254/latest/meta-data/", "ip_literal", "cloud metadata service"),
    ("https://192.168.0.1/", "ip_literal", "a LAN router"),
    ("https://172.17.0.1/", "ip_literal", "the docker bridge gateway"),
    ("https://10.0.0.1/", "ip_literal", "RFC 1918"),
    ("https://8.8.8.8/", "ip_literal", "a *public* literal is refused too — names only"),
    # --- reserved names --------------------------------------------------
    ("https://localhost/", "host_reserved_suffix", "loopback by name"),
    ("https://api.localhost/", "host_reserved_suffix", "loopback suffix"),
    ("https://nas.fritz.box/", "host_reserved_suffix", "a router-assigned LAN name"),
    ("https://printer.local/", "host_reserved_suffix", "mDNS"),
    ("https://vault.internal/", "host_reserved_suffix", "internal TLD"),
    ("https://nas.lan/", "host_reserved_suffix", "LAN TLD"),
    ("https://box.home.arpa/", "host_reserved_suffix", "RFC 8375 home network"),
    ("https://1.0.0.127.in-addr.arpa/", "host_reserved_suffix", "reverse DNS zone"),
    ("https://research-container/", "host_syntax", "a sibling container's compose name"),
    # --- schemes ---------------------------------------------------------
    ("file:///etc/passwd", "scheme_not_allowed", "local file read"),
    ("gopher://example.com/", "scheme_not_allowed", "gopher smuggling"),
    ("javascript:alert(1)", "scheme_not_allowed", "not a transport at all"),
    ("ftp://example.com/x", "scheme_not_allowed", "unsupported transport"),
    ("data:text/html,hi", "scheme_not_allowed", "inline payload"),
    ("//example.com/x", "scheme_not_allowed", "scheme-relative URL"),
    ("http://example.com/", "scheme_not_allowed", "plain http without the host's flag"),
    # --- userinfo --------------------------------------------------------
    ("https://github.com@evil.example/", "userinfo_present", "host-looking userinfo"),
    ("https://user:pass@example.com/", "userinfo_present", "credentials in the URL"),
    # --- ports -----------------------------------------------------------
    ("https://example.com:8850/", "port_not_allowed", "the dashboard's write API"),
    ("https://example.com:22/", "port_not_allowed", "ssh"),
    ("https://example.com:6379/", "port_not_allowed", "redis"),
    ("https://example.com:80/", "port_not_allowed", "https on 80 is not a real combination"),
    ("https://example.com:notaport/", "port_not_allowed", "unparseable authority port"),
    # --- shape -----------------------------------------------------------
    ("https://exa mple.com/", "host_syntax", "space in the authority"),
    ("https://example.com/\x00", "host_syntax", "NUL byte"),
    ("https://example.com/\nHeader: x", "host_syntax", "request-splitting newline"),
    ("https://exämple.com/", "host_syntax", "non-ASCII host, no silent IDNA"),
    ("https://.example.com/", "host_syntax", "empty leading label"),
    ("https://example..com/", "host_syntax", "empty inner label"),
    ("https://-example.com/", "host_syntax", "label starting with a hyphen"),
    ("https://" + "a" * 250 + ".example.com/", "host_syntax", "host over 253 characters"),
    ("https:///path", "host_syntax", "no host at all"),
    (
        "https://[::1/",
        "host_syntax",
        "unbalanced bracket: urlsplit RAISES on this, and the ValueError used "
        "to escape the whole refusal contract and kill the sidecar loop",
    ),
    (
        "http://[::ffff:169.254.169.254/latest/meta-data/",
        "host_syntax",
        "the same crash shape aimed at the metadata service",
    ),
    # --- size ------------------------------------------------------------
    (
        "https://example.com/?q=" + "a" * (DEFAULT_MAX_QUERY_LEN + 10),
        "query_too_long",
        "an oversized query is the widest field an injected agent could write",
    ),
    (
        "https://example.com/" + "a" * (DEFAULT_MAX_URL_LEN + 10),
        "url_too_long",
        "an oversized URL is an exfiltration channel by volume",
    ),
]


@pytest.mark.parametrize(
    "url,expected_reason,why", HOSTILE_URLS, ids=[row[0][:60] for row in HOSTILE_URLS]
)
def test_hostile_url_refused_with_the_named_reason(url: str, expected_reason: str, why: str) -> None:
    with pytest.raises(WebFetchRefused) as caught:
        normalize(url)
    assert caught.value.reason == expected_reason, f"{why}: got {caught.value.reason}"
    assert caught.value.reason in REASONS


def test_every_hostile_row_has_a_distinct_url() -> None:
    urls = [row[0] for row in HOSTILE_URLS]
    assert len(urls) == len(set(urls))


def test_the_table_covers_every_pre_dns_reason() -> None:
    """The pre-DNS reasons are exactly the ones this module can produce, and
    the table exercises all of them — a reason with no row is a rule nobody
    is checking."""
    covered = {row[1] for row in HOSTILE_URLS}
    assert covered == {
        "scheme_not_allowed",
        "userinfo_present",
        "port_not_allowed",
        "ip_literal",
        "host_syntax",
        "host_reserved_suffix",
        "url_too_long",
        "query_too_long",
    }


# --------------------------------------------------------------------------
# what is allowed, and what it normalizes to
# --------------------------------------------------------------------------


def test_ordinary_https_url_survives_intact() -> None:
    result = normalize("https://diataxis.fr/tutorials/")
    assert result.url == "https://diataxis.fr/tutorials/"
    assert result.url_norm == result.url
    assert result.host == "diataxis.fr"
    assert result.port == 443
    assert result.origin == "https://diataxis.fr"
    assert result.request_target == "/tutorials/"
    assert result.query_stripped is False


def test_http_is_allowed_only_with_the_hosts_flag() -> None:
    with pytest.raises(WebFetchRefused) as caught:
        normalize("http://example.com/x")
    assert caught.value.reason == "scheme_not_allowed"
    assert normalize("http://example.com/x", allow_http=True).port == 80


def test_url_norm_lowercases_drops_fragment_default_port_and_trackers() -> None:
    result = normalize(
        "https://Example.COM:443/A/./b/../c?utm_source=news&fbclid=1&gclid=2&q=1#section"
    )
    assert result.url == "https://example.com/A/c?utm_source=news&fbclid=1&gclid=2&q=1"
    assert result.url_norm == "https://example.com/A/c?q=1"


def test_percent_escapes_of_unreserved_bytes_are_decoded_once() -> None:
    """``%7E`` and ``~`` must be one dedup key, or a re-fetch of the same page
    becomes a second source row."""
    assert normalize("https://example.com/%7Euser/").url_norm == "https://example.com/~user/"
    assert normalize("https://example.com/~user/").url_norm == "https://example.com/~user/"


def test_an_encoded_slash_stays_encoded_and_never_becomes_a_separator() -> None:
    """The traversal trick: if ``%2F`` were decoded to a separator, dot-segment
    removal would then act on segments the origin server never had."""
    result = normalize("https://example.com/a%2F..%2F..%2Fetc/passwd")
    assert result.path == "/a%2F..%2F..%2Fetc/passwd"


def test_dot_segments_cannot_climb_above_the_root() -> None:
    assert normalize("https://example.com/../../etc/passwd").path == "/etc/passwd"
    assert normalize("https://example.com/../..").path == "/"


def test_encoded_dot_segments_are_removed_too() -> None:
    assert normalize("https://example.com/a/%2E%2E/b").path == "/b"


def test_trailing_slash_is_preserved_across_normalization() -> None:
    assert normalize("https://example.com/docs/").path == "/docs/"
    assert normalize("https://example.com/docs/.").path == "/docs/"


def test_empty_path_becomes_root() -> None:
    assert normalize("https://example.com").path == "/"


def test_keep_query_false_strips_the_query_and_says_so() -> None:
    result = normalize("https://example.com/search?q=secret", keep_query=False)
    assert result.query == ""
    assert result.query_stripped is True
    assert result.url == "https://example.com/search"


def test_keep_query_false_on_a_url_with_no_query_is_not_a_strip() -> None:
    assert normalize("https://example.com/x", keep_query=False).query_stripped is False


def test_query_limit_is_checked_before_the_url_limit() -> None:
    """An oversized query must report ``query_too_long``: the two caps carry
    different meanings for a reader of the audit log, and the more specific
    one has to win."""
    url = "https://example.com/?q=" + "a" * 3000
    with pytest.raises(WebFetchRefused) as caught:
        normalize(url)
    assert caught.value.reason == "query_too_long"


def test_caps_are_arguments_not_constants() -> None:
    with pytest.raises(WebFetchRefused) as caught:
        normalize("https://example.com/aaaa", max_url_len=10)
    assert caught.value.reason == "url_too_long"


def test_trailing_root_dot_is_dropped_from_the_host() -> None:
    assert normalize("https://example.com./x").host == "example.com"


def test_peek_host_accepts_http_because_the_flag_is_not_known_yet() -> None:
    """``peek_host`` exists precisely because whether plain http is allowed is
    a per-host flag, and the flag cannot be read before the host is."""
    assert peek_host("http://example.com/x") == "example.com"
    assert peek_host("https://Example.com/x") == "example.com"


def test_peek_host_still_refuses_a_literal() -> None:
    with pytest.raises(WebFetchRefused) as caught:
        peek_host("http://127.0.0.1/x")
    assert caught.value.reason == "ip_literal"


def test_same_host_gates_conditional_header_replay() -> None:
    a = normalize("https://example.com/one")
    b = normalize("https://example.com/two")
    c = normalize("https://other.example/three")
    assert same_host(a, b) is True
    assert same_host(a, c) is False


def test_refusal_carries_a_reason_from_the_closed_vocabulary() -> None:
    with pytest.raises(WebFetchRefused) as caught:
        normalize("https://127.0.0.1/")
    refusal = caught.value
    assert refusal.reason in REASONS
    assert refusal.to_dict()["reason"] == "ip_literal"
    assert "127.0.0.1" in refusal.detail


# --------------------------------------------------------------------------
# The same table, on disk: tests/fixtures/webfetch/hostile_urls.jsonl
#
# Design section 6 asks for the B-hostile table as a FILE as well as as code, and
# these three tests are what stop that file becoming decoration. It covers more
# than this module does -- DNS answers, redirects, caps, manifests, robots, git,
# each row naming the test that actually proves it -- so what is checked here is
# the part this module owns (the pre-DNS rows really are refused with the reason
# the file claims) plus two properties of the file as a whole: every reason it
# names is real, and every reason in the closed vocabulary has a row.
# --------------------------------------------------------------------------

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "webfetch" / "hostile_urls.jsonl"


def _fixture_rows() -> list[dict]:
    rows = [
        json.loads(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row for row in rows if row["layer"] != "_meta"]


def test_the_hostile_fixture_names_only_real_reasons() -> None:
    """A reason outside the closed vocabulary would be a row nothing can ever
    satisfy -- an assertion that reads as coverage and is not."""
    rows = _fixture_rows()
    assert rows, "the hostile fixture is empty"
    bogus = sorted({row["expected_reason"] for row in rows} - REASONS)
    assert not bogus, f"hostile_urls.jsonl names reasons that do not exist: {bogus}"
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "duplicate ids in hostile_urls.jsonl"


def test_the_hostile_fixture_covers_every_refusal_reason() -> None:
    """One row per reason, minimum. A reason with no row is a rule nobody has
    written down a way to trip -- which is how a rule quietly stops working."""
    covered = {row["expected_reason"] for row in _fixture_rows()}
    missing = sorted(REASONS - covered)
    assert not missing, (
        f"hostile_urls.jsonl has no row for {missing}. Add one -- do not relax this test."
    )


@pytest.mark.parametrize(
    "row",
    [r for r in _fixture_rows() if r["layer"] == "urlcheck"],
    ids=[r["id"] for r in _fixture_rows() if r["layer"] == "urlcheck"],
)
def test_every_urlcheck_row_of_the_fixture_is_refused_as_the_file_claims(row: dict) -> None:
    with pytest.raises(WebFetchRefused) as caught:
        normalize(row["input"])
    assert caught.value.reason == row["expected_reason"], row["why"]


def test_the_fixture_and_the_inline_table_agree() -> None:
    """Two copies of the same claim have to be one claim. The inline table is
    what runs on every commit; the file is what the acceptance runbook reads.
    If they ever disagree, one of them is lying to somebody."""
    inline = {url: reason for url, reason, _why in HOSTILE_URLS}
    for row in _fixture_rows():
        if row["layer"] != "urlcheck":
            continue
        if row["input"] in inline:
            assert inline[row["input"]] == row["expected_reason"], row["id"]
