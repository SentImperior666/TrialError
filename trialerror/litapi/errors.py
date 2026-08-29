"""``trialerror.litapi`` exceptions. Mirrors the repo-wide precedent
(``trialerror.ingest.errors``, ``trialerror.stores.errors``, ``trialerror.verify.errors``):
one base class every caller that only cares "did this lookup fail" can
catch, plus specific subclasses for callers that need to branch on *why*.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "LitApiError",
    "ProviderConfigError",
    "ProviderTransportError",
    "ProviderNotFoundError",
    "ProviderUnsupportedOperationError",
    "AllProvidersFailedError",
    "TransportNotConfiguredError",
]


class LitApiError(Exception):
    """Base class for every error the ``trialerror.litapi`` package raises."""


class ProviderConfigError(LitApiError):
    """A provider was asked to do something its ``trialerror.toml``
    ``[litapi.<provider>]`` config does not support (e.g. no ``base_url``,
    or a real transport requested with no reachable config at all)."""


class ProviderTransportError(LitApiError):
    """A provider's HTTP call failed at the transport level (network error,
    a non-2xx/404 status the provider doesn't treat as "not found", or a
    malformed response body). Carries the provider name and, when known,
    the HTTP status code -- callers that want to branch on 429-vs-5xx etc.
    can inspect ``status_code`` rather than parsing the message string."""

    def __init__(self, message: str, *, provider: str, status_code: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class ProviderNotFoundError(LitApiError):
    """A provider was reached successfully but has no record for the given
    identifier (OpenAlex/Semantic Scholar both surface this as an HTTP 404
    with a small JSON error body -- see the ``*_not_found.json`` fixtures).
    Deliberately a DIFFERENT class from :class:`ProviderTransportError`:
    "no record here" is an expected, non-exceptional outcome for a
    redundant multi-provider lookup (the other provider may still have
    it), whereas a transport error is a fragility signal worth recording
    in ``providers_failed`` provenance."""

    def __init__(self, message: str, *, provider: str):
        super().__init__(message)
        self.provider = provider


class ProviderUnsupportedOperationError(LitApiError):
    """A provider was asked to perform an operation it structurally does
    not offer at all (e.g. Unpaywall has no search or citation-graph
    endpoint -- DOI-lookup/OA-location only; arXiv has no citation-graph
    endpoint -- metadata/full-text only). Deliberately a DIFFERENT class
    from :class:`ProviderNotFoundError` ("no record for THIS identifier",
    discovered per-call) and :class:`ProviderTransportError` (a real
    HTTP/network failure): this is known upfront, from the provider's own
    documented capability set, not a runtime discovery -- callers (e.g.
    :class:`~trialerror.litapi.client.LitApiClient`) still catch it via the
    common ``LitApiError`` base and record it in ``providers_failed``
    provenance exactly like any other per-provider failure (v1-preview
    build has no separate "unsupported" bucket in that provenance list --
    see that class's own docstring)."""

    def __init__(self, message: str, *, provider: str):
        super().__init__(message)
        self.provider = provider


class AllProvidersFailedError(LitApiError):
    """Every configured provider either raised or returned "not found" for
    one lookup/citations call. Carries the per-provider failure detail
    (``details["failures"]``, a list of ``{"provider", "error"}`` dicts) so
    a CLI/MCP caller can surface *why* each provider came up empty instead
    of a single opaque message -- the whole point of the redundant-fetch
    design is that a caller can see which single API is the current
    bottleneck."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details) if details else {}


class TransportNotConfiguredError(LitApiError):
    """:class:`trialerror.litapi.transport.FakeTransport` has no canned response
    registered for a requested URL -- almost always an incomplete offline
    test fixture, not a real runtime condition (the real transport never
    raises this)."""
