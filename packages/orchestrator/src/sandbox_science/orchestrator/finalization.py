# SPDX-License-Identifier: MIT
"""One durable completion protocol, followed by separately retryable kernel cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext

from .aio import threaded
from .context.history.receipts import Receipts
from .continuity.child_settlement import ChildSettlement
from .continuity.records import Work, WorkRecords, authority
from .control import ControlError
from .delegation.hook import ChildEnd
from .documents import DocumentError, DocumentStore
from .hooks import TurnHook
from .ledger import Ledger
from .projects import ProjectError, ProjectStore, RunRecord, run_name
from .worker_client import WorkerAPIError, WorkerClient

if TYPE_CHECKING:
    from .harness import Finished

_log = logging.getLogger(__name__)
Cleanup = Literal["retained", "released", "deferred"]


@dataclass
class RecordCompletion:
    """Observed completion stages; status alone also includes model/run failures.

    Mutable only while the record protocol runs, so a caught ownership error still
    reports which earlier stages committed. None means no transcript was requested.
    """

    status: int
    transcript_written: bool | None = None
    recorded: bool = False
    acknowledged: bool = False
    receipted: bool = False

    @property
    def confirmed(self) -> bool:
        """Whether all requested persistence and delivery steps were confirmed."""
        return (
            self.recorded
            and self.acknowledged
            and self.receipted
            and self.transcript_written is not False
        )


@dataclass(frozen=True)
class AttemptCompletion:
    """A durable result and the independent disposition of its worker resources."""

    record: RecordCompletion
    child: ChildEnd | None = None
    cleanup: Cleanup = "retained"


class AttemptFinalizer:
    """Checkpoint, record/acknowledge, settle, then release terminal child kernels.

    Persistence and settlement errors keep their existing propagation/replay rules.
    Worker unavailability only defers cleanup; it cannot revise durable completion.
    Recovery scheduling and the interpretation of typed child evidence stay outside.
    """

    def __init__(
        self,
        projects: ProjectStore,
        documents: DocumentStore,
        worker: WorkerClient,
        *,
        close_timeout: float,
    ) -> None:
        self.projects, self.documents, self.worker = projects, documents, worker
        self.close_timeout = close_timeout

    async def finish(
        self, finished: Finished, *, end: ChildEnd | None, error: Callable[[str], None]
    ) -> AttemptCompletion:
        """Finish a root or child; cancellation propagates at its original await boundary."""
        if (finished.delegation is None) != (end is None):
            raise ValueError("a child attempt requires its typed completion evidence")
        settlement = await self.checkpoint(finished, end) if end is not None else None
        record = await record_and_acknowledge(
            finished.fields(), finished.status, finished.hooks, projects=self.projects, error=error
        )
        if settlement is None:
            return AttemptCompletion(record)
        child = await settlement.settle(
            finished.context.project_id,
            finished.context.session_id,
            run_name(finished.started_at, finished.run_id),
        )
        cleanup = await self.release(finished)
        return AttemptCompletion(record, child, cleanup)

    async def checkpoint(self, finished: Finished, end: ChildEnd) -> ChildSettlement:
        """Keep exact typed evidence before the run's final save can complete its work."""
        if finished.delegation is None:
            raise ValueError("child settlement requires a durable delegated run")
        settlement = ChildSettlement(self.projects.control, Ledger(self.documents))
        await threaded(
            settlement.keep,
            finished.context.project_id,
            finished.context.session_id,
            run_name(finished.started_at, finished.run_id),
            finished.delegation,
            end,
        )
        return settlement

    async def release(self, finished: Finished) -> Cleanup:
        """Keep resumable child kernels; release only completed or stopped work."""
        work = await threaded(
            WorkRecords(self.projects.control).for_run,
            finished.context.project_id,
            finished.context.session_id,
            run_name(finished.started_at, finished.run_id),
        )
        return await self.release_work(finished.context, work) if work is not None else "retained"

    async def release_completed(self, context: ExecutionContext, root: str) -> None:
        """Retry terminal child cleanup under the current Resume root's authority."""
        if context.authority is None or context.authority.work != root:
            raise ValueError("child kernel recovery requires the current root authority")
        records = WorkRecords(self.projects.control)
        after = ""
        while page := await threaded(
            records.page, context.project_id, context.session_id, root, after=after
        ):
            for work in page:
                await self.release_work(context, work)
            after = page[-1].work

    async def release_work(self, context: ExecutionContext, work: Work) -> Cleanup:
        """A worker error defers cleanup without undoing its durable result."""
        if work.parent is None or work.kernel is None or work.state not in ("completed", "stopped"):
            return "retained"
        try:
            async with asyncio.timeout(self.close_timeout):
                await self.worker.close_session(context, run=work.kernel)
        except (WorkerAPIError, TimeoutError) as exc:
            _log.warning("could not close completed work kernels: %r", exc)
            return "deferred"
        return "released"


async def save(
    fields: dict[str, object],
    status: int,
    hooks: Sequence[TurnHook],
    *,
    projects: ProjectStore | None,
    error: Callable[[str], None],
    transcript: Path | None = None,
) -> int:
    """Compatible record-and-acknowledge entry point used by the CLI and run service."""
    return (
        await record_and_acknowledge(
            fields, status, hooks, projects=projects, error=error, transcript=transcript
        )
    ).status


async def record_and_acknowledge(
    fields: dict[str, object],
    status: int,
    hooks: Sequence[TurnHook],
    *,
    projects: ProjectStore | None,
    error: Callable[[str], None],
    transcript: Path | None = None,
) -> RecordCompletion:
    """Write the requested transcript, save the record, acknowledge, and receipt it.

    A record that does not validate still reaches the transcript as the harness has
    it. A crash after saving safely repeats notices. A failed acknowledgement is
    reported while the remaining hooks still acknowledge theirs. Durable receipts
    distinguish saved evidence from confirmed delivery even when status is already 1.
    """
    completion = RecordCompletion(status)
    owner = None
    if projects is not None:
        try:
            candidate = RunRecord.model_validate(fields)
            owner = await threaded(
                WorkRecords(projects.control).owner,
                candidate.context.project_id,
                candidate.context.session_id,
                run_name(candidate.started_at, candidate.run_id),
            )
        except (ValidationError, RecursionError):
            pass  # _save preserves the invalid transcript and reports the exact error
    try:
        if owner is not None:
            with authority(owner):
                completion.status = await _save(
                    fields,
                    status,
                    hooks,
                    projects=projects,
                    error=error,
                    transcript=transcript,
                    completion=completion,
                )
        else:
            completion.status = await _save(
                fields,
                status,
                hooks,
                projects=projects,
                error=error,
                transcript=transcript,
                completion=completion,
            )
        return completion
    except ControlError as exc:
        error(f"the run's ownership or persistence failed: {exc}")
        completion.status = 1
        return completion


async def _save(
    fields: dict[str, object],
    status: int,
    hooks: Sequence[TurnHook],
    *,
    projects: ProjectStore | None,
    error: Callable[[str], None],
    transcript: Path | None,
    completion: RecordCompletion,
) -> int:
    persisted = True
    try:
        # A large record takes long to check and to write: off the event loop, which
        # the session's other runs share.
        record: RunRecord | None = await threaded(RunRecord.model_validate, fields)
        invalid = ""
    except (ValidationError, RecursionError) as exc:
        record, invalid = None, str(exc)[:300]
    if transcript is not None:
        try:
            text = record.to_json() if record is not None else json.dumps(fields, default=str)
            transcript.write_text(text)
            completion.transcript_written = True
        except (OSError, ValueError, RecursionError) as exc:
            completion.transcript_written = False
            error(f"the transcript could not be written: {exc}")
            status = 1
            persisted = False
    if projects is None:
        return status
    try:
        if record is None:
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"the record is invalid: {invalid}")
        await threaded(projects.save_run, record)
        completion.recorded = True
    except (ProjectError, DocumentError, OSError, ValueError, RecursionError, sqlite3.Error) as exc:
        error(f"the run's record could not be kept: {exc}")
        if record is not None:
            await threaded(WorkRecords(projects.control).finish, record, persisted=False)
            completion.receipted = True
        return 1
    acknowledged = True
    for hook in hooks:
        try:
            if record.stored_history is not None and hook.name not in ("events",):
                receipts = Receipts(
                    projects.control,
                    record.context.project_id,
                    record.context.session_id,
                    run_name(record.started_at, record.run_id),
                )
                source = hook.name
                after = ""
                while page := await threaded(receipts.page, source, after=after):
                    await hook.acknowledge(
                        record.model_copy(update={"delivered": {source: list(page)}})
                    )
                    after = page[-1]
                if not after:
                    await hook.acknowledge(record)
            else:
                await hook.acknowledge(record)
        except Exception as exc:
            acknowledged = False
            error(f"the run's {hook.name} could not be acknowledged: {exc}")
            status = 1
            persisted = False
    completion.acknowledged = acknowledged
    await threaded(WorkRecords(projects.control).finish, record, persisted=persisted)
    completion.receipted = True
    return status
