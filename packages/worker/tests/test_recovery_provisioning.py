# SPDX-License-Identifier: MIT
"""Queued provisioning retains the request's owner instead of adopting a newer epoch."""

from __future__ import annotations

import asyncio

import pytest

from sandbox_science.contracts import ExecutionContext, ProvisionRequest, ProvisionState
from sandbox_science.contracts.context import ExecutionAuthority
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.worker.provisioner import Provisioner

from .harness import view
from .test_provisioner import CONTEXT, Factory
from .test_provisioner import make_world as make_world

pytestmark = pytest.mark.slow


async def test_queued_provisioning_cannot_adopt_new_session_epoch(make_world: Factory) -> None:
    waiting = asyncio.Event()
    released = asyncio.Event()

    class Queued(Provisioner):
        async def _operate(self, op):
            waiting.set()
            await released.wait()
            await self.stages.execute(
                op,
                ("forbidden-installer",),
                label="probe-epoch",
                stage="check",
                timeout=1,
                limit=1024,
            )

    world = await make_world(kind=Queued)
    first = RecoveryFence(project_id="p1", session_id="s1", epoch=1)
    world.store.recovery_fences.install(first)
    context = ExecutionContext(
        **CONTEXT, authority=ExecutionAuthority(work="work", run="run", epoch=1)
    )
    record, created = await world.provisioner.submit(
        ProvisionRequest(
            operation_id="before-resume", context=context, purpose="test", conda=("pari",)
        )
    )
    assert created
    await waiting.wait()
    world.store.recovery_fences.install(first.model_copy(update={"epoch": 2}))
    released.set()
    ended = await world.until(record.provision_id, lambda op: op.state is ProvisionState.FINISHED)
    assert ended.error is not None
    assert ended.executions == ()
    # Ending updates the package-gate connection record, without opening its channel.
    assert view(world.manager).connection("env1").channel is None
    assert world.store.find_operation("p1", f"{record.provision_id}.probe-epoch") is None
