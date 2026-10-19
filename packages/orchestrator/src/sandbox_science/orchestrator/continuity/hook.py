# SPDX-License-Identifier: MIT
"""Attempt safe points and parent completion follow durable work ownership."""

from __future__ import annotations

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
    ) -> None:
        self.records, self.owner = records, owner
        self.predecessor, self.resume_source = predecessor, resume_source

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
