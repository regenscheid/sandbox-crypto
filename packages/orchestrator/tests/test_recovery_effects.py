# SPDX-License-Identifier: MIT
"""Recovery observes original effects and kernel identity; it never dispatches them."""

from datetime import UTC, datetime
from typing import cast

import pytest

from sandbox_science.contracts import Execution, KernelInfo, KernelObservation, Result
from sandbox_science.contracts.recovery import OperationLookup
from sandbox_science.contracts.worker import ExecutionState
from sandbox_science.orchestrator.continuity.kernels import KernelJournal
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.reconcile import Reconciler
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resolutions import RecoveryControls, Resolution
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.operations import ToolJournal
from sandbox_science.orchestrator.history.usage import AnsweredUsage
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.questions import Answer, Questions
from sandbox_science.orchestrator.tools import PDF_SPECS
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects


class PassiveWorker:
    """Deliberately exposes no submit, run, or restart method."""

    def __init__(self, execution):
        self.execution = execution
        self.lookups = []

    async def lookup(self, context, operation):
        self.lookups.append((context, operation))
        return OperationLookup(execution=self.execution)


@pytest.mark.parametrize(
    "tool", ["shell", "python", "sage", "start_job", *[s.name for s in PDF_SPECS]]
)
@pytest.mark.parametrize("state", ["succeeded", "lost", "absent"])
async def test_original_worker_identity_is_observed_without_reexecution(
    projects: ProjectStore, tool: str, state: str
):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    journal = ToolJournal(projects.control)
    call = ToolCall("original-call", tool, '{"code":"original \u03b1"}')
    journal.start(CONTEXT, RUN, "original-operation", call)
    works.interrupt("p", "s", RUN)
    execution = (
        None
        if state == "absent"
        else Execution(
            execution_id="original-execution",
            attempt_id="original-attempt",
            operation_id="original-operation",
            kind="shell",
            context=CONTEXT,
            state=ExecutionState(state),
            submitted_at=datetime.now(UTC),
            result=Result(exit_code=0),
        )
    )
    worker = PassiveWorker(execution)
    reconciler = Reconciler(projects.control, cast(WorkerClient, worker))
    if state == "succeeded":
        assert execution is not None
        await reconciler.work(works.get("p", "s", RUN))
        assert journal.pending("p", "s", RUN) == []
        (message,) = Archive(projects.control).messages("p", "s", run=RUN)
        assert message.entry.call_id == call.id
        assert journal.record("p", message.message_id) == {
            "tool": tool,
            "arguments": call.arguments,
            "execution": execution.model_dump(mode="json"),
            "reconciled": True,
        }
        await reconciler.work(works.get("p", "s", RUN))
    else:
        with pytest.raises(ControlError, match=r"lost_operation|unknown_operation"):
            await reconciler.work(works.get("p", "s", RUN))
        assert len(journal.pending("p", "s", RUN)) == 1
    assert worker.lookups == [(CONTEXT, "original-operation")]


async def test_background_job_recovers_admission_without_waiting_or_resubmitting(
    projects: ProjectStore,
):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    journal = ToolJournal(projects.control)
    journal.start(CONTEXT, RUN, "job-operation", ToolCall("job-call", "start_job", "{}"))
    works.interrupt("p", "s", RUN)
    execution = Execution(
        execution_id="original-job",
        attempt_id="original-attempt",
        operation_id="job-operation",
        kind="job",
        context=CONTEXT,
        state=ExecutionState.RUNNING,
        submitted_at=datetime.now(UTC),
    )
    worker = PassiveWorker(execution)  # no event wait or submission capability
    reconciler = Reconciler(projects.control, cast(WorkerClient, worker))
    await reconciler.work(works.get("p", "s", RUN))
    await reconciler.work(works.get("p", "s", RUN))
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT * FROM job_origins").fetchall() == [
            ("p", "s", "original-job", RUN)
        ]
    assert journal.pending("p", "s", RUN) == []
    assert worker.lookups == [(CONTEXT, "job-operation")]
    (message,) = Archive(projects.control).messages("p", "s", run=RUN)
    assert message.entry.call_id == "job-call"
    assert '"running"' in (message.entry.content or "")


async def test_answered_question_is_recovered_by_original_call_once(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    questions = Questions(projects.control)
    journal = ToolJournal(projects.control)
    journal.start(CONTEXT, RUN, "question-operation", ToolCall("question-call", "ask_user", "{}"))
    question = questions.ask(CONTEXT, RUN, "question-call", "question-operation", "Which path?")
    works.interrupt("p", "s", RUN)
    projects.control.close()
    questions.answer(
        "p", "s", question.question_id, Answer(kind="free", text="Keep the exact path")
    )
    worker = PassiveWorker(None)
    recovery = Reconciler(projects.control, cast(WorkerClient, worker))
    await recovery.work(works.get("p", "s", RUN))
    await recovery.work(works.get("p", "s", RUN))
    (message,) = Archive(projects.control).messages("p", "s", run=RUN)
    assert message.entry.call_id == "question-call"
    assert "Keep the exact path" in (message.entry.content or "")
    recovered = questions.by_operation("p", "s", "question-operation")
    assert recovered is not None
    assert recovered.question_id == question.question_id
    assert not worker.lookups


@pytest.mark.parametrize("generation", [1, 2, None])
async def test_saved_kernel_generation_must_survive(projects: ProjectStore, generation):
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    saved = KernelObservation(
        environment_id=CONTEXT.environment_id,
        session_id="s",
        observed_at=datetime.now(UTC),
        kernels=(KernelInfo(kernel_id="original-kernel", language="python", generation=1),),
    )
    await journal.record(saved)

    class Observer:
        async def observe_kernels(self, context):
            assert context == CONTEXT
            return saved.model_copy(
                update={
                    "kernels": ()
                    if generation is None
                    else (saved.kernels[0].model_copy(update={"generation": generation}),)
                }
            )

    if generation == 1:
        await journal.reconcile(cast(WorkerClient, Observer()), CONTEXT)
    else:
        with pytest.raises(ControlError, match="lost_kernel"):
            await journal.reconcile(cast(WorkerClient, Observer()), CONTEXT)


def test_operator_observed_tokens_do_not_reset_or_duplicate_the_grant(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    answered = intents.begin(owner, kind="main", alias="scripted")
    unknown = intents.begin(owner, kind="main", alias="scripted")
    Archive(projects.control).append(
        "p",
        "s",
        RUN,
        Entry("assistant", "assistant", "Recorded response"),
        model_usage=AnsweredUsage(
            ordinal=1,
            observed_at=datetime.now(UTC),
            intent_id=answered,
            usage=ModelUsage(input_tokens=2, output_tokens=3),
        ),
    )
    works.interrupt("p", "s", RUN)
    RecoveryControls(projects.control).resolve(
        "p",
        "s",
        Resolution(
            work=RUN,
            current=RUN,
            run=RUN,
            epoch=owner.epoch,
            kind="model_usage",
            identity=unknown,
            reason="Provider receipt checked",
            cost=1.0,
            input_tokens=7,
            output_tokens=11,
        ),
        operator="user",
    )
    assert Plans(projects.control, "p", "s", "unused").spent_tokens(RUN) == 23


@pytest.mark.parametrize("tool", ["wait_delegations", "delegation_status", "cancel_delegation"])
async def test_interrupted_child_observation_does_not_invent_results_or_dispatch(
    projects: ProjectStore, tool: str
):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    journal = ToolJournal(projects.control)
    call = ToolCall("child-observation", tool, '{"delegation_id":"original-child"}')
    journal.start(CONTEXT, RUN, "child-observation-operation", call)
    works.interrupt("p", "s", RUN)
    worker = PassiveWorker(None)
    reconciler = Reconciler(projects.control, cast(WorkerClient, worker))
    if tool == "cancel_delegation":
        with pytest.raises(ControlError, match="unknown_operation"):
            await reconciler.work(works.get("p", "s", RUN))
        assert len(journal.pending("p", "s", RUN)) == 1
    else:
        await reconciler.work(works.get("p", "s", RUN))
        await reconciler.work(works.get("p", "s", RUN))
        assert journal.pending("p", "s", RUN) == []
        (message,) = Archive(projects.control).messages("p", "s", run=RUN)
        assert message.entry.call_id == call.id
        record = journal.record("p", message.message_id)
        assert record["interrupted_observation"] is True
        assert record["arguments"] == call.arguments
        assert "execution" not in record
        assert "report" not in record
    assert not worker.lookups
