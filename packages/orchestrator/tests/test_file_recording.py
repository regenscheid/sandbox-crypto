# SPDX-License-Identifier: MIT
"""Every file a call writes, recorded as an artifact revision by itself (research
artifacts, RA-27 to RA-35), through a real worker's HTTP API."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from sandbox_science.contracts import ErrorKind, ReadFileRequest, WriteFileRequest
from sandbox_science.contracts.artifact_inventory import ArtifactRevision
from sandbox_science.contracts.worker import TERMINAL_STATES
from sandbox_science.orchestrator import recording as recording_module
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.file_settings import DEFAULT_IGNORE, ProjectFiles
from sandbox_science.orchestrator.jobs import JobTools
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.recording import FileRecorder, RecordedFiles
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import eventually

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
    # Only c.bin, recorded by its hash: a tombstone has no content to keep or leave out.
    assert report["without_content"] == 1
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


async def test_the_run_keeps_its_calls_gaps(recording: Recording) -> None:
    """RA-36: the run's list says what its calls left out, by reason, over all its calls."""
    await recording.shell("mkdir -p many && for i in $(seq 1 103); do echo $i > many/$i; done")
    await recording.shell("for i in $(seq 1 102); do echo $i > many/b$i; done")
    hooks = [h for h in recording.toolbox.turn_hooks() if isinstance(h, RecordedFiles)]
    summary = hooks[0].summary()
    assert isinstance(summary, dict)
    assert summary["total"] == 200
    assert summary["gaps"] == {"over the 100-file limit of a call": 5}


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


async def test_a_pdf_helpers_files_are_recorded(recording: Recording) -> None:
    pdf_oxide = pytest.importorskip("pdf_oxide")
    client = recording.toolbox.client
    capabilities = await client.capabilities(CONTEXT.environment_id)
    toolbox = Toolbox(
        client, CONTEXT, features=capabilities.features, artifacts=recording.toolbox.artifacts,
        recorder=recording.recorder,
    )  # fmt: skip
    data = bytes(pdf_oxide.Pdf.from_markdown("# GF(17)\n\nEvery a has a^17 = a.\n").to_bytes())
    await client.write_file(
        WriteFileRequest(
            context=CONTEXT, path="note.pdf", content_b64=base64.b64encode(data).decode()
        )
    )
    arguments = {"path": "note.pdf", "page": 1, "dpi": 50}
    call = ToolCall(id="render", name="pdf_render", arguments=json.dumps(arguments))
    outcome = await toolbox.call(call, "op-render")
    assert outcome.record["recorded_files"]["recorded"] == 1
    (png,) = [path for path in recording.heads() if path.endswith(".png")]
    assert recording.heads()[png].publication.tool_call == "render"


async def test_every_file_tried_counts_toward_the_limit(
    recording: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "the worker is busy")

    monkeypatch.setattr(recording.toolbox.artifacts, "publish", refused)
    outcome = await recording.shell(
        "mkdir -p many && for i in $(seq 1 105); do echo $i > many/$i; done"
    )
    assert outcome.record["recorded_files"]["left_out"] == {
        "could not be recorded": 100, "over the 100-file limit of a call": 5,
    }  # fmt: skip


async def test_the_calls_byte_limit_counts_the_bytes_kept(
    recording: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that grew after its listing cannot carry a call past its limit."""
    monkeypatch.setattr(recording_module, "MAX_BYTES", 10)

    async def small(paths: Any) -> tuple[dict[str, int], dict[str, str]]:
        return dict.fromkeys(paths, 1), {}  # listed as 1 byte each: they grew

    monkeypatch.setattr(recording.recorder, "_sizes", small)
    outcome = await recording.shell("echo 1234567 > a.txt && echo 7654321 > b.txt")  # 8 bytes each
    report = outcome.record["recorded_files"]
    assert (report["recorded"], report["left_out"]) == (1, {"could not be recorded": 1})
    kept = sum(e.reference.size for e in recording.heads().values() if e.content_retained)
    assert kept <= 10


async def test_two_write_files_with_one_model_call_id_keep_both(recording: Recording) -> None:
    """Gateways that number calls per answer repeat call_0; the harness's operation differs."""
    for operation, text in (("op-a", "first"), ("op-b", "second")):
        arguments = json.dumps({"path": "notes.md", "content": text})
        call = ToolCall(id="call_0", name="write_file", arguments=arguments)
        await recording.toolbox.call(call, operation)
    head = recording.heads()["sessions/s1/notes.md"]
    assert head.reference.revision == 2
    assert recording.inventory.content("p1", head.reference) == b"second"
    empty = ToolCall(
        id="", name="write_file", arguments=json.dumps({"path": "e.md", "content": "x"})
    )
    outcome = await recording.toolbox.call(empty, "op-c")
    assert outcome.record["recorded_files"]["recorded"] == 1
    assert recording.heads()["sessions/s1/e.md"].publication.tool_call == "op-c"


async def test_tombstones_count_toward_the_limit(
    recording: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    await recording.shell("echo a > a && echo b > b && echo c > c")
    monkeypatch.setattr(recording_module, "MAX_FILES", 2)
    outcome = await recording.shell("rm a b c")
    report = outcome.record["recorded_files"]
    assert (report["deleted"], report["left_out"]) == (2, {"over the 100-file limit of a call": 1})


async def test_a_directory_that_cannot_be_listed_says_so(
    recording: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unlisted(*args: Any, **kwargs: Any) -> Any:
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "the executor went away")

    monkeypatch.setattr(recording.toolbox.client, "list_files", unlisted)
    outcome = await recording.shell("echo a > a.txt")
    assert outcome.record["recorded_files"]["left_out"] == {"its directory could not be listed": 1}


async def test_files_recorded_before_a_failure_stay(
    recording: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    await recording.shell("echo a > old.txt")

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store went away")

    monkeypatch.setattr(recording.toolbox.artifacts, "record_deletion", broken)
    outcome = await recording.shell("echo b > new.txt && rm old.txt")
    report = outcome.record["recorded_files"]
    assert report["recorded"] == 1
    assert "RuntimeError" in report["failed"]
    assert [f["path"] for f in recording.recorder.files] == ["old.txt", "new.txt"]


async def test_a_jobs_files_are_recorded_when_the_run_sees_it_end(recording: Recording) -> None:
    client = recording.toolbox.client
    jobs = JobTools(client, CONTEXT, run_id="run1")
    jobs.recorder = recording.recorder
    script = "mkdir -p out && echo 42 > out/n.txt"
    await jobs.start(
        {"script": script, "purpose": "write n", "expected_progress": "none"}, "op-job"
    )
    (job,) = (await client.jobs(CONTEXT, limit=10)).jobs

    async def ended() -> bool:
        execution = await client.get(job.execution_id, project_id="p1")
        return execution.state in TERMINAL_STATES

    await eventually(ended, timeout=30)
    (notice,) = await jobs.notices()
    assert cast(dict[str, Any], notice["recorded_files"])["recorded"] == 1
    head = recording.heads()["sessions/s1/out/n.txt"]
    assert (head.publication.tool_call, head.publication.execution_id) == (
        "op-job", job.execution_id,
    )  # fmt: skip
    assert recording.inventory.content("p1", head.reference) == b"42\n"


async def test_code_reaches_the_shared_area_the_file_tools_name(recording: Recording) -> None:
    """The file and PDF tools' shared/ is $PROJECT_SHARED in code; a relative shared/ in
    code is a folder of the session's own, and each code tool says so (lab 38)."""
    described = {spec.name: spec.description for spec in recording.toolbox.specs}
    assert all("$PROJECT_SHARED" in described[name] for name in ("shell", "python"))
    await recording.shell(
        "mkdir -p $PROJECT_SHARED/papers shared && echo project > $PROJECT_SHARED/papers/p.txt"
        " && echo session > shared/local.txt"
    )
    found = await recording.call("read_file", path="shared/papers/p.txt")
    assert "project" in found.text
    missing = await recording.call("read_file", path="shared/local.txt")
    assert "session" not in missing.text
    assert "shared/papers/p.txt" in recording.heads()


async def test_an_import_is_anchored_on_its_files_revision(
    recording: Recording, tmp_path: Path
) -> None:
    """M2 of #457's review (CM-9, the coordinator's decision): a results file a call wrote
    is imported as observed, anchored on the revision the call recorded and the line; a
    file no call recorded is recorded at import and imported as interpreted on it."""
    from sandbox_science.orchestrator.ledger import Ledger
    from sandbox_science.orchestrator.ledger.tool import LedgerTool
    from sandbox_science.orchestrator.projects import ProjectStore

    projects = ProjectStore(tmp_path)
    projects.create("p1", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p1", "s1", dedicated=False)
    projects.close()
    client = recording.toolbox.client
    ledgers = LedgerTool(
        Ledger(recording.files.documents), CONTEXT, run_id="run1", role="main",
        read=client.read_file,
    )  # fmt: skip
    ledgers.recorder = recording.recorder

    def line(n: int) -> str:
        change = {"type": "issue", "title": f"Order {n}", "statement": f"Is the order {n}?"}
        return json.dumps({"operation_id": f"op-{n}", "reason": "Ran.", "changes": [change]})

    async def imported(path: str, n: int) -> tuple[str | None, list[dict[str, Any]]]:
        read = await client.read_file(ReadFileRequest(context=CONTEXT, path=path))
        text, _ = await ledgers.call(
            "ledger_import", {"path": path, "sha256": read.sha256}, operation=f"import-{n}",
            call_id=f"import-{n}", model="m",
        )  # fmt: skip
        assert json.loads(text).get("lines") == 1, text
        view = ledgers.ledger.entry("p1", f"I-{n}")
        return view.basis, view.anchors

    await recording.shell(f"echo '{line(1)}' > ran.jsonl")
    head = recording.heads()["sessions/s1/ran.jsonl"]
    assert await imported("ran.jsonl", 1) == (
        "observed",
        [{"artifact": head.reference.artifact_id, "revision": 1, "line": 1,
          "sha256": head.reference.sha256}],
    )  # fmt: skip
    data = base64.b64encode(f"{line(2)}\n".encode()).decode()
    await client.write_file(WriteFileRequest(context=CONTEXT, path="put.jsonl", content_b64=data))
    assert "sessions/s1/put.jsonl" not in recording.heads()
    basis, anchors = await imported("put.jsonl", 2)
    put = recording.heads()["sessions/s1/put.jsonl"]
    assert put.publication.execution_id is None
    assert (basis, anchors[0]["artifact"]) == ("interpreted", put.reference.artifact_id)
