# SPDX-License-Identifier: MIT
"""find_artifacts (lab 38, B6): a run finds the artifact ID, revision and SHA-256 of a file
an earlier run published, by its path, its media type or as a result, to pin it as
ledger evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ArtifactRef, ExecutionContext
from sandbox_science.contracts.artifact_inventory import ArtifactLink
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.ledger.tool import SPECS as LEDGER_SPECS
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

pytestmark = pytest.mark.asyncio
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
FILES = {
    "results/order.json": (b'{"order": "8N"}', "application/json"),
    "shared/qdsa.pdf": (b"%PDF-1.7 paper", "application/pdf"),
}


class Worker(WorkerClient):
    """A worker that snapshots the files above."""

    def __init__(self) -> None:
        super().__init__("http://trusted-worker.invalid", "test-secret-find")

    async def publish_snapshot(self, request: Any) -> ArtifactRef:
        data, media = FILES[request.path]
        return ArtifactRef(
            artifact_id=f"art-{len(data)}",
            project_id=request.context.project_id,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            media_type=media,
            source_path=request.path,
            created_at=datetime.now(UTC),
        )

    async def verified_artifact_content(self, reference: Any, *, max_bytes: int = 0) -> bytes:
        return next(data for data, _ in FILES.values() if len(data) == reference.size)


async def call(toolbox: Toolbox, name: str, **arguments: Any) -> Any:
    outcome = await toolbox.call(ToolCall("c", name, json.dumps(arguments)), f"op-{name}")
    return json.loads(outcome.text)


async def test_a_later_run_finds_what_an_earlier_one_published(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    published = {}
    async with Worker() as client:
        first = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
        toolbox = Toolbox(client, CONTEXT, artifacts=first)
        for index, path in enumerate(FILES):
            result = await toolbox.call(
                ToolCall(f"c{index}", "publish", json.dumps({"path": path})), f"op-{index}"
            )
            published[path] = result.record["artifact_inventory"]["revision"]
        await first.aclose()

        # A later run: nothing of the first run's results is in its context.
        later = ArtifactPublisher(inventory, client, CONTEXT, run_id="run2")
        toolbox = Toolbox(client, CONTEXT, artifacts=later)
        assert "find_artifacts" in {spec.name for spec in toolbox.specs}
        (found,) = (await call(toolbox, "find_artifacts", path="results/order.json"))["artifacts"]
        pinned = published["results/order.json"]
        assert (found["artifact_id"], found["revision"], found["sha256"]) == (
            pinned["artifact_id"], pinned["revision"], pinned["sha256"],
        )  # fmt: skip
        assert (found["path"], found["result"], found["by"]) == (
            "sessions/s1/results/order.json", True, "agent_publication",
        )  # fmt: skip
        # The three values pin the revision, as ledger evidence does.
        link = ArtifactLink(
            artifact_id=found["artifact_id"], project_id="p1", mode="pinned",
            revision=found["revision"], sha256=found["sha256"],
        )  # fmt: skip
        assert inventory.resolve("p1", link).reference.size == found["size"]

        both = await call(toolbox, "find_artifacts", results_only=True)
        assert [e["path"] for e in both["artifacts"]] == [
            "shared/qdsa.pdf", "sessions/s1/results/order.json",
        ]  # fmt: skip
        assert both["next_before"] is None
        pdfs = await call(toolbox, "find_artifacts", media_type="application/pdf")
        assert [e["path"] for e in pdfs["artifacts"]] == ["shared/qdsa.pdf"]
        assert (await call(toolbox, "find_artifacts", media_type="image/"))["artifacts"] == []
        named = await call(toolbox, "find_artifacts", contains="order")
        assert [e["path"] for e in named["artifacts"]] == ["sessions/s1/results/order.json"]
        shared = await call(toolbox, "find_artifacts", path="shared/qdsa.pdf")
        assert shared["artifacts"][0]["media_type"] == "application/pdf"

        for wrong in ({"path": "../x"}, {"before": 0}, {"results_only": "yes"}, {"path": 3}):
            refused = await call(toolbox, "find_artifacts", **wrong)
            assert refused["error"]["kind"] == "invalid_request", wrong
        await later.aclose()
    documents.close()


async def test_offered_with_an_inventory_and_named_where_evidence_is_pinned() -> None:
    async with Worker() as client:
        assert "find_artifacts" not in {spec.name for spec in Toolbox(client, CONTEXT).specs}
    (change,) = [spec for spec in LEDGER_SPECS if spec.name == "ledger_change"]
    assert "find_artifacts" in json.dumps(change.parameters)
