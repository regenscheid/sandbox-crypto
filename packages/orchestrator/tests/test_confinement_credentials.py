# SPDX-License-Identifier: MIT
"""Control-issued runtime credentials and the runtime manager's listener (KRB-6, KRB-7)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import BaseTestServer, TestServer
from pydantic import ValidationError

from sandbox_science.orchestrator.confinement.credentials import (
    JOURNAL,
    CredentialsError,
    Issued,
    RuntimeCredentials,
)
from sandbox_science.orchestrator.confinement.server import (
    ControlSettings,
    create_app,
    private_token,
)
from sandbox_science.orchestrator.confinement.transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    Capability,
    Reply,
    Request,
)
from sandbox_science.testing import eventually

from .test_confinement_control import TOKEN, private
from .test_confinement_tls import certificates, settings, worker_ready

MANAGER = "test-secret-manager-capability-0123456789abcdef"
FIRST, SECOND = "a" * 32, "b" * 32


def credentials(
    directory: Path, entropy: Callable[[int], bytes] = secrets.token_bytes
) -> RuntimeCredentials:
    """A started instance; a second call on the same directory is a control restart."""
    issuer = RuntimeCredentials(
        directory / "state" / JOURNAL,
        directory / "runtime.token",
        directory / "state" / "generation",
        placeholder="runtime-test",
        entropy=entropy,
    )
    issuer.start()
    return issuer


def files(directory: Path) -> Issued:
    return Issued(
        generation=(directory / "state" / "generation").read_text().strip(),
        token=private_token(directory / "runtime.token"),
    )


def test_latest_request_replays_the_same_values(tmp_path: Path) -> None:
    issuer = credentials(tmp_path)
    issued = issuer.issue(FIRST, occupied=False)
    assert issued.generation.startswith("runtime-")
    assert len(issued.generation) == len("runtime-") + 24
    assert files(tmp_path) == issued
    # A replay while the runtime is connected still gets the same values.
    assert issuer.issue(FIRST, occupied=True) == issued
    journal = (tmp_path / "state" / JOURNAL).read_text()
    assert issued.token not in journal
    assert hashlib.sha256(issued.token.encode()).hexdigest() in journal
    assert (tmp_path / "state" / JOURNAL).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state" / "generation").stat().st_mode & 0o777 == 0o600


def test_older_and_superseded_requests_get_no_token(tmp_path: Path) -> None:
    issuer = credentials(tmp_path)
    first = issuer.issue(FIRST, occupied=False)
    second = issuer.issue(SECOND, occupied=False)
    assert second.generation != first.generation
    assert second.token != first.token
    with pytest.raises(CredentialsError) as older:
        issuer.issue(FIRST, occupied=False)
    assert older.value.kind == "superseded"
    assert files(tmp_path) == second
    restarted = credentials(tmp_path)
    with pytest.raises(CredentialsError) as superseded:
        restarted.issue(SECOND, occupied=False)
    assert superseded.value.kind == "superseded"
    with pytest.raises(CredentialsError):
        restarted.issue(FIRST, occupied=False)


def test_start_revokes_the_issued_token(tmp_path: Path) -> None:
    issued = credentials(tmp_path).issue(FIRST, occupied=False)
    credentials(tmp_path)
    assert private_token(tmp_path / "runtime.token") != issued.token
    states = [
        issue["state"] for issue in json.loads((tmp_path / "state" / JOURNAL).read_text())["issues"]
    ]
    assert states == ["superseded"]


def test_a_new_request_revokes_before_refusing_an_occupied_connection(tmp_path: Path) -> None:
    issuer = credentials(tmp_path)
    first = issuer.issue(FIRST, occupied=False)
    with pytest.raises(CredentialsError) as occupied:
        issuer.issue(SECOND, occupied=True)
    assert occupied.value.kind == "occupied"
    assert private_token(tmp_path / "runtime.token") != first.token
    with pytest.raises(CredentialsError):
        issuer.issue(FIRST, occupied=False)
    # The manager retries the same request once the runtime has drained.
    second = issuer.issue(SECOND, occupied=False)
    assert second.generation != first.generation
    assert issuer.issue(SECOND, occupied=False) == second


def test_crash_between_token_and_journal_yields_fresh_values(tmp_path: Path) -> None:
    issuer = credentials(tmp_path)
    with pytest.raises(CredentialsError):
        issuer.issue(FIRST, occupied=True)
    # Step 4 wrote generation and token, then the process died before the journal.
    stray = Issued(generation="runtime-" + "c" * 24, token="d" * 64)
    private(tmp_path / "state" / "generation", stray.generation)
    private(tmp_path / "runtime.token", stray.token)
    restarted = credentials(tmp_path)
    assert private_token(tmp_path / "runtime.token") != stray.token
    retried = restarted.issue(FIRST, occupied=False)
    assert retried.token != stray.token
    assert retried.generation != stray.generation
    assert files(tmp_path) == retried


def test_no_generation_is_ever_reissued(tmp_path: Path) -> None:
    generations = [b"\x01" * 12, b"\x01" * 12, b"\x02" * 12, b"\x02" * 12, b"\x03" * 12]

    def entropy(size: int) -> bytes:
        return generations.pop(0) if size == 12 else secrets.token_bytes(size)

    issuer = credentials(tmp_path, entropy)
    assert issuer.issue(FIRST, occupied=False).generation == "runtime-" + "01" * 12
    assert issuer.issue(SECOND, occupied=False).generation == "runtime-" + "02" * 12
    assert credentials(tmp_path, entropy).issue("e" * 32, occupied=False).generation == (
        "runtime-" + "03" * 12
    )
    assert generations == []


def test_the_configured_placeholder_generation_is_never_issued(tmp_path: Path) -> None:
    generations = [b"\x00" * 12, b"\x04" * 12]

    def entropy(size: int) -> bytes:
        return generations.pop(0) if size == 12 else secrets.token_bytes(size)

    issuer = RuntimeCredentials(
        tmp_path / "state" / JOURNAL,
        tmp_path / "runtime.token",
        tmp_path / "state" / "generation",
        placeholder="runtime-" + "00" * 12,
        entropy=entropy,
    )
    issuer.start()
    assert issuer.issue(FIRST, occupied=False).generation == "runtime-" + "04" * 12


def configured(directory: Path, worker: str, port: int, **fields: object) -> ControlSettings:
    digest = hashlib.sha256(MANAGER.encode()).hexdigest()
    return settings(
        directory,
        worker,
        **{
            "manager_listen": f"127.0.0.1:{port}",
            "manager_token_sha256_file": private(directory / "manager.sha256", digest),
            "generation_file": directory / "state" / "generation",
            **fields,
        },
    )


@asynccontextmanager
async def control(
    directory: Path, port: int, worker: web.Application | None = None
) -> AsyncIterator[tuple[BaseTestServer, aiohttp.ClientSession, str]]:
    backend_app = worker or web.Application()
    if worker is None:
        backend_app.router.add_get("/v1/ready", worker_ready)
    async with TestServer(backend_app) as backend:
        app = create_app(configured(directory, str(backend.make_url("")), port))
        async with TestServer(app) as server, aiohttp.ClientSession() as client:
            yield server, client, f"http://127.0.0.1:{port}"


async def ask(
    client: aiohttp.ClientSession,
    manager: str,
    body: bytes | str,
    token: str | None = MANAGER,
) -> tuple[int, dict[str, object] | None]:
    headers = {"Authorization": "Bearer " + token} if token is not None else {}
    data = body.encode() if isinstance(body, str) else body
    async with client.post(manager + "/runtime/credentials", data=data, headers=headers) as reply:
        parsed = await reply.json() if reply.content_type == "application/json" else None
        return reply.status, parsed


def request(identity: str) -> str:
    return json.dumps({"request": identity})


@pytest.mark.parametrize(
    "header",
    [None, "Bearer test-secret-wrong-manager-token-0123456789", "Basic " + MANAGER, MANAGER],
)
async def test_manager_token_is_checked_by_digest_and_failure_answers_404(
    tmp_path: Path, unused_tcp_port: int, header: str | None
) -> None:
    async with control(tmp_path, unused_tcp_port) as (_, client, manager):
        headers = {"Authorization": header} if header is not None else {}
        async with client.post(
            manager + "/runtime/credentials", data=request(FIRST), headers=headers
        ) as refused:
            assert refused.status == 404
        status, issued = await ask(client, manager, request(FIRST))
        assert status == 200
        assert issued is not None
        assert set(issued) == {"generation", "token"}


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        '{"request": "' + "A" * 32 + '"}',
        '{"request": "' + "a" * 31 + '"}',
        '{"request": 1}',
        '{"request": "' + "a" * 32 + '", "generation": "runtime-chosen"}',
        "x" * 4096,
    ],
)
async def test_credentials_requests_are_strict_and_bounded(
    tmp_path: Path, unused_tcp_port: int, body: str
) -> None:
    async with control(tmp_path, unused_tcp_port) as (_, client, manager):
        status, refused = await ask(client, manager, body)
        assert status == (413 if len(body) > 1024 else 400)
        assert refused is not None
        assert refused["error"] == "invalid_request"
        assert not (tmp_path / "state" / "generation").exists()


async def test_listeners_are_separate_and_health_names_the_control_instance(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    async with control(tmp_path, unused_tcp_port) as (server, client, manager):
        runtime = str(server.make_url("")).rstrip("/")
        async with client.post(
            runtime + "/runtime/credentials",
            data=request(FIRST),
            headers={"Authorization": "Bearer " + MANAGER},
        ) as missing:
            assert missing.status in (404, 405)
        for route in ("/control", "/ready", "/live"):
            async with client.get(manager + route) as absent:
                assert absent.status == 404
        async with client.get(manager + "/health") as health:
            status = await health.json()
        async with client.get(runtime + "/health") as same:
            assert (await same.json())["control_instance"] == status["control_instance"]
        assert len(status["control_instance"]) == 32
        assert not status["runtime_connected"]
        assert not status["runtime_draining"]
    async with (
        control(tmp_path, unused_tcp_port) as (_, client, manager),
        client.get(manager + "/health") as restarted,
    ):
        assert (await restarted.json())["control_instance"] != status["control_instance"]


async def test_control_start_revokes_the_runtime_token(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    async with control(tmp_path, unused_tcp_port) as (server, client, _):
        assert private_token(tmp_path / "runtime.token") != TOKEN
        with pytest.raises(aiohttp.WSServerHandshakeError) as revoked:
            await client.ws_connect(
                server.make_url("/control"),
                headers={"Authorization": "Bearer " + TOKEN, CONTROL_HEADER: str(CONTROL_PROTOCOL)},
            )
        assert revoked.value.status == 404


async def test_new_request_revokes_then_answers_409_while_connected_or_draining(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def retained(request: web.Request) -> web.Response:
        entered.set()
        await release.wait()
        return await worker_ready(request)

    worker = web.Application()
    worker.router.add_get("/v1/ready", retained)
    async with control(tmp_path, unused_tcp_port, worker) as (server, client, manager):
        status, first = await ask(client, manager, request(FIRST))
        assert status == 200
        assert first is not None
        headers = {
            "Authorization": f"Bearer {first['token']}",
            CONTROL_HEADER: str(CONTROL_PROTOCOL),
        }
        socket = await client.ws_connect(server.make_url("/control"), headers=headers)
        try:
            await socket.send_str(
                Request(
                    id=1, capability=Capability.HANDSHAKE, payload={"image_revision": "a" * 40}
                ).model_dump_json()
            )
            await asyncio.wait_for(entered.wait(), 5)
            status, refused = await ask(client, manager, request(SECOND))
            assert (status, refused) == (
                409,
                {"error": "occupied", "message": "a runtime is connected or draining"},
            )
            assert private_token(tmp_path / "runtime.token") != first["token"]
            # The connected runtime is refused at its next frame.
            await socket.send_str(
                Request(id=2, capability=Capability.QUEUE, payload=None).model_dump_json()
            )
            denied = Reply.model_validate_json((await socket.receive(timeout=5)).data)
            assert (denied.id, denied.error) == (2, "denied")
            await socket.close()

            async def draining() -> bool:
                async with client.get(manager + "/health") as health:
                    return (await health.json())["runtime_draining"]

            await eventually(draining, timeout=5)
            status, _ = await ask(client, manager, request(SECOND))
            assert status == 409
        finally:
            release.set()
            await socket.close()

        async def idle() -> bool:
            async with client.get(manager + "/health") as health:
                current = await health.json()
                return not current["runtime_connected"] and not current["runtime_draining"]

        await eventually(idle, timeout=5)
        status, second = await ask(client, manager, request(SECOND))
        assert status == 200
        assert second is not None
        assert second["generation"] != first["generation"]
        status, older = await ask(client, manager, request(FIRST))
        assert (status, older) == (
            410,
            {"error": "superseded", "message": "the request was superseded"},
        )


async def test_issued_generation_reaches_the_runtime_handshake(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    async with control(tmp_path, unused_tcp_port) as (server, client, manager):
        status, issued = await ask(client, manager, request(FIRST))
        assert status == 200
        assert issued is not None
        headers = {
            "Authorization": f"Bearer {issued['token']}",
            CONTROL_HEADER: str(CONTROL_PROTOCOL),
        }
        async with client.ws_connect(server.make_url("/control"), headers=headers) as socket:
            await socket.send_str(
                Request(
                    id=1, capability=Capability.HANDSHAKE, payload={"image_revision": "a" * 40}
                ).model_dump_json()
            )
            ready = Reply.model_validate_json((await socket.receive(timeout=5)).data)
            assert ready.error is None
            assert isinstance(ready.payload, dict)
            assert ready.payload["generation"] == issued["generation"]


def test_manager_settings_are_off_unless_configured(tmp_path: Path) -> None:
    host = settings(tmp_path, "http://unused.invalid")
    assert host.manager_token_sha256_file is None
    # A serialized host configuration names the default listener but enables nothing.
    assert ControlSettings.model_validate_json(host.model_dump_json()) == host
    with pytest.raises(ValidationError, match="require generation_file"):
        settings(
            tmp_path,
            "http://unused.invalid",
            manager_token_sha256_file=private(tmp_path / "manager.sha256", "0" * 64),
        )
    with pytest.raises(ValidationError, match="non-loopback manager bind requires TLS"):
        configured(tmp_path, "http://unused.invalid", 8444, manager_listen="0.0.0.0:8444")
    with pytest.raises(ValidationError, match="port of its own"):
        configured(tmp_path, "http://unused.invalid", 8202)
    tls, _ = certificates(tmp_path)
    remote = configured(
        tmp_path,
        "http://unused.invalid",
        8444,
        manager_listen="0.0.0.0:8444",
        listen="0.0.0.0:8443",
        tls=tls,
    )
    assert remote.manager_listen == "0.0.0.0:8444"


async def test_host_control_without_manager_settings_is_unchanged(tmp_path: Path) -> None:
    worker = web.Application()
    worker.router.add_get("/v1/ready", worker_ready)
    async with TestServer(worker) as backend:
        app = create_app(settings(tmp_path, str(backend.make_url(""))))
        async with TestServer(app) as server, aiohttp.ClientSession() as client:
            async with client.get(server.make_url("/health")) as health:
                assert "control_instance" not in await health.json()
            async with client.post(server.make_url("/runtime/credentials")) as missing:
                assert missing.status in (404, 405)
    assert private_token(tmp_path / "runtime.token") == TOKEN
    assert not (tmp_path / "state" / JOURNAL).exists()


async def test_unreadable_manager_digest_prevents_startup(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    configured_settings = configured(tmp_path, "http://unused.invalid", unused_tcp_port)
    private(tmp_path / "manager.sha256", "not-a-digest")
    with pytest.raises(ValueError, match="invalid shape"):
        async with TestServer(create_app(configured_settings)):
            pass
