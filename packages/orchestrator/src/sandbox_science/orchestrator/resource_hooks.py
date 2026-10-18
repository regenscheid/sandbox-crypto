# SPDX-License-Identifier: MIT
"""Resource check-ins at model safe points, with no extra decision model calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.continuity.records import Ownership
from sandbox_science.orchestrator.continuity.resources import (
    ResourceDecisionRequest,
    ResourceRequest,
    ResourceRequests,
)
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.worker_client import dump

SOURCE = "resources"
SPEC = ToolSpec(
    "resource_decide",
    "Decide one direct child resource check-in after reading its checkpoint and progress. "
    "continue keeps its current allowance; redirect includes corrected instructions; report "
    "requests its final checkpoint report; pause checkpoints before its next model call. "
    "extend_turns adds up to 300 cumulative turns within project policy and its existing "
    "token grant. Tokens require explicit USER approval of exactly 10,000,000 more: "
    "ask the user at the root, or message_parent to escalate. No action replays a tool.",
    ResourceDecisionRequest.model_json_schema(),
)


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
        clock: Clock = SYSTEM_CLOCK,
    ):
        self.store, self.owner, self.clock = store, owner, clock
        self.turns, self.tokens, self.delegated = turns, tokens, delegated
        self.specs = (SPEC,)
        self.seen: frozenset[str] = frozenset()

    async def call(
        self, name: str, args: dict[str, Any], operation: str
    ) -> tuple[str, dict[str, Any]]:
        decision = await threaded(
            self.store.decide,
            self.owner,
            ResourceDecisionRequest.model_validate(args),
            operation=operation,
        )
        return dump(decision.model_dump(mode="json")), {
            "resource_decision": decision.model_dump(mode="json")
        }

    async def turn_limit(self, run: RunView) -> int | None:
        total, revision, token_total, token_revision, prior = await threaded(
            self.store.limits, self.owner, turns=self.turns, tokens=self.tokens
        )
        used = prior + run.turns
        await threaded(
            self.store.observe, self.owner, "turns", used=used, total=total, revision=revision
        )
        if self.tokens is not None:
            used_tokens = await threaded(self.store.used_tokens, self.owner)
            await threaded(
                self.store.observe,
                self.owner,
                "tokens",
                used=used_tokens,
                total=token_total,
                revision=token_revision,
            )
        return max(0, total - prior) if self.delegated else None

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        if stopped in ("run_budget", "turn_limit"):
            total, revision, tokens, token_revision, prior = await threaded(
                self.store.limits, self.owner, turns=self.turns, tokens=self.tokens
            )
            resource = "tokens" if stopped == "run_budget" and self.tokens is not None else "turns"
            used = (
                await threaded(self.store.used_tokens, self.owner)
                if resource == "tokens"
                else prior + run.turns
            )
            await threaded(
                self.store.observe,
                self.owner,
                resource,
                used=used,
                total=tokens if resource == "tokens" else total,
                revision=token_revision if resource == "tokens" else revision,
                force=True,
            )
        return None

    async def halt(self, run: RunView) -> str | None:
        decisions = await threaded(self.store.decisions, self.owner)
        return "resource_pause" if any(d.request.action == "pause" for d in decisions) else None

    async def last_call(self, run: RunView) -> str | None:
        decisions = await threaded(self.store.decisions, self.owner)
        return "resource_report" if any(d.request.action == "report" for d in decisions) else None

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        if point != "turn":
            return ()
        requests = await threaded(
            self.store.pending, self.owner, seen=frozenset(run.offered.get(self.name, ()))
        )
        notices = [self._request_notice(r) for r in requests]
        for decision in await threaded(self.store.decisions, self.owner):
            identity = "decision-" + decision.request.request_id
            if identity not in run.offered.get(self.name, ()):
                notices.append(
                    Notice(
                        self.name,
                        (identity,),
                        "Your parent recorded a resource decision. This does not change security "
                        "policy, spending caps, deadlines or your token grant.",
                        decision.model_dump(mode="json"),
                    )
                )
        return tuple(notices)

    @staticmethod
    def _request_notice(request: ResourceRequest) -> Notice:
        return Notice(
            SOURCE,
            (request.request_id,),
            "Research resource check-in. Read the checkpoint and assess actual progress; "
            "this is not scientific completion. A direct parent may use resource_decide. "
            "Token increases require an explicit USER decision. "
            "Save results before the allowance ends.",
            request.model_dump(mode="json"),
        )

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        decisions = await threaded(self.store.decisions, self.owner)
        for action in ("pause", "report"):
            if any(d.request.action == action for d in decisions):
                return Closing("stop", stopped=f"resource_{action}", retain_answer=True)
        # An agent's own warning never spends another model call just to respond
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

    async def model_returned(self, run: RunView) -> None:
        self.seen = frozenset(run.offered.get(self.name, ()))

    async def arrived(self) -> None:
        while not await threaded(
            self.store.pending, self.owner, seen=self.seen, children_only=True
        ):
            await self.clock.sleep(1)

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
