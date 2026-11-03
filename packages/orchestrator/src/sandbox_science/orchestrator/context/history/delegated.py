# SPDX-License-Identifier: MIT
"""Attempt-scoped folding without importing another agent's reasoning (RC-18)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.context.compaction import ProviderCompaction
from sandbox_science.orchestrator.digest import Rendered
from sandbox_science.orchestrator.model import ChatModel

from .archive import Archive
from .folds import Folder
from .geometry import Geometry, Shares, geometry
from .view import View, session_view, settle


async def delegated_folder(
    archive: Archive,
    context: ExecutionContext,
    *,
    run: str,
    parent: str,
    brief: str,
    constraints: str | None,
    ceiling: int,
    refresh: Callable[[], str | None] | None = None,
    chunk: int | None,
    shares: Shares,
    clock: Clock = SYSTEM_CLOCK,
    model: ChatModel | None = None,
) -> Folder:
    """Reuse the fold engine on this child's work only, pinning its original brief.

    ``refresh`` gives the state block's own part again at each rebuild, as the records
    have it then, such as the references of the messages shown since (CM-37b); without
    it, ``constraints`` stays. Narrative enrichment uses the child's pinned caller; its
    tokens are recorded with the child's usage, which a soft cap checks in on rather
    than refusing (D102).
    """
    pinned = _pinning(brief)
    text = pinned(constraints)

    def digest(_: int) -> Rendered:
        return Rendered(text if refresh is None else pinned(refresh()))

    start = geometry(ceiling, 0, chunk, shares)
    view = await _view(
        archive, context, run=run, parent=parent, start=start, constraints=constraints,
        pinned=text, clock=clock,
    )  # fmt: skip
    return Folder(
        archive,
        context,
        run=run,
        parent=parent,
        view=view,
        start=start,
        ceiling=ceiling,
        chunk=chunk,
        digest=digest,
        clock=clock,
        model=model,
    )


async def delegated_compaction(
    archive: Archive,
    context: ExecutionContext,
    *,
    run: str,
    parent: str,
    brief: str,
    constraints: str | None,
    ceiling: int,
    chunk: int | None,
    shares: Shares,
    trigger: int,
    clock: Clock = SYSTEM_CLOCK,
) -> ProviderCompaction:
    """The child's view as ``delegated_folder`` builds it, under the provider-compaction
    mode's engine, which never folds (CM-38b). The brief is pinned in the state block
    from the start: the adapter keeps the state block after each compaction, as the fold
    engine keeps it after each fold, so the brief stays in view."""
    text = _pinning(brief)(constraints)
    view = await _view(
        archive, context, run=run, parent=parent, start=geometry(ceiling, 0, chunk, shares),
        constraints=constraints, pinned=text, clock=clock, always=True,
    )  # fmt: skip
    return ProviderCompaction(view, trigger=trigger, ceiling=ceiling)


def _pinning(brief: str) -> Callable[[str | None], str]:
    pinned = "Delegated work brief (unchanged):\n" + brief
    return lambda block: pinned + ("\n\n" + block if block else "")


async def _view(
    archive: Archive,
    context: ExecutionContext,
    *,
    run: str,
    parent: str,
    start: Geometry,
    constraints: str | None,
    pinned: str,
    clock: Clock,
    always: bool = False,
) -> View:
    """The child's view at its start: its own work only, the brief pinned in its state
    block once rebuilding could fold the original task out of the raw tail (``always``:
    from the start), as the first call receives the brief as its task message."""
    view = await threaded(
        session_view,
        archive,
        context,
        geometry=start,
        scope_run=run,
        digest=Rendered(constraints) if constraints else None,
    )
    if view.parts or view.records or always:
        view = replace(view, digest=pinned)
    return await settle(view, archive, context, run=run, parent=parent, kernels=None, clock=clock)
