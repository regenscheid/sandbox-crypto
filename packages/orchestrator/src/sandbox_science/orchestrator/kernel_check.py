# SPDX-License-Identifier: MIT
"""Before each turn: whether a session kernel predates the environment's packages (CP-12).

A stateless check. Before each model call the hook reads the worker's observation of
the session's running kernels, which never starts or wakes one, and tells the run once
about each kernel and package revision where the kernel started before the current
revision: another session may have installed packages since. Every execution result
reports the same fact (``kernel_revision_notice``), so nothing is kept between runs
and nothing is acknowledged: a run that fails is told again by the next one. An
observation that fails, or a kernel whose start revision is unknown, tells nothing.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from sandbox_science.contracts import ExecutionContext, KernelObservation

from .hooks import Notice, Point, RunView, TurnHook
from .notices import KERNELS

_log = logging.getLogger(__name__)


class KernelObserver(Protocol):
    async def observe_kernels(self, context: ExecutionContext) -> KernelObservation: ...

    async def close_session(self, context: ExecutionContext, run: str | None = None) -> object: ...


class KernelCheck(TurnHook):
    """Tells a run about each kernel that predates its environment's package revision."""

    name = "kernels"

    def __init__(
        self,
        client: KernelObserver,
        context: ExecutionContext,
        *,
        run: str | None = None,
        sweep: bool = False,
        keep: Callable[[], Awaitable[set[str]]] | None = None,
    ) -> None:
        self.client, self.context = client, context
        self.run = run
        """The kernels the run's cells use: the session's (``None``) or a delegated run's
        own; it is told only about those."""
        self.sweep = sweep
        """A main run closes, at the first observation that succeeds, the run kernels its
        session's earlier runs left when their harness stopped (PD-40): an executor that
        is not connected yet is observed again at the next turn, after the run's first
        call connected it."""
        self.keep = keep
        """Runs whose kernels the sweep leaves: the run's own live delegated runs, once
        delegation starts them."""
        self.told: set[str] = set()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "turn":
            return ()
        try:
            observed = await self.client.observe_kernels(self.context)
        except Exception:  # an unavailable observation tells nothing
            _log.warning("the kernel observation failed; no kernel notice", exc_info=True)
            return ()
        if self.sweep:
            await self._sweep(observed)
        stale = [
            {
                "notice_id": _notice_id(
                    kernel.kernel_id, kernel.generation, kernel.current_revision
                ),
                "language": kernel.language,
                "kernel_id": kernel.kernel_id,
                "generation": kernel.generation,
                "start_revision": kernel.start_revision,
                "current_revision": kernel.current_revision,
            }
            for kernel in observed.kernels
            if kernel.revision_status == "predates" and kernel.run == self.run
        ]
        new = [entry for entry in stale if entry["notice_id"] not in self.told]
        if not new:
            return ()
        ids = tuple(str(entry["notice_id"]) for entry in new)
        self.told.update(ids)
        return (Notice(self.name, ids, KERNELS.text, new),)

    async def _sweep(self, observed: KernelObservation) -> None:
        runs = {kernel.run for kernel in observed.kernels if kernel.run is not None}
        try:
            kept = await self.keep() if self.keep is not None else set[str]()
            for left in sorted(runs - kept):
                await self.client.close_session(self.context, run=left)
        except Exception:  # a cleanup: tried again at the next turn, never stops the run
            _log.warning("could not close the kernels earlier runs left", exc_info=True)
        else:
            self.sweep = False


def _notice_id(kernel: str, generation: int, revision: int | None) -> str:
    key = f"{kernel}\0{generation}\0{revision}".encode()
    return "kernel-" + hashlib.sha256(key).hexdigest()[:24]
