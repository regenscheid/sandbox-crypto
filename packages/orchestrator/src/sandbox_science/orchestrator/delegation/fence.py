# SPDX-License-Identifier: MIT
"""A delegated run's plan fence (plans-and-delegation spec, PD-34; question 15).

A delegated run gets no plan tools, so it cannot read the plan again as its parent can.
The fence gates outward effects only (D99): its calls that may act beyond the session are
refused while the session's plan is a draft, once the user has changed the plan since
the run started, or once the session was closed or opened again. Calls whose effects stay
in the session (computation, session files, installs through the package cache;
``effects.SESSION_TOOLS``) go on under a draft or an edited plan, which the workspace
shows instead; they are refused only once the session was closed or opened again. A
task's progress on its track is refused once the plan is no longer the version it
started under (PD-9). The first refusal is kept, and the delegation's outcome reports it,
so the parent learns why the run stopped.

Connector tools are judged by the class the MCP proxy gives each listed tool (D96, D99;
delegated agents spec DA-7). A ``read`` stays in the session and passes as computation
does. Another class is outward. Under a draft or user-edited plan, such a call still
reaches the proxy when the proxy says a call of the tool asks a person first: the proxy
holds it, and the user decides it from a card attributed to the child (DA-9). One the
policy would let through unasked is refused, since nobody would see it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.delegation.effects import reach
from sandbox_science.orchestrator.documents.errors import CONFLICT, POLICY_DENIED
from sandbox_science.orchestrator.hooks import Effect, Refusal, TurnHook
from sandbox_science.orchestrator.projects import ProjectStore, SessionSnapshot
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanRef,
    PlanScope,
    SessionPlans,
)

PAGES: Final = 8
"""Pages of plan events a check reads, 16 each; past them the fence holds."""


class ListedTool(Protocol):
    """What the fence reads of a connector tool, as the run's connectors list it."""

    @property
    def effect_class(self) -> str | None: ...

    @property
    def escalates(self) -> bool: ...


ConnectorLookup = Callable[[str], ListedTool | None]
"""A run's connector tool by name, or None for a tool that is not a connector's."""


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
        granted: PlanRef | None = None,
        connectors: ConnectorLookup | None = None,
    ) -> None:
        """Reads the session and its plan as the run starts; call it in a thread.
        ``granted`` is a task's plan version on its track, which its progress needs, and
        ``connectors`` finds the run's connector tools by name."""
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
        self.granted = granted
        self.connectors = connectors

    async def gate(self, effect: Effect) -> Refusal | None:
        """A call of an unnamed tool: an ``effect`` is taken to act outward."""
        return await self.gate_call("", effect)

    async def gate_call(self, tool: str, effect: Effect) -> Refusal | None:
        if effect in ("read", "cancel", "proposal"):
            return None
        if effect == "plan":
            if self.granted is None:
                return None
            refusal = await threaded(self._progress)
        elif (listed := self._connector(tool)) is not None:
            # A read stays in the session; a call the proxy holds for the user is the
            # user's to decide, whatever the plan (D99).
            unfenced = listed.effect_class == "read" or listed.escalates
            refusal = await threaded(self._held if unfenced else self._check)
        elif reach(tool) == "session":
            refusal = await threaded(self._held)
        else:
            refusal = await threaded(self._check)
        if refusal is not None and self.refused is None:
            self.refused = refusal.message
        return refusal

    def _connector(self, tool: str) -> ListedTool | None:
        return self.connectors(tool) if self.connectors is not None and tool else None

    def summary(self) -> object | None:
        return None if self.refused is None else {"refused": self.refused}

    def _progress(self) -> Refusal | None:
        """A task's progress on its track needs the plan version it started under."""
        summary = self.plans.summary(self.authority)
        if summary is None or summary.head != self.granted:
            return Refusal(
                CONFLICT,
                "plan_changed: the session's plan was revised since this delegation started; "
                "its progress on the track is refused, and the parent can delegate the track "
                "again",
            )
        return None

    def _held(self) -> Refusal | None:
        """The session is still the one the run started in: closed or opened again, it
        allows the run no effects, inside the session or out."""
        context = self.context
        current = self.projects.session_snapshot(context.project_id, context.session_id)
        if current != self.session:
            return Refusal(
                POLICY_DENIED, "the session was closed or opened again since you started"
            )
        return None

    def _check(self) -> Refusal | None:
        """An outward effect also needs an active plan the user has not changed since."""
        if (held := self._held()) is not None:
            return held
        summary = self.plans.summary(self.authority)
        if summary is None:
            return None
        if summary.status == "draft":
            return Refusal(
                POLICY_DENIED,
                "the session's plan is a draft, which allows no effects outside the session; "
                "computation and the session's files are not affected",
            )
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
                    "you make no new effects outside the session",
                )
            self.seen = page.after
            if not page.more:
                return None
        return Refusal(CONFLICT, "plan_changed: the plan changed more than this run can read")
