# SPDX-License-Identifier: MIT
"""Durable completion is independent from deferred or cancelled worker cleanup."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.continuity.child_settlement import ChildSettlement
from sandbox_science.orchestrator.finalization import AttemptFinalizer, record_and_acknowledge
from sandbox_science.orchestrator.harness import Finished
from sandbox_science.orchestrator.hooks import TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .test_child_settlement import tree
from .test_tool_journal import RUN
from .test_work_ownership import CHILD, record


def completed(delegation: str, hooks: tuple[TurnHook, ...] = ()) -> Finished:
    final = record(CHILD).model_copy(update={"delegation": delegation, "role": "delegated"})
    return Finished(
        final.run_id,
        final.context,
        final.task,
        final.started_at,
        Transcript(messages=[], stopped="answered", final_text="Checked original evidence."),
        hooks=hooks,
        parent=RUN,
        delegation=delegation,
    )


@pytest.mark.parametrize("cleanup", ["success", "unavailable", "timeout", "cancelled"])
async def test_settlement_precedes_cleanup_and_cleanup_cannot_change_durable_result(
    tmp_path, cleanup
):
    state, documents, works, records, settlement, child, outcome = tree(
        tmp_path, saved=False, finished=False, checkpoint=False
    )
    closed: list[str] = []

    class Worker:
        async def close_session(self, context, *, run):
            # Cleanup must observe all receipts, not just the saved final record.
            assert works.get("p", "s", CHILD).state == "completed"
            assert settlement.page("p", "s") == ()
            assert records.get("p", "s", child.delegation_id).ended == "returned"
            assert len(settlement.ledger.work("p", "C-1").records) == 1
            closed.append(run)
            if cleanup == "unavailable":
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "fixture worker unavailable")
            if cleanup == "timeout":
                await asyncio.Event().wait()
            if cleanup == "cancelled":
                raise asyncio.CancelledError

    finalizer = AttemptFinalizer(
        state.projects, documents, cast(WorkerClient, Worker()), close_timeout=0.01
    )
    try:
        if cleanup == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await finalizer.finish(
                    completed(child.delegation_id), end=outcome, error=pytest.fail
                )
        else:
            result = await finalizer.finish(
                completed(child.delegation_id), end=outcome, error=pytest.fail
            )
            assert result.record.confirmed
            assert result.record.status == 0
            assert result.child == outcome
            assert result.cleanup == ("released" if cleanup == "success" else "deferred")
        assert closed == ["child-1"]
        assert await settlement.settle("p", "s", CHILD) == outcome
        assert len(settlement.ledger.work("p", "C-1").records) == 1
    finally:
        documents.close()
        state.projects.close()


@pytest.mark.parametrize("failure", ["save", "acknowledgement", "settlement"])
async def test_durable_failures_preserve_evidence_and_retain_kernels(
    tmp_path, monkeypatch, failure
):
    state, documents, _, records, settlement, child, outcome = tree(
        tmp_path, saved=False, finished=False, checkpoint=False
    )
    acknowledged: list[str] = []

    class Delivery(TurnHook):
        def __init__(self, name):
            self.name = name

        async def acknowledge(self, record):
            assert state.projects.run("p", "s", CHILD).run_id == record.run_id
            acknowledged.append(self.name)
            if failure == "acknowledgement" and self.name == "first":
                raise OSError("fixture acknowledgement unavailable")

    class Worker:
        async def close_session(self, context, *, run):
            pytest.fail("unconfirmed work must retain its kernels")

    def cannot_save(record):
        raise ProjectError(ErrorKind.UNAVAILABLE, "fixture disk unavailable")

    async def cannot_settle(self, project, session, run):
        raise OSError("fixture ledger unavailable")

    if failure == "save":
        monkeypatch.setattr(state.projects, "save_run", cannot_save)
    if failure == "settlement":
        monkeypatch.setattr(ChildSettlement, "settle", cannot_settle)
    finalizer = AttemptFinalizer(
        state.projects, documents, cast(WorkerClient, Worker()), close_timeout=0.01
    )
    errors: list[str] = []
    try:
        finished = completed(child.delegation_id, (Delivery("first"), Delivery("second")))
        if failure == "settlement":
            with pytest.raises(OSError, match="fixture ledger unavailable"):
                await finalizer.finish(finished, end=outcome, error=errors.append)
            assert state.projects.run("p", "s", CHILD).final_text == outcome.final_text
            assert settlement.page("p", "s") == (CHILD,)
            assert records.get("p", "s", child.delegation_id).ended is None
        else:
            result = await finalizer.finish(finished, end=outcome, error=errors.append)
            assert result.record.status == 1
            assert not result.record.confirmed
            assert result.record.recorded == (failure != "save")
            assert not result.record.acknowledged
            assert result.record.receipted
            assert result.child is not None
            assert result.child.stopped == "persistence_error"
            assert result.child.result is None
            assert result.child.final_text == outcome.final_text
            assert result.cleanup == "retained"
            assert len(errors) == 1
        assert acknowledged == ([] if failure == "save" else ["first", "second"])
    finally:
        documents.close()
        state.projects.close()


async def test_model_failure_status_does_not_imply_failed_persistence(tmp_path):
    state, documents, _, _, _, child, _ = tree(tmp_path, saved=False, finished=False)
    failed = record(CHILD, stopped="error").model_copy(
        update={
            "failure": "model",
            "parent": RUN,
            "delegation": child.delegation_id,
            "role": "delegated",
            "final_text": None,
            "answer": None,
        }
    )
    try:
        result = await record_and_acknowledge(
            failed.model_dump(mode="json"),
            1,
            (),
            projects=state.projects,
            error=pytest.fail,
        )
        assert result.status == 1
        assert result.confirmed
        assert state.projects.run("p", "s", CHILD).failure == "model"
    finally:
        documents.close()
        state.projects.close()


async def test_cancellation_during_acknowledgement_keeps_saved_evidence_unconfirmed(tmp_path):
    state, documents, works, records, settlement, child, outcome = tree(
        tmp_path, saved=False, finished=False, checkpoint=False
    )

    class Interrupted(TurnHook):
        name = "interrupted"

        async def acknowledge(self, record):
            raise asyncio.CancelledError

    class Worker:
        async def close_session(self, context, *, run):
            pytest.fail("cancelled acknowledgements must not release kernels")

    finalizer = AttemptFinalizer(
        state.projects, documents, cast(WorkerClient, Worker()), close_timeout=0.01
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await finalizer.finish(
                completed(child.delegation_id, (Interrupted(),)), end=outcome, error=pytest.fail
            )
        assert state.projects.run("p", "s", CHILD).final_text == outcome.final_text
        assert works.get("p", "s", CHILD).state == "running"
        assert records.get("p", "s", child.delegation_id).ended is None
        await settlement.drain("p", "s")
        kept = records.get("p", "s", child.delegation_id)
        assert kept.stopped == "persistence_error"
        assert kept.result is None
    finally:
        documents.close()
        state.projects.close()


async def test_transcript_write_failure_is_distinct_from_saved_and_acknowledged_record(tmp_path):
    state, documents, _, _, _, child, _ = tree(tmp_path, saved=False, finished=False)
    finished = completed(child.delegation_id)
    errors: list[str] = []
    try:
        result = await record_and_acknowledge(
            finished.fields(),
            0,
            (),
            projects=state.projects,
            transcript=tmp_path,  # a directory cannot be written as a transcript file
            error=errors.append,
        )
        assert result.status == 1
        assert result.transcript_written is False
        assert result.recorded
        assert result.acknowledged
        assert result.receipted
        assert not result.confirmed
        assert state.projects.run("p", "s", CHILD).final_text == finished.transcript.final_text
        assert len(errors) == 1
        assert "transcript could not be written" in errors[0]
    finally:
        documents.close()
        state.projects.close()
