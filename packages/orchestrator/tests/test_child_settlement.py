# SPDX-License-Identifier: MIT
"""Replay exact typed handbacks after final-save and ledger crash windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.continuity.child_settlement import ChildSettlement
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.hook import ChildEnd
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.runstate import RunState

from .test_run_harness import stores
from .test_tool_journal import RUN
from .test_work_ownership import CHILD, CONTINUATION, record


def tree(tmp_path, *, saved=True, finished=True, persisted=True, checkpoint=True):
    state, documents = stores(tmp_path)
    state.projects.open_session("p", "s", dedicated=False)
    works = WorkRecords(state.projects.control)
    works.admit("p", "s", RUN)
    state.projects.save_run(record(RUN))
    works.admit("p", "s", CHILD, parent=RUN)
    ledger = Ledger(documents)
    entry = ledger.change(
        "p",
        {
            "operation_id": "claim",
            "reason": "fixture",
            "changes": [{"type": "claim", "origin": "project", "title": "C", "statement": "S."}],
        },
        USER,
    ).entries[0]
    delegation = Delegation(
        delegation_id=new_id("dlg"),
        parent=RUN,
        run=CHILD,
        kind="check",
        brief={"check": {"entry": entry.entry_id, "handle": "C-1", "revision": 1}},
        started_at=datetime.now(UTC),
    )
    records = DelegationRecords(state.projects.control)
    records.start("p", "s", delegation)
    settlement = ChildSettlement(state.projects.control, ledger)
    outcome = ChildEnd(
        "answered",
        False,
        "Checked original evidence.",
        result={"verdict": "confirmed", "status": "complete", "summary": "Original typed answer"},
    )
    if checkpoint:
        settlement.keep("p", "s", CHILD, delegation.delegation_id, outcome)
    child_record = record(CHILD).model_copy(
        update={"parent": RUN, "delegation": delegation.delegation_id, "role": "delegated"}
    )
    if saved:
        state.projects.save_run(child_record)
    if finished:
        works.finish(child_record, persisted=persisted)
    return state, documents, works, records, settlement, delegation, outcome


async def test_saved_first_child_replays_before_abandon_without_inventing_outcome(tmp_path):
    state, documents, works, records, settlement, child, outcome = tree(tmp_path)
    try:
        assert works.get("p", "s", CHILD).state == "completed"
        assert records.abandon("p", "s", parent=RUN) == []
        assert records.get("p", "s", child.delegation_id).ended is None
        state.projects.control.close()  # durable checkpoint, not an in-memory callback
        await settlement.drain("p", "s")
        kept = records.get("p", "s", child.delegation_id)
        assert kept.ended == "returned"
        assert kept.result == outcome.result
        assert kept.run == CHILD
        assert kept.parent == RUN
        assert settlement.page("p", "s") == ()
        await settlement.settle("p", "s", CHILD)
        assert len(settlement.ledger.work("p", "C-1").records) == 1
    finally:
        documents.close()
        state.projects.close()


async def test_parent_stays_blocked_between_handback_and_ledger_commit(tmp_path, monkeypatch):
    state, documents, works, records, settlement, child, _ = tree(tmp_path)
    try:
        works.interrupt("p", "s", RUN)
        request = works.request("p", "s")
        assert request is not None
        RunState(state.projects.control).acquire("p", "s", CONTINUATION, "fixture")
        root, _ = works.claim("p", "s", request, lease_run=CONTINUATION)
        plans = Plans(state.projects.control, "p", "s", request)
        plans.prepare(root, run=CONTINUATION, started=datetime.now(UTC))
        assert plans.ready() == ()
        change = settlement.ledger.change

        def crash(*args, **kwargs):
            change(*args, **kwargs)
            raise OSError("crash after ledger commit, before settlement receipt")

        monkeypatch.setattr(settlement.ledger, "change", crash)
        with pytest.raises(OSError, match="crash after ledger"):
            await settlement.settle("p", "s", CHILD)
        assert records.get("p", "s", child.delegation_id).ended == "returned"
        assert plans.ready() == ()
        monkeypatch.setattr(settlement.ledger, "change", change)
        await settlement.drain("p", "s")
        assert plans.ready() == (RUN,)
        assert len(settlement.ledger.work("p", "C-1").records) == 1
    finally:
        documents.close()
        state.projects.close()


@pytest.mark.parametrize(("finished", "persisted"), [(False, True), (True, False)])
async def test_unconfirmed_save_never_advertises_checkpoint_as_returned(
    tmp_path, finished, persisted
):
    state, documents, _, records, settlement, child, _ = tree(
        tmp_path, finished=finished, persisted=persisted
    )
    try:
        await settlement.drain("p", "s")
        kept = records.get("p", "s", child.delegation_id)
        assert kept.ended == "failed"
        assert kept.stopped == "persistence_error"
        assert kept.result is None
        assert kept.report == "Checked original evidence."
        assert any("not confirmed" in error for error in kept.errors)
    finally:
        documents.close()
        state.projects.close()


async def test_saved_model_failure_settles_as_model_error_not_persistence_error(tmp_path):
    state, documents = stores(tmp_path)
    state.projects.open_session("p", "s", dedicated=False)
    works = WorkRecords(state.projects.control)
    works.admit("p", "s", RUN)
    state.projects.save_run(record(RUN))
    works.admit("p", "s", CHILD, parent=RUN)
    delegation = Delegation(
        delegation_id=new_id("dlg"),
        parent=RUN,
        run=CHILD,
        kind="task",
        brief={"task": "Inspect evidence"},
        started_at=datetime.now(UTC),
    )
    records = DelegationRecords(state.projects.control)
    records.start("p", "s", delegation)
    settlement = ChildSettlement(state.projects.control, Ledger(documents))
    settlement.keep("p", "s", CHILD, delegation.delegation_id, ChildEnd("error", True, None))
    failed = record(CHILD, stopped="error").model_copy(
        update={
            "role": "delegated",
            "parent": RUN,
            "delegation": delegation.delegation_id,
            "failure": "model",
            "final_text": None,
            "answer": None,
        }
    )
    errors: list[str] = []
    try:
        assert (
            await save(
                failed.model_dump(mode="json"),
                1,
                (),
                projects=state.projects,
                error=errors.append,
            )
            == 1
        )
        assert errors == []
        assert works.get("p", "s", CHILD).reason == "error"
        end = await settlement.settle("p", "s", CHILD)
        assert end.stopped == "error"
        assert records.get("p", "s", delegation.delegation_id).stopped == "error"
    finally:
        documents.close()
        state.projects.close()


async def test_checkpoint_without_saved_run_is_not_eligible_and_cannot_return(tmp_path):
    state, documents, _, records, settlement, child, outcome = tree(
        tmp_path, saved=False, finished=False
    )
    try:
        assert settlement.page("p", "s") == ()
        with pytest.raises(ControlError, match="no saved final"):
            await settlement.settle("p", "s", CHILD)
        with pytest.raises(ControlError, match="another outcome"):
            settlement.keep(
                "p",
                "s",
                CHILD,
                child.delegation_id,
                ChildEnd("answered", False, "fabricated different report"),
            )
        settlement.keep("p", "s", CHILD, child.delegation_id, outcome)
        assert records.abandon("p", "s", parent=RUN) == [(child.delegation_id, CHILD)]
        assert records.get("p", "s", child.delegation_id).result is None
    finally:
        documents.close()
        state.projects.close()


async def test_checkpoint_scope_is_exact_and_unrelated_root_is_not_drained(tmp_path):
    state, documents, _, records, settlement, child, outcome = tree(tmp_path)
    try:
        with pytest.raises(ControlError, match="no original delegation"):
            settlement.keep("p", "other", CHILD, child.delegation_id, outcome)
        with pytest.raises(ControlError, match="no saved final"):
            await settlement.settle("p", "other", CHILD)
        await settlement.drain("p", "s", root=CONTINUATION)
        assert records.get("p", "s", child.delegation_id).ended is None
        await settlement.drain("p", "s", root=RUN)
        assert records.get("p", "s", child.delegation_id).ended == "returned"
    finally:
        documents.close()
        state.projects.close()


def test_pages_exclude_missing_final_records_before_limit_and_remain_bounded(tmp_path):
    from sandbox_science.orchestrator.delegation.records import Ending

    state, documents, works, records, settlement, _, outcome = tree(
        tmp_path, saved=False, finished=False
    )
    try:
        for index in range(202):
            run = f"20261017T030000000000Z-page-{index:04d}"
            works.admit("p", "s", run, parent=RUN)
            child = Delegation(
                delegation_id=new_id("dlg"),
                parent=RUN,
                run=run,
                kind="task",
                brief={"task": "Fixture"},
                started_at=datetime.now(UTC),
            )
            records.start("p", "s", child)
            records.end("p", "s", child.delegation_id, Ending("interrupted", "interrupted"))
            settlement.keep("p", "s", run, child.delegation_id, outcome)
            if index >= 101:
                state.projects.save_run(record(run))
                works.finish(record(run))
        first = settlement.page("p", "s")
        assert len(first) == 100
        assert first[0].endswith("0101")
        second = settlement.page("p", "s", after=first[-1])
        assert len(second) == 1
        assert second[0].endswith("0201")
    finally:
        documents.close()
        state.projects.close()


def test_schema64_upgrade_preserves_existing_run_and_archive(tmp_path, monkeypatch):
    from sandbox_science.orchestrator.context.history import Archive, inspector_index
    from sandbox_science.orchestrator.context.history import archive as archive_module
    from sandbox_science.orchestrator.context.history.archive import Entry
    from sandbox_science.orchestrator.control import migrations

    current = migrations.MIGRATIONS
    with monkeypatch.context() as previous:
        previous.setattr(
            migrations, "MIGRATIONS", current[: current.index(migrations._child_settlements)]
        )
        previous.setattr(migrations, "VERSION", 64)
        # Schema 64's archive writer had no inspector index. Keep its real
        # immutable append and old schema, omitting only that newer derived hook.
        previous.setattr(inspector_index, "append", lambda *args: None)
        # Nor had it a tool result's purpose (version 83): its writer's columns.
        fields = tuple(name for name in archive_module._FIELDS if name != "purpose")
        previous.setattr(archive_module, "_FIELDS", fields)
        previous.setattr(archive_module, "_COLUMNS", ", ".join((*archive_module._PLACE, *fields)))
        state, documents = stores(tmp_path)
        state.projects.open_session("p", "s", dedicated=False)
        state.projects.save_run(record(RUN))
        archived = Archive(state.projects.control).append(
            "p", "s", RUN, Entry("user", "notice", "Existing immutable evidence")
        )
        state.projects.control.close()
    try:
        state.projects.control.initialize()
        assert state.projects.run("p", "s", RUN).final_text == "Evidence retained"
        assert Archive(state.projects.control).message("p", archived.message_id).entry.content == (
            "Existing immutable evidence"
        )
        with state.projects.control.read() as tx:
            assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
            assert tx.sql.execute("SELECT count(*) FROM research_child_settlements").fetchone() == (
                0,
            )
    finally:
        documents.close()
        state.projects.close()


def test_handback_maps_reserved_parent_before_that_attempt_exists(tmp_path):
    from sandbox_science.orchestrator.delegation.records import Ending

    state, documents = stores(tmp_path)
    try:
        state.projects.open_session("p", "s", dedicated=False)
        works = WorkRecords(state.projects.control)
        works.admit("p", "s", RUN)
        works.interrupt("p", "s", RUN)
        request = works.request("p", "s")
        assert request is not None
        RunState(state.projects.control).acquire("p", "s", CONTINUATION, "fixture")
        root, _ = works.claim("p", "s", request, lease_run=CONTINUATION)
        Plans(state.projects.control, "p", "s", request).prepare(
            root, run=CONTINUATION, started=datetime.now(UTC)
        )
        assert works.owner("p", "s", CONTINUATION) is None
        child = Delegation(
            delegation_id=new_id("dlg"),
            parent=CONTINUATION,
            run=CHILD,
            kind="task",
            brief={"task": "A completed child of the reserved parent"},
            started_at=datetime.now(UTC),
        )
        records = DelegationRecords(state.projects.control)
        records.start("p", "s", child)
        records.end("p", "s", child.delegation_id, Ending("returned", "answered", "Original"))
        offered = records.handbacks("p", "s", work=RUN)
        assert [outcome.delegation_id for outcome in offered] == [child.delegation_id]
        assert records.handbacks("p", "s", work=CHILD) == []
        works.finish(record(RUN))
        assert works.get("p", "s", RUN).reason == "unfinished_children"
    finally:
        documents.close()
        state.projects.close()


async def test_parent_completion_requires_ledger_receipt_even_after_outcome_ack(
    tmp_path, monkeypatch
):
    state, documents, works, records, settlement, child, _ = tree(tmp_path)
    try:
        change = settlement.ledger.change

        def crash(*args, **kwargs):
            raise OSError("ledger unavailable")

        monkeypatch.setattr(settlement.ledger, "change", crash)
        with pytest.raises(OSError, match="ledger unavailable"):
            await settlement.settle("p", "s", CHILD)
        records.acknowledge("p", "s", [child.delegation_id], RUN, work=RUN)
        works.finish(record(RUN))
        assert works.get("p", "s", RUN).reason == "unfinished_children"
        monkeypatch.setattr(settlement.ledger, "change", change)
        await settlement.drain("p", "s")
        works.finish(record(RUN))
        assert works.get("p", "s", RUN).state == "completed"
    finally:
        documents.close()
        state.projects.close()


async def test_resume_releases_saved_terminal_child_kernel_before_planning(tmp_path):
    import asyncio
    from typing import cast

    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.contracts.environment_claims import EnvironmentClaimResult
    from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
    from sandbox_science.orchestrator.continuity.recovery import Recovery
    from sandbox_science.orchestrator.harness import Harness, Settings
    from sandbox_science.orchestrator.worker_client import WorkerClient
    from sandbox_science.testing import eventually

    from .test_run_harness import Sink, Worker, main

    state, documents, works, records, _, child, _ = tree(tmp_path)
    paused = "20261017T030001000000Z-paused"
    stopped = "20261017T030002000000Z-stopped"
    works.admit("p", "s", paused, parent=RUN)
    works.finish(record(paused, stopped="turn_limit"))
    works.admit("p", "s", stopped, parent=RUN)
    works.stop("p", "s", stopped)
    session = state.projects.get("p").sessions["s"]
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision=state.projects.get("p").policy,
    )
    owner = works.owner("p", "s", RUN)
    assert owner is not None
    Blueprints(state.projects.control).save(
        owner,
        Blueprint(
            context=context,
            request={
                "project": "p",
                "session": "s",
                "task": "Continue research",
                "alias": "scripted",
            },
        ),
    )
    works.interrupt("p", "s", RUN)
    request = works.request("p", "s")
    assert request is not None

    class ClosingWorker(Worker):
        def __init__(self):
            self.held = False
            self.closed = []

        async def claim_environment(self, request):
            self.held = request.seconds != 0
            return EnvironmentClaimResult(
                environment_id=request.context.environment_id,
                state="held" if self.held else "released",
                sandbox_id="sandbox-fixture",
                expires_at=datetime.now(UTC) + timedelta(seconds=60) if self.held else None,
            )

        async def close_session(self, context, *, run=None):
            assert self.held
            assert records.get("p", "s", child.delegation_id).ended == "returned"
            assert not recovery.started  # no branch may begin before reconciliation
            self.closed.append((context, run))

    worker = ClosingWorker()
    harness = Harness(
        cast(WorkerClient, worker),
        main("unused"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    recovery = Recovery(harness, "p", "s", request)
    task = asyncio.create_task(recovery.run(Sink(), asyncio.Event()))
    try:
        await eventually(lambda: recovery.started or task.done())
        assert not task.done(), task.exception() if task.done() else None
        assert [run for _, run in worker.closed] == ["child-1", "stopped"]
        assert all(ctx.authority.work == RUN for ctx, _ in worker.closed)
        assert all(ctx.authority.run == recovery.run_name for ctx, _ in worker.closed)
        assert all(ctx.authority.epoch == recovery.epoch for ctx, _ in worker.closed)
        assert all(ctx.authority.epoch > owner.epoch for ctx, _ in worker.closed)
        assert "paused" not in [run for _, run in worker.closed]
        assert "run-1" not in [run for _, run in worker.closed]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        documents.close()
        state.projects.close()
