# SPDX-License-Identifier: MIT
"""Trusted recovery bookkeeping; the configured runtime selects ready branches."""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from sandbox_science.contracts import new_id
from sandbox_science.contracts.context import ExecutionAuthority
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.chat import session_lease
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import CONFLICT, NOT_FOUND
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.environment_claim import environment_claim
from sandbox_science.orchestrator.harness import Admitted, RunRequest, save, tier_in_effect
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import run_id_of, run_name
from sandbox_science.orchestrator.run_inputs import RunInputs

from .blueprints import Blueprints
from .child_settlement import ChildSettlement
from .kernels import KernelJournal
from .plans import Plans
from .reconcile import Reconciler
from .records import Ownership, Work, WorkRecords

if TYPE_CHECKING:
    from sandbox_science.orchestrator.harness import Harness, RunSink


class Recovery:
    """One explicit Resume holds one session lease until all selected branches settle."""

    def __init__(self, harness: Harness, project: str, session: str, request: str) -> None:
        if harness.stores is None:
            raise ValueError("Resume requires durable stores")
        self.harness, self.stores = harness, harness.stores
        self.project, self.session, self.request = project, session, request
        self.records = WorkRecords(self.stores.projects.control)
        self.plans = Plans(self.stores.projects.control, project, session, request)
        self.root: str | None = None
        self.epoch: int | None = None
        self.started_at = datetime.now(UTC)
        self.run_name = run_name(self.started_at, new_id("run"))
        self.started = False
        self.active: dict[str, asyncio.Task[object]] = {}
        self.wake = asyncio.Event()
        self.stop: asyncio.Event | None = None
        self.sink: RunSink | None = None

    async def run(self, sink: RunSink, stop: asyncio.Event) -> bool:
        """No branch/model scheduling here: resume_next/resume_branch drive this holder."""
        self.sink, self.stop = sink, stop
        started, name = self.started_at, self.run_name
        queued = await threaded(self.records.queued)
        root = next(
            (r for p, s, q, r in queued if (p, s, q) == (self.project, self.session, self.request)),
            None,
        )
        if root is None:
            return False
        try:
            blueprint = await threaded(
                Blueprints(self.stores.projects.control).get, self.project, self.session, root
            )
        except ControlError as exc:
            await threaded(
                self.records.reject_queued, self.project, self.session, self.request, str(exc)
            )
            return True
        error: str | None = None
        async with (
            session_lease(self.stores.runs, blueprint.context, name, clock=self.harness.clock),
            AsyncExitStack() as resources,
        ):
            self.root, self.epoch = await threaded(
                self.records.claim, self.project, self.session, self.request, lease_run=name
            )
            try:
                await self.harness.client.fence(
                    RecoveryFence(
                        project_id=self.project, session_id=self.session, epoch=self.epoch
                    )
                )
                owned_context = blueprint.context.model_copy(
                    update={"authority": ExecutionAuthority(work=root, run=name, epoch=self.epoch)}
                )
                await resources.enter_async_context(
                    environment_claim(
                        self.harness.client, owned_context, name, clock=self.harness.clock
                    )
                )
                with self.plans.authority(self.epoch, name):
                    await ChildSettlement(
                        self.stores.projects.control, Ledger(self.stores.documents)
                    ).drain(self.project, self.session, root=root)
                    await self.harness.release_completed_children(owned_context, root)
                    await threaded(self.plans.prepare, root, run=name, started=started)
                self.started = True
                while True:
                    self.wake.clear()
                    if not self.active and not await threaded(self.plans.ready):
                        break
                    await self.wake.wait()
                error = await threaded(self.plans.unfinished)
            except BaseException as exc:
                error = str(exc) or type(exc).__name__
                with suppress(ControlError), self.plans.authority(self.epoch, name):
                    if stop.is_set():
                        await threaded(self.records.stop, self.project, self.session, root)
                    else:
                        await threaded(self.records.interrupt, self.project, self.session, root)
                raise
            finally:
                self.started = False
                tasks = tuple(self.active.values())
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(ControlError), self.plans.authority(self.epoch, name):
                    await threaded(
                        self.records.complete_request,
                        self.project,
                        self.session,
                        self.request,
                        error=error,
                    )
        return error is not None

    async def next(self) -> tuple[str, ...]:
        if not self.started:
            return ()
        ready = await threaded(self.plans.ready)
        return tuple(work for work in ready if work not in self.active)[
            : max(0, 4 - len(self.active))
        ]

    async def branch(self, work_id: str) -> bool:
        if not self.started or work_id not in await self.next():
            return False
        if work_id in self.active or len(self.active) >= 4:
            return False
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("recovery branch requires an async task")
        self.active[work_id] = task
        try:
            if self.epoch is None:
                raise ControlError(CONFLICT, "Resume has no claimed epoch")
            with self.plans.authority(self.epoch, self.run_name):
                return await self._branch(work_id)
        finally:
            self.active.pop(work_id, None)
            self.wake.set()

    async def _branch(self, work_id: str) -> bool:
        try:
            work = await threaded(self.records.get, self.project, self.session, work_id)
            admitted = await self.prepare(work)
            if self.sink is None:
                raise RuntimeError("Resume has no sink")
            finished = await self.harness.start(admitted, self.sink, stop=self.stop)
            settlement = (
                await self.harness.checkpoint_child(finished)
                if admitted.request.delegation is not None
                else None
            )
            await save(
                finished.fields(),
                finished.status,
                finished.hooks,
                projects=self.stores.projects,
                error=self.sink.error,
            )
            if settlement is not None:
                await settlement.settle(
                    self.project, self.session, run_name(finished.started_at, finished.run_id)
                )
                await self.harness.release_child(finished)
            return True
        except asyncio.CancelledError:
            await threaded(self.records.block, self.project, self.session, work_id, "interrupted")
            raise
        except Exception as exc:
            await threaded(
                self.records.block,
                self.project,
                self.session,
                work_id,
                f"{type(exc).__name__}: {exc}",
            )
            return False

    async def prepare(self, work: Work) -> Admitted:
        """Validate original inputs, policy, budget, operations and kernels before dispatch."""
        control = self.stores.projects.control
        blueprint = await threaded(Blueprints(control).get, self.project, self.session, work.work)
        try:
            inputs = await threaded(
                RunInputs(control).get, self.project, self.session, work.current
            )
        except ControlError as exc:
            if exc.kind != NOT_FOUND:
                raise
            request = TypeAdapter(RunRequest).validate_python(blueprint.request)
        else:
            request = TypeAdapter(RunRequest).validate_python(inputs.request)
        project = await threaded(self.stores.projects.get, self.project)
        session = project.sessions.get(self.session)
        if (
            session is None
            or session.closed_at is not None
            or session.environment_id != blueprint.context.environment_id
            or project.policy != blueprint.context.policy_revision
        ):
            raise ControlError(
                CONFLICT, "recovery configuration changed: session/environment/policy"
            )
        if await threaded(self.stores.runs.over_cost_limit, self.project, self.harness.clock.now()):
            raise ControlError(CONFLICT, "model_budget: project budget is exhausted")
        # The original grant is immutable; a predecessor's saved remaining allowance
        # must never be reduced a second time on the next Resume.
        original = TypeAdapter(RunRequest).validate_python(blueprint.request)
        tokens = original.tokens
        if tokens is not None:
            tokens -= await threaded(self.plans.spent_tokens, work.work)
            if tokens <= 0:
                raise ControlError(CONFLICT, "model_budget: this work's token grant is exhausted")
        alias = request.alias or original.alias
        if alias is None:
            raise ControlError(CONFLICT, "recovery configuration lacks the original model alias")
        request = dataclasses.replace(request, alias=alias)
        if alias != self.harness.role(request).alias:
            raise ControlError(CONFLICT, "model alias changed since the saved attempt")
        await Reconciler(control, self.harness.client, clock=self.harness.clock).work(work)
        owner = Ownership(
            project=self.project,
            session=self.session,
            work=work.work,
            run=work.current,
            epoch=work.epoch,
        )
        await KernelJournal(control, owner, work.kernel).reconcile(
            self.harness.client, blueprint.context
        )
        capabilities = await self.harness.client.capabilities(blueprint.context.environment_id)
        tier, minimum = (
            tier_in_effect(capabilities.location),
            self.harness.settings.min_isolation_tier,
        )
        if minimum is not None and (tier is None or tier < minimum):
            raise ControlError(CONFLICT, "worker no longer meets the required isolation tier")
        plan = await threaded(self.plans.get, work.work)
        parent = None if work.parent is None else (await threaded(self.plans.get, work.parent)).run
        delegation = None
        if parent is not None:
            if request.delegation is None:
                raise ControlError(CONFLICT, "delegated recovery inputs lack original delegation")
            records = DelegationRecords(control)
            original = await threaded(records.get, self.project, self.session, request.delegation)
            delegation = new_id("dlg")
            await threaded(
                records.start,
                self.project,
                self.session,
                Delegation(
                    delegation_id=delegation,
                    parent=parent,
                    run=plan.run,
                    kind=original.kind,
                    brief=original.brief,
                    started_at=plan.started,
                ),
            )
        run_id = run_id_of(plan.run)
        if run_id is None:
            raise ControlError(CONFLICT, "invalid continuation run identity")
        request = dataclasses.replace(
            request,
            run_id=run_id,
            history=(),
            stop_by=None,
            work=work.work,
            predecessor=plan.predecessor,
            epoch=self.epoch,
            parent=parent,
            delegation=delegation,
            root=self.run_name,
            tokens=tokens,
            message_ids=(),
            requests=False,
            operator=False,
        )
        return Admitted(
            request,
            run_id,
            blueprint.context,
            tuple(capabilities.languages),
            tuple(capabilities.features),
            True,
            plan.started,
        )
