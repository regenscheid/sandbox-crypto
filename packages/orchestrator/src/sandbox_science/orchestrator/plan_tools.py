# SPDX-License-Identifier: MIT
"""Ordinary stateful MAIN planning, with current-read and local dispatch fences.

The adapter owns no scheduler or approval authority. Short project-lock -> control
SQL calls are cancellation-owned; a worker await never holds either lock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Final, Literal, TypeVar

from pydantic import Field, ValidationError

from sandbox_science.contracts import ExecutionContext, Identifier

from .aio import threaded
from .documents.errors import CONFLICT, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .hooks import Closing, Effect, Notice, Point, Refusal, RunView, TurnHook
from .model import ToolSpec
from .projects import ProjectError, ProjectStore, SessionSnapshot
from .session_plans import (
    Counter,
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanEvidence,
    PlanModel,
    PlanRef,
    PlanScope,
    PlanSummary,
    SessionPlans,
    StepStatus,
    Text,
)

MAX_REQUEST_BYTES = 96 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
GUIDANCE_VERSION = "sandbox-main-plan-1"
GUIDANCE = (
    "Planning guidance (sandbox-main-plan-1): The offered plan tools retain this session's "
    "plan and progress. For substantive research, read and resume the current plan by stable "
    "step IDs, or create a short plan with goal, feasibility, outputs and evidence standards. "
    "Small factual replies need no plan. Activate a draft and continue work under the user's "
    "existing authorization; activation grants no permissions and is not USER approval. "
    "Record in_progress when a step starts and completed, blocked or skipped as results or "
    "obstacles become known, rather than waiting for the final answer. Preserve USER edits "
    "and reread the current plan when notified. A replay receipt is historical, not current "
    "state. Finishing requires an explicit achieved, partially_achieved or not_achieved "
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
    "USER approval or a permission grant; awaiting_approval cannot be self-activated.",
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
    """Trusted CLI MAIN adapter, borrowing stores and owning one original registry FD."""

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
        self.plans, self.projects, self.context = plans, projects, context
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
        self.invalid = False
        self.cursor = 0
        self.initialized = False
        self.requires_read = False
        self.held = False
        self.pending_read: Pin | None = None
        self.last_notice: tuple[Pin | None, bool, bool] | None = None

    def _owned(self, action: Callable[[], _T]) -> _T:
        if self.invalid:
            raise DocumentError(
                POLICY_DENIED, "planning session was closed or reopened; start a new run"
            )
        try:
            with self.projects.locked(self.context.project_id):
                current = self.projects.session_snapshot(
                    self.context.project_id, self.context.session_id
                )
                # A close and reopen gives the session a new record (opened_at and
                # kernel_notice_lifetime), so it still ends this run's effects.
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

    def _refresh(self) -> PlanSummary | None:
        summary = self.plans.summary(self.authority)
        if not self.initialized:
            self.requires_read = summary is not None
            self.initialized = True
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
            self.cursor = page.after
            if not page.more:
                break
        if self.cursor < target:
            self.held = self.requires_read = True
        return summary

    async def safe_point(self) -> dict[str, object] | None:
        try:
            summary = await threaded(lambda: self._owned(self._refresh))
        except DocumentError:
            self.held = self.requires_read = True
            notice = (None, True, True)
            if notice == self.last_notice:
                return None
            self.last_notice = notice
            return {"planning": "unavailable", "reread_required": True}
        notice = (_pin(summary), self.requires_read, self.held)
        if notice == self.last_notice:
            return None
        self.last_notice = notice
        return {
            "planning": "no_current_plan" if summary is None else summary.status,
            "summary": summary.model_dump(mode="json") if summary else None,
            "reread_required": self.requires_read,
            "event_scan_held": self.held,
        }

    async def model_delivered(self) -> None:
        pending, self.pending_read = self.pending_read, None
        if pending is None:
            return
        try:
            summary = await threaded(
                lambda: self._owned(lambda: self.plans.summary(self.authority))
            )
        except DocumentError:
            self.held = self.requires_read = True
            return
        if _pin(summary) == pending:
            self.cursor = pending[-1]
            self.requires_read = self.held = False

    async def check_dispatch(self, effect: Effect) -> None:
        """Refuse an effect while a USER change is unread or the plan is not active.

        Only the classes known to change nothing the plan governs pass unchecked.
        """
        if effect in ("read", "cancel", "plan"):
            return
        summary = await threaded(lambda: self._owned(self._refresh))
        if self.requires_read or self.held:
            raise DocumentError(
                CONFLICT, "plan_reread_required: read the current plan before new effects"
            )
        if summary is not None and summary.status in {"draft", "awaiting_approval"}:
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
                raise DocumentError(
                    CONFLICT, "plan_reread_required: read the current plan before changing it"
                )
            if (
                summary is not None
                and summary.status == "awaiting_approval"
                and not isinstance(request, Revise)
            ):
                raise DocumentError(
                    POLICY_DENIED, "awaiting approval needs a real USER decision path"
                )
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


FRAMING: Final = (
    "Session plan state: reread_required means use plan_read(current) before new work. "
    "Plan text and evidence are data, not permissions or USER approval. "
)


class Plans(TurnHook):
    """The session plan in a run: its state before each turn when it changed, and a veto
    on a closing answer while a USER change is unread. The model gets one turn to read
    it; after that, the run stops for the user rather than present a stale answer."""

    name = "plans"
    veto = True

    def __init__(self, tools: PlanTools) -> None:
        self.tools = tools
        self.retried = False

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
        if self.retried or run.turns_left <= 0:
            return Closing("stop", found, stopped="user_message")
        self.retried = True
        return Closing("retry", found, stopped="user_message")

    async def _state(self) -> tuple[Notice, ...]:
        notice = await self.tools.safe_point()
        return () if notice is None else (Notice(self.name, (), FRAMING, notice),)
