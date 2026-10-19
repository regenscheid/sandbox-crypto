# SPDX-License-Identifier: MIT
"""One root claim owns the live tree, including a parent's child-only waiting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from sandbox_science.contracts.environment_claims import EnvironmentClaim, EnvironmentClaimResult
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, children
from .test_delegation import delegating as delegating

pytestmark = pytest.mark.slow


async def test_parent_retains_the_environment_until_its_child_and_final_answer_end(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims: list[EnvironmentClaim] = []
    free = asyncio.Event()

    async def retain(client: WorkerClient, request: EnvironmentClaim) -> EnvironmentClaimResult:
        claims.append(request)
        return EnvironmentClaimResult(
            environment_id=request.context.environment_id,
            state="held" if request.seconds else "released",
            sandbox_id="fixture-managed-sandbox",
            expires_at=datetime.now(UTC) + timedelta(seconds=request.seconds)
            if request.seconds
            else None,
        )

    async def child_waits() -> None:
        await free.wait()

    monkeypatch.setattr(WorkerClient, "claim_environment", retain)
    root_task = asyncio.create_task(
        delegating.run(
            "root",
            [Reply(calls=[CHECK]), Reply("Done already."), Reply("Done with the child.")],
            delegated=[Reply(REPORT, before=child_waits)],
        )
    )
    store = ProjectStore(delegating.state)
    state = RunState(store.control)

    def waiting() -> bool:
        lease = state.lease(PROJECT, SESSION)
        return lease is not None and any(
            event.kind == "waiting" and event.data.get("delegation_ids")
            for event in state.events(PROJECT, SESSION, lease.run)
        )

    try:
        await eventually(waiting)
        assert len({request.holder for request in claims}) == 1
        assert all(request.seconds > 0 for request in claims)
        assert not root_task.done()
    finally:
        free.set()
        store.close()
    root = await root_task
    assert root.final_text == "Done with the child."
    assert [record.stopped for record in children(delegating, root)] == ["answered"]
    assert len({request.holder for request in claims}) == 1
    assert claims[-1].seconds == 0
    assert claims[-1].sandbox_id == "fixture-managed-sandbox"
