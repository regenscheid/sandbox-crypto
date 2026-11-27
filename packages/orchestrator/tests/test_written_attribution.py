# SPDX-License-Identifier: MIT
"""A file of another session never becomes a call's artifact or review evidence, even
when a worker reports it among the call's written files; what the worker did not
attribute to the call is counted, never recorded (``attribution`` in the worker)."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

import pytest

from sandbox_science.contracts import (
    Execution,
    ExecutionState,
    JobStatus,
    Result,
    WriteFileRequest,
)
from sandbox_science.orchestrator.consistency.executions import execution_projection
from sandbox_science.orchestrator.jobs import summary
from sandbox_science.orchestrator.recording import Written

from .test_artifact_publication import CONTEXT
from .test_artifact_publication import worker_url as worker_url
from .test_file_recording import Recording
from .test_file_recording import recording as recording

pytestmark = pytest.mark.asyncio

OTHERS = "shared/uploads/s2/u1/source.pdf"
MINE = "shared/uploads/s1/u2/source.pdf"


def _execution(**result: object) -> Execution:
    return Execution(
        execution_id="exe-1",
        attempt_id="att-1",
        operation_id="op-1",
        kind="job",
        context=CONTEXT,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        result=Result.model_validate(result),
    )


async def test_another_sessions_upload_a_worker_reports_is_never_the_calls() -> None:
    execution = _execution(
        written_files=[OTHERS, MINE, "notes.md"],
        deleted_files=["shared/uploads/s2/u0/source.txt"],
        unattributed_files=["gate", OTHERS],
    )
    written = Written.of(execution)
    assert written.paths == (MINE, "notes.md")
    assert written.deleted == ()
    assert written.unattributed == 1
    view = summary(JobStatus(execution=execution), datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
    assert view["written_files"] == [MINE, "notes.md"]
    assert view["unattributed_files"] == ["gate"]
    projected, complete = execution_projection(execution.model_dump(mode="json"))
    assert projected["written_files"] == [MINE, "notes.md"]
    assert not complete
    alone, complete = execution_projection(
        _execution(written_files=[OTHERS]).model_dump(mode="json")
    )
    assert alone["written_files"] == []
    assert complete  # nothing of its own to copy


async def test_an_upload_to_another_session_during_a_call_is_not_recorded(
    recording: Recording,
) -> None:
    client = recording.toolbox.client
    other = CONTEXT.model_copy(update={"session_id": "s2", "actor": "user"})

    async def deliver() -> None:
        await asyncio.sleep(0.3)
        for context, path in ((other, OTHERS), (CONTEXT, "gate")):
            await client.write_file(
                WriteFileRequest(
                    context=context,
                    path=path,
                    content_b64=base64.b64encode(b"%PDF-").decode(),
                    create_only=True,
                )
            )

    delivery = asyncio.create_task(deliver())
    outcome = await recording.shell(
        "echo mine > mine.txt; while [ ! -e gate ]; do sleep 0.05; done"
    )
    await delivery
    report = outcome.record["recorded_files"]
    assert report["recorded"] == 1
    assert "not_attributed" in report
    assert json.loads(outcome.text)["recorded_files"]["recorded"] == 1
    heads = recording.heads()
    assert "sessions/s1/mine.txt" in heads
    assert not any(path.startswith("shared/uploads/") for path in heads)
    assert "sessions/s1/gate" not in heads
