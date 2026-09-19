"""Address policy and pinned-IP connections — the second SSRF layer.

Design §4, threat T1 ("DNS → private, rebinding"). :mod:`.urlcheck` decided
the URL's *shape* without touching the network; this module decides what the
name it carries is allowed to resolve to, and then makes sure the connection
actually goes to the address that was checked.

The order is the whole design:

1. The host is already known to be on the allowlist (the caller checked;
   this module never resolves a name that policy has not approved — an
   unknown host must not even produce a DNS query, because the query itself
   travels to a resolver an attacker could be running).
2. Resolve **once**. Validate **every** answer against
   :data:`BLOCKED_NETS`; one bad answer refuses the whole name
   (``ip_private``). AAAA-only refuses as ``ipv6_unsupported`` — the sidecar's
   network has IPv6 disabled, so pretending otherwise would only produce a
   confusing timeout.
3. Connect to a **validated address**, not to the name. There is no second
   resolution anywhere in the path, so there is no window for a DNS rebind
   between the check and the connection. TLS still verifies the *hostname*
   (``server_hostname`` + ``check_hostname``), so pinning the address costs
   no certificate strength.

Third layer, outside this file: the sidecar's own network namespace drops
every private/special destination in the kernel, so a bug here still does not
reach the metadata service or the LAN (design §3.2). Two independent layers
was the requirement; this is one of them.

``allow_loopback`` is a **constructor argument only**. It exists so tests can
point the fetcher at a local ``http.server`` fixture. It is deliberately not
readable from any configuration file — the fail-closed rule in design §4
("Config misuse") is that no file the deployment mounts can turn the address
policy off, and the test for that greps the policy loader for this very name.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from trialerror.webfetch import WebFetchRefused

__all__ = [
    "BLOCKED_NETS",
    "Resolution",
    "NetGuard",
]

#: Destinations no fetch may ever reach, mirroring the ipset the sidecar's
#: boot firewall installs (design §3.2) so the two layers agree by
#: construction rather than by comment.
BLOCKED_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # carrier-grade NAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local — the cloud metadata service lives here
        "172.16.0.0/12",  # RFC 1918 (also the usual container bridge range)
        "192.0.0.0/24",  # IETF protocol assignments
        "192.168.0.0/16",  # RFC 1918 — the operator's LAN
        "198.18.0.0/15",  # benchmarking
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "::1/128",  # IPv6 loopback
        "fc00::/7",  # IPv6 unique-local
        "fe80::/10",  # IPv6 link-local
        "::/128",  # unspecified
        "::ffff:0:0/96",  # IPv4-mapped IPv6
        "64:ff9b::/96",  # NAT64
    )
)

#: Addresses exempted when ``allow_loopback=True`` (tests only).
_LOOPBACK_NETS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


@dataclass(frozen=True)
class Resolution:
    """The outcome of one name lookup: the addresses that passed."""

    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def primary(self) -> str:
        return self.addresses[0]


def _addresses_from_getaddrinfo(host: str, port: int) -> list[tuple[int, str]]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [(family, sockaddr[0]) for family, _t, _p, _c, sockaddr in infos]


class NetGuard:
    """Resolve, validate, and connect to a validated address.

    ``resolver`` returns ``[(family, address_string), ...]`` — the shape
    :func:`socket.getaddrinfo` yields once the parts nothing here needs are
    dropped. Tests inject one and assert that a refused input never reaches
    ``socket_factory`` at all.
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str, int], Iterable[tuple[int, str]]] | None = None,
        socket_factory: Callable[[str, int, float], socket.socket] | None = None,
        ssl_context_factory: Callable[[], ssl.SSLContext] | None = None,
        allow_loopback: bool = False,
        allow_ipv6: bool = False,
    ) -> None:
        self._resolver = resolver or _addresses_from_getaddrinfo
        self._socket_factory = socket_factory or self._default_socket_factory
        self._ssl_context_factory = ssl_context_factory or ssl.create_default_context
        self.allow_loopback = bool(allow_loopback)
        self.allow_ipv6 = bool(allow_ipv6)

    # -- address policy --------------------------------------------------
    def is_blocked(self, address: str) -> bool:
        try:
            addr = ipaddress.ip_address(address)
        except ValueError:
            return True  # anything unparseable is not a destination we will use
        if self.allow_loopback and any(addr in net for net in _LOOPBACK_NETS):
            return False
        if any(addr in net for net in BLOCKED_NETS):
            return True
        # Belt and braces: the CIDR list above is the firewall's list, and
        # the attribute checks below are the standard library's opinion of
        # the same question. Either one refusing is enough.
        return bool(
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        )

    def check_address(self, address: str, *, host: str = "") -> None:
        if self.is_blocked(address):
            raise WebFetchRefused(
                "ip_private",
                f"{host or 'host'} resolves to {address}, which is private/special and refused",
                host=host,
                address=address,
            )

    # -- resolution ------------------------------------------------------
    def resolve(self, host: str, port: int) -> Resolution:
        """One lookup, every answer validated. No socket is opened here."""
        try:
            answers = list(self._resolver(host, port))
        except socket.gaierror as exc:
            raise WebFetchRefused("dns_failed", f"{host}: {exc}", host=host) from exc
        except OSError as exc:  # pragma: no cover - defensive
            raise WebFetchRefused("dns_failed", f"{host}: {exc}", host=host) from exc
        if not answers:
            raise WebFetchRefused("dns_failed", f"{host}: no addresses returned", host=host)

        # Validate EVERY answer, both families, before picking one. A name
        # that answers with one public and one private address is refused
        # outright: the private answer is the interesting one, and which of
        # the two a later connection would have used is not a question worth
        # depending on.
        for _family, address in answers:
            self.check_address(address, host=host)

        v4 = [a for f, a in answers if f == socket.AF_INET]
        v6 = [a for f, a in answers if f == socket.AF_INET6]
        if v4:
            return Resolution(host=host, port=port, addresses=tuple(v4))
        if v6 and self.allow_ipv6:
            return Resolution(host=host, port=port, addresses=tuple(v6))
        if v6:
            raise WebFetchRefused(
                "ipv6_unsupported",
                f"{host} has AAAA records only; this network has IPv6 disabled",
                host=host,
            )
        # Non-INET families (AF_UNIX and friends) never reach a fetch.
        raise WebFetchRefused("dns_failed", f"{host}: no usable A record", host=host)

    # -- connection ------------------------------------------------------
    @staticmethod
    def _default_socket_factory(address: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((address, port), timeout=timeout)

    def connect(
        self,
        *,
        host: str,
        port: int,
        scheme: str,
        address: str,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> socket.socket:
        """Open a connection to ``address`` (already validated) for ``host``.

        The address is re-checked here even though the caller resolved it a
        moment ago: this is the last line before a socket exists, and the
        cost of checking twice is a dictionary lookup.
        """
        self.check_address(address, host=host)
        try:
            sock = self._socket_factory(address, port, connect_timeout_s)
        except socket.timeout as exc:
            raise WebFetchRefused("timeout", f"connect to {host} timed out", host=host) from exc
        except OSError as exc:
            raise WebFetchRefused(
                "dns_failed", f"connect to {host} ({address}) failed: {exc}", host=host
            ) from exc
        try:
            sock.settimeout(read_timeout_s)
            if scheme == "https":
                context = self._ssl_context_factory()
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                # server_hostname is the NAME, not the pinned address: the
                # certificate must still be valid for the host the operator
                # approved, so pinning the address adds a check rather than
                # replacing one.
                sock = context.wrap_socket(sock, server_hostname=host)
                sock.settimeout(read_timeout_s)
        except ssl.SSLError as exc:
            sock.close()
            raise WebFetchRefused("http_error", f"TLS failure for {host}: {exc}", host=host) from exc
        except socket.timeout as exc:
            sock.close()
            raise WebFetchRefused("timeout", f"TLS handshake with {host} timed out", host=host) from exc
        except OSError as exc:
            sock.close()
            raise WebFetchRefused("http_error", f"connection to {host} failed: {exc}", host=host) from exc
        return sock

    # -- convenience -----------------------------------------------------
    def resolved_ips(self, resolutions: Sequence[Resolution]) -> list[str]:
        """Flatten several resolutions (one per redirect hop) for the
        provenance record's ``resolved_ips`` field, order preserved,
        duplicates dropped."""
        seen: dict[str, None] = {}
        for res in resolutions:
            for address in res.addresses:
                seen.setdefault(address, None)
        return list(seen)
