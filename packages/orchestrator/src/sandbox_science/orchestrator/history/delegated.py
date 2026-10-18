# SPDX-License-Identifier: MIT
"""Attempt-scoped folding without importing another agent's reasoning (RC-18)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, replace

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.chat import ANSWER_TOKENS
from sandbox_science.orchestrator.continuity.token_usage import fold_tokens, spent_tokens
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.digest import Rendered
from sandbox_science.orchestrator.model import ChatModel, Message, ToolSpec
from sandbox_science.orchestrator.worker_client import dump

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
    allowance: FoldAllowance | None = None,
) -> Folder:
    """Reuse the fold engine on this child's work only, pinning its original brief.

    Narrative enrichment uses the child's pinned caller and shares its grant.
    Without room for enrichment and a report, the same engine keeps a skeleton.
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
        admit=allowance.admit if allowance is not None else None,
        release=allowance.release if allowance is not None else None,
    )


class FoldAllowance:
    """One child's remaining attempt grant, shared with its background summaries.

    The local hold estimates tokens, not money. Observed usage replaces it after
    each attempt. Resume deducts durable prior-attempt usage from the cumulative
    grant before constructing this object, so it must read only this attempt.
    """

    def __init__(
        self, control: ControlStore, context: ExecutionContext, *, work: str, run: str, tokens: int
    ):
        self.control, self.context = control, context
        self.work, self.run, self.tokens = work, run, tokens
        self.pending = 0

    def _used(self, *, folds_only: bool) -> int:
        with self.control.read() as tx:
            count = fold_tokens if folds_only else spent_tokens
            return count(
                tx.sql, self.context.project_id, self.context.session_id, self.work, run=self.run
            )

    async def additional(self) -> int:
        """Fold tokens not already in the foreground run's assistant usage."""
        observed = await threaded(self._used, folds_only=True)
        return observed + self.pending

    async def admit(self, messages: Sequence[Message], specs: Sequence[ToolSpec]) -> bool:
        size = 2 + sum(len(dump(message)) + 2 for message in messages)
        size += len(dump([asdict(spec) for spec in specs]))
        estimate = size // 4 + ANSWER_TOKENS
        used = await threaded(self._used, folds_only=False)
        # Leave a second estimated call for the foreground's next/report round.
        # A retry is separately admitted against the first attempt's saved usage.
        if self.pending or used + 2 * estimate >= self.tokens:
            return False
        self.pending = estimate
        return True

    def release(self) -> None:
        self.pending = 0
