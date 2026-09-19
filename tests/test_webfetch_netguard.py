"""Address policy and pinned-IP connections — the second SSRF layer.

The property under test throughout is not "a private address is rejected" —
it is **that no socket is opened when it is**. A refusal that happens after a
connection has been made is not a refusal, it is a log line about an SSRF
that already succeeded. So every negative case here installs a socket factory
that fails the test if it is called at all, and the resolver is counted to
prove the name is looked up exactly once — leaving no window between the
check and the connection for a DNS rebind to squeeze into.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl

import pytest

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.netguard import BLOCKED_NETS, NetGuard, Resolution


class CountingResolver:
    """A resolver that records every question it is asked."""

    def __init__(self, answers: dict[str, list[tuple[int, str]]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[tuple[int, str]]:
        self.calls.append((host, port))
        if host not in self.answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return list(self.answers[host])


def v4(*addresses: str) -> list[tuple[int, str]]:
    return [(socket.AF_INET, address) for address in addresses]


def v6(*addresses: str) -> list[tuple[int, str]]:
    return [(socket.AF_INET6, address) for address in addresses]


class ExplodingSocketFactory:
    """Fails the test if a connection is ever attempted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, address: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((address, port, timeout))
        raise AssertionError(
            f"a socket was opened to {address}:{port} — the refusal came too late to matter"
        )


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# the blocked set
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,why",
    [
        ("127.0.0.1", "loopback"),
        ("127.53.1.9", "the whole loopback /8, not just .0.1"),
        ("169.254.169.254", "the cloud metadata service"),
        ("169.254.1.1", "link-local generally"),
        ("10.1.2.3", "RFC 1918"),
        ("172.17.0.1", "the docker bridge gateway"),
        ("172.31.250.1", "the sidecar's own bridge gateway"),
        ("192.168.0.1", "a LAN router"),
        ("192.0.0.1", "IETF protocol assignments"),
        ("100.64.0.1", "carrier-grade NAT"),
        ("198.18.0.1", "benchmarking range"),
        ("224.0.0.1", "multicast"),
        ("240.0.0.1", "reserved"),
        ("0.0.0.0", "this network"),
        ("::1", "IPv6 loopback"),
        ("fc00::1", "IPv6 unique-local"),
        ("fe80::1", "IPv6 link-local"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("64:ff9b::7f00:1", "NAT64-mapped loopback"),
        ("not-an-address", "anything unparseable is not a destination"),
    ],
)
def test_blocked_addresses(address: str, why: str) -> None:
    assert NetGuard().is_blocked(address) is True, why


@pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert NetGuard().is_blocked(address) is False


def test_the_blocked_set_mirrors_the_firewall_ipset() -> None:
    """Design §3.2 installs the same list in the kernel. The two layers agree
    by construction rather than by comment, so drift here is a test failure
    rather than a silent divergence."""
    v4_nets = {str(net) for net in BLOCKED_NETS if net.version == 4}
    assert v4_nets == {
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
    }
    assert all(isinstance(net, (ipaddress.IPv4Network, ipaddress.IPv6Network)) for net in BLOCKED_NETS)


# --------------------------------------------------------------------------
# resolution: zero sockets on every refusal
# --------------------------------------------------------------------------


def test_a_private_answer_refuses_and_opens_no_socket() -> None:
    resolver = CountingResolver({"rebind.example": v4("127.0.0.1")})
    factory = ExplodingSocketFactory()
    guard = NetGuard(resolver=resolver, socket_factory=factory)

    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("rebind.example", 443)

    assert caught.value.reason == "ip_private"
    assert factory.calls == []
    assert resolver.calls == [("rebind.example", 443)]


def test_one_private_answer_poisons_the_whole_name() -> None:
    """A name answering with one public and one private address is refused
    outright. Which of the two a later connection would have picked is not a
    question worth depending on."""
    resolver = CountingResolver({"mixed.example": v4("93.184.216.34", "169.254.169.254")})
    factory = ExplodingSocketFactory()
    guard = NetGuard(resolver=resolver, socket_factory=factory)

    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("mixed.example", 443)
    assert caught.value.reason == "ip_private"
    assert factory.calls == []


def test_the_name_is_looked_up_exactly_once() -> None:
    """No second resolution anywhere in the path means no window for a
    rebind between the check and the connection."""
    resolver = CountingResolver({"good.example": v4("93.184.216.34")})
    guard = NetGuard(resolver=resolver, socket_factory=lambda a, p, t: FakeSocket())

    resolution = guard.resolve("good.example", 443)
    guard.connect(
        host="good.example",
        port=443,
        scheme="http",
        address=resolution.primary,
        connect_timeout_s=1.0,
        read_timeout_s=1.0,
    )
    assert resolver.calls == [("good.example", 443)]


def test_a_resolver_failure_is_dns_failed_not_a_crash() -> None:
    resolver = CountingResolver({})
    factory = ExplodingSocketFactory()
    guard = NetGuard(resolver=resolver, socket_factory=factory)
    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("nowhere.example", 443)
    assert caught.value.reason == "dns_failed"
    assert factory.calls == []


def test_an_empty_answer_is_dns_failed() -> None:
    guard = NetGuard(
        resolver=CountingResolver({"empty.example": []}), socket_factory=ExplodingSocketFactory()
    )
    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("empty.example", 443)
    assert caught.value.reason == "dns_failed"


def test_aaaa_only_is_refused_as_unsupported_not_as_a_timeout() -> None:
    """The sidecar's network has IPv6 disabled. Saying so is more useful than
    letting the connection hang and reporting a timeout."""
    guard = NetGuard(
        resolver=CountingResolver({"v6.example": v6("2606:4700::1111")}),
        socket_factory=ExplodingSocketFactory(),
    )
    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("v6.example", 443)
    assert caught.value.reason == "ipv6_unsupported"


def test_ipv4_wins_when_both_families_answer() -> None:
    guard = NetGuard(
        resolver=CountingResolver(
            {"dual.example": v6("2606:4700::1111") + v4("93.184.216.34")}
        )
    )
    assert guard.resolve("dual.example", 443).addresses == ("93.184.216.34",)


def test_ipv6_is_used_only_when_explicitly_enabled() -> None:
    resolver = CountingResolver({"v6.example": v6("2606:4700::1111")})
    guard = NetGuard(resolver=resolver, allow_ipv6=True)
    assert guard.resolve("v6.example", 443).primary == "2606:4700::1111"


def test_a_non_inet_family_never_reaches_a_fetch() -> None:
    """The address itself is unobjectionable; the family is not INET or
    INET6, so there is nothing here a fetch could connect over."""
    odd_family = getattr(socket, "AF_UNIX", socket.AF_UNSPEC)
    guard = NetGuard(
        resolver=CountingResolver({"odd.example": [(odd_family, "93.184.216.34")]}),
        socket_factory=ExplodingSocketFactory(),
    )
    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("odd.example", 443)
    assert caught.value.reason == "dns_failed"


# --------------------------------------------------------------------------
# the loopback seam
# --------------------------------------------------------------------------


def test_allow_loopback_is_a_constructor_argument_only() -> None:
    """Tests point the fetcher at a local ``http.server``; nothing a
    deployment mounts can reach this switch (see the grep in the policy
    tests)."""
    guard = NetGuard(resolver=CountingResolver({"local.test": v4("127.0.0.1")}), allow_loopback=True)
    assert guard.resolve("local.test", 443).primary == "127.0.0.1"


def test_allow_loopback_does_not_open_the_lan() -> None:
    """The seam is loopback, not "private". A test fixture lives on
    127.0.0.1; the operator's router does not."""
    guard = NetGuard(
        resolver=CountingResolver({"lan.test": v4("192.168.0.1")}),
        socket_factory=ExplodingSocketFactory(),
        allow_loopback=True,
    )
    with pytest.raises(WebFetchRefused) as caught:
        guard.resolve("lan.test", 443)
    assert caught.value.reason == "ip_private"


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------


def test_connect_re_checks_the_address_it_is_handed() -> None:
    """The last line before a socket exists. Checking twice costs a
    dictionary lookup; not checking costs the sandbox."""
    factory = ExplodingSocketFactory()
    guard = NetGuard(socket_factory=factory)
    with pytest.raises(WebFetchRefused) as caught:
        guard.connect(
            host="example.com",
            port=443,
            scheme="https",
            address="169.254.169.254",
            connect_timeout_s=1.0,
            read_timeout_s=1.0,
        )
    assert caught.value.reason == "ip_private"
    assert factory.calls == []


def test_connect_uses_the_validated_address_and_sets_the_read_timeout() -> None:
    opened: list[tuple[str, int, float]] = []

    def factory(address: str, port: int, timeout: float) -> socket.socket:
        opened.append((address, port, timeout))
        return FakeSocket()

    guard = NetGuard(socket_factory=factory)
    sock = guard.connect(
        host="example.com",
        port=80,
        scheme="http",
        address="93.184.216.34",
        connect_timeout_s=2.5,
        read_timeout_s=7.5,
    )
    assert opened == [("93.184.216.34", 80, 2.5)]
    assert sock.timeouts == [7.5]


def test_a_connect_timeout_is_reported_as_a_timeout() -> None:
    def factory(address: str, port: int, timeout: float) -> socket.socket:
        raise socket.timeout("too slow")

    guard = NetGuard(socket_factory=factory)
    with pytest.raises(WebFetchRefused) as caught:
        guard.connect(
            host="example.com",
            port=443,
            scheme="http",
            address="93.184.216.34",
            connect_timeout_s=1.0,
            read_timeout_s=1.0,
        )
    assert caught.value.reason == "timeout"


def test_a_refused_connection_is_reported_not_raised_as_oserror() -> None:
    def factory(address: str, port: int, timeout: float) -> socket.socket:
        raise ConnectionRefusedError("nope")

    guard = NetGuard(socket_factory=factory)
    with pytest.raises(WebFetchRefused) as caught:
        guard.connect(
            host="example.com",
            port=443,
            scheme="http",
            address="93.184.216.34",
            connect_timeout_s=1.0,
            read_timeout_s=1.0,
        )
    assert caught.value.reason == "dns_failed"


def test_tls_verifies_the_hostname_even_though_the_address_is_pinned() -> None:
    """Pinning the address adds a check rather than replacing one: the
    certificate must still be valid for the *name* the operator approved."""
    wrapped = FakeSocket()
    seen: dict[str, object] = {}

    class FakeContext:
        check_hostname = False
        verify_mode = ssl.CERT_NONE

        def wrap_socket(self, sock, server_hostname):  # noqa: ANN001
            seen["server_hostname"] = server_hostname
            seen["check_hostname"] = self.check_hostname
            seen["verify_mode"] = self.verify_mode
            return wrapped

    guard = NetGuard(
        socket_factory=lambda a, p, t: FakeSocket(), ssl_context_factory=FakeContext
    )
    result = guard.connect(
        host="example.com",
        port=443,
        scheme="https",
        address="93.184.216.34",
        connect_timeout_s=1.0,
        read_timeout_s=4.0,
    )
    assert result is wrapped
    assert seen["server_hostname"] == "example.com"
    assert seen["check_hostname"] is True
    assert seen["verify_mode"] == ssl.CERT_REQUIRED
    assert wrapped.timeouts == [4.0]


def test_a_tls_failure_closes_the_socket_and_refuses() -> None:
    raw = FakeSocket()

    class FailingContext:
        check_hostname = False
        verify_mode = ssl.CERT_NONE

        def wrap_socket(self, sock, server_hostname):  # noqa: ANN001
            raise ssl.SSLError("certificate verify failed")

    guard = NetGuard(socket_factory=lambda a, p, t: raw, ssl_context_factory=FailingContext)
    with pytest.raises(WebFetchRefused) as caught:
        guard.connect(
            host="example.com",
            port=443,
            scheme="https",
            address="93.184.216.34",
            connect_timeout_s=1.0,
            read_timeout_s=1.0,
        )
    assert caught.value.reason == "http_error"
    assert raw.closed is True


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


def test_resolved_ips_flattens_hops_in_order_without_duplicates() -> None:
    guard = NetGuard()
    hops = [
        Resolution(host="a.example", port=443, addresses=("1.1.1.1", "2.2.2.2")),
        Resolution(host="b.example", port=443, addresses=("2.2.2.2", "3.3.3.3")),
    ]
    assert guard.resolved_ips(hops) == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_resolution_primary_is_the_first_address() -> None:
    assert Resolution(host="a", port=443, addresses=("9.9.9.9", "8.8.4.4")).primary == "9.9.9.9"
