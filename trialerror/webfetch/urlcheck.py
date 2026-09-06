"""Syntactic URL policy — the first of two independent SSRF layers, and the
*only* one that runs on both sides of the trust boundary.

Design §4, threat T1 ("scheme/encoding tricks"): ``urlcheck.py`` is shared by
the enqueue path (inside the research container, so a bad URL fails fast with
the same reason the sidecar would have given) and by the sidecar itself,
which re-runs it on the original URL and again on **every redirect hop**. The
research container's copy is advisory — it can be edited by anything running
there; the sidecar's copy is baked into the image (design §4 T8) and is the
one that decides.

Everything here is pure: no DNS, no sockets, no filesystem. That is the
point — a refusal issued from this module is a refusal that cost the remote
world nothing and told an attacker nothing.

**The host must be a DNS name.** Not "must not be a private IP" — *must not
be an IP literal at all*, in any encoding. Address-shaped hosts are refused
before resolution (``ip_literal``); whether a *resolved* address is
acceptable is :mod:`trialerror.webfetch.netguard`'s question, and it can only
be asked honestly once a name has been looked up. This closes the whole
family of literal encodings at once (``0x7f000001``, ``2130706433``,
``127.1``, ``0177.0.0.1``, ``[::ffff:127.0.0.1]``) instead of enumerating
them, and it costs nothing real: the operator's allowlist holds names.

Refusals raise :class:`~trialerror.webfetch.WebFetchRefused` with a reason
from the closed vocabulary. General URL-shape violations (control characters,
an unparseable authority, an empty host) report ``host_syntax`` — the
vocabulary's shape reason — rather than inventing a new one.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from trialerror.webfetch import WebFetchRefused

__all__ = [
    "DEFAULT_MAX_URL_LEN",
    "DEFAULT_MAX_QUERY_LEN",
    "RESERVED_SUFFIXES",
    "RESERVED_HOSTS",
    "TRACKING_PARAM_PREFIXES",
    "TRACKING_PARAMS",
    "NormalizedUrl",
    "normalize",
    "peek_host",
    "same_host",
]

DEFAULT_MAX_URL_LEN = 2048
DEFAULT_MAX_QUERY_LEN = 512

#: Suffixes that never name a public host. Verbatim from design §4 T1.
#: ``.arpa`` subsumes ``.home.arpa`` and the reverse-DNS zones; both are
#: listed because the design lists both and a reader should not have to
#: derive the containment.
RESERVED_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home.arpa",
    ".arpa",
    ".fritz.box",
)

#: Single-label hosts that are refused by name as well as by shape.
RESERVED_HOSTS: frozenset[str] = frozenset({"localhost"})

#: Query parameters dropped when computing ``url_norm`` (design §5, dedup):
#: campaign/click identifiers that change per referral and would otherwise
#: make one page look like many.
TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)
TRACKING_PARAMS: frozenset[str] = frozenset({"fbclid", "gclid"})

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_HEX_HOST_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")
_ALL_DIGITS_RE = re.compile(r"^[0-9]+$")
#: RFC 3986 unreserved set — the bytes a percent-escape is *decoded* back to
#: when normalizing a path, so ``%7E`` and ``~`` are one URL, not two.
_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
#: Characters legal *inside* one path segment (RFC 3986 ``pchar`` minus the
#: percent, which is handled separately). Anything else is escaped; a slash
#: is absent on purpose, because a segment by definition contains none.
_PATH_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@"
)
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


@dataclass(frozen=True)
class NormalizedUrl:
    """The result of :func:`normalize`.

    ``url`` is what will actually be requested (query already stripped if
    policy said so). ``url_norm`` is the dedup key — the same URL with the
    fragment gone, the default port gone, tracking parameters gone and the
    path percent-normalized. They are usually equal; ``url_norm`` is the one
    that goes in a UNIQUE index.
    """

    url: str
    url_norm: str
    scheme: str
    host: str
    port: int
    path: str
    query: str
    query_stripped: bool

    @property
    def origin(self) -> str:
        """``scheme://host[:port]`` with the default port omitted — the key
        robots caching and per-host pacing are keyed on."""
        default = 443 if self.scheme == "https" else 80
        hostpart = self.host if self.port == default else f"{self.host}:{self.port}"
        return f"{self.scheme}://{hostpart}"

    @property
    def request_target(self) -> str:
        """The origin-form request target (``/path?query``) for HTTP/1.1."""
        return f"{self.path}?{self.query}" if self.query else self.path


def _refuse(reason: str, detail: str, **context: object) -> WebFetchRefused:
    return WebFetchRefused(reason, detail, **context)


def _check_printable(url: str) -> None:
    if not isinstance(url, str) or not url:
        raise _refuse("host_syntax", "empty URL")
    for ch in url:
        code = ord(ch)
        if code <= 0x20 or code == 0x7F:
            raise _refuse(
                "host_syntax",
                f"URL contains a control character or space at offset {url.index(ch)}",
            )


def _check_host(host: str) -> str:
    """Validate a hostname and return it canonicalized (lowercase, no root dot)."""
    if not host:
        raise _refuse("host_syntax", "URL has no host")
    if host.startswith("["):
        # Belt and braces for a caller that hands this function a raw
        # authority. It is NOT the path a bracketed URL takes: ``urlsplit``
        # strips the brackets, so ``https://[::1]/`` arrives here as ``::1``
        # and is refused two checks below by the ``ipaddress.ip_address``
        # probe (which also covers the IPv4-mapped ``[::ffff:127.0.0.1]``
        # spelling). Kept because :func:`_check_host` is the module's one
        # host gate and a future caller may not go through ``urlsplit``.
        raise _refuse("ip_literal", f"IPv6 literal host: {host}")
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        # Deliberately no IDNA transcoding here (design §4 T1: "punycode only
        # if the allowlist entry is punycode"). Transcoding would mean two
        # spellings of one host, one of which the operator never approved.
        raise _refuse(
            "host_syntax", "non-ASCII host; supply the punycode (xn--) form the allowlist holds"
        ) from None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]  # the root label is not part of the allowlist key
    if not host:
        raise _refuse("host_syntax", "URL has no host")
    if len(host) > 253:
        raise _refuse("host_syntax", f"host longer than 253 characters ({len(host)})")

    if _HEX_HOST_RE.match(host) or _ALL_DIGITS_RE.match(host):
        raise _refuse("ip_literal", f"address-shaped host: {host}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _refuse("ip_literal", f"IP literal host: {host}")

    labels = host.split(".")
    if len(labels) < 2:
        if host in RESERVED_HOSTS:
            raise _refuse("host_reserved_suffix", f"reserved host: {host}")
        raise _refuse("host_syntax", f"single-label host is not a public FQDN: {host}")
    if _ALL_DIGITS_RE.match(labels[-1]):
        # A numeric top label is never a real TLD; it is a dotted-decimal
        # address in disguise (127.1, 0177.0.0.1, 1.2.3.4).
        raise _refuse("ip_literal", f"address-shaped host: {host}")
    for label in labels:
        if not _LABEL_RE.match(label):
            raise _refuse("host_syntax", f"invalid DNS label {label!r} in host {host}")

    for suffix in RESERVED_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            raise _refuse("host_reserved_suffix", f"reserved suffix {suffix} in host {host}")
    if host in RESERVED_HOSTS:
        raise _refuse("host_reserved_suffix", f"reserved host: {host}")
    return host


def _normalize_segment(segment: str) -> str:
    """RFC 3986 §6.2.2 normalization of one path segment.

    Two rules, in order: a percent-escape of an *unreserved* byte is decoded
    (so ``%7E`` and ``~`` are one URL and one dedup key), and every other
    escape is kept, upper-cased. Characters already legal in a path stay
    literal; anything else — a space, a non-ASCII byte — is escaped.

    Splitting on ``/`` happens *before* this runs, and that is what keeps the
    two meanings of a slash apart: a literal ``/`` is a separator and
    survives untouched, while ``%2F`` inside a segment decodes to a byte
    that is not unreserved and is therefore re-escaped — never promoted to a
    separator, which is the path-traversal trick this ordering closes.
    """

    def _decode_unreserved(match: re.Match) -> str:
        byte = int(match.group(1), 16)
        if byte in _UNRESERVED:
            return chr(byte)
        return "%" + match.group(1).upper()

    partly = _PERCENT_RE.sub(_decode_unreserved, segment)

    out: list[str] = []
    index = 0
    while index < len(partly):
        char = partly[index]
        if char == "%" and _PERCENT_RE.match(partly, index):
            out.append(partly[index : index + 3].upper())
            index += 3
            continue
        if char in _PATH_ALLOWED:
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        index += 1
    return "".join(out)


def _normalize_path(path: str) -> str:
    """Percent-normalize every segment and remove dot segments (RFC 3986)."""
    if not path:
        return "/"
    raw_segments = [_normalize_segment(segment) for segment in path.split("/")]

    # Dot segments are compared *after* the unreserved decoding above, so the
    # encoded spellings (%2E, %2E%2E) are plain "." / ".." by now and cannot
    # slip past this loop.
    trailing_slash = raw_segments[-1] in (".", "..")
    segments: list[str] = []
    for segment in raw_segments:
        if segment == ".":
            continue
        if segment == "..":
            # Never pop the leading empty segment: ``/../..`` is ``/``, not a
            # path that has climbed out of the site root.
            if len(segments) > 1:
                segments.pop()
            continue
        segments.append(segment)
    if trailing_slash and (not segments or segments[-1] != ""):
        segments.append("")
    result = "/".join(segments)
    if not result.startswith("/"):
        result = "/" + result
    return result or "/"


def _strip_tracking(query: str) -> str:
    if not query:
        return ""
    kept: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key in TRACKING_PARAMS or any(key.startswith(p) for p in TRACKING_PARAM_PREFIXES):
            continue
        kept.append(pair)
    return "&".join(kept)


def _split(url: str, *, allow_http: bool) -> tuple[str, str, int, str, str]:
    """Shared shape check. Returns ``(scheme, host, port, path, query)``."""
    _check_printable(url)
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        # ``urlsplit`` raises rather than returning for a handful of
        # malformed authorities -- an unbalanced bracket (``https://[::1/``)
        # is the reachable one. That exception is NOT in this package's
        # contract: escaping here would carry a bare ``ValueError`` past
        # every refusal path, out of ``Fetcher.fetch``, and into the
        # sidecar's unmodelled-crash branch, where a single hand-written
        # manifest could replay it forever. A URL the standard library
        # cannot even split is a shape refusal like any other.
        raise _refuse("host_syntax", f"unparseable URL authority: {exc}") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise _refuse("scheme_not_allowed", f"scheme {scheme or '(none)'!r} is not http(s)")
    if scheme == "http" and not allow_http:
        raise _refuse(
            "scheme_not_allowed",
            "plain http requires the host's 'http' flag in the allowlist",
        )
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _refuse("userinfo_present", "URL carries userinfo before the host")
    try:
        port = parts.port
    except ValueError:
        raise _refuse("port_not_allowed", "unparseable port in URL authority") from None
    host = _check_host(parts.hostname or "")
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in (80, 443):
        raise _refuse("port_not_allowed", f"port {port} is not 80 or 443")
    if scheme == "https" and port == 80:
        raise _refuse("port_not_allowed", "https on port 80 is not an allowed combination")
    if scheme == "http" and port == 443:
        raise _refuse("port_not_allowed", "http on port 443 is not an allowed combination")
    return scheme, host, port, parts.path, parts.query


def peek_host(url: str) -> str:
    """Validate the URL's *shape* and return its canonical host.

    Used by the sidecar before the allowlist lookup, because whether plain
    ``http`` is acceptable is a per-host flag — which cannot be read until
    the host is known. ``allow_http=True`` here therefore decides nothing:
    :func:`normalize` is still called afterwards with the real flag, and it
    is that call whose result is fetched.
    """
    return _split(url, allow_http=True)[1]


def normalize(
    url: str,
    *,
    allow_http: bool = False,
    keep_query: bool = True,
    max_url_len: int = DEFAULT_MAX_URL_LEN,
    max_query_len: int = DEFAULT_MAX_QUERY_LEN,
) -> NormalizedUrl:
    """Validate and canonicalize ``url``.

    ``keep_query=False`` drops the query string entirely — the control for
    agent-origin URLs on hosts without the ``keep-query`` flag (design §4 T2:
    the query string is the widest field an injected agent could write into,
    and most article URLs do not need one).

    Raises :class:`~trialerror.webfetch.WebFetchRefused`. Length limits are
    measured on the URL *as supplied*, and the query limit is checked first,
    so an oversized query reports ``query_too_long`` rather than being
    masked by ``url_too_long``.
    """
    scheme, host, port, path, query = _split(url, allow_http=allow_http)
    if len(query) > max_query_len:
        raise _refuse(
            "query_too_long", f"query is {len(query)} bytes, limit {max_query_len}", host=host
        )
    if len(url) > max_url_len:
        raise _refuse("url_too_long", f"URL is {len(url)} bytes, limit {max_url_len}", host=host)

    query_stripped = bool(query) and not keep_query
    effective_query = "" if query_stripped else query
    norm_path = _normalize_path(path)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"

    request_url = urlunsplit((scheme, netloc, norm_path, effective_query, ""))
    url_norm = urlunsplit((scheme, netloc, norm_path, _strip_tracking(effective_query), ""))
    return NormalizedUrl(
        url=request_url,
        url_norm=url_norm,
        scheme=scheme,
        host=host,
        port=port,
        path=norm_path,
        query=effective_query,
        query_stripped=query_stripped,
    )


def same_host(a: NormalizedUrl, b: NormalizedUrl) -> bool:
    """True when two normalized URLs share a host.

    Conditional-request headers (``If-None-Match``/``If-Modified-Since``) are
    only ever replayed within one host — an ETag is a value the *previous*
    host chose, and sending it elsewhere would leak it (design §4 T2).
    """
    return a.host == b.host
