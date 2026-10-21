# SPDX-License-Identifier: MIT
"""Real local TLS callbacks, admission and shutdown with synthetic credentials."""

from __future__ import annotations

import asyncio
import datetime
import ssl
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator.confinement.endpoints import ServerTLS, client_tls
from sandbox_science.orchestrator.confinement.runtime import RuntimeSettings, serve
from sandbox_science.orchestrator.confinement.server import (
    ControlSettings,
    create_app,
    private_token,
)
from sandbox_science.orchestrator.confinement.transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    Capability,
    Request,
    TransportError,
)
from sandbox_science.testing import eventually

from .test_confinement_control import TOKEN, WORKER, private


def certificates(directory: Path) -> tuple[ServerTLS, Path]:
    """A test-only self-signed server CA, valid for localhost and no IP address."""
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
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "server.pem", directory / "server-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return ServerTLS(certificate=cert_file, private_key=key_file), cert_file


def settings(directory: Path, worker: str, **fields: object) -> ControlSettings:
    return ControlSettings.model_validate(
        {
            "state_dir": directory / "state",
            "worker": worker,
            "worker_token_file": private(directory / "worker.token", WORKER),
            "runtime_token_file": private(directory / "runtime.token", TOKEN),
            "generation": "runtime-test",
            "projects": ("p",),
            "main": "main",
            "image_revision": "a" * 40,
            **fields,
        }
    )


def runtime_settings(directory: Path, url: str, ca: Path | None) -> RuntimeSettings:
    image = directory / "image.json"
    image.write_text('{"profile":"orchestrator","revision":"' + "a" * 40 + '"}')
    return RuntimeSettings(
        control_url=url,
        control_ca_file=ca,
        token_file=directory / "runtime.token",
        model_url="http://unused-model.example.invalid/v1",
        model_key_file=private(directory / "model.key", "test-secret-model-unused"),
        models=("main",),
        scratch=directory / "scratch",
        image_record=image,
        generation="runtime-test",
    )


@pytest.mark.parametrize("listen", ["0.0.0.0:8202", "10.0.0.1:8202", "[::]:8202"])
def test_non_loopback_control_requires_explicit_tls(tmp_path: Path, listen: str) -> None:
    with pytest.raises(ValidationError, match="requires TLS"):
        settings(tmp_path, "http://unused.invalid", listen=listen)
    tls, _ = certificates(tmp_path)
    assert settings(tmp_path, "http://unused.invalid", listen=listen, tls=tls).listen == listen


@pytest.mark.parametrize("listen", ["localhost:8202", "127.0.0.1:0", "::1:8202", "127.0.0.1:65536"])
def test_control_bind_is_an_explicit_valid_ip_port(tmp_path: Path, listen: str) -> None:
    with pytest.raises(ValidationError):
        settings(tmp_path, "http://unused.invalid", listen=listen)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@localhost/control",
        "https://localhost/other",
        "https://localhost/control?",
        "https://localhost/control#",
        "https://localhost/control?token=anything",
        "https://localhost:65536/control",
        "https://localhost:0/control",
        "https://localhost\\other/control",
        "https://localhost/../control",
        "wss://localhost/control",
        "HTTPS://localhost/control",
        "https://localhost /control",
    ],
)
def test_control_callback_url_refuses_ambiguous_or_unscoped_forms(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValidationError):
        runtime_settings(tmp_path, url, None)


def test_local_http_and_tls_verification_defaults_remain_supported(tmp_path: Path) -> None:
    local = runtime_settings(tmp_path, "http://host.openshell.internal:8202/control", None)
    assert local.control_ca_file is None
    assert settings(tmp_path, "http://unused.invalid").listen == "127.0.0.1:8202"
    with pytest.raises(ValidationError, match="HTTPS"):
        runtime_settings(tmp_path, local.control_url, tmp_path / "ca.pem")
    tls, ca = certificates(tmp_path)
    context = client_tls(ca)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert tls.context().protocol == ssl.PROTOCOL_TLS_SERVER


@pytest.mark.parametrize("kind", ["symlink", "public"])
def test_private_tls_keys_and_capabilities_remain_private_regular_files(
    tmp_path: Path, kind: str
) -> None:
    tls, _ = certificates(tmp_path)
    token = private(tmp_path / "runtime.token", TOKEN)
    if kind == "symlink":
        linked = tmp_path / "key-link.pem"
        linked.symlink_to(tls.private_key)
        tls = tls.model_copy(update={"private_key": linked})
        linked_token = tmp_path / "token-link"
        linked_token.symlink_to(token)
        token = linked_token
    else:
        tls.private_key.chmod(0o640)
        token.chmod(0o640)
    with pytest.raises(ValueError, match="private regular file"):
        tls.context()
    with pytest.raises(ValueError, match="private regular file"):
        private_token(token)


async def worker_ready(request: web.Request) -> web.Response:
    assert request.headers["Authorization"] == "Bearer " + WORKER
    assert request.headers[PROTOCOL_HEADER] == str(PROTOCOL_VERSION)
    return web.json_response(
        {"protocol": PROTOCOL_VERSION, "ready": True},
        headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION)},
    )


async def test_verified_https_runtime_and_service_probes_do_not_depend_on_each_other(
    tmp_path: Path,
) -> None:
    tls, ca = certificates(tmp_path)
    worker = web.Application()
    worker.router.add_get("/v1/ready", worker_ready)
    async with TestServer(worker) as backend:
        control = settings(tmp_path, str(backend.make_url("")), tls=tls)
        server = TestServer(create_app(control), scheme="https")
        await server.start_server(ssl=tls.context())
        try:
            url = str(server.make_url("/control").with_host("localhost"))
            connector = aiohttp.TCPConnector(ssl=client_tls(ca))
            async with aiohttp.ClientSession(connector=connector) as client:
                base = str(server.make_url("/").with_host("localhost"))
                async with client.get(base + "ready") as ready:
                    before = await ready.json()
                    assert ready.status == 200
                    assert before["service_ready"]
                    assert before["process_alive"]
                    assert not before["runtime_connected"]
                    assert not before["runtime_ready"]
                runtime = asyncio.create_task(serve(runtime_settings(tmp_path, url, ca)))
                try:

                    async def connected() -> bool:
                        async with client.get(base + "health") as health:
                            return (await health.json())["runtime_ready"]

                    await eventually(connected, timeout=5)
                    async with client.get(base + "live") as live:
                        assert live.status == 200
                        assert await live.json() == {"process_alive": True, "draining": False}
                    with pytest.raises(aiohttp.WSServerHandshakeError) as duplicate:
                        await client.ws_connect(
                            url,
                            headers={
                                "Authorization": "Bearer " + TOKEN,
                                CONTROL_HEADER: str(CONTROL_PROTOCOL),
                            },
                        )
                    assert duplicate.value.status == 409
                finally:
                    await server.close()
                    with pytest.raises(TransportError, match=r"control connection (closed|ended)"):
                        await asyncio.wait_for(runtime, 5)
        finally:
            await server.close()


@pytest.mark.parametrize("failure", ["untrusted_ca", "name_mismatch"])
async def test_runtime_rejects_untrusted_certificates_and_wrong_names(
    tmp_path: Path, failure: str
) -> None:
    tls, ca = certificates(tmp_path)
    app = web.Application()

    async def forbidden(request: web.Request) -> web.Response:
        pytest.fail("an unverified TLS peer received a capability")

    app.router.add_get("/control", forbidden)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=tls.context())
    private(tmp_path / "runtime.token", TOKEN)
    try:
        url = server.make_url("/control")
        if failure == "untrusted_ca":
            url = url.with_host("localhost")
            ca = None
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await serve(runtime_settings(tmp_path, str(url), ca))
    finally:
        await server.close()


async def test_tls_runtime_does_not_follow_authenticated_redirects(tmp_path: Path) -> None:
    tls, ca = certificates(tmp_path)
    calls: list[str] = []

    async def redirect(request: web.Request) -> web.Response:
        calls.append("control")
        raise web.HTTPFound("/other")

    async def other(request: web.Request) -> web.Response:
        calls.append("other")
        return web.Response()

    app = web.Application()
    app.router.add_get("/control", redirect)
    app.router.add_get("/other", other)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=tls.context())
    private(tmp_path / "runtime.token", TOKEN)
    try:
        url = str(server.make_url("/control").with_host("localhost"))
        with pytest.raises(TransportError, match="redirect refused"):
            await serve(runtime_settings(tmp_path, url, ca))
        assert calls == ["control"]
    finally:
        await server.close()


async def test_tls_shutdown_closes_socket_fences_admission_and_drains_original_effect(
    tmp_path: Path,
) -> None:
    tls, ca = certificates(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    committed: list[str] = []

    async def retained(request: web.Request) -> web.Response:
        entered.set()
        await release.wait()
        committed.append("original")
        return await worker_ready(request)

    worker = web.Application()
    worker.router.add_get("/v1/ready", retained)
    async with TestServer(worker) as backend:
        control = settings(tmp_path, str(backend.make_url("")), tls=tls)
        app = create_app(control)
        server = TestServer(app, scheme="https")
        await server.start_server(ssl=tls.context())
        connector = aiohttp.TCPConnector(ssl=client_tls(ca))
        async with aiohttp.ClientSession(connector=connector) as client:
            base = str(server.make_url("/").with_host("localhost"))
            headers = {"Authorization": "Bearer " + TOKEN, CONTROL_HEADER: str(CONTROL_PROTOCOL)}
            socket = await client.ws_connect(base + "control", headers=headers)
            shutdown: asyncio.Task[None] | None = None
            try:
                await socket.send_str(
                    Request(
                        id=1,
                        capability=Capability.HANDSHAKE,
                        payload={"image_revision": "a" * 40},
                    ).model_dump_json()
                )
                await asyncio.wait_for(entered.wait(), 3)
                shutdown = asyncio.create_task(app.shutdown())
                assert (await socket.receive(timeout=3)).type is aiohttp.WSMsgType.CLOSE
                async with client.get(base + "ready") as ready:
                    status = await ready.json()
                    assert ready.status == 503
                    assert not status["service_ready"]
                    assert status["draining"]
                    assert status["process_alive"]
                    assert status["runtime_draining"]
                    assert not status["runtime_connected"]
                with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                    await client.ws_connect(base + "control", headers=headers)
                assert refused.value.status == 503
                assert committed == []
                assert not shutdown.done()
                release.set()
                await asyncio.wait_for(shutdown, 5)
                assert committed == ["original"]
            finally:
                release.set()
                await socket.close()
                if shutdown is not None:
                    await shutdown
                await server.close()
