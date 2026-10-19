# SPDX-License-Identifier: MIT
"""A resumed tree has one durable owner; completed branches never run again."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Literal

import pytest

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords, authority
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.usage import AnsweredUsage
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord, run_id_of
from sandbox_science.orchestrator.runstate import RunState

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects

CHILD = "20261016T030001000000Z-child-1"
GRANDCHILD = "20261016T030002000000Z-grandchild-1"
CONTINUATION = "20261017T030000000000Z-run-2"


def record(name: str, *, stopped: str = "answered") -> RunRecord:
    run_id = run_id_of(name)
    assert run_id is not None
    return RunRecord(
        run_id=run_id,
        context=CONTEXT,
        task="Fixture",
        started_at=datetime.strptime(name.partition("-")[0], "%Y%m%dT%H%M%S%fZ").replace(
            tzinfo=UTC
        ),
        ended_at=datetime.now(UTC),
        stopped=stopped,
        final_text="Evidence retained",
        messages=[],
        tools=[],
        usage=[],
    )


def test_attempts_and_pending_resume_survive_reopen_and_are_idempotent(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    works.admit("p", "s", GRANDCHILD, parent=CHILD)
    works.finish(record(GRANDCHILD))
    works.finish(record(CHILD, stopped="turn_limit"))
    works.finish(record(RUN))
    assert works.get("p", "s", RUN).state == "paused"
    assert works.get("p", "s", RUN).reason == "unfinished_children"
    request = works.request("p", "s")
    assert request is not None
    assert works.resume_source("p", "s", request) == "operator"
    assert works.request("p", "s") == request
    projects.control.close()
    assert works.queued() == [("p", "s", request, RUN)]
    with pytest.raises(ControlError, match="session run lease"):
        works.claim("p", "s", request, lease_run=CONTINUATION)
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    root, epoch = works.claim("p", "s", request, lease_run=CONTINUATION)
    assert root == RUN
    assert epoch == owner.epoch + 1
    assert works.get("p", "s", GRANDCHILD).state == "completed"
    assert works.get("p", "s", CHILD).state == "waiting"
    with pytest.raises(ControlError, match="no longer queued"):
        works.claim("p", "s", request, lease_run=CONTINUATION)
    next_owner = works.admit("p", "s", CONTINUATION, work=RUN, predecessor=RUN, epoch=epoch)
    assert works.owner("p", "s", RUN) == owner
    assert works.owner("p", "s", CONTINUATION) == next_owner
    assert works.attempts("p", "s", RUN) == [RUN, CONTINUATION]
    with pytest.raises(ControlError, match="not claimed"):
        works.admit("p", "s", CONTINUATION + "x", work=RUN, predecessor=RUN, epoch=epoch)


def test_resume_claim_atomically_recovers_only_its_unconsumed_input(projects: ProjectStore):
    works, state = WorkRecords(projects.control), RunState(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    works.admit("p", "s", GRANDCHILD, parent=RUN)
    other = "20261016T040000000000Z-other-root"
    ids = {RUN: ["own", "consumed"], CHILD: ["done"], GRANDCHILD: ["stopped"]}
    for run, messages in ids.items():
        state.acquire("p", "s", run, "fixture")
        for message in messages:
            state.post("p", "s", message, "Original input", author="user")
        state.offer("p", "s", messages, run)
        state.release("p", "s", "fixture")
    works.finish(record(CHILD))
    works.stop("p", "s", GRANDCHILD)
    works.interrupt("p", "s", RUN)
    works.admit("p", "s", other)
    state.acquire("p", "s", other, "fixture")
    state.post("p", "s", "other", "Unrelated input", author="user")
    state.offer("p", "s", ["other"], other)
    state.release("p", "s", "fixture")
    works.interrupt("p", "s", other)
    state.hold("p", "s", ["own", "consumed", "done", "stopped", "other"])
    Archive(projects.control).append(
        "p",
        "s",
        RUN,
        Entry("assistant", "assistant", "Already incorporated"),
        model_usage=AnsweredUsage(
            ordinal=1, observed_at=datetime.now(UTC), usage=ModelUsage(cost=0)
        ),
        consumed={"messages": {"consumed"}},
    )
    request = works.request("p", "s", RUN)
    assert request is not None
    projects.control.close()  # operator disappeared after saving only the request
    before = works.get("p", "s", RUN).epoch
    state.acquire("p", "s", CONTINUATION, "fixture")
    with projects.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER fail_resume_input BEFORE UPDATE ON session_messages "
            "WHEN NEW.state='queued' BEGIN SELECT RAISE(ABORT,'input admission failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="input admission failed"):
        works.claim("p", "s", request, lease_run=CONTINUATION)
    assert works.get("p", "s", RUN).epoch == before
    assert works.queued() == [("p", "s", request, RUN)]
    assert all(message.state == "held" for message in state.messages("p", "s"))
    with projects.control.write() as tx:
        tx.sql.execute("DROP TRIGGER fail_resume_input")
    works.claim("p", "s", request, lease_run=CONTINUATION)
    assert {m.message_id: m.state for m in state.messages("p", "s")} == {
        "own": "queued",
        "consumed": "held",
        "done": "held",
        "stopped": "held",
        "other": "held",
    }


async def test_delayed_thread_mutations_are_fenced_inside_transaction(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    entered, release = asyncio.Event(), asyncio.Event()

    async def old_attempt():
        with authority(owner):
            entered.set()
            await release.wait()
            await threaded(works.block, "p", "s", RUN, "stale write")

    old = asyncio.create_task(old_attempt())
    await entered.wait()
    works.interrupt("p", "s", RUN)
    request = works.request("p", "s")
    assert request is not None
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    works.claim("p", "s", request, lease_run=CONTINUATION)
    release.set()
    with pytest.raises(ControlError, match="lost ownership"):
        await old
    assert works.get("p", "s", RUN).state == "waiting"


def test_stop_persists_for_subtree_and_parent_completion_waits(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    works.admit("p", "s", GRANDCHILD, parent=CHILD)
    works.stop("p", "s", CHILD)
    works.finish(record(GRANDCHILD, stopped="interrupted"))
    assert works.get("p", "s", GRANDCHILD).state == "stopped"
    works.finish(record(RUN))
    assert works.get("p", "s", RUN).state == "completed"
    assert works.request("p", "s") is None
    with pytest.raises(ControlError, match="completed or explicitly stopped"):
        works.request("p", "s", RUN)


def test_failed_final_save_never_completes_work(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.finish(record(RUN), persisted=False)
    assert works.get("p", "s", RUN).state == "paused"
    assert works.get("p", "s", RUN).reason == "persistence_error"


async def test_saved_model_failure_is_not_a_persistence_failure(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    identity = ModelIntents(projects.control).begin(owner, kind="main", alias="fixture")
    failed = record(RUN, stopped="error").model_copy(
        update={"failure": "model", "final_text": None, "answer": None}
    )
    errors: list[str] = []

    assert (
        await save(failed.model_dump(mode="json"), 1, (), projects=projects, error=errors.append)
        == 1
    )
    assert errors == []
    assert projects.run("p", "s", RUN).failure == "model"
    assert works.get("p", "s", RUN).state == "paused"
    assert works.get("p", "s", RUN).reason == "error"
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)


async def test_first_model_call_checks_ownership_after_start_notices(projects: ProjectStore):
    from sandbox_science.orchestrator.continuity.hook import WorkHook
    from sandbox_science.orchestrator.hooks import TurnHook
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel

    from .test_tool_journal import toolbox

    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    calls = []

    class Supersede(TurnHook):
        async def notices(self, point, run):
            if point == "start":
                works.admit("p", "s", CONTINUATION)
            return ()

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            calls.append(True)
            return await super().complete(messages, tools)

    result = await run_task(
        Model([ModelTurn("must not dispatch")]),
        toolbox(None),
        "Research",
        run_id="run-1",
        hooks=(WorkHook(works, owner), Supersede()),
    )
    assert not calls
    assert result.stopped == "error"


@pytest.mark.parametrize(
    ("source", "expected", "excluded"),
    [
        ("operator", "operator explicitly requested", "Automatic recovery created"),
        ("automatic", "Automatic recovery created", "operator explicitly requested"),
    ],
)
async def test_resume_attempt_source_is_explicitly_shown_once(
    projects: ProjectStore,
    source: Literal["operator", "automatic"],
    expected: str,
    excluded: str,
):
    from types import SimpleNamespace
    from typing import cast

    from sandbox_science.orchestrator.continuity.hook import WorkHook
    from sandbox_science.orchestrator.hooks import RunView

    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    hook = WorkHook(works, owner, predecessor="previous-attempt", resume_source=source)
    view = cast(RunView, SimpleNamespace(offered={}))

    (notice,) = await hook.notices("turn", view)
    assert notice.source == "continuity"
    assert notice.ids == ("previous-attempt",)
    assert "new attempt continuing the same durable research work" in notice.framing
    assert expected in notice.framing
    assert excluded not in notice.framing
    assert notice.payload == {
        "work": owner.work,
        "attempt": owner.run,
        "predecessor": "previous-attempt",
        "epoch": owner.epoch,
        "resume_source": source,
    }
    already_shown = cast(RunView, SimpleNamespace(offered={"continuity": notice.ids}))
    assert await hook.notices("turn", already_shown) == ()


def test_completed_child_outcome_must_be_consumed_before_parent_completion(projects: ProjectStore):
    from sandbox_science.orchestrator.delegation.records import (
        Delegation,
        DelegationRecords,
        Ending,
    )
    from sandbox_science.orchestrator.history import Archive, Entry
    from sandbox_science.orchestrator.history.receipts import save_receipts

    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    delegations = DelegationRecords(projects.control)
    delegations.start(
        "p",
        "s",
        Delegation(
            delegation_id="dlg-aaaaaaaaaaaaaaaaaaaaaaaa",
            parent=RUN,
            run=CHILD,
            kind="task",
            brief={"task": "Compute"},
            started_at=datetime.now(UTC),
        ),
    )
    works.finish(record(CHILD))
    delegations.end(
        "p", "s", "dlg-aaaaaaaaaaaaaaaaaaaaaaaa", Ending("returned", "answered", "Done")
    )
    assert works.unfinished("p", "s", RUN)
    # A later unrelated root's acknowledgement does not consume this work's outcome.
    works.admit("p", "s", CONTINUATION)
    delegations.acknowledge("p", "s", ["dlg-aaaaaaaaaaaaaaaaaaaaaaaa"], CONTINUATION)
    assert works.unfinished("p", "s", RUN)
    response = Archive(projects.control).append(
        "p", "s", RUN, Entry(role="assistant", kind="assistant", content="Consumed")
    )
    with projects.control.write() as tx:
        save_receipts(
            tx.sql,
            ("p", "s", RUN),
            response.message_id,
            {"delegations": {"dlg-aaaaaaaaaaaaaaaaaaaaaaaa"}},
        )
    assert not works.unfinished("p", "s", RUN)
    works.finish(record(RUN))
    assert works.get("p", "s", RUN).state == "completed"
