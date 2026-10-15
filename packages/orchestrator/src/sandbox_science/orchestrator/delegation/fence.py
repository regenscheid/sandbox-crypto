# SPDX-License-Identifier: MIT
"""A delegated run's plan fence (plans-and-delegation spec, PD-34; question 15).

A delegated run gets no plan tools, so it cannot read the plan again as its parent can.
Its calls that may change something are refused while the session's plan is a draft,
once the user has changed the plan since the run started, or once the session was
closed or opened again. The first refusal is kept, and the delegation's outcome reports
it, so the parent learns why the check stopped.
"""

from __future__ import annotations

from typing import Final

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.documents.errors import CONFLICT, POLICY_DENIED
from sandbox_science.orchestrator.hooks import Effect, Refusal, TurnHook
from sandbox_science.orchestrator.projects import ProjectStore, SessionSnapshot
from sandbox_science.orchestrator.session_plans import PlanAuthority, PlanScope, SessionPlans

PAGES: Final = 8
"""Pages of plan events a check reads, 16 each; past them the fence holds."""


class ChildFence(TurnHook):
    """Refuses a delegated run's effects that the session's plan does not allow."""

    name = "fence"

    def __init__(
        self,
        plans: SessionPlans,
        projects: ProjectStore,
        context: ExecutionContext,
        *,
        run_id: str,
    ) -> None:
        """Reads the session and its plan as the run starts; call it in a thread."""
        self.plans, self.projects, self.context = plans, projects, context
        scope = PlanScope(project_id=context.project_id, session_id=context.session_id)
        # It only reads: agent attribution without a grant is the main role's.
        self.authority = PlanAuthority(scope=scope, role="main", run_id=run_id)
        self.session: SessionSnapshot = projects.session_snapshot(
            context.project_id, context.session_id
        )
        summary = plans.summary(self.authority)
        self.seen = summary.event_cursor if summary is not None else 0
        """The plan events read so far, none of them the user's."""
        self.refused: str | None = None
        """Why the fence first refused a call, if it did."""

    async def gate(self, effect: Effect) -> Refusal | None:
        if effect in ("read", "cancel", "plan", "proposal"):
            return None
        refusal = await threaded(self._check)
        if refusal is not None and self.refused is None:
            self.refused = refusal.message
        return refusal

    def summary(self) -> object | None:
        return None if self.refused is None else {"refused": self.refused}

    def _check(self) -> Refusal | None:
        context = self.context
        current = self.projects.session_snapshot(context.project_id, context.session_id)
        if current != self.session:
            return Refusal(
                POLICY_DENIED, "the session was closed or opened again since you started"
            )
        summary = self.plans.summary(self.authority)
        if summary is None:
            return None
        if summary.status == "draft":
            return Refusal(POLICY_DENIED, "the session's plan is a draft, which allows no effects")
        for _ in range(PAGES):
            if self.seen >= summary.event_cursor:
                return None
            page = self.plans.events(
                self.authority, after=self.seen, target=summary.event_cursor, limit=16
            )
            if any(event.author.role == "user" for event in page.items):
                return Refusal(
                    CONFLICT,
                    "plan_changed: the user changed the session's plan since you started; "
                    "you make no new effects",
                )
            self.seen = page.after
            if not page.more:
                return None
        return Refusal(CONFLICT, "plan_changed: the plan changed more than this run can read")
