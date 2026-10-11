# SPDX-License-Identifier: MIT
"""Which opam packages an archive belongs to: the cache's table and its route.

The cache runs with the real opam adapter in front of the local TLS upstream,
which serves an ``index.tar.gz`` built from the captured opam files.
"""

import asyncio
import io
import json
import ssl
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_aiohttp import AiohttpClient

from sandbox_science.package_cache.adapters.opam import OpamAdapter
from sandbox_science.package_cache.app import create_app
from sandbox_science.package_cache.settings import Settings

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "opam"
OCAMLFIND_MD5 = "ca770e5806032a96131b670f6e07f146"
ZED_SHA512 = (
    "637f75129550f6459417549d44bed16bdc62721d2e9e0c6bb5bfab30c5bc6478"
    "de15faece8c091b56f238375cb79a7bc176375400e543120bb31d7ea626b7c5b"
)
UNLISTED = "0123456789abcdef" * 4


class Upstream(Protocol):
    """The ``core_upstream`` fixture."""

    origin: str

    def set(self, path: str, body: bytes = b"", **options: object) -> None: ...
    def count(self, path: str) -> int: ...


class Tls(Protocol):
    """The ``core_tls`` fixture."""

    client: ssl.SSLContext


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


Client = TestClient[web.Request, web.Application]


def index_tar_gz(*packages: str) -> bytes:
    """An index.tar.gz with the captured opam files of ``packages`` (their ids)."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    files = {
        entry["package"]: entry["file"] for entry in manifest["fixtures"] if "package" in entry
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for package in packages:
            content = (FIXTURES / files[package]).read_bytes()
            info = tarfile.TarInfo(f"packages/{package.split('.', 1)[0]}/{package}/opam")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


async def lookup(client: Client, path: str) -> tuple[int, Any]:
    async with client.get(f"/-/opam/archives/{path}") as response:
        if response.content_type == "application/json":
            return response.status, await response.json()
        return response.status, await response.text()


async def until_listed(client: Client, path: str) -> Any:
    """Wait until the table answers for ``path``, and return the answer."""
    async with asyncio.timeout(5):
        while True:
            status, answer = await lookup(client, path)
            if status == 200:
                return answer
            assert status == 503
            await asyncio.sleep(0.01)


Factory = Callable[..., Awaitable[Client]]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_cache(
    aiohttp_client: AiohttpClient,
    core_upstream: Upstream,
    core_tls: Tls,
    core_settings: Callable[..., Settings],
    clock: Clock,
) -> Factory:
    """Start caches on one cache directory, with or without the opam adapter."""

    async def make(*, opam: bool = True) -> Client:
        origin = core_upstream.origin
        settings = core_settings(origin, opam={"repository": origin})
        adapters = {"opam": OpamAdapter(repository=origin)} if opam else {}
        app = create_app(settings, adapters, ssl_context=core_tls.client, clock=clock)
        return await aiohttp_client(app)

    return make


async def test_the_packages_of_an_archive_come_from_the_stored_index(
    make_cache: Factory, core_upstream: Upstream
) -> None:
    client = await make_cache()
    # Nothing to answer from before opam has fetched an index through the cache.
    status, text = await lookup(client, f"md5/{OCAMLFIND_MD5}")
    assert (status, text) == (503, "the opam index has not been read yet\n")
    core_upstream.set("/index.tar.gz", index_tar_gz("ocamlfind.1.9.8", "zed.3.2.3"))
    async with client.get("/opam/index.tar.gz") as response:
        assert response.status == 200
    assert await until_listed(client, f"md5/{OCAMLFIND_MD5}") == {
        "digest": f"md5:{OCAMLFIND_MD5}",
        "packages": ["ocamlfind.1.9.8"],
    }
    assert (await lookup(client, f"sha512/{ZED_SHA512}"))[1]["packages"] == ["zed.3.2.3"]
    # A digest no opam file lists: an empty list, which an approver should question.
    assert await lookup(client, f"sha256/{UNLISTED}") == (
        200,
        {"digest": f"sha256:{UNLISTED}", "packages": []},
    )
    # The table is the cache's own: the lookups reached no upstream.
    assert core_upstream.count("/index.tar.gz") == 1


async def test_the_table_follows_the_index(
    make_cache: Factory, core_upstream: Upstream, clock: Clock
) -> None:
    client = await make_cache()
    core_upstream.set("/index.tar.gz", index_tar_gz("ocamlfind.1.9.8"), headers={"ETag": '"1"'})
    await client.get("/opam/index.tar.gz")
    assert (await until_listed(client, f"sha512/{ZED_SHA512}"))["packages"] == []
    clock.now += 301  # past the TTL: opam update fetches a newer index
    core_upstream.set(
        "/index.tar.gz", index_tar_gz("ocamlfind.1.9.8", "zed.3.2.3"), headers={"ETag": '"2"'}
    )
    await client.get("/opam/index.tar.gz")
    async with asyncio.timeout(5):
        while (await lookup(client, f"sha512/{ZED_SHA512}"))[1]["packages"] != ["zed.3.2.3"]:
            await asyncio.sleep(0.01)


async def test_an_index_stored_earlier_is_read_at_startup(
    make_cache: Factory, core_upstream: Upstream
) -> None:
    first = await make_cache()
    core_upstream.set("/index.tar.gz", index_tar_gz("ocamlfind.1.9.8"))
    await first.get("/opam/index.tar.gz")
    await first.close()
    second = await make_cache()
    answer = await until_listed(second, f"md5/{OCAMLFIND_MD5}")
    assert answer["packages"] == ["ocamlfind.1.9.8"]
    assert core_upstream.count("/index.tar.gz") == 1


async def test_an_index_that_is_no_tar_file_lists_nothing(
    make_cache: Factory, core_upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    client = await make_cache()
    core_upstream.set("/index.tar.gz", b"\x1f\x8b but no gzip stream")
    async with client.get("/opam/index.tar.gz") as response:
        assert response.status == 200  # opam reads it and fails: the cache serves as published
    async with asyncio.timeout(5):
        while "cannot read the opam index" not in caplog.text:
            await asyncio.sleep(0.01)
    for _ in range(3):
        assert (await lookup(client, f"md5/{OCAMLFIND_MD5}"))[0] == 503
    await asyncio.sleep(0.1)
    # Read once: lookups do not unpack the same unreadable index again.
    warnings = [record for record in caplog.records if "opam index" in record.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "path",
    [
        f"md5/{OCAMLFIND_MD5.upper()}",
        f"md5/{OCAMLFIND_MD5[:-1]}",
        f"sha256/{OCAMLFIND_MD5}",
        f"sha1/{'0' * 40}",
        f"md5/ca/{OCAMLFIND_MD5}",
        f"md5/{OCAMLFIND_MD5}/",
        "md5",
        "",
    ],
)
async def test_malformed_digests_are_400(make_cache: Factory, path: str) -> None:
    client = await make_cache()
    assert (await lookup(client, path))[0] == 400


async def test_without_opam_there_is_no_table(make_cache: Factory) -> None:
    client = await make_cache(opam=False)
    assert (await lookup(client, f"md5/{OCAMLFIND_MD5}"))[0] == 404
