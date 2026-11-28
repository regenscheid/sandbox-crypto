# SPDX-License-Identifier: MIT
"""One durable completion protocol, followed by separately retryable kernel cleanup."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.contracts.child_environments import is_child_environment

from .aio import threaded
from .context.history.receipts import Receipts
from .continuity.child_settlement import ChildSettlement
from .continuity.records import Work, WorkRecords, authority
from .control import ControlError
from .delegation.hook import ChildEnd
from .documents import DocumentError, DocumentStore
from .hooks import RECURSION, TurnHook, mark_invalid, sweep_marked
from .jobs import cancel_closed_jobs
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
        """Keep resumable child kernels; release only completed or stopped work. A child
        in its own environment had its kernels released with it (CE-4)."""
        if is_child_environment(finished.context.environment_id):
            return "released"
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
        if await threaded(
            WorkRecords(self.projects.control).awaits_review,
            work.project,
            work.session,
            work.work,
        ):
            # A returned task keeps its kernels while its parent reviews it; they stop as
            # it is accepted or cancelled (PD-46).
            return "retained"
        try:
            async with asyncio.timeout(self.close_timeout):
                # The worker's close stops kernels only: the jobs its runs left go first.
                await cancel_closed_jobs(
                    self.worker, self.projects.control, context, work.kernel, f"it {work.state}"
                )
                await self.worker.close_session(context, run=work.kernel)
        except (WorkerAPIError, TimeoutError) as exc:
            _log.warning("could not close completed work kernels: %r", exc)
            return "deferred"
        return "released"


_STARTED: Final = TypeAdapter(datetime)
SALVAGE_ROUNDS: Final = 8
"""How many times a record that does not validate is pruned of what failed and checked
again before none is kept (``salvaged``)."""
SALVAGED: Final = "record_salvaged"
"""Where a salvaged record names what it left out."""


MINIMAL: Final = (
    "run_id",
    "role",
    "parent",
    "delegation",
    "alias",
    "context",
    "task",
    "started_at",
    "ended_at",
    "stopped",
    "failure",
    "answer",
    "final_text",
)
"""What a minimal record keeps of a run whose record cannot be salvaged otherwise."""


def salvaged(fields: dict[str, object], error: ValidationError) -> RunRecord | None:
    """``fields`` as a record without exactly what failed ``error``: each invalid element
    left out by its own place, a delivered ID, a usage entry, a message, an element of a
    summary (a delivered ID's notice is then given again, never consumed unsaid); an
    optional field that failed whole reset to its default. When a field the record cannot
    do without failed, the minimal record (``minimal``). ``summaries`` names what was left
    out; ``None`` only when not even the minimal record validates."""
    kept = copy.deepcopy(fields)
    dropped: list[str] = []
    for _ in range(SALVAGE_ROUNDS):
        touched: set[str] = set()
        for problem in error.errors():
            loc = problem["loc"]
            name = str(loc[0]) if loc else ""
            field = RunRecord.model_fields.get(name)
            if field is None:
                kept.pop(name, None)
                dropped.append(name)
                continue
            outermost = problem["type"] == RECURSION
            if (
                len(loc) > 1
                and (path := mark_invalid(kept.get(name), loc[1:], outermost=outermost)) is not None
            ):
                touched.add(name)
                dropped.append(f"{name}.{path}")
            elif field.is_required():
                return minimal(fields, [*dropped, name])
            else:
                kept[name] = field.get_default(call_default_factory=True)
                dropped.append(name)
        for name in touched:
            kept[name] = sweep_marked(kept[name])
        summaries = kept.get("summaries")
        summaries = dict(cast(dict[str, object], summaries)) if isinstance(summaries, dict) else {}
        candidate = {**kept, "summaries": {**summaries, SALVAGED: {"left_out": dropped[:100]}}}
        try:
            return RunRecord.model_validate(candidate)
        except ValidationError as again:
            error = again
        except RecursionError:
            break
    return minimal(fields, dropped)


def minimal(fields: dict[str, object], dropped: list[str]) -> RunRecord | None:
    """The least record of a run: its identity, task and times, how it stopped and why it
    failed, its answer and final text where they validate, and what was left out, with no
    conversation, tools or deliveries (its notices are given again). ``None`` when its
    identity, task, times or stop do not validate."""
    kept = {name: fields[name] for name in MINIMAL if name in fields}
    for _ in range(len(MINIMAL)):
        candidate: dict[str, object] = {
            **kept,
            "messages": [],
            "tools": [],
            "summaries": {SALVAGED: {"left_out": dropped[:100], "minimal": True}},
        }
        try:
            return RunRecord.model_validate(candidate)
        except ValidationError as error:
            failed = {str(e["loc"][0]) for e in error.errors() if e["loc"]}
            optional = {n for n in failed if not RunRecord.model_fields[n].is_required()}
            if failed - optional or not optional:
                return None
            for name in optional:
                kept.pop(name, None)
                dropped.append(name)
        except RecursionError:
            return None
    return None


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
    identity = _identity(fields) if projects is not None else None
    if projects is not None and identity is not None:
        # From the run's identity alone, so that a record saved without what failed, or a
        # minimal one, is fenced as a whole one is.
        context, started, run_id = identity
        owner = await threaded(
            WorkRecords(projects.control).owner,
            context.project_id,
            context.session_id,
            run_name(started, run_id),
        )
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


def _identity(fields: dict[str, object]) -> tuple[ExecutionContext, datetime, str] | None:
    """The run's context, start and ID as its record will name them; ``None`` when they
    do not validate, and no record will be kept (``_save`` reports why)."""
    try:
        context = ExecutionContext.model_validate(fields.get("context"))
        started = _STARTED.validate_python(fields.get("started_at"))
        run_id = fields.get("run_id")
    except (ValidationError, RecursionError):
        return None
    return (context, started, run_id) if isinstance(run_id, str) else None


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
        if isinstance(exc, ValidationError):
            # What was checked where it was contributed cannot fail here; whatever still
            # does is left out, so that the run keeps a record that ends as it ended.
            record = await threaded(salvaged, fields, exc)
            if record is not None:
                error(f"the run's record was kept without what did not validate: {invalid}")
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
