"""robots.txt — asked once per host per day, honoured, never argued with.

Design §4, threat T4. Three rules that between them decide every fetch:

* **The parser never touches the network.** ``urllib.robotparser`` is fed
  *text* that this module fetched through exactly the same policy, netguard
  and caps as any other page. ``RobotFileParser.read()`` — which would open
  its own ``urllib`` connection, bypassing the allowlist, the pinned-IP
  connect and the byte caps in one call — is never used.
* **Unavailable is not permission.** ``4xx`` means "there are no rules here",
  which is the web's long-standing convention and is treated as *allow*.
  ``5xx``, a timeout, or a transport refusal means the host has rules and we
  could not read them; that is ``robots_unavailable`` and the fetch does not
  happen. Re-asked in an hour rather than a day, because it is a condition
  that usually clears.
* **An override is an operator's signature, not a flag.** A manifest may name
  a ruling id, but the ruling only counts if the *host-side*
  ``robots-overrides.conf`` — which no process in the research container can
  write — carries the same id for the same URL. A manifest that names a
  ruling the file does not have is recorded as an attempted override and
  otherwise ignored.

``Crawl-delay`` is honoured up to ``crawl_delay_cap_s`` (30 s by default):
past that a site is effectively asking not to be read by a machine at all,
and the answer to that is the operator's manual-delivery path, not a
half-hour sleep in a container.

``git`` fetches report ``n/a``: a smart-HTTP clone is not crawling, and
robots.txt has never governed it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from urllib.robotparser import RobotFileParser

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.urlcheck import NormalizedUrl, normalize

__all__ = [
    "USER_AGENT_TOKEN",
    "RobotsDocument",
    "RobotsVerdict",
    "RobotsCache",
]

#: The token this fetcher answers to in a ``User-agent:`` group. The wildcard
#: group is the fallback, and ``RobotFileParser`` already implements that
#: precedence — most specific matching group first, ``*`` otherwise — so one
#: lookup covers both tokens the design names.
USER_AGENT_TOKEN = "trialerror-webfetch"

_ALLOW = "allow"
_DISALLOW = "disallow"
_UNAVAILABLE = "unavailable"
_NA = "n/a"


@dataclass(frozen=True)
class RobotsDocument:
    """What a robots.txt fetch produced. ``text`` is empty for a 4xx."""

    status: int | None
    text: str


@dataclass(frozen=True)
class RobotsVerdict:
    """The answer for one URL, plus everything the provenance record wants."""

    verdict: str
    fetched: bool
    crawl_delay_s: float = 0.0
    source: str = ""
    override_requested: str | None = None
    override_honored: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict in (_ALLOW, _NA)

    def as_result_block(self) -> dict:
        """The ``robots`` object of ``result.json`` (design §2.3)."""
        return {
            "fetched": self.fetched,
            "verdict": self.verdict,
            "crawl_delay_s": self.crawl_delay_s,
        }

    def raise_if_refused(self, host: str) -> None:
        if self.verdict == _DISALLOW:
            raise WebFetchRefused(
                "robots_disallow",
                f"{host}/robots.txt disallows this path for {USER_AGENT_TOKEN}",
                host=host,
            )
        if self.verdict == _UNAVAILABLE:
            raise WebFetchRefused(
                "robots_unavailable",
                f"{host}/robots.txt could not be read; not treating that as permission",
                host=host,
            )


@dataclass
class _Entry:
    parser: RobotFileParser | None
    verdict_when_no_parser: str
    fetched: bool
    expires_at: float


class RobotsCache:
    """One robots.txt per origin, cached for ``ttl_s``.

    ``fetch_fn`` is handed a fully normalized robots.txt URL and returns a
    :class:`RobotsDocument`, or raises
    :class:`~trialerror.webfetch.WebFetchRefused` for a transport problem.
    The sidecar binds it to the same :class:`~trialerror.webfetch.fetcher.
    Fetcher` that fetches pages, so robots.txt is subject to the identical
    address policy and byte caps — there is no second, laxer code path to
    the network.
    """

    def __init__(
        self,
        fetch_fn: Callable[[NormalizedUrl], RobotsDocument],
        *,
        ttl_s: float = 86400.0,
        unavailable_ttl_s: float = 3600.0,
        crawl_delay_cap_s: float = 30.0,
        user_agent_token: str = USER_AGENT_TOKEN,
        _time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._fetch_fn = fetch_fn
        self.ttl_s = float(ttl_s)
        self.unavailable_ttl_s = float(unavailable_ttl_s)
        self.crawl_delay_cap_s = float(crawl_delay_cap_s)
        self.user_agent_token = user_agent_token
        self._time_fn = _time_fn or time.monotonic
        self._entries: dict[str, _Entry] = {}

    # -- public ----------------------------------------------------------
    def verdict_for(
        self,
        url: NormalizedUrl,
        *,
        kind: str = "page",
        override_requested: str | None = None,
        approved_ruling: str | None = None,
    ) -> RobotsVerdict:
        """Decide whether ``url`` may be fetched.

        ``approved_ruling`` is what the host-side overrides file says for
        this exact ``url_norm`` (``None`` if it says nothing);
        ``override_requested`` is what the manifest asked for. They must be
        equal and non-empty for the override to count.
        """
        if kind == "git":
            return RobotsVerdict(verdict=_NA, fetched=False, source="kind=git")

        if override_requested is not None:
            if approved_ruling is not None and approved_ruling == override_requested:
                return RobotsVerdict(
                    verdict=_ALLOW,
                    fetched=False,
                    source=f"robots-overrides.conf:{approved_ruling}",
                    override_requested=override_requested,
                    override_honored=True,
                )
            # Recorded, not honoured: the manifest side cannot write the
            # overrides file, so a ruling it names that the file does not
            # carry is either stale or an attempt. Either way the normal
            # robots verdict decides.
            verdict = self._verdict_from_cache(url)
            return RobotsVerdict(
                verdict=verdict.verdict,
                fetched=verdict.fetched,
                crawl_delay_s=verdict.crawl_delay_s,
                source=verdict.source,
                override_requested=override_requested,
                override_honored=False,
            )

        return self._verdict_from_cache(url)

    def invalidate(self, origin: str | None = None) -> None:
        if origin is None:
            self._entries.clear()
        else:
            self._entries.pop(origin, None)

    # -- internals -------------------------------------------------------
    def _verdict_from_cache(self, url: NormalizedUrl) -> RobotsVerdict:
        entry, source = self._entry_for(url)
        if entry.parser is None:
            return RobotsVerdict(
                verdict=entry.verdict_when_no_parser,
                fetched=entry.fetched,
                source=source,
            )
        allowed = entry.parser.can_fetch(self.user_agent_token, url.url)
        delay = self._crawl_delay(entry.parser)
        return RobotsVerdict(
            verdict=_ALLOW if allowed else _DISALLOW,
            fetched=True,
            crawl_delay_s=delay,
            source=source,
        )

    def _entry_for(self, url: NormalizedUrl) -> tuple[_Entry, str]:
        origin = url.origin
        cached = self._entries.get(origin)
        nowish = self._time_fn()
        if cached is not None and cached.expires_at > nowish:
            return cached, "cache"

        robots_url = normalize(
            f"{origin}/robots.txt",
            allow_http=url.scheme == "http",
            keep_query=False,
        )
        try:
            document = self._fetch_fn(robots_url)
        except WebFetchRefused:
            entry = _Entry(
                parser=None,
                verdict_when_no_parser=_UNAVAILABLE,
                fetched=False,
                expires_at=nowish + self.unavailable_ttl_s,
            )
            self._entries[origin] = entry
            return entry, "network"

        status = document.status
        if status is not None and 200 <= status < 300:
            parser = RobotFileParser()
            parser.parse(document.text.splitlines())
            entry = _Entry(
                parser=parser,
                verdict_when_no_parser=_ALLOW,
                fetched=True,
                expires_at=nowish + self.ttl_s,
            )
        elif status is not None and 400 <= status < 500:
            # No robots.txt is the same as an empty one.
            entry = _Entry(
                parser=None,
                verdict_when_no_parser=_ALLOW,
                fetched=True,
                expires_at=nowish + self.ttl_s,
            )
        else:
            entry = _Entry(
                parser=None,
                verdict_when_no_parser=_UNAVAILABLE,
                fetched=False,
                expires_at=nowish + self.unavailable_ttl_s,
            )
        self._entries[origin] = entry
        return entry, "network"

    def _crawl_delay(self, parser: RobotFileParser) -> float:
        try:
            raw = parser.crawl_delay(self.user_agent_token)
        except Exception:  # pragma: no cover - robotparser is forgiving
            return 0.0
        if raw is None:
            return 0.0
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if delay <= 0:
            return 0.0
        return min(delay, self.crawl_delay_cap_s)
