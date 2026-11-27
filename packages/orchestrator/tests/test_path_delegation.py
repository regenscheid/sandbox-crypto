# SPDX-License-Identifier: MIT
"""Every way a delegation moves, from ``delegate`` to an accepted result (plans-and-
delegation spec, PD-11 to PD-22, PD-38, PD-39, PD-44 to PD-46; child environments, CE-4
and CE-6; D87, D102): a path-walking test over the tables at the top of this module.

Each row of a table is one path, walked end to end through ``cli.main`` against the
in-process worker and the scripted gateway (``test_delegation``'s ``delegating`` fixture,
with ``test_delegation_environments``' child environment routes), and every aspect
observable at its far end is checked against what the specification says. A row whose
behaviour today differs names, by aspect, the audit identifier of the known violation
(``DEL-V<n>`` for the delegation map's list, ``PR-V<n>`` for the plans and reviews map's,
``T2-N<n>`` for one these walks found):
the row is ``xfail(strict=True)`` on ``KnownViolationsError``, raised only when exactly those
aspects fail. An aspect that fails and is not named, or a named one that passes, fails the
row outright, so the day a violation is fixed its row says so. A known violation that
waits on an owner decision says ``decision:`` and asserts the auditor's recommendation.

The tables are data, so that a later change can make them the single definition the
code's lists are generated from.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

import pytest
from pydantic import TypeAdapter

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.consistency.delegated import observe_delegated
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.context.history.limits import WorkingMemoryLimitError
from sandbox_science.orchestrator.context.history.log import RunArchive
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.recovery import Recovery
from sandbox_science.orchestrator.delegation.hook import ChildEnd, observed
from sandbox_science.orchestrator.delegation.outcome import Outcome
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.digest import Digest, constraints_block
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.harness import Harness as RunHarness
from sandbox_science.orchestrator.harness import Role, RunRequest, Settings
from sandbox_science.orchestrator.hooks import GAPS
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.notices import DELEGATIONS, DELEGATIONS_ATTACHED
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually

from .loop_harness import (
    API_KEY,
    AREA,
    PROJECT,
    SESSION,
    TOKEN,
    USER_PLANS,
    Harness,
    Reply,
    call,
    own,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .scripted import ScriptedModel, ToolCall, submitting
from .test_delegation import (
    CHECK,
    awaiting_children,
    children,
    configure,
    decide,
    delegation,
    results,
    started,
)
from .test_delegation import delegating as delegating
from .test_delegation_environments import ChildService
from .test_delegation_environments import service as service
from .test_delegation_environments import worker as worker
from .test_delegation_tracks import READ, TRACKED, active_plan
from .test_run_harness import Sink, stores
from .test_run_service import serving
from .test_tool_journal import RUN
from .test_tree_recovery import RecoveryWorker, drive_concurrent

pytestmark = pytest.mark.slow

Json = dict[str, Any]
Message = dict[str, Any]

# --- The audit's identifiers -----------------------------------------------------------

AUDIT: Final[Mapping[str, str]] = {
    "DEL-V1": "a child that ends at a limit or fails keeps its work paused, so the root "
    "ends unfinished_children and its final answer is dropped",
    "DEL-V2": "review intake is keyed by delegation, so a reopened task's later runs are "
    "never fed to the review",
    "DEL-V3": "a child past the gateway budget, the memory limit or a model refusal ends "
    "failed and its valid submission is discarded",
    "DEL-V4": "a message to a child that ended at a limit, failed or was interrupted stays "
    "queued, and nobody is told it will not be read",
    "DEL-V5": "the notice that counts as receipt carries no deviations, open questions or "
    "what a check did not examine",
    "DEL-V6": "every end of a parent's run accepts its returned tasks, and no model is told",
    "DEL-V7": "a Resume continues a child under a new delegation id its parent never sees",
    "DEL-V9": "a parent at a limit waits for its children with no wake",
    "DEL-V10": "a user constraint recorded after a child started does not reach it",
    "DEL-V11": "a refusal states the wrong cause or names no next step",
    "PR-V7": "a user plan edit reaches no child as content",
    "T2-N1": "accept_delegation of a returned check says it is returned and that only a "
    "returned task is accepted, not that it is a check (found by this test)",
}
DECISIONS: Final = frozenset({"DEL-V1", "DEL-V6"})
"""Known violations whose correct behaviour is an owner decision (the audit's decisions 1
and 2): their rows assert the auditor's recommendation."""


class KnownViolationsError(Exception):
    """Exactly the aspects a row names as known violations failed."""


@dataclass
class Verdicts:
    """Each aspect's checks, against the aspects a row names as known violations."""

    known: Mapping[str, str]
    failed: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    checked: set[str] = field(default_factory=set[str])

    def check(self, aspect: str, ok: bool, detail: object = "") -> None:
        self.checked.add(aspect)
        if not ok:
            self.failed.setdefault(aspect, []).append(str(detail)[:600])

    def settle(self) -> None:
        unexpected = {a: d for a, d in self.failed.items() if a not in self.known}
        fixed = sorted(a for a in self.known if a not in self.failed)
        if unexpected or fixed:
            raise AssertionError(
                f"unexpected failures: {json.dumps(unexpected, indent=1)}; "
                f"known violations that now pass (update the table): {fixed}"
            )
        if self.failed:
            raise KnownViolationsError(json.dumps(self.failed, indent=1))


def marks(known: Mapping[str, str]) -> list[pytest.MarkDecorator]:
    """A strict xfail on ``KnownViolationsError`` naming each known violation's identifier."""
    if not known:
        return []
    ids = sorted(set(known.values()))
    reason = "; ".join(f"{'decision: ' if i in DECISIONS else ''}{i}: {AUDIT[i]}" for i in ids)
    return [pytest.mark.xfail(strict=True, raises=KnownViolationsError, reason=reason)]


# --- Running a root and reading what it left -------------------------------------------

FINAL: Final = "The final answer: a^16 = 1 for every nonzero a in GF(17)."
TASK_TEXT: Final = "Tabulate a^16 mod 17 for a = 1..16 and save the table."
DEVIATION: Final = "I tabulated a = 1..8 only, not a = 1..16."
QUESTION: Final = "Does the table for a = 9..16 match?"
UNEXAMINED: Final = "a = 9..16"
TABLE: Final = f"{AREA}/table.txt"
TASK_RESULT: Final[Json] = {
    "status": "partial",
    "summary": "a^16 mod 17 is 1 for a = 1..8, saved as table.txt.",
    "deviations": [DEVIATION],
    "open_questions": [QUESTION],
    "result_files": [TABLE],
}
CHECK_RESULT: Final[Json] = {
    "entry": "C-1",
    "revision": 1,
    "status": "partial",
    "verdict": "inconclusive",
    "summary": "a^16 mod 17 is 1 for a = 1..8.",
    "coverage": {"examined": ["a^16 mod 17 for a = 1..8"], "not_examined": [UNEXAMINED]},
    "deviations": [DEVIATION],
    "open_questions": [QUESTION],
}
TASK_DONE: Final[Json] = {
    "status": "complete",
    "summary": "Surveyed.",
    "deviations": [],
    "open_questions": [],
}
STEP: Final = Reply(calls=[call("list_files", path=".")])
_ID = re.compile(r"dlg-[0-9a-f]{24}")
_MESSAGE = re.compile(r"agm-[0-9a-f]+")


async def drive(
    harness: Harness,
    run_id: str,
    script: Sequence[Reply],
    *,
    delegated: Sequence[Reply] = (),
    routed: Mapping[str, Sequence[Reply]] | None = None,
    max_turns: int | None = None,
) -> tuple[int, RunRecord]:
    """One ``run`` through the CLI, as ``Harness.run`` makes it, whatever its exit status:
    the status, and the record as the project keeps it. The scripts must be used up."""
    gateway = harness.gateway
    gateway.script = list(script)
    gateway.delegated = list(delegated)
    gateway.routed = {key: list(replies) for key, replies in (routed or {}).items()}
    gateway.requests.clear()
    transcript = harness.tmp / f"{run_id}.json"
    token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
    token.write_text(TOKEN)
    key.write_text(API_KEY)
    argv = [
        "run", "--worker", harness.worker.url, "--token-file", str(token),
        "--project", PROJECT, "--session", SESSION, "--state-dir", str(harness.state),
        "--allow-unisolated", "--model-url", gateway.url, "--api-key-file", str(key),
        "--model", "main", "--run-id", run_id, "--transcript", str(transcript),
    ]  # fmt: skip
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    status = await asyncio.to_thread(cli.main, [*argv, "Characterize the loop."])
    left = (len(gateway.script), len(gateway.delegated), sum(map(len, gateway.routed.values())))
    assert left == (0, 0, 0), f"scripts left unused (root, delegated, routed): {left}"
    return status, RunRecord.model_validate_json(transcript.read_text())


def first_id(messages: Sequence[Message]) -> str:
    """The first delegation a run's tool results named."""
    return next(
        found[0]
        for message in messages
        if message["role"] == "tool" and (found := _ID.search(str(message["content"])))
    )


def on_first(name: str, **arguments: Any) -> Reply:
    """A call of ``name`` on the first delegation the run started."""
    return Reply(calls_from=lambda m: [call(name, delegation_id=first_id(m), **arguments)])


def waiting() -> Reply:
    """A wait on the first delegation the run started."""
    return Reply(calls_from=lambda m: [call("wait_delegations", delegation_ids=[first_id(m)])])


def message_status() -> Reply:
    """``agent_message_status`` on the last message the run sent."""

    def calls(messages: Sequence[Message]) -> list[tuple[str, Json]]:
        found = [
            m
            for message in messages
            if message["role"] == "tool"
            for m in _MESSAGE.findall(str(message["content"]))
        ]
        # A refused message has no status to read: the refusal told its sender.
        return [call("agent_message_status", message_id=found[-1])] if found else [STEP.calls[0]]

    return Reply(calls_from=calls)


TOLD: Final = frozenset({"delivered", "recipient_ended", "delivery_failed"})
"""A message's states that say it was read, or that it will not be (PD-44)."""


def told_or_refused(sent: Json, status: Json) -> bool:
    """Whether a ``message_child`` was refused, which tells its sender, or its status says
    it was delivered or will not be."""
    return "error" in sent or status.get("state") in TOLD


def records(harness: Harness) -> list[Delegation]:
    store = ProjectStore(harness.state)
    try:
        return DelegationRecords(store.control).listed(PROJECT, SESSION)[0]
    finally:
        store.close()


def until_ended(harness: Harness, count: int = 1) -> Callable[[], Awaitable[None]]:
    """Waits, while a model thinks, until ``count`` of the session's delegations ended."""

    async def ended() -> None:
        await eventually(lambda: sum(1 for d in records(harness) if d.ended) >= count, timeout=60)

    return ended


def until_called(harness: Harness, brief: str) -> Callable[[], Awaitable[None]]:
    """Waits, while a model thinks, until the delegated run whose brief holds ``brief``
    called the model: its delegation is written and its run is under way."""

    async def called() -> None:
        await eventually(
            lambda: any(brief in json.dumps(seen[:2]) for seen in harness.gateway.children),
            timeout=60,
        )

    return called


def until_running(harness: Harness) -> Callable[[], Awaitable[None]]:
    """Waits, while a model thinks, until a delegated run called the model."""

    async def running() -> None:
        await eventually(lambda: bool(harness.gateway.children), timeout=30)

    return running


def work_state(harness: Harness, run: str) -> tuple[str, str | None] | None:
    """The state and reason of the research work run ``run`` is an attempt of."""
    store = ProjectStore(harness.state)
    try:
        with store.control.read() as tx:
            found = tx.sql.execute(
                "SELECT w.state, w.reason FROM research_attempts a JOIN research_work w "
                "ON w.project=a.project AND w.session=a.session AND w.work=a.work "
                "WHERE a.project=? AND a.session=? AND a.run=?",
                (PROJECT, SESSION, run),
            ).fetchone()
    finally:
        store.close()
    return None if found is None else (str(found[0]), found[1])


def notices(requests: Sequence[Sequence[Message]], delegation_id: str) -> list[Json]:
    """Each delegation notice a model was shown of ``delegation_id``, in order: as a
    message of its own or attached to a tool result."""
    found: list[Json] = []
    for messages in requests:
        for message in messages:
            content = body(str(message.get("content", "")))
            items: list[Json] = []
            if message["role"] == "user" and content.startswith(DELEGATIONS.text):
                items = cast(list[Json], json.loads(content[len(DELEGATIONS.text) :]))
            elif message["role"] == "tool" and DELEGATIONS_ATTACHED.text in content:
                attached = json.loads(content)
                items = cast(list[Json], attached.get("delegation_notices") or [])
            found += [i for i in items if i.get("delegation_id") == delegation_id]
    return found


def tool_results(record: RunRecord) -> list[Json]:
    """A record's tool results as JSON, a notice-carrying result's own result unwrapped."""
    found: list[Json] = []
    for text in results(record):
        value = json.loads(text)
        if isinstance(value, dict) and "tool_result" in value:
            value = json.loads(cast(Json, value)["tool_result"])
        found.append(cast(Json, value))
    return found


def fed(harness: Harness, root: RunRecord, delegation_id: str) -> list[Json]:
    """The review feed's items of ``delegation_id`` once the root's review takes in its
    delegations (``observe_delegated``): each item's locator."""
    documents = harness.documents()
    try:
        observe_delegated(ReviewStore(documents), PROJECT, SESSION, root.run_id)
        with documents.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT locator FROM review_feed WHERE project=? AND session=? "
                "AND source_key LIKE ?",
                (PROJECT, SESSION, f"delegated:{delegation_id}%"),
            ).fetchall()
    finally:
        documents.close()
    return [cast(Json, json.loads(row[0])) for row in rows]


# --- 1. Ends: every way a child can end ------------------------------------------------

How = Literal[
    "submit",
    "turns",
    "tokens",
    "budget",
    "memory",
    "error",
    "error_after_submit",
    "cancel",
]
ASPECTS: Final = ("delegation", "work", "parent", "shown", "review", "message", "files")
"""What each end's row checks; its ``known`` names violations by these."""


@dataclass(frozen=True)
class End:
    """One way a child ends, and what the specification says is observable after it."""

    name: str
    kind: Literal["task", "check"]
    how: How
    ended: str
    """The delegation's ``ended`` (PD-17)."""
    stopped: str
    kept: bool
    """Whether a result is kept: a result the child validly submitted is never discarded,
    whatever ended its run after it (PD-19 keeps one at a limit and none for a failure;
    these rows hold a failure after a valid submission to the stronger rule)."""
    work: frozenset[str]
    """The child's research-work states that leave its root free to finish."""
    parent: str
    """How the root run ends."""
    stands: bool
    """Whether the root's final answer is the one it gave."""
    message: bool
    """Whether a ``message_child`` sent after the end is checked: it is delivered, or its
    sender is told it will not be read (PD-44)."""
    files: Literal["shared", "own"] | None
    """Where the child's table is found afterwards: in the session's environment at its
    write area, or handed back from its own (CE-6)."""
    known: Mapping[str, str] = field(default_factory=dict[str, str])


DONE_STATES: Final = frozenset({"completed", "stopped"})
"""A child's work states its root's completion does not wait on (continuity/records.py,
``_pending_children``)."""
STOPPED: Final = frozenset({"stopped"})
COMPLETED: Final = frozenset({"completed"})

ENDS: Final[list[End]] = [
    # Submitted, then answered: the good path.
    End("task-submit", "task", "submit", "returned", "answered", True, COMPLETED, "answered",
        True, False, "shared", {"shown": "DEL-V5"}),
    End("check-submit", "check", "submit", "returned", "answered", True, COMPLETED, "answered",
        True, True, None, {"shown": "DEL-V5"}),
    End("task-submit-own", "task", "submit", "returned", "answered", True, COMPLETED,
        "answered", True, False, "own", {"shown": "DEL-V5"}),
    # The turn ceiling (PD-19): a limit, its submission kept, made partial.
    End("task-turns", "task", "turns", "limit_reached", "turn_limit", True, DONE_STATES,
        "answered", True, True, "shared",
        {"work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V5", "message": "DEL-V4"}),
    End("check-turns", "check", "turns", "limit_reached", "turn_limit", True, DONE_STATES,
        "answered", True, True, None,
        {"work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V5", "message": "DEL-V4"}),
    End("task-turns-own", "task", "turns", "limit_reached", "turn_limit", True, DONE_STATES,
        "answered", True, True, "own",
        {"work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V5", "message": "DEL-V4"}),
    # A soft token cap (D102): the parent's stop ends it with its report round.
    End("task-tokens", "task", "tokens", "limit_reached", "resource_report", True, DONE_STATES,
        "answered", True, True, "shared",
        {"work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V5", "message": "DEL-V4"}),
    End("check-tokens", "check", "tokens", "limit_reached", "resource_report", True,
        DONE_STATES, "answered", True, True, None,
        {"work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V5", "message": "DEL-V4"}),
    # The gateway's budget, which is the project's cost limit (RS-6a): a limit after a
    # valid submission. The root meets it too, so its own end is the budget's.
    End("task-budget", "task", "budget", "limit_reached", "gateway_budget", True, DONE_STATES,
        "gateway_budget", False, False, None,
        {"delegation": "DEL-V3", "work": "DEL-V1", "shown": "DEL-V3"}),
    End("check-budget", "check", "budget", "limit_reached", "gateway_budget", True,
        DONE_STATES, "gateway_budget", False, False, None,
        {"delegation": "DEL-V3", "work": "DEL-V1", "shown": "DEL-V3"}),
    # The working-memory limit after a valid submission.
    End("task-memory", "task", "memory", "limit_reached", "memory_limit", True, DONE_STATES,
        "answered", True, True, "shared",
        {"delegation": "DEL-V3", "work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V3",
         "message": "DEL-V4"}),
    # A model error before anything was submitted: failed, nothing to keep.
    End("task-error", "task", "error", "failed", "error", False, DONE_STATES, "answered", True,
        True, None, {"work": "DEL-V1", "parent": "DEL-V1", "message": "DEL-V4"}),
    End("check-error", "check", "error", "failed", "error", False, DONE_STATES, "answered",
        True, True, None, {"work": "DEL-V1", "parent": "DEL-V1", "message": "DEL-V4"}),
    # A model error after a valid submission: the submission is kept.
    End("task-error-after-submit", "task", "error_after_submit", "failed", "error", True,
        DONE_STATES, "answered", True, True, "shared",
        {"delegation": "DEL-V3", "work": "DEL-V1", "parent": "DEL-V1", "shown": "DEL-V3",
         "message": "DEL-V4"}),
    # Cancelled by its parent (PD-39).
    End("task-cancel", "task", "cancel", "cancelled", "cancelled", False, STOPPED, "answered",
        True, True, None),
    End("check-cancel", "check", "cancel", "cancelled", "cancelled", False, STOPPED,
        "answered", True, True, None),
]  # fmt: skip


def child_script(row: End, harness: Harness, hold: asyncio.Event) -> list[Reply]:
    """The child's replies for ``row``; a cancelled child's call waits for ``hold``."""
    task = row.kind == "task"
    result = TASK_RESULT if task else CHECK_RESULT
    write = (
        [Reply(calls=[call("write_file", path=TABLE, content="1 1 1 1 1 1 1 1\n")])]
        if task and row.files
        else []
    )
    kept = result if row.files else {k: v for k, v in result.items() if k != "result_files"}
    submit = Reply(calls=[call("submit_result", **kept)])
    match row.how:
        case "submit":
            return [*write, submit, Reply("Done.")]
        case "turns":
            # Its ceiling's last call, then its report round, which runs no tool.
            return [*write, submit, STEP, Reply("My report: a = 1..8 done.")]
        case "tokens":
            # Past the soft cap it goes on one step; at its grant's ceiling it checks in.
            return [
                *write,
                Reply(calls=[call("submit_result", **kept)], tokens=60_000),
                Reply(calls=[call("list_files", path=".")], tokens=60_000),
                Reply("My report: a = 1..8 done."),
            ]
        case "budget":

            async def limit() -> None:
                # Once the parent's next call is admitted, so that only its later ones and
                # the child's meet the limit.
                await eventually(lambda: len(harness.gateway.main) >= 2, timeout=30)
                RunState(ProjectStore(harness.state).control).set_cost_limit(PROJECT, 0.5)

            return [*write, Reply(calls=[call("submit_result", **kept)], cost="1.0", before=limit)]
        case "memory":
            return [*write, submit]  # the next call meets the limit
        case "error":
            return [Reply(status=400)]
        case "error_after_submit":
            return [*write, submit, Reply(status=400)]
        case "cancel":

            async def held() -> None:
                await hold.wait()

            return [Reply(calls=[call("list_files", path=".")], before=held)]


def configure_for(row: End, harness: Harness) -> None:
    task = row.kind == "task"
    match row.how:
        case "turns":
            turns = 3 if task and row.files else 2
            configure(harness, depth=1, turn_ceiling=turns, task_turns=1, check_turns=1)
        case "tokens":
            caps = {"task_tokens": 50_000} if task else {"check_tokens": 50_000}
            configure(harness, depth=1, max_token_grant=100_000, **caps)
        case _:
            pass


def brief(row: End) -> tuple[str, Json]:
    return call("delegate", task=TASK_TEXT) if row.kind == "task" else CHECK


def root_script(row: End, harness: Harness, hold: asyncio.Event) -> tuple[list[Reply], int]:
    """The root's replies, and how many come before its observations: it delegates,
    learns of the end by its notice, then waits, reads the status, and, where the row
    says, sends the child a message and reads its status; then it answers."""
    if row.how == "budget":
        return [
            Reply(calls=[brief(row)]),
            Reply(calls=[STEP.calls[0]], before=until_ended(harness)),
        ], 1
    began: list[Reply] = [Reply(calls=[brief(row)])]
    if row.how == "tokens":
        began += [waiting(), decide("stop")]
    if row.how == "cancel":
        cancel = on_first("cancel_delegation")
        cancel.before = until_running(harness)
        began.append(cancel)

    async def ended() -> None:
        hold.set()  # a cancelled child's held call may answer now; nothing reads it
        await until_ended(harness)()

    tail = [Reply(calls=[STEP.calls[0]], before=ended), waiting(), on_first("delegation_status")]
    if row.message:
        tail += [on_first("message_child", text="Please also do a = 9..16."), message_status()]
    if row.files == "shared":
        # The parent reads the child's table where its result names it (PD-46).
        tail.append(
            Reply(calls_from=lambda m: [call("read_file", path=f"work/{first_id(m)}/table.txt")])
        )
    return [*began, *tail, Reply(FINAL)], len(began)


@pytest.mark.parametrize("row", [pytest.param(r, id=r.name, marks=marks(r.known)) for r in ENDS])
async def test_a_child_ends(
    row: End,
    harness: Harness,
    service: ChildService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(harness, depth=1, **({"child_environments": "tasks"} if row.files == "own" else {}))
    claim = {
        "type": "claim",
        "origin": "project",
        "title": "Fermat for 17",
        "statement": "$a^{16} = 1$ for every nonzero $a$ in GF(17).",
        "fields": {"kind": "observation"},
    }
    documents = harness.documents()
    try:
        Ledger(documents).change(
            PROJECT, {"operation_id": "c1", "reason": "Proved.", "changes": [claim]}, USER
        )
    finally:
        documents.close()
    configure_for(row, harness)
    if row.how == "memory":
        original = AgentContext.sent.fget
        assert original is not None

        def guarded(run: AgentContext) -> object:
            # The child's next context after its submission was echoed: the limit.
            if (
                isinstance(run.log, RunArchive)
                and run.log.parent is not None
                and any(
                    m.get("role") == "tool" and "your run can end now" in str(m.get("content"))
                    for m in run.messages
                )
            ):
                raise WorkingMemoryLimitError("synthetic child working-state ceiling")
            return original(run)

        monkeypatch.setattr(AgentContext, "sent", property(guarded))
    hold = asyncio.Event()
    script, began = root_script(row, harness, hold)
    _, root = await drive(harness, "root", script, delegated=child_script(row, harness, hold))
    (delegation_id,) = started(root)
    found = delegation(harness, delegation_id)
    verdicts = Verdicts(row.known)

    # The delegation's outcome (PD-17, PD-19): how it ended, and whether a result is kept.
    verdicts.check(
        "delegation",
        (found.ended, found.stopped) == (row.ended, row.stopped),
        (found.ended, found.stopped),
    )
    verdicts.check("delegation", (found.result is not None) == row.kept, found.result)
    if row.kept and found.result is not None:
        # The child's own result, its deviations among it; at a limit, the platform's
        # marks beside it (PD-19).
        verdicts.check("delegation", found.result.get("deviations") == [DEVIATION], found.result)
        returned = row.ended == "returned"
        verdicts.check("delegation", returned == ("set_by" not in found.result), found.result)

    # The child's research work: it leaves its root free to finish.
    state = work_state(harness, found.run)
    verdicts.check("work", state is not None and state[0] in row.work, state)

    # The root's own end, and whether its answer stands.
    verdicts.check("parent", root.stopped == row.parent, (root.stopped, root.failure))
    verdicts.check("parent", (root.final_text == FINAL) == row.stands, root.final_text)

    # What the parent was shown.
    if row.how == "budget":
        RunState(ProjectStore(harness.state).control).set_cost_limit(PROJECT, None)
        await drive(harness, "later", [Reply("Noted.")])
    answers = tool_results(root)
    # A cancel's result is the outcome as the parent sees it (PD-39); otherwise a notice.
    shown = (
        [answers[began - 1]]
        if row.how == "cancel"
        else notices(harness.gateway.main, delegation_id)
    )
    verdicts.check("shown", bool(shown), "no delegation notice reached the parent")
    if shown:
        first = shown[0]
        verdicts.check(
            "shown", (first.get("ended"), first.get("stopped")) == (row.ended, row.stopped), first
        )
        if row.kept:
            text = json.dumps(first)
            verdicts.check("shown", DEVIATION in text and QUESTION in text, first)
            if row.kind == "check":
                verdicts.check("shown", UNEXAMINED in text, first)
    if row.how != "budget":
        waited, status = answers[began + 1], answers[began + 2]
        verdicts.check("shown", waited.get("reason") == "ended", waited)
        whole = cast(Json, (waited.get("delegations") or [{}])[0])
        for outcome in (whole, status):
            verdicts.check("shown", outcome.get("ended") == row.ended, outcome)
            if row.kept:
                result = cast(Json, outcome.get("result") or {})
                verdicts.check(
                    "shown",
                    result.get("deviations") == [DEVIATION]
                    and result.get("open_questions") == [QUESTION],
                    outcome,
                )
                if row.kind == "check":
                    coverage = cast(Json, result.get("coverage") or {})
                    verdicts.check("shown", coverage.get("not_examined") == [UNEXAMINED], outcome)

    # The review feed takes in the child's work (CR-2).
    items = fed(harness, root, delegation_id)
    verdicts.check("review", any(i.get("run") == run_name_id(found.run) for i in items), items)

    # A message sent after the end: delivered, or its sender told it will not be read.
    if row.message:
        sent, told = answers[began + 3], answers[began + 4]
        verdicts.check("message", told_or_refused(sent, told), (sent, told))

    # The child's files: in its write area in the session's environment, or handed back.
    if row.files == "shared":
        read = answers[-1]
        verdicts.check("files", "1 1 1 1" in json.dumps(read), read)
    elif row.files == "own":
        backs = [b for b in service.hand_backs if b.delegation_id == delegation_id]
        verdicts.check(
            "files", any(f"work/{delegation_id}/table.txt" in b.files for b in backs), backs
        )
    verdicts.settle()


def run_name_id(run: str) -> str:
    """The run ID part of a record name."""
    return run.split("Z-", 1)[1]


async def test_a_childs_submitted_result_outlives_its_deleted_environment(
    harness: Harness, service: ChildService
) -> None:
    """A child in its own environment submits its result; its sandbox is then deleted,
    so the worker answers its closing's job read with ``no environment``. The closing
    still closes (D-G3: a hook that fails for a reason outside the agent's control never
    ends a run): the child ends with its answer, its record names the gap, and the parent
    reads the result it submitted."""
    configure(harness, depth=1, child_environments="tasks")

    async def deleted() -> None:
        (environment,) = [c.environment_id for c in service.made.values()]
        service.delete(environment)

    child = [
        Reply(calls=[call("write_file", path=TABLE, content="1 1 1 1 1 1 1 1\n")]),
        Reply(calls=[call("submit_result", **TASK_RESULT)]),
        Reply("Done.", before=deleted),
    ]
    root = [
        Reply(calls=[call("delegate", task=TASK_TEXT)]),
        Reply(calls=[STEP.calls[0]], before=until_ended(harness)),
        waiting(),
        on_first("delegation_status"),
        Reply(FINAL),
    ]
    _, record = await drive(harness, "root", root, delegated=child)
    (delegation_id,) = started(record)
    found = delegation(harness, delegation_id)
    assert service.gone, "the environment was never deleted"
    assert (found.ended, found.stopped) == ("returned", "answered"), (found.ended, found.stopped)
    assert found.result is not None
    assert found.result.get("deviations") == [DEVIATION]
    store = ProjectStore(harness.state)
    try:
        kept = store.run(PROJECT, SESSION, found.run)
    finally:
        store.close()
    assert (kept.stopped, kept.failure, kept.final_text) == ("answered", None, "Done.")
    gaps = cast(Json, kept.summaries[GAPS])["gaps"]
    assert [(g["source"], g["point"]) for g in gaps] == [("jobs", "closing")], gaps
    status = tool_results(record)[-1]
    assert status.get("ended") == "returned", status
    assert cast(Json, status.get("result") or {}).get("deviations") == [DEVIATION], status
    assert (record.stopped, record.final_text) == ("answered", FINAL)


# --- 1a. Every stop a child's run can record, as its delegation keeps it -------------


@dataclass(frozen=True)
class Stop:
    """A child run's stop reason, the delegation's ``ended`` the specification gives it
    (PD-17, PD-19, PD-22: a turn, time or cost limit is a limit, not a failure), and
    whether a valid submission before it is kept."""

    stopped: str
    ended: str
    kept: bool
    known: Mapping[str, str] = field(default_factory=dict[str, str])


STOPS: Final[list[Stop]] = [
    Stop("answered", "returned", True),
    Stop("turn_limit", "limit_reached", True),
    Stop("context_limit", "limit_reached", True),
    Stop("run_budget", "limit_reached", True),
    Stop("budget", "limit_reached", True),
    Stop("runaway", "limit_reached", True),
    Stop("resource_pause", "limit_reached", True),
    Stop("resource_report", "limit_reached", True),
    Stop("resource_time", "limit_reached", True),
    Stop("resource_storage", "limit_reached", True),
    Stop("resource_retention", "limit_reached", True),
    Stop("environment_time", "limit_reached", True),
    Stop("gateway_budget", "limit_reached", True, {"ended": "DEL-V3", "kept": "DEL-V3"}),
    Stop("memory_limit", "limit_reached", True, {"ended": "DEL-V3", "kept": "DEL-V3"}),
    Stop("model_refused", "failed", True, {"kept": "DEL-V3"}),
    Stop("error", "failed", True, {"kept": "DEL-V3"}),
    Stop("cancelled", "cancelled", False),
    Stop("interrupted", "interrupted", False),
]
"""The model's refusal, which the loop harness's gateway cannot script (its replies have
no refusal), is walked here and not end to end; so are the other stops no scripted run
reaches in seconds."""


@pytest.mark.parametrize(
    "row", [pytest.param(r, id=r.stopped, marks=marks(r.known)) for r in STOPS]
)
async def test_a_childs_stop_is_kept_as_its_delegation_ends(row: Stop) -> None:
    """How the harness observes a child's stop (``observed``), and what its outcome keeps
    of a result the child submitted before it (``Outcome.kept``)."""
    verdicts = Verdicts(row.known)
    verdicts.check("ended", observed(ChildEnd(row.stopped, False, None)) == row.ended)
    found = Outcome(task=True)
    answer, _ = await found.submit(json.dumps(TASK_DONE))
    assert "your run can end now" in answer, answer
    verdicts.check("kept", (found.kept(row.stopped) is not None) == row.kept, row.stopped)
    verdicts.settle()


# --- 1b. Ends that come from the parent: cancelled, or its lease lost ------------------


@dataclass(frozen=True)
class ParentEnd:
    """A child ended by what happens to its parent (PD-39, PD-40): the user's cancel of
    the root, or the root's lease taken by another holder."""

    name: str
    kind: Literal["task", "check"]
    how: Literal["cancelled", "lease_lost"]
    ended: str
    work: frozenset[str]
    """The child's research-work states: a cancelled tree's work is stopped; an
    interrupted one stays paused for a Resume (PD-22)."""
    parent: str | None
    """How the root's record ends, when it saves one."""
    known: Mapping[str, str] = field(default_factory=dict[str, str])


PARENT_ENDS: Final[list[ParentEnd]] = [
    ParentEnd("task-parent-cancelled", "task", "cancelled", "cancelled", STOPPED, "cancelled"),
    ParentEnd("check-parent-cancelled", "check", "cancelled", "cancelled", STOPPED, "cancelled"),
    ParentEnd("task-lease-lost", "task", "lease_lost", "interrupted", frozenset({"paused"}), None),
]  # fmt: skip


@pytest.mark.parametrize(
    "row", [pytest.param(r, id=r.name, marks=marks(r.known)) for r in PARENT_ENDS]
)
async def test_a_child_ends_with_its_parent(row: ParentEnd, delegating: Harness) -> None:
    """The child is held in a model call while its root is cancelled or loses its lease;
    the next main run is told of the outcome, and a message to the ended child is
    delivered or reported as not to be read."""
    free = asyncio.Event()

    async def held() -> None:
        await free.wait()

    delegating.gateway.delegated = [Reply(calls=[STEP.calls[0]], before=held)]
    store = ProjectStore(delegating.state)
    state = RunState(store.control)
    state.post(PROJECT, SESSION, "m-1", "Check C-1.", author="user")
    started_with = call("delegate", task=TASK_TEXT) if row.kind == "task" else CHECK
    lease = 6.0 if row.how == "lease_lost" else 60.0
    try:
        async with serving(delegating, Reply(calls=[started_with]), waiting(), lease_seconds=lease):
            await eventually(lambda: delegating.gateway.children, timeout=20)
            holder = state.lease(PROJECT, SESSION)
            assert holder is not None
            root_run = holder.run
            if row.how == "cancelled":
                state.cancel(PROJECT, SESSION, root_run)
            else:
                with state.control.write() as tx:  # another holder takes the session
                    tx.sql.execute("DELETE FROM run_leases")
                state.acquire(PROJECT, SESSION, "another", "thief")
            await eventually(lambda: any(d.ended for d in records(delegating)), timeout=30)
            if row.how == "lease_lost":
                with state.control.write() as tx:
                    tx.sql.execute("DELETE FROM run_leases")
            else:
                await eventually(lambda: state.ended(PROJECT, SESSION, root_run), timeout=30)
    finally:
        free.set()
        store.close()
    (found,) = records(delegating)
    verdicts = Verdicts(row.known)
    verdicts.check(
        "delegation", (found.ended, found.result) == (row.ended, None), (found.ended, found.result)
    )
    state_of = work_state(delegating, found.run)
    verdicts.check("work", state_of is not None and state_of[0] in row.work, state_of)
    if row.parent is not None:
        projects = ProjectStore(delegating.state)
        try:
            root = projects.run(PROJECT, SESSION, root_run)
        finally:
            projects.close()
        verdicts.check("parent", root.stopped == row.parent, root.stopped)

    # The session's next main run is told how it ended, and messages it.
    script = [
        Reply(calls=[STEP.calls[0]]),
        Reply(
            calls=[
                call(
                    "message_child",
                    delegation_id=found.delegation_id,
                    text="Please go on with a = 9..16.",
                )
            ]
        ),
        message_status(),
        Reply(FINAL),
    ]
    _, later = await drive(delegating, "later", script)
    shown = notices(delegating.gateway.main, found.delegation_id)
    verdicts.check("shown", bool(shown) and shown[0].get("ended") == row.ended, shown)
    sent, told = tool_results(later)[-2:]
    verdicts.check("message", told_or_refused(sent, told), (sent, told))
    verdicts.settle()


# --- 2. Refusals of delegate and of the other delegation tools -----------------------

NEXT_STEP: Final = re.compile(
    r"(?:^|[;:.,(]\s*|\bthen\s+)(call|wait|use|give|choose|remove|drop|send|ask|list|start"
    r"|pass|omit|leave|shorten|split|try|name|read|record)\b",
    re.IGNORECASE,
)
"""A refusal's next step: an imperative that opens a clause, which the agent can act on
(the audit's I4)."""
UNKNOWN_ID: Final = "dlg-" + "0" * 24
TASK_A: Final = "Survey the lattice parameters."
TASK_B: Final = "Tabulate the residues."


@dataclass(frozen=True)
class Refused:
    """A refusal of a delegation tool, the cause it must state, and its kind (the
    delegation map, section 1b, R1 to R22, and the other tools)."""

    name: str
    kind: str
    cause: str
    """A text of the real cause the refusal states."""
    known: Mapping[str, str] = field(default_factory=dict[str, str])
    """By aspect (``kind``, ``cause``, ``next_step``), the known violation."""


NO_NEXT: Final[Mapping[str, str]] = {"next_step": "DEL-V11"}
REFUSALS: Final[list[Refused]] = [
    Refused("R1-task-and-check", "invalid_request", "a check or a task, not both", NO_NEXT),
    Refused("R2-check-connectors", "policy_denied", "connectors", NO_NEXT),
    Refused("R3-check-references", "policy_denied", "references", NO_NEXT),
    Refused("R4-task-too-long", "invalid_request", "8000", NO_NEXT),
    Refused("R5-child-delegates-a-check", "policy_denied", "never checks", NO_NEXT),
    Refused("R6-no-turn-left", "unavailable", "no turn left", NO_NEXT),
    Refused("R7-depth-reduced", "policy_denied", "depth was reduced", NO_NEXT),
    Refused("R8-cap-full", "unavailable", "1 delegated runs are running",
            {"cause": "DEL-V11", "next_step": "DEL-V11"}),
    Refused("R9-model-not-offered", "policy_denied", "model_not_allowed", NO_NEXT),
    Refused("R10-child-task-on-a-track", "unavailable", "take no track", NO_NEXT),
    Refused("R11-no-active-plan", "invalid_request", "there is none", NO_NEXT),
    Refused("R12-delegate-at-depth-one", "policy_denied", "only a main run's task may delegate",
            NO_NEXT),
    Refused("R13-not-a-connector", "invalid_request", "names no configured connector server"),
    Refused("R14-reference-refused", "policy_denied", "X-99", NO_NEXT),
    Refused("R15-not-an-issue", "invalid_request", "not an issue", NO_NEXT),
    Refused("R16-check-of-no-entry", "not_found", "C-9"),
    Refused("R17-own-environment-refused", "policy_denied", "child_environments_max", NO_NEXT),
    Refused("R19-tree-cap-full", "unavailable", "2 delegated runs are running in this session"),
    Refused("R20-track-busy", "conflict", "track_busy", NO_NEXT),
    Refused("R22-plan-fence", "policy_denied", "plan is not active", NO_NEXT),
    Refused("wait-unknown", "not_found", UNKNOWN_ID, NO_NEXT),
    Refused("status-unknown", "not_found", UNKNOWN_ID, NO_NEXT),
    Refused("cancel-unknown", "not_found", UNKNOWN_ID, NO_NEXT),
    Refused("accept-a-check", "conflict", "a check", {"cause": "T2-N1", "next_step": "DEL-V11"}),
    Refused("message-an-accepted-task", "conflict", "task_accepted"),
]  # fmt: skip
"""Not written: R18 (an explicit Stop of the tree as the delegation is written: the Stop
ends the parent's loop before its call runs) and R21 (an operation replayed with other
arguments: the operation ID is the platform's, never the model's)."""


def _held(release: asyncio.Event) -> Reply:
    async def held() -> None:
        await release.wait()

    return Reply(calls=[STEP.calls[0]], before=held)


def refusal_case(
    row: Refused, harness: Harness, service: ChildService, release: asyncio.Event
) -> tuple[list[Reply], dict[str, list[Reply]], bool, int | None]:
    """The root's script, the children's routed scripts, whether the refusal is a
    child's, and the root's turns, for ``row``."""
    task = call("delegate", task=TASK_A)
    check = {"entry": "C-1", "revision": 1}
    root: list[Reply]
    routed: dict[str, list[Reply]] = {}
    child, turns = False, None
    done = Reply(FINAL)

    async def free() -> None:
        release.set()

    freeing = Reply(FINAL, before=free)
    match row.name.split("-", 1)[0] if row.name.startswith("R") else row.name:
        case "R1":
            root = [Reply(calls=[call("delegate", task="Go.", check=check)]), done]
        case "R2":
            root = [Reply(calls=[call("delegate", check=check, connectors=["x"])]), done]
        case "R3":
            root = [Reply(calls=[call("delegate", check=check, references=["C-1"])]), done]
        case "R4":
            root = [Reply(calls=[call("delegate", task="x" * 8001)]), done]
        case "R5" | "R10" | "R19":
            configure(harness, depth=2, **({"cap": 2} if row.name.startswith("R19") else {}))
            asked = {
                "R5": CHECK,
                "R10": call("delegate", task="Grandchild.", track="tables"),
                "R19": call("delegate", task="Grandchild."),
            }[row.name.split("-", 1)[0]]
            pair = [call("delegate", task=TASK_A, delegate=True)]
            if row.name.startswith("R19"):
                pair.append(call("delegate", task=TASK_B))
                routed[TASK_B] = [_held(release), Reply("Done.")]
            first = Reply(calls=[asked])
            if row.name.startswith("R19"):
                # The tree's cap is full only once the sibling runs too: A asks for its
                # grandchild once B's run is under way, held in its first call until A
                # has read its refusal and ended.
                first.before = until_called(harness, TASK_B)
            routed[TASK_A] = [first, Reply("Done.")]
            child = True
            root = [
                Reply(calls=pair),
                Reply(calls=[STEP.calls[0]], before=until_ended(harness)),
                freeing,
            ]
            if row.name.startswith("R19"):
                root.insert(2, Reply(calls=[STEP.calls[0]], before=free))
                root[-1] = Reply(FINAL, before=until_ended(harness, 2))
        case "R6":
            root, turns = [Reply(calls=[task])], 1
        case "R7":

            async def reduce() -> None:
                configure(harness, depth=0)

            root = [Reply(calls=[task], before=reduce), done]
        case "R8":
            configure(harness, depth=1, cap=1)
            routed[TASK_A] = [_held(release), Reply("Done.")]
            root = [Reply(calls=[task, call("delegate", task=TASK_B)]), freeing]
        case "R9":
            root = [Reply(calls=[call("delegate", task=TASK_A, model="other")]), done]
        case "R11":
            root = [Reply(calls=[call("delegate", task=TASK_A, track="tables")]), done]
        case "R12":
            root = [Reply(calls=[call("delegate", task=TASK_A, delegate=True)]), done]
        case "R13":
            root = [Reply(calls=[call("delegate", task=TASK_A, connectors=["nope"])]), done]
        case "R14":
            asked = call("delegate", task=TASK_A, references=[{"entry": "X-99"}])
            root = [Reply(calls=[asked]), done]
        case "R15":
            root = [Reply(calls=[call("delegate", task=TASK_A, issue="C-1")]), done]
        case "R16":
            root = [Reply(calls=[call("delegate", check={"entry": "C-9", "revision": 1})]), done]
        case "R17":
            configure(harness, depth=1, child_environments="tasks")
            service.limit = 0
            root = [Reply(calls=[task]), done]
        case "R20":
            active_plan(harness)
            routed["Tabulate a^16 mod 17."] = [_held(release), Reply("Done.")]
            on_track = call("delegate", task="Tabulate a^16 mod 17.", track="tables")
            root = [READ, Reply(calls=[on_track, on_track]), freeing]
        case "R22":
            harness.user_plan()  # a draft the user made, which the root never read
            root = [Reply(calls=[task]), done]
        case "wait-unknown":
            root = [Reply(calls=[call("wait_delegations", delegation_ids=[UNKNOWN_ID])]), done]
        case "status-unknown":
            root = [Reply(calls=[call("delegation_status", delegation_id=UNKNOWN_ID)]), done]
        case "cancel-unknown":
            root = [Reply(calls=[call("cancel_delegation", delegation_id=UNKNOWN_ID)]), done]
        case "accept-a-check":
            root = [Reply(calls=[CHECK]), waiting(), on_first("accept_delegation"), done]
            harness.gateway.delegated = [
                Reply(calls=[call("submit_result", **CHECK_RESULT)]),
                Reply("Done."),
            ]
        case "message-an-accepted-task":
            routed[TASK_A] = [Reply(calls=[call("submit_result", **TASK_DONE)]), Reply("Done.")]
            root = [
                Reply(calls=[task]),
                waiting(),
                on_first("accept_delegation"),
                on_first("message_child", text="One more thing."),
                done,
            ]
        case _:
            raise AssertionError(row.name)
    return root, routed, child, turns


@pytest.mark.parametrize(
    "row", [pytest.param(r, id=r.name, marks=marks(r.known)) for r in REFUSALS]
)
async def test_a_delegation_tool_refuses(
    row: Refused, delegating: Harness, service: ChildService
) -> None:
    """Each refusal has its kind, states its real cause, and names the next step."""
    release = asyncio.Event()
    script, routed, by_child, turns = refusal_case(row, delegating, service, release)
    delegated = list(delegating.gateway.delegated)
    try:
        _, root = await drive(
            delegating, "root", script, delegated=delegated, routed=routed, max_turns=turns
        )
    finally:
        release.set()
    if by_child:
        found = [r for r in children(delegating, root) if TASK_A in json.dumps(r.messages[:2])]
        answers = [a for r in found for a in tool_results(r)]
    else:
        answers = tool_results(root)
    refusals = [cast(Json, a["error"]) for a in answers if isinstance(a.get("error"), dict)]
    verdicts = Verdicts(row.known)
    verdicts.check("kind", bool(refusals), answers)
    if refusals:
        refused = refusals[0]
        text = str(refused.get("message"))
        verdicts.check("kind", refused.get("kind") == row.kind, refused)
        verdicts.check("cause", row.cause in text, text)
        reason = text.split(": ", 1)[1] if re.match(r"^[a-z_]+: ", text) else text
        verdicts.check("next_step", bool(NEXT_STEP.search(reason)), text)
    verdicts.settle()


# --- 3. The return path: reopening, results, acceptance -------------------------------


@dataclass(frozen=True)
class Walk:
    """A path after a task returned, or through a tree, or of an instruction, with the
    known violations by aspect."""

    name: str
    known: Mapping[str, str] = field(default_factory=dict[str, str])


RETURNS: Final[list[Walk]] = [
    Walk("reopen-keeps-history-environment-and-numbers-results"),
    Walk("reopen-takes-the-current-plan-version"),
    Walk("review-takes-in-each-result", {"review": "DEL-V2"}),
    Walk("acceptance-records-what-was-read"),
    Walk("platform-accepts-only-at-a-final-answer", {"review": "DEL-V6"}),
    Walk("next-run-is-told-of-platform-acceptance", {"told": "DEL-V6"}),
]
"""Not walked here: a task past ``review_max`` (60 s at least, and a CLI run reads the
system clock; ``test_parent_review`` walks it on the hook with a fake clock)."""

FEEDBACK: Final = "Also tabulate a = 9..16."
FIRST: Final = TASK_RESULT
SECOND: Final[Json] = {
    **TASK_RESULT,
    "status": "complete",
    "summary": "a^16 mod 17 is 1 for a = 1..16.",
    "deviations": [],
    "open_questions": [],
}


FILELESS: Final[Json] = {**FIRST, "result_files": []}


def returned_then_reopened() -> list[Reply]:
    """A task's replies: it writes its table and returns; reopened, it returns again."""
    return [
        Reply(calls=[call("write_file", path=TABLE, content="1 1 1 1 1 1 1 1\n")]),
        Reply(calls=[call("submit_result", **FIRST)]),
        Reply("Done."),
        Reply(calls=[call("submit_result", **SECOND)]),
        Reply("Done again."),
    ]


async def walk_return(row: Walk, harness: Harness, verdicts: Verdicts) -> None:
    match row.name:
        case "reopen-keeps-history-environment-and-numbers-results":
            _, root = await drive(
                harness,
                "root",
                [
                    Reply(calls=[call("delegate", task=TASK_TEXT)]),
                    waiting(),
                    on_first("message_child", text=FEEDBACK),
                    waiting(),
                    Reply(FINAL),
                ],
                routed={TASK_TEXT: returned_then_reopened()},
            )
            (delegation_id,) = started(root)
            found = delegation(harness, delegation_id)
            verdicts.check("results", (found.results, found.review) == (2, "accepted"), found)
            store = ProjectStore(harness.state)
            try:
                kept = DelegationRecords(store.control).results(PROJECT, SESSION, delegation_id)
            finally:
                store.close()
            verdicts.check("results", [r.number for r in kept] == [1, 2], kept)
            runs = sorted(
                (r for r in children(harness, root) if r.delegation == delegation_id),
                key=lambda r: r.started_at,
            )
            verdicts.check("history", len(runs) == 2, runs)
            if len(runs) == 2:
                again = json.dumps(runs[1].messages)
                # Its own history: the first run's call and result, then the platform's
                # word and the parent's message (PD-46).
                verdicts.check(
                    "history",
                    "table.txt" in again and "reopened this task" in again and FEEDBACK in again,
                    again[:2000],
                )
                verdicts.check(
                    "environment",
                    runs[0].context.environment_id == runs[1].context.environment_id,
                    runs,
                )
            waited = tool_results(root)[3]
            outcome = cast(Json, (waited.get("delegations") or [{}])[0])
            verdicts.check("results", outcome.get("result_number") == 2, outcome)
        case "reopen-takes-the-current-plan-version":
            ref = active_plan(harness)

            async def revise() -> None:
                documents = harness.documents()
                try:
                    SessionPlans(documents).revise(
                        USER_PLANS, "p3", ref, PlanBody.model_validate(TRACKED), "Edited."
                    )
                finally:
                    documents.close()

            on_track = call("delegate", task="Tabulate a^16 mod 17.", track="tables")
            progress = call("track_progress", step_id="table", status="completed", note="Done.")
            reread = Reply(calls=[call("plan_read")], before=revise)
            _, root = await drive(
                harness,
                "root",
                [
                    READ,
                    Reply(calls=[on_track]),
                    waiting(),
                    reread,
                    on_first("message_child", text=FEEDBACK),
                    waiting(),
                    READ,
                    Reply(FINAL),
                ],
                routed={
                    "Tabulate a^16 mod 17.": [
                        Reply(calls=[call("submit_result", **FILELESS)]),
                        Reply("Done."),
                        Reply(calls=[progress]),
                        Reply(calls=[call("submit_result", **{**SECOND, "result_files": []})]),
                        Reply("Done again."),
                    ]
                },
            )
            (delegation_id,) = started(root)
            runs = sorted(
                (r for r in children(harness, root) if r.delegation == delegation_id),
                key=lambda r: r.started_at,
            )
            sent = tool_results(root)[4]
            verdicts.check("reopened", "reopened" in sent, sent)
            if len(runs) == 2:
                progressed = tool_results(runs[1])[0]
                verdicts.check("track", "error" not in progressed, progressed)
            else:
                verdicts.check("reopened", False, runs)
        case "review-takes-in-each-result":

            async def reviewed() -> None:
                # The root's review takes in what ended so far, as at its first window.
                documents = harness.documents()
                try:
                    observe_delegated(ReviewStore(documents), PROJECT, SESSION, "root")
                finally:
                    documents.close()

            message = on_first("message_child", text=FEEDBACK)
            message.before = reviewed
            _, root = await drive(
                harness,
                "root",
                [
                    Reply(calls=[call("delegate", task=TASK_TEXT)]),
                    waiting(),
                    message,
                    waiting(),
                    Reply(FINAL),
                ],
                routed={TASK_TEXT: returned_then_reopened()},
            )
            (delegation_id,) = started(root)
            runs = sorted(
                (r for r in children(harness, root) if r.delegation == delegation_id),
                key=lambda r: r.started_at,
            )
            items = fed(harness, root, delegation_id)  # and again after the second result
            verdicts.check("review", len(runs) == 2, runs)
            for run in runs:
                verdicts.check(
                    "review", any(i.get("run") == run.run_id for i in items), (run.run_id, items)
                )
        case "acceptance-records-what-was-read":
            _, root = await drive(
                harness,
                "root",
                [
                    Reply(calls=[call("delegate", task=TASK_TEXT), call("delegate", task=TASK_B)]),
                    Reply(
                        calls_from=lambda m: [
                            call(
                                "wait_delegations",
                                delegation_ids=list(dict.fromkeys(_ID.findall(json.dumps(m)))),
                            )
                        ]
                    ),
                    Reply(
                        calls_from=lambda m: [
                            call("read_file", path=f"work/{first_id(m)}/table.txt")
                        ]
                    ),
                    Reply(
                        calls_from=lambda m: [
                            call("accept_delegation", delegation_id=d)
                            for d in list(dict.fromkeys(_ID.findall(json.dumps(m))))[:2]
                        ]
                    ),
                    Reply(FINAL),
                ],
                routed={
                    TASK_TEXT: returned_then_reopened()[:3],
                    TASK_B: [
                        Reply(calls=[call("write_file", path=TABLE, content="2\n")]),
                        Reply(calls=[call("submit_result", **FIRST)]),
                        Reply("Done."),
                    ],
                },
            )
            read_first, unread = tool_results(root)[-2:]
            verdicts.check("reads", "unread" not in read_first, read_first)
            verdicts.check("reads", "accepted unread" in str(unread.get("unread")), unread)
        case "platform-accepts-only-at-a-final-answer":
            # The root's turns end while its returned task awaits review: a limit, not
            # its final answer (the audit's decision 2: accept only at a final answer).
            _, root = await drive(
                harness,
                "root",
                [Reply(calls=[call("delegate", task=TASK_TEXT)]), waiting()],
                routed={TASK_TEXT: returned_then_reopened()[:3]},
                max_turns=2,
            )
            (delegation_id,) = started(root)
            found = delegation(harness, delegation_id)
            verdicts.check(
                "review", found.review == "awaiting", (found.review, found.review_reason)
            )
        case "next-run-is-told-of-platform-acceptance":
            _, root = await drive(
                harness,
                "root",
                [Reply(calls=[call("delegate", task=TASK_TEXT)]), waiting(), Reply(FINAL)],
                routed={TASK_TEXT: returned_then_reopened()[:3]},
            )
            (delegation_id,) = started(root)
            verdicts.check("review", delegation(harness, delegation_id).review == "accepted")
            _, later = await drive(harness, "later", [Reply("Noted.")])
            # Its own messages, after the view of the session's earlier runs (CM-29).
            told = [m for m in own(later) if m["role"] == "user" and delegation_id in str(m)]
            verdicts.check("told", bool(told), json.dumps(own(later))[:1500])
        case _:
            raise AssertionError(row.name)


@pytest.mark.parametrize("row", [pytest.param(r, id=r.name, marks=marks(r.known)) for r in RETURNS])
async def test_the_return_path(row: Walk, delegating: Harness) -> None:
    verdicts = Verdicts(row.known)
    await walk_return(row, delegating, verdicts)
    verdicts.settle()


# --- 4. Trees: depth two, a parent at its limit, a resumed tree ------------------------

TREES: Final[list[Walk]] = [
    Walk("depth-two"),
    Walk("a-parent-at-its-limit-wakes-for-the-user", {"wake": "DEL-V9"}),
    Walk("a-resumed-child-keeps-the-id-its-parent-knows", {"id": "DEL-V7"}),
]
GRANDCHILD: Final = "Tabulate the residues of the grandchild."


async def walk_tree(row: Walk, harness: Harness, verdicts: Verdicts, tmp_path: Path) -> None:
    match row.name:
        case "depth-two":
            configure(harness, depth=2)
            _, root = await drive(
                harness,
                "root",
                [
                    Reply(calls=[call("delegate", task=TASK_A, delegate=True)]),
                    waiting(),
                    Reply(FINAL),
                ],
                routed={
                    TASK_A: [
                        Reply(calls=[call("delegate", task=GRANDCHILD)]),
                        waiting(),
                        Reply(calls=[call("submit_result", **TASK_DONE)]),
                        Reply("Done."),
                    ],
                    GRANDCHILD: [Reply(calls=[call("submit_result", **TASK_DONE)]), Reply("Done.")],
                },
            )
            found = {d.brief.get("task"): d for d in records(harness)}
            child, grandchild = found.get(TASK_A), found.get(GRANDCHILD)
            verdicts.check("tree", child is not None and grandchild is not None, found)
            if child is not None and grandchild is not None:
                verdicts.check(
                    "tree", grandchild.parent == child.run, (grandchild.parent, child.run)
                )
                verdicts.check("tree", (child.ended, grandchild.ended) == ("returned", "returned"))
                for d in (child, grandchild):
                    state = work_state(harness, d.run)
                    verdicts.check("tree", state is not None and state[0] == "completed", state)
            verdicts.check("tree", (root.stopped, root.final_text) == ("answered", FINAL), root)
        case "a-parent-at-its-limit-wakes-for-the-user":
            free = asyncio.Event()

            async def held() -> None:
                await free.wait()

            harness.gateway.delegated = [
                Reply(calls=[STEP.calls[0]], before=held),
                Reply(calls=[call("submit_result", **TASK_DONE)]),
                Reply("Done."),
            ]
            store = ProjectStore(harness.state)
            state = RunState(store.control)
            state.post(PROJECT, SESSION, "m-1", "Tabulate it.", author="user")
            woke = False
            try:
                async with serving(
                    harness,
                    Reply(calls=[call("delegate", task=TASK_TEXT)]),
                    Reply(FINAL),
                    max_turns=2,
                ):
                    run = await awaiting_children(harness)
                    state.post(PROJECT, SESSION, "m-2", "Answer me now.", author="user")
                    try:
                        await eventually(lambda: state.ended(PROJECT, SESSION, run), timeout=10)
                        woke = True
                    except AssertionError:
                        woke = False
                    free.set()
                    await eventually(lambda: state.ended(PROJECT, SESSION, run), timeout=30)
            finally:
                free.set()
                store.close()
            # At its limit, a user's message is not left waiting on its children (I1).
            verdicts.check("wake", woke, "the parent at its limit did not wake for the user")
        case "a-resumed-child-keeps-the-id-its-parent-knows":
            await resumed_child(verdicts, tmp_path)
        case _:
            raise AssertionError(row.name)


async def resumed_child(verdicts: Verdicts, tmp_path: Path) -> None:
    """A root and its child, both interrupted; the user's Resume continues them, leaf
    first; the root's continuation waits on its child by the ID its history holds."""
    state, documents = stores(tmp_path)
    session = state.projects.open_session("p", "s", dedicated=False)
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision=state.projects.get("p").policy,
    )
    works = WorkRecords(state.projects.control)
    blueprints = Blueprints(state.projects.control)
    delegations = DelegationRecords(state.projects.control)
    blueprints.save(
        works.admit("p", "s", RUN),
        Blueprint(
            context=context,
            request=TypeAdapter(RunRequest).dump_python(
                RunRequest("p", "s", "Coordinate a check", alias="scripted"),
                mode="json",
                exclude={"history", "stop_by"},
            ),
        ),
    )
    child_run = "20261019T040001000000Z-child-1"
    original = new_id("dlg")
    blueprints.save(
        works.admit("p", "s", child_run, parent=RUN),
        Blueprint(
            context=context,
            request=TypeAdapter(RunRequest).dump_python(
                RunRequest(
                    "p",
                    "s",
                    "Check branch 1",
                    parent=RUN,
                    delegation=original,
                    tokens=100000,
                    alias="scripted",
                    root=RUN,
                ),
                mode="json",
                exclude={"history", "stop_by"},
            ),
        ),
    )
    delegations.start(
        "p",
        "s",
        Delegation(
            delegation_id=original,
            parent=RUN,
            run=child_run,
            kind="task",
            brief={"task": "Check branch 1"},
            started_at=datetime.now(UTC),
        ),
    )
    delegations.end("p", "s", original, Ending("interrupted", "interrupted"))
    delegations.configure("p", depth=1)
    works.interrupt("p", "s", RUN)
    asked = asyncio.Event()

    class RootModel(ScriptedModel):
        async def complete(self, messages: Any, tools: Any) -> ModelTurn:
            turn = await super().complete(messages, tools)
            asked.set()
            return turn

    class ChildModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])

        async def complete(self, messages: Any, tools: Any) -> ModelTurn:
            self.requests.append(list(messages))
            await asked.wait()  # it runs while its parent waits on it
            return submitting(messages, {"status": "complete", "summary": "Branch 1 holds."})

    waited = ToolCall("w", "wait_delegations", json.dumps({"delegation_ids": [original]}))
    root_model = RootModel([ModelTurn(None, (waited,)), ModelTurn("Integrated branch 1.")])
    harness = RunHarness(
        cast(WorkerClient, RecoveryWorker()),
        Role(root_model, "scripted"),
        delegated=Role(ChildModel(), "scripted"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    try:
        failed = await drive_concurrent(
            Recovery(harness, "p", "s", request), Sink(), asyncio.Event()
        )
        verdicts.check("resume", not failed, "the Resume failed")
        answered = [
            body(str(m.get("content")))
            for m in (root_model.requests[-1] if len(root_model.requests) > 1 else [])
            if m.get("role") == "tool"
        ]
        # Its wait on the ID it knows returns the continued child's outcome.
        waited_on = json.loads(answered[0]) if answered else {}
        if isinstance(waited_on, dict) and "tool_result" in waited_on:
            waited_on = json.loads(cast(Json, waited_on)["tool_result"])
        outcome = cast(Json, (cast(Json, waited_on).get("delegations") or [{}])[0])
        verdicts.check("id", outcome.get("ended") == "returned", waited_on)
    finally:
        documents.close()
        state.projects.close()


@pytest.mark.parametrize("row", [pytest.param(r, id=r.name, marks=marks(r.known)) for r in TREES])
async def test_a_tree(row: Walk, delegating: Harness, tmp_path: Path) -> None:
    verdicts = Verdicts(row.known)
    await walk_tree(row, delegating, verdicts, tmp_path / "resumed")
    verdicts.settle()


# --- 5. Instructions reaching children -------------------------------------------------

INSTRUCTIONS: Final[list[Walk]] = [
    Walk("a-user-constraint-reaches-a-running-task", {"reached": "DEL-V10"}),
    Walk("a-user-constraint-reaches-a-running-check", {"reached": "DEL-V10"}),
    Walk("a-user-plan-edit-reaches-a-running-task", {"reached": "PR-V7"}),
]
CONSTRAINT: Final = "Report every residue in hexadecimal."
PLAN_MARK: Final = "Inspect the recorded messages"
"""A text of the user's plan (``loop_harness.PLAN``) that no brief holds."""


@pytest.mark.parametrize(
    "row", [pytest.param(r, id=r.name, marks=marks(r.known)) for r in INSTRUCTIONS]
)
async def test_an_instruction_reaches_a_child(row: Walk, delegating: Harness) -> None:
    """The user's instruction, recorded while the child works, reaches the child's next
    model call as content (the main prompt: "so that later runs and children apply it")."""
    release = asyncio.Event()

    async def held() -> None:
        await release.wait()

    async def instruct() -> None:
        await until_running(delegating)()
        if "plan" in row.name:
            delegating.user_plan()
        else:
            documents = delegating.documents()
            try:
                decision = {
                    "type": "decision",
                    "status": "in_effect",
                    "title": "Hexadecimal",
                    "statement": CONSTRAINT,
                }
                Ledger(documents).change(
                    PROJECT,
                    {"operation_id": "u-c", "reason": "Asked.", "changes": [decision]},
                    USER,
                )
                # A standing constraint of the user's, as a task started now is given.
                given = constraints_block(Digest(Ledger(documents)).constraints(PROJECT, SESSION))
                assert CONSTRAINT in given, given
            finally:
                documents.close()
        release.set()

    check = "check" in row.name
    result = CHECK_RESULT if check else TASK_DONE
    child = [
        Reply(calls=[STEP.calls[0]], before=held),
        Reply(calls=[call("submit_result", **result)]),
        Reply("Done."),
    ]
    script = [
        Reply(calls=[CHECK if check else call("delegate", task=TASK_TEXT)]),
        Reply(calls=[STEP.calls[0]], before=instruct),
        waiting(),
        Reply(FINAL),
    ]
    await drive(delegating, "root", script, delegated=child)
    later = json.dumps(delegating.gateway.children[1:])
    mark = PLAN_MARK if "plan" in row.name else CONSTRAINT
    verdicts = Verdicts(row.known)
    verdicts.check("reached", mark in later, later[-1500:])
    verdicts.settle()
