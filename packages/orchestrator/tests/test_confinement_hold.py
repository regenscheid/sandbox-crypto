# SPDX-License-Identifier: MIT
"""A held answer in a confined run (consistency review, CR-48): with the loop in the
runtime, a hook's ``holds_answers`` crosses the control boundary (``holds_answers``), and
the loop's ``withheld`` crosses back in its state, so the host's run events leave the
answer out, and say it is being reviewed, until the closing ends. From the cluster's live
test of 2026-11-27, where ``final_review = "hold"`` showed the answer at once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.chat import HELD
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.models import CallbackModel, RuntimeModels
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import records, serving

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
