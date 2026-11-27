# SPDX-License-Identifier: MIT
"""Ordinary stateful MAIN planning, with current-read and local dispatch fences.

The adapter owns no scheduler or approval authority. Short project-lock -> control
SQL calls are cancellation-owned; a worker await never holds either lock.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, Self, TypeVar, cast

from pydantic import Field, ValidationError, model_validator

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, Identifier

from .aio import threaded
from .artifacts import ArtifactInventory, scoped_path
from .continuity.auto_continuation import claim_in_run, continued
from .documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from .hooks import Closing, Effect, Notice, Point, Refusal, RunView, TurnHook
from .model import ToolSpec
from .notices import PLANS, PLANS_HELD, PLANS_WENT_ON
from .projects import ProjectError, ProjectStore, SessionSnapshot
from .questions import Questions
from .session_plans import (
    FINISHED,
    MAX_PROGRESS_STEPS,
    CompactPlan,
    CompactStep,
    Counter,
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanEvent,
    PlanEvidence,
    PlanModel,
    PlanProgress,
    PlanReceipt,
    PlanRef,
    PlanScope,
    PlanStatusChange,
    PlanSummary,
    PlanWriter,
    SessionPlans,
    StepAssertion,
    StepStatus,
    Text,
    step_changes,
)

PLAN_UNREAD = "plan_unread"
"""How a run stops whose answer the plan held for a turn the conversation had no room
for: the loop stops a retry it cannot fit (``loop._ask``), and a hook cannot see the
conversation's room. A run with no turn left, in its report round or halted keeps its
answer instead."""
ANSWER_HELD = (
    "Your answer was held once: the session plan changed while you worked and has open "
    "steps. Check your answer against the plan in this notice, record your progress, then "
    "answer again; that answer is taken."
)
PLAN_OPEN = (
    "Your reply was shown to the user as a message, not taken as the end of the work: the "
    "session plan is active and its steps in next_steps can still be worked. Go on with "
    "the next of them, recording progress as you go; steps in with_children are with "
    "delegated runs, so wait for or review those rather than doing them again. If the "
    "user asked you to stop, pause or wait, record the steps they hold blocked with their "
    "reason and say what stays running. A reply with no tool call while the plan stands "
    "as it does now ends the run."
)
"""Why a run goes on past an answer while its plan is open (``Plans.goes_on``)."""
UNAVAILABLE = (
    "The session plan cannot be read now; tools that change anything are refused until it "
    "can be read again."
)
REREAD = (
    "plan_reread_required: someone else changed the session plan after this turn began; "
    "the plans notice before your next turn shows it as it now stands"
)
REOPENED = (
    "The revision changed the text of these finished steps, so their records no longer "
    "match and each is open again: record each again before plan_finish. When revising, "
    "leave finished steps as they are and add new steps or change open ones."
)
OPEN: Final = frozenset({"pending", "in_progress"})
APPROVALS_SEEN: Final = 200
"""The latest tool records of a run that ``approval_open`` looks through."""
MAX_REQUEST_BYTES = 96 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CHANGES: Final = 8
"""The latest changes by others a plans notice names; it counts the earlier ones."""
MAX_GOAL: Final = 400
"""The characters of the goal, or of a finished plan's outcome, a plans notice shows."""
MAX_REASON: Final = 160
"""The characters of a version's reason a plans notice shows."""
MAX_STEP_IDS: Final = 8
"""The step IDs a plans notice names for each kind of change in a revision, and the next
steps of one that carries a run past an answer."""
MAX_CONTINUED: Final = 32
"""The plan states a run's record names that it went on past an answer at; it counts the
rest."""
UNREAD = (
    "Others changed the plan since you last saw it: these are their changes, and each step "
    "they touched in full, with its latest record's note. Read them before you go on."
)
TOO_MUCH = (
    "Others changed more of the plan since you last saw it than this read can show: the "
    "plans notice before your next turn shows it, and until then this read counts as none."
)
MAX_OPEN_STEPS: Final = 16
"""The open steps, with their cursors, a plan change's result lists; it counts the rest."""
COMPACT = (
    "Each step's status and the step_cursor its next plan_progress names. detail full adds "
    "the plan's whole body and each step's latest record; the progress view pages notes "
    "and evidence."
)
GUIDANCE_VERSION = "sandbox-main-plan-8"
GUIDANCE = (
    "Planning guidance (sandbox-main-plan-8): The offered plan tools keep this session's "
    "plan and its progress. For substantive research, read and resume the current plan by "
    "its stable step IDs, or create a short plan with its goal, feasibility, outputs and "
    "evidence standards; small factual replies need no plan. Before creating one, load the "
    "skills the work needs and check the routes it depends on (the tools for its sources, "
    "the memory and cores it will have), so that its feasibility rests on what you found "
    "and names any tool that is missing. Activate a draft, tell the user its steps in a "
    "few lines and, in a sentence or two, the main risk to the requested result and what "
    "you will deliver if it comes about, and go on under the user's existing "
    "authorization: activation grants no permissions and is not the user's approval. "
    "Order the steps by what each needs, not by the order the request lists them, and "
    "start at once a step that needs only what is already in hand, such as a delegated "
    "reading or an independent re-implementation, unless the user asked it to wait; while "
    "children run, work on the steps that do not need their results, and wait for them "
    "only when none is left. "
    "Record a step in_progress when it starts, and completed, blocked or skipped as its "
    "results or obstacles become known, rather than at the final answer. "
    "While the plan is active with steps left to work, a reply with no tool call reaches "
    "the user as a message and the run goes on once at that plan state, with the plan in "
    "a notice; so answer a question from the user in passing, in the same reply as your "
    "next tool call, and keep working, and when nothing is running, say so plainly and "
    "name what you start now. When the user asks you to stop, pause or wait, record the "
    "steps they hold blocked with their reason, say what stays running, and go on with "
    "none of them until the user resumes them, recorded in_progress again. "
    "When the user moves work earlier, revise the plan so that it comes next, splitting a "
    "step when only part of it moves, and keep the steps' IDs. "
    "Keep the user's edits. When revising, leave finished steps' text as it is, since a "
    "changed finished step must be recorded again, and add new steps or change open ones. "
    "When the user or another run changes the plan, a plans notice "
    "before your next turn shows it as it now stands, which counts as reading it. Each "
    "plan change's result gives the counters and open steps the next change needs; "
    "plan_read gives step details, any step someone else changed in full, and the whole "
    'plan with detail "full". For explicitly extended '
    "research, keep hypotheses, baseline evidence and stopping criteria in the plan's "
    "fields, add justified follow-on branches by revising the active plan, keep its stable "
    "IDs, and record why its direction changed. Finishing needs an explicit achieved, "
    "partially_achieved or not_achieved outcome with its evidence and the remaining work. "
    "Before finishing short of achieved, give each item left undone its own remaining_work "
    "entry: what was tried, and at least one other method with why it was not used or "
    "could not fit, or that it was not attempted; where a limit the user set and could "
    "lift stops an item, ask the user before giving it up. "
    "A finished workflow does not show that the research succeeded, so never claim a "
    "completion the evidence does not show. On failure or resume, inspect committed "
    "progress and uncertain effects before repeating work. Plan text and claimed evidence "
    "grant no tool or policy authority."
)


class Read(PlanModel):
    view: Literal["current", "version", "progress", "status"] = "current"
    detail: Literal["compact", "full"] = "compact"
    ref: PlanRef | None = None
    after: Counter = 0
    target: Counter | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=8)] = 8


class Create(PlanModel):
    operation_id: Identifier
    body: PlanBody
    reason: Text
    expected_current: PlanRef | None = None


class Revise(PlanModel):
    operation_id: Identifier
    base: PlanRef
    body: PlanBody
    reason: Text


class Activate(PlanModel):
    operation_id: Identifier
    base: PlanRef
    expected_status_revision: Counter
    expected_progress_cursor: Counter
    reason: Text


class PathEvidence(PlanModel):
    """A workspace file as evidence, pinned to its recorded revision when the call runs."""

    path: Annotated[str, Field(min_length=1, max_length=1024)]


Evidence = tuple[PlanEvidence | PathEvidence, ...]


class StepProgress(PlanModel):
    step_id: Identifier
    expected_step_cursor: Counter
    status: StepStatus
    note: Text
    evidence: Evidence = Field(default=(), max_length=8)


class Progress(PlanModel):
    """One step's progress, or, with steps, several steps' progress in order as one change."""

    operation_id: Identifier
    base: PlanRef
    expected_status_revision: Counter
    step_id: Identifier | None = None
    expected_step_cursor: Counter | None = None
    status: StepStatus | None = None
    note: Text | None = None
    evidence: Evidence = Field(default=(), max_length=8)
    steps: tuple[StepProgress, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_PROGRESS_STEPS
    )

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        single = (self.step_id, self.expected_step_cursor, self.status, self.note)
        if self.steps is None:
            if any(value is None for value in single):
                raise ValueError(
                    "give step_id, expected_step_cursor, status and note, or a steps list"
                )
        elif any(value is not None for value in single) or self.evidence:
            raise ValueError("give a steps list or one step's fields, not both")
        return self

    def entries(self) -> tuple[StepProgress, ...]:
        """The steps this call records, in order."""
        if self.steps is not None:
            return self.steps
        return (
            StepProgress(
                step_id=cast(str, self.step_id),
                expected_step_cursor=cast(int, self.expected_step_cursor),
                status=cast(StepStatus, self.status),
                note=cast(str, self.note),
                evidence=self.evidence,
            ),
        )


class Outcome(PlanModel):
    """A goal outcome as the agent gives it: its evidence claims, or workspace paths
    pinned as progress evidence's are (``GoalOutcome`` once resolved)."""

    status: Literal["achieved", "partially_achieved", "not_achieved"]
    summary: Text
    evidence: Evidence = Field(max_length=8)
    remaining_work: tuple[Text, ...] = Field(max_length=16)


class Finish(Activate):
    status: Literal["completed", "abandoned"] = "completed"
    outcome: Outcome | None = None


def _paths(request: PlanModel) -> bool:
    """Whether ``request`` gives evidence by workspace path."""
    if isinstance(request, Progress):
        given = [item for step in request.entries() for item in step.evidence]
    elif isinstance(request, Finish) and request.outcome is not None:
        given = list(request.outcome.evidence)
    else:
        return False
    return any(isinstance(item, PathEvidence) for item in given)


def _resolves(request: PlanModel) -> bool:
    """Whether ``request`` gives evidence the tool resolves or checks before it is kept:
    a workspace path, or an artifact claim."""
    if isinstance(request, Progress):
        given = [item for step in request.entries() for item in step.evidence]
    elif isinstance(request, Finish) and request.outcome is not None:
        given = list(request.outcome.evidence)
    else:
        return False
    return any(isinstance(item, PathEvidence) or item.kind == "artifact" for item in given)


REQUESTS: dict[str, type[PlanModel]] = {
    "plan_read": Read,
    "plan_create": Create,
    "plan_revise": Revise,
    "plan_activate": Activate,
    "plan_progress": Progress,
    "plan_finish": Finish,
}
NAMES = frozenset(REQUESTS)
DESCRIPTIONS = {
    "plan_read": "Read the current session plan: by default each step's title, status and "
    "step_cursor with the plan's counters; detail full adds the whole body and each step's "
    "latest record. Long progress/status history is paged; historical reads do not satisfy "
    "a current reread. A plan change's result already carries the counters and open steps "
    "the next change needs.",
    "plan_create": "Create this session's draft research plan, or replace its exact terminal "
    "predecessor. Use stable step IDs and an explicit operation_id for exact retries.",
    "plan_revise": "Append a current-base plan version with stable IDs and an attributed reason. "
    "Reread USER changes; never overwrite a stale version.",
    "plan_activate": "Record a draft as active under existing task authorization. This is not "
    "USER approval or a permission grant.",
    "plan_progress": "Record progress on the active plan's steps with exact step/status CAS, in "
    "the same reply as the call that produced the result: one step, or a steps list applied "
    "in order as one change (finish one step and start the next together; a refused entry "
    "refuses all). Evidence is an artifact claim or a workspace path, pinned to the file's "
    "recorded revision. The result gives the next counters and open steps' cursors. "
    "Completed is an assertion, not scientific verification.",
    "plan_finish": "Finish terminal steps with an explicit goal outcome, or abandon an open "
    "plan with a reason. Blocked/skipped do not mean the goal was achieved.",
}
SPECS = tuple(
    ToolSpec(name, DESCRIPTIONS[name], model.model_json_schema())
    for name, model in REQUESTS.items()
)
_T = TypeVar("_T")
Pin = tuple[str, int, int, int, int]
"""A plan state: its plan, version, status revision, progress and event cursors."""
Shown = Pin | Literal["unavailable", "no_current_plan"]
"""What a plans notice showed: a plan state, or that there was none to show."""


@dataclass(frozen=True)
class PlanNotice:
    """A plans notice's payload and the plan state it shows, whose delivery to a model
    reads the plan; the state is ``None`` when the notice shows no plan."""

    payload: dict[str, object]
    pin: Pin | None


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ids(ids: tuple[str, ...]) -> str:
    named = ", ".join(ids[:MAX_STEP_IDS])
    return named if len(ids) <= MAX_STEP_IDS else f"{named} and {len(ids) - MAX_STEP_IDS} more"


def _pin(summary: PlanSummary | None) -> Pin | None:
    if summary is None:
        return None
    return (
        summary.head.plan_id,
        summary.head.version,
        summary.status_revision,
        summary.progress_cursor,
        summary.event_cursor,
    )


def _json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise DocumentError(INVALID_REQUEST, "plan response exceeds its complete byte budget")
    return text


def _invalid(name: str, error: ValidationError) -> str:
    """A refused request's first problem: the field, what it needs, and the size given."""
    problems = error.errors(include_url=False)
    first = problems[0]
    where = ".".join(str(part) for part in first["loc"]) or "the request"
    message = first["msg"].removeprefix("Value error, ")
    given = first.get("input")
    if first["type"] in ("string_too_long", "string_too_short") and isinstance(given, str):
        message += f" (given {len(given):,})"
    more = f"; and {len(problems) - 1} more problems" if len(problems) > 1 else ""
    return f"invalid {name} request: {where}: {message}{more}"


def _resolved(claim: PlanEvidence) -> dict[str, object]:
    """What a path resolved to, as a result says it: the artifact revision and who
    recorded it, when."""
    return {
        "artifact_id": claim.reference_id,
        "revision": claim.revision,
        "sha256": claim.sha256,
        **(
            {"written_by": claim.written_by.model_dump(mode="json")}
            if claim.written_by is not None
            else {}
        ),
    }


def _counters(plan: CompactPlan) -> dict[str, object]:
    """A plan's version, status and counters, as its next change names them."""
    head = plan.head
    return {
        "ref": head.ref.model_dump(mode="json"),
        "status": head.status,
        "status_revision": head.status_revision,
        "progress_cursor": head.progress_cursor,
    }


def _step_state(step: CompactStep) -> dict[str, object]:
    """A current step's status and the cursor its next progress names: ``pending`` with
    cursor 0 before its first record, and ``changed_since_record`` when a revision
    changed it after its latest one."""
    latest = step.latest
    if latest is None:
        return {"status": "pending", "step_cursor": 0}
    state: dict[str, object] = {"status": latest.status, "step_cursor": latest.sequence}
    if not step.matches_current_step:
        state["changed_since_record"] = True
    return state


class PlanTools:
    """Trusted CLI MAIN adapter over the stores it borrows."""

    specs = SPECS

    def __init__(
        self,
        plans: SessionPlans,
        projects: ProjectStore,
        context: ExecutionContext,
        *,
        run_id: str,
        alias: str,
        clock: Clock = SYSTEM_CLOCK,
        artifacts: ArtifactInventory | None = None,
    ) -> None:
        """``artifacts`` resolves evidence given by a workspace path; without it, evidence
        is given by artifact ID only."""
        self.projects, self.context, self.clock = projects, context, clock
        self.artifacts = artifacts
        # The fence binds this run's own session record. Other sessions and the
        # project's policy revision may change: the worker enforces the current policy.
        self.registry_context = ExecutionContext(
            project_id=context.project_id,
            session_id=context.session_id,
            environment_id=context.environment_id,
        )
        self.authority = PlanAuthority(
            scope=PlanScope(project_id=context.project_id, session_id=context.session_id),
            role="main",
            run_id=run_id,
            alias=alias,
        )
        self.snapshot: SessionSnapshot = projects.session_snapshot(
            context.project_id, context.session_id
        )
        if self.snapshot.context != self.registry_context:
            raise DocumentError(POLICY_DENIED, "planning context differs from the recorded session")
        # Each plan change checks, in its own transaction, that this opening is current.
        opening = self.snapshot.session.kernel_notice_lifetime
        if opening is None:  # every open gives the session one
            raise DocumentError(POLICY_DENIED, "planning requires an open recorded session")
        self.plans = plans.pinned(opening)
        self.invalid = False
        self.cursor = 0
        self.initialized = False
        self.requires_read = False
        self.held = False
        self.pending_read: Pin | None = None
        """The plan state a current read or a plans notice showed, which the next model
        call that returns has read."""
        self.shown: Shown | None = None
        """What the last plans notice showed: the same is not shown again."""
        self.changes: deque[PlanEvent] = deque(maxlen=MAX_CHANGES)
        """Others' changes since the last plans notice, the latest of them."""
        self.unlisted = 0
        """Others' changes since the last plans notice that ``changes`` no longer holds."""
        self.continued: list[Pin] = []
        """The plan states this run went on past an answer at (``went_on``), in order."""
        self.offer: PlanSummary | None = None
        """The plan state ``going_on`` offered to go on at, until ``went_on`` commits it."""

    def _owned(self, action: Callable[[], _T]) -> _T:
        if self.invalid:
            raise DocumentError(
                POLICY_DENIED, "planning session was closed or reopened; start a new run"
            )
        try:
            current = self.projects.session_snapshot(
                self.context.project_id, self.context.session_id
            )
            # A close and reopen gives the session a new record (opened_at and
            # kernel_notice_lifetime), so it still ends this run's effects. A change
            # checks the same again in its own transaction (SessionPlans.pinned).
            if current != self.snapshot or current.context != self.registry_context:
                self.invalid = True
                raise DocumentError(
                    POLICY_DENIED, "planning session was closed or reopened; start a new run"
                )
            return action()
        except ProjectError:
            self.invalid = True
            raise DocumentError(
                POLICY_DENIED, "planning requires its original open recorded session"
            ) from None
        except DocumentError as error:
            if error.details.get("session") != "closed":
                raise
            self.invalid = True
            raise DocumentError(
                POLICY_DENIED, "planning session was closed or reopened; start a new run"
            ) from None

    def _refresh(self) -> PlanSummary | None:
        summary = self.plans.summary(self.authority)
        if not self.initialized:
            # The plan as the run finds it is unread; its first notice shows it whole,
            # so the scan for others' changes starts after it.
            self.initialized = True
            self.requires_read = summary is not None
            self.cursor = 0 if summary is None else summary.event_cursor
            return summary
        if summary is None:
            return None
        target = summary.event_cursor
        for _ in range(4):
            if self.cursor >= target:
                break
            page = self.plans.events(self.authority, after=self.cursor, target=target, limit=16)
            for event in page.items:
                if event.author.role != "main" or event.author.run_id != self.authority.run_id:
                    self.requires_read = True
                    if len(self.changes) == MAX_CHANGES:
                        self.unlisted += 1
                    self.changes.append(event)
            self.cursor = page.after
            if not page.more:
                break
        if self.cursor < target:
            self.held = self.requires_read = True
        return summary

    async def safe_point(self, *, force: bool = False) -> PlanNotice | None:
        """The plans notice the run needs: the current plan, when someone other than this
        run changed it or the run has not seen it, or that it cannot be read. ``None``
        when the run has seen it, or this notice already showed the same, unless
        ``force``. The run's own changes bring none."""
        try:
            return await threaded(lambda: self._owned(lambda: self._notice(force)))
        except DocumentError:
            self.held = self.requires_read = True
            if self.shown == "unavailable" and not force:
                return None
            self.shown = "unavailable"
            return PlanNotice({"status": "unavailable", "plan": UNAVAILABLE}, None)

    def _notice(self, force: bool) -> PlanNotice | None:
        self._refresh()
        if not (self.requires_read or self.held):
            return None
        found = self.plans.compact_summary(self.authority)
        if found is None:
            if self.shown == "no_current_plan" and not force:
                return None
            self.shown = "no_current_plan"
            return PlanNotice({"status": "no_current_plan"}, None)
        summary, plan = found
        pin = _pin(summary)
        if pin is None or (pin == self.shown and not force):
            return None
        changes, unlisted = list(self.changes), self.unlisted
        self.changes.clear()
        self.unlisted = 0
        self.shown = pin
        return PlanNotice(self._render(summary, plan, changes, unlisted), pin)

    def _render(
        self, summary: PlanSummary, plan: CompactPlan, changes: list[PlanEvent], unlisted: int
    ) -> dict[str, object]:
        """The plan as text a model reads: no digests, cursors or operation IDs, which
        its record and ``plan_read`` keep."""
        head, body = plan.head, plan.version.body
        steps = {step.step_id: step for phase in body.phases for step in phase.steps}
        status = {
            "draft": "a draft (effect tools wait until plan_activate makes it active)",
            "active": "active",
            "completed": "completed",
            "abandoned": "abandoned",
        }[head.status]
        lines: list[str] = []
        running: list[str] = []
        waiting: list[str] = []
        for projection in plan.steps:
            step, latest = steps[projection.step_id], projection.latest
            if latest is None:
                state = "pending"
            elif projection.matches_current_step:
                state = latest.status
            else:
                state = f"pending (edited since it was {latest.status})"
            if state == "in_progress":
                running.append(step.step_id)
            elif state not in ("completed", "blocked", "skipped"):
                waiting.append(step.step_id)
            track = f" [track {step.track_id}]" if step.track_id else ""
            lines.append(f"{step.step_id}{track} {state}: {step.title}")
        if head.status in ("completed", "abandoned"):
            current = f"none: the plan is {head.status}"
        elif running:
            current = "in progress: " + ", ".join(running)
        elif waiting:
            current = f"next open step: {waiting[0]}"
        else:
            current = "no open steps"
        if changes:
            # Each author's changes in a row, a repeated one counted.
            grouped: list[tuple[str, list[tuple[str, int]]]] = []
            for event in changes:
                who, what = self._who(event.author), self._change(event, plan)
                if not grouped or grouped[-1][0] != who:
                    grouped.append((who, []))
                whats = grouped[-1][1]
                if whats and whats[-1][0] == what:
                    whats[-1] = (what, whats[-1][1] + 1)
                else:
                    whats.append((what, 1))
            changed = "; ".join(
                who
                + " "
                + ", then ".join(what if n == 1 else f"{what} {n} times" for what, n in whats)
                for who, whats in grouped
            )
            if unlisted:
                changed = f"after {unlisted} earlier changes, {changed}"
        else:
            event = summary.last_event
            changed = f"last change: {self._who(event.author)} {self._change(event, plan)}"
        if self.held:
            changed += "; and more changes than this lists"
        payload: dict[str, object] = {
            "status": head.status,
            "plan": f"{head.ref.plan_id} version {head.ref.version}, {status}: "
            + _cut(body.goal, MAX_GOAL),
            "steps": lines,
            "current": current,
            "changed": changed,
        }
        if head.outcome is not None:
            payload["outcome"] = f"{head.outcome.status}: " + _cut(head.outcome.summary, MAX_GOAL)
        return payload

    def _who(self, author: PlanAuthority) -> str:
        if author.role == "user":
            return "the user"
        if author.role == "delegated":
            return f"delegated run {author.run_id} (track {author.track_id})"
        if author.run_id == self.authority.run_id:
            return "this run"
        return f"main run {author.run_id}"

    def _change(self, event: PlanEvent, plan: CompactPlan) -> str:
        if event.action == "publish":
            return f"created plan {event.ref.plan_id}"
        if event.action == "revise":
            after = self.plans.version(self.authority, event.ref)
            before = self.plans.version(
                self.authority, PlanRef(plan_id=event.ref.plan_id, version=event.ref.version - 1)
            )
            diff = step_changes(before.body, after.body)
            parts = [
                f"{kind} {_ids(ids)}"
                for kind, ids in (
                    ("added", diff.added),
                    ("changed", diff.changed),
                    ("removed", diff.removed),
                )
                if ids
            ]
            parts.append("reason: " + _cut(after.reason, MAX_REASON))
            return f"revised it to version {event.ref.version} ({'; '.join(parts)})"
        if event.action == "progress":
            for step in plan.steps:
                if step.latest is not None and step.latest.sequence == event.number:
                    return f"recorded {step.step_id} {step.latest.status}"
            return "recorded progress on a step"
        if event.ref == plan.head.ref and event.number == plan.head.status_revision:
            return f"set it {plan.head.status}"
        return "changed its status"

    async def open_steps(self) -> tuple[str, list[str]] | None:
        """The current plan's status and its open steps: those with no progress, pending
        or in progress, or changed since their progress. A finished plan, or none, has
        none. ``None`` when the plan cannot be read."""
        try:
            plan = await threaded(
                lambda: self._owned(lambda: self.plans.compact_current(self.authority))
            )
        except DocumentError:
            return None
        if plan is None:
            return "no_current_plan", []
        if plan.head.status in ("completed", "abandoned"):
            return plan.head.status, []
        return plan.head.status, [
            step.step_id
            for step in plan.steps
            if step.latest is None or not step.matches_current_step or step.latest.status in OPEN
        ]

    async def going_on(self) -> PlanNotice | None:
        """The plans notice that would carry the run past an answer with no tool call: the
        current plan, active with steps that can still be worked (pending or in progress,
        a step changed since its progress among them) other than those on a track a
        delegated run holds, running or returned and awaiting its review, with no question
        of the session's open, at a state that has had no continuation yet, in a run or as a
        queued new root. An offer, which ``went_on`` commits. ``None`` otherwise, and while
        the plan is unread or cannot be read: the closing decides then, and waits for the
        children."""
        self.offer = None
        try:
            return await threaded(lambda: self._owned(self._going_on))
        except DocumentError:
            return None

    def _going_on(self) -> PlanNotice | None:
        self._refresh()
        if self.requires_read or self.held:
            return None
        found = self.plans.compact_summary(self.authority)
        if found is None:
            return None
        summary, plan = found
        pin = _pin(summary)
        if summary.status != "active" or pin is None or pin in self.continued:
            return None
        scope = self.authority.scope
        delegated = self._delegated_tracks()
        steps = {step.step_id: step for phase in plan.version.body.phases for step in phase.steps}
        running: list[str] = []
        waiting: list[str] = []
        children: list[str] = []
        for step in plan.steps:
            latest = step.latest
            state = latest.status if latest is not None and step.matches_current_step else None
            if state not in (None, "pending", "in_progress"):
                continue
            if steps[step.step_id].track_id in delegated:
                children.append(step.step_id)
            else:
                (running if state == "in_progress" else waiting).append(step.step_id)
        workable = [*running, *waiting]
        if not workable:
            return None
        if Questions(self.plans.documents).open(scope.project_id, scope.session_id):
            return None
        with self.plans.documents.control.read() as tx:
            if continued(tx.sql, scope.project_id, scope.session_id, summary):
                return None
        self.offer = summary
        payload: dict[str, object] = {
            **self._render(summary, plan, [], 0),
            "next_steps": workable[:MAX_STEP_IDS],
            "plan_open": PLAN_OPEN,
        }
        if children:
            payload["with_children"] = children[:MAX_STEP_IDS]
        return PlanNotice(payload, pin)

    def _delegated_tracks(self) -> frozenset[str]:
        """The tracks of the session's delegated runs that hold their steps: running, or
        returned and awaiting the parent's review, which can send them on (PD-46)."""
        scope = self.authority.scope
        with self.plans.documents.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT DISTINCT json_extract(brief,'$.track.track_id') FROM delegations "
                "WHERE project_id=? AND session_id=? "
                "AND (ended IS NULL OR (ended='returned' AND review='awaiting')) "
                "AND json_extract(brief,'$.track.track_id') IS NOT NULL",
                (scope.project_id, scope.session_id),
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    async def went_on(self) -> bool:
        """Commit the offer ``going_on`` made: whether its plan state is still current and
        had no continuation, which this records (``auto_continuation.claim_in_run``). Only
        then does the run go on, and count it."""
        offer, self.offer = self.offer, None
        if offer is None:
            return False

        def claim() -> bool:
            scope = self.authority.scope
            with self.plans.documents.control.write() as tx:
                return self.plans.summary_in(tx.sql, self.authority) == offer and claim_in_run(
                    tx.sql,
                    scope.project_id,
                    scope.session_id,
                    offer,
                    run=self.authority.run_id or "",
                    observed=self.clock.now().timestamp(),
                )

        try:
            claimed = await threaded(lambda: self._owned(claim))
        except DocumentError:
            return False
        if claimed and (pin := _pin(offer)) is not None:
            self.continued.append(pin)
        return claimed

    async def model_delivered(self) -> None:
        """After a model call returned: the current read or plans notice it saw read the
        plan, if the plan has not changed since."""
        pending, self.pending_read = self.pending_read, None
        if pending is None:
            return
        try:
            summary = await threaded(
                lambda: self._owned(lambda: self.plans.summary(self.authority))
            )
        except DocumentError:
            self.held = self.requires_read = True
            self.shown = None  # the next safe point shows the plan again
            return
        if _pin(summary) == pending:
            self.cursor = pending[-1]
            self.requires_read = self.held = False
            self.changes.clear()
            self.unlisted = 0

    async def check_dispatch(self, effect: Effect) -> None:
        """Refuse an effect while another's change is unread or the plan is not active.
        A change is read once a model call that saw the plans notice showing it, or a
        current read after it, returned.

        Only the classes known to change nothing the plan governs pass unchecked.
        """
        if effect in ("read", "cancel", "plan", "proposal"):
            return
        summary = await threaded(lambda: self._owned(self._refresh))
        if self.requires_read or self.held:
            raise DocumentError(CONFLICT, REREAD)
        if summary is not None and summary.status == "draft":
            raise DocumentError(
                POLICY_DENIED, "plan is not active; activation is not USER approval"
            )

    async def call(
        self,
        name: str,
        arguments: str,
        *,
        call_id: str,
        model: str | None,
    ) -> tuple[str, dict[str, Any]]:
        request_type = REQUESTS[name]
        try:
            size = len(arguments.encode("utf-8", "strict"))
        except UnicodeError:
            raise DocumentError(INVALID_REQUEST, "plan request is not valid UTF-8") from None
        if size > MAX_REQUEST_BYTES:
            raise DocumentError(
                INVALID_REQUEST,
                f"plan request is {size:,} bytes, over its budget of {MAX_REQUEST_BYTES:,}",
            )
        try:
            request = request_type.model_validate_json(arguments or "{}", strict=True)
        except ValidationError as error:
            raise DocumentError(INVALID_REQUEST, _invalid(name, error)) from None
        except (ValueError, RecursionError):
            raise DocumentError(INVALID_REQUEST, f"{name} arguments are not valid JSON") from None

        def perform() -> tuple[str, dict[str, Any]]:
            summary = self._refresh()
            resolved: list[dict[str, object]] = []
            if isinstance(request, Read):
                result, pin = self._read(request)
                try:
                    text = _json(result)
                except DocumentError:
                    if "changed_by_others" not in result:
                        raise
                    # Too much changed to show here: the read reads nothing, and the
                    # plans notice before the next turn shows the plan.
                    result = {**result, "changed_by_others": {"about": TOO_MUCH}}
                    text, pin = _json(result), None
                if pin is not None:
                    self.pending_read = pin
                return text, {"plan_read": result, "plan_call_id": call_id, "resolved_model": model}
            # The actual answering model is known only at this tool boundary.
            # It is part of immutable attribution and exact-operation identity;
            # changing it on a retry must conflict rather than rewrite history.
            authority = self.authority.model_copy(update={"resolved_model": model})
            operation = cast(str, getattr(request, "operation_id"))  # noqa: B009 - every change has one
            # The reread fence comes first: a change by another actor is read before even
            # a retry is answered. Then an operation already recorded is its exact retry,
            # or a conflict, before any check or resolution: its receipt stands however
            # the plan or the files its paths named are now.
            if self.requires_read or self.held:
                raise DocumentError(CONFLICT, REREAD)
            retry = self.plans.recorded(authority, operation)
            # Evidence the tool resolves is digested as given, so a retry is found by it.
            given = request.model_dump(mode="json") if _resolves(request) else None
            if isinstance(request, Create):
                receipt = self.plans.publish(
                    authority,
                    request.operation_id,
                    request.body,
                    request.reason,
                    expected_current=request.expected_current,
                )
            elif isinstance(request, Revise):
                receipt = self.plans.revise(
                    authority, request.operation_id, request.base, request.body, request.reason
                )
            elif isinstance(request, Progress):
                if not retry and (summary is None or summary.status != "active"):
                    raise DocumentError(POLICY_DENIED, "progress requires an active current plan")
                if not retry and (
                    summary is None or request.expected_status_revision != summary.status_revision
                ):
                    raise DocumentError(CONFLICT, "plan status changed; reread it")
                entries, resolved = self._assertions(request, resolve=not retry)
                if request.steps is None:
                    entry = entries[0]
                    receipt = self.plans.progress(
                        authority,
                        request.operation_id,
                        request.base,
                        entry.step_id,
                        entry.expected_step_cursor,
                        entry.status,
                        entry.note,
                        evidence=entry.evidence,
                        expected_status_revision=request.expected_status_revision,
                        given=given,
                        attributed=True,
                    )
                else:
                    receipt = self.plans.progress_steps(
                        authority,
                        request.operation_id,
                        request.base,
                        entries,
                        expected_status_revision=request.expected_status_revision,
                        given=given,
                        attributed=True,
                    )
            else:
                if not isinstance(request, Activate):
                    raise DocumentError(INVALID_REQUEST, "unknown planning request")
                if (
                    not retry
                    and not isinstance(request, Finish)
                    and (
                        summary is None
                        or summary.status not in {"draft", "active"}
                        or (
                            summary.status == "active"
                            and request.expected_status_revision == summary.status_revision
                        )
                    )
                ):
                    raise DocumentError(POLICY_DENIED, "only a draft plan can be activated")
                outcome = None
                if isinstance(request, Finish) and request.outcome is not None:
                    claims, resolved = self._claims(
                        request.outcome.evidence, "outcome.evidence", resolve=not retry
                    )
                    outcome = self._goal(request.outcome, claims)
                receipt = self.plans.set_status(
                    authority,
                    request.operation_id,
                    request.base,
                    request.expected_status_revision,
                    request.expected_progress_cursor,
                    request.status if isinstance(request, Finish) else "active",
                    request.reason,
                    outcome=outcome,
                    given=given,
                    attributed=True,
                )
            value = receipt.model_dump(mode="json")
            result: dict[str, object] = {
                "receipt": value,
                "historical_receipt": summary is not None
                and receipt.event_sequence <= summary.event_cursor,
            }
            if _paths(request):
                if retry or result["historical_receipt"]:
                    # A retry: what the first call resolved, as its records keep it.
                    resolved = self._first_resolved(request, receipt)
                result["evidence_resolved"] = resolved
            if isinstance(request, Revise):
                reopened = self._reopened(receipt.ref)
                if reopened:
                    result["finished_steps_changed"] = reopened
                    result["note"] = REOPENED
            if (standing := self._standing()) is not None:
                result["plan"] = standing
            return _json(result), {
                "plan_receipt": value,
                "plan_call_id": call_id,
                "resolved_model": model,
            }

        return await threaded(lambda: self._owned(perform))

    def _standing(self) -> dict[str, object] | None:
        """The current plan as the next plan call needs it: its version, status and
        counters, how many steps are in each state, and the open steps with their
        cursors; ``None`` with no current plan. A plan another actor changed since this
        run's last read still needs that read (the plans notice) before a change."""
        plan = self.plans.compact_current(self.authority)
        if plan is None:
            return None
        counts: dict[str, int] = {}
        workable: list[dict[str, object]] = []
        for step in plan.steps:
            state = _step_state(step)
            kind = "changed" if "changed_since_record" in state else cast(str, state["status"])
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "changed" or kind in OPEN:
                workable.append({"step_id": step.step_id, **state})
        standing = {**_counters(plan), "steps": counts}
        if plan.head.status in ("completed", "abandoned"):
            return standing
        standing["open_steps"] = workable[:MAX_OPEN_STEPS]
        if len(workable) > MAX_OPEN_STEPS:
            standing["more_open_steps"] = len(workable) - MAX_OPEN_STEPS
        return standing

    def _assertions(
        self, request: Progress, *, resolve: bool = True
    ) -> tuple[tuple[StepAssertion, ...], list[dict[str, object]]]:
        """The call's step assertions, with their evidence resolved (``_claims``), and what
        each path resolved to; for a retry, which the store answers with its receipt
        however the evidence stands now, nothing is resolved (``resolve``)."""
        resolved: list[dict[str, object]] = []
        entries: list[StepAssertion] = []
        several = request.steps is not None
        for index, step in enumerate(request.entries()):
            prefix = f"steps[{index}].evidence" if several else "evidence"
            claims, found = self._claims(step.evidence, prefix, resolve=resolve)
            resolved.extend(found)
            entries.append(
                StepAssertion(
                    step.step_id, step.expected_step_cursor, step.status, step.note, claims
                )
            )
        return tuple(entries), resolved

    def _claims(
        self, evidence: Evidence, prefix: str, *, resolve: bool = True
    ) -> tuple[tuple[PlanEvidence, ...], list[dict[str, object]]]:
        """Evidence as the store keeps it: a path pinned to this session's kept revision
        (``_pinned``), an artifact claim checked against the inventory (``_verified``),
        each naming who recorded it; other claims as given. A claim may not name who
        recorded it: that is the platform's to say. Without ``resolve``, paths are left
        out and claims kept as given, for a retry."""
        claims: list[PlanEvidence] = []
        resolved: list[dict[str, object]] = []
        for number, item in enumerate(evidence):
            where = f"{prefix}[{number}]"
            if isinstance(item, PlanEvidence):
                if item.written_by is not None:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{where}: written_by is the platform's, which says who recorded a "
                        "revision it checked; leave it out",
                    )
                claims.append(self._verified(item, where) if resolve else item)
            elif resolve:
                claim = self._pinned(item.path, where)
                claims.append(claim)
                resolved.append({"path": item.path, **_resolved(claim)})
        return tuple(claims), resolved

    @staticmethod
    def _goal(outcome: Outcome, claims: tuple[PlanEvidence, ...]) -> GoalOutcome:
        try:
            return GoalOutcome(
                status=outcome.status,
                summary=outcome.summary,
                evidence=claims,
                remaining_work=outcome.remaining_work,
            )
        except ValidationError as error:
            raise DocumentError(INVALID_REQUEST, _invalid("plan_finish", error)) from None

    def _verified(self, claim: PlanEvidence, where: str) -> PlanEvidence:
        """An artifact claim as the inventory holds it: the revision exists, its SHA-256
        is the claim's, its bytes are kept, and the platform names who recorded it, this
        session or another. Other claims, and every claim without an inventory, as given."""
        if claim.kind != "artifact" or self.artifacts is None:
            return claim
        revision, sha256 = cast(int, claim.revision), cast(str, claim.sha256)
        try:
            found = self.artifacts.revision(self.context.project_id, claim.reference_id, revision)
        except DocumentError as error:
            raise DocumentError(
                error.kind, f"{where}: {claim.reference_id} revision {revision}: {error.message}"
            ) from None
        if found.deleted or found.reference.sha256 != sha256:
            raise DocumentError(
                CONFLICT,
                f"{where}: {claim.reference_id} revision {revision} "
                + ("is a deletion" if found.deleted else f"has SHA-256 {found.reference.sha256}")
                + ": give the revision and hash find_artifacts lists",
            )
        if not found.content_retained:
            raise DocumentError(
                CONFLICT,
                f"{where}: {claim.reference_id} revision {revision} keeps no bytes, so its hash "
                "cannot be checked; publish the file and claim the revision that publishes",
            )
        source = found.publication
        return claim.model_copy(
            update={
                "written_by": PlanWriter(
                    session_id=source.context.session_id,
                    run_id=source.run_id,
                    recorded_at=found.created_at,
                )
            }
        )

    def _unread(self, plan: CompactPlan) -> dict[str, object]:
        """What a current read shows of the changes others made since this run last saw
        the plan, which its delivery reads as a plans notice's does: who changed what, in
        a line each, and each step they touched in full, its fields and its latest record
        with its note and author; every step when more changed than the run's scan holds.
        Nothing when no other actor changed it."""
        if not (self.changes or self.unlisted or self.held):
            return {}
        touched: set[str] = set()
        whole = bool(self.unlisted or self.held)
        for event in self.changes:
            if event.action == "publish":
                whole = True
            elif event.action == "revise" and event.ref.version > 1:
                before = self.plans.version(
                    self.authority,
                    PlanRef(plan_id=event.ref.plan_id, version=event.ref.version - 1),
                )
                after = self.plans.version(self.authority, event.ref)
                diff = step_changes(before.body, after.body)
                touched.update(diff.added + diff.changed)
            elif event.action == "progress":
                page = self.plans.history(
                    self.authority,
                    event.ref.plan_id,
                    "progress",
                    after=event.number - 1,
                    target=event.number,
                    limit=1,
                )
                touched.update(cast(PlanProgress, item).step_id for item in page.items)
        body = {
            step.step_id: (phase, step)
            for phase in plan.version.body.phases
            for step in phase.steps
        }
        steps: list[dict[str, object]] = []
        for projection in plan.steps:
            if not whole and projection.step_id not in touched:
                continue
            phase, step = body[projection.step_id]
            shown: dict[str, object] = {
                "step_id": step.step_id,
                "phase_id": phase.phase_id,
                "title": step.title,
                "description": step.description,
                "evidence_standard": step.evidence_standard,
                **({"track_id": step.track_id} if step.track_id else {}),
                **_step_state(projection),
            }
            if projection.latest is not None:
                page = self.plans.history(
                    self.authority,
                    plan.head.ref.plan_id,
                    "progress",
                    after=projection.latest.sequence - 1,
                    target=projection.latest.sequence,
                    limit=1,
                )
                for item in page.items:
                    record = cast(PlanProgress, item)
                    shown["latest"] = {
                        "status": record.status,
                        "note": record.note,
                        "by": self._who(record.author),
                        "recorded_at": record.recorded_at.isoformat(),
                        "evidence": [e.model_dump(mode="json") for e in record.evidence],
                    }
            steps.append(shown)
        changed = [
            f"{self._who(event.author)} {self._change(event, plan)}" for event in self.changes
        ]
        if self.unlisted:
            changed.insert(0, f"{self.unlisted} earlier changes")
        return {
            "changed_by_others": {
                "changes": changed,
                "steps": steps,
                "about": UNREAD,
            }
        }

    def _first_resolved(self, request: PlanModel, receipt: PlanReceipt) -> list[dict[str, object]]:
        """What the paths of the call ``receipt`` acknowledges resolved to when it was
        first made, as its records keep it: its progress records, the last of which the
        receipt names, or the finish's outcome."""
        resolved: list[dict[str, object]] = []
        if isinstance(request, Finish):
            page = self.plans.history(
                self.authority,
                receipt.ref.plan_id,
                "status",
                after=receipt.status_revision - 1,
                target=receipt.status_revision,
                limit=1,
            )
            for kept in cast(tuple[PlanStatusChange, ...], page.items):
                given = request.outcome.evidence if request.outcome is not None else ()
                stored = kept.outcome.evidence if kept.outcome is not None else ()
                resolved.extend(
                    {"path": item.path, **_resolved(claim)}
                    for item, claim in zip(given, stored, strict=False)
                    if isinstance(item, PathEvidence)
                )
            return resolved
        if not isinstance(request, Progress):
            return resolved
        entries = request.entries()
        page = self.plans.history(
            self.authority,
            receipt.ref.plan_id,
            "progress",
            after=receipt.progress_cursor - len(entries),
            target=receipt.progress_cursor,
            limit=len(entries),
        )
        for step, kept in zip(entries, cast(tuple[PlanProgress, ...], page.items), strict=False):
            for item, claim in zip(step.evidence, kept.evidence, strict=False):
                if isinstance(item, PathEvidence):
                    resolved.append({"path": item.path, **_resolved(claim)})
        return resolved

    def _pinned(self, path: str, where: str) -> PlanEvidence:
        """The evidence claim for the file at ``path``: its artifact at the latest
        revision the platform recorded, published or from the file tools, with who
        recorded it. Only a revision this session recorded, whose bytes are kept, is
        pinned by path; another session's file is claimed by its artifact ID."""
        if self.artifacts is None:
            raise DocumentError(
                INVALID_REQUEST,
                f"{where}: evidence by path is not available in this run; give the artifact's "
                "kind, reference_id, revision and sha256",
            )
        project, session = self.context.project_id, self.context.session_id
        by_id = "give its artifact's kind, reference_id, revision and sha256 to claim it"
        try:
            scoped = scoped_path(path, session)
            if scoped.startswith("sessions/") and not scoped.startswith(f"sessions/{session}/"):
                raise DocumentError(POLICY_DENIED, f"it is in another session's area: {by_id}")
            found, _ = self.artifacts.recent(project, at_path=scoped)
        except DocumentError as error:
            raise DocumentError(error.kind, f"{where}: {path}: {error.message}") from None
        latest = found[0] if found else None
        if latest is None or latest.deleted:
            state = "was deleted" if latest is not None else "has no recorded revision"
            raise DocumentError(
                NOT_FOUND,
                f"{where}: {path} {state}, so there is nothing to pin; publish the file "
                "(publish with its path) and give the path again",
            )
        if not latest.content_retained:
            raise DocumentError(
                CONFLICT,
                f"{where}: {path}'s latest revision keeps no bytes, so its hash cannot be "
                "checked; publish the file (publish with its path) and give the path again",
            )
        source = latest.publication
        if source.context.session_id != session:
            raise DocumentError(
                POLICY_DENIED,
                f"{where}: {path}'s latest revision {latest.reference.revision} was recorded "
                f"by session {source.context.session_id}'s run {source.run_id} at "
                f"{latest.created_at.isoformat()}, not by this session: {by_id}",
            )
        return PlanEvidence(
            project_id=project,
            kind="artifact",
            reference_id=latest.reference.artifact_id,
            revision=latest.reference.revision,
            sha256=latest.reference.sha256,
            written_by=PlanWriter(
                session_id=source.context.session_id,
                run_id=source.run_id,
                recorded_at=latest.created_at,
            ),
        )

    def _reopened(self, ref: PlanRef) -> list[str]:
        """The steps that were finished, completed, blocked or skipped, and that the
        revision to ``ref`` changed, so that they are open again until recorded against it;
        none once the plan has moved past ``ref``."""
        plan = self.plans.compact_current(self.authority)
        if plan is None or plan.head.ref != ref or ref.version == 1:
            return []
        before = self.plans.version(
            self.authority, PlanRef(plan_id=ref.plan_id, version=ref.version - 1)
        )
        changed = set(step_changes(before.body, plan.version.body).changed)
        return [
            step.step_id
            for step in plan.steps
            if step.step_id in changed
            and step.latest is not None
            and step.latest.status in FINISHED
            and not step.matches_current_step
        ]

    def _read(self, request: Read) -> tuple[dict[str, object], Pin | None]:
        if request.view == "current":
            if request.ref is not None or request.after or request.target is not None:
                raise DocumentError(
                    INVALID_REQUEST, "current plan read cannot name historical pins"
                )
            snapshot = self.plans.compact_current(self.authority)
            if snapshot is None:
                return {"current": None}, None
            summary = self.plans.summary(self.authority)
            if summary is None or (
                snapshot.head.ref,
                snapshot.head.status_revision,
                snapshot.head.progress_cursor,
            ) != (summary.head, summary.status_revision, summary.progress_cursor):
                raise DocumentError(CONFLICT, "plan changed during current read; reread")
            if request.detail == "compact":
                titles = {
                    step.step_id: step
                    for phase in snapshot.version.body.phases
                    for step in phase.steps
                }
                return {
                    "plan": {**_counters(snapshot), "event_cursor": summary.event_cursor},
                    "goal": snapshot.version.body.goal,
                    "steps": [
                        {
                            "step_id": step.step_id,
                            "title": titles[step.step_id].title,
                            **(
                                {"track_id": titles[step.step_id].track_id}
                                if titles[step.step_id].track_id
                                else {}
                            ),
                            **_step_state(step),
                        }
                        for step in snapshot.steps
                    ],
                    "detail": COMPACT,
                    **self._unread(snapshot),
                }, _pin(summary)
            return {
                "head": snapshot.head.model_dump(mode="json"),
                "version": snapshot.version.model_dump(mode="json"),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "step_sha256": step.step_sha256,
                        "matches_current_step": step.matches_current_step,
                        "status": step.latest.status if step.latest else "pending",
                        "step_cursor": step.latest.sequence if step.latest else 0,
                        "latest": step.latest.model_dump(mode="json") if step.latest else None,
                    }
                    for step in snapshot.steps
                ],
                "event_cursor": summary.event_cursor,
                "progress_details": "Use the progress view for complete attributed notes/evidence.",
                **self._unread(snapshot),
            }, _pin(summary)
        if request.ref is None:
            raise DocumentError(INVALID_REQUEST, "historical plan read needs an exact ref")
        if request.view == "version":
            if request.after or request.target is not None:
                raise DocumentError(INVALID_REQUEST, "version read cannot name history cursors")
            return {
                "version": self.plans.version(self.authority, request.ref).model_dump(mode="json")
            }, None
        self.plans.version(self.authority, request.ref)
        limit = request.limit
        target = request.target
        while limit:
            page = self.plans.history(
                self.authority,
                request.ref.plan_id,
                request.view,
                after=request.after,
                target=target,
                limit=limit,
            )
            result: dict[str, object] = {
                "items": [item.model_dump(mode="json") for item in page.items],
                "after": page.after,
                "target": page.target,
                "more": page.more,
                "requested_ref": request.ref.model_dump(mode="json"),
            }
            target = page.target
            try:
                _json(result)
            except DocumentError:
                limit -= 1
            else:
                return result, None
        raise DocumentError(INVALID_REQUEST, "one plan history item exceeds the response budget")


def approval_open(records: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a permission request waits on the user: a call among the run's latest
    ``APPROVALS_SEEN`` tool records refused as ``approval_required`` that no later call of
    the same tool with the same arguments got past."""
    passed: set[tuple[str, str]] = set()
    for index in range(len(records) - 1, max(-1, len(records) - 1 - APPROVALS_SEEN), -1):
        record = records[index]
        key = (str(record.get("tool", "")), str(record.get("arguments", "")))
        if record.get("error") != "approval_required":
            passed.add(key)
        elif key not in passed:
            return True
    return False


def went_on(pin: Pin) -> str:
    """The ID of the plans notice that carried a run past an answer at plan state
    ``pin``: the run's events write it as the notice that the run went on."""
    digest = hashlib.sha256(json.dumps(pin, separators=(",", ":")).encode()).hexdigest()
    return f"plan-open-{digest[:16]}"


class Plans(TurnHook):
    """The session plan in a run: the plan itself, in a notice before the turn, when
    someone other than this run changed it or the run has not seen it, whose delivery
    reads it; and a veto on a closing answer while such a change is unread and the plan
    has open steps. The veto holds the answer once, with the plan in its notice, and takes
    the next closing answer; with no turn to give, the answer stands. A finished plan,
    or one whose steps are all done, blocked or skipped, holds no answer back (lab 41,
    B2).

    An answer with no tool call while the plan is active and has steps that can still be
    worked does not end the run: it goes to the user as a message, and the run takes its
    next turn with the plan in a notice that names the next steps (``goes_on``). Not while
    a question or a permission request waits on the user, nor with no turn left, and once
    per plan state, shared with the automatic continuation of an answered root
    (``continuity.auto_continuation``): an answer at a plan state that has had its
    continuation has its closing, the final review among it."""

    name = "plans"
    veto = True

    def __init__(self, tools: PlanTools) -> None:
        self.tools = tools
        self.retried = False
        """Whether this run's answer was held once already."""

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        return await self._state() if point == "turn" else ()

    async def model_returned(self, run: RunView) -> None:
        await self.tools.model_delivered()

    async def goes_on(self, run: RunView, answer: str) -> Sequence[Notice]:
        # Before ``model_returned``: the plan this model call's notices showed is read now.
        await self.tools.model_delivered()
        self.tools.offer = None
        if run.turns_left <= 0 or run.halted is not None or approval_open(run.tool_records):
            return ()
        found = await self.tools.going_on()
        if found is None or found.pin is None:
            return ()
        return (Notice(self.name, (went_on(found.pin),), PLANS_WENT_ON.text, found.payload),)

    async def went_on(self, run: RunView, notices: Sequence[Notice]) -> bool:
        if run.turns_left <= 0 or run.halted is not None:
            self.tools.offer = None
            return False
        return await self.tools.went_on()

    def summary(self) -> object | None:
        continued = self.tools.continued
        if not continued:
            return None
        return {
            "went_on": len(continued),
            "states": [list(pin) for pin in continued[:MAX_CONTINUED]],
        }

    async def gate(self, effect: Effect) -> Refusal | None:
        try:
            await self.tools.check_dispatch(effect)
        except DocumentError as error:
            return Refusal(error.kind, error.message)
        return None

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        found = await self._state()
        if not (self.tools.requires_read or self.tools.held):
            return Closing("accept", found)
        steps = await self.tools.open_steps()
        if steps is not None and not steps[1]:
            return Closing("accept", found)
        if self.retried or run.turns_left <= 0 or run.halted is not None:
            # Held once already, or no turn to give (a turn limit, a report round, a
            # halt): the answer stands rather than be lost.
            return Closing("accept", found)
        # The notice shows the plan even if an earlier one showed this state.
        shown = found or await self._state(force=True)
        if not shown:
            return Closing("accept")
        self.retried = True
        state = {**cast(dict[str, Any], shown[0].payload), "answer_held": ANSWER_HELD}
        held = Notice(self.name, (), PLANS_HELD.text, state)
        return Closing("retry", (held,), stopped=PLAN_UNREAD)

    async def _state(self, *, force: bool = False) -> tuple[Notice, ...]:
        found = await self.tools.safe_point(force=force)
        if found is None:
            return ()
        if found.pin is not None:
            self.tools.pending_read = found.pin  # its delivery reads the plan
        return (Notice(self.name, (), PLANS.text, found.payload),)
