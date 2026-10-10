# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from sandbox_science.policy.hosts import (
    HostKind,
    HostPattern,
    floor_reason,
    is_public,
    normalize_host,
    parse_ip,
)


def as_destination(host: str) -> str:
    """Names as policy and requests normalize them; addresses as a resolver returns them."""
    return host if parse_ip(host) is not None else normalize_host(host)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.ORG", "example.org"),
        ("example.org.", "example.org"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("1.2.3.4", "1.2.3.4"),
        ("[::1]", "::1"),
        ("2001:DB8::1", "2001:db8::1"),
    ],
)
def test_normalize_host(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "bücher.example",  # must be IDNA-encoded
        "2130706433",  # inet_aton forms of 127.0.0.1
        "0x7f.1",
        "0x7f000001",
        "127.1",
        "010.0.0.1",
        "foo.123",  # a numeric last label is never a DNS name
        "a..b",
        "-a.example",
        "a-.example",
        "exa mple.org",
        "example.org:443",
        "*.example.org",
        "[example.org]",
        "[1.2.3.4]",
        "::1]",
        "::ffff:127.0.0.1",  # IPv4-mapped: write the IPv4 address
        "::FFFF:1.2.3.4",
        "64:ff9b::a00:1",  # NAT64
        "2002:a00:1::",  # 6to4
        "fe80::1%eth0",  # zone ID
        "2606:4700:4700::1111%1",
        "a" * 64 + ".org",
    ],
)
def test_normalize_host_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="host"):
        normalize_host(raw)


@pytest.mark.parametrize(
    "host",
    ["example.org", "a.b.example.com", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"],
)
def test_public_destinations(host: str) -> None:
    assert is_public(as_destination(host))


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",  # shared address space (CGNAT)
        "203.0.113.5",  # documentation
        "198.18.0.1",  # benchmarking
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "fc00::1",  # unique local
        "ff02::1",  # multicast
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2002:a00:1::",  # 6to4 of 10.0.0.1
        "nas.local",
        "db.internal",
        "printer.home.arpa",
        "router.lan",
        "x.corp",
        "api.default.svc",
        "foo.test",
        "intranet",  # single label: completed by local search domains
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
    ],
)
def test_non_public_destinations(host: str) -> None:
    assert not is_public(as_destination(host))


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.8.9.10",
        "::1",
        "::ffff:127.0.0.1",
        "::7f00:1",  # IPv4-compatible 127.0.0.1
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "0.0.0.0",  # noqa: S104 - a destination under test, not a bind address
        "::",
        "169.254.169.254",
        "169.254.1.1",
        "169.254.170.2",
        "fd00:ec2::254",
        "100.100.100.200",
        "168.63.129.16",
        "fe80::1",
        "fe80::1%eth0",
        "localhost",
        "app.localhost",
        "metadata",
        "metadata.google.internal",
        "kubernetes.default.svc",
        "kubernetes.default.svc.cluster.local",
    ],
)
def test_floor_destinations(host: str) -> None:
    assert floor_reason(as_destination(host)) is not None


@pytest.mark.parametrize("host", ["example.org", "10.0.0.1", "8.8.8.8", "nas.local"])
def test_not_floor_destinations(host: str) -> None:
    assert floor_reason(as_destination(host)) is None


def test_host_pattern_kinds() -> None:
    assert HostPattern.parse("*").kind is HostKind.ANY
    wildcard = HostPattern.parse("*.Example.org.")
    assert (wildcard.kind, wildcard.value, wildcard.labels) == (HostKind.WILDCARD, "example.org", 2)
    exact = HostPattern.parse("GitHub.com")
    assert (exact.kind, exact.value, exact.labels) == (HostKind.EXACT, "github.com", 0)
    assert str(wildcard) == "*.example.org"
    assert str(HostPattern.parse("*")) == "*"


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        ("*.example.org", "a.example.org", True),
        ("*.example.org", "a.b.example.org", True),
        ("*.example.org", "example.org", False),
        ("*.example.org", "badexample.org", False),
        ("example.org", "example.org", True),
        ("example.org", "a.example.org", False),
        ("*", "anything.example", True),
        ("10.0.0.1", "10.0.0.1", True),
    ],
)
def test_host_pattern_matches(pattern: str, host: str, expected: bool) -> None:
    assert HostPattern.parse(pattern).matches(host) is expected


@pytest.mark.parametrize(
    "pattern", ["*.1.2.3.4", "foo*.example.org", "*.", "**", "*.*.example.org"]
)
def test_host_pattern_rejects(pattern: str) -> None:
    with pytest.raises(ValueError, match="host"):
        HostPattern.parse(pattern)
