# SPDX-License-Identifier: MIT
"""Binary MCP results kept as artifacts by control (mcp_binaries): the bytes go to the
session's workspace and are published; the model gets a note naming the artifact."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from sandbox_science.contracts import ArtifactRef, ErrorKind, ExecutionContext, PublishRequest
from sandbox_science.contracts.worker import WriteFileRequest
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.mcp_binaries import ArtifactBinaries
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy.mcp_answers import Withheld

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
PNG = b"\x89PNG\r\n\x1a\n"


@dataclass
class FakeWorker:
    writes: list[WriteFileRequest] = field(default_factory=list[WriteFileRequest])
    published: list[PublishRequest] = field(default_factory=list[PublishRequest])
    fail: bool = False

    async def write_file(self, request: WriteFileRequest) -> object:
        if self.fail:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "down")
        self.writes.append(request)
        return None

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        self.published.append(request)
        return ArtifactRef(
            artifact_id="art1", project_id="p1", sha256="0" * 64, size=8,
            media_type="image/png", source_path=request.path,
            created_at=datetime(2026, 10, 24, tzinfo=UTC),
        )  # fmt: skip


def item(data: str, mime: str | None = "image/png") -> Withheld:
    return Withheld(0, "image", mime, data)


async def test_a_binary_item_is_written_published_and_named() -> None:
    worker = FakeWorker()
    sink = ArtifactBinaries(cast(WorkerClient, worker), CONTEXT)
    note = await sink.keep("crypto-research-mcp", "render", item(base64.b64encode(PNG).decode()))
    digest = hashlib.sha256(PNG).hexdigest()
    (write,) = worker.writes
    assert write.path == f"mcp-results/crypto-research-mcp/{digest[:24]}.png"
    assert base64.b64decode(write.content_b64) == PNG
    (published,) = worker.published
    assert (published.path, published.media_type) == (write.path, "image/png")
    assert "artifact art1" in note
    assert write.path in note
    assert base64.b64encode(PNG).decode() not in note


async def test_the_runs_publisher_keeps_it_in_the_inventory() -> None:
    worker = FakeWorker()
    calls: list[tuple[PublishRequest, str, str]] = []

    async def publish(request: PublishRequest, operation: str, *, tool_call: str) -> Any:
        calls.append((request, operation, tool_call))
        return SimpleNamespace(worker=SimpleNamespace(artifact_id="art2"))

    publisher = cast(ArtifactPublisher, SimpleNamespace(run_id="main-1", publish=publish))
    sink = ArtifactBinaries(cast(WorkerClient, worker), CONTEXT, publisher)
    note = await sink.keep("srv", "render", item(base64.b64encode(PNG).decode()))
    again = await sink.keep("srv", "render", item(base64.b64encode(PNG).decode()))
    assert "artifact art2" in note
    assert note == again
    (_, operation, tool_call), (_, second, _) = calls
    assert operation == second  # the same bytes in one run are one publication
    assert operation.startswith("mcp-")
    assert tool_call == "mcp:render"
    assert worker.published == []  # the publisher, not the bare worker call


async def test_what_cannot_be_kept_is_withheld_and_said() -> None:
    down = FakeWorker(fail=True)
    sink = ArtifactBinaries(cast(WorkerClient, down), CONTEXT)
    note = await sink.keep("srv", "render", item(base64.b64encode(PNG).decode()))
    assert note.startswith("[image/png withheld:")
    assert "could not be kept" in note
    bad = await ArtifactBinaries(cast(WorkerClient, FakeWorker()), CONTEXT).keep(
        "srv", "render", item("not base64!", mime=None)
    )
    assert bad.startswith("[binary data withheld:")
    assert "not valid base64" in bad
