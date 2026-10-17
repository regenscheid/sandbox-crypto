# SPDX-License-Identifier: MIT
"""Remember concrete kernel identities without implying they can be reconstructed."""

from __future__ import annotations

import json

from sandbox_science.contracts import SYSTEM_CLOCK, ExecutionContext, KernelObservation
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.worker_client import WorkerClient

from .records import Ownership, check_owner


class KernelJournal:
    def __init__(self, control: ControlStore, owner: Ownership, scope: str | None) -> None:
        self.control, self.owner, self.scope = control, owner, scope

    async def record(self, observation: KernelObservation) -> None:
        await threaded(self._record, observation)

    def _record(self, observation: KernelObservation) -> None:
        owner = self.owner
        kernels = [k.model_dump(mode="json") for k in observation.kernels if k.run == self.scope]
        # An absent observation cannot erase evidence that a kernel existed.
        if not kernels:
            return
        encoded = json.dumps(kernels)
        if len(encoded.encode()) > 16384:
            raise ValueError("kernel identity observation exceeds its storage bound")
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            tx.sql.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'kernels','latest',?,?) "
                "ON CONFLICT(project,session,work,kind,identity) DO UPDATE "
                "SET detail=excluded.detail,observed=excluded.observed",
                (owner.project, owner.session, owner.work, SYSTEM_CLOCK.now().timestamp(), encoded),
            )

    async def reconcile(self, client: WorkerClient, context: ExecutionContext) -> None:
        expected = await threaded(self._expected)
        if not expected:
            return
        observation = await client.observe_kernels(context)
        actual = {
            (k.language, k.kernel_id, k.generation)
            for k in observation.kernels
            if k.run == self.scope
        }
        if not expected <= actual:
            raise ControlError(
                CONFLICT, "lost_kernel: saved kernel identity or generation is unavailable"
            )

    def _expected(self) -> set[tuple[str, str, int]]:
        owner = self.owner
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
                "AND kind='kernels' AND identity='latest'",
                (owner.project, owner.session, owner.work),
            ).fetchone()
        return (
            set()
            if row is None
            else {(k["language"], k["kernel_id"], k["generation"]) for k in json.loads(row[0])}
        )
