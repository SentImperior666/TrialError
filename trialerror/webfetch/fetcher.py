"""The one place a socket is opened. GET, and nothing but GET.

Design §4, threats T1/T2/T5. The request this module builds is the narrowest
thing that can still read a web page:

* **GET only, fixed headers, no body.** There is no method parameter, no
  header parameter and no body parameter anywhere in the public API — the
  manifest that crosses the trust boundary has no field that could carry
  one, and this module has no code that would use it if it did. The only
  request-shaping values an enqueuer can influence are the URL (bounded by
  :mod:`~trialerror.webfetch.urlcheck` and the host allowlist) and the two
  conditional headers, which are replayed from *this* system's own record of
  a previous fetch of the *same host*, never from anything supplied.
* **Every hop is a fresh decision.** A redirect is not followed by the HTTP
  library; it is returned to this module, which re-runs the URL shape check,
  the host allowlist, the address policy and the pacing before it opens the
  next socket. ``https → http`` is refused rather than downgraded, and a
  target on a host the operator has not approved is
  ``redirect_off_allowlist`` — a site cannot bounce this fetcher onto a host
  the operator never saw.
* **Caps are enforced on the decoded stream.** ``Content-Length`` is a hint
  from a party with an interest in lying, so it is checked *and then*
  ignored: the read loop counts compressed bytes in and decoded bytes out,
  refuses at the per-class cap, and refuses earlier still if the ratio
  between them passes ``decompress_ratio_cap`` — the zip-bomb case, where
  the response is small and honest right up to the moment it is 100 MiB of
  zeroes.

Nothing here parses a payload. The body is bytes plus a magic-number sniff
(``%PDF-``, an HTML marker in the first KiB); HTML, PDF and tar parsing
happen in the research container, which has no egress (design §1 P1).

The transport is deliberately built from :mod:`http.client`'s response
parser over a socket this process connected itself, rather than from
``urllib`` or a connection pool: the address to connect to is chosen by
:mod:`~trialerror.webfetch.netguard` and must not be re-resolved, and the
exact bytes of the request must be assertable in a test.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import socket
import time
import zlib
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.netguard import NetGuard
from trialerror.webfetch.policy import HostPacer, HostRule, Policy
from trialerror.webfetch.robots import RobotsDocument
from trialerror.webfetch.urlcheck import NormalizedUrl, normalize, peek_host

__all__ = [
    "CONTENT_TYPE_CLASSES",
    "REDIRECT_STATUSES",
    "RETRYABLE_STATUSES",
    "FetchResult",
    "Fetcher",
    "user_agent_for",
]

#: The five media types this fetcher will store (design §4 T5, "content-type
#: confusion"), mapped to the content class the research side branches on.
#: Anything else — including ``application/octet-stream``, the shape a
#: mislabelled binary usually arrives in — is ``content_type_disallowed``.
CONTENT_TYPE_CLASSES: Mapping[str, str] = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "text/markdown": "text",
    "application/pdf": "pdf",
}

REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

_ACCEPT = (
    "text/html,application/xhtml+xml;q=0.9,application/pdf;q=0.9,"
    "text/plain;q=0.8,text/markdown;q=0.8"
)
_ACCEPT_ROBOTS = "text/plain"
_MAX_CONDITIONAL_LEN = 256
_MAX_HEADER_VALUE_LEN = 1024
_READ_BLOCK = 64 * 1024
#: Below this many decoded bytes the compression ratio is meaningless — a
#: 40-byte response that gzips to 30 is not a bomb — so the ratio cap only
#: starts applying once a response is big enough for the ratio to mean
#: something. The absolute per-class cap applies throughout regardless.
_RATIO_FLOOR_BYTES = 64 * 1024
_MAX_ERROR_BODY = 64 * 1024
_MAX_ROBOTS_BYTES = 512 * 1024
_RETRY_ATTEMPTS = 5
_RETRY_BASE_S = 1.0
_MAX_RETRY_AFTER_S = 120.0

#: Markers of an anti-bot interstitial. Their presence means a human with a
#: browser is being asked to prove something; no evasion is ever attempted
#: (design §4 T4, and the C-0069/C-0048 line on bot walls) — the fetch is
#: refused and the operator gets a request row.
_BOT_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "verifying you are human",
    "ddos protection by",
    "captcha",
)
#: Markers of a subscription wall, including the Substack wording the wave_0
#: list is expected to hit.
_PAYWALL_MARKERS: tuple[str, ...] = (
    "for paid subscribers",
    "this post is for paying subscribers",
    "subscribe to keep reading",
    "become a paying subscriber",
    "already a paid subscriber",
    "sign in to continue reading",
    "subscription required",
    "members only",
)

_ASCII_SAFE_RE = re.compile(r"[^\x20-\x7e]")
_HTML_MARKERS: tuple[bytes, ...] = (b"<html", b"<!doctype html")


def user_agent_for(contact_mailto: str) -> str:
    """The honest User-Agent (C-0069: "identify honestly").

    The contact address comes from the deployment's policy file and is never
    hard-coded (ruling L-A3). It is ASCII-filtered and length-capped like
    any other header value; an address that survives none of that leaves a
    plain, still-honest UA rather than a malformed header.
    """
    contact = _ascii_header(contact_mailto or "", limit=128).strip()
    if not contact or "@" not in contact:
        return "trialerror-webfetch/1"
    return f"trialerror-webfetch/1 (+mailto:{contact})"


def _ascii_header(value: str, *, limit: int = _MAX_HEADER_VALUE_LEN) -> str:
    """Strip a header value to printable ASCII and cap its length.

    Applied to everything that leaves *and* everything that is recorded: a
    header value is attacker-controlled text on its way into a database
    column, an audit line and an operator-facing table (design §4 T3).
    """
    cleaned = _ASCII_SAFE_RE.sub("", str(value))
    return cleaned[:limit]


@dataclass
class _State:
    """Provenance accumulated across hops, attached to whatever comes out —
    a result or a refusal. A fetch that was refused at hop three still has
    to say which addresses it reached and how many bytes it sent."""

    bytes_out: int = 0
    resolved_ips: list[str] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)
    http_status: int | None = None
    final_url: NormalizedUrl | None = None
    host_rule: str | None = None
    query_stripped: bool = False
    started: float = 0.0

    def note_ips(self, addresses: Iterable[str]) -> None:
        for address in addresses:
            if address not in self.resolved_ips:
                self.resolved_ips.append(address)

    def as_context(self, elapsed_ms: int) -> dict:
        return {
            "bytes_out": self.bytes_out,
            "resolved_ips": list(self.resolved_ips),
            "redirect_chain": list(self.redirect_chain),
            "http_status": self.http_status,
            "final_url": self.final_url.url if self.final_url else None,
            "host_rule": self.host_rule,
            "query_stripped": self.query_stripped,
            "elapsed_ms": elapsed_ms,
        }


@dataclass(frozen=True)
class FetchResult:
    """A completed fetch. ``outcome`` is ``fetched`` or ``unchanged`` (304)."""

    outcome: str
    body: bytes
    content_class: str | None
    content_type: str | None
    http_status: int
    final_url: NormalizedUrl
    redirect_chain: tuple[str, ...]
    headers_subset: dict[str, str | None]
    resolved_ips: tuple[str, ...]
    bytes_out: int
    elapsed_ms: int
    sha256: str | None
    host_rule: str | None
    query_stripped: bool


@dataclass
class _RawResponse:
    status: int
    headers: http.client.HTTPMessage
    body: bytes
    bytes_out: int
    request_bytes: bytes
    content_type: str = ""
    content_class: str | None = None
    truncated: bool = False


class Fetcher:
    """Fetch one URL, politely, once.

    ``policy`` supplies the host allowlist and every cap; ``netguard``
    supplies the address policy and the pinned-IP connection; ``pacer``
    enforces the minimum interval between two requests to one host and is
    consulted before *every* socket, redirect hops and robots.txt included.

    ``_time_fn``/``_sleep_fn`` are the codebase's usual clock seams.
    """

    def __init__(
        self,
        policy: Policy,
        netguard: NetGuard,
        *,
        pacer: HostPacer | None = None,
        user_agent: str | None = None,
        _time_fn: Callable[[], float] | None = None,
        _sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.policy = policy
        self.netguard = netguard
        self._time_fn = _time_fn or time.monotonic
        self._sleep_fn = _sleep_fn or time.sleep
        self.pacer = pacer or HostPacer(
            policy.caps.min_host_interval_s, _time_fn=self._time_fn, _sleep_fn=self._sleep_fn
        )
        self.user_agent = user_agent or user_agent_for(policy.contact_mailto)

    # -- public ----------------------------------------------------------
    def resolve_target(self, url: str, *, origin: str = "operator_list") -> tuple[NormalizedUrl, HostRule]:
        """Shape-check ``url``, look its host up in the allowlist, and
        normalize it under that host's flags.

        The order matters: the host is extracted and approved *before*
        anything else happens to the URL and long before any resolver is
        asked about it, so an unapproved host never produces so much as a
        DNS query (design §4 T1).
        """
        host = peek_host(url)
        rule = self.policy.rule_for(host)
        keep_query = origin == "operator_list" or rule.keep_query
        nurl = normalize(
            url,
            allow_http=rule.allow_http,
            keep_query=keep_query,
            max_url_len=self.policy.caps.max_url_len,
            max_query_len=self.policy.caps.max_query_len,
        )
        return nurl, rule

    def fetch(
        self,
        url: str,
        *,
        kind: str = "page",
        origin: str = "operator_list",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """Fetch ``url``, following redirects under policy, and return bytes.

        Raises :class:`~trialerror.webfetch.WebFetchRefused` for every
        refusal, with the provenance gathered so far in ``exc.context`` so
        the caller can still write a complete ``result.json``.
        """
        if kind == "git":
            # A repository is cloned, not GET-ed. Routing a git manifest here
            # would fetch the rendered web UI — the exact thing the git path
            # exists to avoid — so it is refused rather than quietly served.
            raise WebFetchRefused(
                "git_url_shape",
                "a git manifest must go through gitfetch, not the HTTP fetcher",
            )
        state = _State(started=self._time_fn())
        try:
            nurl, rule = self.resolve_target(url, origin=origin)
            state.final_url = nurl
            state.host_rule = rule.source
            state.query_stripped = nurl.query_stripped
            response, final_url, final_rule = self._follow(
                nurl,
                rule,
                state=state,
                origin=origin,
                etag=etag,
                last_modified=last_modified,
                max_bytes=None,
                accept=_ACCEPT,
                raw=False,
            )
            return self._finish(response, final_url, final_rule, state)
        except WebFetchRefused as exc:
            for key, value in state.as_context(self._elapsed_ms(state)).items():
                # Never overwrite what the refusal itself recorded: a 403's
                # own http_status is more specific than the accumulator's.
                exc.context.setdefault(key, value)
            raise

    def fetch_robots(self, robots_url: NormalizedUrl) -> RobotsDocument:
        """Fetch one ``robots.txt`` through the identical policy path.

        Redirects are followed (hosts commonly bounce ``robots.txt`` to a
        canonical origin) under the same allowlist rules; the status is
        returned unjudged, because "what a 404 means" is
        :mod:`~trialerror.webfetch.robots`' decision, not the transport's.
        """
        state = _State(started=self._time_fn())
        rule = self.policy.rule_for(robots_url.host)
        response, _final_url, _rule = self._follow(
            robots_url,
            rule,
            state=state,
            origin="operator_list",
            etag=None,
            last_modified=None,
            max_bytes=min(_MAX_ROBOTS_BYTES, self.policy.caps.max_html_bytes),
            accept=_ACCEPT_ROBOTS,
            raw=True,
        )
        text = response.body.decode("utf-8", errors="replace") if response.body else ""
        return RobotsDocument(status=response.status, text=text)

    # -- redirect loop ---------------------------------------------------
    def _follow(
        self,
        nurl: NormalizedUrl,
        rule: HostRule,
        *,
        state: _State,
        origin: str,
        etag: str | None,
        last_modified: str | None,
        max_bytes: int | None,
        accept: str,
        raw: bool,
    ) -> tuple[_RawResponse, NormalizedUrl, HostRule]:
        caps = self.policy.caps
        first_host = nurl.host
        deadline = self._time_fn() + caps.total_timeout_s
        current, current_rule = nurl, rule

        for hop in range(caps.max_redirects + 1):
            attempt_result = self._attempt_with_backoff(
                current,
                state=state,
                etag=etag if current.host == first_host else None,
                last_modified=last_modified if current.host == first_host else None,
                max_bytes=max_bytes,
                accept=accept,
                deadline=deadline,
                raw=raw,
            )
            state.http_status = attempt_result.status
            state.final_url = current
            state.host_rule = current_rule.source

            if attempt_result.status not in REDIRECT_STATUSES:
                return attempt_result, current, current_rule

            location = attempt_result.headers.get("Location")
            if not location:
                # A redirect status with nowhere to go is a broken response,
                # not a hop — and it has no payload either, so there is
                # nothing to hand on.
                raise WebFetchRefused(
                    "http_error",
                    f"{current.host} answered {attempt_result.status} with no Location",
                    host=current.host,
                    http_status=attempt_result.status,
                )
            if hop >= caps.max_redirects:
                raise WebFetchRefused(
                    "redirect_limit",
                    f"more than {caps.max_redirects} redirects starting at {nurl.url}",
                    host=current.host,
                )
            current, current_rule = self._next_hop(current, _ascii_header(location), origin=origin)
            state.redirect_chain.append(current.url)

        raise WebFetchRefused(  # pragma: no cover - the loop returns or raises first
            "redirect_limit", f"redirect loop exhausted for {nurl.url}", host=nurl.host
        )

    def _next_hop(
        self, current: NormalizedUrl, location: str, *, origin: str
    ) -> tuple[NormalizedUrl, HostRule]:
        try:
            target = urljoin(current.url, location)
            scheme = (urlsplit(target).scheme or current.scheme).lower()
        except ValueError as exc:
            # A server we do not control writes ``Location``. ``urljoin`` and
            # ``urlsplit`` raise rather than return on a malformed authority
            # (``Location: http://[::1/x``), and a bare ``ValueError`` is
            # outside this package's refusal contract: it would escape
            # :meth:`fetch`, skip the ``result.json`` a refusal always
            # produces, and reach the sidecar's unmodelled-crash branch. An
            # allowlisted site must not be able to stop the fetch loop by
            # answering with a broken header.
            raise WebFetchRefused(
                "host_syntax",
                f"{current.url} redirects to an unparseable location ({exc})",
                host=current.host,
            ) from None
        if current.scheme == "https" and scheme == "http":
            raise WebFetchRefused(
                "redirect_downgrade",
                f"{current.url} redirects to plain http ({target})",
                host=current.host,
            )
        host = peek_host(target)
        try:
            rule = self.policy.rule_for(host)
        except WebFetchRefused as exc:
            if exc.reason != "host_not_allowed":
                raise
            raise WebFetchRefused(
                "redirect_off_allowlist",
                f"{current.url} redirects to {host}, which is not on the allowlist",
                host=host,
            ) from exc
        keep_query = origin == "operator_list" or rule.keep_query
        nurl = normalize(
            target,
            allow_http=rule.allow_http,
            keep_query=keep_query,
            max_url_len=self.policy.caps.max_url_len,
            max_query_len=self.policy.caps.max_query_len,
        )
        return nurl, rule

    # -- one request, with 429/5xx backoff -------------------------------
    def _attempt_with_backoff(
        self,
        nurl: NormalizedUrl,
        *,
        state: _State,
        etag: str | None,
        last_modified: str | None,
        max_bytes: int | None,
        accept: str,
        deadline: float,
        raw: bool,
    ) -> _RawResponse:
        last: _RawResponse | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            response = self._request(
                nurl,
                state=state,
                etag=etag,
                last_modified=last_modified,
                max_bytes=max_bytes,
                accept=accept,
                deadline=deadline,
                raw=raw,
            )
            last = response
            if response.status not in RETRYABLE_STATUSES or attempt == _RETRY_ATTEMPTS:
                break
            self._sleep_fn(self._backoff_delay(attempt, response.headers.get("Retry-After")))
        assert last is not None
        # Recorded before any refusal is raised, so a 403's provenance keeps
        # the status that caused it rather than a null.
        state.http_status = last.status
        if not raw:
            self._raise_for_status(last, nurl)
        return last

    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        delay = _RETRY_BASE_S * (2 ** (attempt - 1))
        parsed = self._parse_retry_after(retry_after)
        if parsed is not None:
            delay = parsed
        return max(0.0, min(delay, _MAX_RETRY_AFTER_S))

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        text = value.strip()
        try:
            return float(int(text))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        try:
            import datetime as _dt

            reference = _dt.datetime.now(_dt.timezone.utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            return max(0.0, (when - reference).total_seconds())
        except (OverflowError, ValueError):  # pragma: no cover - defensive
            return None

    def _raise_for_status(self, response: _RawResponse, nurl: NormalizedUrl) -> None:
        status = response.status
        if status < 400:
            return
        wall = self._wall_reason(response)
        if wall is not None:
            raise WebFetchRefused(
                wall,
                f"{nurl.host} answered {status} behind a wall; no evasion attempted",
                host=nurl.host,
                http_status=status,
            )
        raise WebFetchRefused(
            "http_error", f"{nurl.host} answered HTTP {status}", host=nurl.host, http_status=status
        )

    @staticmethod
    def _wall_reason(response: _RawResponse) -> str | None:
        if response.headers.get("cf-mitigated"):
            return "bot_challenge"
        if response.status not in (401, 402, 403):
            return None
        text = response.body[:_MAX_ERROR_BODY].decode("utf-8", errors="replace").lower()
        if any(marker in text for marker in _BOT_MARKERS):
            return "bot_challenge"
        if any(marker in text for marker in _PAYWALL_MARKERS):
            return "paywalled"
        if response.status == 402:
            return "paywalled"
        return None

    # -- one request -----------------------------------------------------
    def _request(
        self,
        nurl: NormalizedUrl,
        *,
        state: _State,
        etag: str | None,
        last_modified: str | None,
        max_bytes: int | None,
        accept: str,
        deadline: float,
        raw: bool,
    ) -> _RawResponse:
        caps = self.policy.caps
        self._check_deadline(deadline, nurl.host)

        resolution = self.netguard.resolve(nurl.host, nurl.port)
        state.note_ips(resolution.addresses)
        self.pacer.wait(nurl.host)
        self._check_deadline(deadline, nurl.host)

        request_bytes = self._build_request(
            nurl, accept=accept, etag=etag, last_modified=last_modified
        )
        sock = self.netguard.connect(
            host=nurl.host,
            port=nurl.port,
            scheme=nurl.scheme,
            address=resolution.primary,
            connect_timeout_s=caps.connect_timeout_s,
            read_timeout_s=caps.read_timeout_s,
        )
        try:
            try:
                sock.sendall(request_bytes)
            except socket.timeout as exc:
                raise WebFetchRefused(
                    "timeout", f"sending the request to {nurl.host} timed out", host=nurl.host
                ) from exc
            except OSError as exc:
                raise WebFetchRefused(
                    "http_error", f"sending the request to {nurl.host} failed: {exc}", host=nurl.host
                ) from exc
            state.bytes_out += len(request_bytes)

            response = http.client.HTTPResponse(sock, method="GET")
            try:
                response.begin()
            except socket.timeout as exc:
                raise WebFetchRefused(
                    "timeout", f"{nurl.host} sent no response headers in time", host=nurl.host
                ) from exc
            except (http.client.HTTPException, OSError) as exc:
                raise WebFetchRefused(
                    "http_error", f"{nurl.host} sent an unreadable response: {exc}", host=nurl.host
                ) from exc

            status = response.status
            headers = response.msg
            content_type = _ascii_header(headers.get("Content-Type") or "")
            content_class: str | None = None
            truncate = False

            if raw:
                cap = max_bytes if max_bytes is not None else _MAX_ROBOTS_BYTES
            elif status >= 400 or status in REDIRECT_STATUSES or status == 304:
                # Never a payload: read only enough to recognise a wall, and
                # stop rather than refuse — a chatty error page must not turn
                # a 403 into a `too_large`.
                cap, truncate = _MAX_ERROR_BODY, True
            else:
                # The content class is decided from the headers, *before* a
                # byte of body is read, so the cap that applies is the cap
                # for what the host says it is sending — and a media type
                # nobody will parse costs us nothing to refuse.
                media_type = content_type.split(";", 1)[0].strip().lower()
                content_class = CONTENT_TYPE_CLASSES.get(media_type)
                if content_class is None:
                    raise WebFetchRefused(
                        "content_type_disallowed",
                        f"{nurl.host} served {media_type or '(no content-type)'}; "
                        f"allowed: {sorted(CONTENT_TYPE_CLASSES)}",
                        host=nurl.host,
                        http_status=status,
                    )
                cap = (
                    max_bytes
                    if max_bytes is not None
                    else self.policy.caps.max_bytes_for(content_class)
                )

            body, was_truncated = self._read_body(
                response,
                headers,
                host=nurl.host,
                status=status,
                max_bytes=cap,
                deadline=deadline,
                truncate=truncate,
            )
            return _RawResponse(
                status=status,
                headers=headers,
                body=body,
                bytes_out=len(request_bytes),
                request_bytes=request_bytes,
                content_type=content_type,
                content_class=content_class,
                truncated=was_truncated,
            )
        finally:
            try:
                sock.close()
            except OSError:  # pragma: no cover - defensive
                pass

    def _build_request(
        self,
        nurl: NormalizedUrl,
        *,
        accept: str,
        etag: str | None,
        last_modified: str | None,
    ) -> bytes:
        """Assemble the exact bytes that go on the wire.

        Written out rather than delegated so the header set is auditable in
        one glance and assertable byte-for-byte in a test. Notice what is
        absent: no ``Cookie``, no ``Referer``, no ``Authorization``, no
        ``Origin``, and no way for a caller to add one.
        """
        default_port = 443 if nurl.scheme == "https" else 80
        host_header = nurl.host if nurl.port == default_port else f"{nurl.host}:{nurl.port}"
        lines = [
            f"GET {nurl.request_target} HTTP/1.1",
            f"Host: {host_header}",
            f"User-Agent: {self.user_agent}",
            f"Accept: {accept}",
            "Accept-Encoding: gzip",
            "Connection: close",
        ]
        if etag:
            lines.append(f"If-None-Match: {_ascii_header(etag, limit=_MAX_CONDITIONAL_LEN)}")
        if last_modified:
            lines.append(
                f"If-Modified-Since: {_ascii_header(last_modified, limit=_MAX_CONDITIONAL_LEN)}"
            )
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", errors="ignore")

    def _read_body(
        self,
        response: http.client.HTTPResponse,
        headers: http.client.HTTPMessage,
        *,
        host: str,
        status: int,
        max_bytes: int,
        deadline: float,
        truncate: bool = False,
    ) -> tuple[bytes, bool]:
        if status == 304:
            return b"", False

        encoding = (headers.get("Content-Encoding") or "").strip().lower()
        if encoding in ("", "identity"):
            decompressor = None
        elif encoding == "gzip":
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        else:
            raise WebFetchRefused(
                "content_type_disallowed",
                f"{host} used Content-Encoding {encoding!r}; only gzip was offered",
                host=host,
            )

        declared = headers.get("Content-Length")
        if declared is not None and not truncate:
            # A hint from a party with an interest in lying — so it is
            # checked *and then* ignored: an honest oversize declaration
            # saves everyone the transfer, and a dishonest small one is
            # caught by the streaming cap below. Skipped entirely when the
            # caller is going to truncate anyway (an error page is read only
            # to recognise a wall; a chatty 403 must stay a 403).
            try:
                if int(declared) > max_bytes and decompressor is None:
                    raise WebFetchRefused(
                        "too_large",
                        f"{host} declares {int(declared)} bytes, cap {max_bytes}",
                        host=host,
                    )
            except ValueError:
                pass

        out = bytearray()
        raw_total = 0
        ratio_cap = self.policy.caps.decompress_ratio_cap
        while True:
            self._check_deadline(deadline, host)
            try:
                chunk = response.read(_READ_BLOCK)
            except socket.timeout as exc:
                raise WebFetchRefused(
                    "timeout", f"reading from {host} timed out", host=host
                ) from exc
            except (http.client.HTTPException, OSError) as exc:
                raise WebFetchRefused(
                    "http_error", f"reading from {host} failed: {exc}", host=host
                ) from exc
            if not chunk:
                break
            raw_total += len(chunk)

            overflowed = False
            if decompressor is None:
                out += chunk
            else:
                allowance = max(1, max_bytes + 1 - len(out))
                try:
                    out += decompressor.decompress(chunk, allowance)
                except zlib.error as exc:
                    raise WebFetchRefused(
                        "http_error", f"{host} sent malformed gzip: {exc}", host=host
                    ) from exc
                # The allowance stopped the decompressor mid-chunk: the
                # decoded stream is already past the cap.
                overflowed = bool(decompressor.unconsumed_tail)

            # The ratio is checked *before* the absolute cap so a bomb is
            # named a bomb rather than reported as an ordinary oversize
            # body — the two call for different responses from a reader of
            # the audit log.
            if len(out) >= _RATIO_FLOOR_BYTES and raw_total > 0:
                if len(out) > raw_total * ratio_cap:
                    raise WebFetchRefused(
                        "decompress_bomb",
                        f"{host}: {raw_total} compressed bytes expanded past "
                        f"{ratio_cap}x into {len(out)} bytes",
                        host=host,
                    )
            if overflowed or len(out) > max_bytes:
                if truncate:
                    return bytes(out[:max_bytes]), True
                self._refuse_oversize(host, raw_total, len(out), max_bytes, ratio_cap)
        return bytes(out), False

    @staticmethod
    def _refuse_oversize(
        host: str, raw_total: int, decoded: int, max_bytes: int, ratio_cap: float
    ) -> None:
        if raw_total > 0 and decoded > raw_total * ratio_cap:
            raise WebFetchRefused(
                "decompress_bomb",
                f"{host}: {raw_total} compressed bytes expanded past {ratio_cap}x",
                host=host,
            )
        raise WebFetchRefused(
            "too_large", f"{host} sent more than the {max_bytes}-byte cap", host=host
        )

    def _check_deadline(self, deadline: float, host: str) -> None:
        if self._time_fn() > deadline:
            raise WebFetchRefused(
                "timeout", f"total time budget exhausted while fetching from {host}", host=host
            )

    # -- finishing -------------------------------------------------------
    def _finish(
        self,
        response: _RawResponse,
        final_url: NormalizedUrl,
        rule: HostRule,
        state: _State,
    ) -> FetchResult:
        elapsed_ms = self._elapsed_ms(state)
        headers_subset = self._headers_subset(response.headers)

        if response.status == 304:
            return FetchResult(
                outcome="unchanged",
                body=b"",
                content_class=None,
                content_type=None,
                http_status=304,
                final_url=final_url,
                redirect_chain=tuple(state.redirect_chain),
                headers_subset=headers_subset,
                resolved_ips=tuple(state.resolved_ips),
                bytes_out=state.bytes_out,
                elapsed_ms=elapsed_ms,
                sha256=None,
                host_rule=rule.source,
                query_stripped=state.query_stripped,
            )

        content_type = response.content_type
        content_class = response.content_class
        if content_class is None:  # pragma: no cover - _request refuses first
            raise WebFetchRefused(
                "content_type_disallowed",
                f"{final_url.host} served an unusable content type",
                host=final_url.host,
            )
        self._check_magic(response.body, content_class, final_url.host)

        cap = self.policy.caps.max_bytes_for(content_class)
        if len(response.body) > cap:
            raise WebFetchRefused(
                "too_large",
                f"{final_url.host} sent {len(response.body)} bytes of {content_class}, cap {cap}",
                host=final_url.host,
            )

        return FetchResult(
            outcome="fetched",
            body=response.body,
            content_class=content_class,
            content_type=content_type,
            http_status=response.status,
            final_url=final_url,
            redirect_chain=tuple(state.redirect_chain),
            headers_subset=headers_subset,
            resolved_ips=tuple(state.resolved_ips),
            bytes_out=state.bytes_out,
            elapsed_ms=elapsed_ms,
            sha256=hashlib.sha256(response.body).hexdigest(),
            host_rule=rule.source,
            query_stripped=state.query_stripped,
        )

    @staticmethod
    def _check_magic(body: bytes, content_class: str, host: str) -> None:
        """Sniff the first KiB and refuse a *positive* contradiction.

        Only a positive identification of a different class counts: PDF
        magic under an HTML declaration, or an HTML marker under a PDF one.
        The absence of a marker is not evidence — plenty of legitimate HTML
        starts with a comment, a BOM or a bare ``<div>`` — and refusing on
        absence would turn a heuristic into an outage.
        """
        head = body[:1024]
        lowered = head.lower()
        looks_pdf = head.startswith(b"%PDF-")
        looks_html = any(marker in lowered for marker in _HTML_MARKERS)
        if content_class == "pdf" and not looks_pdf and looks_html:
            raise WebFetchRefused(
                "magic_mismatch", f"{host} declared PDF but sent HTML", host=host
            )
        if content_class in ("html", "text") and looks_pdf:
            raise WebFetchRefused(
                "magic_mismatch",
                f"{host} declared {content_class} but sent a PDF",
                host=host,
            )

    @staticmethod
    def _headers_subset(headers: http.client.HTTPMessage) -> dict[str, str | None]:
        from trialerror.webfetch.protocol import HEADERS_SUBSET_KEYS

        subset: dict[str, str | None] = {}
        for key in HEADERS_SUBSET_KEYS:
            value = headers.get(key)
            subset[key] = _ascii_header(value) if value is not None else None
        return subset

    def _elapsed_ms(self, state: _State) -> int:
        return max(0, int((self._time_fn() - state.started) * 1000))
