"""**EXPERIMENTAL / FRAGILE** -- a browser-equivalent client for
arxivxplorer.com's search flow, built and gated under C-0069 (the origin-project
corrections ledger, ``research/ops/corrections.md`` last entry as of this
build): "for a PUBLIC, FREE, NO-ACCOUNT web service with no published ToS,
using the site the way a browser user does -- including an agent driving
the same request flow, and including capturing that browser-equivalent
flow as reusable code -- is an ACCEPTED use." This module is that capture,
built to C-0069's binding guardrails, not a general-purpose scraper.

**How the request shape was recovered (passive, browser-equivalent
inspection, not guesswork):** a live browser session (the same Claude
Browser pane tooling any Claude Code session can drive) loaded
``https://arxivxplorer.com/``, typed a real search query, applied the
Tags/Year filters through the site's own UI, and read back
``performance.getEntriesByType('resource')`` -- i.e. the exact network
calls the SITE'S OWN JAVASCRIPT made in response to normal UI interaction,
never a raw HAR-capture-and-replay tool, never an attempt to find
undocumented internals beyond what one interactive search actually
triggers. The frontend (a static Gatsby build served from
``arxivxplorer.com``) turned out to call a SEPARATE API host,
``https://search.arxivxplorer.com``, directly from the browser via
``fetch()`` -- this is the one and only real search request; the
``page-data/*.json`` calls visible on the main host are Gatsby's own
client-side-routing prefetch machinery and carry no search results at all.

**Recovered request shape (verified live, this build, 2026-08-29):**

- ``GET https://search.arxivxplorer.com/?q=<url-encoded text>``
  (repeat ``&cats=<category>`` once per selected tag, ``&year=<YYYY>`` for
  the year filter -- both additive to ``q=``, both optional, params appear
  in this fixed order in the real UI's own requests).
- No auth header, no cookie, no API key of any kind -- an anonymous GET.
- Response: ``Content-Type: application/json``, a bare JSON array (NOT an
  object with a results-wrapper), each item shaped
  ``{"id", "journal", "title", "abstract", "authors" (one COMMA-JOINED
  string, not a list), "date" (ISO-8601), "categories" (list[str]),
  "short_author", "score" (float, cosine-similarity-shaped)}``.
- CORS is open (``arxivxplorer.com`` itself calls ``search.arxivxplorer.com``
  cross-origin via browser ``fetch()`` and succeeds) -- consistent with a
  host meant to be called by a browser from the public frontend's own
  origin, not a same-origin-only internal endpoint.
- The vector-interpolation syntax (``+ 1706.03762 + 1712.01815 -
  1409.0473``, per ``https://arxivxplorer.com/guide/``) is passed through
  ``q=`` completely unmodified -- it is parsed SERVER-SIDE, not something
  this client needs to special-case; a caller who wants that behavior just
  passes that exact string as ``query``.

**What was NOT recovered / deliberately left as an honest stub (per this
build's own instruction: "do NOT guess shapes into code" when passive
inspection doesn't confirm a shape):** the site's "Direct Paper ID or URL"
search method visually degrades to the SAME ``q=`` search box in the UI,
suggesting ``get_by_arxiv``/``get_by_doi`` might just be
``search(arxiv_id)`` with the top hit taken -- but this build never issued
a bare-ID query and diffed its result shape against a text-query result to
confirm that assumption, so :meth:`ArxivxplorerWebProvider.get_by_doi` and
:meth:`~ArxivxplorerWebProvider.get_by_arxiv` both raise
:class:`~trialerror.litapi.errors.ProviderUnsupportedOperationError` rather than
silently guessing. **What a supervised one-time live-capture session would
need to add that support:** issue ``search("2101.02120")`` (a known-good
arXiv id) against the live endpoint, confirm the top (or only) result's
``id`` field round-trips exactly, and confirm behavior for an id with no
match (empty array? error status? a fuzzy nearest-neighbor hit?) before
writing any code that treats a bare-ID query as an identifier lookup.
:meth:`~ArxivxplorerWebProvider.get_citations` is unconditionally
unsupported for a different reason -- arxivxplorer.com has no citation
graph feature at all (confirmed by omission from every page/guide-section
this build and its predecessor review both inspected).

**IMPORTANT DISCLOSURE, surfaced prominently and not just in
``docs/reviews/ALL_ARXIV_SEARCH.md``:** ``https://search.arxivxplorer.com/robots.txt``
(the ACTUAL API host, not ``arxivxplorer.com`` itself, which the
predecessor review checked and found absent/404) returns HTTP 200 with
``User-agent: * / Disallow: /`` -- i.e. the API host publishes a
machine-readable "no crawlers" policy. This was discovered live in THIS
build's own browser-network inspection above, after C-0069 was already
issued naming this exact application ("First application: arxivxplorer.com
search client (metadata/search only)") as pre-authorized. robots.txt is
conventionally a crawler-exclusion signal (aimed at automated,
un-rate-limited, bulk-traversal bots), not a per-request access policy,
and it is not a Terms of Service -- but it IS new, material information
C-0069's own reasoning did not have in hand (that ruling's text says
arxivxplorer.com "publishes ... no robots.txt", which was true of the
FRONTEND host, not the API host this module actually calls). This module
still ships, because C-0069 is an explicit, this-application-named user
ruling, not a discretionary call this build gets to override -- but the
operator should read this disclosure, and it is the FIRST line of the
courtesy-note draft in ``docs/reviews/ALL_ARXIV_SEARCH.md`` for exactly
this reason. If the operator would rather this module not run at all
pending their own read of that disclosure, the fix is one line: leave
``[litapi.arxivxplorer].enabled`` at its shipped default, ``false``.

**Guardrails this module hard-codes (C-0069, binding, not configurable
below the config-file layer):**

1. Browser-equivalent requests ONLY -- exactly the one GET shape above,
   never an auth-bypass, never a bulk-harvest crawl (no site-map walk, no
   pagination-until-exhausted loop -- one ``search()`` call is one logical
   user search, same as clicking the site's own Search button once).
2. Politeness: :class:`~trialerror.litapi.providers.base.RateLimiter` enforces
   ``>= 3s`` between real network requests (same conservative floor
   ``arxiv.py`` uses, chosen for the same reason: no published rate limit
   exists to size against); a per-``(query, categories, year)`` sqlite
   response cache (:func:`ensure_cache_schema`) is checked BEFORE any
   network call at all, so a repeated identical search inside
   ``cache_ttl_s`` (default 24h) never hits the network a second time; a
   daily request cap (default 200, config
   ``[litapi.arxivxplorer].daily_request_cap``) hard-refuses further NEW
   network requests once exceeded (cache hits are exempt -- they cost the
   remote host nothing).
3. Honest identification: every request carries an explicit, descriptive
   ``User-Agent`` (:data:`USER_AGENT`) naming this as a research tool --
   never a spoofed browser UA string.
4. Disabled by default: :meth:`ArxivxplorerWebProvider.__init__` raises
   :class:`~trialerror.litapi.errors.ProviderConfigError` immediately unless
   ``[litapi.arxivxplorer].enabled = true`` is set -- this is enforced in
   the constructor, not just documented, so a caller cannot accidentally
   construct a live client from a default config.
5. Exponential backoff on any transport error or 429/5xx status
   (:func:`_backoff_delay_s`), doubling from a 1s base, on top of (never
   instead of) the flat 3s pacing floor above.

**Deliberately NOT wired into :data:`trialerror.litapi.client.DEFAULT_CLIENTS`
or :data:`~trialerror.litapi.client.ALL_CLIENTS`** -- unlike the four REST
providers in this package, this one is opt-in-constructed only (a caller
who has confirmed ``config.arxivxplorer.enabled`` builds it directly).
Wiring it into the shared redundant-fetch ``LitApiClient`` would mean
every existing caller of ``build_default_providers(..., provider_classes=
ALL_CLIENTS)`` starts attempting to construct this provider even with the
feature left at its default-off state, and a construction-time
``ProviderConfigError`` from one provider class would currently abort that
whole loop (``trialerror.litapi.client.build_default_providers`` has no
per-class try/except) -- rather than change that shared loop's error
handling for one opt-in provider, this module stays a standalone,
explicitly-constructed class, consistent with an EXPERIMENTAL/FRAGILE
feature that may change shape or stop working without notice.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import timezone
from urllib.parse import urlencode

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderConfigError, ProviderTransportError, ProviderUnsupportedOperationError
from trialerror.litapi.models import CitationsPage, WorkRecord, normalize_arxiv_id
from trialerror.litapi.providers.base import RateLimiter
from trialerror.litapi.transport import ProviderTransport, TransportResponse
from trialerror.util.timeutil import now_dt

__all__ = [
    "USER_AGENT",
    "ArxivxplorerWebProvider",
    "ArxivxplorerDailyCapExceededError",
    "ensure_cache_schema",
]

#: Honest, descriptive UA (guardrail 3, module docstring) -- never a
#: spoofed browser string. Points at the docs review this module's own
#: build produced, the same "identify yourself" spirit
#: ``trialerror.litapi.config``'s ``mailto=``/``email=`` fields serve for the
#: REST providers (this endpoint takes no identifying query param at all,
#: so the header is the only honest-identification channel available).
USER_AGENT = (
    "trialerror-litapi-arxivxplorer-web/0.1 "
    "(+research-harness TrialError project; polite low-volume research client; "
    "see docs/reviews/ALL_ARXIV_SEARCH.md)"
)

_CACHE_TABLE = "arxivxplorer_cache"
_LOG_TABLE = "arxivxplorer_request_log"
#: Status codes worth an exponential-backoff retry (guardrail 5) --
#: 429 (rate-limited) and the usual transient-server-error 5xx set.
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class ArxivxplorerDailyCapExceededError(ProviderTransportError):
    """The configured ``daily_request_cap`` was already reached for today
    (UTC calendar day) -- a NEW network request was refused before any
    socket was opened. A cache hit never raises this (guardrail 2)."""


def ensure_cache_schema(conn: sqlite3.Connection) -> None:
    """Create (idempotently) this module's own small cache + request-log
    tables. A DEDICATED table, not a reuse of any existing litapi cache
    (this package had none before this build -- see this module's own
    docstring, guardrail 2) -- kept intentionally tiny (two tables, no
    migration machinery) rather than pulled into ``trialerror.stores``'s
    full program-store/migration system, since this feature is opt-in,
    single-provider-scoped, and has no relationship to the four DBs a
    program's ``Store`` groups (``trialerror/stores/store.py``'s own
    docstring: "research *content* -> knowledge.db; program *operations*
    -> ops.db; ..." -- a raw third-party response cache fits none of
    those cleanly, and forcing it into that schema/migration system would
    be new coupling for a feature explicitly flagged FRAGILE)."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} (
            cache_key      TEXT PRIMARY KEY,
            response_json  TEXT NOT NULL,
            fetched_ts     TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_LOG_TABLE} (
            request_ts     TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _backoff_delay_s(attempt: int, *, base_s: float = 1.0) -> float:
    """Exponential backoff (guardrail 5): ``1s, 2s, 4s, 8s, ...`` from
    ``attempt=0``. Deliberately separate from
    ``trialerror.litapi.providers.base.get_with_retry``'s shared retry helper
    (used by every REST provider), which retries at the SAME flat pacing
    interval every attempt -- this module's own C-0069 guardrail calls for
    growing backoff specifically, on top of (not instead of) that flat
    floor."""
    return base_s * (2**attempt)


def _authors_to_list(authors: str | None) -> list[str]:
    """arxivxplorer's ``authors`` field is one comma-joined string (see
    module docstring's recovered-shape note), unlike every other provider
    in this package which returns a real list -- split on ``", "`` to
    match :class:`~trialerror.litapi.models.WorkRecord`'s ``authors: list[str]``
    shape. A name that itself legitimately contains a comma (rare, but
    real -- e.g. a suffix like "Jr.") would mis-split; no such case was
    observed in this build's live samples, flagged as a known, disclosed
    limitation rather than solved with unverified heuristics."""
    if not authors:
        return []
    return [a.strip() for a in authors.split(",") if a.strip()]


def _item_to_record(item: dict) -> WorkRecord:
    journal = item.get("journal")
    raw_id = item.get("id")
    is_arxiv = journal == "arxiv"
    arxiv_id = normalize_arxiv_id(raw_id) if is_arxiv else None
    date = item.get("date")
    year = int(date[:4]) if date and date[:4].isdigit() else None
    url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None
    oa_pdf_url = f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None
    external_ids: dict[str, str] = {}
    if raw_id:
        external_ids["arxivxplorer"] = str(raw_id)
    return WorkRecord(
        title=item.get("title"),
        doi=None,  # arxivxplorer's search response never carries a DOI field (module docstring)
        arxiv_id=arxiv_id,
        authors=_authors_to_list(item.get("authors")),
        year=year,
        venue=None,  # not present in the recovered response shape
        abstract=item.get("abstract"),
        citation_count=None,  # arxivxplorer has no citation-graph feature at all (module docstring)
        oa_pdf_url=oa_pdf_url,
        url=url,
        external_ids=external_ids,
        other={
            "journal": journal,
            "categories": list(item.get("categories") or []),
            "score": item.get("score"),
            "short_author": item.get("short_author"),
        },
    )


class ArxivxplorerWebProvider:
    """See this module's own docstring for the full guardrail list, the
    recovered request shape, and the robots.txt disclosure. ``name`` is
    ``"arxivxplorer"`` -- distinct from every REST-`Provider` name this
    package already uses, so provenance (``WorkRecord.providers``) never
    conflates a genuinely reconciled record with one sourced from this
    EXPERIMENTAL/FRAGILE client."""

    name = "arxivxplorer"

    def __init__(
        self,
        transport: ProviderTransport,
        config: ProviderApiConfig,
        *,
        cache_conn: sqlite3.Connection,
        program_root=None,  # accepted, unused -- matches every other Provider's constructor call-shape
        _sleep_fn=time.sleep,
        _now_fn=now_dt,
    ):
        if not getattr(config, "enabled", False):
            raise ProviderConfigError(
                "ArxivxplorerWebProvider refuses to construct: [litapi.arxivxplorer].enabled is not "
                "true (C-0069's binding disabled-by-default guardrail). Set it explicitly in "
                "trialerror.toml to use this EXPERIMENTAL/FRAGILE client -- see this module's own docstring "
                "and docs/reviews/ALL_ARXIV_SEARCH.md before doing so."
            )
        self.transport = transport
        self.config = config
        self.cache_conn = cache_conn
        ensure_cache_schema(self.cache_conn)
        self._rate_limiter = RateLimiter(config.min_interval_s)
        self._sleep_fn = _sleep_fn
        self._now_fn = _now_fn

    # -- cache -----------------------------------------------------------

    def _now_iso(self) -> str:
        """Same ISO-8601-UTC-``Z`` shape ``trialerror.util.timeutil.now()``
        produces, but built from ``self._now_fn()`` -- the injectable seam
        -- so cache-write and cache-read timestamps (and the daily-cap
        day-grouping below) always agree with whatever clock a test
        substituted, rather than reading the real wall clock on write and
        a fake one on read (which would silently defeat the TTL/day-window
        math in a test)."""
        dt = self._now_fn()
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _cache_get(self, cache_key: str) -> str | None:
        row = self.cache_conn.execute(
            f"SELECT response_json, fetched_ts FROM {_CACHE_TABLE} WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None
        response_json, fetched_ts = row
        age_s = (self._now_fn() - _parse_ts(fetched_ts)).total_seconds()
        if age_s > self.config.cache_ttl_s:
            return None  # expired -- treated as a miss, a fresh fetch overwrites it below
        return response_json

    def _cache_put(self, cache_key: str, response_json: str) -> None:
        with self.cache_conn:
            self.cache_conn.execute(
                f"INSERT INTO {_CACHE_TABLE}(cache_key, response_json, fetched_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET response_json = excluded.response_json, "
                "fetched_ts = excluded.fetched_ts",
                (cache_key, response_json, self._now_iso()),
            )

    # -- daily cap ---------------------------------------------------------

    def _today_request_count(self) -> int:
        today = self._now_iso()[:10]  # "YYYY-MM-DD" prefix
        row = self.cache_conn.execute(
            f"SELECT COUNT(*) FROM {_LOG_TABLE} WHERE substr(request_ts, 1, 10) = ?", (today,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _log_request(self) -> None:
        with self.cache_conn:
            self.cache_conn.execute(f"INSERT INTO {_LOG_TABLE}(request_ts) VALUES (?)", (self._now_iso(),))

    # -- network -----------------------------------------------------------

    def _build_url(self, query: str, *, categories: list[str] | None = None, year: int | None = None) -> str:
        params: list[tuple[str, str]] = [("q", query)]
        for cat in categories or ():
            params.append(("cats", cat))
        if year is not None:
            params.append(("year", str(year)))
        return f"{self.config.base_url}/?{urlencode(params)}"

    def _fetch(self, url: str) -> TransportResponse:
        if self._today_request_count() >= self.config.daily_request_cap:
            raise ArxivxplorerDailyCapExceededError(
                f"arxivxplorer: daily request cap ({self.config.daily_request_cap}) already reached "
                "for today (UTC) -- refusing a new network request (C-0069 politeness guardrail). "
                "Cached results are unaffected; try again tomorrow or raise "
                "[litapi.arxivxplorer].daily_request_cap.",
                provider=self.name,
            )
        attempts = max(1, self.config.retry_attempts)
        last_response: TransportResponse | None = None
        for attempt in range(attempts):
            self._rate_limiter.wait()
            try:
                response = self.transport.get(
                    url, headers={"User-Agent": USER_AGENT}, timeout_s=self.config.timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - deliberate: any transport failure gets backoff+retry
                self._log_request()  # a real attempt was made, even though it errored -- counts against the cap
                if attempt < attempts - 1:
                    self._sleep_fn(_backoff_delay_s(attempt))
                    continue
                raise ProviderTransportError(
                    f"arxivxplorer: transport error after {attempts} attempt(s): {exc}", provider=self.name
                ) from exc
            self._log_request()
            last_response = response
            if response.ok or response.status_code not in _RETRYABLE_STATUS:
                return response
            if attempt < attempts - 1:
                self._sleep_fn(_backoff_delay_s(attempt))
        assert last_response is not None  # attempts >= 1 guarantees at least one response
        return last_response

    # -- Provider interface --------------------------------------------------

    def search(
        self, query: str, *, limit: int = 10, categories: list[str] | None = None, year: int | None = None
    ) -> list[WorkRecord]:
        """One logical search -- one browser-equivalent request (never a
        paginate-until-exhausted loop, per guardrail 1). ``categories``/
        ``year`` mirror the site's own Tags/Year filters
        (module docstring's recovered request shape)."""
        url = self._build_url(query, categories=categories, year=year)
        cached = self._cache_get(url)
        if cached is not None:
            body_text = cached
        else:
            response = self._fetch(url)
            if not response.ok:
                raise ProviderTransportError(
                    f"arxivxplorer search({query!r}) failed: HTTP {response.status_code}",
                    provider=self.name,
                    status_code=response.status_code,
                )
            body_text = response.text
            self._cache_put(url, body_text)
        items = _parse_json_array(body_text, provider=self.name)
        records = [_item_to_record(item) for item in items if isinstance(item, dict)]
        return records[: max(1, limit)]

    def get_by_doi(self, doi: str) -> WorkRecord | None:
        """See this module's docstring: NOT recovered/verified this
        session -- an honest stub, not a guess."""
        raise ProviderUnsupportedOperationError(
            "arxivxplorer: DOI lookup was not recovered/verified by this build's passive live-network "
            "inspection (see trialerror.litapi.providers.arxivxplorer_web's own module docstring for exactly "
            "what a supervised follow-up capture session would need to confirm before this is safe to "
            "implement) -- use search() instead.",
            provider=self.name,
        )

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None:
        """See :meth:`get_by_doi` -- same disclosed limitation."""
        raise ProviderUnsupportedOperationError(
            "arxivxplorer: by-arxiv-id lookup was not recovered/verified by this build's passive "
            "live-network inspection (see this module's own docstring) -- use search() instead.",
            provider=self.name,
        )

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage:
        raise ProviderUnsupportedOperationError(
            f"arxivxplorer has no citation-graph feature at all (metadata/search only) -- "
            f"identifier={identifier!r}",
            provider=self.name,
        )


def _parse_ts(value: str):
    v = value[:-1] + "+00:00" if value.endswith("Z") else value
    from datetime import datetime

    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_json_array(text: str, *, provider: str) -> list:
    import json

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProviderTransportError(
            f"{provider}: response body was not valid JSON: {exc}", provider=provider
        ) from exc
    if not isinstance(data, list):
        raise ProviderTransportError(
            f"{provider}: expected a bare JSON array response (recovered shape, module docstring), "
            f"got {type(data).__name__}",
            provider=provider,
        )
    return data
