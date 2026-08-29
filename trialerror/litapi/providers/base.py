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

import time
from typing import Any, Mapping, Protocol, Sequence

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import ProviderTransportError
from trialerror.litapi.models import CitationsPage, WorkRecord
from trialerror.litapi.transport import ProviderTransport, TransportResponse

__all__ = ["Provider", "RateLimiter", "get_with_retry"]


class Provider(Protocol):
    """The common provider interface every client implements. ``name`` is
    the short key used throughout this package for provenance
    (``WorkRecord.providers``) and config lookup (``LitApiConfig.provider``)
    -- currently ``"openalex"`` or ``"semanticscholar"``."""

    name: str

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
    """

    def __init__(self, min_interval_s: float, *, _time_fn=time.monotonic, _sleep_fn=time.sleep):
        self.min_interval_s = min_interval_s
        self._time_fn = _time_fn
        self._sleep_fn = _sleep_fn
        self._last_call: float | None = None

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
    and propagates as-is; it is wrapped into
    :class:`~trialerror.litapi.errors.ProviderTransportError` by the caller.
    """
    attempts = max(1, retry_attempts)
    last_response: TransportResponse | None = None
    for attempt in range(attempts):
        rate_limiter.wait()
        response = transport.get(url, headers=headers, timeout_s=timeout_s)
        last_response = response
        if response.ok or response.status_code not in retry_on_status:
            return response
        # retryable status; loop again (final iteration falls through and
        # returns the last response as-is -- the caller's own status-code
        # handling decides pass/not-found/error, this function's job is
        # only "did we exhaust the retry budget").
    assert last_response is not None  # attempts >= 1 guarantees at least one response
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
    an unexpected failure worth surfacing distinctly."""
    if not response.ok:
        raise ProviderTransportError(
            f"{provider} request failed ({context}): HTTP {response.status_code}",
            provider=provider,
            status_code=response.status_code,
        )
