"""The ``ProviderTransport`` abstraction. Design brief (C-0064
litapi-preview mission, "CRITICAL -- OFFLINE-TESTABLE"): "NO live network
calls in tests ... tests inject a FakeTransport returning canned JSON
fixtures ... the real HTTP transport exists but is exercised only by a
skip-marked live smoke test."

Every provider client (``trialerror.litapi.providers.openalex``/
``.semanticscholar``) is constructed with a :class:`ProviderTransport` and
never imports ``urllib``/``httpx`` itself -- this is the one seam that
makes the whole package importable and fully testable on a machine with no
network access at all.

Two implementations ship:

- :class:`FakeTransport` -- deterministic, in-memory, canned-response
  routing keyed by exact URL string. Used by every test in this package's
  suite (mirrors ``trialerror.ingest.backends``'s ``Fake*``/``Real*`` split:
  "tests must NOT need the GPU" here becomes "tests must NOT need the
  network").
- :class:`UrllibTransport` -- the real transport, stdlib ``urllib.request``
  only (design brief: "prefer stdlib urllib to add zero deps" over an
  httpx dependency). Exercised only by
  ``tests/test_litapi_live_smoke.py``, which is skipped unless
  ``TRIALERROR_LITAPI_LIVE_TESTS=1`` is set in the environment (the design's
  F18 discipline, same shape as ``trialerror.ingest.backends``'s GPU-gated
  ``Real*`` backends).

TRIALERROR-DEV-NOTE (deviation, disclosed per instructions): the mission brief
allowed "httpx-or-stdlib-urllib ... add httpx to pyproject as an OPTIONAL
[litapi] extra IF you use it". This build uses stdlib ``urllib`` ONLY --
no httpx client was written and no ``[litapi]`` optional-dependency extra
was added to ``pyproject.toml``. This is the brief's own preferred branch
("else stdlib urllib to add zero deps -- prefer this"), not a shortfall:
zero new runtime dependencies, and the ``ProviderTransport`` Protocol
means a future ``HttpxTransport`` is a pure addition (new file, no changes
to this one or to any provider) if a later session decides connection
pooling/HTTP2 is worth the dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from trialerror.litapi.errors import TransportNotConfiguredError

__all__ = [
    "TransportResponse",
    "ProviderTransport",
    "FakeTransport",
    "UrllibTransport",
]


@dataclass(frozen=True)
class TransportResponse:
    """One HTTP response, transport-agnostic. ``json_body`` is pre-parsed
    when the body was valid JSON (both real providers always respond JSON,
    including their error bodies) and ``None`` when it wasn't/was empty --
    callers branch on ``json_body is None`` rather than re-parsing
    ``text`` themselves."""

    status_code: int
    json_body: Any = None
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class ProviderTransport(Protocol):
    """The one method every provider client calls through. Deliberately
    minimal (GET-only, no POST/streaming) -- both OpenAlex and Semantic
    Scholar's Graph API are pure-GET/JSON per the mining reports
    (``docs/mining/S1-scilit-1__{openalex-api,semantic-scholar-api}.md``);
    a future provider needing POST is a Protocol extension, not a reason
    to widen this one now."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> TransportResponse: ...


class FakeTransport:
    """Offline transport for tests: exact-URL-keyed canned responses, no
    network. Every call is recorded in :attr:`calls` (url + headers sent)
    so a test can assert a provider built the URL/headers it expected
    (e.g. that ``mailto=`` or the API-key header landed) without needing a
    real server on the other end.

    Deliberately strict: a URL with no registered response raises
    :class:`~trialerror.litapi.errors.TransportNotConfiguredError` rather than
    silently 404ing -- an unmatched URL during a test almost always means
    the fixture is incomplete (a provider changed how it builds a URL) and
    should fail loudly, not be mistaken for a real "not found" response.
    """

    def __init__(self) -> None:
        self._routes: dict[str, TransportResponse] = {}
        self.calls: list[dict[str, Any]] = []

    def add_response(self, url: str, response: TransportResponse) -> None:
        """Register the canned response for an exact ``url`` (including
        its query string, if any -- providers build the full URL before
        calling the transport, so matching is exact-string, not
        path-only)."""
        self._routes[url] = response

    def add_json(self, url: str, *, json_body: Any, status_code: int = 200) -> None:
        """Convenience wrapper: register a JSON body, auto-rendering
        ``text`` from it (``json.dumps``) so a provider that reads
        ``response.text`` instead of ``response.json_body`` still works
        against the fixture."""
        self.add_response(
            url,
            TransportResponse(
                status_code=status_code,
                json_body=json_body,
                text=json.dumps(json_body, ensure_ascii=False),
            ),
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> TransportResponse:
        self.calls.append({"url": url, "headers": dict(headers) if headers else {}})
        if url not in self._routes:
            raise TransportNotConfiguredError(
                f"FakeTransport has no canned response registered for URL: {url!r} "
                f"(known routes: {sorted(self._routes)!r})"
            )
        return self._routes[url]


class UrllibTransport:
    """The real transport: stdlib ``urllib.request``, zero third-party
    dependencies. A non-2xx response is NOT raised as a Python exception
    here (``urllib`` itself raises ``HTTPError`` for those) -- it is
    caught and returned as a normal :class:`TransportResponse` with the
    real status code and (when present) the parsed JSON error body, so
    every provider's own status-code branching logic (404 -> not-found,
    5xx -> transport error, etc.) runs uniformly whether the fixture came
    from :class:`FakeTransport` or a live server.
    """

    def __init__(self, *, default_timeout_s: float = 15.0, user_agent: str = "trialerror-litapi/0.1"):
        self.default_timeout_s = default_timeout_s
        self.user_agent = user_agent

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> TransportResponse:
        req_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        request = urllib.request.Request(url, headers=req_headers, method="GET")
        timeout = timeout_s if timeout_s is not None else self.default_timeout_s
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - deliberate: this IS the http client
                body = resp.read()
                return self._to_response(resp.status, body, dict(resp.headers))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return self._to_response(exc.code, body, dict(exc.headers or {}))

    @staticmethod
    def _to_response(status_code: int, body: bytes, headers: Mapping[str, str]) -> TransportResponse:
        text = body.decode("utf-8", errors="replace") if body else ""
        json_body: Any = None
        if text:
            try:
                json_body = json.loads(text)
            except json.JSONDecodeError:
                json_body = None
        return TransportResponse(status_code=status_code, json_body=json_body, text=text, headers=headers)
