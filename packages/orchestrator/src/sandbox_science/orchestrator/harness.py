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
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol, cast

from pydantic import JsonValue, TypeAdapter

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ExecutionContext,
    Identifier,
    JobProfileOffer,
    Location,
    new_id,
)
from sandbox_science.contracts.context import ExecutionAuthority
from sandbox_science.contracts.logs import log_context
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.contracts.worker import Language

from . import capabilities as caps
from . import figurechecks, mathchecks
from .agent_messages import AgentMailbox, AgentMessageStore
from .aio import threaded
from .artifact_publisher import ArtifactPublisher
from .artifacts import ArtifactInventory
from .chat import EventLog, RunChat, SessionHeld, UserMessages
from .connector_settings import ProjectConnectors
from .connectors import Connectors, ProxyClient
from .consistency.controller import ReviewController
from .consistency.models import REVIEWER_MAX_RESPONSE_BYTES, ReviewConfig
from .consistency.store import ReviewStore
from .context.agent import AgentContext
from .context.folding import FoldLifecycle
from .context.history import (
    CEILING,
    View,
    ViewMeter,
    ceiling,
)
from .context.history.geometry import DEFAULT_SHARES, Shares
from .context.history.limits import WorkingMemoryLimitError
from .context.history.retention import fits
from .context.history.snapshot import inline
from .context.preparation import FoldLimits, HistoryScope, PreparedContext, prepare_context
from .continuity.blueprints import Blueprint, Blueprints
from .continuity.child_settlement import ChildSettlement
from .continuity.hook import WorkHook
from .continuity.kernels import KernelJournal
from .continuity.models import IntentModel, ModelIntents
from .continuity.records import Ownership, WorkRecords, authority
from .continuity.resources import ResourceRequests
from .delegation import prompt as delegated_prompt
from .delegation.fence import ChildFence
from .delegation.hook import Child, ChildEnd, Deadline, Delegations
from .delegation.models import ModelChoice
from .delegation.outcome import Outcome, Target, result_of
from .delegation.records import DelegationRecords
from .delegation.requests import ChildApproval, ChildRequests
from .delegation.tracks import TrackTools, grant
from .documents import DocumentStore, DocumentTool
from .documents.tool import REQUESTS_TASK, WorkRequests
from .environment_claim import environment_claim
from .file_settings import ProjectFiles
from .finalization import AttemptCompletion, AttemptFinalizer
from .finalization import save as save
from .hooks import TurnHook
from .jobs import JobTools
from .jobstate import JobState
from .kernel_check import KernelCheck
from .ledger import Ledger
from .ledger.tool import LedgerTool
from .loop import (
    MAX_INPUT_CHARS,
    MAX_TURNS,
    SYSTEM_PROMPT,
    TURNS_CEILING,
    Interruption,
    Transcript,
    run_task,
    user_cancelled,
)
from .mathchecks.controller import MathController
from .mathchecks.jobs import Checker
from .mcp_binaries import ArtifactBinaries
from .memory import MemoryEntry, ProjectMemory
from .memory_prompt import for_reviewer, for_run, for_watcher
from .memory_tools import MemoryTool
from .messages import MessagesModel
from .model import (
    ChatModel,
    GatewayModel,
    Limits,
    Message,
    ModelError,
    ModelGateway,
    cached_for,
)
from .model_profiles import PROFILES, ModelProfile, ModelRole, profiles
from .model_settings import AgentModelRole, AgentModelSelection, ProjectModels
from .plan_tools import PlanTools
from .projects import (
    ProjectStore,
    RunRole,
    ensure_environment,
    operator_ref,
    run_id_of,
    run_name,
)
from .question_tool import QuestionTool
from .questions import Questions
from .recording import FileRecorder, RecordedFiles
from .resource_hooks import Resources
from .run_inputs import RecordInputs, RunInputs
from .runstate import RunState
from .session_plans import PlanRef, SessionPlans
from .skills import SkillStore, SkillTool
from .skills.builtin import builtin_pack
from .skills.derive import VERSION as PASS_VERSION
from .skills.discovery import SkillDiscovery
from .skills.embeddings import SkillVectors
from .skills.staging import Stager
from .tools import HARNESS_TOOL_NAMES, Toolbox, ToolOutcome
from .view_image import ImageViewer
from .worker_client import WorkerClient

_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)
Snapshot = tuple[int, tuple[MemoryEntry, ...]]
"""The project memory at a version: the version and its accepted entries in order."""

_log = logging.getLogger(__name__)
FOLDING_CHARS: Final = 1 << 40
"""A folding run's character limit, by default: none that a run reaches; its ceiling
is in tokens."""

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

    Making it subscribes the math checks to the documents' saved revisions, and the
    figure checks to them and to the ledger's entries, so every harness, however it is
    assembled, schedules them (``mathchecks.subscribe``, ``figurechecks.subscribe``).
    """

    projects: ProjectStore
    documents: DocumentStore
    jobs: JobState
    runs: RunState
    """The sessions' messages, run leases and run events."""

    def __post_init__(self) -> None:
        mathchecks.subscribe(self.documents)
        figurechecks.subscribe(self.documents)


@dataclass(frozen=True)
class Settings:
    min_isolation_tier: int | None = 3
    """``None`` accepts a worker with any isolation, for development."""
    max_turns: int = MAX_TURNS
    """A main run's model calls before its report round (RS-6f): 2,000 by default.
    Delegated runs have independent role allowances; work may outlive an attempt."""
    max_input_chars: int | None = None
    """The longest conversation a run is sent, in characters; a longer one stops the
    run. ``None``: ``MAX_INPUT_CHARS`` for a run that does not fold, and none for a main
    run with stores, which folds within its ceiling in tokens."""
    ceiling: int = CEILING
    """MAIN's ceiling in tokens, before the model's input limit caps it (CM-27)."""
    chunk: int | None = None
    """MAIN's chunk in tokens; ``None`` for max(50,000, 0.2 C)."""
    input_limit: int | None = None
    """MAIN's model's input limit in tokens, without its output, where the gateway's
    model information gives none."""
    shares: Shares = DEFAULT_SHARES
    """The view's shares of C: the user's words', the digest's and the fold region's
    (CM-27); the labs set small ones to force overviews and pointers."""
    skeleton_only: bool = False
    """Whether MAIN's folds are skeleton-only, without the enrichment call: the control
    arm (docs/specs/context-management.md, question 4)."""
    fold_alias: str | None = None
    image_aliases: frozenset[str] = frozenset()
    """Aliases the deployment declares accept images, whatever the gateway says
    (``view_image``; docs/specs/figures.md, FG-33)."""
    """The alias whose model writes MAIN's folds' narratives; ``None`` for MAIN's own,
    which reads the run's view from the prompt cache (CM-19)."""
    watcher_calls_per_hour: int = 60
    watcher_first_check: float = 300
    watcher_interval: float = 1800
    review_token_budget: int | None = ReviewConfig().token_budget
    """An optional cap on the tokens one consistency review may use, over all its calls;
    none by default (D102)."""
    review_calls_per_hour: int = ReviewConfig().calls_per_hour
    """The reviewer calls a project may make in an hour."""
    recover_interrupted: bool = False
    """Operator policy allowing bounded automatic Resume after infrastructure loss."""
    cost_limits: bool = False
    """Whether ``run_share`` applies, as the run service's runs have it (RS-6c). A
    configured project limit is the model-call journal's in every case (D102)."""
    run_share: float | None = None
    """With ``cost_limits``, the part of a project's daily cost limit one run may use,
    above 0 and at most 1, which the model-call journal enforces; ``None``, as by
    default, for no part of its own."""

    def __post_init__(self) -> None:
        if not 1 <= self.max_turns <= TURNS_CEILING:
            raise ValueError(f"max_turns is 1 to {TURNS_CEILING:,}")
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
    consolidate: bool = False
    """A consolidation run after a ``research_state`` stop (CM-45): its view shows every
    current decision in one line, and it is told to propose how to consolidate them."""
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
    """A delegated run's soft token cap: it checks in with its parent there (D102)."""
    target: Target | None = None
    """For a check, the entry and revision its result must name."""
    alias: str | None = None
    """For a delegated run, the model alias its delegation chose (PD-28, PD-30);
    ``None`` for the delegated role's."""
    model_profiles: dict[ModelRole, ModelProfile] | None = None
    track: tuple[PlanRef, str] | None = None
    """For a task on a track, the plan version it works under and the track (PD-9)."""
    root: str | None = None
    """For a delegated run, its tree's root, which holds the session for it (PD-32);
    ``None`` for a child of the root, whose parent is it."""
    may_delegate: bool = False
    """For a delegated task, whether it may delegate tasks once more (PD-15)."""
    work: str | None = None
    """A continuation's durable work identity; set only by explicit recovery."""
    predecessor: str | None = None
    resume_source: Literal["operator", "automatic"] | None = None
    """Trusted provenance for a continuation created by Recovery. It distinguishes an
    operator-requested Resume from bounded automatic recovery; ordinary runs leave it unset."""
    epoch: int | None = None
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


def kernel_note(languages: Sequence[str], reason: str | None = None) -> str:
    """What a run's progress says of the kernels its environment offers, which are the kernel
    tools its agent gets. Without a Python kernel the agent has no ``python`` tool, and
    computes through ``shell`` alone: the note says why that can be, and the executor's own
    ``reason`` when it gave one."""
    if "python" in languages:
        return f"kernels: {', '.join(languages)}"
    others = f"; offered: {', '.join(languages)}" if languages else ""
    said = f" ({reason})" if reason else ""
    return (
        f"kernels: no python kernel{said}, so the run has no python tool"
        f"{others}. An executor offers Python only where its interpreter has ipykernel, "
        "jupyter_client and the python3 kernelspec"
    )


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
    kernel_reason: str | None = None
    """Why the environment offers no Python kernel, when its executor said."""
    job_profiles: tuple[JobProfileOffer, ...] = ()
    """The job sandbox sizes the worker grants the project (D93)."""


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
    sink.note(kernel_note(capabilities.languages, capabilities.kernel_reason))
    return Admitted(
        request,
        request.run_id or new_id("run"),
        context,
        tuple(capabilities.languages),
        tuple(capabilities.features),
        projects is not None,
        started,
        capabilities.kernel_reason,
        capabilities.job_profiles if "job_sandboxes" in capabilities.features else (),
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
    model_profiles: dict[ModelRole, ModelProfile] | None = None
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
        memory = transcript.context.memory
        messages = (
            inline(transcript.context.messages)
            if memory is not None
            else transcript.context.messages
        )
        tools = inline(transcript.tool_records) if memory is not None else transcript.tool_records
        usage = (
            inline(transcript.context.usage)
            if memory is not None
            else [item.model_dump(mode="json") for item in transcript.context.usage]
        )
        delivered: dict[str, object] = {}
        summaries = (
            memory.summaries(transcript.summaries) if memory is not None else transcript.summaries
        )
        complete = True
        for source, ids in transcript.delivered.items():
            page = inline(ids) if memory is not None else sorted(ids)
            if page is None:
                complete = False
            else:
                delivered[source] = sorted(page)
        fields: dict[str, object] = {
            "stored_history": memory.reference() if memory is not None else None,
            "run_id": self.run_id,
            "role": "main" if self.parent is None else "delegated",
            "parent": self.parent,
            "delegation": self.delegation,
            "alias": self.alias,
            "model_profiles": {
                key: value.model_dump(mode="json")
                for key, value in (self.model_profiles or {}).items()
            },
            "limits": self.limits,
            "context": self.context.model_dump(mode="json"),
            "task": self.task,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stopped": transcript.stopped,
            "failure": transcript.failure,
            "final_text": transcript.final_text,
            "answer": transcript.answer,
            "messages": messages or [],
            "prompt_provenance": (
                transcript.context.prompt_provenance.model_dump(mode="json")
                if transcript.context.prompt_provenance is not None
                else None
            ),
            "tools": tools or [],
            "delivered": delivered if complete else {},
            "usage": usage or [],
            "summaries": summaries,
        }
        if memory is not None:
            for name in ("task", "answer", "final_text"):
                text = fields[name]
                if isinstance(text, str) and len(text) > 16384:
                    fields[name] = (
                        text[:8192]
                        + "\n[Preview; exact text is in stored evidence.]\n"
                        + text[-8192:]
                    )
            checked = {
                **fields,
                "started_at": self.started_at.isoformat(),
                "ended_at": self.ended_at.isoformat(),
            }
            if not fits(checked, 1024 * 1024):
                for name in ("messages", "tools", "usage"):
                    fields[name] = []
                fields["delivered"] = {}
                fields["prompt_provenance"] = None
        return fields


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
        loop_runner: Callable[..., Awaitable[Transcript]] = run_task,
        prepare_attempt: Callable[[Admitted], AbstractAsyncContextManager[None]] | None = None,
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
        self.loop_runner = loop_runner
        self.prepare_attempt = prepare_attempt
        self.vectors = vectors
        self.derived = None if skills_metadata is None else (skills_metadata, PASS_VERSION)
        self.watcher, self.reviewer, self.checker = watcher, reviewer, checker
        self.settings = settings or Settings()
        self.clock = clock
        """The time the run's messages and leases are read in."""
        self.mcp = mcp
        """The MCP proxy and the servers behind it whose tools runs are offered."""
        self.delegated = delegated or (
            Role(
                main.model.cached_as("unassigned").for_role(
                    "delegated", gateway.profile(main.alias, role="delegated") if gateway else None
                ),
                main.alias,
            )
            if isinstance(main.model, GatewayModel)
            else main
        )
        self.gateway = gateway
        self._limits_seen: dict[str, Limits] | None = None
        """The limits the gateway's model information gave, once it gave them."""
        self._learned: dict[str, int] = {}
        """Each alias's ceiling a context error capped, for later runs (CM-43)."""

    def project_policy(self, project: str) -> Callable[[], Awaitable[str | None]] | None:
        """How a claim reads the project's current policy revision, to follow a change
        made during a run (``environment_claim``); none without stores."""
        stores = self.stores
        if stores is None:
            return None

        async def current() -> str | None:
            return (await threaded(stores.projects.get, project)).policy

        return current

    def model_profiles(
        self, request: RunRequest, *, legacy: bool = False
    ) -> dict[ModelRole, ModelProfile]:
        """Snapshot callers before work admission, including explicit default absence."""
        if request.model_profiles is not None:
            return dict(request.model_profiles)
        inherited = PROFILES.get()
        if inherited is not None and not legacy:
            result = dict(inherited)
        else:
            result: dict[ModelRole, ModelProfile] = {}
            for name, role in (
                ("main", self.main),
                ("delegated", self.delegated),
                ("reviewer", self.reviewer),
                ("watcher", self.watcher),
            ):
                if role is not None and isinstance(role.model, GatewayModel):
                    result[cast(ModelRole, name)] = role.model.requested_profile
            if not legacy and self.stores is not None and request.parent is None:
                _, selected = ProjectModels(self.stores.documents).selections(request.project)
                for role, choice in selected.items():
                    result[role] = self._selected_profile(role, choice)
            if legacy and request.alias is not None and self.gateway is not None:
                # A legacy blueprint has no profile snapshot, but its immutable alias was
                # saved. Reconstruct that caller instead of adopting later project settings.
                active: AgentModelRole = "main" if request.parent is None else "delegated"
                result[active] = self.gateway.profile(request.alias, role=active)
            for name in ("fold", "overview", "skills_metadata"):
                summary_role: AgentModelRole = "delegated" if request.parent is not None else "main"
                alias = (
                    (self.derived[0] if self.derived else None)
                    if name == "skills_metadata"
                    else self.settings.fold_alias
                    or (
                        result[summary_role].alias
                        if summary_role in result
                        else (self.delegated if request.parent is not None else self.main).alias
                    )
                )
                if alias is not None and self.gateway is not None:
                    result[cast(ModelRole, name)] = self.gateway.profile(
                        alias, role=cast(ModelRole, name)
                    )
        if request.parent is not None and request.alias is not None and self.gateway is not None:
            result["delegated"] = ModelProfile.model_validate(
                {
                    "alias": request.alias,
                    "api": self.gateway.apis.get(request.alias, "chat"),
                    "reasoning_effort": result["delegated"].reasoning_effort
                    if "delegated" in result
                    else None,
                }
            )
        if request.parent is not None and self.gateway is not None:
            # A newly constructed child's summaries follow its recorded selection.
            # An explicit saved profile returned above remains immutable on Resume.
            delegated = result.get("delegated")
            alias = (
                self.settings.fold_alias
                or request.alias
                or (delegated.alias if delegated is not None else self.delegated.alias)
            )
            for role in ("fold", "overview"):
                key = cast(ModelRole, role)
                previous = result.get(key)
                result[key] = self.gateway.profile(alias, role=key).model_copy(
                    update={"reasoning_effort": previous.reasoning_effort if previous else None}
                )
        if legacy:
            result = {
                key: value.model_copy(update={"reasoning_effort": None})
                for key, value in result.items()
            }
        return result

    def _agent_catalog(self) -> frozenset[str]:
        return frozenset(
            role.alias for role in (self.main, self.delegated, self.reviewer) if role is not None
        )

    def _selected_profile(
        self, role: AgentModelRole, selection: AgentModelSelection
    ) -> ModelProfile:
        if selection.alias not in self._agent_catalog():
            raise ModelError(f"project {role} model is outside the deployment catalog")
        configured = {
            "main": self.main,
            "delegated": self.delegated,
            "reviewer": self.reviewer,
        }[role]
        if configured is None:
            raise ModelError(f"project selected an unavailable {role} model")
        if self.gateway is None:
            raise ModelError(f"project cannot change {role} model without a gateway")
        profile = self.gateway.profile(selection.alias, role=role)
        return profile.model_copy(update={"reasoning_effort": selection.reasoning_effort})

    def role(self, request: RunRequest) -> Role:
        """The model a run calls: the delegated role's for a delegated run, or the one
        its delegation chose (PD-28, PD-30)."""
        name: AgentModelRole = "main" if request.parent is None else "delegated"
        configured = self.main if name == "main" else self.delegated
        selected = (request.model_profiles or {}).get(name)
        alias = request.alias if request.parent is not None and request.alias is not None else None
        alias = alias or (selected.alias if selected is not None else configured.alias)
        if alias == configured.alias:
            return configured
        if self.gateway is None:
            raise ModelError(f"no model gateway serves {alias!r}")
        return Role(self.gateway.model(alias, role=name), alias)

    def reviewer_role(self, request: RunRequest) -> Role | None:
        """The consistency reviewer pinned for this tree, with reviewer response bounds."""
        configured = self.reviewer
        if configured is None:
            return None
        selected = (request.model_profiles or {}).get("reviewer")
        alias = selected.alias if selected is not None else configured.alias
        if alias == configured.alias:
            return configured
        if self.gateway is None:
            raise ModelError(f"no model gateway serves reviewer {alias!r}")
        return Role(
            self.gateway.model(alias, role="reviewer").limited(
                max_output=4096, max_response_bytes=REVIEWER_MAX_RESPONSE_BYTES
            ),
            alias,
        )

    def _model(self, admitted: Admitted) -> ChatModel:
        """The model a run calls (``role``), calling as its session, so that all of the
        session's calls share one prompt cache (CM-32)."""
        context = admitted.context
        model = cached_for(
            self.role(admitted.request).model, context.project_id, context.session_id
        )
        if self.stores is None or self.loop_runner is not run_task:
            return model
        owner = WorkRecords(self.stores.projects.control).owner(
            context.project_id, context.session_id, run_name(admitted.started_at, admitted.run_id)
        )
        return (
            model
            if owner is None
            else IntentModel(
                model,
                ModelIntents(
                    self.stores.projects.control,
                    run_share=self.settings.run_share if self.settings.cost_limits else None,
                ),
                owner,
                self.role(admitted.request).alias,
                kind="delegated" if admitted.request.parent is not None else "main",
            )
        )

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
        self,
        admitted: Admitted,
        sink: RunSink,
        *,
        stop: asyncio.Event | None = None,
        wait_children: Callable[[], Awaitable[bool]] | None = None,
    ) -> Finished:
        """Run an admitted task to its end, with its project, session, environment and run
        on every log record it makes (``log_context``). See ``_run``."""
        context = admitted.context
        with log_context(
            project=context.project_id,
            session=context.session_id,
            environment=context.environment_id,
            run=admitted.run_id,
        ):
            return await self._run(admitted, sink, stop=stop, wait_children=wait_children)

    async def _run(
        self,
        admitted: Admitted,
        sink: RunSink,
        *,
        stop: asyncio.Event | None = None,
        wait_children: Callable[[], Awaitable[bool]] | None = None,
    ) -> Finished:
        """Run an admitted task to its end.

        A failure in the run or in closing what it made is recorded in the transcript;
        the first cancellation or exit propagates, except one while ``stop`` is set: the
        user's, which ends the run ``cancelled``, or, once its loop has ended, leaves the
        run's own outcome.
        """
        admitted = dataclasses.replace(
            admitted,
            request=dataclasses.replace(
                admitted.request, model_profiles=self.model_profiles(admitted.request)
            ),
        )
        if self.stores is None:
            return await self._start(admitted, sink, stop=stop)
        request = admitted.request
        records = WorkRecords(self.stores.projects.control, clock=self.clock)
        owner = await threaded(
            records.admit,
            request.project,
            request.session,
            run_name(admitted.started_at, admitted.run_id),
            parent=request.parent,
            work=request.work,
            predecessor=request.predecessor,
            epoch=request.epoch,
        )
        await threaded(
            Blueprints(self.stores.projects.control).save, owner, self._blueprint(admitted)
        )
        await self.client.fence(
            RecoveryFence(project_id=owner.project, session_id=owner.session, epoch=owner.epoch)
        )
        admitted = dataclasses.replace(
            admitted,
            context=admitted.context.model_copy(
                update={
                    "authority": ExecutionAuthority(
                        work=owner.work, run=owner.run, epoch=owner.epoch
                    )
                }
            ),
        )
        request = dataclasses.replace(request, work=owner.work, epoch=owner.epoch)
        admitted = dataclasses.replace(admitted, request=request)
        with authority(owner):
            try:
                return await self._start(
                    admitted,
                    sink,
                    stop=stop,
                    recovering=wait_children is not None,
                    work=WorkHook(
                        records,
                        owner,
                        predecessor=request.predecessor,
                        resume_source=request.resume_source,
                        wait_children=wait_children,
                    ),
                )
            except BaseException:
                await threaded(
                    records.block, owner.project, owner.session, owner.work, "interrupted"
                )
                raise

    async def _start(
        self,
        admitted: Admitted,
        sink: RunSink,
        *,
        stop: asyncio.Event | None = None,
        recovering: bool = False,
        work: WorkHook | None = None,
    ) -> Finished:
        if admitted.stateful != (self.stores is not None):
            raise ValueError("the run was admitted with other stores than this harness has")
        transcript: Transcript | None = None
        hooks: Sequence[TurnHook] = ()
        interruption = Interruption()
        try:
            async with AsyncExitStack() as stack:
                stack.enter_context(profiles(admitted.request.model_profiles or {}))
                with interruption.protect():
                    if (
                        admitted.stateful
                        and admitted.request.parent is None
                        and admitted.request.predecessor is None
                    ):
                        await stack.enter_async_context(
                            environment_claim(
                                self.client,
                                admitted.context,
                                run_name(admitted.started_at, admitted.run_id),
                                clock=self.clock,
                                follow=self.project_policy(admitted.request.project),
                                followed=lambda revision: sink.note(
                                    f"the project's policy changed to {revision}; "
                                    "this run continues under it"
                                ),
                            )
                        )
                    if self.prepare_attempt is not None:
                        await stack.enter_async_context(self.prepare_attempt(admitted))
                    memory = await self.memory(admitted)
                    prepared = await self._prepare_context(admitted)
                    folder = prepared.folder if prepared is not None else None
                    view = prepared.view if prepared is not None else None
                    if view is not None:
                        # The session's view is the earlier conversation (CM-29).
                        request = dataclasses.replace(admitted.request, history=view.messages)
                        admitted = dataclasses.replace(admitted, request=request)
                    chat = self.chat(admitted)
                    if chat is not None and view is not None:
                        chat.shown = view.shown
                    delegations = await self._delegations(
                        admitted,
                        chat,
                        memory,
                        stop,
                        recovering=recovering,
                    )
                    toolbox = await self._toolbox(
                        admitted,
                        chat,
                        lambda close: closing(stack, interruption, close),
                        memory,
                        delegations,
                        stop,
                        prepared=prepared,
                    )
                    if toolbox.mailbox is not None:
                        child_wake = (
                            functools.partial(toolbox.resources.wake, toolbox.mailbox.wake)
                            if toolbox.resources is not None
                            else toolbox.mailbox.wake
                        )
                        if delegations is not None:
                            delegations.wake = child_wake
                        if work is not None:
                            work.wake = child_wake
                    hooks = toolbox.turn_hooks()
                    if work is not None:
                        owner = work.owner
                        for hook in hooks:
                            if isinstance(hook, KernelCheck):
                                journal = KernelJournal(
                                    work.records.control, owner, toolbox.kernel_run
                                )
                                hook.record = journal.record
                                hook.record_tool = journal.record_tool
                                hook.retain = lambda kernel: threaded(
                                    work.records.retains_kernel,
                                    owner.project,
                                    owner.session,
                                    kernel,
                                )
                        hooks = (*hooks, work)
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
                        hooks = (
                            *hooks,
                            *await self._chat_hooks(
                                admitted, chat, discovery, connectors=toolbox.connectors
                            ),
                        )
                    if view is not None:
                        hooks = (*hooks, ViewMeter(view))
                    if folder is not None and chat is not None:
                        hooks = (*hooks, FoldLifecycle(folder))
                        chat.context_stop = lambda: folder.stopped
                        folder.events = functools.partial(chat.write, "fold")
                        folder.refit = functools.partial(_refit, toolbox, discovery)
                        folder.specs = lambda: toolbox.specs
                    log = prepared.log if prepared is not None else None
                    if self.stores is not None and chat is not None:
                        resolved = dataclasses.replace(
                            request,
                            run_id=admitted.run_id,
                            memory_version=memory[0] if memory is not None else None,
                            max_turns=request.max_turns or self.settings.max_turns,
                            alias=self.role(request).alias,
                        )
                        hooks = (
                            RecordInputs(
                                RunInputs(self.stores.projects.control),
                                admitted.context,
                                chat.run,
                                cast(
                                    dict[str, JsonValue],
                                    TypeAdapter(RunRequest).dump_python(
                                        resolved, mode="json", exclude={"history", "stop_by"}
                                    ),
                                ),
                                cast(
                                    dict[str, JsonValue],
                                    TypeAdapter(Settings).dump_python(self.settings, mode="json"),
                                ),
                                predecessor=request.predecessor,
                                accept_changes=request.resume_source == "operator",
                                changed=sink.note,
                            ),
                            *hooks,
                        )
                    transcript = await self.loop_runner(
                        self._model(admitted),
                        toolbox,
                        None if request.queued else request.task,
                        run_id=admitted.run_id,
                        history=request.history,
                        stop=stop,
                        max_turns=request.max_turns or self.settings.max_turns,
                        max_input_chars=self._input_chars(folding=folder is not None),
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
                        folds=folder,
                    )
        except BaseException as error:
            if transcript is not None and user_cancelled(error, stop):
                pass  # it came as the run closed: the run's own outcome stands
            elif interruption.error is not None:
                raise interruption.error from None
            elif isinstance(error, WorkingMemoryLimitError):
                transcript = transcript or Transcript(context=AgentContext(messages=[]))
                transcript.stopped = "memory_limit"
                transcript.summaries["memory"] = {"blocked": str(error)}
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
            model_profiles=admitted.request.model_profiles,
            parent=admitted.request.parent,
            delegation=admitted.request.delegation,
            alias=self.role(admitted.request).alias,
            limits=self._limits(admitted.request),
        )

    async def _prepare_context(self, admitted: Admitted) -> PreparedContext | None:
        """Resolve role policy, then prepare the attempt's scoped history together."""
        stores, request, context = self.stores, admitted.request, admitted.context
        if stores is None:
            return None
        role = self.role(request)
        project, session = context.project_id, context.session_id

        def former(alias: str) -> ChatModel | None:
            if self.gateway is None or self.settings.skeleton_only:
                return None
            return cached_for(self.gateway.model(alias, role="fold").for_former(), project, session)

        name = run_name(admitted.started_at, admitted.run_id)
        return await prepare_context(
            HistoryScope(
                context,
                name,
                request.parent,
                request.message_ids or ((operator_ref(name),) if request.operator else ()),
            ),
            projects=stores.projects,
            documents=stores.documents,
            run_state=stores.runs,
            limits=FoldLimits(
                await self._ceiling(role),
                self.settings.chunk,
                self.settings.shares,
                request.consolidate,
            ),
            alias=role.alias,
            brief=request.task,
            delegation=request.delegation,
            narrator=(
                self._child_narrator(request, context)
                if request.parent is not None
                else self._narrator(project, session, request)
            ),
            former_model=former,
            learned=self._learned,
            observe_kernels=lambda context: self.client.observe_kernels(context),
            clock=self.clock,
        )

    async def _ceiling(self, role: Role | None = None) -> int:
        """MAIN's ceiling C (CM-27): the role's, capped by the model's limit: the setting's
        input limit, or the gateway's model information's. For a Claude model that is a
        window input and output share, so the answer's room comes off it. A gateway that
        cannot be asked is asked again at the next run's start."""
        settings = self.settings
        role = role or self.main
        if settings.input_limit is not None:
            return ceiling(settings.ceiling, input_limit=settings.input_limit)
        if self.gateway is not None and self._limits_seen is None:
            self._limits_seen = await self.gateway.input_limits()
        found = (self._limits_seen or {}).get(role.alias)
        if found is None:
            return settings.ceiling
        model = role.model
        named = " ".join(name or "" for name in (found.provider, found.model, role.alias))
        if isinstance(model, MessagesModel) or "anthropic" in named or "claude" in named.lower():
            output = model.max_output if isinstance(model, GatewayModel) else None
            return ceiling(settings.ceiling, window=found.input, max_output=output or found.output)
        return ceiling(settings.ceiling, input_limit=found.input)

    def _child_narrator(self, request: RunRequest, context: ExecutionContext) -> ChatModel | None:
        if self.settings.skeleton_only:
            return None
        profile = (request.model_profiles or PROFILES.get() or {}).get("fold")
        alias = profile.alias if profile else self.settings.fold_alias
        if alias is not None and self.gateway is not None:
            model = self.gateway.model(alias, role="fold")
        else:
            own = self.role(request).model
            if not isinstance(own, GatewayModel):
                return None
            return cast(
                GatewayModel, cached_for(own, context.project_id, context.session_id)
            ).for_role("fold")
        return cached_for(model, context.project_id, context.session_id)

    def _narrator(self, project: str, session: str, request: RunRequest) -> ChatModel | None:
        """The model that writes MAIN's folds' narratives: the setting's alias through
        the gateway, or MAIN's own; none for skeleton-only folds, or for a model that is
        not the gateway's, as a scripted one, whose answers are the run's (CM-19). It
        calls as the session does, so that it reads the run's view from the same prompt
        cache (CM-32)."""
        if self.settings.skeleton_only:
            return None
        profile = (request.model_profiles or {}).get("fold")
        alias = profile.alias if profile is not None else self.settings.fold_alias
        if alias is not None and self.gateway is not None:
            return cached_for(self.gateway.model(alias, role="fold"), project, session)
        main = self.role(request)
        if isinstance(main.model, GatewayModel):
            return cast(GatewayModel, cached_for(main.model, project, session)).for_role("fold")
        return None

    def _input_chars(self, *, folding: bool) -> int:
        """The longest conversation a run is sent, in characters: the setting's, or by
        default ``MAX_INPUT_CHARS`` for a run that does not fold and no limit of its own
        for one that does, whose ceiling is in tokens (CM-27)."""
        if self.settings.max_input_chars is not None:
            return self.settings.max_input_chars
        return FOLDING_CHARS if folding else MAX_INPUT_CHARS

    def _limits(self, request: RunRequest) -> dict[str, JsonValue]:
        """The limits a run runs under, as its record keeps them (PD-29)."""
        found: dict[str, JsonValue] = {
            "max_turns": request.max_turns or self.settings.max_turns,
            "max_input_chars": (
                None
                if self.settings.max_input_chars is None and self.stores is not None
                else self._input_chars(folding=False)
            ),
        }
        if request.tokens is not None:
            found["tokens"] = request.tokens
        elif self.settings.run_share is not None and self.stores is not None:
            found["run_share"] = self.settings.run_share  # of the daily cost limit
        return found

    async def _chat_hooks(
        self,
        admitted: Admitted,
        chat: RunChat,
        discovery: SkillDiscovery | None = None,
        *,
        recovering: bool = False,
        connectors: Connectors | None = None,
    ) -> tuple[TurnHook, ...]:
        """The user's messages, which reach the model last, and the run's events, which
        come last. No hook keeps a budget (D102): a configured project cost limit is the
        model-call journal's, and a delegated run's tokens are a soft cap its resource
        check-ins keep. A delegated run's calls that may change something need its root's
        lease and the plan's leave (PD-34); the user writes to the root."""
        request = admitted.request
        if request.parent is not None and self.stores is not None:
            fence = await threaded(
                ChildFence,
                SessionPlans(self.stores.documents),
                self.stores.projects,
                admitted.context,
                run_id=admitted.run_id,
                granted=request.track[0] if request.track is not None else None,
                # It reads each connector tool's class as the proxy listed it (D99).
                connectors=connectors.tools.get if connectors is not None else None,
            )
            # Its result is read from its closing answer once every other hook has had its say.
            outcome = Outcome(request.target, task=request.target is None)
            return (SessionHeld(chat), fence, outcome, EventLog(chat))
        discover = discovery.about if discovery is not None else None
        return (UserMessages(chat, discover), EventLog(chat))

    async def _delegations(
        self,
        admitted: Admitted,
        chat: RunChat | None,
        memory: Snapshot | None,
        stop: asyncio.Event | None,
        *,
        recovering: bool = False,
    ) -> Delegations | None:
        """A main run's delegations: their handbacks always, and the delegate tools when
        its project lets its runs delegate (PD-33, PD-35); and a delegated task's own, when
        its main run let it delegate once more (PD-15): tasks on no track."""
        stores, context, clock = self.stores, admitted.context, self.clock
        request = admitted.request
        if stores is None or (request.parent is not None and not request.may_delegate):
            return None
        records = DelegationRecords(stores.documents, clock=clock)
        limits = await threaded(records.settings, context.project_id)
        # A delegated task its main run allowed delegates once more (PD-15).
        depth = 0 if request.parent is None else 1

        async def waiting(ids: Sequence[str] | None) -> None:
            if chat is not None:
                await chat.write("waiting", {"delegation_ids": ids, "mode": "all" if ids else None})

        # Every run of the tree is given the memory its root was (PM-8).
        version = memory[0] if memory is not None else None
        return Delegations(
            records,
            Ledger(stores.documents),
            context,
            parent=run_name(admitted.started_at, admitted.run_id),
            work=request.work if request.predecessor is not None else None,
            runner=functools.partial(self.run_child, admitted, version),
            blueprint=lambda child: self._blueprint(self._child_admitted(admitted, version, child)),
            max_turns=request.max_turns or self.settings.max_turns,
            offer=limits.depth >= 1 + depth,
            wake=chat.arrived if chat is not None and depth == 0 else None,
            waiting=waiting,
            limits=limits,
            refresh_settings=lambda: threaded(records.settings, context.project_id),
            models=ModelChoice(
                (
                    (request.model_profiles or {})["delegated"].alias
                    if "delegated" in (request.model_profiles or {})
                    else self.delegated.alias
                ),
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
            stop_by=request.stop_by,
            stop=stop,
            recovering=recovering,
            clock=clock,
        )

    def _blueprint(self, admitted: Admitted) -> Blueprint:
        return Blueprint(
            context=admitted.context,
            request=cast(
                dict[str, JsonValue],
                TypeAdapter(RunRequest).dump_python(
                    dataclasses.replace(admitted.request, alias=self.role(admitted.request).alias),
                    mode="json",
                    exclude={"history", "stop_by"},
                ),
            ),
        )

    def _child_admitted(
        self, parent: Admitted, memory_version: int | None, child: Child
    ) -> Admitted:
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
            model_profiles=dict(parent.request.model_profiles or {}),
        )
        profiles_for_child = dict(request.model_profiles or {})
        if child.alias is not None:
            previous = profiles_for_child.get("delegated")
            if self.gateway is None and child.alias != self.delegated.alias:
                raise ModelError(f"no model gateway serves {child.alias!r}")
            if self.gateway is not None:
                profiles_for_child["delegated"] = self.gateway.profile(
                    child.alias, role="delegated"
                ).model_copy(
                    update={"reasoning_effort": previous.reasoning_effort if previous else None}
                )
        if self.gateway is not None:
            # A child's narrative summaries use its selected model unless the deployment
            # names a dedicated folding model. The parent's effort pins remain immutable.
            delegated = profiles_for_child.get("delegated")
            alias = self.settings.fold_alias or (
                delegated.alias if delegated is not None else self.delegated.alias
            )
            for role in ("fold", "overview"):
                key = cast(ModelRole, role)
                previous = profiles_for_child.get(key)
                profiles_for_child[key] = self.gateway.profile(alias, role=key).model_copy(
                    update={"reasoning_effort": previous.reasoning_effort if previous else None}
                )
        request = dataclasses.replace(request, model_profiles=profiles_for_child)
        return dataclasses.replace(
            parent, request=request, run_id=child.run_id, started_at=child.started_at
        )

    async def run_child(
        self, parent: Admitted, memory_version: int | None, child: Child, stop: asyncio.Event
    ) -> ChildEnd:
        """Run a delegated run of ``parent`` to its end and save its record (PD-34): in
        the parent's session and environment, with the memory version its root was
        given (PM-8). Cleanup saves partial evidence; only an explicit Stop ends it
        ``cancelled``, while parent failure or lease loss ends it ``interrupted``."""
        if self.stores is None:
            raise ValueError("a delegated run needs the state directory's stores")
        admitted = self._child_admitted(parent, memory_version, child)
        sink = _ChildSink(child.run_id)
        finished = await self.start(admitted, sink, stop=stop)
        if finished.transcript.stopped == "cancelled" and child.deadline.interrupted:
            finished.transcript.stopped = "interrupted"
        completed = await self.finalize(finished, error=sink.error)
        if completed.child is None:
            raise RuntimeError("a delegated run completed without a child settlement")
        return completed.child

    def finalizer(self) -> AttemptFinalizer:
        """The durable attempt protocol, shared by fresh and resumed runs."""
        if self.stores is None:
            raise ValueError("attempt finalization requires durable stores")
        return AttemptFinalizer(
            self.stores.projects,
            self.stores.documents,
            self.client,
            close_timeout=KERNEL_CLOSE_SECONDS,
        )

    async def finalize(
        self, finished: Finished, *, error: Callable[[str], None]
    ) -> AttemptCompletion:
        """Record and acknowledge a run, settling delegated evidence before cleanup."""
        end = (
            self.child_outcome(finished, finished.status)
            if finished.delegation is not None
            else None
        )
        return await self.finalizer().finish(finished, end=end, error=error)

    async def checkpoint_child(self, finished: Finished) -> ChildSettlement:
        """Save typed validation evidence before final persistence can complete work."""
        return await self.finalizer().checkpoint(
            finished, self.child_outcome(finished, finished.status)
        )

    async def release_child(self, finished: Finished) -> None:
        """Release only completed/stopped child kernels after durable finalization."""
        if self.stores is not None:
            await self.finalizer().release(finished)

    async def release_completed_children(self, context: ExecutionContext, root: str) -> None:
        """Reconcile terminal child kernels under the current Resume root's authority."""
        await self.finalizer().release_completed(context, root)

    def child_outcome(self, finished: Finished, saved_status: int) -> ChildEnd:
        """Typed durable handback shared by first and continued child attempts."""
        transcript = finished.transcript
        if saved_status == 1 and transcript.failure is None:
            # Saving or acknowledging failed after the child computed its answer.
            # Preserve its report, but never advertise an unsettled result as returned.
            return ChildEnd(
                "persistence_error",
                True,
                transcript.final_text,
                errors=("the child's record or delivery acknowledgements could not be saved",),
                alias=finished.alias or self.delegated.alias,
            )
        fence = next((hook for hook in finished.hooks if isinstance(hook, ChildFence)), None)
        found = next((hook for hook in finished.hooks if isinstance(hook, Outcome)), None)
        failed = transcript.failure is not None
        errors = tuple(found.errors) if found is not None else ()
        if transcript.failure is not None:
            # The exception may contain gateway data or credentials. Hand the parent
            # the trusted failure phase, not an exception string or a false result.
            errors = (*errors, f"the child's {transcript.failure} phase failed")
        # A result stands only for a run that ended with that answer.
        result = result_of(found, transcript.stopped, failed) if found is not None else None
        return ChildEnd(
            transcript.stopped,
            failed,
            transcript.final_text,
            fence.refused if fence is not None else None,
            result.model_dump(mode="json") if result is not None else None,
            errors,
            finished.alias or self.delegated.alias,
            tuple(dict.fromkeys(u.model for u in transcript.context.usage if u.model is not None))[
                :10
            ],
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

    def _on_approval(
        self, chat: RunChat | None, admitted: Admitted
    ) -> Callable[[dict[str, object] | None], Awaitable[None]] | None:
        """What a run's connector calls report as they wait for the user: the chat's event,
        and for a delegated run its attributed request and its parent's notice (D99)."""
        if chat is None:
            return None
        delegation = admitted.request.delegation
        if delegation is None or self.stores is None:
            return chat.approval
        control = self.stores.projects.control
        return ChildApproval(
            chat,
            ChildRequests(control, clock=self.clock),
            AgentMessageStore(control, clock=self.clock),
            admitted.context,
            delegation=delegation,
        )

    async def _toolbox(
        self,
        admitted: Admitted,
        chat: RunChat | None,
        on_close: Callable[[Callable[[], Awaitable[object]]], None],
        memory: Snapshot | None = None,
        delegations: Delegations | None = None,
        stop: asyncio.Event | None = None,
        *,
        prepared: PreparedContext | None,
    ) -> Toolbox:
        """The run's tools; ``on_close`` takes what closes each part the run made,
        ``memory`` is the project memory the run was given, which its reviewer is too, and
        ``stop`` is set when the user cancels the run.

        A delegated run gets its parent's tools without the review tools, the plan tools,
        the delegate tools and ``ask_user``, and a check only the reads of the ledger, the
        documents and the memory, and of the connectors only the tools the proxy classes
        ``read``; it is not reviewed, and the user's messages do not end its job waits
        (PD-34, DA-1, DA-3)."""
        stores, settings = self.stores, self.settings
        alias = self.role(admitted.request).alias
        watcher, reviewer = self.watcher, self.reviewer_role(admitted.request)
        context, run_id = admitted.context, admitted.run_id
        languages, features = admitted.languages, admitted.features
        if "python" not in languages:
            # Rare but silent otherwise: the agent then computes only through shell.
            _log.warning(
                "run %s: %s", admitted.run_id, kernel_note(languages, admitted.kernel_reason)
            )
        role = admitted.request.role
        delegated = role == "delegated"
        bundles: list[caps.Capability] = []
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
            job_profiles=admitted.job_profiles,
            min_isolation_tier=settings.min_isolation_tier,
        )
        if "jobs" in features:
            bundles.append(caps.jobs(jobs))
        connectors = None
        if self.mcp is not None and self.mcp.project == context.project_id:
            # Listed at the run's start: a server added later is offered to the next run.
            # The servers the project's user turned off are not offered (narrowing only).
            # A delegated run's calls go through the same client under the same policy,
            # each naming its own run (DA-3, DA-4).
            settings_of = (
                functools.partial(ProjectConnectors(stores.documents).disabled, context.project_id)
                if stores is not None
                else None
            )
            off: frozenset[str] = (
                await asyncio.to_thread(settings_of) if settings_of is not None else frozenset()
            )
            connectors = Connectors(
                self.mcp.client,
                [server for server in self.mcp.servers if server not in off],
                session=context.session_id,
                run=run_name(admitted.started_at, admitted.run_id),
                reads_only=delegated and admitted.request.target is not None,
                about=self.mcp.about,
                shown=self.mcp.shown,
                disabled=settings_of,
                on_approval=self._on_approval(chat, admitted),
                # Binary results a direct client gets become artifacts (D98); the proxy's
                # answers carry none.
                binaries=ArtifactBinaries(self.client, context),
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
                capabilities=caps.main(bundles),
                connectors=connectors,
            )
        if prepared is None:
            raise ValueError("stateful tools require prepared agent context")
        if delegations is not None:
            bundles.append(caps.delegations(delegations))
        accepts, why = await self._images(alias)
        viewer = None
        if accepts:
            viewer = ImageViewer(ArtifactInventory(stores.documents), context, self.client)
            bundles.append(caps.images(viewer, on_close))
        documents = DocumentTool(stores.documents, context, run_id=run_id, role=role, alias=alias)
        bundles.append(caps.documents(documents))
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
        bundles.append(caps.ledger(ledger))
        # Main and delegated runs propose; only the user changes the memory (PM-3).
        memory_tool = MemoryTool(
            ProjectMemory(stores.documents),
            context,
            run_id=run_id,
            role=role,
            alias=alias,
        )
        bundles.append(caps.memory(memory_tool))
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
        if skills is not None:
            # A loaded skill's helpers are staged in the session's environment (SK-16).
            skills.stager = Stager(self.client, context, skills.store, self.derived)
        if skills is not None and self.vectors is not None:
            # Reads the vectors the store has; the gateway's work goes on in the background.
            await skills.use(self.vectors, context.project_id, clock=self.clock)
        if skills is not None:
            bundles.append(caps.skills(skills))
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
                images=await self._reviewer_images(inventory, context, reviewer),
            )
            bundles.append(caps.reviews(reviews, on_close))
        artifacts = ArtifactPublisher(inventory, self.client, context, run_id=run_id, alias=alias)
        bundles.append(caps.artifacts(artifacts, on_close))
        if connectors is not None:
            # Kept binary results enter the inventory as the agent's publications do.
            connectors.binaries = ArtifactBinaries(self.client, context, artifacts)
        await artifacts.drain_retention()
        recorder = FileRecorder(
            artifacts,
            ProjectFiles(stores.documents),
            role=role,
            parent_run=admitted.request.parent,
            delegation=admitted.request.delegation,
        )
        bundles.append(caps.files(recorder))
        jobs.recorder = ledger.recorder = recorder
        math_checks = MathController(
            review_store, context.project_id, context.session_id, run_id, checker=self.checker
        )
        bundles.append(caps.math(math_checks))
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
            bundles.append(caps.plans(plans))
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
            bundles.append(caps.questions(questions))
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
            bundles.append(caps.tracks(tracks))
        kernel_run = (
            (
                run_id_of(admitted.request.work)
                if admitted.request.work is not None
                else admitted.run_id
            )
            if admitted.request.parent is not None
            else None
        )
        mailbox = AgentMailbox(
            AgentMessageStore(stores.projects.control, clock=self.clock),
            context,
            run=run_name(admitted.started_at, admitted.run_id),
            delegation=admitted.request.delegation,
            may_message_child=delegations is not None and bool(delegations.specs),
            user_arrived=(
                chat.arrived if chat is not None and admitted.request.delegation is None else None
            ),
            requests=ChildRequests(stores.projects.control, clock=self.clock),
            clock=self.clock,
        )
        resources = (
            Resources(
                ResourceRequests(stores.projects.control, clock=self.clock),
                Ownership(
                    project=context.project_id,
                    session=context.session_id,
                    work=context.authority.work,
                    run=context.authority.run,
                    epoch=context.authority.epoch,
                ),
                turns=admitted.request.max_turns or self.settings.max_turns,
                tokens=admitted.request.tokens,
                delegated=delegated,
                inventory=inventory,
                clock=self.clock,
                resume_source=admitted.request.resume_source,
            )
            if context.authority is not None
            else None
        )
        bundles.append(caps.mailbox(mailbox))
        if resources is not None:
            bundles.append(caps.resources(resources))
        bundles.append(caps.history(prepared.retrieval))
        selected = (
            caps.main(bundles, read_only_records=admitted.request.target is not None)
            if not delegated
            else caps.check(bundles)
            if admitted.request.target is not None
            else caps.task(bundles)
        )
        return Toolbox(
            self.client,
            context,
            languages=languages,
            features=features,
            check_kernels=True,
            sweep_left_kernels=kernel_run is None,
            connectors=connectors,
            kernel_run=kernel_run,
            delegated=delegated,
            capabilities=selected,
            image_offer={"offered": accepts, "why": why},
        )

    async def _reviewer_images(
        self, inventory: ArtifactInventory, context: ExecutionContext, reviewer: Role | None
    ) -> ImageViewer | None:
        """The reviewer's viewer, when its alias accepts images (FG-39). What it draws
        is the platform's execution, never the reviewer's (CR-2, CR-F8)."""
        if reviewer is None or not (await self._images(reviewer.alias))[0]:
            return None
        return ImageViewer(inventory, context.model_copy(update={"actor": "platform"}), self.client)

    async def _images(self, alias: str) -> tuple[bool, str]:
        """Whether a run on ``alias`` is offered ``view_image``, and why (FG-33): the
        deployment declares the alias, or the gateway says every deployment of it accepts
        images. An alias the gateway does not describe, or a gateway that cannot say,
        is not offered it."""
        if alias in self.settings.image_aliases:
            return True, "declared"
        if self.gateway is None:
            return False, "no_gateway"
        accepts = await self.gateway.image_input()
        if accepts is None:
            return False, "gateway_did_not_answer"
        if alias not in accepts:
            return False, "not_described"
        return accepts[alias], "gateway" if accepts[alias] else "gateway_says_no"


def _refit(toolbox: Toolbox, discovery: SkillDiscovery | None, view: View) -> None:
    """After a fold: the skill bodies the rebuilt view still holds are loaded, and the
    skills its discovery notices showed are shown; others load and show again (CM-29b)."""
    if toolbox.skills is not None:
        toolbox.skills.refit(view.loaded)
    if discovery is not None:
        discovery.shown.clear()
        discovery.shown.update(view.discovered)


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
