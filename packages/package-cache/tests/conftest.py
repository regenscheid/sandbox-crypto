# SPDX-License-Identifier: MIT
"""Shared fixtures for the cache core's tests: a local TLS upstream and settings.

Fixtures are prefixed ``core_`` so they do not collide with the adapters'
tests. Test modules cannot import this file (``--import-mode=importlib``), so
they describe the fixture objects they use with protocols of their own.
"""

import asyncio
import datetime
import ipaddress
import ssl
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from sandbox_science.package_cache.settings import Settings


@dataclass(frozen=True)
class CoreTls:
    """TLS contexts for a test upstream on 127.0.0.1: one serves, one trusts it."""

    server: ssl.SSLContext
    client: ssl.SSLContext


@pytest.fixture(scope="session")
def core_tls(tmp_path_factory: pytest.TempPathFactory) -> CoreTls:
    """A throwaway CA and a certificate for 127.0.0.1 and localhost, made at test time."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "package cache test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls")
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(ca_path))
    return CoreTls(server=server, client=client)


@dataclass
class CannedResponse:
    """What the test upstream answers for one path."""

    body: bytes = b""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict[str, str])
    chunked: bool = False
    """Stream the body without a Content-Length."""
    gate: asyncio.Event | None = None
    """Wait for this event before answering."""
    pause: asyncio.Event | None = None
    """Send the headers and the first ``pause_after`` bytes, then wait for this event."""
    pause_after: int = 0


@dataclass(frozen=True)
class SeenRequest:
    """A request the test upstream received; header names are lowercased."""

    method: str
    path: str
    headers: dict[str, str]


class CoreUpstream:
    """A local TLS server that answers canned responses and records every request."""

    def __init__(self) -> None:
        self.origin = ""
        self.responses: dict[str, CannedResponse] = {}
        self.requests: list[SeenRequest] = []

    def url(self, path: str) -> str:
        """The https URL of ``path`` on this upstream."""
        return self.origin + path

    def set(self, path: str, body: bytes = b"", **options: object) -> None:
        """Answer ``path`` with ``body``; options are CannedResponse fields."""
        self.responses[path] = CannedResponse(body, **options)  # pyright: ignore[reportArgumentType]

    def count(self, path: str) -> int:
        """How many requests for ``path`` arrived."""
        return sum(1 for request in self.requests if request.path == path)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        headers = {name.lower(): value for name, value in request.headers.items()}
        self.requests.append(SeenRequest(request.method, request.raw_path, headers))
        canned = self.responses.get(request.raw_path)
        if canned is None:
            return web.Response(status=404, text="no canned response")
        if canned.gate is not None:
            await canned.gate.wait()
        if not canned.chunked and canned.pause is None:
            return web.Response(status=canned.status, body=canned.body, headers=canned.headers)
        response = web.StreamResponse(status=canned.status, headers=canned.headers)
        if canned.chunked:
            response.enable_chunked_encoding()
        else:
            response.content_length = len(canned.body)
        await response.prepare(request)
        start = 0
        if canned.pause is not None:
            await response.write(canned.body[: canned.pause_after])
            await canned.pause.wait()
            start = canned.pause_after
        for offset in range(start, len(canned.body), 4096):
            await response.write(canned.body[offset : offset + 4096])
        await response.write_eof()
        return response


@pytest_asyncio.fixture
async def core_upstream(core_tls: CoreTls) -> AsyncIterator[CoreUpstream]:
    """A started TLS upstream on 127.0.0.1, trusted by ``core_tls.client``."""
    upstream = CoreUpstream()
    app = web.Application()
    app.router.add_route("*", r"/{path:[\s\S]*}", upstream.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(ssl=core_tls.server)
    upstream.origin = f"https://127.0.0.1:{server.port}"
    yield upstream
    await server.close()


@pytest.fixture
def core_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Build settings with npm's registry at ``origin``; keyword arguments override keys."""

    def make(origin: str, **overrides: object) -> Settings:
        data: dict[str, object] = {
            "public_url": "http://cache.test:8090",
            "cache_dir": tmp_path / "cache",
            "npm": {"registry": origin},
        }
        data.update(overrides)
        return Settings.model_validate(data)

    return make
