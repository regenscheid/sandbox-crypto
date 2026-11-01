# SPDX-License-Identifier: MIT
"""A Resume's continuation writes in its research work's write area (DA-19): the area
of the delegation the work was first admitted for, not one of its own, so a resumed
child edits the files it was working on; a sibling's area is still refused, and only
the work's current, running attempt holds the area."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from sandbox_science.contracts import (
    EditFileRequest,
    EditFileResult,
    ErrorKind,
    ExecutionContext,
    new_id,
    outside_write_area,
)
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.recovery import Recovery
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.harness import Harness, Role, RunRequest, Settings
from sandbox_science.orchestrator.model import ModelTurn, ToolCall
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .scripted import ScriptedModel
from .test_run_harness import Sink, main, stores
from .test_tool_journal import RUN
from .test_tree_recovery import RecoveryWorker, drive
from .test_work_ownership import CHILD

SIBLING = "dlg-" + "5" * 24


class Editing(RecoveryWorker):
    """Edits as the worker checks them: only below the request's write area or in
    ``shared/`` (the worker's own check, ``outside_write_area``)."""

    def __init__(self) -> None:
        super().__init__()
        self.edits: list[EditFileRequest] = []

    async def edit_file(self, request: EditFileRequest) -> EditFileResult:
        self.edits.append(request)
        refusal = outside_write_area(request.context, request.path)
        if refusal is not None:
            raise WorkerAPIError(ErrorKind.POLICY_DENIED, refusal)
        return EditFileResult(sha256="0" * 64, size=3, replacements=1)


def prepare(tmp_path: Path) -> tuple[Any, Any, WorkRecords, str]:
    """A root and one task child, both interrupted: the child's delegation is the one
    its work was first admitted for."""
    state, documents = stores(tmp_path)
    session = state.projects.open_session("p", "s", dedicated=False)
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision=state.projects.get("p").policy,
    )
    works = WorkRecords(state.projects.control)
    first = new_id("dlg")
    for run, parent in ((RUN, None), (CHILD, RUN)):
        owner = works.admit("p", "s", run, parent=parent)
        request = RunRequest(
            "p",
            "s",
            "Continue research",
            parent=parent,
            delegation=None if parent is None else first,
            tokens=100000 if parent else None,
            alias="scripted",
            memory_version=0,
            root=RUN if parent else None,
        )
        Blueprints(state.projects.control).save(
            owner,
            Blueprint(
                context=context,
                request=TypeAdapter(RunRequest).dump_python(
                    request, mode="json", exclude={"history", "stop_by"}
                ),
            ),
        )
    records = DelegationRecords(state.projects.control)
    records.start(
        "p",
        "s",
        Delegation(
            delegation_id=first,
            parent=RUN,
            run=CHILD,
            kind="task",
            brief={"task": "Continue research"},
            started_at=datetime.now(UTC),
        ),
    )
    records.end("p", "s", first, Ending("interrupted", "interrupted"))
    works.interrupt("p", "s", RUN)
    return state, documents, works, first


def edit(n: int, path: str) -> ToolCall:
    arguments = {"path": path, "old_string": "draft", "new_string": "final"}
    return ToolCall(f"edit-{n}", "edit_file", json.dumps(arguments))


async def test_a_resumed_child_edits_its_earlier_file_and_not_a_siblings(tmp_path: Path) -> None:
    state, documents, works, first = prepare(tmp_path)
    worker = Editing()
    harness = Harness(
        cast(WorkerClient, worker),
        main("Recovered root"),
        delegated=Role(
            ScriptedModel(
                [
                    ModelTurn("", (edit(1, f"work/{first}/notes.md"),)),
                    ModelTurn("", (edit(2, f"work/{SIBLING}/notes.md"),)),
                    ModelTurn(json.dumps({"status": "complete", "summary": "Edited."})),
                ]
            ),
            "scripted",
        ),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    try:
        sink = Sink()
        failed = await drive(Recovery(harness, "p", "s", request), sink, asyncio.Event())
        assert not failed, sink.errors
        # The continuation is a delegation of its own, for the same work.
        attempts = works.attempts("p", "s", CHILD)
        assert len(attempts) == 2
        records = DelegationRecords(state.projects.control)
        continuation = records.of_runs("p", "s", [attempts[1]])[attempts[1]]
        assert continuation.delegation_id != first
        # It wrote in the work's area: its earlier file is edited, a sibling's refused.
        mine, sibling = worker.edits
        assert mine.context.write_area == sibling.context.write_area == f"work/{first}"
        assert mine.path == f"work/{first}/notes.md"
        assert outside_write_area(mine.context, mine.path) is None
        assert outside_write_area(sibling.context, sibling.path) is not None
        assert records.write_area("p", "s", continuation.delegation_id) == f"work/{first}"
        assert records.write_area("p", "s", first) == f"work/{first}"
    finally:
        documents.close()
        state.projects.close()


def test_only_the_current_running_attempt_holds_the_area(tmp_path: Path) -> None:
    state, documents, _, first = prepare(tmp_path)
    try:
        records = DelegationRecords(state.projects.control)
        # The interrupted attempt is not running: it holds nothing.
        with pytest.raises(ControlError) as refused:
            records.held_write_area("p", "s", CHILD, first)
        assert refused.value.kind == "conflict"
        assert refused.value.message.startswith(f"write_area_held: work/{first}/ is the write ")
        # A delegation with no recorded work has its own area.
        assert records.write_area("p", "s", SIBLING) == f"work/{SIBLING}"
    finally:
        documents.close()
        state.projects.close()
