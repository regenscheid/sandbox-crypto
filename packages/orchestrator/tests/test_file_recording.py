# SPDX-License-Identifier: MIT
"""Every file a call writes, recorded as an artifact revision by itself (research
artifacts, RA-27 to RA-35), through a real worker's HTTP API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts.artifact_inventory import ArtifactRevision
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.file_settings import DEFAULT_IGNORE, ProjectFiles
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.recording import FileRecorder, RecordedFiles
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_artifact_publication import CONTEXT, TOKEN
from .test_artifact_publication import worker_url as worker_url

pytestmark = pytest.mark.asyncio


@dataclass
class Recording:
    toolbox: Toolbox
    inventory: ArtifactInventory
    files: ProjectFiles
    recorder: FileRecorder
    calls: int = 0

    async def call(self, name: str, **arguments: Any) -> ToolOutcome:
        self.calls += 1
        call = ToolCall(id=f"call{self.calls}", name=name, arguments=json.dumps(arguments))
        return await self.toolbox.call(call, f"op{self.calls}", model="gateway-model")

    async def shell(self, script: str) -> ToolOutcome:
        return await self.call("shell", script=script)

    def heads(self) -> dict[str, ArtifactRevision]:
        found: dict[str, ArtifactRevision] = {}
        after = ""
        while True:
            page = self.inventory.list("p1", after=after)
            found.update({entry.path: entry for entry in page.entries})
            if page.next_after is None:
                return found
            after = page.next_after


@pytest_asyncio.fixture
async def recording(worker_url: str, tmp_path: Path) -> AsyncIterator[Recording]:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents, limits=ArtifactLimits(artifact_bytes=1024))
    files = ProjectFiles(documents)
    async with WorkerClient(worker_url, TOKEN) as client:
        publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1", alias="research")
        recorder = FileRecorder(
            publisher, files, role="delegated", parent_run="20261014T120000000000Z-root",
            delegation="dlg-1",
        )  # fmt: skip
        toolbox = Toolbox(client, CONTEXT, artifacts=publisher, recorder=recorder)
        yield Recording(toolbox, inventory, files, recorder)
        await publisher.aclose()
    documents.close()


async def test_a_calls_files_become_revisions(recording: Recording) -> None:
    outcome = await recording.shell(
        "mkdir -p notes data .cache/pip $PROJECT_SHARED && echo hello > notes/summary.md"
        " && echo cached > .cache/pip/x && head -c 2048 /dev/zero > data/big.bin"
        " && echo '@book{x}' > $PROJECT_SHARED/bibliography.bib"
    )
    report = outcome.record["recorded_files"]
    assert (report["recorded"], report["without_content"], report["deleted"]) == (3, 1, 0)
    assert json.loads(outcome.text)["recorded_files"]["recorded"] == 3  # the model is told
    heads = recording.heads()
    assert set(heads) == {
        "sessions/s1/notes/summary.md", "sessions/s1/data/big.bin", "shared/bibliography.bib",
    }  # fmt: skip
    summary = heads["sessions/s1/notes/summary.md"]
    assert recording.inventory.content("p1", summary.reference) == b"hello\n"
    big = heads["sessions/s1/data/big.bin"]
    assert (big.content_retained, big.content_note, big.reference.size) == (
        False, "too_large", 2048,
    )  # fmt: skip
    source = summary.publication
    execution = outcome.record["execution"]
    assert (source.kind, source.role, source.delegation, source.tool_call) == (
        "execution_record", "delegated", "dlg-1", "call1",
    )  # fmt: skip
    assert (source.execution_id, source.attempt_state) == (execution["execution_id"], "succeeded")
    assert source.model == "gateway-model"


async def test_deletions_and_rewrites(recording: Recording) -> None:
    await recording.shell("echo one > a.txt && echo two > b.txt && head -c 2048 /dev/zero > c.bin")
    again = await recording.call(
        "python",
        code="import os\nos.remove('a.txt')\nopen('c.bin', 'wb').write(bytes(2048))",
    )
    report = again.record["recorded_files"]
    assert (report["deleted"], report["recorded"]) == (1, 1)
    heads = recording.heads()
    assert heads["sessions/s1/a.txt"].deleted
    assert heads["sessions/s1/a.txt"].reference.revision == 2
    assert heads["sessions/s1/c.bin"].reference.revision == 1  # the same content: reproduced
    assert [f["reproduced"] for f in report["files"] if f["path"] == "c.bin"] == [True]


async def test_a_failed_calls_files_say_so(recording: Recording) -> None:
    outcome = await recording.call(
        "python", code="open('partial.csv', 'w').write('a,b\\n')\nraise ValueError('half done')"
    )
    assert outcome.record["execution"]["state"] == "failed"
    head = recording.heads()["sessions/s1/partial.csv"]
    assert head.publication.attempt_state == "failed"


async def test_write_file_is_recorded_too(recording: Recording) -> None:
    outcome = await recording.call("write_file", path="draft.md", content="# Draft\n")
    assert outcome.record["recorded_files"]["recorded"] == 1
    head = recording.heads()["sessions/s1/draft.md"]
    assert (head.publication.execution_id, head.publication.tool_call) == (None, "call1")


async def test_a_call_records_at_most_100_files(recording: Recording) -> None:
    outcome = await recording.shell(
        "mkdir -p many && for i in $(seq 1 105); do echo $i > many/$i; done"
    )
    report = outcome.record["recorded_files"]
    assert report["recorded"] == 100
    assert report["left_out"] == {"over the 100-file limit of a call": 5}
    assert len(recording.heads()) == 100


async def test_results_only_records_nothing(recording: Recording) -> None:
    change = {"expected": 0, "recording": "results", "ignore": list(DEFAULT_IGNORE)}
    recording.files.change("p1", {**change, "operation_id": "o1"}, USER)
    outcome = await recording.shell("echo x > x.txt")
    assert "recorded_files" not in outcome.record
    assert recording.heads() == {}


async def test_the_project_patterns_apply(recording: Recording) -> None:
    change = {"expected": 0, "recording": "all", "ignore": ["*.log", "scratch/"]}
    recording.files.change("p1", {**change, "operation_id": "o1"}, USER)
    await recording.shell("mkdir -p scratch .cache && echo a > run.log && echo b > scratch/c"
                          " && echo d > .cache/e && echo f > kept.txt")  # fmt: skip
    assert set(recording.heads()) == {"sessions/s1/kept.txt", "sessions/s1/.cache/e"}


async def test_the_run_keeps_its_list_of_recorded_files(recording: Recording) -> None:
    await recording.shell("echo a > a.txt && echo b > b.txt")
    hooks = [h for h in recording.toolbox.turn_hooks() if isinstance(h, RecordedFiles)]
    assert len(hooks) == 1
    summary = hooks[0].summary()
    assert isinstance(summary, dict)
    assert summary["total"] == 2
    assert [f["path"] for f in summary["files"]] == ["a.txt", "b.txt"]


async def test_a_full_budget_keeps_hashes_and_then_stops(worker_url: str, tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(
        documents, limits=ArtifactLimits(project_bytes=4, project_revisions=2)
    )
    async with WorkerClient(worker_url, TOKEN) as client:
        publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
        recorder = FileRecorder(publisher, ProjectFiles(documents))
        toolbox = Toolbox(client, CONTEXT, artifacts=publisher, recorder=recorder)
        call = ToolCall(
            id="c1",
            name="shell",
            arguments=json.dumps({"script": "echo 123456 > a; echo 2 > b; echo 3 > c"}),
        )
        outcome = await toolbox.call(call, "op1")
        await publisher.aclose()
    report = outcome.record["recorded_files"]
    assert (report["recorded"], report["without_content"]) == (2, 1)  # a is 7 bytes of 4
    assert report["left_out"] == {"the project's artifact storage is full": 1}
    notes = {e.path: e.content_note for e in inventory.list("p1").entries}
    assert notes == {"sessions/s1/a": "quota", "sessions/s1/b": None}
    documents.close()
