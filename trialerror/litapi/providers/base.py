"""The ``Provider`` interface + the small pieces of behavior every
provider client shares (rate-limit pacing, retry-on-status). Design brief:
"a common Provider interface (get_by_doi, get_by_arxiv, search,
get_citations)".

Both concrete providers (:mod:`trialerror.litapi.providers.openalex`,
:mod:`trialerror.litapi.providers.semanticscholar`) are constructed the same
way -- ``Provider(transport, config)`` -- and share :class:`RateLimiter`
and :func:`get_with_retry` rather than each reimplementing pacing/retry,
so a rate-limit or retry-policy fix lands in exactly one place.
"""

from __future__ import annotations

import email.utils
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderTransportError
from trialerror.litapi.models import CitationsPage, WorkRecord
from trialerror.litapi.transport import ProviderTransport, TransportResponse

__all__ = [
    "DEFAULT_MAX_TOTAL_WAIT_S",
    "DEFAULT_BACKOFF_BASE_S",
    "MAX_BLOCK_S",
    "Provider",
    "RateLimiter",
    "parse_retry_after",
    "get_with_retry",
]

#: The STATED hard cap: one :func:`get_with_retry` call never sleeps more than
#: this on backoff in total, however many 429s it is answered with. Together
#: with the limiter's own spacing, the worst case per provider call is
#: ``DEFAULT_MAX_TOTAL_WAIT_S + retry_attempts * (min_interval_s + timeout_s)``,
#: and a lookup over P providers is at most P times that, sequentially. There is
#: no unbounded loop anywhere: the retry loop is ``range(attempts)``.
DEFAULT_MAX_TOTAL_WAIT_S = 30.0

#: Exponential backoff base, used only when the provider named no
#: ``Retry-After`` of its own: 1, 2, 4, ... seconds.
DEFAULT_BACKOFF_BASE_S = 1.0

#: The ceiling on a cross-invocation block (:meth:`RateLimiter.note_blocked`).
#: A provider that asks for a day is not given a day: fifteen minutes is as
#: long as one recorded refusal is allowed to gate the next process.
MAX_BLOCK_S = 900.0


class Provider(Protocol):
    """The common provider interface every client implements. ``name`` is
    the short key used throughout this package for provenance
    (``WorkRecord.providers``) and config lookup (``LitApiConfig.provider``)
    -- currently ``"openalex"`` or ``"semanticscholar"``."""

    name: str

    #: What THIS provider's ``search`` actually matches on, in one phrase
    #: (lane FB-1 item F3). The three search-capable providers query three
    #: different things -- a title-only filter, an all-fields query, a
    #: relevance endpoint -- and a caller comparing their hit counts
    #: without knowing that is comparing three different questions. Stated
    #: on the class because the provider is the only place that knows it;
    #: surfaced beside ``providers_succeeded`` so a result carries it.
    search_scope: str = "unspecified"

    def get_by_doi(self, doi: str) -> WorkRecord | None: ...

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None: ...

    def search(self, query: str, *, limit: int = 10) -> list[WorkRecord]: ...

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage: ...


class RateLimiter:
    """The simplest possible pacing gate: never issue two requests less
    than ``min_interval_s`` apart, sleeping to make up the difference.
    Deliberately not a token-bucket/sliding-window limiter -- the mission
    brief's own instruction is "conservative defaults", and this package
    has no concurrent-caller story yet (v1-preview, single-process CLI
    usage); a real bucket/window limiter is a natural v1 upgrade once
    ``docs/EXTERNAL_API_FACTS.md`` gives real numbers to size one against.

    ``_time_fn``/``_sleep_fn`` are internal seams (mirrors
    ``trialerror.ingest.backends.FakeEmbedBackend``'s own ``delay_s`` precedent)
    letting a test observe/replace pacing deterministically without an
    actual wall-clock sleep; production callers never pass them.

    **``stamp_path`` -- the cross-INVOCATION half** (lane FB-acq item 2). The
    in-memory gate above is one per provider INSTANCE, and every CLI
    invocation is a new process, so across invocations the spacing was zero: a
    shell loop of ``lit lookup`` calls hit a provider as fast as processes
    started. With a ``stamp_path`` this limiter additionally reads and writes a
    tiny JSON file -- ``{"last_request_ts": float, "blocked_until_ts": float |
    null}`` -- and paces against WALL time since the last request ANY process
    made through that same file.

    It is deliberately BEST EFFORT and takes no lock: two processes racing into
    :meth:`wait` at the same moment may both pass, and a stamp file that is
    missing, unreadable or corrupt is treated as empty rather than as an error.
    The alternative -- a real lock, with its own staleness and cleanup problem
    -- buys strictness this does not need: the stamp exists to stop a retry
    storm, not to be a distributed rate limiter. Without a ``stamp_path``
    nothing here engages at all, which is what every test and library caller
    that passes nothing gets.
    """

    def __init__(
        self,
        min_interval_s: float,
        *,
        stamp_path: Path | None = None,
        _time_fn=time.monotonic,
        _sleep_fn=time.sleep,
        _wall_fn=time.time,
    ):
        self.min_interval_s = min_interval_s
        self.stamp_path = Path(stamp_path) if stamp_path is not None else None
        self._time_fn = _time_fn
        self._sleep_fn = _sleep_fn
        self._wall_fn = _wall_fn
        self._last_call: float | None = None

    # -- the stamp file ------------------------------------------------------

    def _read_stamp(self) -> dict[str, Any]:
        """The stamp's contents, or ``{}`` for absent/unreadable/corrupt --
        every one of which means the same thing to a best-effort gate: nothing
        is known about the last request."""
        if self.stamp_path is None:
            return {}
        try:
            raw = json.loads(self.stamp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_stamp(self, **fields: Any) -> None:
        """Merge ``fields`` into the stamp and replace it atomically (temp file
        + ``os.replace``), so a reader never sees a half-written file. A write
        that cannot happen at all is swallowed: pacing is not worth failing a
        lookup over."""
        if self.stamp_path is None:
            return
        data = self._read_stamp()
        data.update(fields)
        tmp = self.stamp_path.with_name(self.stamp_path.name + f".{os.getpid()}.tmp")
        try:
            self.stamp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.stamp_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def note_blocked(self, retry_after_s: float | None) -> None:
        """Record that the provider refused us and asked for a wait, so the
        NEXT process does not walk straight into the same 429. Clamped at
        :data:`MAX_BLOCK_S`. A no-op without a ``stamp_path`` or without a
        stated wait -- a block nobody asked for is a block this will not
        invent."""
        if self.stamp_path is None or retry_after_s is None:
            return
        self._write_stamp(blocked_until_ts=self._wall_fn() + min(float(retry_after_s), MAX_BLOCK_S))

    def blocked_remaining_s(self) -> float:
        """Seconds left on a recorded block, ``0.0`` when none is in force."""
        until = self._read_stamp().get("blocked_until_ts")
        if not isinstance(until, (int, float)) or isinstance(until, bool):
            return 0.0
        return max(0.0, float(until) - self._wall_fn())

    def clear_block(self) -> None:
        self._write_stamp(blocked_until_ts=None)

    # -- the gate ------------------------------------------------------------

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        now = self._time_fn()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval_s - elapsed
            if remaining > 0:
                self._sleep_fn(remaining)
                now = self._time_fn()
        self._last_call = now
        if self.stamp_path is None:
            return
        last = self._read_stamp().get("last_request_ts")
        wall = self._wall_fn()
        if isinstance(last, (int, float)) and not isinstance(last, bool):
            remaining = self.min_interval_s - (wall - float(last))
            if remaining > 0:
                self._sleep_fn(remaining)
                wall = self._wall_fn()
        self._write_stamp(last_request_ts=wall)


def parse_retry_after(headers: Mapping[str, str] | None, *, _now_fn=time.time) -> float | None:
    """The ``Retry-After`` header as seconds, or ``None`` when it is absent or
    unparseable. Both documented forms are accepted: a number of seconds, and
    an HTTP-date (parsed with :func:`email.utils.parsedate_to_datetime`, the
    stdlib's own RFC-compliant reader). A date already in the past, or a
    negative number, reads as ``0.0`` -- "you may retry now" -- never as a
    negative wait.

    The header lookup is case-insensitive: HTTP header names are, and
    ``TransportResponse.headers`` carries whatever the server or the fixture
    actually spelled."""
    if not headers:
        return None
    raw = None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return max(0.0, parsed.timestamp() - _now_fn())
    except (OverflowError, OSError, ValueError):
        return None


def get_with_retry(
    transport: ProviderTransport,
    url: str,
    *,
    provider: str,
    headers: Mapping[str, str] | None,
    timeout_s: float,
    rate_limiter: RateLimiter,
    retry_attempts: int,
    retry_on_status: Sequence[int],
    max_total_wait_s: float = DEFAULT_MAX_TOTAL_WAIT_S,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    stats: dict[str, Any] | None = None,
    _sleep_fn=None,
) -> TransportResponse:
    """One GET through ``transport``, paced by ``rate_limiter``, retried
    (with the SAME pacing gate between attempts) up to ``retry_attempts``
    times when the response status is in ``retry_on_status`` -- the
    mining-report-grounded retry policy (see
    ``trialerror.litapi.config``'s module docstring): OpenAlex retries on HTTP
    500, Semantic Scholar retries on HTTP 403, both observed as transient
    in production per paper-qa's client code comments.

    A transport-level exception (network failure, not a non-2xx status --
    :class:`FakeTransport`/:class:`UrllibTransport` both return
    non-2xx as a normal response rather than raising) is NOT retried here
    -- it is wrapped into :class:`~trialerror.litapi.errors.ProviderTransportError`
    right here (``status_code=None``, ``host``/``scheme`` parsed from
    ``url``) and re-raised immediately, on the FIRST occurrence, rather
    than propagating the raw ``urllib.error.URLError``/socket
    timeout/``ConnectionError`` (all ``OSError`` subclasses -- catching
    the base class here covers all three, plus DNS failures
    (``socket.gaierror``), with one clause) all the way up as an uncaught
    exception (litapi-arxiv-https build, C-0093(a) egress-hardening
    incident: a raw ``URLError`` from one provider used to abort
    :class:`~trialerror.litapi.client.LitApiClient`'s entire per-provider
    tolerance loop, discarding already-succeeded records from OTHER
    providers, and reached the CLI as a bare Python traceback instead of a
    structured error envelope). Wrapping it into the SAME exception class
    every other transport failure already uses means every existing
    ``except LitApiError``/``except ProviderTransportError`` catch site
    (the per-provider tolerance loops in ``trialerror.litapi.client``, the
    "a transport hiccup is tolerated silently here" catches in
    ``trialerror.ingest.acquire._resolve_oa``) now actually catches it, as
    those call sites' own docstrings already assumed.

    **Waiting is bounded, always** (lane FB-acq item 2). A retryable status
    with attempts left waits the provider's own ``Retry-After`` when it named
    one, else ``backoff_base_s * 2 ** attempt``. If the next wait would push
    the total past ``max_total_wait_s``, this STOPS and returns the last
    response as it stands: sleeping a truncated wait and retrying into a
    certain second 429 is worse than answering now, and the caller's own
    status handling reports the refusal either way. On giving up on a 429 that
    carried a ``Retry-After``, the limiter records the block
    (:meth:`RateLimiter.note_blocked`), so the next INVOCATION does not walk
    into it -- and a block already in force is honoured here, before the
    request, either by sleeping out its remainder (when that fits the budget)
    or by refusing without sending anything at all.

    ``stats``, when given, is filled with ``{"attempts", "waited_s",
    "last_status", "retry_after_s", "request_sent"}`` -- what
    ``trialerror.litapi.client`` reports per provider so a caller can see which
    single API is the current bottleneck.
    """
    sleep_fn = _sleep_fn if _sleep_fn is not None else rate_limiter._sleep_fn
    attempts = max(1, retry_attempts)
    waited = 0.0
    made = 0
    retry_after_s: float | None = None
    if stats is not None:
        stats.update({"attempts": 0, "waited_s": 0.0, "last_status": None, "retry_after_s": None,
                      "request_sent": False})

    blocked_for = rate_limiter.blocked_remaining_s()
    if blocked_for > 0:
        if blocked_for > max_total_wait_s:
            raise ProviderTransportError(
                f"{provider}: rate-limited earlier, provider asked to wait {blocked_for:.0f}s more "
                "-- no request sent",
                provider=provider,
                status_code=429,
                retry_after_s=blocked_for,
            )
        sleep_fn(blocked_for)
        waited += blocked_for
        rate_limiter.clear_block()

    last_response: TransportResponse | None = None
    for attempt in range(attempts):
        rate_limiter.wait()
        made += 1
        if stats is not None:
            stats["attempts"] = made
            stats["request_sent"] = True
        try:
            response = transport.get(url, headers=headers, timeout_s=timeout_s)
        except OSError as exc:
            parsed = urlsplit(url)
            raise ProviderTransportError(
                f"{provider}: transport unreachable ({parsed.scheme}://{parsed.netloc}): {exc}",
                provider=provider,
                status_code=None,
                host=parsed.hostname,
                scheme=parsed.scheme,
            ) from exc
        last_response = response
        if stats is not None:
            stats["last_status"] = response.status_code
        if response.ok or response.status_code not in retry_on_status:
            return response
        retry_after_s = parse_retry_after(response.headers)
        if stats is not None:
            stats["retry_after_s"] = retry_after_s
        if attempt == attempts - 1:
            break
        wait = retry_after_s if retry_after_s is not None else backoff_base_s * (2**attempt)
        if waited + wait > max_total_wait_s:
            break
        sleep_fn(wait)
        waited += wait
        if stats is not None:
            stats["waited_s"] = waited
    assert last_response is not None  # attempts >= 1 guarantees at least one response
    if last_response.status_code == 429 and retry_after_s is not None:
        rate_limiter.note_blocked(retry_after_s)
    if stats is not None:
        stats["waited_s"] = waited
    return last_response


def build_headers(cfg: ProviderApiConfig, api_key: str | None) -> dict[str, str]:
    """Shared header-building convention: an API key (when resolved) goes
    in ``cfg.api_key_header`` -- never a query param, never logged (this
    function receives the already-resolved key string; it does not read
    the key file itself -- see ``trialerror.litapi.config.resolve_api_key``)."""
    headers: dict[str, str] = {}
    if api_key:
        headers[cfg.api_key_header] = api_key
    return headers


def raise_for_transport_error(response: TransportResponse, *, provider: str, context: str) -> None:
    """Shared "this status code is a real transport failure, not a
    not-found" guard. Callers check for 404 (-> ``ProviderNotFoundError``)
    BEFORE calling this, so by the time this runs, any non-2xx status is
    an unexpected failure worth surfacing distinctly.

    The provider's own ``Retry-After`` (when it sent one) travels on the
    exception, so a caller can say WHEN to try again rather than only that it
    failed."""
    if not response.ok:
        raise ProviderTransportError(
            f"{provider} request failed ({context}): HTTP {response.status_code}",
            provider=provider,
            status_code=response.status_code,
            retry_after_s=parse_retry_after(response.headers),
        )
