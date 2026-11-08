# SPDX-License-Identifier: MIT
"""A confined child's turns (D102): its soft turn cap checks in, and only its
``turn_ceiling`` stops it, as in an unconfined run. The resource hook's turn limit crosses
the control boundary (``turn_limit``), and the host holds the runtime to it; so do the
report round's notices of the child's hooks (``report_notices``), which the host admits."""

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
from sandbox_science.orchestrator.confinement.wire import (
    FoldRequest,
    HookRequest,
    LogAppend,
    WireMessage,
    payload,
)
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.continuity.resources import ResourceRequest, ResourceRequests
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.hooks import Notice, RunView, TurnHook
from sandbox_science.orchestrator.loop import REPORT_SOURCE, Transcript, notice_message
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.notices import OUTCOME_REPORT, REPORT
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_confinement_drain import prepared_attempt
from .test_delegation import configure, submitted, wait
from .test_delegation_tasks import DONE
from .test_run_service import records, serving
from .test_tool_journal import projects as projects
from .test_tool_journal import toolbox as journal_toolbox

pytestmark = pytest.mark.slow


ANSWER = json.dumps(DONE)


def step(n: int) -> Reply:
    return Reply(calls=[call("shell", script=f"printf step-{n}")])


async def confined_tree(
    harness: Harness, tmp_path: Path, delegated: Sequence[Reply]
) -> tuple[RunRecord, RunRecord, list[Capability]]:
    """A root that delegates one task and waits for it, with every run's loop in the
    runtime: the task's first check-in at its soft cap goes on for one step by itself
    (D102). The root's record, the child's, and the capabilities the runtime asked of the
    host."""
    asked: list[Capability] = []
    main = [Reply(calls=[call("delegate", task="Take three steps.")]), wait(), Reply("Done.")]
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


async def test_a_confined_child_at_its_first_soft_turn_cap_goes_on_and_answers(
    harness: Harness, tmp_path: Path
) -> None:
    """At its soft cap of two turns the child checks in, the platform gives it one step,
    as its first check-in at a soft cap, and it takes its third turn, submits and answers,
    rather than ending at two."""
    configure(harness, depth=1, task_turns=2)
    script = [step(1), step(2), *submitted(DONE, "Done.")]
    _, child, asked = await confined_tree(harness, tmp_path, script)
    assert child.stopped == "answered"
    assert child.final_text == "Done."
    assert [t["tool"] for t in child.tools] == ["shell", "shell", "submit_result"]
    (request,) = check_ins(harness)
    assert (request.resource, request.used, request.total) == ("turns", 2, 2)
    assert request.decision is not None
    assert (request.decision.total, request.decision.automatic) == (302, True)
    assert Capability.TURN_LIMIT in asked


async def test_a_confined_childs_turn_ceiling_still_stops_it(
    harness: Harness, tmp_path: Path
) -> None:
    """Past the extension, its ``turn_ceiling`` of three turns is a hard limit: the child
    makes its report round there and stops ``turn_limit``. Its outcome's notice for that
    round crosses the host, which admits it, and the JSON the report gives is its result,
    partial at the limit (PD-19)."""
    configure(harness, depth=1, task_turns=2, turn_ceiling=3)
    report = "Out of turns. My result:\n```json\n" + ANSWER + "\n```"
    _, child, asked = await confined_tree(
        harness, tmp_path, [step(1), step(2), step(3), Reply(report)]
    )
    assert child.stopped == "turn_limit"
    assert child.final_text == report
    assert Capability.REPORT_NOTICES in asked
    last = [str(m.get("content")) for m in harness.gateway.children[-1] if m["role"] == "user"]
    assert any(OUTCOME_REPORT.text in text for text in last)
    result = cast(dict[str, Any], child.summaries["outcome"])["result"]
    assert (result["status"], result["child_status"]) == ("partial", "complete")
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


async def test_the_host_admits_a_report_round_only_at_a_limit_it_knows(
    projects: ProjectStore,
) -> None:
    """The orchestrator audit: the report round's notice, one of the loop's own, names a
    limit the loop takes one at, or one a hook of the attempt gave as its last call
    (``last_call``), which the host heard as it answered; any other is refused."""

    class Budget(TurnHook):
        name = "budget_fixture"

        async def last_call(self, run: RunView) -> str | None:
            return "run_budget"

    async def unused(arguments: object) -> object:
        raise AssertionError("no tool runs")

    prepared = prepared_attempt(projects, journal_toolbox(unused), (Budget(),))
    attempt = prepared.given.attempt
    run = Transcript(
        context=AgentContext(
            messages=[{"role": "system", "content": "system"}], max_input_chars=100000
        ),
        max_turns=3,
    )
    await prepared.call(
        Capability.LOG_INIT, payload(FoldRequest(attempt=attempt, state=state(run)))
    )

    async def report(limit: str) -> JsonValue:
        notice = Notice(REPORT_SOURCE, (), REPORT.text, {"limit": limit, "detail": "a limit"})
        message = WireMessage.of(notice_message(notice))
        appended = LogAppend(attempt=attempt, message=message, kind="notice", notice=notice)
        return await prepared.call(Capability.LOG_APPEND, payload(appended))

    with pytest.raises(TransportError, match="its limit is none the loop or the attempt's hooks"):
        await report("run_budget")  # no hook has given it
    asked = payload(HookRequest(attempt=attempt, slot=0, state=state(run)))
    assert await prepared.call(Capability.LAST_CALL, asked) == "run_budget"
    await report("run_budget")
    with pytest.raises(TransportError, match="its limit is none the loop or the attempt's hooks"):
        await report("planted")
    await report("turn_limit")
    await prepared.close()
