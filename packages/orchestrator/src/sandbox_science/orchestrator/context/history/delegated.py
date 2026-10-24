# SPDX-License-Identifier: MIT
"""Attempt-scoped folding without importing another agent's reasoning (RC-18)."""

from __future__ import annotations

from dataclasses import replace

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.digest import Rendered
from sandbox_science.orchestrator.model import ChatModel

from .archive import Archive
from .folds import Folder
from .geometry import Shares, geometry
from .view import session_view, settle


async def delegated_folder(
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
    clock: Clock = SYSTEM_CLOCK,
    model: ChatModel | None = None,
) -> Folder:
    """Reuse the fold engine on this child's work only, pinning its original brief.

    Narrative enrichment uses the child's pinned caller; its tokens are recorded with
    the child's usage, which a soft cap checks in on rather than refusing (D102).
    """
    text = "Delegated work brief (unchanged):\n" + brief
    if constraints:
        text += "\n\n" + constraints
    digest = Rendered(text)
    start = geometry(ceiling, 0, chunk, shares)
    # The first call already receives the brief as its task message. Pin it in the
    # state block once rebuilding could fold that original task out of the raw tail.
    view = await threaded(
        session_view,
        archive,
        context,
        geometry=start,
        scope_run=run,
        digest=Rendered(constraints) if constraints else None,
    )
    if view.parts or view.records:
        view = replace(view, digest=text)
    view = await settle(view, archive, context, run=run, parent=parent, kernels=None, clock=clock)
    return Folder(
        archive,
        context,
        run=run,
        parent=parent,
        view=view,
        start=start,
        ceiling=ceiling,
        chunk=chunk,
        digest=lambda _: digest,
        clock=clock,
        model=model,
    )
