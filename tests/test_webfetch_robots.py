"""robots.txt — the politeness gate, and the one place "we could not read the
rules" must not quietly become "there are no rules".

Every robots document here is handed to the cache as *text*. That is the
point: ``RobotFileParser.read()`` would open its own connection, bypassing the
allowlist, the pinned-IP connect and the byte caps in a single call, so the
cache never has a network of its own — it is given a fetch function, and these
tests count exactly how often it uses it.
"""

from __future__ import annotations

import pytest

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.robots import USER_AGENT_TOKEN, RobotsCache, RobotsDocument
from trialerror.webfetch.urlcheck import NormalizedUrl, normalize

DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ALLOW_ALL = "User-agent: *\nDisallow:\n"


class RecordingFetch:
    """Serves canned robots documents and counts the questions."""

    def __init__(self, documents: dict[str, RobotsDocument] | None = None) -> None:
        self.documents = documents or {}
        self.calls: list[str] = []
        self.raises: Exception | None = None

    def __call__(self, robots_url: NormalizedUrl) -> RobotsDocument:
        self.calls.append(robots_url.url)
        if self.raises is not None:
            raise self.raises
        return self.documents.get(robots_url.url, RobotsDocument(status=404, text=""))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def doc(text: str, status: int = 200) -> RobotsDocument:
    return RobotsDocument(status=status, text=text)


def cache_for(
    text: str | None = None, *, status: int = 200, clock: FakeClock | None = None, **kwargs
) -> tuple[RobotsCache, RecordingFetch]:
    documents = {}
    if text is not None:
        documents["https://example.com/robots.txt"] = doc(text, status)
    fetch = RecordingFetch(documents)
    return RobotsCache(fetch, _time_fn=clock or FakeClock(), **kwargs), fetch


def url(path: str = "/article") -> NormalizedUrl:
    return normalize(f"https://example.com{path}")


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------


def test_a_disallowing_site_is_refused() -> None:
    cache, fetch = cache_for(DISALLOW_ALL)
    verdict = cache.verdict_for(url())
    assert verdict.verdict == "disallow"
    assert verdict.fetched is True
    assert verdict.allowed is False
    assert fetch.calls == ["https://example.com/robots.txt"]


def test_an_allowing_site_passes() -> None:
    cache, _ = cache_for(ALLOW_ALL)
    assert cache.verdict_for(url()).verdict == "allow"


def test_a_rule_that_matches_only_another_path_does_not_apply() -> None:
    cache, _ = cache_for("User-agent: *\nDisallow: /private/\n")
    assert cache.verdict_for(url("/public/x")).verdict == "allow"
    assert cache.verdict_for(url("/private/x")).verdict == "disallow"


def test_our_own_user_agent_group_wins_over_the_wildcard() -> None:
    cache, _ = cache_for(
        f"User-agent: *\nDisallow: /\n\nUser-agent: {USER_AGENT_TOKEN}\nDisallow: /admin\n"
    )
    assert cache.verdict_for(url("/article")).verdict == "allow"
    assert cache.verdict_for(url("/admin")).verdict == "disallow"


def test_a_group_naming_us_specifically_can_shut_us_out() -> None:
    cache, _ = cache_for(f"User-agent: {USER_AGENT_TOKEN}\nDisallow: /\n")
    assert cache.verdict_for(url()).verdict == "disallow"


def test_a_missing_robots_file_means_there_are_no_rules() -> None:
    cache, fetch = cache_for(None)  # RecordingFetch answers 404
    verdict = cache.verdict_for(url())
    assert verdict.verdict == "allow"
    assert verdict.fetched is True
    assert fetch.calls == ["https://example.com/robots.txt"]


def test_a_server_error_is_not_permission() -> None:
    """The host has rules and we could not read them. Proceeding anyway would
    be the one interpretation nobody could defend afterwards."""
    cache, _ = cache_for("", status=503)
    verdict = cache.verdict_for(url())
    assert verdict.verdict == "unavailable"
    assert verdict.fetched is False


def test_a_transport_refusal_is_also_unavailable() -> None:
    cache, fetch = cache_for(ALLOW_ALL)
    fetch.raises = WebFetchRefused("timeout", "robots.txt timed out")
    assert cache.verdict_for(url()).verdict == "unavailable"


def test_git_never_asks_about_robots() -> None:
    """A smart-HTTP clone is not crawling, and robots.txt has never governed
    it."""
    cache, fetch = cache_for(DISALLOW_ALL)
    verdict = cache.verdict_for(url(), kind="git")
    assert verdict.verdict == "n/a"
    assert verdict.allowed is True
    assert fetch.calls == []


# --------------------------------------------------------------------------
# refusal reasons
# --------------------------------------------------------------------------


def test_disallow_raises_the_named_reason() -> None:
    cache, _ = cache_for(DISALLOW_ALL)
    with pytest.raises(WebFetchRefused) as caught:
        cache.verdict_for(url()).raise_if_refused("example.com")
    assert caught.value.reason == "robots_disallow"
    assert caught.value.context["host"] == "example.com"


def test_unavailable_raises_its_own_reason() -> None:
    cache, _ = cache_for("", status=500)
    with pytest.raises(WebFetchRefused) as caught:
        cache.verdict_for(url()).raise_if_refused("example.com")
    assert caught.value.reason == "robots_unavailable"


def test_an_allowed_verdict_raises_nothing() -> None:
    cache, _ = cache_for(ALLOW_ALL)
    cache.verdict_for(url()).raise_if_refused("example.com")


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------


def test_one_fetch_serves_many_urls_on_one_origin() -> None:
    cache, fetch = cache_for(ALLOW_ALL)
    for index in range(5):
        cache.verdict_for(url(f"/page-{index}"))
    assert len(fetch.calls) == 1


def test_the_cache_expires_after_its_ttl() -> None:
    clock = FakeClock()
    cache, fetch = cache_for(ALLOW_ALL, clock=clock, ttl_s=86400.0)
    cache.verdict_for(url())
    clock.advance(86399)
    cache.verdict_for(url())
    assert len(fetch.calls) == 1
    clock.advance(2)
    cache.verdict_for(url())
    assert len(fetch.calls) == 2


def test_an_unavailable_answer_is_retried_sooner_than_a_good_one() -> None:
    """Unavailability usually clears; a day of refusing a host because of one
    500 would be its own outage."""
    clock = FakeClock()
    cache, fetch = cache_for("", status=500, clock=clock, unavailable_ttl_s=3600.0)
    cache.verdict_for(url())
    clock.advance(3599)
    cache.verdict_for(url())
    assert len(fetch.calls) == 1
    clock.advance(2)
    cache.verdict_for(url())
    assert len(fetch.calls) == 2


def test_the_cache_is_keyed_on_the_origin_not_the_host() -> None:
    fetch = RecordingFetch(
        {
            "https://example.com/robots.txt": doc(ALLOW_ALL),
            "http://example.com/robots.txt": doc(DISALLOW_ALL),
        }
    )
    cache = RobotsCache(fetch, _time_fn=FakeClock())
    assert cache.verdict_for(normalize("https://example.com/x")).verdict == "allow"
    assert (
        cache.verdict_for(normalize("http://example.com/x", allow_http=True)).verdict
        == "disallow"
    )


def test_invalidate_forces_a_re_read() -> None:
    cache, fetch = cache_for(ALLOW_ALL)
    cache.verdict_for(url())
    cache.invalidate("https://example.com")
    cache.verdict_for(url())
    assert len(fetch.calls) == 2

    cache.invalidate()
    cache.verdict_for(url())
    assert len(fetch.calls) == 3


def test_the_robots_url_is_derived_from_the_origin() -> None:
    fetch = RecordingFetch()
    cache = RobotsCache(fetch, _time_fn=FakeClock())
    cache.verdict_for(normalize("https://docs.example.org/deep/page?x=1"))
    assert fetch.calls == ["https://docs.example.org/robots.txt"]


# --------------------------------------------------------------------------
# crawl delay
# --------------------------------------------------------------------------


def test_a_crawl_delay_is_honoured() -> None:
    cache, _ = cache_for("User-agent: *\nCrawl-delay: 10\nDisallow:\n")
    assert cache.verdict_for(url()).crawl_delay_s == 10.0


def test_a_crawl_delay_is_capped() -> None:
    """Past the cap a site is asking not to be read by a machine at all, and
    the answer to that is the operator's manual-delivery path, not a
    half-hour sleep in a container."""
    cache, _ = cache_for(
        "User-agent: *\nCrawl-delay: 3600\nDisallow:\n", crawl_delay_cap_s=30.0
    )
    assert cache.verdict_for(url()).crawl_delay_s == 30.0


def test_a_nonsense_crawl_delay_is_ignored() -> None:
    cache, _ = cache_for("User-agent: *\nCrawl-delay: soon\nDisallow:\n")
    assert cache.verdict_for(url()).crawl_delay_s == 0.0


def test_no_crawl_delay_means_zero() -> None:
    cache, _ = cache_for(ALLOW_ALL)
    assert cache.verdict_for(url()).crawl_delay_s == 0.0


# --------------------------------------------------------------------------
# overrides
# --------------------------------------------------------------------------


def test_an_override_counts_only_when_the_host_side_file_agrees() -> None:
    cache, fetch = cache_for(DISALLOW_ALL)
    target = url()
    verdict = cache.verdict_for(
        target, override_requested="C-0069", approved_ruling="C-0069"
    )
    assert verdict.verdict == "allow"
    assert verdict.override_honored is True
    assert "C-0069" in verdict.source
    assert fetch.calls == [], "an approved override does not need the file at all"


def test_an_override_the_file_does_not_carry_is_recorded_and_ignored() -> None:
    """The manifest side cannot write the overrides file, so a ruling it
    names that the file does not have is either stale or an attempt."""
    cache, _ = cache_for(DISALLOW_ALL)
    verdict = cache.verdict_for(url(), override_requested="C-9999", approved_ruling=None)
    assert verdict.verdict == "disallow"
    assert verdict.override_requested == "C-9999"
    assert verdict.override_honored is False


def test_a_mismatched_ruling_id_is_not_an_override() -> None:
    cache, _ = cache_for(DISALLOW_ALL)
    verdict = cache.verdict_for(
        url(), override_requested="C-0001", approved_ruling="C-0069"
    )
    assert verdict.verdict == "disallow"
    assert verdict.override_honored is False


# --------------------------------------------------------------------------
# the provenance block
# --------------------------------------------------------------------------


def test_the_result_block_matches_the_schema_field_names() -> None:
    cache, _ = cache_for("User-agent: *\nCrawl-delay: 5\nDisallow:\n")
    block = cache.verdict_for(url()).as_result_block()
    assert block == {"fetched": True, "verdict": "allow", "crawl_delay_s": 5.0}


def test_the_n_a_block_says_nothing_was_fetched() -> None:
    cache, _ = cache_for(ALLOW_ALL)
    block = cache.verdict_for(url(), kind="git").as_result_block()
    assert block == {"fetched": False, "verdict": "n/a", "crawl_delay_s": 0.0}
