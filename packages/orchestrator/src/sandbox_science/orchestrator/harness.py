# SPDX-License-Identifier: MIT
"""The harness: what runs one task of a session, built once and able to run many.

The CLI builds a ``Harness`` from its arguments, and a run service can build the same.
It takes its worker client, models and stores as constructed objects and does not close
them: their owner does, after the run. ``admit`` first resolves the session's
execution context and refuses a worker below the minimum isolation tier, needing only
the worker client and the project records, so a caller can build the rest after it.
``Harness.start`` then assembles the run's tools and turn hooks, runs the tool loop and
closes what it made; ``Harness.run`` does both. ``save`` keeps the run's record and lets
each hook acknowledge what the record names.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ErrorKind,
    ExecutionContext,
    Identifier,
    Location,
    new_id,
)
from sandbox_science.contracts.worker import Language

from . import mathchecks
from .aio import threaded
from .artifact_publisher import ArtifactPublisher
from .artifacts import ArtifactInventory
from .chat import EventLog, RunChat, UserMessages
from .connectors import Connectors, ProxyClient
from .consistency.controller import ReviewController
from .consistency.store import ReviewStore
from .documents import DocumentError, DocumentStore, DocumentTool
from .file_settings import ProjectFiles
from .hooks import TurnHook
from .jobs import JobTools
from .jobstate import JobState
from .ledger import Ledger
from .ledger.tool import LedgerTool
from .loop import MAX_INPUT_CHARS, Interruption, Transcript, run_task, user_cancelled
from .mathchecks.controller import MathController
from .mathchecks.jobs import Checker
from .model import ChatModel, Message
from .plan_tools import PlanTools
from .projects import (
    ProjectError,
    ProjectStore,
    RunRecord,
    RunRole,
    ensure_environment,
    run_name,
)
from .recording import FileRecorder
from .runstate import RunState
from .session_plans import SessionPlans
from .tools import HARNESS_TOOL_NAMES, Toolbox, ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient

_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)

_log = logging.getLogger(__name__)


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
    documents, reviews, math checks, plans, artifact inventory or kernel check.

    Making it subscribes the math checks to the documents' saved revisions, so every
    harness, however it is assembled, schedules them (``mathchecks.subscribe``).
    """

    projects: ProjectStore
    documents: DocumentStore
    jobs: JobState
    runs: RunState
    """The sessions' messages, run leases and run events."""

    def __post_init__(self) -> None:
        mathchecks.subscribe(self.documents)


@dataclass(frozen=True)
class Settings:
    min_isolation_tier: int | None = 3
    """``None`` accepts a worker with any isolation, for development."""
    max_turns: int = 30
    max_input_chars: int = MAX_INPUT_CHARS
    """The longest conversation MAIN is sent; a longer one stops the run."""
    watcher_calls_per_hour: int = 60
    watcher_first_check: float = 300
    watcher_interval: float = 1800


@dataclass(frozen=True)
class RunRequest:
    """One task for a session. Without stores, ``environment`` names the environment
    the session runs in as it is; with stores, the project's records choose it."""

    project: str
    session: str
    task: str
    environment: str | None = None
    dedicated: bool = False
    run_id: str | None = None
    """With stores, an identifier: it names the run's record."""
    research_template: str | None = None
    history: tuple[Message, ...] = ()
    """The session's earlier conversation, after the system message (RS-10)."""
    message_ids: tuple[str, ...] = ()
    """The queued messages the run is started for (RS-6): its conversation begins with
    them, and ``task`` only names them in the record."""
    parent: str | None = None
    """A delegated run's parent: the name its record will be kept under (PD-32)."""
    delegation: str | None = None
    """A delegated run's delegation. Nothing starts delegated runs yet."""

    def __post_init__(self) -> None:
        if (self.parent is None) != (self.delegation is None):
            raise ValueError("a delegated run names its parent and its delegation together")

    @property
    def role(self) -> RunRole:
        return "main" if self.parent is None else "delegated"


@dataclass(frozen=True)
class McpAccess:
    """The MCP proxy the harness calls, and the servers whose tools a run gets (D-08).

    The proxy knows the harness's token as one project's caller, and applies that
    project's policy and approvals to every call, so only runs of ``project`` get the
    servers' tools."""

    client: ProxyClient
    servers: tuple[str, ...]
    project: str


class RunSink(Protocol):
    """Where a run reports as it goes."""

    def note(self, text: str) -> None:
        """Progress, such as the environment and the worker the run uses."""
        ...

    def error(self, text: str) -> None: ...

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None: ...


class IsolationRefusedError(Exception):
    """The worker offers less isolation than the run requires; nothing ran."""


@dataclass(frozen=True)
class Admitted:
    """A run whose session is resolved and whose worker is isolated enough."""

    request: RunRequest
    run_id: str
    context: ExecutionContext
    languages: tuple[Language, ...]
    features: tuple[str, ...]
    stateful: bool
    started_at: datetime


async def admit(
    client: WorkerClient,
    projects: ProjectStore | None,
    request: RunRequest,
    sink: RunSink,
    *,
    min_isolation_tier: int | None,
) -> Admitted:
    """Resolve the session's execution context and check the worker's isolation, before
    anything else of the run is built.

    With ``projects``, the session is opened and the worker creates or revises its
    environment. Raises ``IsolationRefusedError`` for a worker below ``min_isolation_tier``.
    """
    started = datetime.now(UTC)
    if projects is not None:
        if request.environment is not None:
            raise ValueError("with stores, the project's records choose the environment")
        if request.run_id is not None:
            _IDENTIFIER.validate_python(request.run_id)
        project = projects.get(request.project)
        session = projects.open_session(
            request.project, request.session, dedicated=request.dedicated
        )
        info = await ensure_environment(client, project, session.environment_id)
        if info is not None:
            sink.note(
                f"environment: {info.environment_id} ({info.profile}, {info.policy}), "
                f"sandbox {info.sandbox}"
            )
        context = ExecutionContext(
            project_id=request.project,
            session_id=request.session,
            environment_id=session.environment_id,
            policy_revision=project.policy,
        )
    elif request.environment is None:
        raise ValueError("a run without stores names its environment")
    else:
        context = ExecutionContext(
            project_id=request.project,
            session_id=request.session,
            environment_id=request.environment,
        )
    capabilities = await client.capabilities(context.environment_id)
    location = capabilities.location
    tier = tier_in_effect(location)
    if min_isolation_tier is not None and (tier is None or tier < min_isolation_tier):
        raise IsolationRefusedError(
            f"the worker offers {describe_isolation(location)}. At least tier "
            f"{min_isolation_tier} is required"
        )
    sink.note(
        f"worker: pool {location.pool_id}, {location.backend} backend, sandbox "
        f"{location.sandbox_id}, host {location.hostname}, {describe_isolation(location)}"
    )
    return Admitted(
        request,
        request.run_id or new_id("run"),
        context,
        tuple(capabilities.languages),
        tuple(capabilities.features),
        projects is not None,
        started,
    )


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
    parent: str | None = None
    delegation: str | None = None

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
            "role": "main" if self.parent is None else "delegated",
            "parent": self.parent,
            "delegation": self.delegation,
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
            "usage": [usage.model_dump(mode="json") for usage in transcript.usage],
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
        checker: Checker | None = None,
        settings: Settings | None = None,
        clock: Clock = SYSTEM_CLOCK,
        mcp: McpAccess | None = None,
    ) -> None:
        """``checker`` is the configured math checker; without it, math checks report
        the renderer unavailable. A reviewer and a checker need stores."""
        if stores is None and (reviewer is not None or checker is not None):
            raise ValueError("a reviewer and a math checker need the state directory's stores")
        self.client, self.main, self.stores = client, main, stores
        self.watcher, self.reviewer, self.checker = watcher, reviewer, checker
        self.settings = settings or Settings()
        self.clock = clock
        """The time the run's messages and leases are read in."""
        self.mcp = mcp
        """The MCP proxy and the servers behind it whose tools runs are offered."""

    async def run(self, request: RunRequest, sink: RunSink) -> Finished:
        """Admit the run (``admit``), then run it (``start``)."""
        projects = self.stores.projects if self.stores is not None else None
        admitted = await admit(
            self.client,
            projects,
            request,
            sink,
            min_isolation_tier=self.settings.min_isolation_tier,
        )
        return await self.start(admitted, sink)

    async def start(
        self, admitted: Admitted, sink: RunSink, *, stop: asyncio.Event | None = None
    ) -> Finished:
        """Run an admitted task to its end.

        A failure in the run or in closing what it made is recorded in the transcript;
        the first cancellation or exit propagates, except one while ``stop`` is set: the
        user's, which ends the run ``cancelled``, or, once its loop has ended, leaves the
        run's own outcome.
        """
        if admitted.stateful != (self.stores is not None):
            raise ValueError("the run was admitted with other stores than this harness has")
        transcript: Transcript | None = None
        hooks: Sequence[TurnHook] = ()
        interruption = Interruption()
        try:
            async with AsyncExitStack() as stack:
                with interruption.protect():
                    chat = self.chat(admitted)
                    toolbox = await self._toolbox(
                        admitted, chat, lambda close: closing(stack, interruption, close)
                    )
                    hooks = toolbox.turn_hooks()
                    if chat is not None:
                        # The user's messages reach the model last; the events come last.
                        hooks = (*hooks, UserMessages(chat), EventLog(chat))
                    request = admitted.request
                    transcript = await run_task(
                        self.main.model,
                        toolbox,
                        None if request.message_ids else request.task,
                        run_id=admitted.run_id,
                        history=request.history,
                        stop=stop,
                        max_turns=self.settings.max_turns,
                        max_input_chars=self.settings.max_input_chars,
                        on_tool=sink.tool,
                        research_template=admitted.request.research_template,
                        hooks=hooks,
                    )
        except BaseException as error:
            if transcript is not None and user_cancelled(error, stop):
                pass  # it came as the run closed: the run's own outcome stands
            elif interruption.error is not None:
                raise interruption.error from None
            elif not isinstance(error, Exception) or transcript is None:
                raise
            else:
                transcript.fail("cleanup")
        return Finished(
            admitted.run_id,
            admitted.context,
            admitted.request.task,
            admitted.started_at,
            transcript,
            hooks,
            parent=admitted.request.parent,
            delegation=admitted.request.delegation,
        )

    def chat(self, admitted: Admitted) -> RunChat | None:
        """The run's messages and events, with stores: the run is named as its record
        will be."""
        if self.stores is None:
            return None
        request = admitted.request
        return RunChat(
            self.stores.runs,
            admitted.context,
            run=run_name(admitted.started_at, admitted.run_id),
            run_id=admitted.run_id,
            task=None if request.message_ids else request.task,
            message_ids=request.message_ids,
            prefix=1 + len(request.history) + (0 if request.message_ids else 1),
            parent=request.parent,
            delegation=request.delegation,
            clock=self.clock,
        )

    async def _toolbox(
        self,
        admitted: Admitted,
        chat: RunChat | None,
        on_close: Callable[[Callable[[], Awaitable[object]]], None],
    ) -> Toolbox:
        """The run's tools; ``on_close`` takes what closes each part the run made."""
        stores, settings, alias = self.stores, self.settings, self.main.alias
        watcher, reviewer = self.watcher, self.reviewer
        context, run_id = admitted.context, admitted.run_id
        languages, features = admitted.languages, admitted.features
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
            wake=chat.arrived if chat is not None else None,
            on_wait=chat.waiting if chat is not None else None,
        )
        connectors = None
        if self.mcp is not None and self.mcp.project == context.project_id:
            # Listed at the run's start: a server added later is offered to the next run.
            connectors = Connectors(self.mcp.client, self.mcp.servers, session=context.session_id)
            await connectors.load(taken=HARNESS_TOOL_NAMES)
        if stores is None:
            return Toolbox(
                self.client,
                context,
                languages=languages,
                features=features,
                jobs=jobs,
                connectors=connectors,
            )
        documents = DocumentTool(stores.documents, context, run_id=run_id, alias=alias)
        # Code in the sandbox has no route to the ledger: the agent imports what it
        # writes (LG-41), reading the file as its read_file tool does.
        ledger = LedgerTool(
            Ledger(stores.documents),
            context,
            run_id=run_id,
            role=admitted.request.role,
            alias=alias,
            read=lambda request: self.client.read_file(request),
        )
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
        recorder = FileRecorder(
            artifacts,
            ProjectFiles(stores.documents),
            role=admitted.request.role,
            parent_run=admitted.request.parent,
            delegation=admitted.request.delegation,
        )
        math_checks = MathController(
            reviews.store, context.project_id, context.session_id, run_id, checker=self.checker
        )
        # The plan tools hold nothing to close: the run's project store is the stores'.
        plans = await threaded(
            PlanTools,
            SessionPlans(stores.documents),
            stores.projects,
            context,
            run_id=run_id,
            alias=alias,
        )
        # A delegated run's cells run in kernels of its own, which stop when it ends
        # (PD-37); a main run uses the session's, and its kernel check closes those its
        # session's earlier runs left (PD-40) at its first observation of a connected
        # executor.
        kernel_run = admitted.run_id if admitted.request.parent is not None else None
        client = self.client
        if kernel_run is not None:

            async def stop_kernels() -> None:
                try:
                    await client.close_session(context, run=kernel_run)
                except WorkerAPIError as exc:
                    # The next main run of the session closes them.
                    _log.warning("could not stop run %s's kernels: %s", kernel_run, exc)

            on_close(stop_kernels)
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
            ledger=ledger,
            check_kernels=True,
            sweep_left_kernels=kernel_run is None,
            connectors=connectors,
            kernel_run=kernel_run,
            recorder=recorder,
        )


def closing(
    stack: AsyncExitStack, interruption: Interruption, close: Callable[[], Awaitable[object]]
) -> None:
    """Close with ``stack``, in reverse order, keeping a cancellation for ``interruption``."""

    async def protected() -> None:
        with interruption.protect():
            await close()

    stack.push_async_callback(protected)


async def save(
    fields: dict[str, object],
    status: int,
    hooks: Sequence[TurnHook],
    *,
    projects: ProjectStore | None,
    error: Callable[[str], None],
    transcript: Path | None = None,
) -> int:
    """Keep a run's record, then let each hook acknowledge what the saved record names;
    ``status`` unless something fails, which ``error`` reports.

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
