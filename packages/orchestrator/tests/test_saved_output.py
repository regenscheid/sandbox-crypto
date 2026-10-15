# SPDX-License-Identifier: MIT
"""Output too long for a call's result is saved whole to a file in the session workspace,
recorded like any file a call writes, and the model is told that what it sees is a sample
(tools spec Shell 5, as amended on 2026-10-15; lane K's triage of the owner's prompt
research, P4), through a real worker's HTTP API."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    ErrorKind,
    ListFilesRequest,
    ReadFileRequest,
    WriteFileRequest,
    WriteFileResult,
)
from sandbox_science.contracts.artifact_inventory import ArtifactRevision
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.file_settings import ProjectFiles
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.recording import FileRecorder
from sandbox_science.orchestrator.tools import LONG_OUTPUT, SAGE, SPECS, Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .test_artifact_publication import CONTEXT, TOKEN
from .test_artifact_publication import worker_url as worker_url

LINES = 50_000
NUMBERED = f"seq 1 {LINES}"


class RefusingClient(WorkerClient):
    """A worker client whose file writes fail, as a full workspace's would."""

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        raise WorkerAPIError(ErrorKind.POLICY_DENIED, "the workspace is full")


@dataclass
class Session:
    client: WorkerClient
    toolbox: Toolbox
    inventory: ArtifactInventory
    calls: int = 0

    async def call(self, name: str, **arguments: Any) -> tuple[ToolOutcome, dict[str, Any]]:
        self.calls += 1
        call = ToolCall(id=f"call{self.calls}", name=name, arguments=json.dumps(arguments))
        outcome = await self.toolbox.call(call, f"op{self.calls}", model="gateway-model")
        return outcome, json.loads(outcome.text)

    async def read(self, path: str) -> str:
        content = await self.client.read_file(ReadFileRequest(context=CONTEXT, path=path))
        return base64.b64decode(content.content_b64).decode()

    def heads(self) -> dict[str, ArtifactRevision]:
        page = self.inventory.list("p1")
        return {entry.path: entry for entry in page.entries}


async def _session(
    client: WorkerClient, tmp_path: Path
) -> tuple[Session, ArtifactPublisher, DocumentStore]:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1", alias="research")
    recorder = FileRecorder(publisher, ProjectFiles(documents))
    toolbox = Toolbox(client, CONTEXT, artifacts=publisher, recorder=recorder)
    return Session(client, toolbox, inventory), publisher, documents


@pytest_asyncio.fixture
async def session(worker_url: str, tmp_path: Path) -> AsyncIterator[Session]:
    async with WorkerClient(worker_url, TOKEN) as client:
        found, publisher, documents = await _session(client, tmp_path)
        yield found
        await publisher.aclose()
    documents.close()


def test_the_tools_that_run_code_say_long_output_is_saved() -> None:
    specs = {spec.name: spec for spec in (*SPECS, SAGE)}
    for name in ("shell", "python", "sage"):
        assert LONG_OUTPUT in specs[name].description


@pytest.mark.asyncio
async def test_output_that_fits_is_shown_whole_and_saved_nowhere(session: Session) -> None:
    _, result = await session.call("shell", script="seq 1 5")
    assert result["stdout"] == "1\n2\n3\n4\n5\n"
    assert "sample" not in result
    assert "saved" not in result
    listing = await session.client.list_files(ListFilesRequest(context=CONTEXT))
    assert "call-output" not in {entry.name for entry in listing.entries}


@pytest.mark.asyncio
async def test_long_output_is_a_sample_saved_whole_and_recorded(session: Session) -> None:
    outcome, result = await session.call("shell", script=NUMBERED)
    execution = outcome.record["execution"]["execution_id"]
    path = f"call-output/{execution}/stdout.txt"
    assert result["saved"] == {"stdout": path}
    assert outcome.record["saved_output"] == {"stdout": path}
    assert "characters omitted" in result["stdout"]
    assert result["stdout"].startswith("1\n2\n")
    assert result["stdout"].endswith(f"{LINES - 1}\n{LINES}\n")
    assert "Shown here only in part" in result["sample"]
    assert "stdout" in result["sample"]
    assert "take no counts" in result["sample"]
    assert len(outcome.text) <= session.toolbox.output_chars
    whole = await session.read(path)
    assert whole.splitlines() == [str(n) for n in range(1, LINES + 1)]
    # Recorded like any file a call writes: by the call that printed it.
    report = outcome.record["recorded_files"]
    assert [entry["path"] for entry in report["files"]] == [path]
    revision = session.heads()[f"sessions/s1/{path}"]
    assert session.inventory.content("p1", revision.reference) == whole.encode()
    assert revision.publication.execution_id == execution


@pytest.mark.asyncio
async def test_a_cells_long_print_and_value_are_saved(session: Session) -> None:
    code = f"for n in range(1, {LINES + 1}):\n    print(n)\n'ab' * 15000 + 'end'"
    outcome, result = await session.call("python", code=code)
    execution = outcome.record["execution"]["execution_id"]
    assert result["saved"] == {
        "stdout": f"call-output/{execution}/stdout.txt",
        "output-1": f"call-output/{execution}/output-1.txt",
    }
    assert "stdout, output-1" in result["sample"]
    printed = await session.read(result["saved"]["stdout"])
    assert len(printed.splitlines()) == LINES
    value = await session.read(result["saved"]["output-1"])
    (shown,) = outcome.record["execution"]["result"]["outputs"]
    assert value == shown["data"]["text/plain"]
    assert "end" in value[-10:]
    assert len(value) > 30000


@pytest.mark.asyncio
async def test_output_the_worker_cut_is_saved_as_the_sample_it_is(session: Session) -> None:
    outcome, result = await session.call("shell", script="yes b | head -c 2000000")
    assert result["stdout_total_bytes"] == 2_000_000
    assert "The worker kept only the first and last parts of stdout" in result["sample"]
    assert "write it to a file in the script" in result["sample"]
    kept = await session.read(result["saved"]["stdout"])
    assert "bytes omitted" in kept
    assert len(kept) < 2_000_000
    assert outcome.record["saved_output"] == result["saved"]


@pytest.mark.asyncio
async def test_output_that_cannot_be_saved_is_still_a_sample(
    worker_url: str, tmp_path: Path
) -> None:
    async with RefusingClient(worker_url, TOKEN) as client:
        found, publisher, documents = await _session(client, tmp_path)
        outcome, result = await found.call("shell", script=NUMBERED)
        await publisher.aclose()
    documents.close()
    assert result["state"] == "succeeded"
    assert "saved" not in result
    assert "Shown here only in part" in result["sample"]
    assert "stdout could not be saved (policy_denied: the workspace is full)" in result["sample"]
    assert outcome.record["saved_output"] == {}
    assert "characters omitted" in result["stdout"]
