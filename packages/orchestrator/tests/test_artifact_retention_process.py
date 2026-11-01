# SPDX-License-Identifier: MIT
"""Real worker restarts and offline release retain original control-owned content."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import pytest

from sandbox_science.contracts import ErrorKind, PublishRequest, ReadFileRequest, WriteFileRequest
from sandbox_science.contracts.artifact_retention import ArtifactRetentionAcknowledgement
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifact_retention import RetentionOutbox
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import eventually

from .test_artifact_retention import CONTEXT

pytestmark = pytest.mark.slow
TOKEN = "test-secret-retention-process-0000000000000000"
WORKER = str(Path(sys.executable).parent / "sandbox-science-worker")


@asynccontextmanager
async def worker(settings: Path, port: int) -> AsyncIterator[WorkerClient]:
    with (settings.parent / "worker.log").open("ab") as log:
        process = await asyncio.create_subprocess_exec(
            WORKER, "serve", "--settings", str(settings), stdout=log, stderr=log
        )
        try:
            async with WorkerClient(f"http://127.0.0.1:{port}", TOKEN) as client:

                async def ready() -> bool:
                    assert process.returncode is None, "disposable worker exited during startup"
                    try:
                        await client.readiness()
                        return True
                    except WorkerAPIError:
                        return False

                await eventually(ready, timeout=20)
                yield client
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    pytest.fail("disposable worker did not drain within 15 seconds")


async def release(settings: Path, *, apply: bool = False) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        WORKER,
        "artifact-retention",
        "--settings",
        str(settings),
        *(["--apply"] if apply else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode is not None
        return process.returncode, stdout.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_worker_release_restart_and_original_receipt_replay(
    tmp_path: Path, unused_tcp_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "worker-state"
    token = tmp_path / "worker.token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    settings = tmp_path / "worker.toml"
    settings.write_text(
        f'listen = "127.0.0.1:{unused_tcp_port}"\n'
        f"token_file = {json.dumps(str(token))}\nstate_dir = {json.dumps(str(state))}\n"
        f"[local]\npython = {json.dumps(sys.executable)}\n"
    )
    data = b"original result\x00\xff" * 16384
    changed = b"source changed after its immutable capture"
    database = tmp_path / "control.sqlite3"
    with closing(DocumentStore(database)) as documents:
        inventory = ArtifactInventory(documents)
        async with worker(settings, unused_tcp_port) as client:
            await client.write_file(
                WriteFileRequest(
                    context=CONTEXT, path="out.bin", content_b64=base64.b64encode(data).decode()
                )
            )
            original = client.acknowledge_artifact_retention

            async def lose_reply(ack: ArtifactRetentionAcknowledgement, *, timeout: float = 5):
                await original(ack, timeout=timeout)
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "synthetic lost ACK reply")

            monkeypatch.setattr(client, "acknowledge_artifact_retention", lose_reply)
            publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
            try:
                first = await publisher.publish(
                    PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1"
                )
            finally:
                await publisher.aclose()
            assert first.receipt.state == "recorded"
            reference = first.receipt.revision
            snapshot = first.receipt.worker_snapshot
            assert reference is not None
            assert snapshot is not None
            endpoint = publisher.source("call1").worker_endpoint_sha256
            assert len(RetentionOutbox(documents.control).pending("p1", endpoint)) == 1
            blob = state / "artifacts" / "blobs" / snapshot.sha256[:2] / snapshot.sha256
            assert blob.is_file()
            code, output = await release(settings, apply=True)
            assert code == 1
            assert "another worker" in output
            assert blob.is_file()
            await client.write_file(
                WriteFileRequest(
                    context=CONTEXT, path="out.bin", content_b64=base64.b64encode(changed).decode()
                )
            )
        code, output = await release(settings)
        assert code == 0
        assert "would_release" in output
        assert blob.is_file()
        code, output = await release(settings, apply=True)
        assert code == 0
        assert "released" in output
        assert not blob.exists()
        assert inventory.content("p1", reference) == data
        async with worker(settings, unused_tcp_port) as client:
            publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
            try:
                assert (await publisher.drain_retention()).delivered == 1
                replay = await publisher.publish(
                    PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1"
                )
                assert replay.receipt == first.receipt
                assert inventory.content("p1", reference) == data
                current = await client.read_file(ReadFileRequest(context=CONTEXT, path="out.bin"))
                assert base64.b64decode(current.content_b64) == changed
                # Reusing the old receipt must not create a second mutable capture.
                with sqlite3.connect(state / "worker.sqlite3") as db:
                    assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
                    assert (
                        db.execute(
                            "SELECT count(*) FROM artifact_release_intents "
                            "WHERE settled_at IS NOT NULL"
                        ).fetchone()[0]
                        == 1
                    )
                assert not blob.exists()
            finally:
                await publisher.aclose()
