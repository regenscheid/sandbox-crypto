# SPDX-License-Identifier: MIT
"""Recover evidence by original identity, without replaying an uncertain effect."""

from __future__ import annotations

import json

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.contracts.worker import TERMINAL_STATES, ExecutionState
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.operations import ToolJournal, ToolRecord
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.question_tool import result
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.tools import PDF_SPECS
from sandbox_science.orchestrator.worker_client import WorkerClient

from .records import Work, WorkRecords


class Reconciler:
    def __init__(
        self, control: ControlStore, client: WorkerClient, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.control, self.client, self.clock = control, client, clock

    async def work(self, work: Work) -> None:
        """One bounded attempt page at a time; unresolved operations visibly block."""
        records, journal = WorkRecords(self.control), ToolJournal(self.control)
        after = ""
        while attempts := await threaded(
            records.attempts, work.project, work.session, work.work, after=after
        ):
            for run in attempts:
                cursor = 0
                while pending := await threaded(
                    journal.pending, work.project, work.session, run, after=cursor
                ):
                    for item in pending:
                        await self.operation(work, run, item.operation_id)
                    cursor = pending[-1].seq
            after = attempts[-1]

    async def operation(self, work: Work, run: str, operation: str) -> None:
        intent = await threaded(
            ToolJournal(self.control).intent, work.project, work.session, run, operation
        )
        tool = str(intent["tool"])
        context = ExecutionContext.model_validate(intent["context"])
        if tool == "ask_user":
            question = await threaded(
                Questions(self.control).by_operation, work.project, work.session, operation
            )
            if question is None:
                raise ControlError(CONFLICT, "unknown_question: original question was not saved")
            while question.state == "open":
                await self.clock.sleep(1)
                question = await threaded(
                    Questions(self.control).by_operation, work.project, work.session, operation
                )
                if question is None:
                    raise ControlError(CONFLICT, "original question disappeared")
            text, record = result(question)
        elif tool == "delegate":
            record = await threaded(self.delegation, work, operation)
            text = json.dumps(record)
        elif tool in {"wait_delegations", "delegation_status"}:
            # These observe children; losing their response creates no uncertain
            # effect. Do not invent the old observation or redispatch any child.
            record = {
                "reconciled": True,
                "interrupted_observation": True,
                "note": "The previous child observation was interrupted. Inspect the "
                "durable child outcomes with delegation_status or wait_delegations.",
            }
            text = json.dumps(record)
        elif tool in {"shell", "python", "sage", "start_job", *(spec.name for spec in PDF_SPECS)}:
            found = await self.client.lookup(context, operation)
            execution = found.execution
            if execution is None:
                raise ControlError(
                    CONFLICT, "unknown_operation: original worker operation not found"
                )
            if execution.state not in TERMINAL_STATES and tool != "start_job":
                async for _ in self.client.events(
                    execution.execution_id, execution.last_seq, project_id=work.project
                ):
                    pass
                execution = await self.client.get(execution.execution_id, project_id=work.project)
                if execution.state not in TERMINAL_STATES:
                    raise ControlError(
                        CONFLICT, "original worker computation has no terminal result"
                    )
            if execution.state == ExecutionState.LOST:
                raise ControlError(CONFLICT, "lost_operation: worker cannot determine its effect")
            if tool == "start_job":
                jobs = JobState(self.control.path.parent, control=self.control)
                await threaded(jobs.origin, context, execution.execution_id, run)
            record = {"execution": execution.model_dump(mode="json"), "reconciled": True}
            text = json.dumps(record)
            if len(text.encode()) > 1024 * 1024:
                text = json.dumps(
                    {
                        "execution_id": execution.execution_id,
                        "state": execution.state,
                        "reconciled": True,
                        "detail": "Full result is retained in the tool evidence journal.",
                    }
                )
        else:
            raise ControlError(
                CONFLICT, f"unknown_operation: {tool} requires explicit reconciliation"
            )
        record = {"tool": tool, "arguments": intent["arguments"], **record}
        parent = await threaded(self.parent, work, run)
        await threaded(
            Archive(self.control).append,
            work.project,
            work.session,
            run,
            Entry(
                role="tool",
                kind="tool_result",
                content=text,
                source=tool,
                call_id=str(intent["call_id"]),
            ),
            parent=parent,
            tool_record=ToolRecord(record, operation),
        )

    def delegation(self, work: Work, operation: str) -> dict[str, object]:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT detail FROM research_observations WHERE project=? "
                "AND session=? AND work=? AND kind='delegation_operation' AND identity=?",
                (work.project, work.session, work.work, operation),
            ).fetchone()
        if row is None:
            raise ControlError(CONFLICT, "unknown_delegation: no durable child admission")
        mapping = json.loads(row[0])
        return {
            "delegation_id": mapping["delegation_id"],
            "run": mapping["run"],
            "reconciled": True,
            "note": "Child admission recovered; consult its durable outcome.",
        }

    def parent(self, work: Work, run: str) -> str | None:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT parent FROM delegations WHERE project_id=? AND session_id=? AND run=?",
                (work.project, work.session, run),
            ).fetchone()
        return row[0] if row else None
