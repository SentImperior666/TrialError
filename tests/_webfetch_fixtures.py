"""Shared fixtures for the ``trialerror.webfetch`` suite.

Two kinds of stand-in, and the distinction matters:

* :class:`LocalSite` is a **real** ``http.server`` on ``127.0.0.1``. Redirect
  chains, a gzip bomb, a slow response, a 304 and a robots.txt are served by
  an actual HTTP server over an actual socket, so the fetcher's response
  parsing, streaming caps and timeouts are exercised rather than mimed. No
  test in this suite reaches the internet.
* :class:`FakeSocket` is a canned byte stream, used only where the *request*
  is the thing under test — asserting the exact bytes that go on the wire is
  not something a real server can do for you.

The bridge between "the policy approves ``test.example``" and "the fixture
listens on an ephemeral port" is :func:`loopback_netguard`: a resolver that
answers ``127.0.0.1`` and a socket factory that dials the fixture's real
port. Both are constructor arguments on :class:`~trialerror.webfetch.netguard.
NetGuard` — the design's rule is that no *mounted file* can weaken the
address policy, and nothing here is a file.
"""

from __future__ import annotations

import gzip
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable

from trialerror.webfetch.netguard import NetGuard
from trialerror.webfetch.policy import (
    ALLOWED_HOSTS_FILENAME,
    POLICY_FILENAME,
    ROBOTS_OVERRIDES_FILENAME,
    Policy,
)

__all__ = [
    "Route",
    "LocalSite",
    "FakeSocket",
    "write_policy",
    "load_policy",
    "loopback_netguard",
    "gzip_bomb",
]

#: Hosts the fixtures approve. ``http`` because the local server speaks plain
#: HTTP; ``git`` on the repo host so the clone path has an approved home.
DEFAULT_HOSTS = "test.example http\nother.example http\nrepos.example http git\n"
DEFAULT_TOML = 'contact_mailto = "ops@example.com"\nmin_host_interval_s = 0\n'


def write_policy(
    root: Path, *, hosts: str = DEFAULT_HOSTS, toml: str = DEFAULT_TOML, overrides: str | None = None
) -> Path:
    """Write a policy directory and return its path."""
    directory = Path(root) / "policy"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ALLOWED_HOSTS_FILENAME).write_text(hosts, encoding="utf-8")
    (directory / POLICY_FILENAME).write_text(toml, encoding="utf-8")
    if overrides is not None:
        (directory / ROBOTS_OVERRIDES_FILENAME).write_text(overrides, encoding="utf-8")
    return directory


def load_policy(root: Path, **kwargs) -> Policy:
    return Policy.load(write_policy(root, **kwargs))


def gzip_bomb(decompressed_bytes: int) -> bytes:
    """Gzip of a long run of zeroes — small on the wire, enormous decoded."""
    return gzip.compress(b"\0" * decompressed_bytes, compresslevel=9)


@dataclass
class Route:
    """One canned response.

    ``etag`` turns on conditional handling: a request whose
    ``If-None-Match`` matches gets a 304 with no body, which is how the
    ``unchanged`` outcome is exercised end to end.
    """

    status: int = 200
    body: bytes = b""
    content_type: str | None = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0
    gzip_body: bool = False
    etag: str | None = None
    omit_content_length: bool = False
    #: Send this ``Content-Length`` instead of the body's real length. The
    #: only way to prove the pre-read cap check actually happens before the
    #: read: declare more than the cap, send almost nothing, and see which
    #: number the fetcher acted on.
    declared_length: int | None = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # A stable, recognisable Server header: the base class emits its own
    # before any route header, so a route that also set one would be
    # shadowed. Tests assert on this value instead.
    server_version = "fixture"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - silence the test run
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        site: LocalSite = self.server.site  # type: ignore[attr-defined]
        site.record(self)
        route = site.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if route.delay_s:
            time.sleep(route.delay_s)

        if route.etag and self.headers.get("If-None-Match") == route.etag:
            self.send_response(304)
            self.send_header("ETag", route.etag)
            self.end_headers()
            return

        body = gzip.compress(route.body) if route.gzip_body else route.body
        self.send_response(route.status)
        if route.content_type:
            self.send_header("Content-Type", route.content_type)
        if route.gzip_body:
            self.send_header("Content-Encoding", "gzip")
        if route.etag:
            self.send_header("ETag", route.etag)
        for key, value in route.headers.items():
            self.send_header(key, value)
        if not route.omit_content_length:
            declared = route.declared_length if route.declared_length is not None else len(body)
            self.send_header("Content-Length", str(declared))
        self.end_headers()
        if body:
            self.wfile.write(body)


class _QuietServer(ThreadingHTTPServer):
    """Never print a traceback for a client that hung up.

    Half the tests here refuse a response part-way through — that is the
    point of a cap — and the client closing early is the *expected* outcome,
    not something a reader of the test log should have to triage.
    """

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        return


class LocalSite:
    """A real HTTP server on ``127.0.0.1``, routed by path."""

    def __init__(self, routes: dict[str, Route] | None = None) -> None:
        self.routes: dict[str, Route] = dict(routes or {})
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._lock = threading.Lock()
        self._server = _QuietServer(("127.0.0.1", 0), _Handler)
        self._server.site = self  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        # A short poll interval because ``shutdown()`` waits for one: the
        # default 0.5 s would put half a second on the clock for every test
        # in this suite that starts a site.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "LocalSite":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    # -- inspection ------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def record(self, handler: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.requests.append((handler.path, dict(handler.headers.items())))

    @property
    def paths(self) -> list[str]:
        with self._lock:
            return [path for path, _headers in self.requests]

    def add(self, path: str, route: Route) -> None:
        self.routes[path] = route


def loopback_netguard(port: int, *, hosts: Iterable[str] = ()) -> NetGuard:
    """A guard that resolves the fixture hosts to loopback and dials ``port``.

    ``allow_loopback`` is a constructor argument by design (§4, "Config
    misuse"): a test can point the fetcher at a local server, and nothing a
    deployment mounts can do the same.
    """
    known = set(hosts) or {"test.example", "other.example", "repos.example"}

    def resolver(host: str, _port: int) -> list[tuple[int, str]]:
        if host in known:
            return [(socket.AF_INET, "127.0.0.1")]
        raise socket.gaierror(socket.EAI_NONAME, f"unknown test host {host}")

    def socket_factory(address: str, _port: int, timeout: float) -> socket.socket:
        # The port is the fixture's, not the URL's: the URL layer only ever
        # allows 80/443, and an ephemeral test port has no bearing on the
        # policy being exercised.
        return socket.create_connection((address, port), timeout=timeout)

    return NetGuard(resolver=resolver, socket_factory=socket_factory, allow_loopback=True)


class FakeSocket:
    """A socket that records what was sent and replays a canned response."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = bytearray()
        self.timeouts: list[float] = []
        self.closed = False
        self._stream: object | None = None

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str = "rb", *_args, **_kwargs):  # noqa: ANN001
        import io

        self._stream = io.BytesIO(self.response)
        return self._stream

    def close(self) -> None:
        self.closed = True

    @property
    def request_text(self) -> str:
        return bytes(self.sent).decode("ascii", errors="replace")

    @property
    def request_lines(self) -> list[str]:
        return self.request_text.split("\r\n")


def http_response(
    status: int = 200,
    *,
    body: bytes = b"<html><body>hello</body></html>",
    content_type: str = "text/html; charset=utf-8",
    extra: dict[str, str] | None = None,
    include_length: bool = True,
) -> bytes:
    """Assemble a canned HTTP/1.1 response for :class:`FakeSocket`."""
    reason = {200: "OK", 301: "Moved Permanently", 304: "Not Modified", 403: "Forbidden"}.get(
        status, "Status"
    )
    lines = [f"HTTP/1.1 {status} {reason}"]
    if content_type:
        lines.append(f"Content-Type: {content_type}")
    if include_length:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    return head + body
