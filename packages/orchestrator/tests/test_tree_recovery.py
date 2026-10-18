# SPDX-License-Identifier: MIT
"""Explicit Resume drives linked leaf-first attempts through the real harness."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import KernelObservation, new_id
from sandbox_science.orchestrator.agent_messages import AgentMessageStore
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.recovery import Recovery
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.harness import Harness, Role, RunRequest, Settings
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually

from .test_run_harness import Sink, Worker, main, stores
from .test_tool_journal import RUN
from .test_work_ownership import CHILD, CONTINUATION, GRANDCHILD, record

STOPPED = "20261016T030003000000Z-stopped"
COMPLETED = "20261016T030004000000Z-completed"


class RecoveryWorker(Worker):
    def __init__(self):
        self.claims = []
        self.closed = []
        self.ready = False

    async def capabilities(self, environment_id):
        self.ready = True
        return await super().capabilities(environment_id)

    async def claim_environment(self, request):
        assert self.ready, "cannot retain a suspended environment"
        self.claims.append(request)
        return await super().claim_environment(request)

    async def observe_kernels(self, context):
        return KernelObservation(
            environment_id=context.environment_id,
            session_id=context.session_id,
            observed_at=datetime.now(UTC),
            kernels=(),
        )

    async def close_session(self, context, *, run=None):
        self.closed.append(run)


def prepare_tree(tmp_path: Path, *, alias: str | None = "scripted"):
    state, documents = stores(tmp_path)
    session = state.projects.open_session("p", "s", dedicated=False)
    from sandbox_science.contracts import ExecutionContext

    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision=state.projects.get("p").policy,
    )
    works = WorkRecords(state.projects.control)
    blueprints = Blueprints(state.projects.control)
    delegations = DelegationRecords(state.projects.control)
    for run, parent in ((RUN, None), (CHILD, RUN), (GRANDCHILD, CHILD)):
        owner = works.admit("p", "s", run, parent=parent)
        delegation = None if parent is None else new_id("dlg")
        request = RunRequest(
            "p",
            "s",
            "Continue research",
            parent=parent,
            delegation=delegation,
            tokens=100000 if parent else None,
            alias=alias,
            memory_version=0,
            root=RUN if parent else None,
            may_delegate=run == CHILD,
        )
        from pydantic import TypeAdapter

        blueprints.save(
            owner,
            Blueprint(
                context=context,
                request=TypeAdapter(RunRequest).dump_python(
                    request, mode="json", exclude={"history", "stop_by"}
                ),
            ),
        )
        if parent:
            assert delegation is not None
            delegations.start(
                "p",
                "s",
                Delegation(
                    delegation_id=delegation,
                    parent=parent,
                    run=run,
                    kind="task",
                    brief={"task": "Continue research"},
                    started_at=datetime.now(UTC),
                ),
            )
            delegations.end("p", "s", delegation, Ending("interrupted", "interrupted"))
    works.admit("p", "s", STOPPED, parent=RUN)
    works.stop("p", "s", STOPPED)
    works.admit("p", "s", COMPLETED, parent=RUN)
    works.finish(record(COMPLETED))
    works.interrupt("p", "s", RUN)
    return state, documents, works


async def drive(recovery, sink, stop):
    running = asyncio.create_task(recovery.run(sink, stop))
    try:
        await eventually(lambda: recovery.started or running.done())
        while not running.done():
            ready = await recovery.next()
            if ready:
                await asyncio.gather(*(recovery.branch(work) for work in ready))
            else:
                await asyncio.sleep(0)
        return await running
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_one_resume_recovers_three_levels_and_preserves_stopped_branch(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    worker = RecoveryWorker()
    models = [
        ModelTurn(json.dumps({"status": "complete", "summary": "Recovered"})) for _ in range(8)
    ]
    harness = Harness(
        cast(WorkerClient, worker),
        main("Recovered root"),
        delegated=Role(ScriptedModel(models), "scripted"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    assert request
    recovery = Recovery(harness, "p", "s", request)
    try:
        sink = Sink()
        failed = await drive(recovery, sink, asyncio.Event())
        states = [(w.work, w.state, w.reason) for w in works.page("p", "s", RUN)]
        assert not failed, (states, sink.errors)
        assert all(
            w.state == ("stopped" if w.work == STOPPED else "completed")
            for w in works.page("p", "s", RUN)
        ), states
        assert all(len(works.attempts("p", "s", run)) == 2 for run in (RUN, CHILD, GRANDCHILD))
        assert works.attempts("p", "s", STOPPED) == [STOPPED]
        assert works.attempts("p", "s", COMPLETED) == [COMPLETED]
        assert worker.claims
        assert worker.claims[0].context.authority.work == RUN
        assert works.request("p", "s") is None
    finally:
        documents.close()
        state.projects.close()


def test_leaf_first_resume_reserves_parent_mailbox_before_parent_runs(tmp_path: Path) -> None:
    state, documents, works = prepare_tree(tmp_path)
    harness = Harness(
        cast(WorkerClient, RecoveryWorker()),
        main("Recovered root"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    recovery = Recovery(harness, "p", "s", request)
    try:
        RunState(state.projects.control).acquire("p", "s", recovery.run_name, "fixture")
        _, epoch = works.claim("p", "s", request, lease_run=recovery.run_name)
        with recovery.plans.authority(epoch, recovery.run_name):
            recovery.plans.prepare(RUN, run=recovery.run_name, started=recovery.started_at)
            grandchild = works.get("p", "s", GRANDCHILD)
            grandchild_delegation = recovery._delegation_for(grandchild)
            assert grandchild_delegation is not None
            assert recovery._delegation_for(grandchild) == grandchild_delegation
            parent_run = recovery.plans.get(CHILD).run
            with state.projects.control.read() as tx:
                (parent_delegation,) = tx.sql.execute(
                    "SELECT delegation_id FROM delegations WHERE project_id='p' "
                    "AND session_id='s' AND run=?",
                    (parent_run,),
                ).fetchone()
            mailbox = AgentMessageStore(state.projects.control)
            message = mailbox.send_parent(
                "p",
                "s",
                recovery.plans.get(GRANDCHILD).run,
                grandchild_delegation,
                "Found a blocker",
                "resume-message",
            )
            assert message.recipient_delegation == parent_delegation
            assert [item.message_id for item in mailbox.pending("p", "s", parent_delegation)] == [
                message.message_id
            ]
    finally:
        documents.close()
        state.projects.close()


async def test_failed_environment_preparation_prevents_claim_and_model_dispatch(tmp_path: Path):
    from sandbox_science.contracts import ErrorKind
    from sandbox_science.orchestrator.worker_client import WorkerAPIError

    class MissingWorker(RecoveryWorker):
        async def capabilities(self, environment_id):
            raise WorkerAPIError(ErrorKind.NOT_FOUND, "original environment missing")

    state, documents, works = prepare_tree(tmp_path)
    worker = MissingWorker()
    model = ScriptedModel([ModelTurn("Must not run")])
    harness = Harness(
        cast(WorkerClient, worker),
        Role(model, "scripted"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    try:
        with pytest.raises(WorkerAPIError, match="original environment missing"):
            await drive(Recovery(harness, "p", "s", request), Sink(), asyncio.Event())
        assert not worker.claims
        assert not model.requests
        assert works.attempts("p", "s", RUN) == [RUN]
        with state.projects.control.read() as tx:
            status, reason = tx.sql.execute(
                "SELECT status, reason FROM research_resumes WHERE request=?", (request,)
            ).fetchone()
        assert status == "blocked"
        assert reason == "interrupted"
    finally:
        documents.close()
        state.projects.close()


def test_late_cleanup_and_guard_cannot_mutate_new_resume(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    try:
        first = works.request("p", "s")
        assert first is not None
        RunState(state.projects.control).acquire("p", "s", CONTINUATION, "fixture")
        _, epoch = works.claim("p", "s", first, lease_run=CONTINUATION)
        plans = Plans(state.projects.control, "p", "s", first)
        works.admit("p", "s", CONTINUATION, work=RUN, predecessor=RUN, epoch=epoch)
        works.admit("p", "s", "20261018T030000000000Z-next")
        before = works.get("p", "s", RUN)
        works.interrupt_run("p", "s", CONTINUATION)
        assert works.get("p", "s", RUN) == before
        with pytest.raises(ControlError, match="lost"), plans.authority(epoch, CONTINUATION):
            works.block("p", "s", RUN, "stale")
        assert works.get("p", "s", RUN) == before
    finally:
        documents.close()
        state.projects.close()


async def test_unknown_paid_child_without_old_pins_resumes_with_new_pins(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    owner = works.owner("p", "s", GRANDCHILD)
    assert owner is not None
    # Simulate a paid dispatch whose provider response disappeared.
    with state.projects.control.write() as tx:
        tx.sql.execute("UPDATE research_work SET state='running' WHERE work=?", (GRANDCHILD,))
    identity = ModelIntents(state.projects.control).begin(owner, kind="delegated", alias="scripted")
    works.interrupt("p", "s", RUN)
    worker = RecoveryWorker()
    harness = Harness(
        cast(WorkerClient, worker),
        main("Root waits"),
        delegated=Role(
            ScriptedModel([ModelTurn('{"status":"complete","summary":"Wait"}')] * 8), "scripted"
        ),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    try:
        assert not await drive(Recovery(harness, "p", "s", request), Sink(), asyncio.Event())
        assert works.get("p", "s", GRANDCHILD).state == "completed"
        assert len(works.attempts("p", "s", GRANDCHILD)) == 2
        assert ModelIntents(state.projects.control).pending("p", "s", GRANDCHILD) == (identity,)
        with state.projects.control.read() as tx:
            assert (
                tx.sql.execute(
                    "SELECT status FROM research_resumes WHERE request=?", (request,)
                ).fetchone()[0]
                == "finished"
            )
    finally:
        documents.close()
        state.projects.close()


async def test_legacy_missing_alias_blocks_before_new_model_dispatch(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path, alias=None)
    model = ScriptedModel([ModelTurn("Must not be called")])
    harness = Harness(
        cast(WorkerClient, RecoveryWorker()),
        Role(model, "changed-default"),
        delegated=Role(model, "changed-default"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    assert request
    try:
        assert await drive(Recovery(harness, "p", "s", request), Sink(), asyncio.Event())
        assert not model.requests
        assert "original model alias" in (works.get("p", "s", RUN).reason or "")
    finally:
        documents.close()
        state.projects.close()


async def test_resume_can_be_interrupted_again_before_parent_admission(tmp_path: Path):
    from sandbox_science.orchestrator.history.operations import ToolJournal
    from sandbox_science.orchestrator.model import ToolCall
    from sandbox_science.orchestrator.questions import Answer, Questions

    state, documents, works = prepare_tree(tmp_path)
    context = Blueprints(state.projects.control).get("p", "s", RUN).context
    ToolJournal(state.projects.control).start(
        context, RUN, "pending-question", ToolCall("original-question", "ask_user", "{}")
    )
    questions = Questions(state.projects.control)
    question = questions.ask(context, RUN, "original-question", "pending-question", "Which path?")
    model = ScriptedModel([ModelTurn("Recovered root")])
    harness = Harness(
        cast(WorkerClient, RecoveryWorker()),
        Role(model, "scripted"),
        delegated=Role(
            ScriptedModel(
                [ModelTurn('{"status":"complete","summary":"Recovered child evidence"}')] * 8
            ),
            "scripted",
        ),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    # This fixture injects a historical tool/question below the original loop.
    # Supply that original loop's known scripted prompt pins as well; real started
    # work without this evidence must not qualify as never-started recovery.
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.run_inputs import RunInputs, StartingInputs

    async def original_pins(run):
        if run.prompt_provenance is not None and run.prompt_provenance.role == "main":
            inputs = RunInputs(state.projects.control)
            try:
                inputs.get("p", "s", RUN)
            except ControlError:
                inputs.save(
                    StartingInputs(
                        run=RUN,
                        context=context,
                        request=Blueprints(state.projects.control).get("p", "s", RUN).request,
                        configuration={},
                        pins=run.prompt_provenance,
                    )
                )

    async def fixture_loop(model, toolbox, task, **options):
        return await run_task(model, toolbox, task, on_started=original_pins, **options)

    harness.loop_runner = fixture_loop
    request = works.request("p", "s")
    assert request is not None
    assert request
    first = Recovery(harness, "p", "s", request)
    running = asyncio.create_task(drive(first, Sink(), asyncio.Event()))
    try:
        await eventually(lambda: RUN in first.active)
        assert works.get("p", "s", CHILD).state == "completed"
        assert works.get("p", "s", GRANDCHILD).state == "completed"
        assert works.attempts("p", "s", RUN) == [RUN]
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        questions.answer("p", "s", question.question_id, Answer(kind="free", text="Continue"))
        second = works.request("p", "s")
        assert second
        assert second != request
        assert not await drive(Recovery(harness, "p", "s", second), Sink(), asyncio.Event())
        assert len(works.attempts("p", "s", CHILD)) == 2
        assert len(works.attempts("p", "s", GRANDCHILD)) == 2
        assert works.get("p", "s", RUN).state == "completed"
        assert "Recovered child evidence" in json.dumps(model.requests)
        recovered = questions.by_operation("p", "s", "pending-question")
        assert recovered is not None
        assert recovered.question_id == question.question_id
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        documents.close()
        state.projects.close()
