# SPDX-License-Identifier: MIT
"""The web application, end to end against a local TLS upstream and a fake adapter."""

import asyncio
import contextlib
import gzip
import hashlib
import logging
import os
import ssl
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDictProxy
from pytest_aiohttp import AiohttpClient

from sandbox_science.package_cache.adapter import GZIP_MAGIC, Digest, Fetch, Kind, Route, Static
from sandbox_science.package_cache.app import (
    CACHE,
    MAX_REWRITES,
    SEND_TIMEOUT,
    PackageCache,
    create_app,
)
from sandbox_science.package_cache.errors import StoreError
from sandbox_science.package_cache.settings import Settings
from sandbox_science.package_cache.upstream import USER_AGENT

pytestmark = pytest.mark.asyncio

STATS_KEYS = {"entries", "bytes", "hits", "misses", "revalidated", "stale", "errors"}


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


class Clock:
    """A settable stand-in for ``time.time``."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeAdapter:
    """Answers routes from a table and records what it was asked."""

    prefix: str
    routes: dict[str, Route] = field(default_factory=dict[str, Route])
    seen: list[tuple[str, dict[str, str]]] = field(default_factory=list[tuple[str, dict[str, str]]])
    broken: bool = False

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        self.seen.append((path, dict(headers)))
        if self.broken:
            raise RuntimeError("test: the adapter has a bug")
        return self.routes.get(path)


Response = tuple[int, bytes, CIMultiDictProxy[str]]


@dataclass
class Harness:
    """A running cache with one fake adapter (prefix ``pkg``) in front of the test upstream."""

    client: TestClient[web.Request, web.Application]
    upstream: Upstream
    adapter: FakeAdapter
    clock: Clock
    settings: Settings

    @property
    def cache(self) -> PackageCache:
        return self.client.app[CACHE]

    def route(self, tail: str, path: str | None = None, **options: object) -> Fetch:
        """Answer ``/pkg/<tail>`` with a fetch of ``path`` (default ``/<tail>``) upstream."""
        fields: dict[str, object] = {"kind": Kind.METADATA}
        fields.update(options)
        fetch = Fetch(key=tail, url=self.upstream.url(path or f"/{tail}"), **fields)  # pyright: ignore[reportArgumentType]
        self.adapter.routes[tail] = fetch
        return fetch

    async def get(self, tail: str, method: str = "GET", **options: object) -> Response:
        async with self.client.request(method, f"/pkg/{tail}", **options) as response:  # pyright: ignore[reportArgumentType]
            return response.status, await response.read(), response.headers

    async def stats(self) -> dict[str, int]:
        async with self.client.get("/-/stats") as response:
            return await response.json()

    def files(self, directory: str) -> list[Path]:
        return [path for path in (self.settings.cache_dir / directory).rglob("*") if path.is_file()]


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest_asyncio.fixture
async def make_harness(
    aiohttp_client: AiohttpClient,
    core_upstream: Upstream,
    core_tls: Tls,
    core_settings: Callable[..., Settings],
) -> HarnessFactory:
    """Start caches; keyword arguments override settings keys (and ``create_app``'s waits)."""

    async def make(
        *,
        rate_limit_wait: float = 10.0,
        download_wait: float = 5.0,
        send_timeout: float = SEND_TIMEOUT,
        **overrides: object,
    ) -> Harness:
        clock = Clock()
        adapter = FakeAdapter("pkg")
        settings = core_settings(core_upstream.origin, **overrides)
        app = create_app(
            settings,
            {"pkg": adapter},
            ssl_context=core_tls.client,
            clock=clock,
            rate_limit_wait=rate_limit_wait,
            download_wait=download_wait,
            send_timeout=send_timeout,
        )
        client = await aiohttp_client(app)
        return Harness(client, core_upstream, adapter, clock, settings)

    return make


@pytest_asyncio.fixture
async def harness(make_harness: HarnessFactory) -> Harness:
    return await make_harness()


async def raw_request(
    client: TestClient[web.Request, web.Application],
    target: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
) -> int:
    """Send a request line exactly as given (clients would normalize it) and return the status."""
    reader, writer = await asyncio.open_connection(client.host, client.port)
    try:
        lines = [f"{method} {target} HTTP/1.1", "Host: cache.test", "Connection: close"]
        lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        status_line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    return int(status_line.split()[1])


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait until ``condition()`` holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def digest(algorithm: Literal["md5", "sha256", "sha512"], body: bytes) -> Digest:
    return Digest(algorithm, hashlib.new(algorithm, body).hexdigest())


def open_files() -> set[str]:
    """What this process's file descriptors point at (Linux)."""
    paths: set[str] = set()
    for name in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            paths.add(os.readlink(f"/proc/self/fd/{name}"))
    return paths


# Hits, misses, freshness and revalidation.


async def test_a_miss_is_fetched_and_stored_then_hit(harness: Harness) -> None:
    harness.upstream.set("/index", b"index body", headers={"Content-Type": "application/json"})
    harness.route("index")
    for _ in range(2):
        status, body, headers = await harness.get("index")
        assert (status, body) == (200, b"index body")
        assert headers["Content-Type"] == "application/json"
        assert headers["Content-Length"] == "10"
    assert harness.upstream.count("/index") == 1
    assert await harness.stats() == {
        "entries": 1,
        "bytes": 10,
        "hits": 1,
        "misses": 1,
        "revalidated": 0,
        "stale": 0,
        "errors": 0,
    }


async def test_head_answers_without_a_body_and_fills_the_cache(harness: Harness) -> None:
    harness.upstream.set("/file.tgz", b"x" * 100)
    harness.route("file.tgz", kind=Kind.ARTIFACT)
    status, body, headers = await harness.get("file.tgz", method="HEAD")
    assert (status, body, headers["Content-Length"]) == (200, b"", "100")
    status, body, _ = await harness.get("file.tgz")
    assert (status, body) == (200, b"x" * 100)
    assert harness.upstream.count("/file.tgz") == 1


async def test_index_data_is_fresh_for_the_ttl(harness: Harness) -> None:
    harness.upstream.set("/index", b"v1")
    harness.route("index")
    await harness.get("index")
    harness.clock.now += 299
    assert (await harness.get("index"))[:2] == (200, b"v1")
    assert harness.upstream.count("/index") == 1


async def test_expired_index_data_is_revalidated_with_its_validators(harness: Harness) -> None:
    validators = {"ETag": '"v1"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
    harness.upstream.set("/index", b"v1 body", headers=validators)
    harness.route("index")
    await harness.get("index")
    harness.clock.now += 301
    harness.upstream.set("/index", status=304, headers={"ETag": '"v1"'})
    assert (await harness.get("index"))[:2] == (200, b"v1 body")
    sent = harness.upstream.requests[-1].headers
    assert sent["if-none-match"] == '"v1"'
    assert sent["if-modified-since"] == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert (await harness.get("index"))[:2] == (200, b"v1 body")
    assert harness.upstream.count("/index") == 2
    stats = await harness.stats()
    assert (stats["misses"], stats["revalidated"], stats["hits"]) == (1, 1, 1)


async def test_expired_index_data_is_replaced_when_upstream_changed(harness: Harness) -> None:
    harness.upstream.set("/index", b"v1 body", headers={"ETag": '"v1"'})
    harness.route("index")
    await harness.get("index")
    harness.clock.now += 301
    harness.upstream.set("/index", b"v2 body, longer", headers={"ETag": '"v2"'})
    assert (await harness.get("index"))[:2] == (200, b"v2 body, longer")
    assert (await harness.get("index"))[:2] == (200, b"v2 body, longer")
    assert harness.upstream.count("/index") == 2
    entry = harness.cache.store.get("pkg:index")
    assert entry is not None
    assert entry.etag == '"v2"'
    assert len(harness.files("blobs")) == 1


async def test_immutable_index_files_and_artifacts_never_expire(harness: Harness) -> None:
    harness.upstream.set("/x.metadata", b"metadata")
    harness.route("x.metadata", immutable=True)
    harness.upstream.set("/x.tgz", b"artifact")
    harness.route("x.tgz", kind=Kind.ARTIFACT)
    await harness.get("x.metadata")
    await harness.get("x.tgz")
    harness.clock.now += 30 * 86400
    harness.upstream.set("/x.metadata", b"changed")
    harness.upstream.set("/x.tgz", b"changed")
    assert (await harness.get("x.metadata"))[:2] == (200, b"metadata")
    assert (await harness.get("x.tgz"))[:2] == (200, b"artifact")
    assert harness.upstream.count("/x.metadata") == harness.upstream.count("/x.tgz") == 1


# Failures, stale copies and negative caching.


async def test_a_stale_copy_is_served_while_upstream_fails(harness: Harness) -> None:
    harness.upstream.set("/index", b"last good")
    harness.route("index")
    await harness.get("index")
    harness.clock.now += 301
    harness.upstream.set("/index", b"broken", status=500)
    assert (await harness.get("index"))[:2] == (200, b"last good")
    assert (await harness.stats())["stale"] == 1
    harness.clock.now += 86400
    status, body, _ = await harness.get("index")
    assert (status, body) == (502, b"upstream error\n")
    assert (await harness.stats())["errors"] == 1


@pytest.mark.parametrize("upstream_status", [500, 503, 403, 429])
async def test_a_failure_without_a_copy_is_502(harness: Harness, upstream_status: int) -> None:
    harness.upstream.set("/index", b"failure", status=upstream_status)
    harness.route("index")
    assert (await harness.get("index"))[0] == 502
    assert (await harness.stats())["entries"] == 0


@pytest.mark.parametrize("upstream_status", [404, 410])
async def test_index_not_found_passes_through_and_is_cached_briefly(
    harness: Harness, upstream_status: int
) -> None:
    harness.upstream.set(
        "/missing", b"no such package", status=upstream_status, headers={"Content-Type": "text/x"}
    )
    harness.route("missing")
    for _ in range(2):
        status, body, headers = await harness.get("missing")
        assert (status, body, headers["Content-Type"]) == (
            upstream_status,
            b"no such package",
            "text/x",
        )
    assert harness.upstream.count("/missing") == 1
    harness.clock.now += 61
    assert (await harness.get("missing"))[0] == upstream_status
    assert harness.upstream.count("/missing") == 2


async def test_index_not_found_replaces_an_expired_copy(harness: Harness) -> None:
    harness.upstream.set("/index", b"exists")
    harness.route("index")
    await harness.get("index")
    harness.clock.now += 301
    harness.upstream.set("/index", b"gone", status=404)
    assert (await harness.get("index"))[:2] == (404, b"gone")
    assert (await harness.get("index"))[:2] == (404, b"gone")
    assert harness.upstream.count("/index") == 2


async def test_artifact_not_found_is_not_cached(harness: Harness) -> None:
    harness.upstream.set("/gone.tgz", b"gone", status=404)
    harness.route("gone.tgz", kind=Kind.ARTIFACT)
    for _ in range(2):
        assert (await harness.get("gone.tgz"))[:2] == (404, b"gone")
    assert harness.upstream.count("/gone.tgz") == 2
    assert (await harness.stats())["entries"] == 0


# Verification, limits and rewriting.


@pytest.mark.parametrize("algorithm", ["md5", "sha256", "sha512"])
async def test_artifacts_are_verified_against_their_digest(
    harness: Harness, algorithm: Literal["md5", "sha256", "sha512"]
) -> None:
    body = b"artifact bytes"
    expected = digest(algorithm, body)
    harness.upstream.set("/a.tgz", body)
    harness.route("a.tgz", kind=Kind.ARTIFACT, digest=expected)
    assert (await harness.get("a.tgz"))[:2] == (200, body)
    entry = harness.cache.store.get("pkg:a.tgz")
    assert entry is not None
    assert entry.digest == f"{algorithm}:{expected.hex}"


@pytest.mark.parametrize("algorithm", ["md5", "sha256", "sha512"])
async def test_a_digest_mismatch_is_502_and_nothing_is_stored(
    harness: Harness, algorithm: Literal["md5", "sha256", "sha512"]
) -> None:
    harness.upstream.set("/a.tgz", b"tampered bytes")
    harness.route("a.tgz", kind=Kind.ARTIFACT, digest=digest(algorithm, b"published bytes"))
    assert (await harness.get("a.tgz"))[:2] == (502, b"digest mismatch\n")
    assert (await harness.stats())["entries"] == 0
    assert harness.files("blobs") == []
    assert harness.files("tmp") == []


async def test_a_stored_body_is_served_only_for_the_digest_it_was_verified_against(
    harness: Harness,
) -> None:
    body = b"release bytes"
    harness.upstream.set("/a.tgz", body)
    harness.route("a.tgz", kind=Kind.ARTIFACT, digest=digest("sha512", body))
    harness.adapter.routes["b.tgz"] = Fetch(
        key="a.tgz",
        url=harness.upstream.url("/a.tgz"),
        kind=Kind.ARTIFACT,
        digest=digest("sha512", b"other bytes"),
    )
    assert (await harness.get("a.tgz"))[:2] == (200, body)
    assert (await harness.get("b.tgz"))[:2] == (502, b"digest mismatch\n")
    assert (await harness.get("a.tgz"))[:2] == (200, body)
    # The download for a.tgz showed what the file is: b.tgz's digest is refused unfetched.
    assert harness.upstream.count("/a.tgz") == 1


def route_digest(harness: Harness, tail: str, path: str, expected: bytes) -> None:
    """Route ``tail`` to the artifact at ``path``, keyed by the digest, as the adapters do."""
    checked = digest("sha256", expected)
    harness.adapter.routes[tail] = Fetch(
        key=f"{path} sha256={checked.hex}",
        url=harness.upstream.url(path),
        kind=Kind.ARTIFACT,
        immutable=True,
        digest=checked,
    )


async def test_requests_naming_a_wrong_digest_download_the_file_once(harness: Harness) -> None:
    body = b"the published file"
    harness.upstream.set("/f.whl", body)
    for number in range(3):
        route_digest(harness, f"wrong{number}", "/f.whl", b"forged %d" % number)
    route_digest(harness, "right", "/f.whl", body)
    for number in range(3):
        for _ in range(2):
            assert (await harness.get(f"wrong{number}"))[:2] == (502, b"digest mismatch\n")
    assert harness.upstream.count("/f.whl") == 1
    assert (await harness.get("right"))[:2] == (200, body)
    assert (await harness.get("wrong0"))[:2] == (502, b"digest mismatch\n")
    assert (await harness.get("right"))[:2] == (200, body)
    assert harness.upstream.count("/f.whl") == 2
    assert harness.files("tmp") == []
    stats = await harness.stats()
    assert (stats["entries"], stats["errors"], stats["hits"]) == (1, 7, 1)


async def test_concurrent_requests_naming_different_digests_download_the_file_once(
    harness: Harness,
) -> None:
    gate = asyncio.Event()
    harness.upstream.set("/f.whl", b"x" * 100_000, gate=gate)
    for number in range(5):
        route_digest(harness, f"wrong{number}", "/f.whl", b"forged %d" % number)
    requests = [asyncio.create_task(harness.get(f"wrong{number}")) for number in range(5)]
    await until(lambda: harness.upstream.count("/f.whl") == 1)
    await asyncio.sleep(0.1)
    gate.set()
    results = await asyncio.gather(*requests)
    assert [result[:2] for result in results] == [(502, b"digest mismatch\n")] * 5
    assert harness.upstream.count("/f.whl") == 1


async def test_index_data_that_is_rewritten_has_its_own_size_limit(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(
        max_size="1Mi", max_artifact_size="4Ki", max_metadata_size="4Ki", max_rewrite_size="1Ki"
    )
    calls: list[int] = []

    def rewrite(body: bytes, content_type: str) -> bytes:
        calls.append(len(body))
        return body

    harness.upstream.set("/big", b"x" * 2000)
    harness.route("big", rewrite=rewrite)
    harness.upstream.set(
        "/big.gz", gzip.compress(b"x" * 2000), headers={"Content-Encoding": "gzip"}
    )
    harness.route("big.gz", rewrite=rewrite)
    harness.upstream.set("/small", b"s" * 1000)
    harness.route("small", rewrite=rewrite)
    harness.upstream.set("/plain", b"y" * 2000)
    harness.route("plain")
    for tail in ("big", "big.gz"):
        assert (await harness.get(tail))[:2] == (502, b"upstream file too large\n")
    assert (await harness.get("small"))[:2] == (200, b"s" * 1000)
    assert (await harness.get("plain"))[:2] == (200, b"y" * 2000)
    assert calls == [1000]


async def test_at_most_two_rewrites_run_at_once(harness: Harness) -> None:
    release = threading.Event()
    lock = threading.Lock()
    running = 0
    peak = 0

    def rewrite(body: bytes, content_type: str) -> bytes:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        with lock:
            running -= 1
        return body

    for number in range(4):
        harness.upstream.set(f"/index{number}", b"body %d" % number)
        harness.route(f"index{number}", rewrite=rewrite)
    requests = [asyncio.create_task(harness.get(f"index{number}")) for number in range(4)]
    await until(lambda: running == MAX_REWRITES == 2)
    await asyncio.sleep(0.2)  # the other two have downloaded and are waiting their turn
    assert (running, peak) == (2, 2)
    assert all(harness.upstream.count(f"/index{number}") == 1 for number in range(4))
    release.set()
    results = await asyncio.gather(*requests)
    assert [result[:2] for result in results] == [(200, b"body %d" % n) for n in range(4)]
    assert peak == 2


async def test_size_limits_bound_every_download(make_harness: HarnessFactory) -> None:
    harness = await make_harness(max_size="1Mi", max_artifact_size="1Ki", max_metadata_size="2Ki")
    harness.upstream.set("/big.tgz", b"x" * 2000)
    harness.route("big.tgz", kind=Kind.ARTIFACT)
    harness.upstream.set("/chunked.tgz", b"x" * 2000, chunked=True)
    harness.route("chunked.tgz", kind=Kind.ARTIFACT)
    bomb = gzip.compress(b" " * 100_000)
    harness.upstream.set("/bomb.json", bomb, headers={"Content-Encoding": "gzip"})
    harness.route("bomb.json")
    harness.upstream.set("/ok.json", b"y" * 2000)
    harness.route("ok.json")
    for tail in ("big.tgz", "chunked.tgz", "bomb.json"):
        assert (await harness.get(tail))[:2] == (502, b"upstream file too large\n")
    assert (await harness.get("ok.json"))[:2] == (200, b"y" * 2000)
    assert (await harness.stats())["entries"] == 1
    assert harness.files("tmp") == []


async def test_index_bodies_are_rewritten_before_they_are_stored(harness: Harness) -> None:
    calls: list[tuple[bytes, str]] = []

    def rewrite(body: bytes, content_type: str) -> bytes:
        calls.append((body, content_type))
        return body.replace(b"https://upstream.test", b"http://cache.test:8090")

    original = b'{"url": "https://upstream.test/files/x.tgz"}'
    harness.upstream.set(
        "/index",
        gzip.compress(original),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json", "ETag": '"1"'},
    )
    harness.route("index", rewrite=rewrite)
    rewritten = b'{"url": "http://cache.test:8090/files/x.tgz"}'
    assert (await harness.get("index"))[:2] == (200, rewritten)
    assert (await harness.get("index"))[:2] == (200, rewritten)
    assert calls == [(original, "application/json")]
    harness.clock.now += 301
    harness.upstream.set("/index", status=304)
    assert (await harness.get("index"))[:2] == (200, rewritten)
    assert len(calls) == 1


async def test_a_rewrite_failure_is_502_and_nothing_is_stored(harness: Harness) -> None:
    def rewrite(body: bytes, content_type: str) -> bytes:
        raise ValueError("test: not an index")

    harness.upstream.set("/index", b"garbage")
    harness.route("index", rewrite=rewrite)
    status, body, _ = await harness.get("index")
    assert (status, body) == (502, b"upstream index cannot be rewritten\n")
    assert (await harness.stats())["entries"] == 0
    assert harness.files("blobs") == []
    assert harness.files("tmp") == []


async def test_the_content_type_can_be_overridden(harness: Harness) -> None:
    harness.upstream.set("/a", b"{}", headers={"Content-Type": "text/plain"})
    harness.route("a", content_type="application/vnd.pypi.simple.v1+json")
    harness.upstream.set("/b", b"{}", headers={"Content-Type": "application/json; charset=utf-8"})
    harness.route("b")
    assert (await harness.get("a"))[2]["Content-Type"] == "application/vnd.pypi.simple.v1+json"
    assert (await harness.get("b"))[2]["Content-Type"] == "application/json; charset=utf-8"


async def test_static_answers_are_served_as_given(harness: Harness) -> None:
    harness.adapter.routes["config.json"] = Static(b'{"dl": "x"}', "application/json")
    status, body, headers = await harness.get("config.json")
    assert (status, body, headers["Content-Type"]) == (200, b'{"dl": "x"}', "application/json")
    status, body, _ = await harness.get("config.json", method="HEAD")
    assert (status, body) == (200, b"")
    harness.adapter.routes["teapot"] = Static(b"no", "text/plain", status=418)
    assert (await harness.get("teapot"))[:2] == (418, b"no")
    assert harness.upstream.requests == []
    assert (await harness.stats())["entries"] == 0


# Byte ranges and shorter lifetimes (Hackage's index and signed metadata).


async def test_one_byte_range_of_a_stored_body_is_answered_206(harness: Harness) -> None:
    harness.upstream.set("/file.tgz", b"0123456789", headers={"Content-Type": "application/x-test"})
    harness.route("file.tgz", kind=Kind.ARTIFACT)
    for header, part, content_range in (
        ("bytes=2-5", b"2345", "bytes 2-5/10"),  # a miss: fetched, stored, then cut
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=8-", b"89", "bytes 8-9/10"),
        ("bytes=6-100", b"6789", "bytes 6-9/10"),
    ):
        status, body, headers = await harness.get("file.tgz", headers={"Range": header})
        assert (status, body) == (206, part)
        assert headers["Content-Range"] == content_range
        assert headers["Content-Length"] == str(len(part))
        assert headers["Content-Type"] == "application/x-test"
    status, body, headers = await harness.get("file.tgz")
    assert (status, body) == (200, b"0123456789")
    assert "Content-Range" not in headers
    assert harness.upstream.count("/file.tgz") == 1
    # Client headers never reach upstream, Range included.
    assert "range" not in harness.upstream.requests[0].headers


async def test_a_range_beyond_the_body_is_answered_416(harness: Harness) -> None:
    harness.upstream.set("/index", b"0123456789")
    harness.route("index")
    for header in ("bytes=10-", "bytes=-0"):
        status, _, headers = await harness.get("index", headers={"Range": header})
        assert status == 416
        assert headers["Content-Range"] == "bytes */10"
    # The body was stored all the same, and is served whole without a range.
    assert (await harness.get("index"))[:2] == (200, b"0123456789")
    assert harness.upstream.count("/index") == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Range": "bytes=0-1,4-5"},
        {"Range": "bytes=5-2"},
        {"Range": "items=0-1"},
        {"Range": "bytes=0-1", "If-Range": '"v1"'},
        {"Range": "bytes=0-1", "If-Range": "Sat, 10 Oct 2026 00:00:00 GMT"},
    ],
)
async def test_other_range_requests_get_the_whole_body(
    harness: Harness, headers: dict[str, str]
) -> None:
    harness.upstream.set("/index", b"0123456789", headers={"ETag": '"v1"'})
    harness.route("index")
    status, body, answer = await harness.get("index", headers=headers)
    assert (status, body) == (200, b"0123456789")
    assert "Content-Range" not in answer


async def test_head_and_not_found_answers_ignore_ranges(harness: Harness) -> None:
    harness.upstream.set("/index", b"0123456789")
    harness.route("index")
    status, body, headers = await harness.get(
        "index", method="HEAD", headers={"Range": "bytes=0-1"}
    )
    assert (status, body, headers["Content-Length"]) == (200, b"", "10")
    assert "Content-Range" not in headers
    harness.upstream.set("/missing", b"no such file", status=404)
    harness.route("missing")
    status, body, _ = await harness.get("missing", headers={"Range": "bytes=0-1"})
    assert (status, body) == (404, b"no such file")


async def test_a_range_is_logged_with_the_bytes_sent(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.package_cache.requests")
    harness.upstream.set("/index", b"0123456789")
    harness.route("index")
    await harness.get("index")
    await harness.get("index", headers={"Range": "bytes=0-3"})
    await harness.get("index", headers={"Range": "bytes=20-"})
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sandbox_science.package_cache.requests"
    ]
    assert lines[1:] == [
        "ecosystem=pkg method=GET path=/pkg/index result=hit status=206 bytes=4 upstream_ms=-",
        "ecosystem=pkg method=GET path=/pkg/index result=hit status=416 bytes=0 upstream_ms=-",
    ]


async def test_index_data_without_a_lifetime_is_revalidated_on_every_request(
    harness: Harness,
) -> None:
    harness.upstream.set("/timestamp.json", b"v1", headers={"ETag": '"v1"'})
    harness.route("timestamp.json", max_age=0)
    assert (await harness.get("timestamp.json"))[:2] == (200, b"v1")
    harness.upstream.set("/timestamp.json", status=304, headers={"ETag": '"v1"'})
    for _ in range(2):
        assert (await harness.get("timestamp.json"))[:2] == (200, b"v1")
        assert harness.upstream.requests[-1].headers["if-none-match"] == '"v1"'
    harness.upstream.set("/timestamp.json", b"v2", headers={"ETag": '"v2"'})
    assert (await harness.get("timestamp.json"))[:2] == (200, b"v2")
    assert harness.upstream.count("/timestamp.json") == 4
    stats = await harness.stats()
    assert (stats["misses"], stats["revalidated"], stats["hits"]) == (2, 2, 0)


async def test_a_range_comes_from_the_revalidated_copy(harness: Harness) -> None:
    # hackage-security asks for the end of 01-index.tar.gz as the snapshot it just
    # fetched describes it: the cache must not answer from an older copy.
    harness.upstream.set("/01-index.tar.gz", b"0123456789", headers={"ETag": '"v1"'})
    harness.route("01-index.tar.gz", max_age=0)
    assert (await harness.get("01-index.tar.gz"))[0] == 200
    harness.upstream.set("/01-index.tar.gz", b"01234567XYZ", headers={"ETag": '"v2"'})
    status, body, headers = await harness.get("01-index.tar.gz", headers={"Range": "bytes=8-10"})
    assert (status, body, headers["Content-Range"]) == (206, b"XYZ", "bytes 8-10/11")


async def test_a_lifetime_shortens_the_ttl_but_never_extends_it(harness: Harness) -> None:
    harness.upstream.set("/short", b"short", headers={"ETag": '"s"'})
    harness.route("short", max_age=60)
    harness.upstream.set("/long", b"long", headers={"ETag": '"l"'})
    harness.route("long", max_age=3600)
    await harness.get("short")
    await harness.get("long")
    harness.clock.now += 59
    await harness.get("short")
    assert harness.upstream.count("/short") == 1
    harness.clock.now += 2
    await harness.get("short")
    assert harness.upstream.count("/short") == 2
    harness.clock.now += 240  # 301 seconds after the first fetch: past the 300-second TTL
    await harness.get("long")
    assert harness.upstream.count("/long") == 2


# Byte-exact index data and artifacts the cache cannot verify (Hackage, opam's index).

TARBALL = gzip.compress(b"a source distribution")


async def test_byte_exact_index_data_is_requested_and_stored_unencoded(harness: Harness) -> None:
    # A gzip file sent without a content coding is the file itself.
    harness.upstream.set("/index.tar.gz", TARBALL, headers={"Content-Encoding": "identity"})
    harness.route("index.tar.gz", exact=True, magic=GZIP_MAGIC)
    assert (await harness.get("index.tar.gz"))[:2] == (200, TARBALL)
    assert harness.upstream.requests[-1].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("coding", ["gzip", "br", "identity, gzip", "x-gzip"])
async def test_byte_exact_index_data_with_a_content_coding_is_refused(
    harness: Harness, coding: str
) -> None:
    # Decoding would store the unpacked tar as the .tar.gz; keeping the bytes would
    # store a second gzip layer if the server really applied one. Neither is the file.
    harness.upstream.set(
        "/index.tar.gz", gzip.compress(TARBALL), headers={"Content-Encoding": coding}
    )
    harness.route("index.tar.gz", exact=True)
    assert (await harness.get("index.tar.gz"))[:2] == (502, b"unexpected upstream content\n")
    assert (await harness.stats())["entries"] == 0
    assert harness.files("blobs") == harness.files("tmp") == []


async def test_a_stale_copy_is_served_when_byte_exact_data_comes_encoded(harness: Harness) -> None:
    harness.upstream.set("/index.tar.gz", TARBALL, headers={"ETag": '"v1"'})
    harness.route("index.tar.gz", exact=True)
    await harness.get("index.tar.gz")
    harness.clock.now += 301
    harness.upstream.set(
        "/index.tar.gz", gzip.compress(TARBALL), headers={"Content-Encoding": "gzip"}
    )
    assert (await harness.get("index.tar.gz"))[:2] == (200, TARBALL)
    assert (await harness.stats())["stale"] == 1


@pytest.mark.parametrize("body", [b"<html>Service Unavailable</html>", b"", b"\x1f"])
async def test_a_body_without_its_magic_number_is_refused(harness: Harness, body: bytes) -> None:
    # One bad 200 from upstream or a CDN would otherwise be stored and served to all.
    harness.upstream.set("/split-0.2.5.tar.gz", body)
    harness.route("split-0.2.5.tar.gz", kind=Kind.ARTIFACT, magic=GZIP_MAGIC)
    status, answer, _ = await harness.get("split-0.2.5.tar.gz")
    assert (status, answer) == (502, b"unexpected upstream content\n")
    assert (await harness.stats())["entries"] == 0
    assert harness.files("blobs") == harness.files("tmp") == []
    harness.upstream.set("/split-0.2.5.tar.gz", TARBALL)
    assert (await harness.get("split-0.2.5.tar.gz"))[:2] == (200, TARBALL)


async def test_an_unverified_artifact_is_revalidated_after_its_interval(harness: Harness) -> None:
    harness.upstream.set("/s.tar.gz", TARBALL, headers={"ETag": '"v1"'})
    harness.route("s.tar.gz", kind=Kind.ARTIFACT, magic=GZIP_MAGIC, revalidate_after=86400)
    await harness.get("s.tar.gz")
    harness.clock.now += 86399
    assert (await harness.get("s.tar.gz"))[:2] == (200, TARBALL)
    assert harness.upstream.count("/s.tar.gz") == 1
    # A day on: a conditional request, which confirms the copy.
    harness.clock.now += 2
    harness.upstream.set("/s.tar.gz", status=304, headers={"ETag": '"v1"'})
    assert (await harness.get("s.tar.gz"))[:2] == (200, TARBALL)
    seen = harness.upstream.requests[-1].headers
    assert (seen["if-none-match"], seen["accept-encoding"]) == ('"v1"', "identity")
    assert (await harness.get("s.tar.gz"))[:2] == (200, TARBALL)
    assert harness.upstream.count("/s.tar.gz") == 2
    # Another day on, upstream has other bytes (the stored copy was bad): replaced.
    harness.clock.now += 86401
    fixed = gzip.compress(b"the right source distribution")
    harness.upstream.set("/s.tar.gz", fixed, headers={"ETag": '"v2"'})
    assert (await harness.get("s.tar.gz"))[:2] == (200, fixed)
    stats = await harness.stats()
    assert (stats["entries"], stats["misses"], stats["revalidated"]) == (1, 2, 1)
    assert len(harness.files("blobs")) == 1


@pytest.mark.parametrize(
    ("status", "body"), [(500, b"broken"), (404, b"gone"), (200, b"<html>maintenance</html>")]
)
async def test_a_stored_artifact_outlives_a_failed_revalidation(
    harness: Harness, status: int, body: bytes
) -> None:
    # However old the copy, a failure to confirm it (an error, a 404 or a body that is
    # no gzip file) serves it rather than nothing.
    harness.upstream.set("/s.tar.gz", TARBALL, headers={"ETag": '"v1"'})
    harness.route("s.tar.gz", kind=Kind.ARTIFACT, magic=GZIP_MAGIC, revalidate_after=86400)
    await harness.get("s.tar.gz")
    harness.clock.now += 30 * 86400
    harness.upstream.set("/s.tar.gz", body, status=status)
    assert (await harness.get("s.tar.gz"))[:2] == (200, TARBALL)
    assert (await harness.stats())["stale"] == 1
    assert harness.cache.store.get("pkg:s.tar.gz") is not None


# What reaches adapters and upstreams.


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def test_other_methods_are_405(harness: Harness, method: str) -> None:
    harness.route("index")
    for path in ("/pkg/index", "/-/health", "/-/stats", "/elsewhere"):
        async with harness.client.request(method, path) as response:
            assert response.status == 405
            assert response.headers["Allow"] == "GET, HEAD"
    assert harness.adapter.seen == []
    assert harness.upstream.requests == []


async def test_unknown_prefixes_and_unrouted_paths_are_404(harness: Harness) -> None:
    for path in ("/", "/other/x", "/pkg", "/pkg/", "/pkg/unrouted", "/-/other", "/PKG/x"):
        async with harness.client.get(path) as response:
            assert response.status == 404, path
    assert [tail for tail, _ in harness.adapter.seen] == ["", "", "unrouted"]


@pytest.mark.parametrize(
    "target",
    [
        "/pkg/../secret",
        "/pkg/a/../../x",
        "/pkg/%2e%2e/x",
        "/pkg/%2E%2e/x",
        "/pkg/.%2e/x",
        "/pkg/a/./b",
        "/pkg/a%2F..%2Fb",
        "/pkg/a%2f%2e%2e%2fb",
        "/pkg/%C0%AE%C0%AE/x",
        "/pkg/a%5Cb",
        "/pkg/a%5C..%5Cb",
        "/pkg/a\\b",
        "/pkg/a%00b",
        "/pkg/a%0Ab",
        "/pkg/a%0d%0aX-Injected:%201",
        "/pkg/a%C2%85b",
        "/pkg/%ff",
        "/pkg/a%zz",
        "/pkg/a%",
        "/pkg/a|b",
        "/pkg/a<b>",
        "/pkg/a#b",
        "pkg/x",
        "http://cache.test/pkg/x",
    ],
)
async def test_unsafe_paths_never_reach_an_adapter(harness: Harness, target: str) -> None:
    harness.route("x")
    assert await raw_request(harness.client, target) == 400
    assert harness.adapter.seen == []


async def test_adapters_get_the_raw_path_and_lowercased_headers(harness: Harness) -> None:
    target = "/pkg/@scope%2Fname?write=true"
    status = await raw_request(harness.client, target, headers={"X-Test": "1", "Accept": "a/b"})
    assert status == 404
    tail, headers = harness.adapter.seen[0]
    assert tail == "@scope%2Fname"
    assert headers["x-test"] == "1"
    assert headers["accept"] == "a/b"


async def test_client_headers_never_reach_upstream(harness: Harness) -> None:
    harness.upstream.set("/index", b"x")
    harness.route("index", headers={"Accept": "application/vnd.test+json"})
    client_headers = {
        "Authorization": "Bearer test-secret-token",
        "Cookie": "session=test-secret",
        "X-Forwarded-For": "10.0.0.1",
        "Accept": "text/html",
    }
    assert (await harness.get("index", headers=client_headers))[0] == 200
    sent = harness.upstream.requests[-1].headers
    assert sent["accept"] == "application/vnd.test+json"
    assert sent["user-agent"] == USER_AGENT
    assert not {"authorization", "cookie", "x-forwarded-for"} & set(sent)
    assert harness.adapter.seen[0][1]["authorization"] == "Bearer test-secret-token"


async def test_urls_outside_the_configured_upstreams_are_refused(harness: Harness) -> None:
    harness.adapter.routes["escape"] = Fetch(
        key="escape", url="https://elsewhere.invalid/x", kind=Kind.METADATA
    )
    assert (await harness.get("escape"))[:2] == (502, b"upstream URL refused\n")
    assert harness.upstream.requests == []


@pytest.mark.parametrize(
    "location",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/x",
        "https://10.0.0.5/x",
        "https://registry.internal/x",
    ],
)
async def test_redirects_to_hosts_that_are_not_public_are_refused(
    harness: Harness, location: str
) -> None:
    harness.upstream.set("/moved.tgz", status=302, headers={"Location": location})
    harness.route("moved.tgz", kind=Kind.ARTIFACT)
    assert (await harness.get("moved.tgz"))[:2] == (502, b"upstream redirect refused\n")
    assert (await harness.stats())["entries"] == 0


async def test_redirects_are_followed_but_only_to_https(harness: Harness) -> None:
    harness.upstream.set("/moved", status=301, headers={"Location": "/new"})
    harness.upstream.set("/new", b"new home")
    harness.route("moved")
    assert (await harness.get("moved"))[:2] == (200, b"new home")
    insecure = harness.upstream.origin.replace("https://", "http://") + "/new"
    harness.upstream.set("/insecure", status=302, headers={"Location": insecure})
    harness.route("insecure")
    assert (await harness.get("insecure"))[:2] == (502, b"upstream redirect refused\n")
    assert harness.upstream.count("/new") == 1


# Sharing fetches, the rate limit and eviction.


async def test_concurrent_requests_share_one_fetch(harness: Harness) -> None:
    gate = asyncio.Event()
    body = b"z" * 500_000
    harness.upstream.set("/big.tgz", body, gate=gate)
    harness.route("big.tgz", kind=Kind.ARTIFACT)
    requests = [asyncio.create_task(harness.get("big.tgz")) for _ in range(5)]
    await until(lambda: len(harness.adapter.seen) == 5)
    await until(lambda: harness.upstream.count("/big.tgz") == 1)
    await asyncio.sleep(0.1)  # let the last lookups join the running fetch
    gate.set()
    results = await asyncio.gather(*requests)
    assert [result[:2] for result in results] == [(200, body)] * 5
    assert harness.upstream.count("/big.tgz") == 1


async def test_a_client_that_goes_away_does_not_cancel_the_shared_fetch(
    harness: Harness,
) -> None:
    gate = asyncio.Event()
    harness.upstream.set("/a.tgz", b"kept", gate=gate)
    harness.route("a.tgz", kind=Kind.ARTIFACT)
    request = asyncio.create_task(harness.get("a.tgz"))
    await until(lambda: harness.upstream.count("/a.tgz") == 1)
    request.cancel()
    gate.set()
    await until(lambda: harness.cache.store.get("pkg:a.tgz") is not None)
    assert (await harness.get("a.tgz"))[:2] == (200, b"kept")
    assert harness.upstream.count("/a.tgz") == 1


async def test_a_client_that_stops_reading_is_not_an_error(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    body = b"w" * 20_000_000
    harness.upstream.set("/huge.tgz", body)
    harness.route("huge.tgz", kind=Kind.ARTIFACT)
    assert (await harness.get("huge.tgz", method="HEAD"))[0] == 200
    reader, writer = await asyncio.open_connection(harness.client.host, harness.client.port)
    writer.write(b"GET /pkg/huge.tgz HTTP/1.1\r\nHost: cache.test\r\n\r\n")
    assert (await reader.readline()).startswith(b"HTTP/1.1 200")
    writer.transport.abort()
    await asyncio.sleep(0.2)
    assert (await harness.get("huge.tgz"))[:2] == (200, body)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_a_client_that_stalls_is_cut_off_and_the_file_closed(
    make_harness: HarnessFactory, caplog: pytest.LogCaptureFixture
) -> None:
    harness = await make_harness(send_timeout=0.3)
    body = b"v" * 40_000_000  # far more than the socket buffers hold
    harness.upstream.set("/huge.tgz", body)
    harness.route("huge.tgz", kind=Kind.ARTIFACT)
    assert (await harness.get("huge.tgz", method="HEAD"))[0] == 200
    blob = str(harness.cache.store.blob_path(hashlib.sha256(body).hexdigest()))
    reader, writer = await asyncio.open_connection(harness.client.host, harness.client.port)
    try:
        writer.write(b"GET /pkg/huge.tgz HTTP/1.1\r\nHost: cache.test\r\n\r\n")
        assert (await reader.readline()).startswith(b"HTTP/1.1 200")
        assert harness.cache.active_requests == 1
        assert blob in open_files()
        # The client reads nothing more; the cache gives up within a few send timeouts.
        await until(lambda: harness.cache.active_requests == 0, timeout=5)
        assert blob not in open_files()
        received = b""
        with contextlib.suppress(ConnectionResetError):
            async with asyncio.timeout(5):
                received = await reader.read()
        assert len(received) < len(body)
    finally:
        writer.close()
    assert (await harness.get("huge.tgz"))[:2] == (200, body)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_answers_built_in_memory_have_a_deadline_too(
    make_harness: HarnessFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.package_cache.app")
    harness = await make_harness(send_timeout=0.3)
    body = b"m" * 20_000_000
    harness.adapter.routes["big.json"] = Static(body, "application/json")
    reader, writer = await asyncio.open_connection(harness.client.host, harness.client.port)
    try:
        writer.write(b"GET /pkg/big.json HTTP/1.1\r\nHost: cache.test\r\n\r\n")
        assert (await reader.readline()).startswith(b"HTTP/1.1 200")
        await until(
            lambda: any("stopped receiving an answer" in r.getMessage() for r in caplog.records)
        )
        received = b""
        with contextlib.suppress(ConnectionResetError):
            async with asyncio.timeout(5):
                received = await reader.read()
        assert len(received) < len(body)
    finally:
        writer.close()
    assert (await harness.get("big.json"))[:2] == (200, body)


async def test_requests_beyond_max_requests_are_answered_503(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(max_requests=2)
    gate = asyncio.Event()
    harness.upstream.set("/slow", b"slow", gate=gate)
    harness.route("slow")
    harness.upstream.set("/fast", b"fast")
    harness.route("fast")
    waiting = [asyncio.create_task(harness.get("slow")) for _ in range(2)]
    await until(lambda: harness.cache.active_requests == 2)
    status, body, headers = await harness.get("fast")
    assert (status, body, headers["Retry-After"]) == (503, b"too many requests, retry later\n", "1")
    assert harness.upstream.count("/fast") == 0
    async with harness.client.get("/-/health") as response:
        assert response.status == 200
    assert (await harness.stats())["entries"] == 0
    gate.set()
    assert [result[:2] for result in await asyncio.gather(*waiting)] == [(200, b"slow")] * 2
    assert harness.cache.active_requests == 0
    assert (await harness.get("fast"))[:2] == (200, b"fast")


async def test_artifact_downloads_beyond_the_cap_are_503_while_index_data_flows(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(max_artifact_downloads=1, download_wait=0.2)
    gate = asyncio.Event()
    harness.upstream.set("/a.tgz", b"a", gate=gate)
    harness.route("a.tgz", kind=Kind.ARTIFACT)
    harness.upstream.set("/b.tgz", b"b")
    harness.route("b.tgz", kind=Kind.ARTIFACT)
    harness.upstream.set("/index", b"index")
    harness.route("index")
    first = asyncio.create_task(harness.get("a.tgz"))
    await until(lambda: harness.upstream.count("/a.tgz") == 1)
    status, body, headers = await harness.get("b.tgz")
    assert (status, body, headers["Retry-After"]) == (503, b"cache busy, retry later\n", "1")
    assert harness.upstream.count("/b.tgz") == 0
    assert (await harness.get("index"))[:2] == (200, b"index")
    gate.set()
    assert (await first)[:2] == (200, b"a")
    assert (await harness.get("b.tgz"))[:2] == (200, b"b")


async def test_downloads_that_would_overflow_the_temp_space_are_503(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(
        max_size="1Mi", max_artifact_size="600", max_metadata_size="600", max_temp_size="1000"
    )
    pause = asyncio.Event()
    harness.upstream.set("/a.tgz", b"a" * 600, pause=pause, pause_after=100)
    harness.route("a.tgz", kind=Kind.ARTIFACT)
    harness.upstream.set("/b.tgz", b"b" * 600)
    harness.route("b.tgz", kind=Kind.ARTIFACT)
    first = asyncio.create_task(harness.get("a.tgz"))
    await until(lambda: len(harness.files("tmp")) == 1)
    status, body, headers = await harness.get("b.tgz")
    assert (status, body, headers["Retry-After"]) == (503, b"cache busy, retry later\n", "1")
    pause.set()
    assert (await first)[:2] == (200, b"a" * 600)
    assert (await harness.get("b.tgz"))[:2] == (200, b"b" * 600)
    assert harness.files("tmp") == []


async def test_the_upstream_rate_limit_answers_503(make_harness: HarnessFactory) -> None:
    harness = await make_harness(upstream_rate=0.001, upstream_burst=1, rate_limit_wait=0)
    harness.upstream.set("/a", b"a")
    harness.upstream.set("/b", b"b")
    harness.route("a")
    harness.route("b")
    assert (await harness.get("a"))[:2] == (200, b"a")
    status, _, headers = await harness.get("b")
    assert (status, headers["Retry-After"]) == (503, "1")
    assert (await harness.get("a"))[:2] == (200, b"a")
    assert harness.upstream.count("/b") == 0


async def test_eviction_keeps_the_store_under_its_cap(make_harness: HarnessFactory) -> None:
    # Each entry counts its 300 bytes and 1 KiB of overhead: four are over the cap.
    harness = await make_harness(max_size="5000", max_artifact_size="400", max_metadata_size="400")
    for name in "abcd":
        harness.upstream.set(f"/{name}.tgz", name.encode() * 300)
        harness.route(f"{name}.tgz", kind=Kind.ARTIFACT)
        assert (await harness.get(f"{name}.tgz"))[0] == 200
        harness.clock.now += 61
    store = harness.cache.store
    await until(lambda: len(store.keys()) == 3)
    assert store.keys() == ["pkg:b.tgz", "pkg:c.tgz", "pkg:d.tgz"]
    assert store.stats().bytes == 900
    assert len(harness.files("blobs")) == 3


async def test_not_found_answers_cannot_grow_the_index_without_limit(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(max_size="5000", max_artifact_size="400", max_metadata_size="400")
    store = harness.cache.store
    for number in range(10):
        harness.upstream.set(f"/made-up-{number}", b"not found", status=404)
        harness.route(f"made-up-{number}")
        assert (await harness.get(f"made-up-{number}"))[0] == 404
        harness.clock.now += 61
    # They share one 9-byte blob, but each counts 1 KiB toward the 5000-byte cap.
    await until(lambda: len(store.keys()) == 4)
    assert store.stats().bytes == 9
    # Once past their TTL and stale_if_error, eviction purges them before anything else.
    harness.clock.now += 86400
    harness.upstream.set("/exists", b"x" * 400)
    harness.route("exists")
    assert (await harness.get("exists"))[0] == 200
    harness.upstream.set("/made-up-new", b"not found", status=404)
    harness.route("made-up-new")
    assert (await harness.get("made-up-new"))[0] == 404
    await until(lambda: len(store.keys()) == 2)
    assert store.keys() == ["pkg:exists", "pkg:made-up-new"]


# Status endpoints, logging and setup.


async def test_health_and_stats(harness: Harness) -> None:
    for target in ("/-/health", "/-/health?probe=1"):
        async with harness.client.get(target) as response:
            assert (response.status, await response.json()) == (200, {"status": "ok"})
    async with harness.client.head("/-/health") as response:
        assert response.status == 200
    assert set(await harness.stats()) == STATS_KEYS


async def test_each_request_is_logged_once(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.package_cache.requests")
    harness.upstream.set("/index", b"0123456789")
    harness.route("index")
    await harness.get("index")
    await harness.get("index")
    await harness.get("index", method="HEAD")
    await harness.get("unrouted")
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sandbox_science.package_cache.requests"
    ]
    assert len(lines) == 4
    assert lines[0].startswith(
        "ecosystem=pkg method=GET path=/pkg/index result=miss status=200 bytes=10 upstream_ms="
    )
    assert not lines[0].endswith("upstream_ms=-")
    assert lines[1:] == [
        "ecosystem=pkg method=GET path=/pkg/index result=hit status=200 bytes=10 upstream_ms=-",
        "ecosystem=pkg method=HEAD path=/pkg/index result=hit status=200 bytes=0 upstream_ms=-",
        "ecosystem=pkg method=GET path=/pkg/unrouted result=- status=404 bytes=0 upstream_ms=-",
    ]


async def test_an_adapter_failure_is_500_and_logged(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.adapter.broken = True
    assert (await harness.get("index"))[0] == 500
    assert any("the pkg adapter failed" in record.getMessage() for record in caplog.records)
    assert (await harness.stats())["errors"] == 1


async def test_adapters_are_registered_under_their_prefix(
    core_settings: Callable[..., Settings],
) -> None:
    settings = core_settings("https://registry.test")
    for name, prefix in (("other", "pkg"), ("-", "-"), ("Pkg", "Pkg"), ("a/b", "a/b")):
        with pytest.raises(ValueError, match="prefix"):
            create_app(settings, {name: FakeAdapter(prefix)})


async def test_a_connector_must_leave_connections_for_index_data(
    core_settings: Callable[..., Settings],
) -> None:
    settings = core_settings("https://registry.test", max_artifact_downloads=4)
    connector = aiohttp.TCPConnector(limit=4)
    try:
        with pytest.raises(ValueError, match="index data"):
            create_app(settings, {"pkg": FakeAdapter("pkg")}, connector=connector)
    finally:
        await connector.close()


async def test_two_caches_cannot_share_a_directory(make_harness: HarnessFactory) -> None:
    first = await make_harness()
    server = TestServer(create_app(first.settings, {"pkg": FakeAdapter("pkg")}))
    try:
        with pytest.raises(StoreError, match="in use"):
            await server.start_server()
    finally:
        await server.close()
