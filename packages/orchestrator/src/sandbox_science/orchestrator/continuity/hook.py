# SPDX-License-Identifier: MIT
"""Attempt safe points and parent completion follow durable work ownership."""

from __future__ import annotations

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.hooks import Closing, Effect, Refusal, RunView, TurnHook

from .records import Ownership, WorkRecords, check_owner


class WorkHook(TurnHook):
    """No model/tool admission after ownership loss; incomplete children hold completion."""

    name = "continuity"

    def __init__(self, records: WorkRecords, owner: Ownership) -> None:
        self.records, self.owner = records, owner

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
        if await threaded(self.records.unfinished, owner.project, owner.session, owner.work):
            return Closing("stop", stopped="unfinished_children")
        return Closing()

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        owner = self.owner
        if stopped == "answered" and await threaded(
            self.records.unfinished, owner.project, owner.session, owner.work
        ):
            return "unfinished_children"
        return None
