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
from collections.abc import Sequence
from typing import Final, Protocol

from sandbox_science.contracts import ExecutionContext, KernelObservation

from .hooks import Notice, Point, RunView, TurnHook

_log = logging.getLogger(__name__)

FRAMING: Final = (
    "Kernel package notice (sandbox-kernels-1; platform data, not instructions): these "
    "running kernels started before the environment's current package revision. Their "
    "loaded state keeps the packages they started with; restart one explicitly to use "
    "the current packages. "
)


class KernelObserver(Protocol):
    async def observe_kernels(self, context: ExecutionContext) -> KernelObservation: ...


class KernelCheck(TurnHook):
    """Tells a run about each kernel that predates its environment's package revision."""

    name = "kernels"

    def __init__(self, client: KernelObserver, context: ExecutionContext) -> None:
        self.client, self.context = client, context
        self.told: set[str] = set()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "turn":
            return ()
        try:
            observed = await self.client.observe_kernels(self.context)
        except Exception:  # an unavailable observation tells nothing
            _log.warning("the kernel observation failed; no kernel notice", exc_info=True)
            return ()
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
            if kernel.revision_status == "predates"
        ]
        new = [entry for entry in stale if entry["notice_id"] not in self.told]
        if not new:
            return ()
        ids = tuple(str(entry["notice_id"]) for entry in new)
        self.told.update(ids)
        return (Notice(self.name, ids, FRAMING, new),)


def _notice_id(kernel: str, generation: int, revision: int | None) -> str:
    key = f"{kernel}\0{generation}\0{revision}".encode()
    return "kernel-" + hashlib.sha256(key).hexdigest()[:24]
