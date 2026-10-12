# SPDX-License-Identifier: MIT
"""Live harness driver for durable document math work; no idle scheduler or model."""

# Logical modules share control-store connections and queue leases.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.consistency.mathnotices import MathNotices
from sandbox_science.orchestrator.consistency.store import ReviewStore

from .mathjobs import Checker, MathJobs, owned, threaded


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
        with self.store.documents._read() as db:
            return db is not None and bool(
                db.execute(
                    (
                        "SELECT blocked FROM math_sources WHERE document=(SELECT document FROM "
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
        with self.store.documents._read() as db:
            return (
                db is not None
                and db.execute(
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
        async def renew() -> None:
            while True:
                await asyncio.sleep(30)
                await threaded(self.notice_store.renew, self.project, self.session, self.run)

        try:
            configuration = await threaded(self.checker.identity) if self.checker else None
        except (OSError, ValueError):
            configuration = None
        await threaded(self.jobs.retry_unavailable, self.project, configuration)
        renewal = asyncio.create_task(renew())
        try:
            yield
        finally:

            async def cleanup() -> None:
                renewal.cancel()
                if self.task is not None and not self.task.done():
                    self.task.cancel()
                await asyncio.gather(
                    renewal, *([self.task] if self.task is not None else []), return_exceptions=True
                )
                await threaded(
                    self.notice_store.release, self.project, self.session, self.run, self.delivered
                )
                await threaded(self.release_work)

            await owned(cleanup())

    def release_work(self) -> None:
        # Cursor/proofs/results survive; a later owner can resume immediately.
        with self.store.documents._write() as db:
            db.execute(
                "UPDATE math_jobs SET lease_until=0 WHERE owner=? AND state='running'",
                (self.owner,),
            )
