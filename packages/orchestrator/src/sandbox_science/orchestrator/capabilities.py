# SPDX-License-Identifier: MIT
"""Ordinary run capabilities: tools, ordered lifecycle adapters and resource ownership."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import cast

from .agent_messages import (
    CHILD_SPEC,
    PARENT_SPEC,
    STATUS_SPEC,
    WAIT_SPEC,
    WITHDRAW_SPEC,
    AgentMailbox,
)
from .aio import threaded
from .artifact_publisher import ArtifactPublisher
from .consistency.controller import AUTHOR_SPECS, REVIEW_EFFECTS, ReviewController, Reviews
from .context.history import SPECS as HISTORY_SPECS
from .context.history import HistoryTool
from .delegation import specs as delegation_specs
from .delegation.hook import Delegations
from .delegation.tracks import EFFECTS as TRACK_EFFECTS
from .delegation.tracks import SPECS as TRACK_SPECS
from .delegation.tracks import TrackTools
from .documents import annotation_tools
from .documents.tool import DOCUMENT_EFFECTS, DocumentTool, WorkRequests
from .documents.tool import SPECS as DOCUMENT_SPECS
from .hooks import TurnHook
from .jobs import JOB_EFFECTS, JOB_SPECS, JobNotices, JobTools
from .ledger.tool import LEDGER_EFFECTS, LedgerTool
from .ledger.tool import PROPOSALS as LEDGER_PROPOSALS
from .ledger.tool import SPECS as LEDGER_SPECS
from .mathchecks.controller import MathChecks, MathController
from .memory_tools import MEMORY_EFFECTS, MemoryTool
from .memory_tools import SPECS as MEMORY_SPECS
from .model import ToolSpec
from .plan_tools import SPECS as PLAN_SPECS
from .plan_tools import Plans, PlanTools
from .question_tool import EFFECTS as QUESTION_EFFECTS
from .question_tool import SPECS as QUESTION_SPECS
from .question_tool import QuestionTool
from .recording import FileRecorder, RecordedFiles
from .resource_hooks import SPEC as RESOURCE_SPEC
from .resource_hooks import Resources
from .skills.tool import SPEC as SKILL_SPEC
from .skills.tool import SkillTool
from .tool_entry import Request, ToolEntry, ToolOutcome, group
from .view_image import SPEC as VIEW_IMAGE
from .view_image import ImageViewer

Close = Callable[[], Awaitable[object]]
OnClose = Callable[[Close], object]
DEFAULT_OUTPUT_CHARS = 20_000


@dataclass(frozen=True)
class Capability:
    """One admitted domain, with its entries, fresh hook factory and owned resource."""

    name: str
    order: int
    hook_order: int | None
    service: object
    entries: tuple[ToolEntry, ...]
    hook: Callable[[object, int], TurnHook] | None = None
    close: Close | None = None
    main_only: bool = False


@dataclass(frozen=True)
class Definition[T]:
    """A built-in domain's identity, reservation and observable assembly positions."""

    name: str
    order: int
    specs: tuple[ToolSpec, ...] = ()
    hook_order: int | None = None
    hook: Callable[[T, int], TurnHook] | None = None
    main_only: bool = False

    def bind(
        self,
        service: T | None,
        entries: Sequence[ToolEntry] = (),
        *,
        hook: Callable[[T, int], TurnHook] | None = None,
        close: Close | None = None,
        on_close: OnClose | None = None,
    ) -> Capability:
        """Bind a scoped controller, registering cleanup before further setup can fail."""
        if close is not None and on_close is not None:
            on_close(close)
        hook = self.hook if hook is None else hook
        return Capability(
            self.name,
            self.order,
            self.hook_order,
            service,
            tuple(entries),
            (lambda current, output_chars: hook(cast(T, current), output_chars))
            if hook is not None
            else None,
            close,
            self.main_only,
        )


# Entry order and hook order are independently observable. Reserved specs include
# role-specific variants so connectors cannot occupy a name absent from this run.
JOBS = Definition[JobTools](
    "jobs",
    10,
    tuple(JOB_SPECS),
    20,
    lambda current, output_chars: JobNotices(current, max_chars=output_chars // 4),
)
DOCUMENTS = Definition[DocumentTool](
    "documents",
    20,
    (*DOCUMENT_SPECS, *annotation_tools.SPECS),
    10,
    lambda current, _: WorkRequests(current),
)
REVIEWS = Definition[ReviewController](
    "reviews", 30, tuple(AUTHOR_SPECS), 80, lambda current, _: Reviews(current), True
)
PLANS = Definition[PlanTools](
    "plans", 40, tuple(PLAN_SPECS), 70, lambda current, _: Plans(current), True
)
LEDGER = Definition[LedgerTool]("ledger", 50, (*LEDGER_SPECS, LEDGER_PROPOSALS))
MEMORY = Definition[MemoryTool]("memory", 60, tuple(MEMORY_SPECS))
TRACKS = Definition[TrackTools]("tracks", 70, tuple(TRACK_SPECS))
DELEGATIONS = Definition[Delegations](
    "delegations", 80, tuple(delegation_specs.SPECS), 30, lambda current, _: current
)
RESOURCES = Definition[Resources]("resources", 90, (RESOURCE_SPEC,), 50, lambda current, _: current)
MAILBOX = Definition[AgentMailbox](
    "mailbox",
    100,
    (CHILD_SPEC, PARENT_SPEC, STATUS_SPEC, WAIT_SPEC, WITHDRAW_SPEC),
    40,
    lambda current, _: current,
)
SKILLS = Definition[SkillTool]("skills", 110, (SKILL_SPEC,))
HISTORY = Definition[HistoryTool]("history", 120, tuple(HISTORY_SPECS))
IMAGES = Definition[ImageViewer]("images", 130, (VIEW_IMAGE,))
QUESTIONS = Definition[QuestionTool](
    "questions", 140, tuple(QUESTION_SPECS), 60, lambda current, _: current, True
)
MATH = Definition[MathController](
    "math_checks", 150, hook_order=90, hook=lambda current, _: MathChecks(current)
)
KERNELS = Definition[Callable[[], TurnHook]](
    "kernels", 160, hook_order=100, hook=lambda factory, _: factory()
)
FILES = Definition[FileRecorder](
    "recorder", 170, hook_order=110, hook=lambda current, _: RecordedFiles(current)
)
ARTIFACTS = Definition[ArtifactPublisher]("artifacts", 180)

DEFINITIONS = (
    JOBS,
    DOCUMENTS,
    REVIEWS,
    PLANS,
    LEDGER,
    MEMORY,
    TRACKS,
    DELEGATIONS,
    RESOURCES,
    MAILBOX,
    SKILLS,
    HISTORY,
    IMAGES,
    QUESTIONS,
    MATH,
    KERNELS,
    FILES,
    ARTIFACTS,
)
RESERVED_NAMES = frozenset(spec.name for definition in DEFINITIONS for spec in definition.specs)


@dataclass(frozen=True)
class CapabilitySet:
    """The selected domains; no parallel mutable controller configuration."""

    bundles: tuple[Capability, ...]
    allow_main: bool = True

    def with_hook_slots(self) -> CapabilitySet:
        """Retain absent controller slots without granting excluded role capabilities."""
        names = {bundle.name for bundle in self.bundles}
        missing = tuple(
            definition.bind(None)
            for definition in DEFINITIONS
            if definition.hook is not None
            and definition.name not in names
            and (self.allow_main or not definition.main_only)
        )
        return replace(self, bundles=(*self.bundles, *missing))

    @property
    def entries(self) -> tuple[ToolEntry, ...]:
        return tuple(
            entry
            for bundle in sorted(self.bundles, key=lambda b: b.order)
            for entry in bundle.entries
        )

    def service[T](self, definition: Definition[T]) -> T | None:
        """Read a controller through the same typed definition that bound it."""
        return next(
            (cast(T, bundle.service) for bundle in self.bundles if bundle.name == definition.name),
            None,
        )

    def turn_hooks(
        self,
        resolve: Callable[[str, object], object | None] | None = None,
        *,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> tuple[TurnHook, ...]:
        """New adapters in notice order, optionally resolving current compatibility fields."""
        hooks: list[TurnHook] = []
        for bundle in sorted(self.bundles, key=lambda b: b.hook_order or 0):
            if bundle.hook is None:
                continue
            service = bundle.service if resolve is None else resolve(bundle.name, bundle.service)
            if service is not None:
                hooks.append(bundle.hook(service, output_chars))
        return tuple(hooks)


def main(bundles: Iterable[Capability], *, read_only_records: bool = False) -> CapabilitySet:
    """Select capabilities constructed for a trusted main-agent scope."""
    selected = CapabilitySet(tuple(bundles))
    return record_reads(selected) if read_only_records else selected


def task(bundles: Iterable[Capability]) -> CapabilitySet:
    """Select delegated task capabilities, withholding all main-only domains."""
    return CapabilitySet(tuple(b for b in bundles if not b.main_only), allow_main=False)


def record_reads(selected: CapabilitySet) -> CapabilitySet:
    """Narrow document, ledger and memory operations to classified reads."""
    return replace(
        selected,
        bundles=tuple(
            replace(b, entries=tuple(e for e in b.entries if e.effect == "read"))
            if b.name in {DOCUMENTS.name, LEDGER.name, MEMORY.name}
            else b
            for b in selected.bundles
        ),
    )


def check(bundles: Iterable[Capability]) -> CapabilitySet:
    """Select delegated checks: record stores contribute reads only."""
    return record_reads(task(bundles))


def documents(tool: DocumentTool) -> Capability:
    """Bind document operations and work-request delivery."""
    return DOCUMENTS.bind(
        tool,
        group(
            tool.specs,
            lambda name, call: tool.call(
                name, call.args, operation=call.operation, call_id=call.call.id, model=call.model
            ),
            DOCUMENT_EFFECTS,
        ),
    )


def jobs(tool: JobTools | None) -> Capability:
    """Bind background job operations and their bounded notices."""
    return JOBS.bind(
        tool,
        group(
            JOB_SPECS,
            lambda name, call: tool.handlers[name](call.args, call.operation),
            JOB_EFFECTS,
            bound=True,
        )
        if tool is not None
        else (),
    )


def reviews(controller: ReviewController, on_close: OnClose | None = None) -> Capability:
    """Own review cleanup before binding author tools and review delivery."""
    bundle = REVIEWS.bind(
        controller,
        close=lambda: controller.close(),
        on_close=on_close,
    )
    return replace(
        bundle,
        entries=tuple(
            group(
                controller.specs,
                lambda name, call: controller.author_call(
                    name, call.args, call.operation, model=call.model
                ),
                REVIEW_EFFECTS,
                bound=True,
            )
        ),
    )


def plans(tool: PlanTools) -> Capability:
    """Bind raw plan operations with their required plan fence."""
    return PLANS.bind(
        tool,
        group(
            tool.specs,
            lambda name, call: tool.call(
                name, call.call.arguments, call_id=call.call.id, model=call.model
            ),
            {spec.name: "plan" for spec in tool.specs},
            raw=True,
            whole=True,
        ),
    )


def ledger(tool: LedgerTool) -> Capability:
    """Bind ledger operations with explicit effects."""
    return LEDGER.bind(
        tool,
        group(
            tool.specs,
            lambda name, call: tool.call(
                name, call.args, operation=call.operation, call_id=call.call.id, model=call.model
            ),
            LEDGER_EFFECTS,
        ),
    )


def memory(tool: MemoryTool) -> Capability:
    """Bind memory reads and proposals with explicit effects."""
    return MEMORY.bind(
        tool,
        group(
            tool.specs,
            lambda name, call: tool.call(
                name, call.args, operation=call.operation, call_id=call.call.id, model=call.model
            ),
            MEMORY_EFFECTS,
        ),
    )


def tracks(tool: TrackTools) -> Capability:
    """Bind scoped track operations through the existing thread boundary."""
    return TRACKS.bind(
        tool,
        group(
            tool.specs,
            lambda name, call: threaded(
                tool.call, name, call.args, operation=call.operation, model=call.model
            ),
            TRACK_EFFECTS,
        ),
    )


def delegations(controller: Delegations) -> Capability:
    """Bind child operations and the controller that delivers their outcomes."""
    return DELEGATIONS.bind(
        controller,
        group(
            controller.specs,
            lambda name, call: controller.call(name, call.args, operation=call.operation),
            delegation_specs.EFFECTS,
        ),
    )


def resources(controller: Resources) -> Capability:
    """Bind resource requests and their delivery hook."""
    return RESOURCES.bind(
        controller,
        group(
            controller.specs,
            lambda name, call: controller.call(name, call.args, call.operation),
            {spec.name: "read" for spec in controller.specs},
        ),
    )


def mailbox(controller: AgentMailbox) -> Capability:
    """Bind agent messaging operations and their delivery hook."""
    return MAILBOX.bind(
        controller,
        group(
            controller.specs,
            lambda name, call: controller.call(name, call.args, call.operation),
            # A withdrawal only stops a child's call, as cancelling a job does.
            {
                spec.name: "cancel" if spec.name == WITHDRAW_SPEC.name else "read"
                for spec in controller.specs
            },
        ),
    )


def skills(tool: SkillTool) -> Capability:
    """Bind whole skill bodies; the same controller supplies prompt pins."""
    return SKILLS.bind(
        tool,
        group(
            tool.specs,
            lambda _, call: tool.call(call.args, call_id=call.call.id, model=call.model),
            {SKILL_SPEC.name: "read"},
            whole=True,
        ),
    )


def history(tool: HistoryTool) -> Capability:
    """Bind scoped history pages that reach the model whole."""
    # Retrieval pages and loaded skill bodies reach the model without attached notices.
    return HISTORY.bind(
        tool,
        group(
            tool.specs,
            lambda name, call: tool.call(name, call.args),
            {spec.name: "read" for spec in tool.specs},
            whole=True,
        ),
    )


async def _view(viewer: ImageViewer, request: Request) -> ToolOutcome:
    viewed = await viewer.call(request.args, request.operation)
    image = None
    if viewed.notice is not None and viewed.token is not None:
        image = (viewed.notice, viewed.token)
    return ToolOutcome(viewed.text, viewed.record, image=image)


def images(viewer: ImageViewer, on_close: OnClose | None = None) -> Capability:
    """Bind image viewing and immediately own its cached images."""

    async def close() -> None:
        viewer.close()

    return IMAGES.bind(
        viewer,
        (ToolEntry(VIEW_IMAGE, "read", partial(_view, viewer)),),
        close=close,
        on_close=on_close,
    )


def questions(controller: QuestionTool) -> Capability:
    """Bind user questions and their notice source."""
    return QUESTIONS.bind(
        controller,
        group(
            controller.specs,
            lambda _, call: controller.call(
                call.args, operation=call.operation, call_id=call.call.id
            ),
            QUESTION_EFFECTS,
        ),
    )


def math(controller: MathController) -> Capability:
    """Bind background mathematical checks to their lifecycle adapter."""
    return MATH.bind(controller)


def files(recorder: FileRecorder) -> Capability:
    """Bind artifact recording to the written-file lifecycle adapter."""
    return FILES.bind(recorder)


def artifacts(publisher: ArtifactPublisher, on_close: OnClose | None = None) -> Capability:
    """Own an artifact publisher used by worker file operations."""
    return ARTIFACTS.bind(publisher, close=lambda: publisher.aclose(), on_close=on_close)
