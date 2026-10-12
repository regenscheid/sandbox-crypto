# SPDX-License-Identifier: MIT
"""Partial observations survive failures; exception data and unseen delivery do not."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelError, ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, RunRecord
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

HOSTILE = (
    "Authorization: Bearer test-secret-failed-run-not-a-credential; "
    'body={"api_key":"test-secret-provider-body"}'
)
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


def record(transcript: Transcript, *, context: ExecutionContext = CONTEXT) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        run_id="failed",
        context=context,
        task="Continue",
        started_at=now,
        ended_at=now,
        stopped=transcript.stopped,
        failure=transcript.failure,
        final_text=transcript.final_text,
        messages=transcript.messages,
        tools=transcript.tool_records,
        job_notice_ids=transcript.job_notice_ids,
        work_request_ids=transcript.work_request_ids,
        review_notice_ids=transcript.review_notice_ids,
        math_notice_ids=transcript.math_notice_ids,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "tool", "harness"])
async def test_partial_observations_and_incomplete_calls_without_exception_text(
    phase: str, monkeypatch
) -> None:
    model = ScriptedModel.from_json(
        [
            {
                "text": "Computing",
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "one"}},
                    {"name": "read_file", "arguments": {"path": "two"}},
                ],
            }
        ]
    )
    calls = 0

    async def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if phase == "tool" and calls == 2:
            raise RuntimeError(HOSTILE)
        return ToolOutcome('{"content":"safely observed"}', {"path": str(calls)})

    async def on_tool(*args):
        if phase == "harness":
            raise OSError(HOSTILE)

    async def complete(messages, tools):
        if model.requests:
            raise ModelError(HOSTILE, status=429)
        return await ScriptedModel.complete(model, messages, tools)

    monkeypatch.setattr(model, "complete", complete)
    monkeypatch.setattr(Toolbox, "call", observed)
    async with WorkerClient("http://127.0.0.1:1", "synthetic") as client:
        transcript = await run_task(
            model, Toolbox(client, CONTEXT), "Continue", run_id="failed", on_tool=on_tool
        )
    assert (transcript.stopped, transcript.failure, transcript.final_text) == ("error", phase, None)
    kept = record(transcript)
    assert HOSTILE not in kept.to_json()
    assert "api_key" not in kept.to_json()
    assert "traceback" not in kept.to_json()
    assert len(kept.tools) == (2 if phase == "model" else 1)
    assert len([m for m in kept.messages if m["role"] == "tool"]) == len(kept.tools)
    requested = kept.messages[2]["tool_calls"]
    assert isinstance(requested, list)
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_job_delivery_snapshot_excludes_failing_model_call_and_cleanup_preserves_first(
    monkeypatch,
):
    class Jobs:
        def __init__(self):
            self.seen = set()
            self.fetches = 0

        @asynccontextmanager
        async def active(self):
            yield
            raise RuntimeError(HOSTILE)

        async def notices(self):
            self.fetches += 1
            job = f"exe-{self.fetches}"
            self.seen.add(job)
            return [{"job_id": job}]

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 2:
                raise ModelError(HOSTILE)
            return ModelTurn("First response")

    async with WorkerClient("http://127.0.0.1:1", "synthetic") as client:
        toolbox = Toolbox(client, CONTEXT)
        monkeypatch.setattr(toolbox, "jobs", Jobs())
        transcript = await run_task(Model(), toolbox, "Continue", run_id="failed")
    assert transcript.failure == "model"
    assert transcript.job_notice_ids == ["exe-1"]
    assert "exe-2" in json.dumps(transcript.messages)
    assert HOSTILE not in record(transcript).to_json()


@pytest.mark.asyncio
async def test_cancellation_and_setup_do_not_invent_a_record(monkeypatch):
    class Cancelled:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError

    class SetupFailure:
        @asynccontextmanager
        async def active(self):
            raise RuntimeError("setup failed")
            yield

    async with WorkerClient("http://127.0.0.1:1", "synthetic") as client:
        toolbox = Toolbox(client, CONTEXT)
        with pytest.raises(asyncio.CancelledError):
            await run_task(Cancelled(), toolbox, "Continue", run_id="cancelled")
        monkeypatch.setattr(toolbox, "jobs", SetupFailure())
        with pytest.raises(RuntimeError, match="setup failed"):
            await run_task(Cancelled(), toolbox, "Continue", run_id="not-started")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", [None, "model", "setup"])
async def test_cli_cleanup_failure_keeps_partial_record_and_closes_model(
    tmp_path: Path, monkeypatch, capsys, cancel_phase
):
    closed = []

    class Model(ScriptedModel):
        def __init__(self, *args, **kwargs):
            super().__init__([ModelTurn("Observed answer before cleanup")])

        async def complete(self, messages, tools):
            if cancel_phase == "model":
                raise asyncio.CancelledError("model cancelled")
            return await super().complete(messages, tools)

        async def close(self):
            closed.append("model")
            raise RuntimeError(HOSTILE)

    class Client:
        base_url = "http://unused.invalid"

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("client")
            if cancel_phase:
                raise RuntimeError(HOSTILE)

        async def capabilities(self, *args):
            return SimpleNamespace(
                location=SimpleNamespace(
                    isolation_evidence=None,
                    pool_id="p",
                    backend="local",
                    sandbox_id="e",
                    hostname="synthetic",
                ),
                languages=(),
                features=(),
            )

    async def context(*args):
        if cancel_phase == "setup":
            raise asyncio.CancelledError("setup cancelled")
        return CONTEXT.model_copy(
            update={"environment_id": ProjectStore(tmp_path).get("p").default_environment_id}
        )

    original_close = DocumentStore.close

    def close(store):
        original_close(store)
        if store.producer == "harness":
            closed.append("store")
            raise OSError(HOSTILE)

    monkeypatch.setattr(cli, "OpenAICompatibleModel", Model)
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "_context", context)
    monkeypatch.setattr(cli, "_read_secret", lambda _: "synthetic")
    monkeypatch.setattr(DocumentStore, "close", close)
    args = argparse.Namespace(
        script=None,
        model_url="http://unused.invalid",
        model="main",
        api_key_file=None,
        run_id="failed",
        worker="unused",
        token_file=None,
        allow_unisolated=True,
        state_dir=tmp_path,
        watcher_model=None,
        reviewer_model=None,
        math_node=None,
        watcher_calls_per_hour=1,
        watcher_first_check=1,
        watcher_interval=1,
        task="Continue",
        max_turns=1,
        transcript=tmp_path / "transcript.json",
    )
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    if cancel_phase:
        with pytest.raises(asyncio.CancelledError, match=f"{cancel_phase} cancelled"):
            await cli._run(args)
        assert projects.runs("p", "s") == []
        assert not args.transcript.exists()
        assert closed == (["client"] if cancel_phase == "setup" else ["store", "model", "client"])
        return
    assert await cli._run(args) == 1
    assert closed == ["store", "model", "client"]
    kept = projects.run("p", "s", projects.runs("p", "s")[0].name)
    assert kept.failure == "cleanup"
    assert kept.final_text is None
    assert kept.messages[-1]["content"] == "Observed answer before cleanup"
    assert HOSTILE not in args.transcript.read_text()
    assert HOSTILE not in capsys.readouterr().err


def test_failed_save_never_acknowledges_and_foreign_session_is_refused(tmp_path, monkeypatch):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    transcript = Transcript(messages=[{"role": "user", "content": "Continue"}])
    transcript.fail("model")
    kept = record(transcript)
    with pytest.raises(ProjectError, match="no session"):
        projects.save_run(
            kept.model_copy(
                update={"context": CONTEXT.model_copy(update={"session_id": "foreign"})}
            )
        )
    acknowledged = []
    monkeypatch.setattr(JobState, "acknowledge", lambda *args: acknowledged.append(args))

    def failed(*args):
        raise OSError("synthetic save failure")

    save = ProjectStore.save_run
    monkeypatch.setattr(ProjectStore, "save_run", failed)
    args = argparse.Namespace(state_dir=tmp_path, transcript=None)
    assert cli._keep(args, kept.model_dump(mode="json"), 1) == 1
    assert projects.runs("p", "s") == []
    assert acknowledged == []
    monkeypatch.setattr(ProjectStore, "save_run", save)
    assert cli._keep(args, kept.model_dump(mode="json"), 1) == 1
    assert len(projects.runs("p", "s")) == len(acknowledged) == 1


def test_legacy_records_and_failure_classification_are_bounded():
    old = record(Transcript(messages=[])).model_dump(mode="json")
    old.pop("failure")
    assert RunRecord.model_validate(old).failure is None
    for failure in (HOSTILE, {"message": HOSTILE}, "model" * 10000):
        with pytest.raises(ValidationError):
            RunRecord.model_validate({**old, "stopped": "error", "failure": failure})
    with pytest.raises(ValidationError, match="no final answer"):
        RunRecord.model_validate({**old, "failure": "model"})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "entry"])
async def test_cancellation_survives_active_context_cleanup_failure(phase, monkeypatch):
    class Jobs:
        @asynccontextmanager
        async def active(self):
            try:
                yield
            finally:
                raise RuntimeError(HOSTILE)

        async def notices(self):
            return []

    class Other:
        @asynccontextmanager
        async def active(self):
            raise asyncio.CancelledError("entry cancelled")
            yield

    class Model:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError("model cancelled")

    async with WorkerClient("http://127.0.0.1:1", "synthetic") as worker:
        toolbox = Toolbox(worker, CONTEXT)
        monkeypatch.setattr(toolbox, "jobs", Jobs())
        if phase == "entry":
            monkeypatch.setattr(toolbox, "reviews", Other())
        with pytest.raises(asyncio.CancelledError, match=f"{phase} cancelled"):
            await run_task(Model(), toolbox, "Continue", run_id="cancelled")
