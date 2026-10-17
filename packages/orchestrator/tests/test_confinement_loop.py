# SPDX-License-Identifier: MIT
"""Production loop/capability boundary with a local worker and scripted HTTP gateway."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.models import (
    CallbackGateway,
    CallbackModel,
    RuntimeModels,
)
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.model import ModelGateway

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import records, serving

pytestmark = pytest.mark.slow


async def test_vm_loop_commits_host_state_without_a_local_harness_fallback(
    harness: Harness, tmp_path: Path
) -> None:
    async with serving(
        harness,
        Reply(calls=[call("shell", command="printf confined")]),
        Reply("Done."),
        serve=False,
    ) as service:
        gateway_models = ModelGateway(harness.gateway.url, API_KEY)
        vm_models = RuntimeModels(gateway_models, frozenset({"main"}))

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            return await host.capability(kind, value)

        async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
            if kind is Capability.RUN:
                return await runtime.run(value)
            if kind is Capability.CANCEL:
                return await runtime.cancel(value)
            if kind is Capability.MODEL:
                return await vm_models.complete(value)
            if kind is Capability.MODEL_INFO:
                return await vm_models.info(value)
            raise TransportError("denied", "fixture refused capability")

        host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
        vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
        host = HostLoops(host_channel, frozenset({PROJECT}))
        runtime = RuntimeLoops(vm_channel, gateway_models, tmp_path / "scratch")
        runtime.scratch.mkdir()
        service.harness.main = Role(CallbackModel(host_channel, "main"), "main")
        service.harness.gateway = CallbackGateway(host_channel)
        service.harness.loop_runner = host.run
        try:
            service.runs.post(PROJECT, SESSION, "msg-runtime", "Print confined", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            (record,) = records(harness)
            assert record.final_text == "Done."
            assert record.failure is None
            assert record.stopped == "answered"
            assert not runtime.active
            assert not host.attempts
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await gateway_models.close()
