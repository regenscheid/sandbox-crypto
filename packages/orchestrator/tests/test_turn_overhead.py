# SPDX-License-Identifier: MIT
"""The platform's own time between model calls: what it counts once instead of each
turn, the round trips a confined run leaves out, and the record of that time each call
keeps (docs/specs/context-management.md, CM-28; confinement)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, cast

import pytest
from pydantic import JsonValue, ValidationError

from sandbox_science.orchestrator import loop as loop_module
from sandbox_science.orchestrator.confinement.runtime_loop import RemoteHook
from sandbox_science.orchestrator.confinement.transport import Capability, Channel
from sandbox_science.orchestrator.confinement.wire import HOOK_ASKS, HookGiven, adapter
from sandbox_science.orchestrator.consistency.store import scoped
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history.memory import RunMemory
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook, overrides
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import (
    AssistantMessage,
    Message,
    ModelTurn,
    ToolSpec,
    timed,
)
from sandbox_science.orchestrator.plan_tools import Plans
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.worker_client import dump

from .scripted import ScriptedModel, ToolCall
from .test_long_runs import toolbox


def recount(context: AgentContext) -> int:
    """``AgentContext.size`` as it counts a plain list: every message, each time."""
    return 2 + sum(len(dump(m)) + 2 for m in context.working()) + context.tools_chars


def test_a_stored_historys_size_is_counted_once_and_matches_a_recount(tmp_path: Path) -> None:
    """Each stored message is counted as it is first read, and the total is the one a
    recount gives: as the history grows, after a fold's rebuild, when the view is
    restored as it is, and when a new view replaces it."""
    memory = RunMemory.scratch(tmp_path / "scratch.sqlite3", ("p", "s", "r"))
    try:
        system: Message = {"role": "system", "content": "rules"}
        stored = memory.messages([system])
        context = AgentContext(messages=stored, tools_chars=17)
        reads: list[int] = []
        decode = stored.decode

        def counted(value: Any) -> Message:
            reads.append(1)
            return decode(value)

        stored.decode = counted
        for index in range(40):
            context.messages.append({"role": "user", "content": f"message {index} " + "é" * index})
            if index % 7 == 0:
                context.messages.append(AssistantMessage("answer", native=b"\x00native"))
            assert context.size == recount(context)
        reads.clear()
        size = context.size
        assert reads == []  # nothing was read again
        assert size == recount(context)
        context.rebuild([{"role": "user", "content": "the folded view"}])
        context.messages.append({"role": "tool", "content": "after the fold"})
        assert context.size == recount(context)
        kept = context.rebuilt
        context.restore(kept, base=context.base, since=context.since)
        assert context.rebuilt is kept
        assert context.size == recount(context)
        context.restore([{"role": "user", "content": "another view"}], base=3, since=0)
        assert context.size == recount(context)
        context.restore(None, base=0, since=0)
        assert context.size == recount(context)
    finally:
        memory.control.close()


def test_the_scratch_history_waits_for_no_disk_and_the_control_store_does(
    tmp_path: Path,
) -> None:
    """A confined runtime's scratch history is deleted with its attempt: its changes do
    not wait for the disk. The control store's still do (``synchronous=FULL``)."""
    memory = RunMemory.scratch(tmp_path / "scratch.sqlite3", ("p", "s", "r"))
    store = ProjectStore(tmp_path / "state")
    try:
        memory.messages([{"role": "system", "content": "rules"}])
        with memory.control.read() as tx:
            assert tx.sql.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        with store.control.read() as tx:
            assert tx.sql.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        memory.control.close()
        store.close()


class Recorder:
    """A channel that records what the runtime asks and answers as a hook that does
    nothing would."""

    def __init__(self) -> None:
        self.asked: list[Capability] = []

    async def request(self, kind: Capability, value: JsonValue, **options: Any) -> JsonValue:
        self.asked.append(kind)
        if kind in (Capability.NOTICES, Capability.REPORT_NOTICES, Capability.GOES_ON):
            return []
        if kind is Capability.WENT_ON:
            return False
        if kind is Capability.CONSUMPTION:
            return {"ids": []}
        if kind is Capability.CLOSING:
            return adapter(Closing).dump_python(Closing(), mode="json")
        return None


class Nothing(TurnHook):
    name = "nothing"


PLANS = cast(Plans, object.__new__(Plans))
"""The plan hook, unbuilt: only its class's methods are looked at."""


type Ask = Callable[[RemoteHook, Transcript], Awaitable[object]]
POINTS: dict[str, Ask] = {
    "notices": lambda hook, run: hook.notices("turn", run),
    "halt": lambda hook, run: hook.halt(run),
    "last_call": lambda hook, run: hook.last_call(run),
    "report_notices": lambda hook, run: hook.report_notices(run, "turn_limit"),
    "consumption": lambda hook, run: hook.consumption(),
    "model_returned": lambda hook, run: hook.model_returned(run),
    "observe_tool": lambda hook, run: hook.observe_tool(run, {"tool": "x"}),
    "goes_on": lambda hook, run: hook.goes_on(run, "answer"),
    "went_on": lambda hook, run: hook.went_on(run, ()),
    "closing": lambda hook, run: hook.closing(run, "answer"),
    "accept": lambda hook, run: hook.accept(run),
    "finishing": lambda hook, run: hook.finishing(run, "answered"),
}
"""How the loop asks a hook for each of ``HOOK_ASKS``: a method added there is added
here (``test_every_ask_has_its_point``)."""


def test_every_ask_has_its_point() -> None:
    assert set(POINTS) == set(HOOK_ASKS)


@pytest.mark.parametrize(
    "asks", [(), ("halt", "observe_tool", "closing"), ("goes_on", "went_on"), HOOK_ASKS]
)
async def test_a_runtime_asks_the_host_only_for_what_its_hook_does(
    asks: tuple[Any, ...],
) -> None:
    """The host names the methods each hook has of its own; the runtime asks for those
    and takes the default, which does nothing, for the rest, without a round trip."""
    channel = Recorder()
    given = HookGiven(name="h", asks=asks)
    hook = RemoteHook(cast(Channel, channel), "cap-" + "0" * 48, 0, given)
    run = Transcript(context=AgentContext(messages=[{"role": "system", "content": "s"}]))
    for point in POINTS.values():
        await point(hook, run)
    assert {kind.value for kind in channel.asked} == set(asks)
    assert hook.asks == frozenset(asks)


def test_a_hook_given_without_asks_is_asked_everything() -> None:
    """A host that names no methods has the runtime ask for all of them, as before."""
    assert HookGiven(name="h").asks == HOOK_ASKS


def test_a_hook_that_offers_to_go_on_is_asked_to_commit_too() -> None:
    """``goes_on`` and ``went_on`` are named together, as the plan hook, the one hook
    that carries a run past an answer, has both; a hook given one alone is refused."""
    assert {"goes_on", "went_on"} <= {name for name in HOOK_ASKS if overrides(PLANS, name)}
    assert not {"goes_on", "went_on"} & {name for name in HOOK_ASKS if overrides(Nothing(), name)}
    for alone in ("goes_on", "went_on"):
        with pytest.raises(ValidationError, match="asked to commit too"):
            HookGiven(name="h", asks=(alone,))


def test_an_observations_scope_is_checked_in_its_own_change(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state")
    try:
        store.create("p", "minimal", "sha256:" + "c" * 64)
        store.open_session("p", "s", dedicated=False)
        with store.control.write() as tx:
            scoped(tx.sql, "p", "s")
            with pytest.raises(DocumentError, match="no such review session"):
                scoped(tx.sql, "p", "other")
            with pytest.raises(DocumentError, match="no such review project"):
                scoped(tx.sql, "q", "s")
            with pytest.raises(DocumentError, match="invalid review scope"):
                scoped(tx.sql, "p", "../s")
        # A damaged project record is no project to review, as a fresh read finds too.
        with store.control.write() as tx:
            tx.sql.execute("UPDATE projects SET policy='damaged' WHERE project_id='p'")
        with pytest.raises(ProjectError, match="damaged"):
            store.get("p")
        with (
            store.control.write() as tx,
            pytest.raises(DocumentError, match="no such review project"),
        ):
            scoped(tx.sql, "p", "s")
    finally:
        store.close()


class Timed(ScriptedModel):
    """A scripted model whose answers are timed, as a gateway client's are."""

    async def complete(self, messages: Any, tools: Sequence[ToolSpec]) -> ModelTurn:
        return await timed(super().complete(messages, tools))


async def test_each_call_records_the_loops_own_time_around_it() -> None:
    """A call's usage says how long the loop worked before it (``prepared_ms``) and,
    after the first, how long it worked on the previous call's results, its tools' own
    time left out (``recorded_ms``)."""
    model = Timed([ModelTurn(None, (ToolCall("c1", "no_such_tool", "{}"),)), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    first, second = transcript.context.usage
    assert first.duration_ms is not None
    assert second.duration_ms is not None
    assert isinstance(first.prepared_ms, int)
    assert first.recorded_ms is None
    assert isinstance(second.prepared_ms, int)
    assert isinstance(second.recorded_ms, int)


async def test_a_report_round_at_a_limit_is_timed_from_its_turns_start() -> None:
    model = Timed([ModelTurn(None, (ToolCall("c1", "no_such_tool", "{}"),)), ModelTurn("So far.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1)
    assert transcript.stopped == "turn_limit"
    _, report = transcript.context.usage
    assert isinstance(report.prepared_ms, int)
    assert isinstance(report.recorded_ms, int)


class Clock:
    """The loop's monotonic clock, which a scripted model's call and a hook advance by
    the time they say they take, as ``loop.time`` has it."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset


class Slow(ScriptedModel):
    """A scripted model whose calls each take ``MODEL_SECONDS`` of the clock."""

    def __init__(self, clock: Clock, turns: Sequence[ModelTurn]) -> None:
        super().__init__(turns)
        self.clock = clock

    async def complete(self, messages: Any, tools: Sequence[ToolSpec]) -> ModelTurn:
        turn = await super().complete(messages, tools)
        self.clock.offset += MODEL_SECONDS
        return replace(turn, duration_ms=round(MODEL_SECONDS * 1000))


MODEL_SECONDS: Final = 5.0
OFFER_SECONDS: Final = 0.25


class GoesOnce(TurnHook):
    """Carries the run past its first answer, taking ``OFFER_SECONDS`` to offer."""

    name = "goes_once"

    def __init__(self, clock: Clock) -> None:
        self.clock, self.went = clock, False

    async def goes_on(self, run: RunView, answer: str) -> Sequence[Notice]:
        if self.went:
            return ()
        self.clock.offset += OFFER_SECONDS
        return (Notice(self.name, ("n1",), "Go on: ", {}),)

    async def went_on(self, run: RunView, notices: Sequence[Notice]) -> bool:
        self.went = True
        return True


async def test_a_turn_that_goes_on_past_an_answer_is_timed_from_that_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that goes on past a bare answer takes its next call with no tool call
    between: that call's ``recorded_ms`` is the loop's work after the answer, the offer
    included, and neither it nor ``prepared_ms`` counts the answer's own model time."""
    clock = Clock()
    monkeypatch.setattr(loop_module, "time", clock)
    hook = GoesOnce(clock)
    model = Slow(clock, [ModelTurn("Status: step one is next."), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=(hook,))
    assert (transcript.stopped, transcript.answer) == ("answered", "Done.")
    assert hook.went
    first, second = transcript.context.usage
    assert first.recorded_ms is None
    for usage in (first, second):
        assert usage.prepared_ms is not None
        assert 0 <= usage.prepared_ms < MODEL_SECONDS * 1000
    assert second.recorded_ms is not None
    assert OFFER_SECONDS * 1000 <= second.recorded_ms < MODEL_SECONDS * 1000
