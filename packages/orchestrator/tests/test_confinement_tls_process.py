# SPDX-License-Identifier: MIT
"""SIGTERM of the actual TLS control entrypoint retains its admitted callback."""

import asyncio
import os
import signal
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator.confinement.endpoints import client_tls
from sandbox_science.orchestrator.confinement.transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    Capability,
    Request,
)
from sandbox_science.testing import eventually

from .test_confinement_control import TOKEN
from .test_confinement_tls import certificates, settings, worker_ready

pytestmark = pytest.mark.slow


async def test_sigterm_of_control_process_drains_original_tls_callback(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    committed: list[str] = []

    async def retained(request: web.Request) -> web.Response:
        entered.set()
        await release.wait()
        committed.append("original")
        return await worker_ready(request)

    tls, ca = certificates(tmp_path)
    worker = web.Application()
    worker.router.add_get("/v1/ready", retained)
    async with TestServer(worker) as backend:
        configured = settings(
            tmp_path,
            str(backend.make_url("")),
            tls=tls,
            listen=f"127.0.0.1:{unused_tcp_port}",
        )
        path = tmp_path / "control.json"
        path.write_text(configured.model_dump_json())
        path.chmod(0o600)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sandbox_science.orchestrator.confinement.server",
            "--settings",
            str(path),
            cwd=tmp_path,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            connector = aiohttp.TCPConnector(ssl=client_tls(ca))
            async with aiohttp.ClientSession(connector=connector) as client:
                base = f"https://localhost:{unused_tcp_port}"

                async def ready() -> bool:
                    try:
                        async with client.get(base + "/ready") as response:
                            return response.status == 200
                    except aiohttp.ClientConnectionError:
                        return False

                await eventually(ready, timeout=10)
                async with client.ws_connect(
                    base + "/control",
                    headers={
                        "Authorization": "Bearer " + TOKEN,
                        CONTROL_HEADER: str(CONTROL_PROTOCOL),
                    },
                ) as socket:
                    await socket.send_str(
                        Request(
                            id=1,
                            capability=Capability.HANDSHAKE,
                            payload={"image_revision": "a" * 40},
                        ).model_dump_json()
                    )
                    await asyncio.wait_for(entered.wait(), 5)
                    process.send_signal(signal.SIGTERM)
                    assert (await socket.receive(timeout=5)).type is aiohttp.WSMsgType.CLOSE
                    assert process.returncode is None
                    assert committed == []
                    release.set()
                output, _ = await asyncio.wait_for(process.communicate(), 10)
                assert process.returncode == 0, output.decode()
                assert committed == ["original"]
        finally:
            release.set()
            if process.returncode is None:
                process.kill()
                await process.communicate()
