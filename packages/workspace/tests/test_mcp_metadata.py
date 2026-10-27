# SPDX-License-Identifier: MIT
"""The user's availability check asks the control service, with the workspace's token, for
one listing by the agent runtime's client (D98), and keeps nothing but the status."""

from __future__ import annotations

import asyncio
import datetime
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from sandbox_science.workspace.mcp_metadata import CHECK_INTERVAL, AvailabilityClient

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
TOKEN = "test-secret-workspace-check-0123456789abcdef"


async def test_a_check_asks_the_control_service_and_keeps_the_status(
    aiohttp_client: ClientFactory,
) -> None:
    asked: list[dict[str, Any]] = []
    answer: dict[str, Any] = {
        "status": "listed",
        "tool_count": 12,
        "digest": "d" * 64,
        "error_kind": None,
    }

    async def check(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        asked.append(await request.json())
        return web.json_response(answer)

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    client = AvailabilityClient(str(control.make_url("")), TOKEN)
    found = await client.check("papers")
    assert (found.status, found.tool_count) == ("listed", 12)
    assert asked == [{"server": "papers"}]
    assert client.checks["papers"] == found
    assert "digest" not in found.model_dump()  # only the status and the count are kept
    answer = {"status": "timeout", "tool_count": None, "digest": None, "error_kind": "timeout"}
    assert (await client.check("papers")).status == "listed"  # within 30 s: the kept outcome
    assert len(asked) == 1


async def test_a_server_is_checked_at_most_once_in_the_interval(
    aiohttp_client: ClientFactory,
) -> None:
    asked: list[str] = []

    async def check(request: web.Request) -> web.Response:
        asked.append((await request.json())["server"])
        return web.json_response({"status": "listed", "tool_count": len(asked)})

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    now = [1000.0]
    client = AvailabilityClient(str(control.make_url("")), TOKEN, clock=lambda: now[0])
    first = await client.check("papers")
    now[0] += CHECK_INTERVAL - 1
    assert await client.check("papers") == first  # too soon: nothing is asked
    assert (await client.check("notes")).tool_count == 2  # another server is its own
    now[0] += 1
    assert (await client.check("papers")).tool_count == 3
    assert asked == ["papers", "notes", "papers"]


@pytest.mark.parametrize(
    "reply",
    [
        web.Response(status=404),
        web.Response(status=500),
        web.Response(body=b"not json"),
        web.json_response({"status": "everything is fine"}),
        web.Response(body=b"{" + b" " * (64 * 1024 + 1) + b"}", content_type="application/json"),
    ],
)
async def test_anything_but_a_listing_outcome_is_unavailable(
    aiohttp_client: ClientFactory, reply: web.Response
) -> None:
    async def check(request: web.Request) -> web.Response:
        return reply

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    found = await AvailabilityClient(str(control.make_url("")), TOKEN).check("papers")
    assert found.status == "unavailable"


async def test_an_absent_control_service_is_unavailable() -> None:
    found = await AvailabilityClient("http://127.0.0.1:9", TOKEN).check("papers")
    assert found.status == "unavailable"


async def test_one_check_at_a_time(aiohttp_client: ClientFactory) -> None:
    running, most = 0, 0

    async def check(request: web.Request) -> web.Response:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        return web.json_response({"status": "listed", "tool_count": 1})

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    client = AvailabilityClient(str(control.make_url("")), TOKEN)
    await asyncio.gather(client.check("a"), client.check("b"), client.check("c"))
    assert most == 1


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("http://control.example.org:8202", TOKEN),  # plain http off loopback
        ("https://user:pw@control.example.org", TOKEN),
        ("ftp://127.0.0.1", TOKEN),
        ("http://127.0.0.1:8202", "test-secret-line1\ntest-secret-line2-0123456789"),
        ("http://127.0.0.1:8202", "short"),
    ],
)
def test_an_address_or_token_the_client_should_not_use_is_refused(url: str, token: str) -> None:
    with pytest.raises(ValueError, match=r"control service|token") as refused:
        AvailabilityClient(url, token)
    assert "test-secret" not in str(refused.value)


def _private_ca(directory: Path) -> tuple[ssl.SSLContext, Path]:
    """A test-only self-signed certificate for localhost, and the server context with it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "control.crt", directory / "control.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return context, cert_file


async def test_an_https_control_service_is_checked_with_its_private_ca(tmp_path: Path) -> None:
    """The Kubernetes bundle's control service is https, signed by the site's private CA."""

    async def check(request: web.Request) -> web.Response:
        return web.json_response({"status": "listed", "tool_count": 2})

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    context, ca = _private_ca(tmp_path)
    server = TestServer(app, host="localhost")
    await server.start_server(ssl=context)
    try:
        url = f"https://localhost:{server.port}"
        found = await AvailabilityClient(url, TOKEN, ca_file=ca).check("papers")
        assert (found.status, found.tool_count) == ("listed", 2)
        # Without the CA the certificate is not trusted: unavailable, never an error.
        assert (await AvailabilityClient(url, TOKEN).check("papers")).status == "unavailable"
    finally:
        await server.close()
    with pytest.raises(ValueError, match="https"):
        AvailabilityClient("http://127.0.0.1:8202", TOKEN, ca_file=ca)
