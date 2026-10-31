# SPDX-License-Identifier: MIT
"""A confined child's turns (D102): its soft turn cap checks in, and only its
``turn_ceiling`` stops it, as in an unconfined run. The resource hook's turn limit crosses
the control boundary (``turn_limit``), and the host holds the runtime to it."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.models import CallbackModel, RuntimeModels
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops, state
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import HookRequest, payload
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.continuity.resources import ResourceRequest, ResourceRequests
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.hooks import RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_confinement_drain import prepared_attempt
from .test_delegation import configure, decide, wait
from .test_delegation_tasks import DONE
from .test_run_service import records, serving
from .test_tool_journal import projects as projects

pytestmark = pytest.mark.slow


ANSWER = json.dumps(DONE)


def step(n: int) -> Reply:
    return Reply(calls=[call("shell", script=f"printf step-{n}")])


async def confined_tree(
    harness: Harness, tmp_path: Path, delegated: Sequence[Reply]
) -> tuple[RunRecord, RunRecord, list[Capability]]:
    """A root that delegates one task, waits, extends the task's check-in, and waits again,
    with every run's loop in the runtime; the root's record, the child's, and the
    capabilities the runtime asked of the host."""
    asked: list[Capability] = []
    main = [
        Reply(calls=[call("delegate", task="Take three steps.")]),
        wait(),
        decide("extend", "Go on."),
        wait(),
        Reply("Done."),
    ]
    async with serving(harness, *main, serve=False) as service:
        harness.gateway.delegated = list(delegated)
        models = ModelGateway(harness.gateway.url, API_KEY)

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            asked.append(kind)
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
        vm_models = RuntimeModels(models, frozenset({"main"}), vm_channel)
        host = HostLoops(host_channel, frozenset({PROJECT}))
        runtime = RuntimeLoops(vm_channel, models, tmp_path / "scratch")
        runtime.scratch.mkdir()
        profile = ModelProfile(alias="main")
        service.harness.main = Role(
            CallbackModel(host_channel, "main", kind="main", role="main", profile=profile),
            "main",
        )
        service.harness.delegated = Role(
            CallbackModel(
                host_channel, "main", kind="delegated", role="delegated", profile=profile
            ),
            "main",
        )

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-steps", "Take three steps", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            assert not harness.gateway.delegated, "the child ended before its script did"
            kept = records(harness)
            (root,) = [r for r in kept if r.parent is None]
            (child,) = [r for r in kept if r.parent is not None]
            return root, child, asked
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await models.close()


def check_ins(harness: Harness) -> tuple[ResourceRequest, ...]:
    store = ProjectStore(harness.state)
    try:
        return ResourceRequests(store.control).page(PROJECT, SESSION).requests
    finally:
        store.close()


async def test_a_confined_child_extended_at_its_soft_turn_cap_goes_on_and_answers(
    harness: Harness, tmp_path: Path
) -> None:
    """At its soft cap of two turns the child checks in, its parent extends it, and it
    takes its third turn and answers, rather than ending at two."""
    configure(harness, depth=1, task_turns=2)
    _, child, asked = await confined_tree(harness, tmp_path, [step(1), step(2), Reply(ANSWER)])
    assert child.stopped == "answered"
    assert child.final_text == ANSWER
    assert [t["tool"] for t in child.tools] == ["shell", "shell"]
    (request,) = check_ins(harness)
    assert (request.resource, request.used, request.total) == ("turns", 2, 2)
    assert request.decision is not None
    assert request.decision.total == 302
    assert Capability.TURN_LIMIT in asked


async def test_a_confined_childs_turn_ceiling_still_stops_it(
    harness: Harness, tmp_path: Path
) -> None:
    """Past the extension, its ``turn_ceiling`` of three turns is a hard limit: the child
    makes its report round there and stops ``turn_limit``."""
    configure(harness, depth=1, task_turns=2, turn_ceiling=3)
    _, child, _ = await confined_tree(
        harness, tmp_path, [step(1), step(2), step(3), Reply("Partial: three steps.")]
    )
    assert child.stopped == "turn_limit"
    assert child.final_text == "Partial: three steps."
    assert [t["tool"] for t in child.tools] == ["shell", "shell", "shell"]
    (request,) = check_ins(harness)
    assert request.decision is not None
    assert request.decision.total == 3


async def test_the_host_holds_the_runtime_to_the_hooks_turn_limit(projects: ProjectStore) -> None:
    """The runtime's loop state must carry the admitted limit until a hook sets another,
    then that one: a runtime cannot raise its own."""

    class Ceiling(TurnHook):
        name = "ceiling"

        async def turn_limit(self, run: RunView) -> int | None:
            return 7

    prepared = prepared_attempt(projects, cast(Toolbox, object()), (Ceiling(), TurnHook()))
    given = prepared.given
    run = Transcript(
        context=AgentContext(
            messages=[{"role": "system", "content": "system"}], max_input_chars=100000
        ),
        max_turns=3,
    )

    def asked(slot: int) -> JsonValue:
        return payload(HookRequest(attempt=given.attempt, slot=slot, state=state(run)))

    assert await prepared.call(Capability.TURN_LIMIT, asked(1)) is None  # no override
    run.max_turns = 9
    with pytest.raises(TransportError, match="changed the admitted attempt limits"):
        await prepared.call(Capability.TURN_LIMIT, asked(0))
    run.max_turns = 3
    assert await prepared.call(Capability.TURN_LIMIT, asked(0)) == 7
    with pytest.raises(TransportError, match="changed the admitted attempt limits"):
        await prepared.call(Capability.HALT, asked(1))  # the runtime kept the old limit
    run.max_turns = 7
    assert await prepared.call(Capability.HALT, asked(1)) is None
    await prepared.close()
