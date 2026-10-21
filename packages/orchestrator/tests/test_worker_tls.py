# SPDX-License-Identifier: MIT
"""A real worker HTTPS entrypoint retains scoped bearer and hostname boundaries."""

from __future__ import annotations

import asyncio
import signal
import socket
import sys

import aiohttp
import pytest

from sandbox_science.orchestrator.transport_tls import client_tls
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import eventually
from sandbox_science.worker.settings import load_settings
from sandbox_science.worker.tls import ServerTLS

from .test_confinement_tls import certificates

pytestmark = pytest.mark.slow
TOKEN = "test-secret-worker-tls-scoped-token-000000"


@pytest.fixture
async def server(tmp_path):
    tls, certificate = certificates(tmp_path)
    token = tmp_path / "worker.token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "worker.toml"
    path.write_text(
        f'listen = "127.0.0.1:{port}"\ntoken_file = "worker.token"\nstate_dir = "state"\n'
        '[tls]\ncertificate = "server.pem"\nprivate_key = "server-key.pem"\n[local]\n'
    )
    settings = load_settings(path)
    assert settings.tls == ServerTLS(certificate=certificate, private_key=tls.private_key)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from sandbox_science.worker.cli import main; raise SystemExit(main())",
        "serve",
        "--settings",
        str(path),
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    url = f"https://localhost:{port}"
    try:
        async with WorkerClient(url, TOKEN, ca_file=certificate) as client:

            async def ready():
                if process.returncode is not None:
                    pytest.fail((await process.communicate())[0].decode())
                try:
                    return (await client.readiness()).ready
                except WorkerAPIError:
                    return False

            await eventually(ready, timeout=10)
        yield url, certificate
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, output.decode()


async def test_actual_worker_https_accepts_custom_ca_without_relaxing_auth(server):
    url, certificate = server
    async with WorkerClient(url, TOKEN, ca_file=certificate) as client:
        assert (await client.readiness()).ready
    async with WorkerClient(url, "test-secret-wrong-token-000000", ca_file=certificate) as client:
        with pytest.raises(WorkerAPIError):
            await client.readiness()


async def test_actual_worker_https_refuses_unknown_ca_and_hostname_mismatch(server):
    url, certificate = server
    async with WorkerClient(url, TOKEN) as client:
        with pytest.raises((WorkerAPIError, aiohttp.ClientError)):
            await client.readiness()
    async with WorkerClient(
        url.replace("localhost", "127.0.0.1"), TOKEN, ca_file=certificate
    ) as client:
        with pytest.raises((WorkerAPIError, aiohttp.ClientError)):
            await client.readiness()


async def test_ca_cannot_silently_change_plaintext_or_a_borrowed_session(tmp_path):
    _, certificate = certificates(tmp_path)
    with pytest.raises(ValueError, match="requires HTTPS"):
        WorkerClient("http://localhost:8100", TOKEN, ca_file=certificate)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="owned"):
            WorkerClient("https://localhost:8100", TOKEN, ca_file=certificate, session=session)


def test_context_adds_ca_while_preserving_hostname_verification(tmp_path):
    _, certificate = certificates(tmp_path)
    context = client_tls(certificate)
    assert context.check_hostname
    assert context.verify_mode.name == "CERT_REQUIRED"


@pytest.mark.parametrize("kind", ["nonprivate", "symlink"])
def test_worker_rejects_nonprivate_projected_server_keys(tmp_path, kind):
    identity, certificate = certificates(tmp_path)
    key = identity.private_key
    if kind == "nonprivate":
        key.chmod(0o644)
    else:
        projected = tmp_path / "projected.pem"
        projected.symlink_to(key)
        key = projected
    with pytest.raises(ValueError, match="private regular"):
        ServerTLS(certificate=certificate, private_key=key).context()
