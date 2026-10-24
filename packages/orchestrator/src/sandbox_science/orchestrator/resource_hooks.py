# SPDX-License-Identifier: MIT
"""Soft caps and loop heuristics as check-ins at model safe points (D102).

Before each model call the hook reads the work's soft caps (tokens, turns, active time,
cost) and recorded use. At a cap, or when a loop heuristic trips, the work checks in:

- a child records a request with a bounded state summary, which reaches its parent as a
  notice and wakes the parent's wait, then waits without model calls for the decision:
  ``extend`` by one bounded step, ``redirect`` (the reason is the new guidance, with one
  step), or ``stop`` (its next call is its report). A parent that does not answer
  within ``wait_seconds`` gets the root's rule applied for it;
- the root checks itself: one step while it keeps producing evidence, with a notice to
  the user and to itself to compare its progress with its plan. After
  ``max_idle_extensions`` steps in a row without evidence it pauses and the user is asked.

The loop heuristics are counters over what the run already records: the same tool with
the same arguments, the same error, turns without new evidence, and the same answer
text. No heuristic stops a run; the runaway guard (RS-6f) still does, later.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Final

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator.accounting import account
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.continuity.records import Ownership
from sandbox_science.orchestrator.continuity.resource_policy import STOPPING
from sandbox_science.orchestrator.continuity.resources import (
    ResourceDecision,
    ResourceDecisionRequest,
    ResourceRequest,
    ResourceRequests,
    SafePoint,
)
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import dump

SOURCE = "resources"
SAME_CALL: Final = 8
"""Calls of one tool with the same arguments in an attempt that ask for a check-in."""
SAME_ERROR: Final = 6
"""Failures with the same error, its kind and its message, in an attempt that ask for a
check-in."""
IDLE_TURNS: Final = 30
"""Model calls in a row with no new evidence, file or plan change, nor a new call."""
SAME_OUTPUT: Final = 4
"""Answers in a row with the same text: a child whose output stopped changing."""
WAIT_SECONDS: Final = 1800.0
"""How long a child waits for its parent's decision before the root's rule decides."""
POLLS: Final = frozenset(
    {
        "wait_jobs",
        "job_status",
        "list_jobs",
        "wait_delegations",
        "wait_agent_message",
        "delegation_status",
        "provision_status",
        "track_progress",
        "resource_usage",
    }
)
"""Tools whose same answer is time passing (the runaway guard's ``POLLS``)."""
SPEC = ToolSpec(
    "resource_decide",
    "Answer one direct child's check-in after reading its summary: it reached a soft cap "
    "(tokens, turns, active time or cost) or a loop heuristic, and waits for you. extend "
    "gives one bounded step (300 turns, 3600 active seconds, 20,000,000 tokens, 25.00 of "
    "cost or 1 GiB of retained artifacts) or lets a loop check-in go on; redirect sends "
    "the reason as corrected instructions, with one step; stop makes its next call its "
    "report, then it ends. After repeated extensions without new evidence, extend is "
    "refused: stop or redirect. Hard retention capacity, worker disk and execution "
    "deadlines cannot be extended. Ask the user (at the root) or message_parent only for "
    "a decision you cannot make. No action replays a tool.",
    ResourceDecisionRequest.model_json_schema(),
)
USAGE_SPEC = ToolSpec(
    "resource_usage",
    "Read the recorded model spend and tokens of this session and its runs, by role "
    "(main, delegated, reviewer, watcher), and the project's total. Accounting only: "
    "spending is enforced by the model gateway's budget, not by this tool.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()


class Resources(TurnHook):
    name = SOURCE
    veto = True

    def __init__(
        self,
        store: ResourceRequests,
        owner: Ownership,
        *,
        turns: int,
        tokens: int | None,
        delegated: bool,
        inventory: ArtifactInventory | None = None,
        clock: Clock = SYSTEM_CLOCK,
        poll_seconds: float = 1.0,
        wait_seconds: float = WAIT_SECONDS,
    ):
        self.store, self.owner, self.clock = store, owner, clock
        self.turns, self.tokens, self.delegated = turns, tokens, delegated
        self.specs = (SPEC, USAGE_SPEC)
        self.inventory = inventory
        self.started = clock.monotonic()
        self.waited = 0.0
        """Time spent waiting for a decision, which is not active research time."""
        self.poll_seconds, self.wait_seconds = poll_seconds, wait_seconds
        self.calls: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.evidence: set[str] = set()
        self.marker: tuple[int, int] | None = None
        self.idle = 0
        self.output: str | None = None
        self.same_output = 0
        self.tripped: tuple[str, str, int, int] | None = None
        """A loop heuristic's rule, detail, count and threshold, until its check-in."""
        self.loops = 0
        self.point: SafePoint | None = None
        """The last safe point's reading, which the turn limit reuses."""

    # --- Tools ------------------------------------------------------------------------

    async def call(
        self, name: str, args: dict[str, Any], operation: str
    ) -> tuple[str, dict[str, Any]]:
        if name == USAGE_SPEC.name:
            found = await threaded(
                account, RunState(self.store.control), self.owner.project, self.owner.session
            )
            shown = found.model_dump(mode="json")
            return dump(shown), {"resource_usage": {"session": self.owner.session}}
        decision = await threaded(
            self.store.decide,
            self.owner,
            ResourceDecisionRequest.model_validate(args),
            operation=operation,
        )
        return dump(decision.model_dump(mode="json")), {
            "resource_decision": decision.model_dump(mode="json")
        }

    # --- Loop heuristics --------------------------------------------------------------

    def _trip(self, rule: str, detail: str, count: int, threshold: int) -> None:
        if self.tripped is None:
            self.tripped = (rule, detail, count, threshold)

    def _reset(self) -> None:
        self.calls.clear()
        self.errors.clear()
        self.idle, self.same_output, self.tripped = 0, 0, None

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        current = run.tool_records[-1] if run.tool_records else {}
        tool, arguments = str(current.get("tool", "")), str(current.get("arguments", ""))
        error = record.get("error")
        if error is not None:
            # The record keeps only the error's kind; its message is in the result the
            # conversation just received, which the loop appends before observers run.
            messages = run.context.messages
            said = messages[-1].get("content") if messages else None
            key = _digest(json.dumps(record, sort_keys=True, default=str), str(said))
            self.errors[key] += 1
            if self.errors[key] >= SAME_ERROR:
                self._trip(
                    "same_error",
                    f"{self.errors[key]} calls failed with the same error ({error})",
                    self.errors[key],
                    SAME_ERROR,
                )
            return
        if tool in POLLS:
            return
        key = _digest(tool, arguments)
        self.calls[key] += 1
        self.evidence.add(key)
        if self.calls[key] >= SAME_CALL:
            self._trip(
                "same_call",
                f"{self.calls[key]} calls of {tool} with the same arguments",
                self.calls[key],
                SAME_CALL,
            )

    async def model_returned(self, run: RunView) -> None:
        messages = run.context.messages
        last = messages[-1] if messages else None
        text = last.get("content") if last is not None and last.get("role") == "assistant" else None
        if not isinstance(text, str) or not text.strip():
            return
        digest = _digest(text.strip())
        self.same_output = self.same_output + 1 if digest == self.output else 1
        self.output = digest
        if self.same_output >= SAME_OUTPUT:
            self._trip(
                "same_output",
                f"{self.same_output} answers in a row with the same text",
                self.same_output,
                SAME_OUTPUT,
            )

    def _idle(self, progress: int) -> None:
        marker = (progress, len(self.evidence))
        self.idle = self.idle + 1 if marker == self.marker else 0
        self.marker = marker
        if self.idle >= IDLE_TURNS:
            self._trip(
                "no_progress",
                f"{self.idle} model calls without new evidence, files, plan changes or calls",
                self.idle,
                IDLE_TURNS,
            )

    # --- Safe points ------------------------------------------------------------------

    async def _point(self, run: RunView) -> SafePoint:
        seconds = max(0, int(self.clock.monotonic() - self.started - self.waited))
        self.point = await threaded(
            self.store.safe_point, self.owner, seconds=seconds, turns=self.turns, tokens=self.tokens
        )
        return self.point

    async def _capacity(self, point: SafePoint) -> str | None:
        """Retained bytes and retention capacity keep their earlier stops."""
        stopped = None
        for resource, used, total, revision in point.storage:
            await threaded(
                self.store.observe,
                self.owner,
                resource,
                used=used,
                total=total,
                revision=revision,
                force=used >= total,
            )
            if used >= total:
                stopped = "resource_storage"
        if self.inventory is not None:
            for pressure in await threaded(self.inventory.pressure, self.owner.project):
                await threaded(
                    self.store.observe,
                    self.owner,
                    pressure.resource,
                    used=pressure.used,
                    total=pressure.total,
                    revision=pressure.total,
                    scope=pressure.scope,
                    renewable=False,
                    force=pressure.used >= pressure.total,
                )
                if pressure.used >= pressure.total:
                    stopped = "resource_retention"
        return stopped

    def _recent(self, run: RunView) -> tuple[str, ...]:
        records = run.tool_records
        start = max(0, len(records) - 8)
        recent: list[str] = []
        for index in range(start, len(records)):
            record = records[index]
            failed = record.get("error")
            recent.append(
                f"{record.get('tool', '?')}: " + (f"failed ({failed})" if failed else "ok")
            )
        return tuple(recent)

    def _answer(self, run: RunView) -> str | None:
        for message in reversed(run.context.messages[-6:]):
            text = message.get("content")
            if message.get("role") == "assistant" and isinstance(text, str) and text.strip():
                return text
        return None

    async def _check_in(self, run: RunView, point: SafePoint) -> ResourceRequest | None:
        used = {**point.used, "turns": point.prior_turns + run.turns}
        due = next(
            (
                (resource, used[resource], total, revision, None, None)
                for resource, total, revision in point.caps
                if used.get(resource, 0) >= total
            ),
            None,
        )
        if due is None and self.tripped is not None:
            rule, detail, count, threshold = self.tripped
            due = ("loop", count, threshold, self.loops, rule, detail)
        if due is None:
            return None
        resource, value, total, revision, rule, detail = due
        summary = await threaded(
            self.store.summary,
            self.owner,
            recent=self._recent(run),
            answer=self._answer(run),
            rule=rule,
            detail=detail,
            used=used,
            turns=used["turns"],
        )
        return await threaded(
            self.store.observe,
            self.owner,
            resource,
            used=value,
            total=total,
            revision=revision,
            force=True,
            summary=summary,
        )

    async def _decided(self, request: ResourceRequest) -> ResourceDecision | str:
        """Wait for the parent's decision without model calls; ``"ended"`` once Stop or
        an ending reached the work."""
        started = self.clock.monotonic()
        try:
            while True:
                found = await threaded(self.store.awaited, self.owner, request.request_id)
                if found is not None:
                    return found
                if self.clock.monotonic() - started >= self.wait_seconds:
                    decision = await threaded(
                        self.store.self_check, self.owner, request, ask_user=False
                    )
                    if decision is not None:
                        return decision
                await self.clock.sleep(self.poll_seconds)
        finally:
            self.waited += self.clock.monotonic() - started

    async def turn_limit(self, run: RunView) -> int | None:
        """A child's soft turn cap checks in, and only its cumulative ceiling is a limit; a
        root keeps its run's ``max_turns`` unless an extension was recorded (RS-6f)."""
        point = self.point or await self._point(run)
        if self.delegated:
            return max(0, point.hard_turns - point.prior_turns)
        total, revision = point.turns
        if revision > 0 or await threaded(self.store.root_enabled, self.owner):
            return max(0, total - point.prior_turns)
        return None

    async def halt(self, run: RunView) -> str | None:
        decisions = await threaded(self.store.decisions, self.owner)
        if any(d.request.action == "pause" for d in decisions):
            return "resource_pause"
        if any(d.request.action in STOPPING for d in decisions):
            return None  # its next call is its report round (``last_call``)
        point = await self._point(run)
        if (stopped := await self._capacity(point)) is not None:
            return stopped
        self._idle(point.progress)
        request = await self._check_in(run, point)
        if request is None:
            return None
        if request.resource == "loop":
            self.loops += 1
        self._reset()
        if not self.delegated:
            decision = await threaded(self.store.self_check, self.owner, request, ask_user=True)
            if decision is None:
                await threaded(
                    self.store.notify,
                    self.owner,
                    request,
                    "The research paused at a soft cap after repeated extensions without new "
                    "evidence; extend it in the resource panel and Resume, or stop it.",
                )
                return "resource_pause"
            await threaded(
                self.store.notify,
                self.owner,
                request,
                f"The research reached a soft {request.resource} check-in and continues "
                f"(cap now {decision.total:,}); nothing was stopped.",
            )
            return None
        found = await self._decided(request)
        if found == "ended":
            return "cancelled"
        if isinstance(found, ResourceDecision) and found.request.action == "pause":
            return "resource_pause"
        return None

    async def last_call(self, run: RunView) -> str | None:
        decisions = await threaded(self.store.decisions, self.owner)
        return "resource_report" if any(d.request.action in STOPPING for d in decisions) else None

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        await self._point(run)  # the attempt's last active-time sample
        return None

    # --- Notices ----------------------------------------------------------------------

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        if point != "turn":
            return ()
        requests = await threaded(
            self.store.pending, self.owner, seen=frozenset(run.offered.get(self.name, ()))
        )
        notices = [self._request_notice(r) for r in requests if r.work != self.owner.work]
        for decision in await threaded(self.store.decisions, self.owner):
            identity = "decision-" + decision.request.request_id
            if identity not in run.offered.get(self.name, ()):
                notices.append(self._decision_notice(identity, decision))
        return tuple(notices)

    def _decision_notice(self, identity: str, decision: ResourceDecision) -> Notice:
        if decision.automatic and not self.delegated:
            text = (
                "You reached a soft cap: a check-in, not a stop. Compare your progress with "
                "the session plan; continue only with work that advances it, revise the plan, "
                "or finish with a report. The cap was raised by one step and the user was "
                "notified."
            )
        else:
            text = (
                "Your check-in was decided. A redirect's reason is your parent's corrected "
                "instruction; stop means your next answer is your report. This changes no "
                "security policy or deadline."
            )
        return Notice(self.name, (identity,), text, decision.model_dump(mode="json"))

    @staticmethod
    def _request_notice(request: ResourceRequest) -> Notice:
        return Notice(
            SOURCE,
            (request.request_id,),
            "A child checked in and waits for you: it reached a soft cap or a loop "
            "heuristic. Read its summary and answer with resource_decide: extend (one "
            "bounded step), redirect (the reason is its corrected instruction) or stop (it "
            "reports and ends). This is not scientific completion.",
            request.model_dump(mode="json"),
        )

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        decisions = await threaded(self.store.decisions, self.owner)
        if any(d.request.action == "pause" for d in decisions):
            return Closing("stop", stopped="resource_pause", retain_answer=True)
        if any(d.request.action in STOPPING for d in decisions):
            return Closing("stop", stopped="resource_report", retain_answer=True)
        # An agent's own check-in never spends another model call just to respond
        # to its final answer. Only an unfinished child's request needs routing.
        requests = (
            await threaded(
                self.store.pending,
                self.owner,
                seen=frozenset(run.offered.get(self.name, ())),
                children_only=True,
            )
            if run.turns_left
            else []
        )
        notices = tuple(self._request_notice(r) for r in requests if r.parent == self.owner.work)
        return Closing("continue", notices) if notices else Closing()

    async def arrived(self) -> None:
        """Until a child's check-in waits for this run, even one already shown: a child
        blocked on its parent while the parent waits on it would wait for nothing."""
        while not await threaded(self.store.pending, self.owner, children_only=True):
            await self.clock.sleep(self.poll_seconds)

    async def wake(self, other: Callable[[], Awaitable[str]]) -> str:
        waits: dict[asyncio.Future[None] | asyncio.Future[str], str | None] = {
            asyncio.create_task(self.arrived()): "resource_request",
            asyncio.ensure_future(other()): None,
        }
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            selected = next(iter(done))
            result = await selected
            return waits[selected] or str(result)
        finally:
            for wait in waits:
                wait.cancel()
            pending: list[Awaitable[object]] = list(waits)
            await asyncio.gather(*pending, return_exceptions=True)
