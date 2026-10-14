# SPDX-License-Identifier: MIT
"""The Harness built from constructed objects, as the CLI and a run service build it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.harness import (
    Harness,
    IsolationRefusedError,
    Role,
    RunRequest,
    Settings,
    Stores,
    save,
)
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.mathchecks.jobs import Checker
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tools import ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient


class Worker:
    """A local worker: it manages no environments and has no isolation evidence."""

    base_url = "http://worker.invalid"

    async def environment(self, environment_id: str) -> None:
        raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

    async def capabilities(self, environment_id: str) -> SimpleNamespace:
        self.environment_id = environment_id
        location = SimpleNamespace(
            isolation_evidence=None, pool_id="p", backend="local", sandbox_id="e", hostname="h"
        )
        return SimpleNamespace(location=location, languages=(), features=())


class Sink:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.errors: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None:
        self.notes.append(name)


def main(*replies: str) -> Role:
    return Role(ScriptedModel([ModelTurn(reply) for reply in replies]), "scripted")


async def test_a_run_with_stores_resolves_its_session_runs_and_is_saved(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    worker = Worker()
    harness = Harness(
        cast(WorkerClient, worker),
        main("Answered."),
        stores=Stores(projects, documents, JobState(tmp_path), RunState(projects.control)),
        settings=Settings(min_isolation_tier=None),
    )
    sink = Sink()
    try:
        finished = await harness.run(RunRequest("p", "s", "Go", run_id="run-1"), sink)
    finally:
        documents.close()
    assert finished is not None
    session = projects.get("p").sessions["s"]
    assert finished.context.environment_id == worker.environment_id == session.environment_id
    assert (finished.status, finished.transcript.final_text) == (0, "Answered.")
    names = [hook.name for hook in finished.hooks]
    assert names == ["requests", "plans", "reviews", "math", "kernels", "messages", "events"]
    assert sink.notes[0].startswith("worker: pool p")
    assert (
        await save(
            finished.fields(), finished.status, finished.hooks, projects=projects, error=print
        )
        == 0
    )
    (entry,) = projects.runs("p", "s")
    assert projects.run("p", "s", entry.name).final_text == "Answered."


async def test_a_worker_below_the_minimum_isolation_is_refused(tmp_path: Path) -> None:
    harness = Harness(cast(WorkerClient, Worker()), main("Never asked."))
    request = RunRequest("p", "s", "Go", environment="env")
    with pytest.raises(IsolationRefusedError) as refused:
        await harness.run(request, Sink())
    assert str(refused.value) == (
        "the worker offers no isolation evidence. At least tier 3 is required"
    )


def stores(tmp_path: Path) -> tuple[Stores, DocumentStore]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    return Stores(projects, documents, JobState(tmp_path), RunState(projects.control)), documents


@pytest.mark.parametrize(
    "request_",
    [
        RunRequest("p", "s", "Go", run_id="../a run"),
        RunRequest("p", "s", "Go", environment="env-x"),
    ],
)
async def test_with_stores_a_request_must_name_its_record_and_not_its_environment(
    tmp_path: Path, request_: RunRequest
) -> None:
    held, documents = stores(tmp_path)
    worker = Worker()
    harness = Harness(cast(WorkerClient, worker), main(), stores=held)
    try:
        with pytest.raises(ValueError, match=r"should match pattern|choose the environment"):
            await harness.run(request_, Sink())
    finally:
        documents.close()
    # Refused before the session is opened or the worker is asked anything.
    assert "s" not in held.projects.get("p").sessions
    assert not hasattr(worker, "environment_id")


def test_a_reviewer_or_a_math_checker_needs_stores() -> None:
    client = cast(WorkerClient, Worker())
    with pytest.raises(ValueError, match="need the state directory"):
        Harness(client, main(), reviewer=main())
    with pytest.raises(ValueError, match="need the state directory"):
        Harness(client, main(), checker=cast(Checker, object()))


async def test_a_run_without_stores_uses_its_environment_and_keeps_nothing(
    tmp_path: Path,
) -> None:
    harness = Harness(
        cast(WorkerClient, Worker()), main("Done."), settings=Settings(min_isolation_tier=None)
    )
    finished = await harness.run(RunRequest("p", "s", "Go", environment="env"), Sink())
    assert finished is not None
    assert finished.context.environment_id == "env"
    assert {hook.name for hook in finished.hooks} == set()
    transcript = tmp_path / "run.json"
    saved = await save(
        finished.fields(), 0, finished.hooks, projects=None, transcript=transcript, error=print
    )
    assert saved == 0
    assert transcript.exists()
    with pytest.raises(ValueError, match="names its environment"):
        await harness.run(RunRequest("p", "s", "Go"), Sink())


async def test_a_delegated_runs_kernels_stop_when_it_ends(tmp_path: Path) -> None:
    """PD-37: a delegated run's own kernels stop with it; a main run's are the session's."""

    class Closing(Worker):
        def __init__(self) -> None:
            self.closed: list[str | None] = []

        async def close_session(self, context: object, run: str | None = None) -> None:
            self.closed.append(run)

    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    worker = Closing()
    harness = Harness(
        cast(WorkerClient, worker),
        main("Answered.", "Answered."),
        stores=Stores(projects, documents, JobState(tmp_path), RunState(projects.control)),
        settings=Settings(min_isolation_tier=None),
    )
    try:
        await harness.run(RunRequest("p", "s", "Go", run_id="run-1"), Sink())
        assert worker.closed == []
        delegated = RunRequest(
            "p", "s", "Look.", run_id="run-2", parent="run-1", delegation="dlg-1"
        )
        await harness.run(delegated, Sink())
    finally:
        documents.close()
    assert worker.closed == ["run-2"]
