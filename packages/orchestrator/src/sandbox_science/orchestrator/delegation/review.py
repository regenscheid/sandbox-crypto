# SPDX-License-Identifier: MIT
"""Closing a returned task (plans-and-delegation spec, PD-46).

A task that returns awaits its parent's review: its archive, its kernels and its child
environment, if it has one, are kept. It closes when its parent accepts it
(``accept_delegation``) or cancels it, or when the platform accepts it for the parent:
as the parent's run ends, as the session closes, or past ``review_max``. Closing it is
one transaction that only a task still awaiting review passes
(``DelegationRecords.accept``), then what it held goes: its child environment hands its
files back into the session's ``work/<delegation_id>/`` and is released through the
child environment code (CE-4, CE-6), or its kernels in the session's environment stop.
A process that stops between the two leaves the environment ``closing``, which the next
run's start finishes (``finish``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from sandbox_science.contracts.child_environments import RetainedFile
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import CONFLICT

from .environments import ChildPlacement
from .records import Closed, Delegation, DelegationRecords, ReviewReason, held_plan

_log = logging.getLogger(__name__)

PLATFORM = "platform"
"""Who closes a returned task the parent left unreviewed."""


@dataclass
class Closer:
    """Closes a session's returned tasks and releases what they held."""

    records: DelegationRecords
    placement: ChildPlacement | None = None
    """The session's child environments; without it, a child environment is left
    ``closing`` for a run that has them, or the session's close, to release."""
    release_kernel: Callable[[str], Awaitable[object]] | None = None
    """Stops the kernels of a run in the session's environment, by its run ID."""

    async def close(
        self,
        project: str,
        session: str,
        delegation_id: str,
        *,
        by: str,
        reason: ReviewReason,
        note: str | None = None,
        review: Literal["accepted", "cancelled"] = "accepted",
        parent: str | None = None,
        work: str | None = None,
    ) -> Delegation:
        """Accept or cancel one returned task, then release what it held; refused, with a
        ``ControlError``, for a delegation that does not await review, or, with
        ``parent``, is not that run's child."""
        closed = await threaded(
            self.records.accept,
            project,
            session,
            delegation_id,
            by=by,
            reason=reason,
            note=note,
            review=review,
            parent=parent,
            work=work,
        )
        return await self.release(project, session, closed)

    async def close_all(
        self,
        project: str,
        session: str,
        *,
        reason: ReviewReason,
        parent: str | None = None,
        work: str | None = None,
        returned_before: datetime | None = None,
    ) -> list[Delegation]:
        """Accept, for the parent, the returned tasks that await review: ``parent``'s, or
        the session's; with ``returned_before``, those that returned before it. One a
        message reopened, or its parent closed, meanwhile is left as it is."""
        found = await threaded(
            self.records.awaiting,
            project,
            session,
            parent=parent,
            work=work,
            returned_before=returned_before,
        )
        closed: list[Delegation] = []
        for delegation in found:
            try:
                closed.append(
                    await self.close(
                        project, session, delegation.delegation_id, by=PLATFORM, reason=reason
                    )
                )
            except ControlError as error:
                if error.kind != CONFLICT:
                    raise
        return closed

    async def release(self, project: str, session: str, closed: Closed) -> Delegation:
        """Release what a closed task held: its child environment, after its hand-back,
        or its kernels in the session's environment. A failure is logged and leaves the
        rest to the sweeps: the environment's, and the kernel check's."""
        delegation = closed.delegation
        if delegation.environment_id is not None and closed.plan is not None:
            return await self._environment(project, session, delegation, closed.plan)
        release = self.release_kernel
        if closed.kernel is not None and delegation.environment_id is None and release:
            try:
                await release(closed.kernel)
            except Exception:
                _log.warning(
                    "the kernels of delegation %s did not stop",
                    delegation.delegation_id,
                    exc_info=True,
                )
        return delegation

    async def finish(self, project: str, session: str) -> None:
        """Release the child environments closed tasks left ``closing``: the process
        stopped between a task's acceptance and its environment's account."""
        for delegation in await threaded(self.records.closing, project, session):
            plan = held_plan(delegation.environment)
            if plan is not None:
                await self._environment(project, session, delegation, plan)

    async def _environment(
        self,
        project: str,
        session: str,
        delegation: Delegation,
        plan: tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]],
    ) -> Delegation:
        environment_id = delegation.environment_id
        if self.placement is None or environment_id is None:
            return delegation
        try:
            # With what the environment's other delegations left planned: a Resume continues
            # a reopened task in it under a delegation of its own (CE-12, PD-46).
            (files, named), others = await threaded(
                self.records.shared_plan,
                project,
                session,
                delegation.delegation_id,
                environment_id,
                plan,
            )
            retained = tuple(RetainedFile.model_validate(item) for item in named)
            account = await self.placement.close(
                environment_id, delegation.delegation_id, (files, retained)
            )
            await threaded(
                self.records.handed_back,
                project,
                session,
                others,
                account,
                delegation.delegation_id,
            )
        except Exception:
            _log.warning(
                "the child environment of delegation %s was not released",
                delegation.delegation_id,
                exc_info=True,
            )
            return delegation
        if delegation.environment is not None and "restarted" in delegation.environment:
            # A Resume's restart in a new environment stays told (CE-12).
            account = {**account, "restarted": delegation.environment["restarted"]}
        return await threaded(
            self.records.closed_environment,
            project,
            session,
            delegation.delegation_id,
            account,
        )
