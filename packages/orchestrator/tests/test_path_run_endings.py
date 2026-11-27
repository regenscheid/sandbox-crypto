# SPDX-License-Identifier: MIT
"""Every way a main run ends, walked end to end (the run-service spec RS-6 to RS-16 and
the ``stopped`` event of its RS-14).

``ENDINGS`` is the table: one row per stop reason a main run's code can assign, with
what the specification says is observable once the run has ended. Each row is driven
through the run service against the in-process worker and the scripted gateway of
``loop_harness``, once with the loop in the service's own process (``direct``) and once
with it in the confined runtime, whose hooks reach the host over the in-memory channel
(``confined``). The test then reads what is left: the saved record, the ``stopped``
event, the research work's state, the message queue, whether a report round was taken,
what the service starts next without the user, and what the session's next run is given
first. ``UNWRITTEN`` names the stop reasons no row drives yet, and why.

A row's ``known`` names the checks the code fails today, each with the audit's
identifier (the path audit of 2026-11-27: V-nn are its violations, I-n its invariants;
"decision:" an open owner decision, asserted as the auditor recommends). Such a case is
``xfail(strict=True, raises=KnownViolationError)``: it raises that only when exactly
those checks fail, so any other failure is a plain failure, and it fails loudly the day
the behaviour is fixed. The cross cases after the table walk the transitions the audit's violations
name, in the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

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
from sandbox_science.orchestrator.context import agent
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.continuity.records import Work, WorkRecords
from sandbox_science.orchestrator.continuity.resources import ResourceRequests
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.loop import MAX_TURNS, RUNAWAY, Transcript
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.notices import FRAMINGS, REPORT, SUPERVISOR
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, RunRecord
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.runstate import RunEvent
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import (
    API_KEY,
    PROJECT,
    SESSION,
    USER_HEADER,
    USER_PLANS,
    Harness,
    Reply,
    call,
    start_job,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import Gate, runs, serving

Json = dict[str, Any]
Way = Literal["direct", "confined"]
WAYS: Final[tuple[Way, ...]] = ("direct", "confined")
Next = Literal["nothing", "recovery", "continuation", "chat"]
Relay = Callable[[Capability, JsonValue], Awaitable[JsonValue]]
Forge = Callable[[Capability, JsonValue, Relay], Awaitable[JsonValue]]
TASK: Final = "Go on"
"""The user's message that starts each case's run."""
NEXT: Final = "Next."
"""The user's next message, after the run ended."""
NEW_ATTEMPT: Final = "new-attempt"
"""How ``given`` names the notice that opens a Resume's attempt (``continuity.hook``)."""


class KnownViolationError(Exception):
    """The checks a case fails today, as the audit found them."""


def verdict(found: Mapping[str, str | None], known: Mapping[str, str]) -> None:
    """Fail plainly on a check that fails and is not known; raise ``KnownViolationError`` when the
    known checks, and only those, fail; pass when every check holds, which a strict
    ``xfail`` turns into a failure: the behaviour was fixed, and ``known`` is stale."""
    unexpected = [failure for name, failure in found.items() if failure and name not in known]
    assert not unexpected, unexpected
    fixed = [name for name in known if not found.get(name)]
    assert not fixed or len(fixed) == len(known), f"now holds, no longer known: {fixed}"
    failing = [f"{known[name]} ({found[name]})" for name in known if found.get(name)]
    if failing:
        raise KnownViolationError("; ".join(failing))


def expected_to_fail(known: Mapping[str, str]) -> list[pytest.MarkDecorator]:
    if not known:
        return []
    reason = "; ".join(dict.fromkeys(known.values()))
    return [pytest.mark.xfail(strict=True, raises=KnownViolationError, reason=reason)]


# --- What a case drives ------------------------------------------------------------------


@dataclass
class Case:
    """One case's run, as it is driven: the session's service and the gate a scripted
    reply may wait at."""

    harness: Harness
    service: RunService
    monkeypatch: pytest.MonkeyPatch
    gate: Gate = field(default_factory=Gate)

    def work(self) -> Work:
        """The research work of the run that holds the session."""
        lease = self.service.runs.lease(PROJECT, SESSION)
        assert lease is not None
        works = WorkRecords(self.service.stores.projects.control)
        found = works.for_run(PROJECT, SESSION, lease.run)
        assert found is not None
        return found


Step = Callable[[Case], Coroutine[Any, Any, None]]

BUDGET_REFUSAL: Final = json.dumps(
    {"error": {"type": "budget_exceeded", "message": "Budget has been exceeded: 1.0 > 1.0"}}
)
"""LiteLLM's refusal of a call for its key's budget."""
CONTEXT_REFUSAL: Final = json.dumps(
    {"error": {"code": "context_length_exceeded", "message": "The input is too long."}}
)
"""An OpenAI-compatible refusal of a call for the model's context."""


async def priced_earlier(case: Case) -> None:
    """A project whose earlier run spent past its configured daily cost limit (RS-6a)."""
    await case.harness.run("run-priced", [Reply("A priced answer.", cost="0.75")])
    case.service.runs.set_cost_limit(PROJECT, 0.5)


def activate_plan(harness: Harness) -> None:
    """The user's plan for the session, active, with its one step still to work."""
    harness.user_plan()
    documents = harness.documents()
    try:
        plans = SessionPlans(documents)
        current = plans.current(USER_PLANS)
        assert current is not None
        head = current.head
        plans.set_status(
            USER_PLANS,
            "user-activates",
            head.ref,
            head.status_revision,
            head.progress_cursor,
            "active",
            "USER steering",
        )
    finally:
        documents.close()


async def planned(case: Case) -> None:
    activate_plan(case.harness)


async def cancel_at_gate(case: Case) -> None:
    """The user cancels while the run's model call waits at the gate (RS-11)."""
    await asyncio.wait_for(case.gate.entered.wait(), 20)
    lease = case.service.runs.lease(PROJECT, SESSION)
    assert lease is not None
    case.service.runs.cancel(PROJECT, SESSION, lease.run)
    await case.service.maintenance_tick()  # the service reads the user's cancel
    case.gate.release.set()


async def shut_down_at_gate(case: Case) -> None:
    """The service shuts down while the run's model call waits (RS-12)."""
    await asyncio.wait_for(case.gate.entered.wait(), 20)
    await case.service.close()
    case.gate.release.set()


async def bridge_lost_at_gate(case: Case) -> None:
    """The runtime's connection is lost while the run's model call waits: the service
    ends as for a shutdown, with the loss marked transient (D100)."""
    case.service.lost = "bridge_loss"
    await shut_down_at_gate(case)


async def block_work(case: Case) -> None:
    """An operator blocks the run's work while its model call is out (tree recovery)."""
    work = case.work()
    WorkRecords(case.service.stores.projects.control).block(
        PROJECT, SESSION, work.work, "operator_block"
    )


async def stop_work(case: Case) -> None:
    """The user stops the run's work from the research tree while its model call is out."""
    work = case.work()
    WorkRecords(case.service.stores.projects.control).stop(PROJECT, SESSION, work.work)


async def lower_memory_ceiling(case: Case) -> None:
    """The run's working state passes its ceiling at its next model call. The ceiling,
    64 MiB, has no setting, so the one injection that patches a constant lowers it while
    the model call is out (the fixture of ``test_confinement_loop`` patches the property
    that checks it instead)."""
    case.monkeypatch.setattr(agent, "VIEW_BYTES", 1)


async def interrupted_reply(kind: Capability, value: JsonValue, runtime: Relay) -> JsonValue:
    """A runtime that answers its attempt ``interrupted`` (V-18), after running it."""
    reply = await runtime(kind, value)
    if kind is Capability.RUN and isinstance(reply, dict):
        return {**reply, "interrupted": True, "state": None}
    return reply


# --- The table -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Ending:
    """One way a main run ends, and what the specification says is left of it."""

    name: str
    stopped: str
    """The run's ``stopped``, in its record and its ``stopped`` event."""
    script: Callable[[Case], Sequence[Reply]]
    saved: bool = True
    """Whether a run record is saved (RS-11: a cancelled run's is; RS-16: an interrupted
    run has none)."""
    answer: str | None = None
    final_text: str | None = None
    failure: str | None = None
    reason: str | None = None
    """The ``stopped`` event's ``reason``, for the endings that give one."""
    owed: frozenset[str] = frozenset()
    """The work the run leaves owed, which its ``stopped`` event names (I2): ``jobs``,
    ``children``, ``messages``, ``questions``, ``plan``, ``findings``."""
    report: bool = False
    """Whether the run took a report round: every limit does (the owner's rule)."""
    work: tuple[str, str | None] = ("completed", "answered")
    """The research work's state and reason once the run ended."""
    messages: str = "delivered"
    """The state the run's message is left in."""
    next: Next = "nothing"
    """What the service starts next without the user."""
    given: tuple[str, ...] = (TASK, NEXT)
    """What the session's next run is given first (``Left.given``)."""
    setup: Step | None = None
    during: Step | None = None
    max_turns: int = MAX_TURNS
    runtime: Forge | None = None
    """How the confined runtime answers the host, where a case forges its answers."""
    known: Mapping[str, str] = field(default_factory=dict[str, str])
    """Checks the code fails today, each with the audit's identifier, for both ways."""
    confined_known: Mapping[str, str] = field(default_factory=dict[str, str])
    """Further checks the confined arrangement fails today."""
    ways: tuple[Way, ...] = WAYS

    def known_in(self, way: Way) -> dict[str, str]:
        return {**self.known, **(self.confined_known if way == "confined" else {})}


LIMIT_REPORTS: Final = "V-04: this limit ends the run without a report round"
HELD_UNSAID: Final = "I2, V-06: the stopped event does not name the message it leaves held"
PLAN_UNSAID: Final = "I2: the stopped event does not name the active plan's workable steps"
CONFINED_CANCEL: Final = (
    "V-11: a confined cancel saves no record; the service writes the stop as for a lost run"
)
UNEXPLAINED: Final = "I2: the stopped event says neither why nothing starts next nor what to do"
RESENT: Final = "V-08: a Resume sends the saved task again as a new user message"


ENDINGS: Final[tuple[Ending, ...]] = (
    Ending(
        "answered",
        "answered",
        lambda case: [Reply("Done.")],
        answer="Done.",
        final_text="Done.",
    ),
    Ending(
        "answered_with_plan_open",
        "answered",
        lambda case: [Reply("First."), Reply("Second.")],
        setup=planned,
        answer="Second.",
        final_text="Second.",
        owed=frozenset({"plan"}),
        given=("digest", "plans", TASK, "plans", "plans", NEXT),
        known={"owed": PLAN_UNSAID},
    ),
    Ending(
        "turn_limit",
        "turn_limit",
        lambda case: [Reply(calls=[call("plan_read")])],
        max_turns=1,
        reason="check_in",
        report=True,
        work=("paused", "turn_limit"),
        known={"report": LIMIT_REPORTS},
    ),
    Ending(
        "runaway",
        "runaway",
        lambda case: [
            *(Reply(calls=[call("no_such_tool")]) for _ in range(RUNAWAY)),
            Reply("The report."),
        ],
        answer="The report.",
        final_text="The report.",
        reason="same_error",
        report=True,
        work=("paused", "runaway"),
        given=(TASK, "error-streak", "report", NEXT),
    ),
    Ending(
        "context_limit_overflow",
        "context_limit",
        lambda case: [Reply(status=400, error=CONTEXT_REFUSAL)],
        reason="overflow",
        report=True,
        work=("paused", "context_limit"),
        messages="held",
        known={"report": LIMIT_REPORTS},
    ),
    Ending(
        "gateway_budget",
        "gateway_budget",
        lambda case: [Reply(status=400, error=BUDGET_REFUSAL)],
        report=True,
        work=("paused", "gateway_budget"),
        messages="held",
        known={"report": LIMIT_REPORTS, "explained": UNEXPLAINED},
    ),
    Ending(
        "gateway_budget_local",
        "gateway_budget",
        lambda case: [],  # the journal refuses the call before it is sent
        setup=priced_earlier,
        report=True,
        work=("paused", "gateway_budget"),
        messages="held",
        given=(),  # the limit is still reached: the next run's call is refused too
        known={"report": LIMIT_REPORTS, "explained": UNEXPLAINED},
    ),
    Ending(
        "gateway_budget_after_going_on",
        "gateway_budget",
        lambda case: [Reply("First."), Reply(status=400, error=BUDGET_REFUSAL)],
        setup=planned,
        answer="First.",
        report=True,
        work=("paused", "gateway_budget"),
        owed=frozenset({"plan"}),
        given=("digest", "plans", TASK, "plans", "plans", NEXT),
        known={
            "record": "V-07: a halt after a go-on closes on the mid-plan status as the final text",
            "event": "V-07: a halt after a go-on closes on the mid-plan status as the final text",
            "report": LIMIT_REPORTS,
            "owed": PLAN_UNSAID,
            "explained": UNEXPLAINED,
        },
    ),
    Ending(
        "memory_limit",
        "memory_limit",
        lambda case: [
            Reply(calls=[call("plan_read")], before=lambda: lower_memory_ceiling(case)),
        ],
        report=True,
        work=("paused", "memory_limit"),
        given=(),  # the ceiling stays lowered: the next run cannot call either
        known={"report": LIMIT_REPORTS, "explained": UNEXPLAINED},
    ),
    Ending(
        "error",
        "error",
        lambda case: [Reply(status=400, error="a malformed request")],
        failure="model",
        work=("paused", "error"),
        messages="held",
    ),
    Ending(
        "error_transient",
        "error",
        lambda case: [Reply(status=500)],
        failure="model",
        work=("paused", "error"),
        messages="held",
        next="recovery",
        given=(TASK, NEW_ATTEMPT, "redelivered"),
        known={"given": RESENT},
    ),
    Ending(
        "cancelled",
        "cancelled",
        lambda case: [Reply("Never.", before=case.gate)],
        during=cancel_at_gate,
        work=("stopped", "cancelled"),
        messages="held",
        owed=frozenset({"messages"}),
        given=(TASK, "redelivered", NEXT),
        known={"owed": HELD_UNSAID},
        confined_known={"record": CONFINED_CANCEL, "work": CONFINED_CANCEL},
    ),
    Ending(
        "work_stopped",
        "cancelled",
        lambda case: [Reply(calls=[call("plan_read")], before=lambda: stop_work(case))],
        work=("stopped", "user_stop"),
    ),
    Ending(
        "recovery_blocked",
        "recovery_blocked",
        lambda case: [Reply(calls=[call("plan_read")], before=lambda: block_work(case))],
        work=("blocked", "operator_block"),
        known={
            "work": "new: the attempt's finish turns the operator's block into a pause, "
            "its reason lost (continuity/records.py:405-409)",
            "explained": UNEXPLAINED,
        },
    ),
    Ending(
        "interrupted",
        "interrupted",
        lambda case: [Reply("Never.", before=case.gate)],
        during=shut_down_at_gate,
        saved=False,
        work=("blocked", "interrupted"),
        messages="held",
        owed=frozenset({"messages"}),
        known={"owed": HELD_UNSAID, "explained": UNEXPLAINED},
    ),
    Ending(
        "interrupted_bridge_loss",
        "interrupted",
        lambda case: [Reply("Never.", before=case.gate)],
        during=bridge_lost_at_gate,
        saved=False,
        work=("blocked", "interrupted"),
        messages="held",
        owed=frozenset({"messages"}),
        next="recovery",
        given=(TASK, NEW_ATTEMPT, "redelivered"),
        known={"owed": HELD_UNSAID, "given": RESENT},
    ),
    Ending(
        "interrupted_by_its_runtime",
        "interrupted",
        lambda case: [Reply("Done.")],
        runtime=interrupted_reply,
        saved=False,
        work=("blocked", "interrupted"),
        ways=("confined",),
        known={"event": "V-18: a runtime that answers interrupted leaves no stopped event"},
    ),
)

UNWRITTEN: Final[Mapping[str, str]] = {
    "answered_after_hold": "needs the reviewer and the hold mode of test_hold_round through "
    "the run service; not built here",
    "plan_unread": "the Plans retry stops the run only with no room in a folding run's view "
    "(loop.py:624-626); no fixture brings a service run to its view's limit at a closing",
    "context_limit_non_folding": "a main run with durable records always folds "
    "(harness.py:1152); only a run without stores ends this way",
    "context_limit_breaker_irreducible_start": "the fold engine's breaker and geometry stops "
    "need a gateway whose limits make folds free too little; test_history_folds drives them "
    "below the service",
    "context_limit_research_state": "needs current ledger decisions too large for the view "
    "(test_run_service's research-state test patches the folder)",
    "resource_pause": "a root's no-progress pause needs the resource policy's soft caps and "
    "progress samples (test_check_ins); not driven through the service here",
    "resource_report": "a parent's checkpoint report applies to delegated runs only",
    "resource_storage, resource_retention": "need worker storage figures past the policy's "
    "caps; the in-process worker reports none",
    "model_refused": "the scripted gateway answers no Messages-API refusal (stop_reason "
    "refusal); test_model_refusals' fake is not wired to the run service",
    "pins_changed": "needs an automatic Resume whose pinned inputs changed "
    "(test_run_inputs); not driven through the service here",
    "unfinished_children, child_outcome_unavailable": "need a delegation tree through the "
    "run service whose child stops at a limit or loses its outcome; not built here",
    "user_message": "never produced (V-03): the final review's message queue is not wired",
    "budget, run_budget, resource_time, cost_limit": "never produced (V-09); see "
    "test_the_listed_endings_are_the_ones_a_run_can_reach",
    "wall_time": "a delegated run's ending (harness.py:1834)",
}
"""The stop reasons no row drives, and why: what the fixtures lack."""


# --- The arrangements ------------------------------------------------------------------------


@asynccontextmanager
async def arrangement(
    harness: Harness,
    tmp_path: Path,
    way: Way,
    *,
    max_turns: int = MAX_TURNS,
    runtime_forge: Forge | None = None,
) -> AsyncIterator[RunService]:
    """The run service on the harness, its loop in its own process (``direct``) or in a
    confined runtime whose hooks and tools reach the host over the in-memory channel, as
    ``test_confinement_loop.confined`` wires them; ``runtime_forge`` answers the host in
    the runtime's place."""
    async with serving(harness, serve=False, max_turns=max_turns) as service:
        if way == "direct":
            yield service
            return
        models = ModelGateway(harness.gateway.url, API_KEY)

        async def host_side(kind: Capability, value: JsonValue) -> JsonValue:
            return await host.capability(kind, value)

        async def runtime_side(kind: Capability, value: JsonValue) -> JsonValue:
            if kind is Capability.RUN:
                return await runtime.run(value)
            if kind is Capability.CANCEL:
                return await runtime.cancel(value)
            if kind is Capability.MODEL:
                return await vm_models.complete(value)
            if kind is Capability.MODEL_INFO:
                return await vm_models.info(value)
            raise TransportError("denied", "fixture refused capability")

        async def runtime_handle(kind: Capability, value: JsonValue) -> JsonValue:
            if runtime_forge is None:
                return await runtime_side(kind, value)
            return await runtime_forge(kind, value, runtime_side)

        host_channel = Channel(lambda text: vm_channel.receive(text), host_side)
        vm_channel = Channel(lambda text: host_channel.receive(text), runtime_handle)
        vm_models = RuntimeModels(models, frozenset({"main"}), vm_channel)
        host = HostLoops(host_channel, frozenset({PROJECT}))
        runtime = RuntimeLoops(vm_channel, models, tmp_path / "scratch")
        runtime.scratch.mkdir()
        service.harness.main = Role(CallbackModel(host_channel, "main", role="main"), "main")
        service.harness.gateway = CallbackGateway(host_channel)

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            yield service
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await models.close()


# --- What is left of a run -------------------------------------------------------------------


@dataclass
class Left:
    """What is observable once a run has ended."""

    run: str
    record: RunRecord | None
    events: list[RunEvent]
    work: Work | None
    messages: dict[str, str]
    reported: bool
    next: Next
    given: tuple[str, ...] = ()
    """What the session's next run is given first: each user-role message of its first
    model call, the user's own as their text, a notice as its framing's ID."""

    @property
    def stopped(self) -> dict[str, JsonValue] | None:
        found = [e.data for e in self.events if e.kind == "stopped"]
        assert len(found) <= 1, found
        return found[0] if found else None


def all_events(harness: Harness, run: str) -> list[RunEvent]:
    found: list[RunEvent] = []
    while page := runs(harness).events(PROJECT, SESSION, run, after=found[-1].seq if found else 0):
        found.extend(page)
    return found


def saved(harness: Harness, run: str) -> RunRecord | None:
    store = ProjectStore(harness.state)
    try:
        return store.run(PROJECT, SESSION, run)
    except ProjectError:
        return None
    finally:
        store.close()


async def what_starts_next(service: RunService) -> Next:
    """What the service would start next without the user: a queued Resume (automatic
    recovery), the research supervisor's message (automatic continuation), or a chat
    message that waits."""
    # As the service's own looks do: an ended run's bookkeeping is let go and queued
    # input reconciled, which holds what unfinished work was offered; automatic recovery
    # and continuation are then asked.
    await service.maintenance_tick()
    chat = await service.waiting_work()
    await service.auto_recovery_tick()
    await service.auto_continuation_tick()
    if WorkRecords(service.stores.projects.control).queued():
        return "recovery"
    queued = service.runs.messages(PROJECT, SESSION, state="queued")
    if any(m.author == SUPERVISOR for m in queued):
        return "continuation"
    return "chat" if chat else "nothing"


FRAMED: Final = {
    **{framing.text: framing.id for framing in FRAMINGS},
    "This is a new attempt continuing the same durable research work.": NEW_ATTEMPT,
}


def shown(message: Json) -> str:
    """A user-role message as ``given`` names it."""
    content = str(message.get("content"))
    text = body(content)
    if USER_HEADER.match(content):
        return text
    return next((name for prefix, name in FRAMED.items() if text.startswith(prefix)), text[:40])


async def run_next(case: Case, starts: Next, text: str = NEXT) -> tuple[str, ...]:
    """The session's next run, as the user or the service starts it: the queued Resume
    for automatic recovery, the supervisor's message for continuation, else the user's
    next message; what its first model call is given."""
    harness, service = case.harness, case.service
    await service.maintenance_tick()  # the ended run's bookkeeping is let go
    harness.gateway.requests.clear()
    harness.gateway.script = [Reply("Next answer.")]
    service.waiting_until.clear()  # the time a failed or budget-stopped session waits passes
    branches: list[asyncio.Task[bool]] = []
    request: str | None = None
    if starts == "recovery":
        ((_, _, request, _),) = WorkRecords(service.stores.projects.control).queued()
        assert await service.start_selected(PROJECT, SESSION, request=request)
    else:
        if starts != "continuation":
            service.runs.post(PROJECT, SESSION, "m-next", text, author="user")
        assert await service.start_selected(PROJECT, SESSION)
    task = service.active[PROJECT, SESSION].task
    assert task is not None

    async def ended() -> None:
        # As the service's looks drive a Resume: each ready branch of its tree runs.
        while not task.done() or any(not branch.done() for branch in branches):
            if request is not None:
                for work in await service.resume_next(PROJECT, SESSION, request):
                    branches.append(
                        asyncio.create_task(service.resume_branch(PROJECT, SESSION, request, work))
                    )
            await asyncio.sleep(0.02)

    await asyncio.wait_for(ended(), 60)
    if not harness.gateway.main:
        return ()
    return tuple(shown(m) for m in harness.gateway.main[0] if m.get("role") == "user")


def reported(harness: Harness) -> bool:
    """Whether any main model call was the report round's (RS-6f)."""
    return any(
        message.get("role") == "user" and REPORT.text in str(message.get("content"))
        for request in harness.gateway.main
        for message in request
    )


async def run_one(case: Case, script: Sequence[Reply], during: Step | None = None) -> str:
    """One run of the session for the user's ``TASK``, ``during`` beside it; its name."""
    harness, service = case.harness, case.service
    harness.gateway.script = list(script)
    harness.gateway.requests.clear()
    service.runs.post(PROJECT, SESSION, "m-1", TASK, author="user")
    assert await service.start_selected(PROJECT, SESSION)
    active = service.active[PROJECT, SESSION]
    assert active.task is not None
    beside = asyncio.create_task(during(case)) if during is not None else None
    with contextlib.suppress(asyncio.CancelledError):  # the service's shutdown ended it
        await asyncio.wait_for(asyncio.shield(active.task), 60)
    if beside is not None:
        await asyncio.wait_for(beside, 20)
    assert active.run is not None
    assert not harness.gateway.script, "the run ended before its script did"
    return active.run


async def left_of(case: Case, run: str, *, then: bool = True) -> Left:
    """What is left of ``run``; with ``then``, the session's next run is run too."""
    harness, service = case.harness, case.service
    starts = await what_starts_next(service)
    works = WorkRecords(service.stores.projects.control)
    left = Left(
        run=run,
        record=saved(harness, run),
        events=all_events(harness, run),
        work=works.for_run(PROJECT, SESSION, run),
        messages={m.message_id: m.state for m in service.runs.messages(PROJECT, SESSION)},
        reported=reported(harness),
        next=starts,
    )
    if then:
        left.given = await run_next(case, starts)
    return left


# --- The checks ------------------------------------------------------------------------------


def owed_in(stopped: Mapping[str, JsonValue]) -> set[str]:
    found = stopped.get("owed")
    if isinstance(found, dict):
        return {key for key, value in found.items() if value}
    if isinstance(found, list):
        return {str(item) for item in found}
    return set()


def checks(row: Ending, left: Left) -> dict[str, str | None]:
    """Each check's failure, or ``None`` where it holds."""
    found: dict[str, str | None] = {}
    record = left.record

    def expect(name: str, seen: object, wanted: object) -> None:
        found[name] = None if seen == wanted else f"{name}: {seen!r} where {wanted!r}"

    if row.saved:
        expect(
            "record",
            None if record is None else (record.stopped, record.answer, record.final_text),
            (row.stopped, row.answer, row.final_text),
        )
        if record is not None:
            expect("failure", record.failure, row.failure)
    else:
        expect("record", record, None)
    stopped = left.stopped
    if stopped is None:
        found["event"] = "event: no stopped event"
    else:
        expect(
            "event",
            (stopped.get("stopped"), stopped.get("reason"), stopped.get("final_text")),
            (row.stopped, row.reason, row.final_text),
        )
        expect("owed", owed_in(stopped), set(row.owed))
        if row.next == "nothing" and row.stopped not in ("answered", "cancelled"):
            said = stopped.get("reason") or stopped.get("detail") or stopped.get("failure")
            found["explained"] = None if said else "explained: the stopped event says nothing"
    expect("report", left.reported, row.report)
    expect("work", None if left.work is None else (left.work.state, left.work.reason), row.work)
    expect("messages", left.messages.get("m-1"), row.messages)
    expect("next", left.next, row.next)
    expect("given", left.given, row.given)
    return found


def cases() -> list[Any]:
    return [
        pytest.param(row, way, id=f"{row.name}-{way}", marks=expected_to_fail(row.known_in(way)))
        for row in ENDINGS
        for way in row.ways
    ]


@pytest.mark.parametrize(("row", "way"), cases())
async def test_a_main_run_ends(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: Ending, way: Way
) -> None:
    """Each row of ``ENDINGS``, driven to its end, leaves what the specification says."""
    async with arrangement(
        harness, tmp_path, way, max_turns=row.max_turns, runtime_forge=row.runtime
    ) as service:
        case = Case(harness, service, monkeypatch)
        if row.setup is not None:
            await row.setup(case)
        run = await run_one(case, row.script(case), row.during)
        left = await left_of(case, run)
    verdict(checks(row, left), row.known_in(way))


def test_the_table_names_each_ending_once() -> None:
    """A way to end is either driven by a row or named in ``UNWRITTEN``, never both."""
    names = [row.name for row in ENDINGS]
    assert len(set(names)) == len(names)
    assert not set(names) & set(UNWRITTEN)


SPEC: Final = Path(__file__).parents[3] / "docs" / "specs" / "run-service.md"
LISTED: Final = re.compile(r"\| `stopped` \| `stopped` \(([^)]*)\)")


@pytest.mark.xfail(
    strict=True,
    raises=KnownViolationError,
    reason="V-09: the spec's list of endings names dead reasons and lacks reachable ones",
)
def test_the_listed_endings_are_the_ones_a_run_can_reach() -> None:
    """The ``stopped`` event's reasons, as RS-14 lists them, are the reasons a main run
    can end with: those the table drives and those it names as not driven yet, less the
    ones never produced."""
    found = LISTED.search(SPEC.read_text())
    assert found is not None
    listed = set(re.findall(r"`([a-z_]+)`", found[1]))
    reachable = {row.stopped for row in ENDINGS} | {
        "context_limit",
        "plan_unread",
        "resource_pause",
        "resource_storage",
        "resource_retention",
        "model_refused",
        "pins_changed",
        "unfinished_children",
        "child_outcome_unavailable",
    }
    if listed != reachable:
        raise KnownViolationError(f"listed only: {sorted(listed - reachable)}; reached only: "
                    f"{sorted(reachable - listed)}")  # fmt: skip


# --- The cross cases the audit's violations name ----------------------------------------------


# Direct only: a confined run waiting in ask_user never ends when the service shuts down
# (its runtime's lease exit waits on the host's ask_user call, which nothing cancels),
# and the arrangement's teardown then hangs with it; see the lane's report.
@pytest.mark.parametrize("way", ["direct"])
@pytest.mark.xfail(
    strict=True,
    raises=KnownViolationError,
    reason="V-01: an interrupted run's question stays open after a new root replaces it",
)
async def test_a_message_after_an_interrupted_question_settles_it(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: Way
) -> None:
    """A run interrupted while it waits in ``ask_user`` leaves its question open, which
    the user can answer into a Resume. A message the user writes instead starts a new
    root (RS-12: fresh input is eligible independently); the replaced tree's question is
    then settled, not left open for a run that no longer exists (I1, I7)."""
    async with arrangement(harness, tmp_path, way) as service:
        case = Case(harness, service, monkeypatch)
        questions = Questions(service.stores.documents)

        async def asked_then_shut_down(case: Case) -> None:
            for _ in range(1000):
                if questions.open(PROJECT, SESSION):
                    break
                await asyncio.sleep(0.02)
            await case.service.close()

        script = [Reply(calls=[call("ask_user", question="Which variant?")])]
        await run_one(case, script, asked_then_shut_down)
        assert [q.state for q in questions.open(PROJECT, SESSION)] == ["open"]
        assert NEXT in await run_next(case, "nothing")
        still = [q.question_id for q in questions.open(PROJECT, SESSION)]
    verdict({"question": f"still open: {still}" if still else None}, {"question": "V-01"})


@pytest.mark.parametrize(
    "held",
    [False, pytest.param(True, marks=expected_to_fail({"next": "V-02"}))],
    ids=["nothing-held", "a-message-held"],
)
async def test_a_held_message_and_the_automatic_continuation(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    """An answered root whose session plan has workable steps it has not continued from
    is continued by the research supervisor (auto-continuation). A message an earlier,
    interrupted run left held belongs to that run's tree, which a later root replaced; it
    does not block the continuation of every later root (V-02)."""
    async with arrangement(harness, tmp_path, "direct") as service:
        case = Case(harness, service, monkeypatch)
        if held:
            await run_one(case, [Reply("Never.", before=case.gate)], shut_down_at_gate)
            assert {m.state for m in service.runs.messages(PROJECT, SESSION)} == {"held"}
        else:
            await run_one(case, [Reply("Done.")])
        assert await run_next(case, "nothing") != ()
        activate_plan(harness)  # after the answer: a plan state no run went on from
        starts = await what_starts_next(service)
    found = None if starts == "continuation" else f"next: {starts!r}"
    verdict({"next": found}, {"next": "V-02"} if held else {})


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.xfail(
    strict=True,
    raises=KnownViolationError,
    reason="V-12: a message after a turn-limit pause leaves its check-in pending",
)
async def test_a_message_after_a_turn_limit_pause_settles_its_check_in(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: Way
) -> None:
    """A root paused at its turn limit asks the user to continue or stop (D102). A chat
    message instead starts a new root; the paused tree's check-in is then settled, not
    left asking about work the session replaced (I1, I5)."""
    async with arrangement(harness, tmp_path, way, max_turns=1) as service:
        case = Case(harness, service, monkeypatch)
        await run_one(case, [Reply(calls=[call("plan_read")])])
        control = service.stores.projects.control
        (asked,) = ResourceRequests(control).page(PROJECT, SESSION).requests
        assert asked.decision is None
        assert not asked.superseded
        await run_next(case, "nothing")
        (after,) = [
            r for r in ResourceRequests(control).page(PROJECT, SESSION).requests
            if r.request_id == asked.request_id
        ]  # fmt: skip
    pending = after.decision is None and not after.superseded
    verdict({"check_in": "still pending" if pending else None}, {"check_in": "V-12"})


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.xfail(
    strict=True,
    raises=KnownViolationError,
    reason="V-14: an answer the output limit cut a third time ends silently truncated",
)
async def test_an_answer_cut_past_its_continuations_says_so(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: Way
) -> None:
    """A run continues an answer the output limit cut at most twice (RS-6f); past that,
    the text so far is the answer. The user is then told that it is incomplete: the
    ``stopped`` event says the answer was cut (I8)."""
    script = [Reply(part, finish="length") for part in ("Part one, ", "part two, ", "part")]
    async with arrangement(harness, tmp_path, way) as service:
        case = Case(harness, service, monkeypatch)
        run = await run_one(case, script)
        left = await left_of(case, run, then=False)
    assert left.record is not None
    assert left.record.final_text == "Part one, part two, part"
    length = cast(dict[str, Any], left.record.summaries["length"])
    assert (length["stops"], length["continued"]) == (3, 2)
    stopped = left.stopped or {}
    said = "output_limit" in json.dumps(stopped)
    verdict({"cut": None if said else f"cut: {stopped}"}, {"cut": "V-14"})


@pytest.mark.slow
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.xfail(
    strict=True,
    raises=KnownViolationError,
    reason="V-05: a run answers with its job running, says nothing of it, and nothing "
    "starts when the job ends",
)
async def test_a_run_that_answers_with_a_job_running(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: Way
) -> None:
    """A run may answer while its background job runs (jobs outlive their run, D83). Its
    ``stopped`` event names the job it leaves running (I2); and when the job ends, the
    session is woken to read its result (the run-service spec's wake on a detached job's
    end, approved and not built)."""
    script = [Reply(calls=[start_job()]), Reply("A job is running.")]
    async with arrangement(harness, tmp_path, way) as service:
        case = Case(harness, service, monkeypatch)
        run = await run_one(case, script)
        left = await left_of(case, run, then=False)
        await harness.end_job()
        starts = await what_starts_next(service)
    assert left.record is not None
    assert (left.record.stopped, left.record.final_text) == ("answered", "A job is running.")
    stopped = left.stopped or {}
    verdict(
        {
            "owed": None if "jobs" in owed_in(stopped) else f"owed: {owed_in(stopped)}",
            "wake": None if starts != "nothing" else "wake: nothing starts once the job ended",
        },
        {"owed": "I2, V-05", "wake": "V-05"},
    )
