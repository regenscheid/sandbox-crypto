# SPDX-License-Identifier: MIT
"""Authenticated production control boundary, independent of the OpenShell transport."""

from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator.confinement.server import ControlSettings, create_app
from sandbox_science.orchestrator.confinement.transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    Capability,
    Reply,
    Request,
)

TOKEN = "test-secret-runtime-capability-0123456789abcdef"
WORKER = "test-secret-worker-capability-0123456789abcdef"


def private(path: Path, value: str) -> Path:
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


async def test_control_checks_auth_protocol_readiness_scope_and_rotation(tmp_path: Path) -> None:
    seen: list[str] = []

    async def ready(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer " + WORKER
        assert request.headers[PROTOCOL_HEADER] == str(PROTOCOL_VERSION)
        seen.append("ready")
        return web.json_response(
            {"protocol": PROTOCOL_VERSION, "ready": True},
            headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION)},
        )

    worker = web.Application()
    worker.router.add_get("/v1/ready", ready)
    async with TestServer(worker) as backend:
        token = private(tmp_path / "runtime.token", TOKEN)
        settings = ControlSettings(
            state_dir=tmp_path / "state",
            listen="127.0.0.1:8202",
            worker=str(backend.make_url("")),
            worker_token_file=private(tmp_path / "worker.token", WORKER),
            runtime_token_file=token,
            generation="runtime-test",
            projects=("p",),
            main="main",
            image_revision="a" * 40,
        )
        async with TestClient(TestServer(create_app(settings))) as client:
            headers = {
                "Authorization": "Bearer " + TOKEN,
                CONTROL_HEADER: str(CONTROL_PROTOCOL),
            }
            with pytest.raises(aiohttp.WSServerHandshakeError) as wrong:
                await client.ws_connect("/control")
            assert wrong.value.status == 404
            with pytest.raises(aiohttp.WSServerHandshakeError) as incompatible:
                await client.ws_connect("/control", headers={**headers, CONTROL_HEADER: "999"})
            assert incompatible.value.status == 422
            async with client.ws_connect("/control", headers=headers) as socket:

                async def ask(identity: int, kind: Capability, value: object) -> Reply:
                    request = Request.model_validate(
                        {"id": identity, "capability": kind, "payload": value}
                    )
                    await socket.send_str(request.model_dump_json())
                    response = await socket.receive(timeout=5)
                    return Reply.model_validate_json(response.data)

                denied = await ask(1, Capability.QUEUE, None)
                assert denied.error == "denied"
                mismatch = await ask(2, Capability.HANDSHAKE, {"image_revision": "b" * 40})
                assert mismatch.error == "conflict"
                assert seen == []
                hello = await ask(3, Capability.HANDSHAKE, {"image_revision": "a" * 40})
                assert hello.error is None
                assert seen == ["ready"]
                with pytest.raises(aiohttp.WSServerHandshakeError) as duplicate:
                    await client.ws_connect("/control", headers=headers)
                assert duplicate.value.status == 409
                outside = await ask(4, Capability.ADMIT, {"project": "other", "session": "s"})
                assert outside.error == "denied"
                token.write_text("test-secret-replaced-capability-0123456789abcdef\n")
                revoked = await ask(5, Capability.QUEUE, None)
                assert revoked.error == "denied"
