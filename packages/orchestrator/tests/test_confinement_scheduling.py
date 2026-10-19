# SPDX-License-Identifier: MIT
"""The VM selects recovery branches; the host only admits their scoped effects."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.scheduling import Scheduling
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import (
    Queue,
    ResumeBranch,
    ResumeReady,
    Waiting,
    payload,
    typed,
)
from sandbox_science.orchestrator.projects import run_name


async def test_vm_schedules_each_offered_branch_once_while_root_queue_remains_live() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    child = run_name(datetime.now(UTC), "child")
    parent = run_name(datetime.now(UTC), "parent")
    completed: list[str] = []
    admitted: list[Waiting] = []
    dispatched: list[str] = []
    resume = Waiting(project="p", session="s", request="resume-test")

    async def host_handler(kind: Capability, value: JsonValue) -> JsonValue:
        if kind is Capability.QUEUE:
            return payload(Queue(entries=(resume, Waiting(project="p", session="ordinary"))))
        if kind is Capability.ADMIT:
            admitted.append(typed(Waiting, value))
            return True
        if kind is Capability.RESUME_NEXT:
            return payload(ResumeReady(work=(parent if child in completed else child,)))
        if kind is Capability.RESUME_BRANCH:
            branch = typed(ResumeBranch, value)
            dispatched.append(branch.work)
            if branch.work == child:
                entered.set()
                await release.wait()
            completed.append(branch.work)
            return True
        raise TransportError("denied", "fixture does not offer this capability")

    async def vm_handler(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("denied", "fixture has no agent loops")

    host = Channel(lambda frame: vm.receive(frame), host_handler)
    vm = Channel(lambda frame: host.receive(frame), vm_handler)
    scheduler = Scheduling(vm, frozenset({"p"}), lambda: True)
    try:
        await scheduler.poll()
        await entered.wait()
        await scheduler.poll()
        assert dispatched == [child]
        assert any(entry.session == "ordinary" for entry in admitted)
        release.set()
        await asyncio.gather(*scheduler.active.values())
        await scheduler.poll()
        await asyncio.gather(*scheduler.active.values())
        assert dispatched == [child, parent]
    finally:
        release.set()
        await scheduler.close()
        await vm.close()
        await host.close()


async def test_vm_refuses_cross_project_queue_entries_before_admission() -> None:
    async def handler(kind: Capability, value: JsonValue) -> JsonValue:
        assert kind is Capability.QUEUE
        return payload(Queue(entries=(Waiting(project="other", session="s"),)))

    host = Channel(lambda frame: vm.receive(frame), handler)
    vm = Channel(lambda frame: host.receive(frame), handler)
    scheduler = Scheduling(vm, frozenset({"p"}), lambda: True)
    try:
        with pytest.raises(TransportError, match="outside its scope"):
            await scheduler.poll()
        assert not scheduler.active
    finally:
        await scheduler.close()
        await vm.close()
        await host.close()
