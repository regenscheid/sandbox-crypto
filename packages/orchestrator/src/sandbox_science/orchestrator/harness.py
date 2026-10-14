# SPDX-License-Identifier: MIT
"""The harness: what runs one task of a session, built once and able to run many.

The CLI builds a ``Harness`` from its arguments, and a run service can build the same.
It takes its worker client, models and stores as constructed objects and does not close
them: their owner does, after the run. Each run resolves the session's execution
context, refuses a worker below the minimum isolation tier, assembles the run's tools
and turn hooks, runs the tool loop, and closes what it made. ``save`` then keeps the
run's record and lets each hook acknowledge what the record names.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext, Location, new_id
from sandbox_science.contracts.worker import Language

from .aio import threaded
from .artifact_publisher import ArtifactPublisher
from .artifacts import ArtifactInventory
from .consistency.controller import ReviewController
from .consistency.store import ReviewStore
from .documents import DocumentError, DocumentStore, DocumentTool
from .documents.mathcontroller import MathController
from .documents.mathjobs import Checker
from .hooks import TurnHook
from .jobs import JobTools
from .jobstate import JobState
from .loop import Interruption, Transcript, run_task
from .model import ChatModel
from .plan_tools import PlanTools
from .projects import ProjectError, ProjectStore, RunRecord, ensure_environment
from .session_plans import SessionPlans
from .tools import Toolbox, ToolOutcome
from .worker_client import WorkerClient


def tier_in_effect(location: Location) -> int | None:
    """The isolation tier the worker's probe left standing (D43); ``None`` without a probe.

    The worker lowers the declared tier when its probe finds less isolation, so the
    declared tier alone never counts.
    """
    return location.isolation_tier if location.isolation_evidence is not None else None


def describe_isolation(location: Location) -> str:
    """The tier in effect, the declared one if it differs, and what limits it."""
    evidence = location.isolation_evidence
    if evidence is None:
        return "no isolation evidence"
    text = f"isolation tier {location.isolation_tier}"
    if location.declared_isolation_tier != location.isolation_tier:
        text += f" (declared {location.declared_isolation_tier})"
    if evidence.findings:
        text += ": " + "; ".join(evidence.findings)
    return text


@dataclass(frozen=True)
class Role:
    """A model and the alias the gateway knows it by, which records name."""

    model: ChatModel
    alias: str


@dataclass(frozen=True)
class Stores:
    """A state directory's stores. Without them a run keeps no record and gets no
    documents, reviews, math checks, plans, artifact inventory or kernel check."""

    projects: ProjectStore
    documents: DocumentStore
    jobs: JobState


@dataclass(frozen=True)
class Settings:
    min_isolation_tier: int | None = 3
    """``None`` accepts a worker with any isolation, for development."""
    max_turns: int = 30
    watcher_calls_per_hour: int = 60
    watcher_first_check: float = 300
    watcher_interval: float = 1800
    checker: Checker | None = None
    """The configured math checker; without it, math checks report the renderer unavailable."""


@dataclass(frozen=True)
class RunRequest:
    """One task for a session. ``environment`` runs a session without stores in that
    environment as it is; with stores, the project's records choose it."""

    project: str
    session: str
    task: str
    environment: str | None = None
    dedicated: bool = False
    run_id: str | None = None
    research_template: str | None = None


class RunSink(Protocol):
    """Where a run reports as it goes."""

    def note(self, text: str) -> None:
        """Progress, such as the environment and the worker the run uses."""
        ...

    def error(self, text: str) -> None: ...

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None: ...


@dataclass
class Finished:
    """A run that reached the tool loop, with the hooks that acknowledge its record."""

    run_id: str
    context: ExecutionContext
    task: str
    started_at: datetime
    transcript: Transcript
    hooks: Sequence[TurnHook] = ()
    ended_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def status(self) -> int:
        """0 for an answer, 1 for a failed run, 3 for a run that stopped without one."""
        transcript = self.transcript
        return 1 if transcript.failure is not None else 0 if transcript.stopped == "answered" else 3

    def fields(self) -> dict[str, object]:
        """The run record's fields, as the harness has them."""
        transcript = self.transcript
        return {
            "run_id": self.run_id,
            "context": self.context.model_dump(mode="json"),
            "task": self.task,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stopped": transcript.stopped,
            "failure": transcript.failure,
            "final_text": transcript.final_text,
            "messages": transcript.messages,
            "prompt_provenance": (
                transcript.prompt_provenance.model_dump(mode="json")
                if transcript.prompt_provenance is not None
                else None
            ),
            "tools": transcript.tool_records,
            "delivered": {s: sorted(ids) for s, ids in transcript.delivered.items() if ids},
            "summaries": transcript.summaries,
        }


class Harness:
    def __init__(
        self,
        client: WorkerClient,
        main: Role,
        *,
        stores: Stores | None = None,
        watcher: Role | None = None,
        reviewer: Role | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.client, self.main, self.stores = client, main, stores
        self.watcher, self.reviewer = watcher, reviewer
        self.settings = settings or Settings()

    async def run(self, request: RunRequest, sink: RunSink) -> Finished | None:
        """Run the task to its end; ``None`` when the worker's isolation is refused.

        A failure in the run or in closing what it made is recorded in the transcript;
        the first cancellation or exit propagates.
        """
        run_id = request.run_id or new_id("run")
        started = datetime.now(UTC)
        context = await self.context(request, sink)
        capabilities = await self.client.capabilities(context.environment_id)
        location = capabilities.location
        minimum = self.settings.min_isolation_tier
        tier = tier_in_effect(location)
        if minimum is not None and (tier is None or tier < minimum):
            sink.error(
                f"the worker offers {describe_isolation(location)}. At least tier {minimum} "
                "is required (--allow-unisolated for development)"
            )
            return None
        sink.note(
            f"worker: pool {location.pool_id}, {location.backend} backend, sandbox "
            f"{location.sandbox_id}, host {location.hostname}, {describe_isolation(location)}"
        )
        transcript: Transcript | None = None
        hooks: Sequence[TurnHook] = ()
        interruption = Interruption()
        try:
            async with AsyncExitStack() as stack:
                with interruption.protect():
                    toolbox = await self._toolbox(
                        context, run_id, capabilities.languages, capabilities.features,
                        lambda close: closing(stack, interruption, close),
                    )  # fmt: skip
                    hooks = toolbox.turn_hooks()
                    transcript = await run_task(
                        self.main.model,
                        toolbox,
                        request.task,
                        run_id=run_id,
                        max_turns=self.settings.max_turns,
                        on_tool=sink.tool,
                        research_template=request.research_template,
                        hooks=hooks,
                    )
        except BaseException as error:
            if interruption.error is not None:
                raise interruption.error from None
            if not isinstance(error, Exception) or transcript is None:
                raise
            transcript.fail("cleanup")
        return Finished(run_id, context, request.task, started, transcript, hooks)

    async def context(self, request: RunRequest, sink: RunSink) -> ExecutionContext:
        """The session's execution context; from the project's records with stores, which
        open the session and have the worker create or revise its environment."""
        if self.stores is None:
            if request.environment is None:
                raise ValueError("a run without stores names its environment")
            return ExecutionContext(
                project_id=request.project,
                session_id=request.session,
                environment_id=request.environment,
            )
        projects = self.stores.projects
        project = projects.get(request.project)
        session = projects.open_session(
            request.project, request.session, dedicated=request.dedicated
        )
        info = await ensure_environment(self.client, project, session.environment_id)
        if info is not None:
            sink.note(
                f"environment: {info.environment_id} ({info.profile}, {info.policy}), "
                f"sandbox {info.sandbox}"
            )
        return ExecutionContext(
            project_id=request.project,
            session_id=request.session,
            environment_id=session.environment_id,
            policy_revision=project.policy,
        )

    async def _toolbox(
        self,
        context: ExecutionContext,
        run_id: str,
        languages: Sequence[Language],
        features: Sequence[str],
        on_close: Callable[[Callable[[], Awaitable[object]]], None],
    ) -> Toolbox:
        """The run's tools; ``on_close`` takes what closes each part the run made."""
        stores, settings, alias = self.stores, self.settings, self.main.alias
        watcher, reviewer = self.watcher, self.reviewer
        jobs = JobTools(
            self.client,
            context,
            run_id=run_id,
            state=stores.jobs if stores is not None else None,
            watcher=watcher.model if watcher is not None else None,
            watcher_alias=watcher.alias if watcher is not None else None,
            watcher_calls_per_hour=settings.watcher_calls_per_hour,
            first_check_seconds=settings.watcher_first_check,
            check_interval_seconds=settings.watcher_interval,
        )
        if stores is None:
            return Toolbox(self.client, context, languages=languages, features=features, jobs=jobs)
        documents = DocumentTool(stores.documents, context, run_id=run_id, alias=alias)
        inventory = ArtifactInventory(stores.documents)
        reviews = ReviewController(
            ReviewStore(stores.documents, artifacts=inventory),
            context,
            run_id=run_id,
            model=reviewer.model if reviewer is not None else None,
            alias=reviewer.alias if reviewer is not None else None,
            author_alias=alias,
        )
        on_close(reviews.close)
        artifacts = ArtifactPublisher(inventory, self.client, context, run_id=run_id, alias=alias)
        on_close(artifacts.aclose)
        math_checks = MathController(
            reviews.store, context.project_id, context.session_id, run_id, checker=settings.checker
        )
        owners: list[PlanTools] = []

        def make_plans() -> PlanTools:
            owner = PlanTools(
                SessionPlans(stores.documents), stores.projects, context, run_id=run_id, alias=alias
            )
            # Publish ownership before cancellation-owned threaded() can discard its
            # return value while propagating an interrupt.
            owners.append(owner)
            return owner

        async def close_plans() -> None:
            if owners:
                await threaded(owners[0].close)

        on_close(close_plans)
        plans = await threaded(make_plans)
        return Toolbox(
            self.client,
            context,
            languages=languages,
            features=features,
            documents=documents,
            jobs=jobs,
            reviews=reviews,
            math_checks=math_checks,
            artifacts=artifacts,
            plans=plans,
            check_kernels=True,
        )


def closing(
    stack: AsyncExitStack, interruption: Interruption, close: Callable[[], Awaitable[object]]
) -> None:
    """Close with ``stack``, in reverse order, keeping a cancellation for ``interruption``."""

    async def protected() -> None:
        with interruption.protect():
            await close()

    stack.push_async_callback(protected)


def _stderr(text: str) -> None:
    print(f"error: {text}", file=sys.stderr)


async def save(
    fields: dict[str, object],
    status: int,
    hooks: Sequence[TurnHook],
    *,
    projects: ProjectStore | None,
    transcript: Path | None = None,
    error: Callable[[str], None] = _stderr,
) -> int:
    """Keep a run's record, then let each hook acknowledge what the saved record names;
    ``status`` unless something fails.

    The record is written to ``transcript`` first, then kept in ``projects``. A record
    that does not validate, such as a tool result nested deeper than pydantic follows,
    still reaches ``transcript`` as the harness has it. A crash between the save and an
    acknowledgement safely repeats those notices. A hook that fails to acknowledge is
    reported; the others still acknowledge theirs.
    """
    try:
        record: RunRecord | None = RunRecord.model_validate(fields)
        invalid = ""
    except (ValidationError, RecursionError) as exc:
        record, invalid = None, str(exc)[:300]
    if transcript is not None:
        try:
            text = record.to_json() if record is not None else json.dumps(fields, default=str)
            transcript.write_text(text)
        except (OSError, ValueError, RecursionError) as exc:
            error(f"the transcript could not be written: {exc}")
            status = 1
    if projects is None:
        return status
    try:
        if record is None:
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"the record is invalid: {invalid}")
        projects.save_run(record)
    except (ProjectError, DocumentError, OSError, ValueError, RecursionError, sqlite3.Error) as exc:
        error(f"the run's record could not be kept: {exc}")
        return 1
    for hook in hooks:
        try:
            await hook.acknowledge(record)
        except Exception as exc:
            error(f"the run's {hook.name} could not be acknowledged: {exc}")
            status = 1
    return status
