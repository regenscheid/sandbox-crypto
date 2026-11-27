# SPDX-License-Identifier: MIT
"""A held answer in a confined run (consistency review, CR-48): with the loop in the
runtime, a hook's ``holds_answers`` crosses the control boundary (``holds_answers``), and
the loop's ``withheld`` crosses back in its state, so the host's run events leave the
answer out, and say it is being reviewed, until the closing ends. From the cluster's live
test of 2026-11-27, where ``final_review = "hold"`` showed the answer at once."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import ANY

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.chat import HELD, WENT_ON
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.models import CallbackModel, RuntimeModels
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops, state
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import HookRequest, payload
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.plan_tools import PLAN_OPEN
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_confinement_drain import prepared_attempt
from .test_run_service import records, serving
from .test_session_plans import actor, made, status
from .test_tool_journal import projects as projects

pytestmark = pytest.mark.slow


class Holds(TurnHook):
    """Keeps the run's closing answer from the user until its closing ends, as a final
    review in ``hold`` does, and records what the run's events showed meanwhile."""

    name = "holds_fixture"

    def __init__(self, events: Any) -> None:
        self.events = events
        self.closed = False
        self.during: list[tuple[str, dict[str, Any]]] = []

    @property
    def holds_answers(self) -> bool:
        return not self.closed

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.during = self.events()
        self.closed = True
        return Closing()


async def test_a_held_answer_is_not_in_a_confined_runs_events_until_its_closing_ends(
    harness: Harness, tmp_path: Path
) -> None:
    asked: list[Capability] = []
    async with serving(harness, Reply("The held answer."), serve=False) as service:
        models = ModelGateway(harness.gateway.url, API_KEY)

        def events() -> list[tuple[str, dict[str, Any]]]:
            with service.stores.projects.control.read() as tx:
                rows = tx.sql.execute(
                    "SELECT kind,data FROM run_events WHERE project_id=? AND session_id=? "
                    "ORDER BY seq",
                    (PROJECT, SESSION),
                ).fetchall()
            return [(kind, json.loads(data)) for kind, data in rows]

        holds = Holds(events)

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
        service.harness.main = Role(
            CallbackModel(host_channel, "main", role="main", profile=ModelProfile(alias="main")),
            "main",
        )

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            options["hooks"] = (*options["hooks"], holds)
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-held", "Answer, held", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            (record,) = records(harness)
            assert (record.stopped, record.final_text) == ("answered", "The held answer.")
            # While the closing ran: no answer, and the notice that it is being reviewed.
            assert not [d for kind, d in holds.during if kind == "assistant"]
            assert ("notice", {"source": "reviews", "ids": [], "text": HELD}) in holds.during
            # Once it ended, the answer.
            assert [d["text"] for kind, d in events() if kind == "assistant"] == [
                "The held answer."
            ]
            # The runtime asked the host's hook, which alone knows.
            assert "holds_answers" in {kind.value for kind in asked}
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await models.close()


class HoldsForRound(TurnHook):
    """A final review in ``hold`` whose findings take a round: the first closing asks for
    it with the answer held, and the hook holds answers until the round's closing."""

    name = "holds_round_fixture"

    def __init__(self, events: Any) -> None:
        self.events = events
        self.closings = 0
        self.during: list[tuple[str, dict[str, Any]]] = []

    @property
    def holds_answers(self) -> bool:
        return self.closings < 2

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings += 1
        if self.closings == 1:
            notice = Notice("reviews", ("f1",), "Review findings.", {"finding": "f1"})
            return Closing("respond", (notice,), hold_answer=True)
        self.during = self.events()
        return Closing()


async def test_a_held_answer_stays_out_of_a_confined_runs_events_through_its_round(
    harness: Harness, tmp_path: Path
) -> None:
    """The first answer is not shown when its review ends: the user sees the answering
    turn's answer once that turn's closing ends, and the record keeps both (CR-48)."""
    script = (Reply("The first answer."), Reply("The answering turn's answer."))
    async with serving(harness, *script, serve=False) as service:
        models = ModelGateway(harness.gateway.url, API_KEY)

        def events() -> list[tuple[str, dict[str, Any]]]:
            with service.stores.projects.control.read() as tx:
                rows = tx.sql.execute(
                    "SELECT kind,data FROM run_events WHERE project_id=? AND session_id=? "
                    "ORDER BY seq",
                    (PROJECT, SESSION),
                ).fetchall()
            return [(kind, json.loads(data)) for kind, data in rows]

        holds = HoldsForRound(events)

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

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            return await host.capability(kind, value)

        host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
        vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
        vm_models = RuntimeModels(models, frozenset({"main"}), vm_channel)
        host = HostLoops(host_channel, frozenset({PROJECT}))
        runtime = RuntimeLoops(vm_channel, models, tmp_path / "scratch")
        runtime.scratch.mkdir()
        service.harness.main = Role(
            CallbackModel(host_channel, "main", role="main", profile=ModelProfile(alias="main")),
            "main",
        )

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            options["hooks"] = (*options["hooks"], holds)
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-round", "Answer, held", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            (record,) = records(harness)
            assert (record.answer, record.final_text) == (
                "The answering turn's answer.",
                "The answering turn's answer.",
            )
            assert "The first answer." in [m.get("content") for m in record.messages]
            # Through the round: neither answer, and the notice that it is being reviewed.
            assert not [d for kind, d in holds.during if kind == "assistant" and d["text"]]
            assert ("notice", {"source": "reviews", "ids": [], "text": HELD}) in holds.during
            # Once it ended, the round's answer alone.
            assert [d["text"] for kind, d in events() if kind == "assistant" and d["text"]] == [
                "The answering turn's answer."
            ]
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await models.close()


class Closes(TurnHook):
    """Keeps every closing answer from the user while it decides, as a final review in
    ``hold`` does, and counts its closings."""

    name = "closes_fixture"

    def __init__(self) -> None:
        self.closings: list[str | None] = []

    @property
    def holds_answers(self) -> bool:
        return True

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings.append(answer)
        return Closing()


async def test_a_confined_answer_while_the_plan_is_open_goes_on_in_the_run(
    harness: Harness, tmp_path: Path
) -> None:
    """Baseline run 2, R1: a status reply while the plan had pending steps ended the run,
    its final review ran on the reply, and a new run started cold. Confined, the plan
    hook, which only the host holds, carries the run past the reply (``goes_on``): the
    reply is in the run's events at once, never held, then the notice that the run went
    on; one run, one closing, at its real end, and no new root for the same plan state."""
    asked: list[Capability] = []
    carriers: set[object] = set()
    script = (Reply("Status: step one is next."), Reply("Stopping here."))
    async with serving(harness, *script, serve=False) as service:
        plans = SessionPlans(service.stores.documents)
        author = actor(project=PROJECT, session=SESSION)
        status(plans, made(plans, author).ref, "active", authority=author)
        models = ModelGateway(harness.gateway.url, API_KEY)

        def events() -> list[tuple[str, dict[str, Any]]]:
            with service.stores.projects.control.read() as tx:
                rows = tx.sql.execute(
                    "SELECT kind,data FROM run_events WHERE project_id=? AND session_id=? "
                    "ORDER BY seq",
                    (PROJECT, SESSION),
                ).fetchall()
            return [(kind, json.loads(data)) for kind, data in rows]

        closes = Closes()

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            asked.append(kind)
            if kind in (Capability.GOES_ON, Capability.WENT_ON) and isinstance(value, dict):
                carriers.add(value.get("slot"))
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
        service.harness.main = Role(
            CallbackModel(host_channel, "main", role="main", profile=ModelProfile(alias="main")),
            "main",
        )

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            options["hooks"] = (*options["hooks"], closes)
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-status", "Where are we?", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            (record,) = records(harness)
            assert (record.stopped, record.final_text) == ("answered", "Stopping here.")
            assert closes.closings == ["Stopping here."]
            assert record.summaries["plans"] == {"went_on": 1, "states": [ANY]}
            (went_on,) = record.delivered["plans"]
            # The second call saw the plan's notice, which names the next step.
            (_, second) = harness.gateway.main
            notice = str(second[-1]["content"])
            assert PLAN_OPEN in json.loads(notice[notice.index("{") :])["plan_open"]
            shown = [
                (kind, d.get("text"))
                for kind, d in events()
                if kind == "assistant" or (kind == "notice" and d["source"] in ("plans", "reviews"))
            ]
            assert shown == [
                ("assistant", "Status: step one is next."),
                ("notice", WENT_ON),
                ("notice", HELD),
                ("assistant", "Stopping here."),
            ]
            assert ("notice", {"source": "plans", "ids": [went_on], "text": WENT_ON}) in events()
            assert {Capability.GOES_ON, Capability.WENT_ON} <= set(asked)
            # Only the plan hook names them in its ``asks``: no other hook is asked.
            assert len(carriers) == 1, carriers
            assert None not in carriers
            # The plan state had its continuation: no new root is queued for it.
            await service.auto_continuation_tick()
            assert [m.message_id for m in service.runs.messages(PROJECT, SESSION)] == ["msg-status"]
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await models.close()


async def test_the_host_carries_a_run_on_only_past_an_answer_with_no_tool_call(
    projects: ProjectStore,
) -> None:
    """Review of de930eca: a runtime must not spend a plan state's continuation for a
    reply that was not an answer with no tool call: the host checks the last committed
    message itself, for the offer and for its commitment."""

    class Offers(TurnHook):
        name = "offers_fixture"

        def __init__(self) -> None:
            self.committed = 0

        async def goes_on(self, run: RunView, answer: str) -> Sequence[Notice]:
            return (Notice(self.name, (), "Go on: ", {}),)

        async def went_on(self, run: RunView, notices: Sequence[Notice]) -> bool:
            self.committed += 1
            return True

    offers = Offers()
    prepared = prepared_attempt(projects, cast(Toolbox, object()), (offers,))
    run = Transcript(
        context=AgentContext(
            messages=[{"role": "system", "content": "system"}], max_input_chars=100000
        ),
        max_turns=3,
    )
    asked = HookRequest(attempt=prepared.given.attempt, slot=0, state=state(run), answer="x")
    for kind in (Capability.GOES_ON, Capability.WENT_ON):
        with pytest.raises(TransportError, match="only an answer with no tool call"):
            await prepared.call(kind, payload(asked))
    assert offers.committed == 0
    assert not prepared.notices
    await prepared.close()
