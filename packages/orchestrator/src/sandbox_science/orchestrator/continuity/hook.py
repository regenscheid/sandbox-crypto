# SPDX-License-Identifier: MIT
"""Attempt safe points and parent completion follow durable work ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.hooks import (
    Closing,
    Effect,
    Notice,
    Point,
    Refusal,
    RunView,
    TurnHook,
)

from .records import Ownership, WorkRecords, check_owner


class WorkHook(TurnHook):
    """No model/tool admission after ownership loss; incomplete children hold completion."""

    name = "continuity"

    def __init__(
        self,
        records: WorkRecords,
        owner: Ownership,
        *,
        predecessor: str | None = None,
        resume_source: Literal["operator", "automatic"] | None = None,
        wait_children: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.records, self.owner = records, owner
        self.predecessor, self.resume_source = predecessor, resume_source
        self.wait_children = wait_children
        self.wake: Callable[[], Awaitable[str]] | None = None
        # Recovery-owned children live outside this attempt's Delegations hook. Ask
        # this guard first at closing so their durable handbacks are present when the
        # Delegations hook runs later in the same closing pass.
        self.veto = wait_children is not None

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        """Make trusted continuation provenance observable before its first call."""
        predecessor = self.predecessor
        if point != "turn" or predecessor is None or predecessor in run.offered.get(self.name, ()):
            return ()
        source = self.resume_source
        provenance = (
            "An operator explicitly requested the Resume that created this attempt. "
            "Standing instructions conditioned on an explicit Resume and a new attempt may "
            "apply; evaluate every other condition exactly. "
            if source == "operator"
            else "Automatic recovery created this attempt; it is not proof of a new operator "
            "authorization. Do not activate an instruction that requires an explicit operator "
            "Resume. "
            if source == "automatic"
            else "The recovery source is unavailable, so do not infer an explicit operator "
            "authorization from this notice. "
        )
        return (
            Notice(
                self.name,
                (predecessor,),
                "This is a new attempt continuing the same durable research work. The previous "
                "attempt ended before this attempt began. "
                + provenance
                + "Reconcile interrupted effects by their original identities, continue from "
                "durable results, and do not repeat completed work. The continuation identity is:",
                {
                    "work": self.owner.work,
                    "attempt": self.owner.run,
                    "predecessor": predecessor,
                    "epoch": self.owner.epoch,
                    "resume_source": source,
                },
            ),
        )

    def _reason(self) -> str | None:
        owner = self.owner
        with self.records.control.read() as tx:
            check_owner(tx.sql, owner)
        work = self.records.get(owner.project, owner.session, owner.work)
        if work.state == "stopped":
            return "cancelled"
        if work.state in ("paused", "blocked"):
            return "recovery_blocked"
        return None

    async def halt(self, run: RunView) -> str | None:
        return await threaded(self._reason)

    async def gate(self, effect: Effect) -> Refusal | None:
        try:
            reason = await threaded(self._reason)
        except ControlError:
            reason = "research ownership changed"
        return Refusal("policy_denied", reason) if reason else None

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        owner = self.owner
        unfinished = await threaded(
            self.records.unfinished, owner.project, owner.session, owner.work
        )
        if unfinished and self.wait_children is not None and run.can_respond and run.halted is None:
            settled = await self._recovery_progress()
            if settled is None:
                # The parent or user sent new input while a recovered child still ran.
                # Retry reaches the next turn safe point, where AgentMailbox offers it.
                return Closing("retry", stopped="unfinished_children")
            if settled:
                # Terminal child work can still be unfinished until this response
                # consumes its delegation handback. Let Delegations offer it below.
                return Closing()
        if unfinished:
            return Closing("stop", stopped="unfinished_children")
        return Closing()

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        owner = self.owner
        if stopped == "answered":
            unfinished = await threaded(
                self.records.unfinished, owner.project, owner.session, owner.work
            )
            if unfinished and self.wait_children is not None:
                await self.wait_children()
                unfinished = await threaded(
                    self.records.unfinished, owner.project, owner.session, owner.work
                )
            if unfinished:
                return "unfinished_children"
        return None

    async def _recovery_progress(self) -> bool | None:
        """Recovered children settled/blocked, or ``None`` when a message wakes them."""
        if self.wait_children is None:
            return False
        children = asyncio.ensure_future(self.wait_children())
        if self.wake is None:
            return await children
        message = asyncio.ensure_future(self.wake())
        waits = (children, message)
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if children in done:
                return children.result()
            return None
        finally:
            for wait in waits:
                if not wait.done():
                    wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
