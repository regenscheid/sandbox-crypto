# SPDX-License-Identifier: MIT
"""Whether an environment may be used now, for the execution manager.

One ``EnvironmentUse`` record holds what stands in the way of using an environment:
a daemon recovery, a fence until its sandbox is recycled, a release, sessions
closing. ``Usability`` keeps one record for each environment that has any of them,
with the worker's own shutdown, and answers every question about them. The fence
has a persisted part, the recycle intent in the store, which survives a restart;
this is the only place the manager reads it.

The facts overlap: an environment can be recovering while it is fenced or released,
so the record keeps each fact rather than one state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sandbox_science.contracts import ExecutionContext

from .store import Store


@dataclass
class EnvironmentUse:
    """What stands in the way of using one environment; all clear when nothing does."""

    recovering: bool = False
    """A daemon recovery reattaches its executions; one at a time."""
    fence: asyncio.Task[None] | None = None
    """The recycle of its sandbox, which every request waits for."""
    quarantined: bool = False
    """Fenced until its sandbox is proven stopped; the store keeps the recycle intent."""
    releases: int = 0
    """Releases of its executor in progress."""
    closing_sessions: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    """Closes in progress, by project and session."""

    def clear(self) -> bool:
        return not (
            self.recovering
            or self.fence is not None
            or self.quarantined
            or self.releases
            or self.closing_sessions
        )


class Usability:
    """The use record of every environment, and whether the worker is closing."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.closing = False
        """The worker is shutting down: no environment may be used again."""
        self._uses: dict[str, EnvironmentUse] = {}

    def use(self, environment_id: str) -> EnvironmentUse:
        """The environment's record; a clear one, not kept, when nothing stands in its way."""
        return self._uses.get(environment_id) or EnvironmentUse()

    def close(self) -> None:
        self.closing = True

    # Questions.

    def usable(self, context: ExecutionContext) -> bool:
        """Whether nothing stands in the way of using the environment for the session."""
        return not (
            self.closing
            or self.use(context.environment_id).recovering
            or self.fenced(context.environment_id)
            or self.winding_down(context)
        )

    def fenced(self, environment_id: str) -> bool:
        """Whether no new work may start until the environment's sandbox is recycled."""
        use = self.use(environment_id)
        return use.quarantined or use.fence is not None or self.recycle_pending(environment_id)

    def recycle_pending(self, environment_id: str) -> bool:
        """Whether the store holds an intent to recycle the environment's sandbox."""
        return self.store.recycle_needed(environment_id)

    def winding_down(self, context: ExecutionContext) -> bool:
        """Whether the environment's executor or the session is being closed."""
        use = self.use(context.environment_id)
        return bool(use.releases) or (context.project_id, context.session_id) in (
            use.closing_sessions
        )

    def releasing(self, environment_id: str) -> bool:
        return bool(self.use(environment_id).releases)

    def fence(self, environment_id: str) -> asyncio.Task[None] | None:
        """The recycle the environment's requests wait for, if one runs."""
        return self.use(environment_id).fence

    # Changes.

    def begin_recovery(self, environment_id: str) -> bool:
        """Mark a recovery of the environment as running; False if one is, or the worker
        is closing."""
        if self.closing or self.use(environment_id).recovering:
            return False
        self._change(environment_id).recovering = True
        return True

    def end_recovery(self, environment_id: str) -> None:
        self._change(environment_id).recovering = False
        self._prune(environment_id)

    def require_recycle(self, environment_id: str, incarnation: str) -> None:
        """Record that the sandbox must be recycled before the environment is used."""
        self.store.require_recycle(environment_id, incarnation)
        self.quarantine(environment_id)

    def start_fence(
        self, environment_id: str, recycle: Callable[[], asyncio.Task[None]]
    ) -> asyncio.Task[None]:
        """The environment's fence; ``recycle`` starts one unless one runs."""
        self.quarantine(environment_id)
        use = self._change(environment_id)
        if use.fence is None:
            fence = recycle()
            use.fence = fence
            fence.add_done_callback(lambda done: self._fence_done(environment_id, done))
        return use.fence

    def _fence_done(self, environment_id: str, task: asyncio.Task[None]) -> None:
        use = self._uses.get(environment_id)
        if use is not None and use.fence is task:
            use.fence = None
            self._prune(environment_id)

    def sandbox_stopped(self, environment_id: str) -> None:
        """The old sandbox is proven stopped: forget its endpoint and recycle intent and
        lift the quarantine."""
        self.store.finish_recycle(environment_id)
        self.lift_quarantine(environment_id)

    def quarantine(self, environment_id: str) -> None:
        self._change(environment_id).quarantined = True

    def lift_quarantine(self, environment_id: str) -> None:
        if environment_id in self._uses:
            self._uses[environment_id].quarantined = False
            self._prune(environment_id)

    @contextmanager
    def releasing_executor(self, environment_id: str) -> Generator[None]:
        """The environment's executor is being released while the block runs."""
        self._change(environment_id).releases += 1
        try:
            yield
        finally:
            self._uses[environment_id].releases -= 1
            self._prune(environment_id)

    @contextmanager
    def closing_session(self, context: ExecutionContext) -> Generator[None]:
        """The session is being closed in the environment while the block runs."""
        key = (context.project_id, context.session_id)
        closing = self._change(context.environment_id).closing_sessions
        closing[key] = closing.get(key, 0) + 1
        try:
            yield
        finally:
            closing = self._uses[context.environment_id].closing_sessions
            if closing[key] == 1:
                closing.pop(key)
            else:
                closing[key] -= 1
            self._prune(context.environment_id)

    def _change(self, environment_id: str) -> EnvironmentUse:
        return self._uses.setdefault(environment_id, EnvironmentUse())

    def _prune(self, environment_id: str) -> None:
        use = self._uses.get(environment_id)
        if use is not None and use.clear():
            del self._uses[environment_id]
