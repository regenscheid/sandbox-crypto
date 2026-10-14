# SPDX-License-Identifier: MIT
"""Live harness driver for durable document math work; no idle scheduler or model."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.aio import lease, owned, threaded
from sandbox_science.orchestrator.consistency.mathnotices import MathNotices
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.notices import MATH

from .mathjobs import Checker, MathJobs
from .store import DocumentStore

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import RunRecord


class MathController:
    def __init__(
        self,
        store: ReviewStore,
        project: str,
        session: str,
        run: str,
        *,
        checker: Checker | None = None,
    ) -> None:
        store.scope(project, session)
        self.store, self.project, self.session, self.run = store, project, session, run
        self.jobs, self.notice_store = MathJobs(store), MathNotices(store)
        self.checker, self.owner = checker, new_id("math-owner")
        self.seen: set[str] = set()
        self.delivered: set[str] = set()
        self.task: asyncio.Task[None] | None = None

    async def _drain(self) -> None:
        # Eight bounded steps: <=256 formulas or16 historical maps, then yield to
        # the author. Durable remaining work is explicitly visible and resumable.
        check: str | None = None
        for _ in range(8):
            if check is None:
                check = await threaded(self.jobs.claim, self.project, self.owner)
                if check is None:
                    return
            ended = await self.jobs.step(check, self.owner, self.checker)
            if ended:
                check = None
            else:
                if await threaded(self.blocked, check):
                    return

    def blocked(self, check: str) -> bool:
        from .comment_math import metadata
        from .response_math import metadata as response_metadata

        if self.store.documents.control.missing:
            return False
        with self.store.documents.control.read() as tx:
            db = tx.sql
            if (response := response_metadata(db, check)) is not None:
                return bool(
                    db.execute(
                        "SELECT blocked IS NOT NULL AND blocked!='' FROM "
                        "response_math_sources WHERE source=?",
                        (response["source"],),
                    ).fetchone()[0]
                )
            if (meta := metadata(db, check)) is not None:
                return bool(
                    db.execute(
                        "SELECT blocked IS NOT NULL AND blocked!='' "
                        "FROM comment_math_sources WHERE entry_id=?",
                        (meta["entry_id"],),
                    ).fetchone()[0]
                )
            return bool(
                db.execute(
                    (
                        "SELECT blocked IS NOT NULL AND blocked!='' FROM math_sources "
                        "WHERE document=(SELECT document FROM "
                        "math_jobs WHERE check_id=?)"
                    ),
                    (check,),
                ).fetchone()[0]
            )

    def safe_point(self) -> None:
        if self.task is None or self.task.done():
            # Consume an earlier exception at the closing boundary; never silently
            # start another batch after a failed renderer/store step.
            if self.task is not None and self.task.exception() is not None:
                return
            self.task = asyncio.create_task(self._drain())

    async def closing(self) -> dict[str, Any]:
        self.safe_point()
        pending = False
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
            except TimeoutError:
                pending = True
            except Exception:
                # Source commits remain valid. A fenced/failed step stays queued;
                # the summary discloses failure rather than fabricating a verdict.
                return {
                    "pending": True,
                    "detail": "A math check step failed; durable work remains queued.",
                }
        return {
            "pending": pending or await threaded(self.remaining),
            "renderer": "configured" if self.checker else "unavailable",
            "counts": await threaded(self.notice_store.counts, self.project, self.session),
        }

    def remaining(self) -> bool:
        from .comment_math import remaining
        from .project_macros import needs_refresh
        from .response_math import remaining as response_remaining

        if self.store.documents.control.missing:
            return False
        with self.store.documents.control.read() as tx:
            db = tx.sql
            return (
                needs_refresh(db, self.project)
                or remaining(db, self.project)
                or response_remaining(db, self.project)
                or db.execute(
                    (
                        "SELECT 1 FROM math_sources WHERE project=? AND (completed<target OR "
                        "retry_requested=1 OR EXISTS(SELECT 1 FROM math_jobs j WHERE "
                        "j.document=math_sources.document AND j.state='running')) LIMIT 1"
                    ),
                    (self.project,),
                ).fetchone()
                is not None
            )

    async def notices(self) -> list[dict[str, Any]]:
        return await threaded(
            self.notice_store.pending, self.project, self.session, self.run, self.seen
        )

    @asynccontextmanager
    async def active(self) -> AsyncGenerator[None]:
        try:
            configuration = await threaded(self.checker.identity) if self.checker else None
        except (OSError, ValueError):
            configuration = None
        await threaded(self.jobs.retry_unavailable, self.project, configuration)
        renew = partial(self.notice_store.renew, self.project, self.session, self.run)
        try:
            async with lease(renew, interval=30, name="math notice"):
                yield
        finally:

            async def cleanup() -> None:
                if self.task is not None and not self.task.done():
                    self.task.cancel()
                if self.task is not None:
                    await asyncio.gather(self.task, return_exceptions=True)
                await threaded(
                    self.notice_store.release, self.project, self.session, self.run, self.delivered
                )
                await threaded(self.release_work)

            await owned(cleanup())

    def release_work(self) -> None:
        # Cursor/proofs/results survive; a later owner can resume immediately.
        with self.store.documents.control.write() as tx:
            db = tx.sql
            db.execute(
                "UPDATE math_jobs SET lease_until=0 WHERE owner=? AND state='running'",
                (self.owner,),
            )


class MathChecks(TurnHook):
    """Document math checks in a run: a bounded batch before each turn, findings at a
    closing answer with a response round, and at the end the bound on delivered
    findings never fixed. A failed run's acknowledgement finalizes nothing."""

    name = "math"

    def __init__(self, controller: MathController) -> None:
        self.controller = controller
        self.result: dict[str, Any] | None = None

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self.controller.active()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point == "turn":
            self.controller.safe_point()
        return ()

    async def model_returned(self, run: RunView) -> None:
        self.controller.delivered.update(run.delivered_ids(self.name))

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.result = await self.controller.closing()
        if not run.can_respond:
            return Closing()
        found = await self.controller.notices()
        ids = tuple(str(notice["notice_id"]) for notice in found)
        self.controller.seen.update(ids)
        return Closing("respond", (Notice(self.name, ids, MATH.text, found),) if found else ())

    async def accept(self, run: RunView) -> None:
        controller = self.controller
        if not controller.delivered:
            return
        scope = controller.project, controller.session
        await threaded(controller.notice_store.bound, *scope, controller.run, controller.delivered)
        if self.result is not None:
            self.result["counts"] = await threaded(controller.notice_store.counts, *scope)

    def summary(self) -> object | None:
        return self.result

    async def acknowledge(self, record: RunRecord) -> None:
        if ids := record.delivered.get(self.name):
            await asyncio.to_thread(self._acknowledge, record, ids)

    def _acknowledge(self, record: RunRecord, ids: list[str]) -> None:
        documents = self.controller.store.documents
        store = DocumentStore(documents.path, producer=documents.producer)
        try:
            MathNotices(ReviewStore(store)).acknowledge(
                record.context.project_id,
                record.context.session_id,
                record.run_id,
                ids,
                finalize=record.failure is None,
            )
        finally:
            store.close()
