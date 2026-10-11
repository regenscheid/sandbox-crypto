# SPDX-License-Identifier: MIT
"""Upstream fetches: TLS, headers, limits, digests, validators, redirects and the rate limit,
and the capacity limits: download slots, temp space, the connection pool and deadlines."""

import asyncio
import gzip
import hashlib
import logging
import os
import ssl
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal, Protocol

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import unused_port
from yarl import URL

from sandbox_science.package_cache.adapter import Digest, Fetch, Kind
from sandbox_science.package_cache.errors import (
    BusyError,
    DigestMismatchError,
    RateLimitedError,
    RedirectError,
    TooLargeError,
    UpstreamError,
    UpstreamStatusError,
    UpstreamURLError,
)
from sandbox_science.package_cache.upstream import (
    DEFAULT_TIMEOUT,
    INDEX_CONNECTIONS,
    NEGATIVE_BODY_LIMIT,
    USER_AGENT,
    DigestMemory,
    Downloaded,
    NotModified,
    SandboxBudgets,
    SingleFlight,
    TempSpace,
    TokenBucket,
    UpstreamClient,
    UpstreamResult,
    Validators,
    check_redirect,
    check_url,
    create_session,
    pool_size,
)


class Seen(Protocol):
    """A request the test upstream received (conftest's SeenRequest)."""

    method: str
    path: str
    headers: dict[str, str]


class Upstream(Protocol):
    """The ``core_upstream`` fixture."""

    origin: str
    requests: list[Seen]

    def url(self, path: str) -> str: ...
    def set(self, path: str, body: bytes = b"", **options: object) -> None: ...
    def count(self, path: str) -> int: ...


class Tls(Protocol):
    """The ``core_tls`` fixture."""

    client: ssl.SSLContext


ClientFactory = Callable[..., UpstreamClient]
Algorithm = Literal["md5", "sha256", "sha512"]


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    path = tmp_path / "tmp"
    path.mkdir()
    return path


@pytest_asyncio.fixture
async def make_client(
    core_upstream: Upstream, core_tls: Tls, temp_dir: Path
) -> AsyncIterator[ClientFactory]:
    """Build clients for the test upstream; keyword arguments adjust them."""
    sessions: list[aiohttp.ClientSession] = []

    def make(
        *,
        rate: float = 1000.0,
        burst: int = 100,
        rate_wait: float = 10.0,
        allowed: tuple[str, ...] | None = None,
        trusted: bool = True,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
        pool: int = 100,
        temp_space: TempSpace | None = None,
        **options: object,
    ) -> UpstreamClient:
        session = create_session(
            ssl_context=core_tls.client if trusted else None,
            timeout=timeout,
            limit=pool,
        )
        sessions.append(session)
        return UpstreamClient(
            session,
            temp_dir=temp_dir,
            limiter=TokenBucket(rate, burst, max_wait=rate_wait),
            allowed=allowed if allowed is not None else (core_upstream.origin,),
            temp_space=temp_space if temp_space is not None else TempSpace(10**9),
            **options,  # pyright: ignore[reportArgumentType]
        )

    yield make
    for session in sessions:
        await session.close()


def artifact(url: str, **options: object) -> Fetch:
    return Fetch(key="k", url=url, kind=Kind.ARTIFACT, **options)  # pyright: ignore[reportArgumentType]


def metadata(url: str, **options: object) -> Fetch:
    return Fetch(key="k", url=url, kind=Kind.METADATA, **options)  # pyright: ignore[reportArgumentType]


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def download(client: UpstreamClient, fetch: Fetch, max_size: int = 1_000_000) -> Downloaded:
    result = await client.fetch(fetch, max_size=max_size)
    assert isinstance(result, Downloaded)
    return result


def close(result: UpstreamResult) -> None:
    assert isinstance(result, Downloaded)
    result.close()


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait until ``condition()`` holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


# The token bucket and single flights.


class FakeTime:
    """A clock that only moves when told to, and a sleep that records instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.gate: asyncio.Event | None = None

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.gate is not None:
            await self.gate.wait()


@pytest.mark.asyncio
async def test_the_bucket_allows_a_burst_then_paces_and_refuses_long_waits() -> None:
    fake = FakeTime()
    bucket = TokenBucket(10, 3, max_wait=0.25, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert fake.sleeps == []
    await bucket.acquire()
    await bucket.acquire()
    assert fake.sleeps == [pytest.approx(0.1), pytest.approx(0.2)]
    with pytest.raises(RateLimitedError, match="delay"):
        await bucket.acquire()
    fake.now += 0.5
    for _ in range(3):
        await bucket.acquire()
    assert len(fake.sleeps) == 2


@pytest.mark.asyncio
async def test_a_cancelled_wait_returns_its_token() -> None:
    fake = FakeTime()
    bucket = TokenBucket(1, 1, max_wait=5, clock=fake.clock, sleep=fake.sleep)
    await bucket.acquire()
    fake.gate = asyncio.Event()
    waiting = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    fake.gate = None
    await bucket.acquire()
    assert fake.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


@pytest.mark.parametrize(("rate", "burst", "wait"), [(0, 1, 1.0), (1, 0, 1.0), (1, 1, -1.0)])
def test_the_bucket_needs_a_positive_rate_and_burst(rate: float, burst: int, wait: float) -> None:
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(rate, burst, max_wait=wait)


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_run() -> None:
    flights: SingleFlight[int] = SingleFlight()
    calls = 0
    gate = asyncio.Event()

    async def operation() -> int:
        nonlocal calls
        calls += 1
        await gate.wait()
        return calls

    waiters = [asyncio.create_task(flights.run("k", operation)) for _ in range(5)]
    await asyncio.sleep(0)
    assert len(flights) == 1
    gate.set()
    assert await asyncio.gather(*waiters) == [1] * 5
    assert len(flights) == 0
    assert await flights.run("k", operation) == 2


@pytest.mark.asyncio
async def test_a_failure_reaches_every_caller() -> None:
    flights: SingleFlight[int] = SingleFlight()

    async def fail() -> int:
        await asyncio.sleep(0)
        raise ValueError("boom")

    results = await asyncio.gather(
        flights.run("k", fail), flights.run("k", fail), return_exceptions=True
    )
    assert [type(result) for result in results] == [ValueError, ValueError]


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_cancel_the_shared_run() -> None:
    flights: SingleFlight[str] = SingleFlight()
    gate = asyncio.Event()

    async def operation() -> str:
        await gate.wait()
        return "done"

    first = asyncio.create_task(flights.run("k", operation))
    second = asyncio.create_task(flights.run("k", operation))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    assert await second == "done"


@pytest.mark.asyncio
async def test_close_cancels_running_operations() -> None:
    flights: SingleFlight[int] = SingleFlight()
    started = asyncio.Event()

    async def forever() -> int:
        started.set()
        await asyncio.Event().wait()
        return 0

    waiter = asyncio.create_task(flights.run("k", forever))
    await started.wait()
    await flights.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(flights) == 0


# Which URLs may be fetched.


@pytest.mark.parametrize(
    "url",
    [
        "https://r.test/x",
        "https://r.test/@scope%2Fname",
        "https://r.test/a/b?q=1",
        "https://r.test/npm/x.tgz",
        "https://R.Test/x",
    ],
)
def test_urls_below_a_configured_upstream_are_accepted(url: str) -> None:
    check_url(url, allowed=("https://r.test", "https://other.test/npm"))


def test_accepted_urls_are_left_encoded() -> None:
    assert str(check_url("https://r.test/@scope%2Fname")) == "https://r.test/@scope%2Fname"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        pytest.param("http://r.test/x", None, id="http"),
        pytest.param("https://user:pw@r.test/x", None, id="credentials"),
        pytest.param("https://r.test/x#fragment", None, id="fragment"),
        pytest.param("https://r.test/a/../b", None, id="dot-dot"),
        pytest.param("https://r.test/a/%2e%2e/b", None, id="encoded-dot-dot"),
        pytest.param("https://r.test/a/%2E%2e%2Fb", None, id="encoded-slash"),
        pytest.param("https://r.test/a/./b", None, id="dot"),
        pytest.param("https://r.test/a%5C..%5Cb", None, id="backslash-dots"),
        pytest.param("https://r.test/a b", None, id="space"),
        pytest.param("https://r.test/ä", None, id="non-ascii"),
        pytest.param("https://r.test/a\\b", None, id="backslash"),
        pytest.param("https://r.test/a|b", None, id="pipe"),
        pytest.param("https:///x", None, id="no-host"),
        pytest.param("https://other.test/x", ("https://r.test",), id="other-host"),
        pytest.param("https://r.test/npmx/y", ("https://r.test/npm",), id="prefix-boundary"),
        pytest.param("https://r.test:8443/x", ("https://r.test",), id="other-port"),
    ],
)
def test_other_urls_are_refused(url: str, allowed: tuple[str, ...] | None) -> None:
    with pytest.raises(UpstreamURLError):
        check_url(url, allowed=allowed)


@pytest.mark.asyncio
async def test_only_configured_upstreams_are_fetched(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/npm/x", b"x")
    refusing = make_client(allowed=(core_upstream.url("/np"),))
    with pytest.raises(UpstreamURLError):
        await refusing.fetch(metadata(core_upstream.url("/npm/x")), max_size=10)
    assert core_upstream.requests == []
    allowing = make_client(allowed=(core_upstream.url("/npm"),))
    result = await download(allowing, metadata(core_upstream.url("/npm/x")))
    assert result.blob.path.read_bytes() == b"x"


@pytest.mark.asyncio
async def test_create_session_takes_tls_settings_or_a_connector(core_tls: Tls) -> None:
    connector = aiohttp.TCPConnector()
    try:
        with pytest.raises(ValueError, match="not both"):
            create_session(ssl_context=core_tls.client, connector=connector)
    finally:
        await connector.close()


# Fetching.


@pytest.mark.asyncio
async def test_a_body_streams_to_a_temp_file_with_its_sha256(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    body = os.urandom(300_000)
    core_upstream.set(
        "/a.tgz",
        body,
        headers={
            "Content-Type": "application/x-tar",
            "ETag": '"e1"',
            "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
        },
    )
    result = await download(make_client(), artifact(core_upstream.url("/a.tgz")))
    assert result.status == 200
    assert result.blob.size == len(body)
    assert result.blob.sha256 == sha256(body)
    assert result.blob.path.parent == temp_dir
    assert result.blob.path.read_bytes() == body
    assert result.content_type == "application/x-tar"
    assert result.etag == '"e1"'
    assert result.last_modified == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert result.digest is None


@pytest.mark.asyncio
async def test_only_the_adapters_headers_are_sent(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/a", b"x")
    client = make_client()
    headers = {"Accept": "application/x-test", "accept-encoding": "gzip"}
    await download(client, artifact(core_upstream.url("/a"), headers=headers))
    await download(client, metadata(core_upstream.url("/a"), headers={"Accept": "text/html"}))
    sent_artifact, sent_metadata = (request.headers for request in core_upstream.requests)
    assert sent_artifact["accept"] == "application/x-test"
    assert sent_artifact["accept-encoding"] == "identity"
    assert sent_artifact["user-agent"] == USER_AGENT
    assert sent_metadata["accept"] == "text/html"
    assert "gzip" in sent_metadata["accept-encoding"]
    for sent in (sent_artifact, sent_metadata):
        assert set(sent) <= {"host", "accept", "accept-encoding", "user-agent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["md5", "sha256", "sha512"])
async def test_digests_are_verified(
    core_upstream: Upstream, make_client: ClientFactory, algorithm: Algorithm
) -> None:
    body = b"verified bytes"
    core_upstream.set("/f", body)
    expected = hashlib.new(algorithm, body).hexdigest()
    fetch = artifact(core_upstream.url("/f"), digest=Digest(algorithm, expected))
    result = await download(make_client(), fetch)
    assert result.digest == f"{algorithm}:{expected}"
    assert result.blob.sha256 == sha256(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["md5", "sha256", "sha512"])
async def test_a_digest_mismatch_leaves_nothing_behind(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path, algorithm: Algorithm
) -> None:
    core_upstream.set("/f", b"tampered bytes")
    published = hashlib.new(algorithm, b"published bytes").hexdigest()
    fetch = artifact(core_upstream.url("/f"), digest=Digest(algorithm, published))
    with pytest.raises(DigestMismatchError, match=published):
        await make_client().fetch(fetch, max_size=100)
    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_a_declared_length_over_the_limit_is_refused_unread(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    core_upstream.set("/big", b"x" * 2000)
    with pytest.raises(TooLargeError, match="2000 bytes"):
        await make_client().fetch(artifact(core_upstream.url("/big")), max_size=1000)
    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_a_streamed_body_over_the_limit_is_abandoned(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    core_upstream.set("/big", b"x" * 20_000, chunked=True)
    with pytest.raises(TooLargeError):
        await make_client().fetch(artifact(core_upstream.url("/big")), max_size=10_000)
    assert list(temp_dir.iterdir()) == []
    result = await download(make_client(), artifact(core_upstream.url("/big")), 20_000)
    assert result.blob.size == 20_000


@pytest.mark.asyncio
async def test_compressed_index_data_is_measured_after_decoding(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    plain = b"{" + b" " * 100_000 + b"}"
    core_upstream.set(
        "/index.json",
        gzip.compress(plain),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    client = make_client()
    with pytest.raises(TooLargeError):
        await client.fetch(metadata(core_upstream.url("/index.json")), max_size=10_000)
    assert list(temp_dir.iterdir()) == []
    result = await download(client, metadata(core_upstream.url("/index.json")), 200_000)
    assert result.blob.path.read_bytes() == plain
    assert result.blob.sha256 == sha256(plain)


@pytest.mark.asyncio
async def test_artifacts_are_stored_exactly_as_sent(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    # A server that labels a .tar.gz "Content-Encoding: gzip" must not get it unpacked.
    tarball = gzip.compress(b"tar contents")
    core_upstream.set(
        "/x.tar.gz",
        tarball,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-tar"},
    )
    fetch = artifact(core_upstream.url("/x.tar.gz"), digest=Digest("sha256", sha256(tarball)))
    result = await download(make_client(), fetch)
    assert result.blob.path.read_bytes() == tarball


@pytest.mark.asyncio
async def test_conditional_requests_send_the_validators(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/index", status=304, headers={"ETag": '"v2"'})
    validators = Validators('"v1"', "Mon, 01 Jan 2024 00:00:00 GMT")
    result = await make_client().fetch(
        metadata(core_upstream.url("/index")), max_size=100, validators=validators
    )
    assert result == NotModified(etag='"v2"', last_modified=None)
    sent = core_upstream.requests[-1].headers
    assert sent["if-none-match"] == '"v1"'
    assert sent["if-modified-since"] == "Mon, 01 Jan 2024 00:00:00 GMT"


@pytest.mark.asyncio
async def test_a_304_to_an_unconditional_request_is_an_error(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/index", status=304)
    with pytest.raises(UpstreamStatusError) as caught:
        await make_client().fetch(metadata(core_upstream.url("/index")), max_size=100)
    assert caught.value.status == 304


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_not_found_bodies_are_kept_short(
    core_upstream: Upstream, make_client: ClientFactory, status: int
) -> None:
    core_upstream.set(
        "/gone", b"n" * 100_000, status=status, headers={"Content-Type": "text/plain"}
    )
    result = await download(make_client(), metadata(core_upstream.url("/gone")), 10)
    assert result.status == status
    assert result.blob.size == NEGATIVE_BODY_LIMIT
    assert result.content_type == "text/plain"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 403, 401, 429, 206])
async def test_other_statuses_are_errors(
    core_upstream: Upstream, make_client: ClientFactory, status: int
) -> None:
    core_upstream.set("/x", b"error", status=status)
    with pytest.raises(UpstreamStatusError) as caught:
        await make_client().fetch(metadata(core_upstream.url("/x")), max_size=100)
    assert caught.value.status == status


# Redirects.


@pytest.mark.asyncio
async def test_up_to_five_redirects_are_followed(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    for hop in range(5):
        target = f"/r{hop + 1}"
        location = target if hop % 2 else core_upstream.url(target)
        core_upstream.set(f"/r{hop}", status=302, headers={"Location": location})
    core_upstream.set("/r5", b"arrived")
    result = await download(make_client(), metadata(core_upstream.url("/r0")))
    assert result.blob.path.read_bytes() == b"arrived"
    assert core_upstream.count("/r5") == 1


@pytest.mark.asyncio
async def test_a_sixth_redirect_is_refused(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    for hop in range(6):
        core_upstream.set(f"/r{hop}", status=301, headers={"Location": f"/r{hop + 1}"})
    core_upstream.set("/r6", b"never")
    with pytest.raises(RedirectError, match="more than 5"):
        await make_client().fetch(metadata(core_upstream.url("/r0")), max_size=100)
    assert core_upstream.count("/r6") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        pytest.param("http://{host}/x", id="http"),
        pytest.param("ftp://{host}/x", id="ftp"),
        pytest.param("https://user:pw@{host}/x", id="credentials"),
        pytest.param(None, id="no-location"),
    ],
)
async def test_redirects_must_lead_to_https(
    core_upstream: Upstream, make_client: ClientFactory, location: str | None
) -> None:
    host = core_upstream.origin.removeprefix("https://")
    headers = {} if location is None else {"Location": location.format(host=host)}
    core_upstream.set("/r", status=302, headers=headers)
    core_upstream.set("/x", b"never")
    with pytest.raises(RedirectError):
        await make_client().fetch(artifact(core_upstream.url("/r")), max_size=100)
    assert core_upstream.count("/x") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        pytest.param("https://localhost:{port}/cdn/file", id="localhost"),
        pytest.param("https://LOCALHOST.:{port}/cdn/file", id="localhost-dot"),
        pytest.param("https://127.0.0.1:{other}/cdn/file", id="loopback-other-port"),
        pytest.param("https://[::1]:{port}/cdn/file", id="ipv6-loopback"),
        pytest.param("https://[::ffff:127.0.0.1]:{port}/cdn/file", id="ipv4-mapped"),
        pytest.param("https://10.1.2.3/cdn/file", id="private"),
        pytest.param("https://100.64.0.1/cdn/file", id="shared-address-space"),
        pytest.param("https://169.254.169.254/latest/meta-data/", id="metadata-address"),
        pytest.param("https://168.63.129.16/machine", id="public-address-on-the-floor"),
        pytest.param("https://metadata.google.internal/x", id="metadata-name"),
        pytest.param("https://host.openshell.internal:8090/x", id="gateway-alias"),
        pytest.param("https://intranet/x", id="single-label"),
        pytest.param("https://mirror.corp/x", id="private-suffix"),
        pytest.param("https://2130706433/x", id="numeric-address"),
        pytest.param("https://127.1/x", id="short-address"),
    ],
)
async def test_redirects_to_hosts_that_are_not_public_are_refused(
    core_upstream: Upstream, make_client: ClientFactory, location: str
) -> None:
    port = core_upstream.origin.rsplit(":", 1)[1]
    target = location.format(port=port, other=unused_port())
    core_upstream.set("/r", status=307, headers={"Location": target})
    core_upstream.set("/cdn/file", b"from the CDN")
    with pytest.raises(RedirectError, match="refused"):
        await make_client().fetch(artifact(core_upstream.url("/r")), max_size=100)
    assert [request.path for request in core_upstream.requests] == ["/r"]


def test_redirects_may_lead_to_public_hosts_or_a_configured_upstream() -> None:
    source = URL("https://registry.test/pkg/-/pkg-1.0.0.tgz", encoded=True)
    allowed = ("https://registry.test", "https://127.0.0.1:8443/npm")
    followed = {
        "https://cdn.example.org/blob?sig=x": "https://cdn.example.org/blob?sig=x",
        "https://93.184.216.34/blob": "https://93.184.216.34/blob",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/blob": (
            "https://[2606:2800:220:1:248:1893:25c8:1946]/blob"
        ),
        # registry.test and 127.0.0.1 are not public, but configured.
        "/elsewhere": "https://registry.test/elsewhere",
        "https://REGISTRY.test:443/x": "https://registry.test/x",
        "https://127.0.0.1:8443/other/path": "https://127.0.0.1:8443/other/path",
    }
    for location, expected in followed.items():
        assert str(check_redirect(source, location, allowed=allowed)) == expected
    for location in ("https://127.0.0.1:8444/x", "https://registry.test:8443/x"):
        with pytest.raises(RedirectError, match=r"not a public host|loopback"):
            check_redirect(source, location, allowed=allowed)


@pytest.mark.asyncio
async def test_followed_redirects_log_the_target_host(
    core_upstream: Upstream, make_client: ClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.package_cache.upstream")
    core_upstream.set("/r0", status=302, headers={"Location": "/r1?sig=test-secret"})
    core_upstream.set("/r1?sig=test-secret", status=302, headers={"Location": "/x"})
    core_upstream.set("/x", b"arrived")
    close(await make_client().fetch(metadata(core_upstream.url("/r0")), max_size=100))
    host = core_upstream.origin.removeprefix("https://")
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        f"{core_upstream.origin}/r0 redirected to host {host}",
        f"{core_upstream.origin}/r1 redirected to host {host}",
    ]


# Transport failures and the rate limit.


@pytest.mark.asyncio
async def test_upstream_certificates_are_verified(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/x", b"x")
    with pytest.raises(UpstreamError, match="certificate"):
        await make_client(trusted=False).fetch(metadata(core_upstream.url("/x")), max_size=10)
    assert core_upstream.requests == []


@pytest.mark.asyncio
async def test_connection_failures_are_upstream_errors(make_client: ClientFactory) -> None:
    origin = f"https://127.0.0.1:{unused_port()}"
    client = make_client(allowed=(origin,))
    with pytest.raises(UpstreamError, match="cannot fetch"):
        await client.fetch(metadata(f"{origin}/x"), max_size=10)


@pytest.mark.asyncio
async def test_stalled_upstreams_time_out(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/slow", b"x", gate=asyncio.Event())
    client = make_client(timeout=aiohttp.ClientTimeout(sock_read=0.2))
    with pytest.raises(UpstreamError, match="cannot fetch"):
        await client.fetch(metadata(core_upstream.url("/slow")), max_size=10)


@pytest.mark.asyncio
async def test_every_upstream_request_takes_a_token(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/r", status=302, headers={"Location": "/x"})
    core_upstream.set("/x", b"x")
    client = make_client(rate=0.001, burst=1, rate_wait=0)
    with pytest.raises(RateLimitedError):
        await client.fetch(metadata(core_upstream.url("/r")), max_size=10)
    assert core_upstream.count("/r") == 1
    assert core_upstream.count("/x") == 0


# Remembered digests.


def test_the_digest_memory_is_an_expiring_lru() -> None:
    now = [0.0]
    memory = DigestMemory(size=2, ttl=10, clock=lambda: now[0])
    memory.remember("https://r.test/a", {"sha256": "1"})
    memory.remember("https://r.test/b", {"sha256": "2", "sha512": "3"})
    assert memory.actual("https://r.test/a", "sha256") == "1"
    memory.remember("https://r.test/c", {"sha256": "4"})  # b was used least recently
    assert memory.actual("https://r.test/b", "sha512") is None
    assert memory.actual("https://r.test/a", "sha512") is None
    assert len(memory) == 2
    now[0] = 10
    assert memory.actual("https://r.test/a", "sha256") is None
    assert len(memory) == 1
    with pytest.raises(ValueError, match="size"):
        DigestMemory(size=0)


@pytest.mark.asyncio
async def test_a_wrong_digest_is_not_downloaded_again(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    body = b"the real bytes"
    core_upstream.set("/f.tgz", body)
    client = make_client()
    url = core_upstream.url("/f.tgz")
    wrong = artifact(url, digest=Digest("sha256", sha256(b"other bytes")))
    with pytest.raises(DigestMismatchError, match="index published"):
        await client.fetch(wrong, max_size=100)
    with pytest.raises(DigestMismatchError, match="when it was last downloaded"):
        await client.fetch(wrong, max_size=100)
    assert core_upstream.count("/f.tgz") == 1
    close(await client.fetch(artifact(url, digest=Digest("sha256", sha256(body))), max_size=100))
    assert core_upstream.count("/f.tgz") == 2
    another = artifact(url, digest=Digest("sha256", sha256(b"a third body")))
    with pytest.raises(DigestMismatchError, match="when it was last downloaded"):
        await client.fetch(another, max_size=100)
    assert core_upstream.count("/f.tgz") == 2
    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_the_memory_answers_only_for_algorithms_it_computed(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    body = b"tarball bytes"
    core_upstream.set("/f.tgz", body)
    client = make_client()
    url = core_upstream.url("/f.tgz")
    close(await client.fetch(artifact(url, digest=Digest("sha256", sha256(body))), max_size=100))
    wrong = artifact(url, digest=Digest("sha512", hashlib.sha512(b"other").hexdigest()))
    with pytest.raises(DigestMismatchError, match="index published"):
        await client.fetch(wrong, max_size=100)  # no SHA-512 known yet: downloaded
    with pytest.raises(DigestMismatchError, match="when it was last downloaded"):
        await client.fetch(wrong, max_size=100)
    assert core_upstream.count("/f.tgz") == 2


@pytest.mark.asyncio
async def test_immutable_index_data_with_a_digest_is_remembered_too(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    core_upstream.set("/shard.msgpack.zst", b"shard bytes")
    client = make_client()
    shard = metadata(
        core_upstream.url("/shard.msgpack.zst"),
        immutable=True,
        digest=Digest("sha256", sha256(b"another shard")),
    )
    for message in ("index published", "when it was last downloaded"):
        with pytest.raises(DigestMismatchError, match=message):
            await client.fetch(shard, max_size=100)
    assert core_upstream.count("/shard.msgpack.zst") == 1


@pytest.mark.asyncio
async def test_a_remembered_digest_expires(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    now = [0.0]
    core_upstream.set("/f.tgz", b"bytes")
    client = make_client(digests=DigestMemory(ttl=60, clock=lambda: now[0]))
    wrong = artifact(core_upstream.url("/f.tgz"), digest=Digest("sha256", sha256(b"x")))
    for _ in range(2):
        with pytest.raises(DigestMismatchError):
            await client.fetch(wrong, max_size=100)
    assert core_upstream.count("/f.tgz") == 1
    now[0] = 60
    with pytest.raises(DigestMismatchError, match="index published"):
        await client.fetch(wrong, max_size=100)
    assert core_upstream.count("/f.tgz") == 2


@pytest.mark.asyncio
async def test_downloads_of_one_url_for_different_digests_take_turns(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    gate = asyncio.Event()
    core_upstream.set("/f.tgz", b"x" * 1000, gate=gate)
    client = make_client()
    url = core_upstream.url("/f.tgz")
    wrong = [artifact(url, digest=Digest("sha256", sha256(bytes([n])))) for n in range(5)]
    tasks = [asyncio.create_task(client.fetch(fetch, max_size=10_000)) for fetch in wrong]
    await until(lambda: core_upstream.count("/f.tgz") == 1)
    await asyncio.sleep(0.1)  # the others are waiting for their turn, not downloading
    assert core_upstream.count("/f.tgz") == 1
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert [type(result) for result in results] == [DigestMismatchError] * 5
    assert core_upstream.count("/f.tgz") == 1


@pytest.mark.asyncio
async def test_waiting_for_another_digests_download_is_bounded(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    gate = asyncio.Event()
    core_upstream.set("/f.tgz", b"x" * 1000, gate=gate)
    client = make_client(max_wait=0.2)
    url = core_upstream.url("/f.tgz")
    first = asyncio.create_task(
        client.fetch(artifact(url, digest=Digest("sha256", sha256(b"a"))), max_size=10_000)
    )
    await until(lambda: core_upstream.count("/f.tgz") == 1)
    with pytest.raises(BusyError, match="another digest"):
        await client.fetch(artifact(url, digest=Digest("sha256", sha256(b"b"))), max_size=10_000)
    gate.set()
    with pytest.raises(DigestMismatchError):
        await first
    assert core_upstream.count("/f.tgz") == 1


# Download slots, temp space, connections and deadlines.


@pytest.mark.asyncio
async def test_artifact_downloads_wait_briefly_for_a_free_slot(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    gate = asyncio.Event()
    core_upstream.set("/a.tgz", b"a", gate=gate)
    core_upstream.set("/b.tgz", b"b")
    core_upstream.set("/index", b"i")
    client = make_client(max_artifact_downloads=1, max_wait=0.2)
    first = asyncio.create_task(client.fetch(artifact(core_upstream.url("/a.tgz")), max_size=10))
    await until(lambda: core_upstream.count("/a.tgz") == 1)
    with pytest.raises(BusyError, match="download slots"):
        await client.fetch(artifact(core_upstream.url("/b.tgz")), max_size=10)
    assert core_upstream.count("/b.tgz") == 0
    close(await client.fetch(metadata(core_upstream.url("/index")), max_size=10))
    gate.set()
    close(await first)
    close(await client.fetch(artifact(core_upstream.url("/b.tgz")), max_size=10))


@pytest.mark.asyncio
async def test_a_freed_slot_goes_to_a_waiting_download(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    gate = asyncio.Event()
    core_upstream.set("/a.tgz", b"a", gate=gate)
    core_upstream.set("/b.tgz", b"b")
    client = make_client(max_artifact_downloads=1, max_wait=5)
    first = asyncio.create_task(client.fetch(artifact(core_upstream.url("/a.tgz")), max_size=10))
    await until(lambda: core_upstream.count("/a.tgz") == 1)
    second = asyncio.create_task(client.fetch(artifact(core_upstream.url("/b.tgz")), max_size=10))
    await asyncio.sleep(0.1)
    assert not second.done()
    assert core_upstream.count("/b.tgz") == 0
    gate.set()
    close(await first)
    close(await second)


def test_the_pool_keeps_connections_for_index_data() -> None:
    assert INDEX_CONNECTIONS > 0
    assert pool_size(16) == 16 + INDEX_CONNECTIONS
    with pytest.raises(ValueError, match="max_artifact_downloads"):
        UpstreamClient(
            None,  # pyright: ignore[reportArgumentType]
            temp_dir=Path(),
            limiter=TokenBucket(1, 1),
            allowed=(),
            temp_space=TempSpace(1),
            max_artifact_downloads=0,
        )


@pytest.mark.asyncio
async def test_waiting_for_an_upstream_connection_is_bounded(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    gate = asyncio.Event()
    core_upstream.set("/slow", b"s", gate=gate)
    core_upstream.set("/other", b"o")
    client = make_client(pool=1, timeout=aiohttp.ClientTimeout(connect=0.3, sock_read=10))
    first = asyncio.create_task(client.fetch(metadata(core_upstream.url("/slow")), max_size=10))
    await until(lambda: core_upstream.count("/slow") == 1)
    with pytest.raises(BusyError, match="no connection"):
        await client.fetch(metadata(core_upstream.url("/other")), max_size=10)
    assert core_upstream.count("/other") == 0
    gate.set()
    close(await first)
    close(await client.fetch(metadata(core_upstream.url("/other")), max_size=10))


def test_temp_space_reservations() -> None:
    space = TempSpace(100)
    first = space.reserve(60)
    with pytest.raises(BusyError, match="do not fit"):
        space.reserve(41)
    second = space.reserve(40)
    assert space.used == 100
    with pytest.raises(BusyError):
        second.grow(1)
    first.release()
    first.release()
    assert space.used == 40
    second.grow(10)
    assert (second.size, space.used) == (50, 50)
    second.release()
    assert space.used == 0
    with pytest.raises(RuntimeError, match="released"):
        second.grow(1)
    with pytest.raises(ValueError, match="positive"):
        TempSpace(0)


@pytest.mark.asyncio
async def test_a_download_that_would_overflow_the_temp_space_fails_at_once(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    pause = asyncio.Event()
    core_upstream.set("/a.tgz", b"a" * 600, pause=pause, pause_after=100)
    core_upstream.set("/b.tgz", b"b" * 600)
    space = TempSpace(1000)
    client = make_client(temp_space=space)
    first = asyncio.create_task(
        client.fetch(artifact(core_upstream.url("/a.tgz")), max_size=10_000)
    )
    await until(lambda: space.used == 600)  # reserved from its Content-Length
    with pytest.raises(BusyError, match="do not fit"):
        await client.fetch(artifact(core_upstream.url("/b.tgz")), max_size=10_000)
    assert space.used == 600
    pause.set()
    result = await first
    assert isinstance(result, Downloaded)
    assert result.reservation.size == 600
    result.close()
    result.close()
    assert space.used == 0
    assert list(temp_dir.iterdir()) == []
    close(await client.fetch(artifact(core_upstream.url("/b.tgz")), max_size=10_000))
    assert space.used == 0


@pytest.mark.asyncio
async def test_bodies_without_a_length_reserve_space_as_they_arrive(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    core_upstream.set("/big", b"x" * 20_000, chunked=True)
    core_upstream.set("/small", b"y" * 5_000, chunked=True)
    space = TempSpace(10_000)
    client = make_client(temp_space=space)
    with pytest.raises(BusyError, match="do not fit"):
        await client.fetch(artifact(core_upstream.url("/big")), max_size=100_000)
    assert space.used == 0
    assert list(temp_dir.iterdir()) == []
    result = await download(client, artifact(core_upstream.url("/small")), 100_000)
    assert 5_000 <= result.reservation.size <= space.limit
    result.close()
    assert space.used == 0


@pytest.mark.asyncio
async def test_decoded_index_data_reserves_beyond_its_length(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    plain = b"{" + b" " * 100_000 + b"}"
    core_upstream.set("/index.json", gzip.compress(plain), headers={"Content-Encoding": "gzip"})
    small = TempSpace(50_000)
    with pytest.raises(BusyError):
        await make_client(temp_space=small).fetch(
            metadata(core_upstream.url("/index.json")), max_size=1_000_000
        )
    assert small.used == 0
    large = TempSpace(1_000_000)
    result = await download(
        make_client(temp_space=large), metadata(core_upstream.url("/index.json"))
    )
    assert result.reservation.size >= len(plain)
    result.close()
    assert large.used == 0


@pytest.mark.asyncio
async def test_index_fetches_have_a_deadline(
    core_upstream: Upstream, make_client: ClientFactory, temp_dir: Path
) -> None:
    pause = asyncio.Event()
    core_upstream.set("/index", b"{" + b" " * 10_000 + b"}", pause=pause, pause_after=100)
    space = TempSpace(10**6)
    client = make_client(metadata_deadline=0.3, temp_space=space)
    try:
        with pytest.raises(UpstreamError, match=r"took longer than 0\.3s"):
            await client.fetch(metadata(core_upstream.url("/index")), max_size=100_000)
        assert list(temp_dir.iterdir()) == []
        assert space.used == 0
    finally:
        pause.set()


@pytest.mark.asyncio
async def test_artifact_downloads_have_no_deadline(
    core_upstream: Upstream, make_client: ClientFactory
) -> None:
    gate = asyncio.Event()
    core_upstream.set("/a.tgz", b"a", gate=gate)
    client = make_client(metadata_deadline=0.2)
    task = asyncio.create_task(client.fetch(artifact(core_upstream.url("/a.tgz")), max_size=10))
    await asyncio.sleep(0.4)
    assert not task.done()
    gate.set()
    close(await task)


def test_sandbox_budgets_keep_the_sandboxes_seen_last() -> None:
    budgets = SandboxBudgets(1.0, 1, capacity=2)
    a, b = budgets.bucket("a"), budgets.bucket("b")
    assert budgets.bucket("a") is a
    budgets.bucket("c")  # b was seen longest ago
    assert budgets.bucket("a") is a
    assert budgets.bucket("b") is not b
