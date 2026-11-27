# SPDX-License-Identifier: MIT
"""What can be pending when a main run's model gives a reply with no tool call, and the
order in which the platform acts on it: a table-driven walk of the closing, through the
whole harness (``cli.main`` against the in-process worker and the scripted gateway, with
the scripted reviewer), so that the production wiring is what runs.

Each row of ``CASES`` names the conditions pending at the reply A1 and what the
specifications say then follows: which participant acts first; what each later model
call is given; which answer is the run's last; and which answer the run's one final
review reviewed (consistency review CR-48: the final answer). Conditions are made the
way the user, an earlier run, a child or a job makes them (``CONDITIONS``). Every row
runs with reviews on, so the final review's place is checked in every cell.

The table is data so that the closing's order can later be defined once in the source
and generated from it. A row whose behaviour today differs from the specification is
``xfail(strict=True)`` with the audit's identifier (``V…`` for the plans and reviews map,
``V-…`` for the run map, ``new:`` for a fault this walk found that neither lists); one
whose correct behaviour is an open owner decision says ``decision:``.

Beside the matrix: closings that are a sequence of their own (``SEQUENCES``: a message
or a plan edit during the final review, a plan finished before it, a finding raised
when the run cannot respond, what the end says is owed, a child's track progress during
its parent's reply); what wakes each kind of wait (``WAKES``); and two agreement walks,
every reader of "a step that can still be worked" over generated plan states
(``READERS``) and every reader of "the user's word" over each kind of input (``WORDS``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import notices as framings
from sandbox_science.orchestrator import question_tool
from sandbox_science.orchestrator.consistency.actions import plan_item
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.snapshot_reads import request_entries
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.context.history import Archive, body
from sandbox_science.orchestrator.continuity.auto_continuation import AutoContinuation
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.digest import Digest, Section
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.provenance import users_words
from sandbox_science.orchestrator.plan_tools import PLAN_OPEN, PlanTools
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import (
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanRef,
    PlanScope,
    SessionPlans,
    StepStatus,
)
from sandbox_science.testing import eventually

from .loop_harness import (
    PLAN,
    PROJECT,
    SESSION,
    TASK,
    USER_HEADER,
    USER_PLANS,
    Harness,
    Message,
    Reply,
    Reviewer,
    call,
    start_job,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import awaiting_children, submitted, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE
from .test_delegation_tracks import active_plan as track_plan
from .test_delegation_tracks import on_track
from .test_run_service import post

# Its rows start real job processes through the local backend, and the whole walk takes
# minutes on four CPUs.
pytestmark = pytest.mark.slow

Json = dict[str, Any]
Step = Callable[[], Coroutine[Any, Any, None]]
Given = tuple[tuple[str, ...], ...]

AGENT: Final = PlanAuthority(
    scope=PlanScope(project_id=PROJECT, session_id=SESSION), role="main", run_id="prior"
)
"""An earlier main run of the session, as the author of the plan a run starts with."""
ANSWER: Final = "A{}: the task is done."
"""The run's n-th reply with no tool call; a completion claim, so its closing is terminal."""
RELEASE_SECONDS: Final = 3.0
"""How long a child that waits for its parent's next turn waits when none comes."""

# The kinds of what a model call is given, as ``kind`` names them.
U, J, D, DR, AR = "user_message", "jobs", "delegations", "delegations-running", "agent-report"
PH, PW, M = "plans:held", "plans:went_on", "math"
RF: Final = ("review-findings", "review-counts")
FIRST: Final = {
    U: "messages",
    J: "jobs",
    D: "delegations",
    DR: "delegations",
    AR: "mailbox",
    PH: "plans",
    PW: "went_on",
    M: "math",
    RF[0]: "reviews",
}
"""The participant whose notice a kind is: the one that acted at the closing."""


# --- The table -------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """The conditions pending at the reply A1, and what follows as the specifications say.

    ``first`` is the participant that acts first at A1's closing (``none``: the closing
    accepts A1); ``given`` is what each model call after A1's is given, by kind; ``final``
    is the answer the run ends with (A1 is 1), ``reviewed`` the one its final review
    reviewed. ``order`` and ``review`` are the known violations of those two aspects."""

    pending: tuple[str, ...]
    first: str
    given: Given
    final: int | None
    reviewed: int | None
    stopped: str = "answered"
    order: str | None = None
    review: str | None = None

    @property
    def id(self) -> str:
        return "+".join(self.pending)


V1: Final = "V1: a queued user message is taken after the final review, spent on A1"
V4: Final = "V4: the went-on bound counts the run's earlier response round (loop.py:534)"
LOST_MATH: Final = (
    "new: a delegated run's closing takes the main run's math finding (capabilities.py:160 "
    "gives MathChecks to children; mathchecks/controller.py:209-216 reads no role; CR-36)"
)
LAST_RETURNED: Final = (
    "new: on the last turn a returned, unread child ends the run unfinished_children and "
    "drops its answer (continuity/hook.py:116-122; PD-46, RS-6f)"
)
LAST_RUNNING: Final = (
    "new: on the last turn a running child's retry has no room and the run stops "
    "turn_limit, dropping its answer (delegation/hook.py:1474-1481, loop.py:617-619; RS-6f)"
)

CASES: Final[tuple[Case, ...]] = (
    # Each condition alone.
    Case(("message",), "messages", ((U,),), 2, 2, review=V1),
    Case(("agent_message",), "delegations", ((DR, AR), (D,)), 3, 3),
    Case(("running_child",), "delegations", ((D,),), 2, 2),
    Case(("returned_child",), "delegations", ((D,),), 2, 2),
    Case(("running_job",), "none", (), 1, 1),
    Case(("ended_job",), "jobs", ((J,),), 2, 2),
    Case(("plan_changed",), "plans", ((PH,),), 2, 2),
    Case(("plan_workable",), "went_on", ((PW,),), 2, 2),
    Case(("plan_draft",), "none", (), 1, 1),
    Case(("open_finding",), "none", (), 1, 1),
    Case(("final_findings",), "reviews", (RF,), 2, 1),
    Case(("math",), "math", ((M,),), 2, 1),
    Case(("last_turn",), "none", (), 1, 1),
    Case(("hold",), "none", (), 1, 1),
    Case(("rounds_spent",), "reviews", (RF, (PH,), (PW,)), 4, 1, order=V4),
    # A queued user message with each other condition.
    Case(("message", "agent_message"), "delegations", ((DR, AR, U), (D,)), 3, 3),
    Case(("message", "running_child"), "delegations", ((DR, U), (D,)), 3, 3),
    Case(("message", "returned_child"), "delegations", ((D, U),), 2, 2),
    Case(("message", "running_job"), "messages", ((U,),), 2, 2, review=V1),
    Case(("message", "ended_job"), "jobs", ((J, U),), 2, 2),
    Case(("message", "plan_changed"), "plans", ((PH, U),), 2, 2),
    Case(("message", "plan_workable"), "went_on", ((PW, U),), 2, 2),
    Case(("message", "plan_draft"), "messages", ((U,),), 2, 2, review=V1),
    Case(("message", "open_finding"), "messages", ((U,),), 2, 2, review=V1),
    Case(("message", "final_findings"), "messages", ((U,), RF), 3, 2, order=V1, review=V1),
    Case(("message", "math"), "messages", ((U,), (M,)), 3, 2, order=V1, review=V1),
    Case(("message", "last_turn"), "none", (), 1, 1),
    Case(("message", "hold"), "messages", ((U,),), 2, 2, review=V1),
    # A child's message (the child still running) with each other condition.
    Case(("agent_message", "running_child"), "delegations", ((DR, AR), (D,)), 3, 3),
    Case(("agent_message", "returned_child"), "delegations", ((D, DR, AR), (D,)), 3, 3),
    Case(("agent_message", "running_job"), "delegations", ((DR, AR), (D,)), 3, 3),
    Case(("agent_message", "ended_job"), "jobs", ((J, AR), (D,)), 3, 3),
    Case(("agent_message", "plan_changed"), "plans", ((PH, AR), (D,)), 3, 3),
    Case(("agent_message", "plan_workable"), "went_on", ((PW, AR), (D,)), 3, 3),
    Case(("agent_message", "plan_draft"), "delegations", ((DR, AR), (D,)), 3, 3),
    Case(("agent_message", "open_finding"), "delegations", ((DR, AR), (D,)), 3, 3),
    Case(("agent_message", "final_findings"), "delegations", ((DR, AR), (D,), RF), 4, 3),
    Case(("agent_message", "math"), "delegations", ((DR, AR), (D,), (M,)), 4, 3, order=LOST_MATH),
    Case(("agent_message", "last_turn"), "none", (), 1, 1, order=LAST_RUNNING),
    Case(("agent_message", "hold"), "delegations", ((DR, AR), (D,)), 3, 3),
    # A running child with each other condition.
    Case(("running_child", "returned_child"), "delegations", ((D,),), 2, 2),
    Case(("running_child", "running_job"), "delegations", ((D,),), 2, 2),
    Case(("running_child", "ended_job"), "jobs", ((J,), (D,)), 3, 3),
    Case(("running_child", "plan_changed"), "plans", ((PH,), (D,)), 3, 3),
    Case(("running_child", "plan_workable"), "went_on", ((PW,), (D,)), 3, 3),
    Case(("running_child", "plan_draft"), "delegations", ((D,),), 2, 2),
    Case(("running_child", "open_finding"), "delegations", ((D,),), 2, 2),
    Case(("running_child", "final_findings"), "delegations", ((D,), RF), 3, 2),
    Case(("running_child", "math"), "delegations", ((D,), (M,)), 3, 2, order=LOST_MATH),
    Case(("running_child", "last_turn"), "none", (), 1, 1, order=LAST_RUNNING),
    Case(("running_child", "hold"), "delegations", ((D,),), 2, 2),
    # A returned child not yet read with each other condition.
    Case(("returned_child", "running_job"), "delegations", ((D,),), 2, 2),
    Case(("returned_child", "ended_job"), "jobs", ((J, D),), 2, 2),
    Case(("returned_child", "plan_changed"), "plans", ((PH, D),), 2, 2),
    Case(("returned_child", "plan_workable"), "went_on", ((PW, D),), 2, 2),
    Case(("returned_child", "plan_draft"), "delegations", ((D,),), 2, 2),
    Case(("returned_child", "open_finding"), "delegations", ((D,),), 2, 2),
    Case(("returned_child", "final_findings"), "delegations", ((D,), RF), 3, 2),
    Case(("returned_child", "math"), "delegations", ((D,), (M,)), 3, 2, order=LOST_MATH),
    Case(("returned_child", "last_turn"), "none", (), 1, 1, order=LAST_RETURNED),
    Case(("returned_child", "hold"), "delegations", ((D,),), 2, 2),
    # A running job with each other condition.
    Case(("running_job", "ended_job"), "jobs", ((J,),), 2, 2),
    Case(("running_job", "plan_changed"), "plans", ((PH,),), 2, 2),
    Case(("running_job", "plan_workable"), "went_on", ((PW,),), 2, 2),
    Case(("running_job", "plan_draft"), "none", (), 1, 1),
    Case(("running_job", "open_finding"), "none", (), 1, 1),
    Case(("running_job", "final_findings"), "reviews", (RF,), 2, 1),
    Case(("running_job", "math"), "math", ((M,),), 2, 1),
    Case(("running_job", "last_turn"), "none", (), 1, 1),
    Case(("running_job", "hold"), "none", (), 1, 1),
    # An ended job not yet announced with each other condition.
    Case(("ended_job", "plan_changed"), "plans", ((PH,), (J,)), 3, 3),
    Case(("ended_job", "plan_workable"), "went_on", ((PW,), (J,)), 3, 3),
    Case(("ended_job", "plan_draft"), "jobs", ((J,),), 2, 2),
    Case(("ended_job", "open_finding"), "jobs", ((J,),), 2, 2),
    Case(("ended_job", "final_findings"), "jobs", ((J,), RF), 3, 2),
    Case(("ended_job", "math"), "jobs", ((J,), (M,)), 3, 2),
    Case(("ended_job", "last_turn"), "none", (), 1, 1),
    Case(("ended_job", "hold"), "jobs", ((J,),), 2, 2),
    # The plan changed by the user with each other condition.
    Case(("plan_changed", "plan_workable"), "plans", ((PH,), (PW,)), 3, 3),
    Case(("plan_changed", "plan_draft"), "plans", ((PH,),), 2, 2),
    Case(("plan_changed", "open_finding"), "plans", ((PH,),), 2, 2),
    Case(("plan_changed", "final_findings"), "plans", ((PH,), RF), 3, 2),
    Case(("plan_changed", "math"), "plans", ((PH,), (M,)), 3, 2),
    Case(("plan_changed", "last_turn"), "none", (), 1, 1),
    Case(("plan_changed", "hold"), "plans", ((PH,),), 2, 2),
    # An active plan with a workable step with each other condition.
    Case(("plan_workable", "open_finding"), "went_on", ((PW,),), 2, 2),
    Case(("plan_workable", "final_findings"), "went_on", ((PW,), RF), 3, 2),
    Case(("plan_workable", "math"), "went_on", ((PW,), (M,)), 3, 2),
    Case(("plan_workable", "last_turn"), "none", (), 1, 1),
    Case(("plan_workable", "hold"), "went_on", ((PW,),), 2, 2),
    # The run's own draft plan with each other condition.
    Case(("plan_draft", "open_finding"), "none", (), 1, 1),
    Case(("plan_draft", "final_findings"), "reviews", (RF,), 2, 1),
    Case(("plan_draft", "math"), "math", ((M,),), 2, 1),
    Case(("plan_draft", "last_turn"), "none", (), 1, 1),
    Case(("plan_draft", "hold"), "none", (), 1, 1),
    # The findings, the math check, the last turn and hold.
    Case(("open_finding", "final_findings"), "reviews", (RF,), 2, 1),
    Case(("open_finding", "math"), "math", ((M,),), 2, 1),
    Case(("open_finding", "last_turn"), "none", (), 1, 1),
    Case(("open_finding", "hold"), "none", (), 1, 1),
    Case(("final_findings", "math"), "reviews", ((*RF, M),), 2, 1),
    Case(("final_findings", "last_turn"), "none", (), 1, 1),
    Case(("final_findings", "hold"), "reviews", (RF,), 2, 1),
    Case(("math", "last_turn"), "none", (), 1, 1),
    Case(("math", "hold"), "math", ((M,),), 2, 1),
    Case(("last_turn", "hold"), "none", (), 1, 1),
)
"""Every condition alone and every pair that can co-occur. Not here: ``plan_workable``
with ``plan_draft`` (one plan is either), and ``rounds_spent`` with the others: it is
itself a sequence of closings (a response round, a held answer, then a reply), and its
own plan and finding are the conditions it concerns."""

UNWRITTEN: Final = {
    "check_in": "a child's check-in waiting at the parent's closing needs a second soft "
    "cap reached by a child's reported tokens; the scripted gateway can report them, but "
    "the decision wait runs in the child for up to 30 minutes, which no fixture shortens "
    "(continuity/resources.py takes no clock or timeout from the harness)",
    "figure_finding": "figure checks have no closing participant: FigureChecks.drain is "
    "the run service's, and its results reach no run as a notice (figurechecks.py:1-14)",
}
"""Conditions the brief names that no row drives, and why."""


# --- Reading what the model was given ---------------------------------------------------

_FRAMINGS: Final = sorted(
    (
        (value.text, value.id)
        for value in vars(framings).values()
        if isinstance(value, framings.Framing) and value.text
    ),
    key=lambda pair: -len(pair[0]),
)


def kind(message: Message) -> str:
    """What a user-role message given to the model is: a user's chat message, a plans
    notice (held, went on, or shown), or the notice framing it starts with."""
    content = str(message.get("content"))
    if USER_HEADER.match(content):
        return U
    text = body(content)
    if text.startswith(framings.PLANS.text):
        if PLAN_OPEN[:60] in text:
            return PW
        return PH if '"answer_held"' in text else "plans"
    # The notices that carry a run past an answer and that hold one have framings of
    # their own; each is still that notice only with what it says to do.
    for framing, says, name in (
        (framings.PLANS_WENT_ON, PLAN_OPEN[:60], PW),
        (framings.PLANS_HELD, '"answer_held"', PH),
    ):
        if text.startswith(framing.text):
            return name if says in text else f"{framing.id}:without-{name}"
    for prefix, name in _FRAMINGS:
        if text.startswith(prefix):
            return name
    return "other:" + text[:48]


@dataclass
class Recorder(Reviewer):
    """The scripted reviewer, which also keeps each window's kind and what the final
    review's manifest held, and can act as a user would while the final review runs."""

    windows_kinds: list[str] = field(default_factory=list[str])
    final_manifest: list[Json] = field(default_factory=list[Json])
    on_final: Step | None = None
    """Runs once, while the final review reads its manifest."""

    def __call__(self, messages: list[Message]) -> Reply:
        last = messages[-1]
        reply = super().__call__(messages)
        if last["role"] == "user":
            opening = cast(Json, json.loads(str(last["content"])))
            self.windows_kinds.append("final" if "final" in opening else "other")
        elif (
            self.windows_kinds[-1:] == ["final"]
            and messages[-2]["tool_calls"][0]["function"]["name"] == "review_manifest"
        ):
            self.final_manifest.append(cast(Json, json.loads(str(last["content"]))))
            if self.on_final is not None:
                reply.before, self.on_final = self.on_final, None
        return reply


# --- The conditions: each adds itself to a world -------------------------------------


@dataclass
class World:
    """One run's pending conditions as they are made: what is set up before the run,
    the replies before A1, what arrives while the model writes A1 and A2, and the
    delegated runs' scripts."""

    harness: Harness
    before: list[Step] = field(default_factory=list[Step])
    prefix: list[Reply] = field(default_factory=list[Reply])
    last: list[Reply] = field(default_factory=list[Reply])
    """Replies after the prefix, just before A1."""
    arrive: list[Step] = field(default_factory=list[Step])
    second: list[Step] = field(default_factory=list[Step])
    """Runs while the model writes A2."""
    background: list[Step] = field(default_factory=list[Step])
    """Started with the run, beside it."""
    routed: dict[str, Sequence[Reply]] = field(default_factory=dict[str, Sequence[Reply]])
    config: ReviewConfig = field(default_factory=ReviewConfig)
    reviewer: Recorder = field(default_factory=lambda: Recorder(flags=0))
    max_turns: int | None = None
    last_turn: bool = False
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    """Set as the model writes A1."""


def plans(harness: Harness) -> tuple[SessionPlans, Callable[[], None]]:
    """The session's plans, and how to close their store."""
    documents = harness.documents()
    return SessionPlans(documents), documents.close


def agent_plan(harness: Harness, *, active: bool, body: Json = PLAN) -> PlanRef:
    """The plan an earlier main run left, a draft or made active; its reference."""
    store, close = plans(harness)
    try:
        made = store.publish(AGENT, "agent-plan", PlanBody.model_validate(body), "Plan the work.")
        if active:
            activate(store, made.ref)
        return made.ref
    finally:
        close()


def activate(store: SessionPlans, ref: PlanRef) -> None:
    current = store.current(AGENT)
    assert current is not None
    store.set_status(
        AGENT, "agent-activate", ref, current.head.status_revision,
        current.head.progress_cursor, "active", "Work the plan.",
    )  # fmt: skip


def user_revision(harness: Harness, goal: str = "Characterize the loop, as the user asks") -> None:
    """The user's edit of the current plan's body, which keeps its status."""
    store, close = plans(harness)
    try:
        current = store.current(USER_PLANS)
        assert current is not None
        edited = PlanBody.model_validate({**current.version.body.model_dump(), "goal": goal})
        store.revise(USER_PLANS, f"user-revise-{len(goal)}", current.head.ref, edited, "USER")
    finally:
        close()


def handbacks(harness: Harness) -> list[str]:
    """The session's returned delegations no run has received yet."""
    store = ProjectStore(harness.state)
    try:
        found = DelegationRecords(store.control).handbacks(PROJECT, SESSION)
        return [d.delegation_id for d in found]
    finally:
        store.close()


def message(w: World) -> None:
    """The user's chat message, sent while the model writes A1."""

    async def arrive() -> None:
        post(w.harness, "m-1", "Use 32 instead of 16.")

    w.arrive.append(arrive)


def returned_child(w: World) -> None:
    """A task that returned before A1, whose outcome the run has not received."""
    task = "Tabulate the returned cases."
    go = asyncio.Event()

    async def wait_go() -> None:
        # Not before A1: an outcome that arrives during an earlier call rides on its result.
        await go.wait()

    w.prefix.append(Reply(calls=[call("delegate", task=task)]))
    w.routed[task] = submitted(DONE, "Done.", before=wait_go)

    async def arrive() -> None:
        go.set()
        await eventually(lambda: bool(handbacks(w.harness)), describe="the child's return")

    w.arrive.append(arrive)


async def waits_for_children(w: World) -> None:
    """Once the parent waits for its children: at its closing, or, with no turn after A1,
    as its end waits (``Delegations.finishing``). A run that does not wait for them at its
    end, as one whose answer stands on its last turn, is let go a while after A1."""
    if not w.last_turn:
        await awaiting_children(w.harness)
        return
    await w.answered.wait()
    waiting = asyncio.create_task(awaiting_children(w.harness))
    try:
        await asyncio.wait((waiting,), timeout=RELEASE_SECONDS)
    finally:
        waiting.cancel()


def running_child(w: World) -> None:
    """A task still running at A1's closing, which returns once its parent waits (or,
    with no turn after A1, a while later)."""
    task = "Tabulate the running cases."
    free = asyncio.Event()

    async def blocked() -> None:
        await free.wait()

    async def release() -> None:
        await waits_for_children(w)
        free.set()

    w.prefix.append(Reply(calls=[call("delegate", task=task)]))
    w.routed[task] = submitted(DONE, "Done.", before=blocked)
    w.background.append(release)


def agent_message(w: World) -> None:
    """A running child's message to its parent, sent while the parent writes A1; the
    child goes on until the parent writes A2, or a while if no A2 comes."""
    task = "Tabulate the reported cases."
    go, sent, free = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait_go() -> None:
        await go.wait()

    async def report_then_block() -> None:
        sent.set()
        await free.wait()

    async def arrive() -> None:
        go.set()
        await asyncio.wait_for(sent.wait(), 30)

    async def later() -> None:
        free.set()

    async def no_second_turn() -> None:
        await waits_for_children(w)
        free.set()

    w.prefix.append(Reply(calls=[call("delegate", task=task)]))
    w.routed[task] = [
        Reply(calls=[call("message_parent", text="The first case fails.")], before=wait_go),
        *submitted(DONE, "Done.", before=report_then_block),
    ]
    w.arrive.append(arrive)
    if w.last_turn:
        w.background.append(no_second_turn)
    else:
        w.second.append(later)


def running_job(w: World) -> None:
    """A job that is still running when the run ends."""
    w.prefix.append(Reply(calls=[start_job("never")]))


def ended_job(w: World) -> None:
    """A job that ends while the model writes A1."""
    w.prefix.append(Reply(calls=[start_job("go")]))

    async def arrive() -> None:
        await w.harness.end_job("go")

    w.arrive.append(arrive)


def plan_changed(w: World) -> None:
    """The user changes the plan while the model writes A1: a new plan of the user's if
    the session has none, else an edit of the current one."""

    async def arrive() -> None:
        store, close = plans(w.harness)
        try:
            exists = store.current(USER_PLANS) is not None
        finally:
            close()
        if exists:
            user_revision(w.harness)
        else:
            w.harness.user_plan()

    w.arrive.append(arrive)


def plan_workable(w: World) -> None:
    """An active plan with a pending step, which the run reads in its first notice."""

    async def made() -> None:
        agent_plan(w.harness, active=True)

    w.before.append(made)


def plan_draft(w: World) -> None:
    """The run's own draft, made after whatever else it started (a draft refuses the
    effects that would start a job or a child)."""
    made = call("plan_create", operation_id="own-plan", body=PLAN, reason="Plan the work.")
    w.last.append(Reply(calls=[made]))


def open_finding(w: World) -> None:
    """A fail finding an earlier run's review left open."""

    async def made() -> None:
        w.harness.review_finding()

    w.before.append(made)


def final_findings(w: World) -> None:
    """The final review raises a fail finding on the run's work."""
    w.reviewer.flags = 1


def math(w: World) -> None:
    """A math finding on a workbook an earlier run wrote (CR-36)."""

    async def made() -> None:
        await w.harness.math_finding()

    w.before.append(made)


def last_turn(w: World) -> None:
    """A1 is the run's last turn."""
    w.last_turn = True


def hold(w: World) -> None:
    """The project's final review holds the answer until it ends (CR-48)."""
    w.config = w.config.model_copy(update={"final_review": "hold"})


def rounds_spent(w: World) -> None:
    """A response round already spent: the final review flags A1; while the model writes
    A2, its round's answer, an earlier run activates the plan the run read as a draft, so
    A2 is held to show it, and A3 is a reply while the plan has a workable step."""
    final_findings(w)

    async def made() -> None:
        agent_plan(w.harness, active=False)

    async def activated() -> None:
        store, close = plans(w.harness)
        try:
            current = store.current(AGENT)
            assert current is not None
            activate(store, current.head.ref)
        finally:
            close()

    w.before.append(made)
    w.second.append(activated)


CONDITIONS: Final[dict[str, Callable[[World], None]]] = {
    "message": message,
    "agent_message": agent_message,
    "running_child": running_child,
    "returned_child": returned_child,
    "running_job": running_job,
    "ended_job": ended_job,
    "plan_changed": plan_changed,
    "plan_workable": plan_workable,
    "plan_draft": plan_draft,
    "open_finding": open_finding,
    "final_findings": final_findings,
    "math": math,
    "last_turn": last_turn,
    "hold": hold,
    "rounds_spent": rounds_spent,
}


# --- Driving a world and reading what happened ---------------------------------------


@dataclass
class Seen:
    record: RunRecord
    given: Given
    """For each model call after A1's, the kinds of user-role messages added for it."""
    windows: tuple[str, ...]
    """Each review window's kind, in order: ``final`` or ``other``."""
    reviewed: int | None
    """The answer that is the final review's newest transcript item of the run's own."""


def number(text: object) -> int | None:
    """Which answer ``text`` is (1 for A1), or 0 for another text; ``None`` for none."""
    if text is None:
        return None
    return next((n for n in range(1, 10) if text == ANSWER.format(n)), 0)


def world(harness: Harness, pending: tuple[str, ...]) -> World:
    w = World(harness)
    # The last turn first: the children's conditions read it.
    for name in sorted(pending, key=lambda name: name != "last_turn"):
        CONDITIONS[name](w)
    w.prefix += w.last
    if w.last_turn:
        w.max_turns = len(w.prefix) + 1
    return w


def each(steps: list[Step]) -> Step:
    async def run() -> None:
        for step in steps:
            await step()

    return run


async def drive(w: World, answers: int, *, run_id: str = "run-1") -> Seen:
    """Run the world's run with the replies A1 to A``answers`` after its prefix, and
    read what each call was given and what the final review reviewed."""
    harness = w.harness
    for step in w.before:
        await step()
    harness.configure_reviews(w.config)
    harness.gateway.reviewer = w.reviewer

    async def answered() -> None:
        w.answered.set()

    script = [
        *w.prefix,
        Reply(ANSWER.format(1), before=each([answered, *w.arrive])),
        *(
            Reply(ANSWER.format(n), before=each(w.second) if n == 2 else None)
            for n in range(2, answers + 1)
        ),
    ]
    tasks = [asyncio.create_task(step()) for step in w.background]
    try:
        record = await harness.run(
            run_id, script, reviewer=True, max_turns=w.max_turns, routed=w.routed
        )
    finally:
        for task in tasks:
            task.cancel()
    calls = harness.gateway.main
    given = tuple(
        tuple(
            kind(m)
            for m in now[len(before) + 1 :]
            if m["role"] == "user" and body(str(m.get("content"))) != TASK
        )
        for before, now in zip(calls[len(w.prefix) :], calls[len(w.prefix) + 1 :], strict=False)
    )
    return Seen(record, given, tuple(w.reviewer.windows_kinds), reviewed(w.reviewer, record))


def reviewed(reviewer: Recorder, record: RunRecord) -> int | None:
    """Which answer the final review's newest transcript item of the run's own is."""
    if not reviewer.final_manifest:
        return None
    positions = [
        int(item["locator"]["message"])
        for item in cast(list[Json], reviewer.final_manifest[0]["evidence"])
        if item["kind"] == "transcript"
        and item["locator"].get("run") == record.run_id
        and "message" in item["locator"]
        and record.messages[int(item["locator"]["message"])]["role"] == "assistant"
    ]
    return number(record.messages[max(positions)].get("content")) if positions else None


def check_order(case: Case, seen: Seen) -> None:
    """Which participant acted first, what each later call was given, and how the run
    ended."""
    first = FIRST[seen.given[0][0]] if seen.given and seen.given[0] else "none"
    assert (first, seen.given) == (case.first, case.given)
    assert seen.record.stopped == case.stopped
    assert number(seen.record.final_text) == case.final


def check_review(case: Case, seen: Seen) -> None:
    """The run's one final review (CR-48) reviewed the answer it ends with, or the one
    whose response round's answer that is."""
    assert seen.windows.count("final") == 1
    assert seen.reviewed == case.reviewed


CHECKS: Final = {"order": check_order, "review": check_review}


def params() -> list[Any]:
    """One test for a row's aspects that hold, and one xfail for each reason known
    wrong today, so that a known fault in one aspect hides no regression in another."""
    found: list[Any] = []
    for case in CASES:
        # The script follows the specified order, so with the order wrong today the
        # final review's place cannot be read apart from it.
        known = {"order": case.order, "review": case.review or case.order}
        sound = tuple(a for a, why in known.items() if why is None)
        if sound:
            found.append(pytest.param(case, sound, id=f"{case.id}-{'+'.join(sound)}"))
        for why in dict.fromkeys(w for w in known.values() if w is not None):
            aspects = tuple(a for a, w in known.items() if w == why)
            found.append(
                pytest.param(
                    case,
                    aspects,
                    id=f"{case.id}-{'+'.join(aspects)}",
                    marks=pytest.mark.xfail(strict=True, reason=why),
                )
            )
    return found


@pytest.mark.parametrize(("case", "aspects"), params())
async def test_a_closing_acts_in_the_specified_order(
    delegating: Harness, case: Case, aspects: tuple[str, ...]
) -> None:
    seen = await drive(world(delegating, case.pending), 1 + len(case.given))
    for aspect in aspects:
        CHECKS[aspect](case, seen)


# --- Rows that are a sequence of their own ---------------------------------------------


async def bounded(condition: Callable[[], bool], seconds: float) -> bool:
    """Whether ``condition`` holds within ``seconds``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not condition():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def ending(harness: Harness, record: RunRecord) -> str:
    """What the run's end says: its last event's data and its record's summaries."""
    store = ProjectStore(harness.state)
    try:
        events = RunState(store.control).events(
            PROJECT, SESSION, run_name(record.started_at, record.run_id)
        )
    finally:
        store.close()
    return json.dumps([events[-1].data, record.summaries], sort_keys=True)


def tool_results(record: RunRecord) -> list[Json]:
    """The run's tool results, each as JSON (``{}`` for one that is not an object)."""
    found: list[Json] = []
    for m in record.messages:
        if m["role"] == "tool":
            try:
                value = json.loads(body(str(m["content"])))
            except ValueError:
                value = {}
            found.append(cast(Json, value) if isinstance(value, dict) else {})
    return found


def job_ids(record: RunRecord) -> list[str]:
    return [str(r["job_id"]) for r in tool_results(record) if "job_id" in r]


async def user_writes_during_the_final_review(harness: Harness) -> None:
    """CR-16, CR-F13: a message the user sends while the final review runs starts the new
    turn at once; the review goes on beside it."""
    w = world(harness, ())
    started: list[bool] = []

    async def write() -> None:
        before = len(harness.gateway.main)
        post(harness, "m-1", "Use 32 instead of 16.")
        started.append(await bounded(lambda: len(harness.gateway.main) > before, 4.0))

    w.reviewer.on_final = write
    seen = await drive(w, 2)
    assert seen.given == ((U,),)
    assert started == [True]


async def user_edits_the_plan_during_the_final_review(harness: Harness) -> None:
    """``accept`` ends the closing (hooks.py: no response round follows) and the run's one
    final review is of its final answer (CR-48): the second veto pass, after every hook
    accepted and the findings were bound, takes no turn the review did not see."""
    w = world(harness, ("plan_draft",))

    async def edit() -> None:
        user_revision(harness)

    w.reviewer.on_final = edit
    seen = await drive(w, 2)
    assert seen.windows.count("final") == 1
    assert seen.reviewed == number(seen.record.final_text)


def plan_calls(harness: Harness, action: str) -> Callable[[list[Message]], list[tuple[str, Json]]]:
    """A call that records the active plan's step completed, or finishes it achieved,
    against the plan as the store has it when the model makes it."""

    def calls(_: list[Message]) -> list[tuple[str, Json]]:
        store, close = plans(harness)
        try:
            current = store.current(AGENT)
            assert current is not None
            head = current.head
        finally:
            close()
        base = head.ref.model_dump(mode="json")
        if action == "progress":
            return [
                call(
                    "plan_progress", operation_id="own-progress", base=base, step_id="step-0",
                    expected_step_cursor=0, expected_status_revision=head.status_revision,
                    status="completed", note="Inspected.",
                )
            ]  # fmt: skip
        outcome = {"status": "achieved", "summary": "Done.", "evidence": [], "remaining_work": []}
        return [
            call(
                "plan_finish", operation_id="own-finish", base=base,
                expected_status_revision=head.status_revision,
                expected_progress_cursor=head.progress_cursor, reason="All steps done.",
                status="completed", outcome=outcome,
            )
        ]  # fmt: skip

    return calls


async def plan_finished_before_a_failing_final_review(harness: Harness) -> None:
    """PD-8 (the reviewer checks completion claims) and D7: a plan the run finished
    ``achieved`` does not stand as achieved once its final review raises a fail finding
    on the work that the run leaves open."""
    w = world(harness, ("plan_workable", "final_findings"))
    w.prefix += [
        Reply(calls_from=plan_calls(harness, "progress")),
        Reply(calls_from=plan_calls(harness, "finish")),
    ]
    seen = await drive(w, 2)
    assert seen.given == (RF,)
    store, close = plans(harness)
    try:
        current = store.current(AGENT)
        assert current is not None
        outcome = current.head.outcome
    finally:
        close()
    assert current.head.status == "completed"
    assert "open" in harness.findings()["review"]
    assert outcome is None or outcome.status != "achieved"


async def a_finding_raised_when_the_run_cannot_respond(harness: Harness) -> None:
    """CR-16 as amended: a finding the run's last closing raised and never delivered
    reaches the session's next run as a count; the run's end names it open."""
    seen = await drive(world(harness, ("final_findings", "last_turn")), 1)
    final = cast(Json, cast(Json, seen.record.summaries["reviews"])["final_review"])
    assert [f["status"] for f in cast(list[Json], final["findings"])] == ["open"]
    await harness.run("run-2", [Reply("Noted.")], reviewer=True)
    (first,) = harness.gateway.main
    assert "review-counts" in [kind(m) for m in first if m["role"] == "user"]


async def a_job_still_running_at_the_end(harness: Harness) -> None:
    """The run's end states the work it leaves owed: a job still running."""
    seen = await drive(world(harness, ("running_job",)), 1)
    (job,) = job_ids(seen.record)
    assert job in ending(harness, seen.record)


async def a_message_left_queued_on_the_last_turn(harness: Harness) -> None:
    """The run's end states the work it leaves owed: a user message it did not take."""
    seen = await drive(world(harness, ("message", "last_turn")), 1)
    assert "m-1" in ending(harness, seen.record)


async def a_job_ended_unannounced_on_the_last_turn(harness: Harness) -> None:
    """The run's end states the work it leaves owed: a job whose end it did not read."""
    seen = await drive(world(harness, ("ended_job", "last_turn")), 1)
    (job,) = job_ids(seen.record)
    assert job in ending(harness, seen.record)


async def a_child_records_progress_during_its_parents_reply(harness: Harness) -> None:
    """PD-9, PD-12: a child's progress on its own track is the plan's, not another
    author's change to it; the parent's next effect in the same reply is not refused."""
    track_plan(harness)
    thinking, free = asyncio.Event(), asyncio.Event()

    def recorded() -> bool:
        store, close = plans(harness)
        try:
            current = store.compact_current(USER_PLANS)
        finally:
            close()
        assert current is not None
        latest = next(s.latest for s in current.steps if s.step_id == "table")
        return latest is not None and latest.author.role == "delegated"

    async def after_the_turns_notices() -> None:
        # The child records only once the parent's call is made, its notices given.
        await asyncio.wait_for(thinking.wait(), 30)

    async def after_progress() -> None:
        # The parent's effect call comes only once the child's record is stored.
        thinking.set()
        await eventually(recorded, describe="the child's track progress")

    async def blocked() -> None:
        await free.wait()

    async def release() -> None:
        free.set()

    waiting = wait()
    waiting.before = release
    record = await harness.run(
        "run-1",
        [
            Reply(calls=[call("plan_read")]),
            Reply(calls=[on_track()]),
            Reply(calls=[call("shell", script="echo one")], before=after_progress),
            waiting,
            Reply(calls=[call("plan_read")]),
            Reply(ANSWER.format(1)),
        ],
        routed={
            "Tabulate a^16 mod 17.": [
                Reply(
                    calls=[
                        call(
                            "track_progress", step_id="table", status="in_progress", note="Started."
                        )
                    ],
                    before=after_the_turns_notices,
                ),
                *submitted(DONE, "Done.", before=blocked),
            ]
        },
    )
    shell = [m for m in record.messages if m["role"] == "tool"][2]
    assert "plan_reread_required" not in str(shell["content"])


@dataclass(frozen=True)
class Row:
    """A row of its own: a scenario and, if it is wrong today, why."""

    scenario: Callable[[Harness], Awaitable[None]]
    known: str | None = None

    @property
    def id(self) -> str:
        return self.scenario.__name__


OWED: Final = "decision: the end states what it leaves owed (synthesis B); today it names none"
SEQUENCES: Final[tuple[Row, ...]] = (
    Row(user_writes_during_the_final_review, "V2: the harness never passes the review a queue"),
    Row(
        user_edits_the_plan_during_the_final_review,
        "V14: the second veto pass takes a turn after accept; the final review saw A1",
    ),
    Row(
        plan_finished_before_a_failing_final_review,
        "V9: plan_finish reads no finding and the outcome is immutable (session_plans.py:1239)",
    ),
    Row(a_finding_raised_when_the_run_cannot_respond),
    Row(
        a_child_records_progress_during_its_parents_reply,
        "V3: a child's track progress sets its parent's reread fence (plan_tools.py:358-360)",
    ),
    Row(a_job_still_running_at_the_end, "V-05: a run answers while its job runs; " + OWED),
    Row(a_message_left_queued_on_the_last_turn, OWED),
    Row(a_job_ended_unannounced_on_the_last_turn, OWED),
)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            row,
            id=row.id,
            marks=[pytest.mark.xfail(strict=True, reason=row.known)] if row.known else [],
        )
        for row in SEQUENCES
    ],
)
async def test_a_closing_sequence(delegating: Harness, row: Row) -> None:
    await row.scenario(delegating)


# --- What wakes a wait ------------------------------------------------------------------

WAKE_SECONDS: Final = 4.0
"""The bound within which a waiting run should take what arrived during its wait."""
ASK: Final = call("ask_user", question="Which curve?", options=[{"label": "A"}, {"label": "B"}])


def finished_plan(harness: Harness) -> None:
    """An active plan whose one step an earlier run recorded completed: nothing in it
    is workable and it fences nothing, so the user may edit it while the run waits."""
    ref = agent_plan(harness, active=True)
    store, close = plans(harness)
    try:
        current = store.current(AGENT)
        assert current is not None
        store.progress(
            AGENT, "agent-progress", ref, "step-0", 0, "completed", "Inspected.",
            expected_status_revision=current.head.status_revision,
        )  # fmt: skip
    finally:
        close()


def finding_for(harness: Harness, run_id: str) -> None:
    """A fail finding a review of run ``run_id``'s work raises, as a checkpoint would."""
    documents = harness.documents()
    try:
        reviews = ReviewStore(documents)
        reviews.observe(
            PROJECT, SESSION, f"{run_id}-claim", "transcript", {"run": f"{run_id}-fixture"},
            {"content": "There are 16."},
        )  # fmt: skip
        review = reviews.dispatch(
            PROJECT, SESSION, "checkpoint", author_run=run_id, owner="fixture", alias="scripted"
        )
        assert review is not None
        evidence = reviews.manifest(PROJECT, SESSION, review)["evidence"][0]["evidence"]
        Findings(reviews).create(
            PROJECT, SESSION, review, "fixture",
            {
                "severity": "fail", "category": "contradicting_value",
                "statement": "The count contradicts the pinned evidence.",
                "anchors": [{"evidence": evidence}], "operation_id": f"{run_id}-finding",
            },
        )  # fmt: skip
        reviews.finish(PROJECT, SESSION, review, "fixture", "findings", "A checkpoint.")
    finally:
        documents.close()


def waiting_on(harness: Harness, key: str) -> bool:
    """Whether the run holding the session recorded that it waits on ``key``'s items."""
    store = ProjectStore(harness.state)
    try:
        state = RunState(store.control)
        held = state.lease(PROJECT, SESSION)
        return held is not None and any(
            event.kind == "waiting" and event.data.get(key)
            for event in state.events(PROJECT, SESSION, held.run)
        )
    finally:
        store.close()


def open_questions(harness: Harness) -> bool:
    store = ProjectStore(harness.state)
    try:
        return bool(Questions(store.control).open(PROJECT, SESSION))
    finally:
        store.close()


@dataclass(frozen=True)
class Wake:
    """A wait, what arrives during it, and why it is known wrong today, if it is.

    The run waits in a call (``wait_jobs``, ``wait_delegations``, ``ask_user``), in its
    closing's wait for its children, or in its final review; the input arrives once it
    waits, and the wait should end, or the input be taken, within ``WAKE_SECONDS``.
    After the bound the test ends the wait itself (the job ends, the child returns, the
    user answers), so a wait nothing woke still ends."""

    wait: str
    arrives: str
    known: str | None

    @property
    def id(self) -> str:
        return f"{self.wait}+{self.arrives}"


V8: Final = "V8: a plan edit or a finding wakes no wait (capabilities.py:212-225)"
WAKES: Final[tuple[Wake, ...]] = (
    Wake("wait_jobs", "plan_edit", V8),
    Wake("wait_jobs", "finding", V8),
    Wake("wait_delegations", "plan_edit", V8),
    Wake("wait_delegations", "finding", V8),
    Wake(
        "ask_user",
        "plan_edit",
        "decision: PD-23 has the call wait only for its answer; the audit's one input "
        "source has it woken (synthesis C)",
    ),
    Wake("ask_user", "finding", "decision: as for a plan edit (PD-23; synthesis C)"),
    Wake("closing_children", "plan_edit", V8),
    Wake("closing_children", "finding", V8),
    Wake(
        "final_review",
        "plan_edit",
        "V8: a plan edit during the final review reaches no turn of the run when its plan "
        "has no open step (the second veto pass takes it only then, V14)",
    ),
)
"""Each wait with each input. Not here: a finding during the final review, which cannot
arise: a session runs one review at a time (``ReviewStore.due``), and the final review's
own findings are its response round."""


async def woken(harness: Harness, monkeypatch: pytest.MonkeyPatch, wake: Wake) -> None:
    monkeypatch.setattr(question_tool, "POLL_SECONDS", 0.05)
    harness.configure_reviews(ReviewConfig())
    harness.gateway.reviewer = Recorder(flags=0)
    finished_plan(harness)
    free, taken = asyncio.Event(), list[bool]()

    async def blocked() -> None:
        await free.wait()

    async def arrive() -> None:
        if wake.arrives == "plan_edit":
            user_revision(harness)
        else:
            finding_for(harness, "run-1")

    task = "Tabulate the cases."
    script: list[Reply]
    match wake.wait:
        case "wait_jobs":

            def waiting(messages: list[Message]) -> list[tuple[str, Json]]:
                ids = [str(r["job_id"]) for r in map(_json, messages) if "job_id" in r]
                return [call("wait_jobs", job_ids=ids[:1])]

            script = [Reply(calls=[start_job("go")]), Reply(calls_from=waiting)]

            async def fallback() -> None:
                await harness.end_job("go")

        case "wait_delegations" | "closing_children":
            script = [Reply(calls=[call("delegate", task=task)])]
            if wake.wait == "wait_delegations":
                script.append(wait())

            async def fallback() -> None:
                free.set()

        case "ask_user":
            script = [Reply(calls=[ASK])]

            async def fallback() -> None:
                questions = Questions(ProjectStore(harness.state).control)
                (question,) = questions.open(PROJECT, SESSION)
                RunState(ProjectStore(harness.state).control).say(
                    PROJECT, SESSION, "m-answer", "A.", author="user",
                    answers=question.question_id,
                )  # fmt: skip

        case _:
            script = []

            async def fallback() -> None:
                pass

    waits_at = len(script)  # the calls made once the run waits
    if wake.wait == "closing_children":
        waits_at += 1

    async def watch() -> None:
        try:
            # The input arrives once the run's wait is recorded as begun, not after a while.
            if wake.wait in ("closing_children", "wait_delegations"):
                await awaiting_children(harness)
            elif wake.wait == "wait_jobs":
                await eventually(lambda: waiting_on(harness, "job_ids"), describe="wait_jobs")
            else:
                await eventually(lambda: open_questions(harness), describe="the question")
            await arrive()
            taken.append(await bounded(lambda: len(harness.gateway.main) > waits_at, WAKE_SECONDS))
        finally:
            await fallback()

    if wake.wait == "final_review":
        reviewer = cast(Recorder, harness.gateway.reviewer)

        async def during() -> None:
            await arrive()

        reviewer.on_final = during
        await harness.run("run-1", [Reply(ANSWER.format(1))], reviewer=True)
        # Today's sequence: the run ends with A1, and no call is given the edit.
        assert len(harness.gateway.main) > 1
        assert PLANS_SHOWN in [kind(m) for m in harness.gateway.main[-1] if m["role"] == "user"]
        return
    delegating = wake.wait in ("wait_delegations", "closing_children")
    # Today's sequence after the wait: the closing's own wait takes a turn for the child.
    answers = [
        Reply(ANSWER.format(n)) for n in (1, 2)[: 2 if wake.wait == "closing_children" else 1]
    ]
    watcher = asyncio.create_task(watch())
    try:
        await harness.run(
            "run-1",
            [*script, *answers],
            reviewer=True,
            routed={task: submitted(DONE, "Done.", before=blocked)} if delegating else None,
        )
    finally:
        free.set()
        await watcher
    assert taken == [True]
    given = [kind(m) for m in harness.gateway.main[waits_at] if m["role"] == "user"]
    assert (PLANS_SHOWN if wake.arrives == "plan_edit" else RF[0]) in given


PLANS_SHOWN: Final = "plans"


def _json(message: Message) -> Json:
    try:
        found = json.loads(body(str(message.get("content"))))
    except ValueError:
        return {}
    return cast(Json, found) if isinstance(found, dict) else {}


@pytest.mark.parametrize(
    "wake",
    [
        pytest.param(
            wake,
            id=wake.id,
            marks=[pytest.mark.xfail(strict=True, reason=wake.known)] if wake.known else [],
        )
        for wake in WAKES
    ],
)
async def test_what_arrives_during_a_wait_is_taken_within_a_bound(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch, wake: Wake
) -> None:
    await woken(delegating, monkeypatch, wake)


# --- Agreement: every reader of "a step that can still be worked" -----------------------

STATES: Final = ("pending", "in_progress", "completed", "blocked", "skipped", "edited")
"""A step's state: no record, a recorded status, or completed and then its text edited."""
TWO_STEPS: Final[Json] = {
    **PLAN,
    "tracks": ({"track_id": "t1", "title": "Tables"},),
    "phases": (
        {
            "phase_id": "analysis",
            "title": "Analysis",
            "steps": (
                {"step_id": "s1", "title": "Tabulate", "description": "Tabulate",
                 "evidence_standard": "A table", "track_id": "t1"},
                {"step_id": "s2", "title": "Write", "description": "Write it up",
                 "evidence_standard": "A document"},
            ),
        },
    ),
}  # fmt: skip
OPEN_STATES: Final = frozenset({"pending", "in_progress", "edited"})
"""PD (the went-on rule, as amended 2026-11-27): a step can still be worked when it is
pending or in progress, or changed since its last record."""


@dataclass(frozen=True)
class PlanState:
    s1: str
    s2: str
    held: bool
    """Whether a running delegation holds track t1, so step s1 is a child's."""

    @property
    def open(self) -> frozenset[str]:
        return frozenset(
            s for s, state in (("s1", self.s1), ("s2", self.s2)) if state in OPEN_STATES
        )

    @property
    def workable(self) -> frozenset[str]:
        """The run's own: not on a track a delegated run holds."""
        return self.open - ({"s1"} if self.held else set())


ALL_STATES: Final = tuple(
    PlanState(a, b, held) for a in STATES for b in STATES for held in (False, True)
)


def made_state(harness: Harness, number_: int, state: PlanState) -> tuple[str, PlanTools]:
    """A session of its own whose active plan is in ``state``, and a main run's plan
    tools over it."""
    session = f"agree-{number_}"
    projects = ProjectStore(harness.state)
    projects.open_session(PROJECT, session, dedicated=False)
    authority = AGENT.model_copy(
        update={"scope": PlanScope(project_id=PROJECT, session_id=session)}
    )
    store, close = plans(harness)
    try:
        made = store.publish(authority, "p", PlanBody.model_validate(TWO_STEPS), "Plan.")
        current = store.current(authority)
        assert current is not None
        store.set_status(authority, "a", made.ref, current.head.status_revision,
                         current.head.progress_cursor, "active", "Work.")  # fmt: skip
        for step, value in (("s1", state.s1), ("s2", state.s2)):
            if value == "pending":
                continue
            current = store.current(authority)
            assert current is not None
            store.progress(
                authority, f"progress-{step}", current.head.ref, step, 0,
                cast(StepStatus, "completed" if value == "edited" else value), "Recorded.",
                expected_status_revision=current.head.status_revision,
            )  # fmt: skip
        for step, value in (("s1", state.s1), ("s2", state.s2)):
            if value != "edited":
                continue
            current = store.current(authority)
            assert current is not None
            body_ = current.version.body.model_dump()
            for phase in body_["phases"]:
                for found in phase["steps"]:
                    if found["step_id"] == step:
                        found["description"] += ", again"
            store.revise(authority, f"edit-{step}", current.head.ref,
                         PlanBody.model_validate(body_), "Edited.")  # fmt: skip
    finally:
        close()
    if state.held:
        DelegationRecords(projects.control).start(
            PROJECT, session,
            Delegation.model_validate({
                "delegation_id": f"dlg-{number_:024x}", "parent": "20261127T120000000000Z-root",
                "run": f"20261127T120001000000Z-child{number_}", "kind": "task",
                "brief": {"task": "Tabulate.", "track": {"track_id": "t1"}},
                "started_at": "2026-11-27T12:00:01+00:00",
            }),
        )  # fmt: skip
    context = ExecutionContext(
        project_id=PROJECT,
        session_id=session,
        environment_id=projects.get(PROJECT).sessions[session].environment_id,
    )
    tools = PlanTools(
        SessionPlans(harness.documents()), projects, context, run_id="reader", alias="s"
    )
    return session, tools


def _open_in_lines(lines: list[str]) -> frozenset[str]:
    """The steps a list of ``<step> [track …] <state>: <title>`` lines shows open."""
    found: set[str] = set()
    for line in lines:
        step, _, rest = line.partition(" ")
        state = rest.split("] ", 1)[-1].split(":", 1)[0]
        if state.startswith("pending") or state == "in_progress":
            found.add(step)
    return frozenset(found)


async def read_by(reader: str, harness: Harness, session: str, tools: PlanTools) -> object:
    """What ``reader`` takes as the steps that can still be worked, in the form the
    specification's sets are compared with (``expected``)."""
    notice = await tools.safe_point(force=True)
    assert notice is not None
    assert notice.pin is not None
    tools.pending_read = notice.pin
    await tools.model_delivered()
    authority = tools.authority
    store = tools.plans
    match reader:
        case "finish_check":
            current = store.current(authority)
            assert current is not None
            outcome = GoalOutcome(
                status="partially_achieved", summary="Part.", evidence=(),
                remaining_work=("More.",),
            )  # fmt: skip
            try:
                store.set_status(
                    authority, "finish", current.head.ref, current.head.status_revision,
                    current.head.progress_cursor, "completed", "Done.", outcome=outcome,
                )  # fmt: skip
            except DocumentError as error:
                return frozenset(cast(list[str], error.details.get("steps") or ()))
            return frozenset()
        case "closing_veto":
            found = await tools.open_steps()
            assert found is not None
            return frozenset(found[1])
        case "went_on":
            offer = await tools.going_on()
            return frozenset(cast(list[str], offer.payload["next_steps"])) if offer else frozenset()
        case "plans_notice":
            return _open_in_lines(cast(list[str], notice.payload["steps"]))
        case "auto_continuation":
            return AutoContinuation._workable(store.compact_current(authority))
        case "digest":
            section = Section("plan", "Plan", 0.1)
            Digest(Ledger(store.documents), plans=store)._plan(section, PROJECT, session)
            lines = [line.text for line in section.lines[1:]]
            shown = frozenset(
                text.split(" ", 1)[0]
                for text in lines
                if text.split(" ")[1 + ("(current)" in text)] in ("pending", "in_progress")
            )
            current = next((t.split(" ", 1)[0] for t in lines if "(current)" in t), None)
            return shown, current
        case "reviewer_plan_item":
            with store.documents.control.read() as tx:
                found_item = plan_item(store, tx.sql, PROJECT, session, "reader")
            assert found_item is not None
            return frozenset(
                str(step["step"])
                for step in cast(list[Json], found_item[1]["steps"])
                if step["status"] in ("pending", "in_progress") or step.get("step_changed_since")
            )
    raise AssertionError(reader)


def expected(reader: str, state: PlanState) -> object:
    """What the specification has each reader take (the went-on rule as amended
    2026-11-27, which the automatic continuation shares; PD-5, PD-7 for the rest)."""
    match reader:
        case "went_on":
            return state.workable
        case "auto_continuation":
            return bool(state.workable)
        case "digest":
            first = next((s for s in ("s1", "s2") if s in state.open), None)
            return state.open, first
        case _:
            return state.open


READERS: Final = {
    "finish_check": None,
    "closing_veto": None,
    "went_on": None,
    "plans_notice": None,
    "auto_continuation": "V15: the automatic continuation counts a step a child holds "
    "(auto_continuation.py:182-193)",
    "digest": "V5: the digest shows an edited step's old status and marks a blocked or "
    "skipped step current (digest.py:501-508)",
    "reviewer_plan_item": None,
}
"""Every reader of "a step that can still be worked" (the plans map, D5), and why one
disagrees today."""


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(
            reader,
            marks=[pytest.mark.xfail(strict=True, reason=why)] if why else [],
        )
        for reader, why in READERS.items()
    ],
)
async def test_every_reader_of_a_workable_step_agrees(harness: Harness, reader: str) -> None:
    wrong: list[tuple[PlanState, object, object]] = []
    for n, state in enumerate(ALL_STATES):
        session, tools = made_state(harness, n, state)
        try:
            found = await read_by(reader, harness, session, tools)
        finally:
            tools.plans.documents.close()
            tools.projects.close()
        if found != (want := expected(reader, state)):
            wrong.append((state, found, want))
    assert wrong == []


# --- Agreement: every reader of "the user's word" ---------------------------------------

INPUTS: Final = ("chat_message", "task", "comment", "answer", "plan_edit")
SAID: Final = {
    "chat_message": "Use 32 instead of 16.",
    "task": TASK,
    "comment": "Explain the result.",
    "answer": "Curve A, for the margin.",
    "plan_edit": "Characterize the loop for 32",
}
"""The words of each kind of input the user gives, which each reader is asked about."""


async def user_inputs(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """One run that receives each kind of the user's input; the archive message that
    carried each to it."""
    monkeypatch.setattr(question_tool, "POLL_SECONDS", 0.05)
    harness.configure_reviews(ReviewConfig())
    harness.gateway.reviewer = Recorder(flags=0)
    finished_plan(harness)

    async def answer() -> None:
        found: list[Any] = []

        def asked() -> bool:
            store = ProjectStore(harness.state)
            try:
                found[:] = Questions(store.control).open(PROJECT, SESSION)
            finally:
                store.close()
            return bool(found)

        await eventually(asked)
        harness.work_request(body=SAID["comment"])
        user_revision(harness, SAID["plan_edit"])
        store = ProjectStore(harness.state)
        try:
            RunState(store.control).say(
                PROJECT, SESSION, "m-answer", SAID["answer"], author="user",
                answers=found[0].question_id,
            )  # fmt: skip
        finally:
            store.close()

    async def chat() -> None:
        post(harness, "m-1", SAID["chat_message"])

    answering = asyncio.create_task(answer())
    record = await harness.run(
        "run-1",
        [Reply(calls=[ASK]), Reply(ANSWER.format(1), before=chat), Reply(ANSWER.format(2))],
        reviewer=True,
    )
    await answering
    documents = harness.documents()
    try:
        with documents.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT message_id,role,kind,source,coalesce(content,'') FROM archive_messages "
                "WHERE project=? AND session=? AND run=? ORDER BY position",
                (PROJECT, SESSION, run_name(record.started_at, record.run_id)),
            ).fetchall()
    finally:
        documents.close()
    carried: dict[str, str] = {}
    for message, role, kind_, source, content in rows:
        if kind_ == "task":
            carried["task"] = message
        elif kind_ == "user" and SAID["chat_message"] in content:
            carried["chat_message"] = message
        elif kind_ == "comment":
            carried["comment"] = message
        elif role == "tool" and source == "ask_user":
            carried["answer"] = message
        elif SAID["plan_edit"] in content:
            carried["plan_edit"] = message
    assert set(carried) == set(INPUTS), rows
    return carried


def counted(reader: str, harness: Harness, input_: str, message: str) -> bool:
    """Whether ``reader`` takes the input that ``message`` carried as the user's word."""
    documents = harness.documents()
    try:
        match reader:
            case "ledger_provenance":
                with documents.control.read() as tx:
                    return any(
                        SAID[input_] in said for said in users_words(tx.sql, PROJECT, message)
                    )
            case "review_grounds":
                info = Archive(documents.control).message_info(PROJECT, message)
                return Findings(ReviewStore(documents))._ground(PROJECT, SESSION, info) is not None
            case "reviewer_request":
                with documents.control.read() as tx:
                    entries = request_entries(tx.sql, PROJECT, SESSION)
                return SAID[input_] in json.dumps(entries, ensure_ascii=False)
    finally:
        documents.close()
    raise AssertionError(reader)


V6: Final = "V6: four definitions of the user's word (provenance.py:371; findings.py:1505)"
WORDS: Final[dict[tuple[str, str], str | None]] = {
    # The ledger's rule (LG-12; CR-42 as amended names it: a chat message, a main run's
    # task made of them, the operator's task) and the request text of a comment (the
    # requests notice: "request_text is the user's instruction").
    ("ledger_provenance", "chat_message"): None,
    ("ledger_provenance", "task"): None,
    ("ledger_provenance", "comment"): None,
    ("ledger_provenance", "answer"): "decision: " + V6 + "; an answer is not user_quoted",
    ("ledger_provenance", "plan_edit"): "decision: " + V6 + "; a plan edit is no one's word",
    # CR-42 as amended: the ledger's rule and the ask_user result.
    ("review_grounds", "chat_message"): None,
    ("review_grounds", "task"): None,
    ("review_grounds", "comment"): V6 + "; grounds read written_by, which counts no comment "
    "the ledger's rule counts (findings.py:1515)",
    ("review_grounds", "answer"): None,
    ("review_grounds", "plan_edit"): "decision: " + V6 + "; a plan edit grounds no fix",
    # CR-48 as amended: the user's messages and answers, the first first.
    ("reviewer_request", "chat_message"): None,
    ("reviewer_request", "task"): "decision: " + V6 + "; the request item holds the task "
    "only in a session with no chat message (snapshot_reads.py:507-514)",
    ("reviewer_request", "comment"): "decision: " + V6 + "; a comment is not in the request",
    ("reviewer_request", "answer"): None,
    ("reviewer_request", "plan_edit"): "decision: " + V6 + "; a plan edit is not in it",
}
"""For each reader and kind of input, why it is known not to count it today, if so;
every cell asserts the input counts, the auditor's one definition (synthesis D)."""


@pytest.mark.parametrize(
    ("reader", "input_"),
    [
        pytest.param(
            reader, input_, id=f"{reader}-{input_}",
            marks=[pytest.mark.xfail(strict=True, reason=why)] if why else [],
        )
        for (reader, input_), why in WORDS.items()
    ],
)  # fmt: skip
async def test_every_reader_takes_the_users_word_alike(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, reader: str, input_: str
) -> None:
    carried = await user_inputs(harness, monkeypatch)
    assert counted(reader, harness, input_, carried[input_])
