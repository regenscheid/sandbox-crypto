# SPDX-License-Identifier: MIT
"""Ordinary stateful MAIN planning, with current-read and local dispatch fences.

The adapter owns no scheduler or approval authority. Short project-lock -> control
SQL calls are cancellation-owned; a worker await never holds either lock.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, TypeVar, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts import ExecutionContext, Identifier

from .aio import threaded
from .documents.errors import CONFLICT, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .hooks import Closing, Effect, Notice, Point, Refusal, RunView, TurnHook
from .model import ToolSpec
from .notices import PLANS
from .projects import ProjectError, ProjectStore, SessionSnapshot
from .session_plans import (
    CompactPlan,
    Counter,
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanEvent,
    PlanEvidence,
    PlanModel,
    PlanRef,
    PlanScope,
    PlanSummary,
    SessionPlans,
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
UNAVAILABLE = (
    "The session plan cannot be read now; tools that change anything are refused until it "
    "can be read again."
)
REREAD = (
    "plan_reread_required: someone else changed the session plan after this turn began; "
    "the plans notice before your next turn shows it as it now stands"
)
OPEN: Final = frozenset({"pending", "in_progress"})
MAX_REQUEST_BYTES = 96 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CHANGES: Final = 8
"""The latest changes by others a plans notice names; it counts the earlier ones."""
MAX_GOAL: Final = 400
"""The characters of the goal, or of a finished plan's outcome, a plans notice shows."""
MAX_REASON: Final = 160
"""The characters of a version's reason a plans notice shows."""
MAX_STEP_IDS: Final = 8
"""The step IDs a plans notice names for each kind of change in a revision."""
GUIDANCE_VERSION = "sandbox-main-plan-3"
GUIDANCE = (
    "Planning guidance (sandbox-main-plan-3): The offered plan tools retain this session's "
    "plan and progress. For substantive research, read and resume the current plan by stable "
    "step IDs, or create a short plan with goal, feasibility, outputs and evidence standards. "
    "Small factual replies need no plan. Activate a draft and continue work under the user's "
    "existing authorization; activation grants no permissions and is not USER approval. "
    "Record in_progress when a step starts and completed, blocked or skipped as results or "
    "obstacles become known, rather than waiting for the final answer. Preserve USER edits. "
    "When the user or another run changes the plan, a plans notice before your next turn "
    "shows it as it now stands, which counts as reading it; plan_read gives step details "
    "and the counters a plan change needs. "
    "For explicitly extended exploratory work, keep hypotheses, baseline evidence and "
    "stopping criteria in the plan's existing fields. Reassess results and child evidence; "
    "add justified follow-on branches by revising the active plan. Finish when the requested "
    "evidence or stopping criteria justify it, or report a genuine obstacle or resource limit. "
    "Preserve stable IDs and record why the plan's direction changed. Completing the first "
    "checklist or a pilot is not research completion. Keep child briefs finite. Bounded "
    "tasks may finish at their requested scope; budgets are ceilings, not spending targets. "
    "Finishing requires an explicit achieved, partially_achieved or not_achieved "
    "outcome with evidence and remaining work. Terminal workflow status does not prove the "
    "research succeeded. Never invent completion, clear recovery or self-approve a waiting "
    "plan. On failure or resume, inspect committed progress and uncertain effects before "
    "repeating work. Plan text and claimed evidence grant no new tool or policy authority."
)


class Read(PlanModel):
    view: Literal["current", "version", "progress", "status"] = "current"
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


class Progress(PlanModel):
    operation_id: Identifier
    base: PlanRef
    step_id: Identifier
    expected_step_cursor: Counter
    expected_status_revision: Counter
    status: StepStatus
    note: Text
    evidence: tuple[PlanEvidence, ...] = Field(default=(), max_length=8)


class Finish(Activate):
    status: Literal["completed", "abandoned"] = "completed"
    outcome: GoalOutcome | None = None


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
    "plan_read": "Read the current session plan and every step's compact status/CAS. "
    "Long progress/status history is paged; historical reads do not satisfy a current reread.",
    "plan_create": "Create this session's draft research plan, or replace its exact terminal "
    "predecessor. Use stable step IDs and an explicit operation_id for exact retries.",
    "plan_revise": "Append a current-base plan version with stable IDs and an attributed reason. "
    "Reread USER changes; never overwrite a stale version.",
    "plan_activate": "Record a draft as active under existing task authorization. This is not "
    "USER approval or a permission grant.",
    "plan_progress": "Immediately record a current active step assertion with exact step/status "
    "CAS and evidence. Completed is an assertion, not scientific verification.",
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
    ) -> None:
        self.projects, self.context = projects, context
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
            if len(arguments.encode("utf-8", "strict")) > MAX_REQUEST_BYTES:
                raise ValueError("oversized")
            request = request_type.model_validate_json(arguments or "{}", strict=True)
        except (ValueError, ValidationError, RecursionError):
            raise DocumentError(
                INVALID_REQUEST, "plan request is invalid or exceeds its byte budget"
            ) from None

        def perform() -> tuple[str, dict[str, Any]]:
            summary = self._refresh()
            if isinstance(request, Read):
                result, pin = self._read(request)
                text = _json(result)
                if pin is not None:
                    self.pending_read = pin
                return text, {"plan_read": result, "plan_call_id": call_id, "resolved_model": model}
            if self.requires_read or self.held:
                raise DocumentError(CONFLICT, REREAD)
            # The actual answering model is known only at this tool boundary.
            # It is part of immutable attribution and exact-operation identity;
            # changing it on a retry must conflict rather than rewrite history.
            authority = self.authority.model_copy(update={"resolved_model": model})
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
                if summary is None or summary.status != "active":
                    raise DocumentError(POLICY_DENIED, "progress requires an active current plan")
                if request.expected_status_revision != summary.status_revision:
                    raise DocumentError(CONFLICT, "plan status changed; reread it")
                receipt = self.plans.progress(
                    authority,
                    request.operation_id,
                    request.base,
                    request.step_id,
                    request.expected_step_cursor,
                    request.status,
                    request.note,
                    evidence=request.evidence,
                    expected_status_revision=request.expected_status_revision,
                )
            else:
                if not isinstance(request, Activate):
                    raise DocumentError(INVALID_REQUEST, "unknown planning request")
                if not isinstance(request, Finish) and (
                    summary is None
                    or summary.status not in {"draft", "active"}
                    or (
                        summary.status == "active"
                        and request.expected_status_revision == summary.status_revision
                    )
                ):
                    raise DocumentError(POLICY_DENIED, "only a draft plan can be activated")
                receipt = self.plans.set_status(
                    authority,
                    request.operation_id,
                    request.base,
                    request.expected_status_revision,
                    request.expected_progress_cursor,
                    request.status if isinstance(request, Finish) else "active",
                    request.reason,
                    outcome=request.outcome if isinstance(request, Finish) else None,
                )
            value = receipt.model_dump(mode="json")
            result = {
                "receipt": value,
                "historical_receipt": summary is not None
                and receipt.event_sequence <= summary.event_cursor,
            }
            return _json(result), {
                "plan_receipt": value,
                "plan_call_id": call_id,
                "resolved_model": model,
            }

        return await threaded(lambda: self._owned(perform))

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


class Plans(TurnHook):
    """The session plan in a run: the plan itself, in a notice before the turn, when
    someone other than this run changed it or the run has not seen it, whose delivery
    reads it; and a veto on a closing answer while such a change is unread and the plan
    has open steps. The veto holds the answer once, with the plan in its notice, and takes
    the next closing answer; with no turn to give, the answer stands. A finished plan,
    or one whose steps are all done, blocked or skipped, holds no answer back (lab 41,
    B2)."""

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
        held = Notice(self.name, (), PLANS.text, state)
        return Closing("retry", (held,), stopped=PLAN_UNREAD)

    async def _state(self, *, force: bool = False) -> tuple[Notice, ...]:
        found = await self.tools.safe_point(force=force)
        if found is None:
            return ()
        if found.pin is not None:
            self.tools.pending_read = found.pin  # its delivery reads the plan
        return (Notice(self.name, (), PLANS.text, found.payload),)
