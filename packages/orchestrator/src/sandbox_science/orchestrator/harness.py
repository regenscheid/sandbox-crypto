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
import dataclasses
import functools
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, cast

from pydantic import JsonValue, TypeAdapter, ValidationError

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
from .chat import EventLog, ModelBudget, RunChat, SessionHeld, UserMessages
from .connectors import Connectors, ProxyClient
from .consistency.controller import ReviewController
from .consistency.models import ReviewConfig
from .consistency.store import ReviewStore
from .delegation import prompt as delegated_prompt
from .delegation.fence import ChildFence
from .delegation.hook import Child, ChildEnd, Deadline, Delegations
from .delegation.models import ModelChoice
from .delegation.outcome import Outcome, Target, result_of
from .delegation.records import DelegationRecords
from .delegation.tracks import TrackTools, grant
from .documents import DocumentError, DocumentStore, DocumentTool
from .documents.tool import REQUESTS_TASK, WorkRequests
from .file_settings import ProjectFiles
from .history import (
    Archive,
    HistoryTool,
    RunArchive,
    View,
    ViewMeter,
    recorded_words,
    session_view,
    settle,
)
from .hooks import TurnHook
from .jobs import JobTools
from .jobstate import JobState
from .ledger import Ledger
from .ledger.tool import LedgerTool
from .loop import (
    MAX_INPUT_CHARS,
    SYSTEM_PROMPT,
    Interruption,
    Transcript,
    run_task,
    user_cancelled,
)
from .mathchecks.controller import MathController
from .mathchecks.jobs import Checker
from .memory import MemoryEntry, ProjectMemory
from .memory_prompt import for_reviewer, for_run, for_watcher
from .memory_tools import MemoryTool
from .model import ChatModel, Message, ModelError, ModelGateway
from .plan_tools import PlanTools
from .projects import (
    ProjectError,
    ProjectStore,
    RunRecord,
    RunRole,
    ensure_environment,
    operator_ref,
    run_name,
)
from .question_tool import QuestionTool
from .questions import Questions
from .recording import FileRecorder, RecordedFiles
from .runstate import RunState
from .session_plans import PlanRef, SessionPlans
from .skills import SkillStore, SkillTool
from .skills.builtin import builtin_pack
from .skills.derive import VERSION as PASS_VERSION
from .skills.discovery import SkillDiscovery
from .skills.embeddings import SkillVectors
from .tokens import chars_per_token
from .tools import HARNESS_TOOL_NAMES, Toolbox, ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient

_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)
Snapshot = tuple[int, tuple[MemoryEntry, ...]]
"""The project memory at a version: the version and its accepted entries in order."""

_log = logging.getLogger(__name__)

KERNEL_CLOSE_SECONDS: Final = 20.0
"""How long a delegated run's end waits for the worker to stop its kernels; the next
main run's kernel check closes those it could not (PD-40)."""


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
    review_token_budget: int = ReviewConfig().token_budget
    """The tokens one consistency review may use, over all its calls."""
    review_calls_per_hour: int = ReviewConfig().calls_per_hour
    """The reviewer calls a project may make in an hour."""
    cost_limits: bool = False
    """With stores, whether each main run keeps its project's daily cost limit, if the
    project has one, at its safe points, as the run service's runs do (RS-6c)."""
    run_share: float | None = None
    """With ``cost_limits``, the part of a project's daily cost limit one run may use,
    above 0 and at most 1; ``None``, as by default, for no part of its own."""

    def __post_init__(self) -> None:
        if self.run_share is not None and not 0 < self.run_share <= 1:
            raise ValueError("run_share is above 0 and at most 1")
        if self.run_share is not None and not self.cost_limits:
            raise ValueError("run_share is a part of the cost limits, which are not kept")

    def reviews(self) -> ReviewConfig:
        """The review settings of a project that has none of its own."""
        return ReviewConfig(
            token_budget=self.review_token_budget, calls_per_hour=self.review_calls_per_hour
        )


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
    operator: bool = False
    """Whether ``task`` is the operator's own words, typed at the command line: its
    archived task names it so (CM-9), as a queued run's names the user's messages."""
    requests: bool = False
    """Whether the run is started for the session's waiting work requests (LD-22): its
    conversation begins with them, as its hooks offer them, and ``task`` only names
    them in the record."""
    parent: str | None = None
    """A delegated run's parent: the name its record will be kept under (PD-32)."""
    delegation: str | None = None
    """A delegated run's delegation (``Harness.run_child``)."""
    memory_version: int | None = None
    """The project memory's version a run of a tree is given: the one its root run was
    given (PM-8). Delegation sets it from the version the root run was given, as the
    harness read it, never from an agent's arguments: the version decides what every
    run of the tree is told. Without it, a run is given the memory as it is when it
    starts."""
    max_turns: int | None = None
    """A delegated run's turns, at most; ``None`` for the harness's setting."""
    tokens: int | None = None
    """A delegated run's token limit (``run_budget`` when its calls reach it)."""
    target: Target | None = None
    """For a check, the entry and revision its result must name."""
    alias: str | None = None
    """For a delegated run, the model alias its delegation chose (PD-28, PD-30);
    ``None`` for the delegated role's."""
    track: tuple[PlanRef, str] | None = None
    """For a task on a track, the plan version it works under and the track (PD-9)."""
    root: str | None = None
    """For a delegated run, its tree's root, which holds the session for it (PD-32);
    ``None`` for a child of the root, whose parent is it."""
    may_delegate: bool = False
    """For a delegated task, whether it may delegate tasks once more (PD-15)."""
    stop_by: Deadline | None = None
    """For a delegated run, when its end must have stopped its own children, which its
    parent sets as it cancels it (PD-39)."""

    @property
    def queued(self) -> bool:
        """Whether the run begins with what waited for it rather than with ``task``."""
        return bool(self.message_ids) or self.requests

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
    about: Mapping[str, str] = field(default_factory=dict[str, str])
    """The operator's word on what each server is for, by its name in the index: a
    configured server, or one behind a portal (``e-print-mcp``)."""
    shown: tuple[str, ...] | None = None
    """The servers whose tools the index lists one by one; ``None`` lists every one."""


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
    alias: str | None = None
    limits: dict[str, JsonValue] | None = None

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
            "alias": self.alias,
            "limits": self.limits,
            "context": self.context.model_dump(mode="json"),
            "task": self.task,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stopped": transcript.stopped,
            "failure": transcript.failure,
            "final_text": transcript.final_text,
            "answer": transcript.answer,
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
        delegated: Role | None = None,
        gateway: ModelGateway | None = None,
        vectors: SkillVectors | None = None,
        skills_metadata: str | None = None,
    ) -> None:
        """``checker`` is the configured math checker; without it, math checks report
        the renderer unavailable. A reviewer and a checker need stores. ``delegated`` is
        the model delegated runs call (PD-27, PD-28); MAIN's without it. ``gateway``
        serves the other aliases a brief may choose, and says which it lists (PD-30);
        without it, delegated runs call ``delegated``'s alias only. ``vectors`` makes the
        skills' embeddings in the background, so that runs compare them by meaning too
        once they exist (docs/specs/skills.md, SK-29). ``skills_metadata`` is the alias
        of the import's model pass whose derivations, of this harness's version, runs are
        given (SK-30); without it, none."""
        if stores is None and (reviewer is not None or checker is not None):
            raise ValueError("a reviewer and a math checker need the state directory's stores")
        self.client, self.main, self.stores = client, main, stores
        self.vectors = vectors
        self.derived = None if skills_metadata is None else (skills_metadata, PASS_VERSION)
        self.watcher, self.reviewer, self.checker = watcher, reviewer, checker
        self.settings = settings or Settings()
        self.clock = clock
        """The time the run's messages and leases are read in."""
        self.mcp = mcp
        """The MCP proxy and the servers behind it whose tools runs are offered."""
        self.delegated = delegated or main
        self.gateway = gateway

    def role(self, request: RunRequest) -> Role:
        """The model a run calls: the delegated role's for a delegated run, or the one
        its delegation chose (PD-28, PD-30)."""
        if request.parent is None:
            return self.main
        if request.alias is None or request.alias == self.delegated.alias:
            return self.delegated
        if self.gateway is None:
            raise ModelError(f"no model gateway serves {request.alias!r}")
        return Role(self.gateway.model(request.alias), request.alias)

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
                    memory = await self.memory(admitted)
                    view = await self._view(admitted)
                    if view is not None:
                        # The session's view is the earlier conversation (CM-29).
                        request = dataclasses.replace(admitted.request, history=view.messages)
                        admitted = dataclasses.replace(admitted, request=request)
                    chat = self.chat(admitted)
                    if chat is not None and view is not None:
                        chat.shown = view.shown
                    delegations = await self._delegations(admitted, chat, memory)
                    toolbox = await self._toolbox(
                        admitted,
                        chat,
                        lambda close: closing(stack, interruption, close),
                        memory,
                        delegations,
                        stop,
                    )
                    hooks = toolbox.turn_hooks()
                    request = admitted.request
                    if view is not None and toolbox.skills is not None:
                        toolbox.skills.hold(view.loaded)  # CM-29b
                    # Discovery shows the skills that match the task, and each message.
                    discovery = None
                    if toolbox.skills is not None:
                        # A run started for comments alone has a stand-in task: discovery
                        # answers the comments' own texts, as they arrive (lab 42).
                        stand_in = request.queued or request.task == REQUESTS_TASK
                        task = None if stand_in else request.task
                        discovery = SkillDiscovery(toolbox.skills, task, clock=self.clock)
                        if view is not None:
                            discovery.shown.update(view.discovered)
                        for hook in hooks:
                            if isinstance(hook, WorkRequests):
                                hook.discover = functools.partial(
                                    discovery.about, answered="comment"
                                )
                        hooks = (*hooks, discovery)
                    if chat is not None:
                        hooks = (*hooks, *await self._chat_hooks(admitted, chat, discovery))
                    if view is not None:
                        hooks = (*hooks, ViewMeter(view))
                    log = None
                    if self.stores is not None and chat is not None:
                        # Each message is kept in the session's archive as it is added (CM-4).
                        archive = Archive(self.stores.projects.control, clock=self.clock)
                        log = RunArchive(
                            archive,
                            admitted.context,
                            run=chat.run,
                            parent=chat.parent,
                            message_ids=request.message_ids
                            or ((operator_ref(chat.run),) if request.operator else ()),
                            clock=self.clock,
                        )
                    transcript = await run_task(
                        self.role(request).model,
                        toolbox,
                        None if request.queued else request.task,
                        run_id=admitted.run_id,
                        history=request.history,
                        stop=stop,
                        max_turns=request.max_turns or self.settings.max_turns,
                        max_input_chars=self.settings.max_input_chars,
                        system=(
                            SYSTEM_PROMPT
                            if request.parent is None
                            else delegated_prompt.CHECK
                            if request.target is not None
                            else delegated_prompt.TASK
                        ),
                        on_tool=sink.tool,
                        research_template=admitted.request.research_template,
                        hooks=hooks,
                        memory=for_run(*memory) if memory is not None else None,
                        role=request.role,
                        log=log,
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
            alias=self.role(admitted.request).alias,
            limits=self._limits(admitted.request),
        )

    async def _view(self, admitted: Admitted) -> View | None:
        """A main run's view of its session, with stores (CM-29): the earlier messages of
        its session from the archive, which it starts with; ``None`` for any other run."""
        stores, request, context = self.stores, admitted.request, admitted.context
        if stores is None or request.parent is not None:
            return None
        project, session = context.project_id, context.session_id
        archive = Archive(stores.projects.control, clock=self.clock)
        calls = await threaded(stores.projects.usage, project, session, self.main.alias)
        runs = await threaded(stores.projects.runs, project, session)
        view = await threaded(
            session_view,
            archive,
            context,
            budget=self.settings.max_input_chars,
            per_token=chars_per_token(calls),
            runs=[entry.name for entry in runs if entry.role == "main"],
            earlier=functools.partial(
                recorded_words, stores.projects, stores.runs, project, session
            ),
        )
        kernels = None
        if view.cells:  # whether the kernels its cells ran in still run (CM-29c)
            try:
                kernels = (await self.client.observe_kernels(context)).kernels
            except Exception as error:  # unknown, then: the run goes on
                _log.warning("the kernels could not be observed for the view: %r", error)
        name = run_name(admitted.started_at, admitted.run_id)
        return await settle(view, archive, context, run=name, kernels=kernels, clock=self.clock)

    def _limits(self, request: RunRequest) -> dict[str, JsonValue]:
        """The limits a run runs under, as its record keeps them (PD-29)."""
        found: dict[str, JsonValue] = {
            "max_turns": request.max_turns or self.settings.max_turns,
            "max_input_chars": self.settings.max_input_chars,
        }
        if request.tokens is not None:
            found["tokens"] = request.tokens
        elif self.settings.run_share is not None and self.stores is not None:
            found["run_share"] = self.settings.run_share  # of the daily cost limit
        return found

    async def _chat_hooks(
        self, admitted: Admitted, chat: RunChat, discovery: SkillDiscovery | None = None
    ) -> tuple[TurnHook, ...]:
        """A run's budget, the user's messages, which reach the model last, and its
        events, which come last. A delegated run keeps its token limit, and its calls that
        may change something need its root's lease and the plan's leave (PD-34); the user
        writes to the root."""
        request = admitted.request
        if request.parent is not None and self.stores is not None:
            limit = ()
            if request.tokens is not None:
                limit = (ModelBudget(chat, tokens=request.tokens),)
            fence = await threaded(
                ChildFence,
                SessionPlans(self.stores.documents),
                self.stores.projects,
                admitted.context,
                run_id=admitted.run_id,
                granted=request.track[0] if request.track is not None else None,
            )
            # Its result is read from its closing answer once every other hook has had its say.
            outcome = Outcome(request.target, task=request.target is None)
            return (*limit, SessionHeld(chat), fence, outcome, EventLog(chat))
        settings = self.settings
        kept = (ModelBudget(chat, settings.run_share),) if settings.cost_limits else ()
        discover = discovery.about if discovery is not None else None
        return (*kept, UserMessages(chat, discover), EventLog(chat))

    async def _delegations(
        self,
        admitted: Admitted,
        chat: RunChat | None,
        memory: Snapshot | None,
    ) -> Delegations | None:
        """A main run's delegations: their handbacks always, and the delegate tools when
        its project lets its runs delegate (PD-33, PD-35); and a delegated task's own, when
        its main run let it delegate once more (PD-15): tasks on no track, whose tokens are
        carved from what it has left."""
        stores, context, clock = self.stores, admitted.context, self.clock
        request = admitted.request
        if stores is None or (request.parent is not None and not request.may_delegate):
            return None
        records = DelegationRecords(stores.documents, clock=clock)
        limits = await threaded(records.settings, context.project_id)
        # A delegated task its main run allowed delegates once more (PD-15).
        depth = 0 if request.parent is None else 1

        async def over_budget() -> bool:
            return await threaded(stores.runs.over_cost_limit, context.project_id, clock.now())

        # Every run of the tree is given the memory its root was (PM-8).
        version = memory[0] if memory is not None else None
        return Delegations(
            records,
            Ledger(stores.documents),
            context,
            parent=run_name(admitted.started_at, admitted.run_id),
            runner=functools.partial(self.run_child, admitted, version),
            max_turns=request.max_turns or self.settings.max_turns,
            offer=limits.depth >= 1 + depth,
            wake=chat.arrived if chat is not None and depth == 0 else None,
            over_budget=over_budget,
            limits=limits,
            models=ModelChoice(
                self.delegated.alias,
                limits,
                self.gateway.aliases if self.gateway is not None else None,
                self.gateway.tool_calling if self.gateway is not None else None,
            ),
            track_grant=(
                functools.partial(_track_grant, SessionPlans(stores.documents), context)
                if depth == 0
                else None
            ),
            depth=depth,
            tokens=request.tokens if depth > 0 else None,
            stop_by=request.stop_by,
            clock=clock,
        )

    async def run_child(
        self, parent: Admitted, memory_version: int | None, child: Child, stop: asyncio.Event
    ) -> ChildEnd:
        """Run a delegated run of ``parent`` to its end and save its record (PD-34): in
        the parent's session and environment, with the memory version its root was
        given (PM-8). A cancel while ``stop`` is set ends it ``cancelled``."""
        if self.stores is None:
            raise ValueError("a delegated run needs the state directory's stores")
        request = RunRequest(
            parent.request.project,
            parent.request.session,
            child.task,
            run_id=child.run_id,
            parent=run_name(parent.started_at, parent.run_id),
            delegation=child.delegation_id,
            memory_version=memory_version,
            max_turns=child.max_turns,
            tokens=child.tokens,
            target=child.target,
            alias=child.alias,
            track=child.track,
            root=parent.request.root or parent.request.parent,
            may_delegate=child.delegate,
            stop_by=child.deadline,
        )
        admitted = dataclasses.replace(
            parent, request=request, run_id=child.run_id, started_at=child.started_at
        )
        sink = _ChildSink(child.run_id)
        finished = await self.start(admitted, sink, stop=stop)
        await save(
            finished.fields(),
            finished.status,
            finished.hooks,
            projects=self.stores.projects,
            error=sink.error,
        )
        transcript = finished.transcript
        fence = next((hook for hook in finished.hooks if isinstance(hook, ChildFence)), None)
        found = next((hook for hook in finished.hooks if isinstance(hook, Outcome)), None)
        failed = transcript.failure is not None
        # A result stands only for a run that ended with that answer.
        result = result_of(found, transcript.stopped, failed) if found is not None else None
        return ChildEnd(
            transcript.stopped,
            failed,
            transcript.final_text,
            fence.refused if fence is not None else None,
            result.model_dump(mode="json") if result is not None else None,
            tuple(found.errors) if found is not None else (),
            request.alias or self.delegated.alias,
            tuple(dict.fromkeys(u.model for u in transcript.usage if u.model is not None))[:10],
            found.read if found is not None else None,
            found.read_proofs if found is not None else None,
            _recorded(finished.hooks, found.pinned if found is not None and result else []),
            tuple(found.created) if found is not None else (),
        )

    async def memory(self, admitted: Admitted) -> Snapshot | None:
        """The project memory the run and its reviewer are given, read once as the run
        starts and kept to its end (PM-5, PM-8); ``None`` without stores."""
        if self.stores is None:
            return None
        return await threaded(
            ProjectMemory(self.stores.documents).snapshot,
            admitted.context.project_id,
            admitted.request.memory_version,
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
            task=None if request.queued else request.task,
            message_ids=request.message_ids,
            prefix=1 + len(request.history) + (0 if request.queued else 1),
            parent=request.parent,
            delegation=request.delegation,
            # A delegated run's root holds the session for it: its parent, or its parent's.
            holder=request.root or request.parent,
            clock=self.clock,
        )

    async def _toolbox(
        self,
        admitted: Admitted,
        chat: RunChat | None,
        on_close: Callable[[Callable[[], Awaitable[object]]], None],
        memory: Snapshot | None = None,
        delegations: Delegations | None = None,
        stop: asyncio.Event | None = None,
    ) -> Toolbox:
        """The run's tools; ``on_close`` takes what closes each part the run made,
        ``memory`` is the project memory the run was given, which its reviewer is too, and
        ``stop`` is set when the user cancels the run.

        A delegated run gets its parent's tools without the review tools, the plan tools,
        the delegate tools, ``ask_user`` and connectors, and a check only the reads of the
        ledger, the documents and the memory; it is not reviewed, and the user's messages do
        not end its job waits (PD-34)."""
        stores, settings = self.stores, self.settings
        alias = self.role(admitted.request).alias
        watcher, reviewer = self.watcher, self.reviewer
        context, run_id = admitted.context, admitted.run_id
        languages, features = admitted.languages, admitted.features
        role = admitted.request.role
        delegated = role == "delegated"
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
            wake=chat.arrived if chat is not None and not delegated else None,
            on_wait=chat.waiting if chat is not None else None,
            # The watcher is given the memory as it is at each check: a job can outlive
            # its run (PM-8).
            memory=(
                (lambda: for_watcher(*ProjectMemory(stores.documents).snapshot(context.project_id)))
                if stores is not None
                else None
            ),
        )
        connectors = None
        if self.mcp is not None and self.mcp.project == context.project_id and not delegated:
            # Listed at the run's start: a server added later is offered to the next run.
            connectors = Connectors(
                self.mcp.client,
                self.mcp.servers,
                session=context.session_id,
                about=self.mcp.about,
                shown=self.mcp.shown,
            )
            await connectors.load(taken=HARNESS_TOOL_NAMES)
            if chat is not None and connectors.failed:
                chat.unavailable = dict(connectors.failed)
        if stores is None:
            return Toolbox(
                self.client,
                context,
                languages=languages,
                features=features,
                jobs=jobs,
                connectors=connectors,
            )
        documents = DocumentTool(stores.documents, context, run_id=run_id, role=role, alias=alias)
        if chat is not None:
            chat.requests = documents.requests_waiting
        # Code in the sandbox has no route to the ledger: the agent imports what it
        # writes (LG-41), reading the file as its read_file tool does.
        ledger = LedgerTool(
            Ledger(stores.documents),
            context,
            run_id=run_id,
            role=role,
            alias=alias,
            read=lambda request: self.client.read_file(request),
        )
        # Main and delegated runs propose; only the user changes the memory (PM-3).
        memory_tool = MemoryTool(
            ProjectMemory(stores.documents),
            context,
            run_id=run_id,
            role=role,
            alias=alias,
        )
        # The project's skills as the run starts: its set stays the same to the end (SK-5).
        # A check, and a run given no delegate tools, gets no skill that teaches them.
        delegates = delegations is not None and admitted.request.target is None
        skills = await threaded(
            _skills,
            stores,
            context.project_id,
            role,
            alias,
            delegates=delegates,
            derived=self.derived,
        )
        if skills is not None and self.vectors is not None:
            # Reads the vectors the store has; the gateway's work goes on in the background.
            await skills.use(self.vectors, context.project_id, clock=self.clock)
        inventory = ArtifactInventory(stores.documents)
        review_store = ReviewStore(
            stores.documents, artifacts=inventory, defaults=self.settings.reviews()
        )
        reviews = None
        if not delegated:
            reviews = ReviewController(
                review_store,
                context,
                run_id=run_id,
                model=reviewer.model if reviewer is not None else None,
                alias=reviewer.alias if reviewer is not None else None,
                author_alias=alias,
                memory=for_reviewer(*memory) if memory is not None else None,
                connectors=connectors.servers if connectors is not None else (),
                earlier=len(admitted.request.history),
                archived_as=chat.run if chat is not None else None,
            )
            on_close(reviews.close)
        artifacts = ArtifactPublisher(inventory, self.client, context, run_id=run_id, alias=alias)
        on_close(artifacts.aclose)
        recorder = FileRecorder(
            artifacts,
            ProjectFiles(stores.documents),
            role=role,
            parent_run=admitted.request.parent,
            delegation=admitted.request.delegation,
        )
        jobs.recorder = ledger.recorder = recorder
        math_checks = MathController(
            review_store, context.project_id, context.session_id, run_id, checker=self.checker
        )
        # The plan tools hold nothing to close: the run's project store is the stores'.
        plans = None
        if not delegated:
            plans = await threaded(
                PlanTools,
                SessionPlans(stores.documents),
                stores.projects,
                context,
                run_id=run_id,
                alias=alias,
            )
        # A main run asks the user and waits in the call; a delegated run that needs
        # the user ends blocked (PD-41).
        questions = None
        if not delegated:
            questions = QuestionTool(
                Questions(stores.documents, clock=self.clock),
                context,
                run=run_name(admitted.started_at, admitted.run_id),
                stop=stop,
                clock=self.clock,
            )
        # A delegated run's cells run in kernels of its own, which stop when it ends
        # (PD-37); a main run uses the session's, and its kernel check closes those its
        # session's earlier runs left (PD-40) at its first observation of a connected
        # executor.
        # A task on a track records its progress on that track's steps only (PD-9).
        tracks = None
        if (track := admitted.request.track) is not None:
            tracks = await threaded(
                TrackTools,
                SessionPlans(stores.documents),
                stores.projects,
                context,
                run_id=run_id,
                alias=alias,
                granted=track[0],
                track=track[1],
            )
        kernel_run = admitted.run_id if admitted.request.parent is not None else None
        client = self.client
        if kernel_run is not None:

            async def stop_kernels() -> None:
                try:
                    async with asyncio.timeout(KERNEL_CLOSE_SECONDS):
                        await client.close_session(context, run=kernel_run)
                except (WorkerAPIError, TimeoutError) as exc:
                    # The next main run of the session closes them.
                    _log.warning("could not stop run %s's kernels: %r", kernel_run, exc)

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
            memory=memory_tool,
            check_kernels=True,
            sweep_left_kernels=kernel_run is None,
            connectors=connectors,
            kernel_run=kernel_run,
            recorder=recorder,
            delegations=delegations,
            questions=questions,
            delegated=delegated,
            # A check reads and recomputes; it changes no entry, document or memory.
            check=admitted.request.target is not None,
            tracks=tracks,
            skills=skills,
            # Main and delegated runs read the archive; a delegated run its own run (CM-37).
            history=HistoryTool(
                Archive(stores.projects.control, clock=self.clock),
                context,
                run=run_name(admitted.started_at, admitted.run_id),
                delegated=delegated,
            ),
        )


def _skills(
    stores: Stores,
    project: str,
    role: str,
    alias: str | None,
    *,
    delegates: bool,
    derived: tuple[str, str] | None = None,
) -> SkillTool | None:
    """The run's skills, the built-in skills that ship pinned first (SK-40), or ``None``
    when the project pins none; ``delegates`` says whether the run has the delegate
    tools, without which the skills that teach them are left out."""
    store = SkillStore(stores.projects.control)
    store.ensure(project, builtin_pack())
    found = store.effective(project, role, delegates=delegates, derived=derived)
    return SkillTool(store, found, role=role, alias=alias) if found.entries else None


async def _track_grant(plans: SessionPlans, context: ExecutionContext, track: str) -> PlanRef:
    """The plan version a task on ``track`` works under (PD-9)."""
    return await threaded(grant, plans, context, track)


def _recorded(
    hooks: Sequence[TurnHook], results: Sequence[dict[str, JsonValue]] = ()
) -> dict[str, JsonValue] | None:
    """The files a run's calls recorded, as its record keeps them (RA-36), and the result
    files it declared, pinned (``results``, RA-38)."""
    found = next((hook for hook in hooks if isinstance(hook, RecordedFiles)), None)
    summary = found.summary() if found is not None else None
    files = cast(dict[str, JsonValue], summary) if isinstance(summary, dict) else None
    if not results:
        return files
    kept: dict[str, JsonValue] = dict(files) if files is not None else {"total": 0, "files": []}
    kept["results"] = list[JsonValue](results)
    return kept


class _ChildSink:
    """A delegated run's progress, to the log; its events show it in the GUI."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def note(self, text: str) -> None:
        _log.info("delegated run %s: %s", self.run_id, text)

    def error(self, text: str) -> None:
        _log.warning("delegated run %s: %s", self.run_id, text)

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None:
        return None


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
        except (OSError, ValueError, RecursionError) as exc:
            error(f"the transcript could not be written: {exc}")
            status = 1
    if projects is None:
        return status
    try:
        if record is None:
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"the record is invalid: {invalid}")
        await threaded(projects.save_run, record)
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
