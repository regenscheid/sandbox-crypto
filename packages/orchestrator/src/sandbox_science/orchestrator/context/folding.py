# SPDX-License-Identifier: MIT
"""Folding sees context and a diagnostic call index, independent of loop policy."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sandbox_science.orchestrator.hooks import TurnHook

if TYPE_CHECKING:
    from sandbox_science.orchestrator.hooks import RunView
    from sandbox_science.orchestrator.model import Message
    from sandbox_science.orchestrator.projects import ModelUsage
    from sandbox_science.orchestrator.prompts import PromptPins


class ContextView(Protocol):
    """Read-only conversation inputs shared by hooks and context consumers."""

    @property
    def messages(self) -> Sequence[Message]: ...

    @property
    def prompt_provenance(self) -> PromptPins | None: ...

    @property
    def usage(self) -> Sequence[ModelUsage]: ...

    @property
    def since(self) -> int: ...

    @property
    def size(self) -> int: ...


class FoldingView(ContextView, Protocol):
    """Live fold inputs and updates, preserving bounded payload and usage access."""

    @property
    def tools_chars(self) -> int: ...

    @property
    def sent(self) -> Sequence[Message]: ...

    def rebuild(self, view: Sequence[Message]) -> None:
        """Replace the working view after its canonical fold has been kept."""
        ...

    def restore(self, view: Sequence[Message] | None, *, base: int, since: int) -> None:
        """Apply a trusted remote fold's view and exact measurement offsets."""
        ...


@dataclass(frozen=True)
class FoldPoint:
    """A safe point with live context and the execution loop's diagnostic call index."""

    context: FoldingView
    call_index: int


class Folds(Protocol):
    """Local or remote folding; execution owns the decision to attempt a model call."""

    async def before_call(self, point: FoldPoint) -> bool:
        """Fold if needed; false means the working view cannot fit another call."""
        ...

    async def overflow(self, point: FoldPoint) -> bool:
        """Whether folding after a refused model call made room for a retry."""
        ...


class FoldEngine(Folds, Protocol):
    """The authoritative engine's lifecycle; remote folding need not own cleanup."""

    async def finish_background(self) -> None:
        """Cancel unfinished enrichment, account it, and join any canonical save."""
        ...

    async def response_returned(self) -> None:
        """Reset overflow tracking after the loop commits an answered response."""
        ...

    def summary(self) -> object | None:
        """The engine's final accounting and reconstruction diagnostics."""
        ...


class FoldLifecycle(TurnHook):
    """Keep folding at its existing hook position while context owns its lifecycle."""

    name = "folds"

    def __init__(self, engine: FoldEngine) -> None:
        self.engine = engine

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self._held()

    @asynccontextmanager
    async def _held(self) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await self.engine.finish_background()

    async def model_returned(self, run: RunView) -> None:
        await self.engine.response_returned()

    def summary(self) -> object | None:
        return self.engine.summary()
