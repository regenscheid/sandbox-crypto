# SPDX-License-Identifier: MIT
"""Recovery observes original effects and kernel identity; it never dispatches them."""

import json
import time
from datetime import UTC, datetime
from typing import cast

import pytest

from sandbox_science.contracts import Execution, KernelInfo, KernelObservation, Result
from sandbox_science.contracts.recovery import OperationLookup
from sandbox_science.contracts.worker import ExecutionState
from sandbox_science.orchestrator.continuity.kernels import KernelJournal, _pure_python_assignments
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.reconcile import Reconciler
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resolutions import RecoveryControls, Resolution
from sandbox_science.orchestrator.continuity.status import RecoveryStatus
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.operations import ToolJournal, ToolRecord
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


async def test_unknown_model_charge_is_visible_but_does_not_block_reconciliation(
    projects: ProjectStore,
):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    identity = ModelIntents(projects.control).begin(owner, kind="main", alias="fixture")
    works.interrupt("p", "s", RUN)
    worker = PassiveWorker(None)
    await Reconciler(projects.control, cast(WorkerClient, worker)).work(works.get("p", "s", RUN))
    assert worker.lookups == []
    assert RecoveryStatus(projects.control).pending("p", "s", RUN).models[0].identity == identity


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


@pytest.mark.parametrize("wrong_operation", [False, True])
async def test_replayable_kernel_cells_rebuild_once_without_model_call(
    projects: ProjectStore,
    wrong_operation: bool,
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    original = KernelInfo(
        kernel_id="kernel-original",
        language="python",
        generation=1,
        start_revision=1,
        current_revision=1,
        revision_status="current",
    )
    replacement = original.model_copy(update={"kernel_id": "kernel-rebuilt", "generation": 2})

    def observed(*kernels: KernelInfo) -> KernelObservation:
        return KernelObservation(
            environment_id=CONTEXT.environment_id,
            session_id="s",
            current_revision=1,
            observed_at=datetime.now(UTC),
            kernels=kernels,
        )

    first = Execution(
        execution_id="cell-original",
        attempt_id="attempt-original",
        operation_id="op-original",
        kind="cell",
        context=CONTEXT,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime.now(UTC),
        result=Result(status="ok", execution_count=1, kernel=original),
    )
    await journal.record_tool(
        {
            "tool": "python",
            "arguments": '{"code":"x = 41","replayable":true}',
            "execution": first.model_dump(mode="json"),
            "kernel_replay_binding_free": True,
        },
        observed(original),
    )
    # A passive empty observation on the next turn must retain the last known
    # identity so Resume can recognize that state was lost.
    await journal.record(observed())

    class Rebuilding:
        def __init__(self) -> None:
            self.kernel: KernelInfo | None = None
            self.calls = 0

        async def observe_kernels(self, context: object) -> KernelObservation:
            assert context == CONTEXT
            return observed(*(() if self.kernel is None else (self.kernel,)))

        async def restart_kernel(
            self, context: object, language: str, run: str | None, *, only_if_absent: bool
        ):
            assert (context, language, run) == (CONTEXT, "python", None)
            assert only_if_absent is True
            self.kernel = replacement
            return replacement

        async def run(self, request):
            assert request.code == "x = 41"
            assert request.run is None
            self.calls += 1
            return first.model_copy(
                update={
                    "operation_id": "stale-operation" if wrong_operation else request.operation_id,
                    "result": Result(status="ok", execution_count=1, kernel=replacement),
                }
            )

    worker = Rebuilding()
    if wrong_operation:
        with pytest.raises(ControlError, match="did not rebuild exact state"):
            await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
        assert worker.calls == 1
        with pytest.raises(ControlError, match="partial reconstruction needs operator review"):
            await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
        assert worker.calls == 1
        return
    await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    assert worker.calls == 1
    await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    assert worker.calls == 1
    # A later loss has complete provenance for the replacement generation too.
    worker.kernel = None
    assert journal._replay_cells("python", replacement.kernel_id, replacement.generation)


async def test_archived_cell_without_kernel_journal_blocks_prefix_replay(
    projects: ProjectStore,
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    kernel = KernelInfo(
        kernel_id="kernel-original",
        language="python",
        generation=1,
        current_revision=1,
    )
    observed = KernelObservation(
        environment_id=CONTEXT.environment_id,
        session_id="s",
        current_revision=1,
        observed_at=datetime.now(UTC),
        kernels=(kernel,),
    )
    first = Execution(
        execution_id="cell-first",
        attempt_id="attempt-first",
        operation_id="op-first",
        kind="cell",
        context=CONTEXT,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime.now(UTC),
        result=Result(status="ok", execution_count=1, kernel=kernel),
    )
    await journal.record_tool(
        {
            "tool": "python",
            "arguments": '{"code":"x = 41","replayable":true}',
            "execution": first.model_dump(mode="json"),
            "kernel_replay_binding_free": True,
        },
        observed,
    )
    call = ToolCall("second-call", "python", '{"code":"y = 42","replayable":true}')
    ToolJournal(projects.control).start(CONTEXT, RUN, "op-second", call)
    second = first.model_copy(
        update={
            "operation_id": "op-second",
            "result": Result(status="ok", execution_count=2, kernel=kernel),
        }
    )
    Archive(projects.control).append(
        "p",
        "s",
        RUN,
        Entry("tool", "tool_result", "second cell completed", call_id=call.id),
        tool_record=ToolRecord(
            {
                "tool": "python",
                "arguments": call.arguments,
                "execution": second.model_dump(mode="json"),
            },
            "op-second",
        ),
    )
    # Simulates a process crash after the archived result commit and before the
    # kernel hook wrote its second cell row.
    assert journal._replay_cells("python", kernel.kernel_id, kernel.generation) is None


async def test_new_main_work_inherits_lost_session_kernel_before_first_observation(
    projects: ProjectStore,
) -> None:
    works = WorkRecords(projects.control)
    first_owner = works.admit("p", "s", RUN)
    first = KernelJournal(projects.control, first_owner, None)
    kernel = KernelInfo(
        kernel_id="shared-kernel", language="python", generation=1, current_revision=1
    )
    observed = KernelObservation(
        environment_id=CONTEXT.environment_id,
        session_id="s",
        current_revision=1,
        observed_at=datetime.now(UTC),
        kernels=(kernel,),
    )
    await first.record(observed)
    next_run = "20261018T010000000000Z-run-2"
    second_owner = works.admit("p", "s", next_run)
    second = KernelJournal(projects.control, second_owner, None)
    assert second._expected() == {("python", "shared-kernel", 1)}
    with projects.control.read() as tx:
        row = tx.sql.execute(
            "SELECT detail FROM research_observations WHERE project='p' AND session='s' "
            "AND work=? AND kind='kernels' AND identity='latest'",
            (next_run,),
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])[0]["kernel_id"] == "shared-kernel"


async def test_partial_kernel_restore_requires_acknowledgement_and_discard(
    projects: ProjectStore,
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    original = KernelInfo(
        kernel_id="shared-kernel", language="python", generation=1, current_revision=1
    )
    partial = original.model_copy(update={"generation": 2})

    def observed(*kernels: KernelInfo) -> KernelObservation:
        return KernelObservation(
            environment_id=CONTEXT.environment_id,
            session_id="s",
            current_revision=1,
            observed_at=datetime.now(UTC),
            kernels=kernels,
        )

    await journal.record(observed(original))
    snapshot_identity = RecoveryStatus(projects.control).pending("p", "s", RUN).kernel_identity
    assert snapshot_identity is not None
    identity = "d" * 64
    journal._begin_restore(
        identity,
        [
            {
                "code": "x = 41",
                "language": "python",
                "kernel_id": original.kernel_id,
                "generation": 1,
                "run": None,
            }
        ],
    )
    assert (
        RecoveryStatus(projects.control).pending("p", "s", RUN).pending_kernel_restore == identity
    )

    class Recomputing:
        def __init__(self) -> None:
            self.kernel: KernelInfo | None = partial

        async def observe_kernels(self, context: object) -> KernelObservation:
            assert context == CONTEXT
            return observed(*(() if self.kernel is None else (self.kernel,)))

        async def restart_kernel(
            self, context: object, language: str, run: str | None, *, only_if_absent: bool
        ) -> KernelInfo:
            pytest.fail("discarding acknowledged lost state must not restart a kernel")

    worker = Recomputing()
    with pytest.raises(ControlError, match="needs operator review"):
        await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    with projects.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO research_observations VALUES (?,?,?,?,?,?,?)",
            (
                "p",
                "s",
                RUN,
                "resolution:kernel_recompute",
                "decision",
                time.time() + 1,
                json.dumps({"resolution": {"identity": snapshot_identity}}),
            ),
        )
    with pytest.raises(ControlError, match="partial kernel is live"):
        await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    worker.kernel = None  # operator closed the partial process
    await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    assert RecoveryStatus(projects.control).pending("p", "s", RUN).pending_kernel_restore is None
    await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    # A later root inherits the empty snapshot, not the older lost identity.
    next_run = "20261018T020000000000Z-run-2"
    next_owner = WorkRecords(projects.control).admit("p", "s", next_run)
    next_journal = KernelJournal(projects.control, next_owner, None)
    assert next_journal._expected() == set()
    await next_journal.reconcile(cast(WorkerClient, worker), CONTEXT)


async def test_unmarked_kernel_cell_never_replays(projects: ProjectStore) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    kernel = KernelInfo(
        kernel_id="kernel-unsafe",
        language="sage",
        generation=1,
        start_revision=1,
        current_revision=1,
        revision_status="current",
    )
    execution = Execution(
        execution_id="cell-unsafe",
        attempt_id="attempt-unsafe",
        operation_id="op-unsafe",
        kind="cell",
        context=CONTEXT,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime.now(UTC),
        result=Result(status="ok", execution_count=1, kernel=kernel),
    )
    await journal.record_tool(
        {
            "tool": "sage",
            "arguments": '{"code":"danger()"}',
            "execution": execution.model_dump(mode="json"),
            "kernel_replay_binding_free": True,
        },
        KernelObservation(
            environment_id=CONTEXT.environment_id,
            session_id="s",
            current_revision=1,
            observed_at=datetime.now(UTC),
            kernels=(kernel,),
        ),
    )

    class Missing:
        async def observe_kernels(self, context):
            return KernelObservation(
                environment_id=CONTEXT.environment_id,
                session_id="s",
                observed_at=datetime.now(UTC),
                kernels=(),
            )

        async def restart_kernel(self, *args):
            pytest.fail("unmarked cell must not be replayed")

    with pytest.raises(ControlError, match="complete replayable cell provenance"):
        await journal.reconcile(cast(WorkerClient, Missing()), CONTEXT)


async def test_truncated_file_observation_is_not_replayable(projects: ProjectStore) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    kernel = KernelInfo(kernel_id="kernel-truncated", language="python", generation=1)
    execution = Execution(
        execution_id="cell-truncated",
        attempt_id="attempt-truncated",
        operation_id="op-truncated",
        kind="cell",
        context=CONTEXT,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime.now(UTC),
        result=Result(status="ok", execution_count=1, kernel=kernel, written_files_truncated=True),
    )
    await journal.record_tool(
        {
            "tool": "python",
            "arguments": '{"code":"x = 41","replayable":true}',
            "execution": execution.model_dump(mode="json"),
            "kernel_replay_binding_free": True,
        },
        KernelObservation(
            environment_id=CONTEXT.environment_id,
            session_id="s",
            observed_at=datetime.now(UTC),
            kernels=(kernel,),
        ),
    )
    with projects.control.read() as tx:
        (detail,) = tx.sql.execute(
            "SELECT detail FROM research_observations WHERE kind='kernel_cell' AND identity=?",
            ("op-truncated",),
        ).fetchone()
    assert json.loads(detail)["replayable"] is False


def test_kernel_restore_intent_fences_other_work(projects: ProjectStore) -> None:
    works = WorkRecords(projects.control)
    parent = works.admit("p", "s", RUN)
    first = KernelJournal(projects.control, parent, None)
    first._record(
        KernelObservation(
            environment_id=CONTEXT.environment_id,
            session_id="s",
            observed_at=datetime.now(UTC),
            kernels=(KernelInfo(kernel_id="first", language="python", generation=1),),
        )
    )
    first._begin_restore("a" * 64, [{"code": "x = 41"}])
    status = RecoveryStatus(projects.control).page("p", "s")
    assert (status.pending_kernel_restore_work, status.pending_kernel_restore_identity) == (
        RUN,
        "a" * 64,
    )
    new_root = "20261018T020000000000Z-run-2"
    with pytest.raises(ControlError, match="unresolved reconstruction") as refused:
        works.admit("p", "s", new_root)
    assert refused.value.details == {"work": RUN, "identity": "a" * 64}
    with projects.control.read() as tx:
        assert (
            tx.sql.execute(
                "SELECT 1 FROM research_work WHERE project='p' AND session='s' AND work=?",
                (new_root,),
            ).fetchone()
            is None
        )
    child = works.admit("p", "s", "20261018T000000000000Z-child", parent=RUN)
    second = KernelJournal(projects.control, child, None)
    with pytest.raises(ControlError, match="prior reconstruction intent"):
        second._begin_restore("a" * 64, [{"code": "x = 41"}])
    first._abandon_restore(RUN, "a" * 64)
    assert RecoveryStatus(projects.control).page("p", "s").pending_kernel_restore_work is None
    assert works.admit("p", "s", new_root).work == new_root


@pytest.mark.parametrize(
    "code",
    [
        "requests.post('https://example.org', data='x')",
        "x = random.random()",
        "os.system('touch marker')",
        "x = 2 ** 100000",
        "x = 41\nprint(x)",
        "x = [1, 2, 3]",
    ],
)
def test_automatic_kernel_replay_refuses_effectful_or_unbounded_code(code: str) -> None:
    assert _pure_python_assignments(code) is False


def test_automatic_kernel_replay_accepts_literal_state() -> None:
    assert _pure_python_assignments("x = 41\nlabel = 'known'\nready = True") is True


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


@pytest.mark.parametrize(
    "tool",
    [
        "wait_delegations",
        "delegation_status",
        "wait_agent_message",
        "wait_jobs",
        "cancel_delegation",
    ],
)
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


async def test_the_users_resume_accepts_a_kernel_it_cannot_rebuild(projects: ProjectStore):
    # A relaunch restarted the sandbox and its kernel with it; the cells were not
    # replayable, so every Resume stopped there (Mac, 2026-10-21). The user's own Resume
    # goes on with the kernel reported lost; nothing is replayed.
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    saved = KernelObservation(
        environment_id=CONTEXT.environment_id,
        session_id="s",
        observed_at=datetime.now(UTC),
        kernels=(KernelInfo(kernel_id="lost-kernel", language="sage", generation=3),),
    )
    await journal.record(saved)

    class Restarted:
        async def observe_kernels(self, context):
            return saved.model_copy(update={"kernels": ()})

        async def restart_kernel(self, *args, **kwargs):
            pytest.fail("an accepted loss is not rebuilt")

    with pytest.raises(ControlError, match="lost_kernel"):
        await journal.reconcile(cast(WorkerClient, Restarted()), CONTEXT)
    await journal.reconcile(cast(WorkerClient, Restarted()), CONTEXT, accept_loss=True)
    with projects.control.read() as tx:
        detail = tx.sql.execute(
            "SELECT detail FROM research_observations WHERE kind='kernel_loss_accepted'"
        ).fetchone()[0]
    assert json.loads(detail) == {
        "lost": [{"language": "sage", "kernel_id": "lost-kernel", "generation": 3}],
        "accepted_by": "user",
    }
    # Later resumes have no kernel to rebuild: the next cell starts a clean one.
    await journal.reconcile(cast(WorkerClient, Restarted()), CONTEXT)
