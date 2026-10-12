# SPDX-License-Identifier: MIT
"""Cancellation-owned explicit publication, with a durable uncertain-outcome fence."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

from pydantic import ValidationError

from sandbox_science.contracts import ArtifactRef, ExecutionContext, PublishRequest
from sandbox_science.contracts.artifact_inventory import (
    ArtifactPublicationSource,
    ArtifactReceipt,
    ArtifactSnapshot,
)

from .artifacts import ArtifactInventory
from .documents.errors import DocumentError
from .worker_client import WorkerAPIError, WorkerClient

_T = TypeVar("_T")


@dataclass(frozen=True)
class ArtifactPublicationOutcome:
    """Keep the real worker outcome even when control retention/transfer is refused."""

    worker: ArtifactRef | None
    receipt: ArtifactReceipt


class ArtifactPublisher:
    """An injected trusted run binding; arguments never select authors or projects.

    The caller must drain ``aclose`` before closing the client or shared control store.
    Caller cancellation detaches only its await, not admitted network/SQL ownership.
    """

    def __init__(
        self,
        inventory: ArtifactInventory,
        client: WorkerClient,
        context: ExecutionContext,
        *,
        run_id: str,
        alias: str | None = None,
    ) -> None:
        self.inventory, self.client, self.context = inventory, client, context
        self.run_id, self.alias = run_id, alias
        self.endpoint = client.base_url
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artifact-inventory")
        self._work: set[asyncio.Task[ArtifactPublicationOutcome]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    async def _sql(self, call: Callable[[], _T]) -> _T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, call)

    async def publish(
        self, request: PublishRequest, operation: str, *, tool_call: str, model: str | None = None
    ) -> ArtifactPublicationOutcome:
        """Reserve, snapshot once, pin the known outcome, then verify/retain exact bytes."""
        if request.context != self.context:
            raise ValueError("artifact publisher context differs")
        if self._closing or len(self._work) >= 8:
            return ArtifactPublicationOutcome(
                None,
                ArtifactReceipt(
                    state="held",
                    operation_id=operation,
                    message="artifact publisher is closing or at its admission limit",
                ),
            )
        source = ArtifactPublicationSource(
            context=self.context,
            run_id=self.run_id,
            tool_call=tool_call,
            alias=self.alias,
            model=model,
            worker_endpoint_sha256=hashlib.sha256(self.endpoint.encode()).hexdigest(),
        )
        task = asyncio.create_task(self._publish(request, operation, source))
        self._work.add(task)
        task.add_done_callback(self._work.discard)
        return await asyncio.shield(task)

    def _binding(self) -> None:
        if self.client.base_url != self.endpoint:
            raise ValueError("artifact worker endpoint changed during publication")

    async def _publish(
        self, request: PublishRequest, operation: str, source: ArtifactPublicationSource
    ) -> ArtifactPublicationOutcome:
        worker: ArtifactRef | None = None
        try:
            self._binding()
            state = await self._sql(
                lambda: self.inventory.begin(source, operation, request.path, request.media_type)
            )
            worker = state.worker
            if state.state == "done" and state.receipt is not None:
                return ArtifactPublicationOutcome(worker, state.receipt)
            if state.state == "intent" and not state.fresh:
                return ArtifactPublicationOutcome(
                    None,
                    ArtifactReceipt(
                        state="held",
                        operation_id=operation,
                        message=(
                            "worker publication outcome is unknown; retained intent "
                            "will not republish a mutable path"
                        ),
                    ),
                )
            if state.fresh:
                self._binding()
                new_worker = await self.client.publish_snapshot(request)
                worker = new_worker
                # Save the known outcome even if the endpoint changed meanwhile. The
                # original intent binding still fences reuse, and bytes are not fetched.
                await self._sql(
                    lambda: self.inventory.pin(
                        source, operation, request.path, request.media_type, new_worker
                    )
                )
            assert worker is not None  # noqa: S101 - pinned transfers retain worker ref
            pinned_worker = worker
            snapshot = ArtifactSnapshot.model_validate(pinned_worker.model_dump())
            if (
                snapshot.project_id != self.context.project_id
                or snapshot.source_path != request.path
            ):
                raise ValueError("worker snapshot project/path differs from request")
            self._binding()
            data = await self.client.verified_artifact_content(
                pinned_worker, max_bytes=self.inventory.limits.artifact_bytes
            )
            self._binding()
            receipt = await self._sql(
                lambda: self.inventory.finish(
                    source, operation, request.path, request.media_type, pinned_worker, data
                )
            )
            return ArtifactPublicationOutcome(worker, receipt)
        except (
            WorkerAPIError,
            DocumentError,
            ValidationError,
            ValueError,
            UnicodeError,
            sqlite3.DatabaseError,
        ) as error:
            # There is no transaction across worker and control. Keep a known worker
            # reference, including when quota or content verification refuses ingestion.
            message = str(error)[:512]
            receipt = ArtifactReceipt(state="held", operation_id=operation, message=message)
            try:
                receipt = await self._sql(
                    lambda: self.inventory.hold(
                        source, operation, request.path, request.media_type, message
                    )
                )
            except (
                DocumentError,
                ValidationError,
                ValueError,
                UnicodeError,
                sqlite3.DatabaseError,
            ):
                # Failed reservation, corruption, or storage failure cannot promise
                # durable audit. Existing intent/pin is never cleared to force a retry.
                receipt = receipt.model_copy(
                    update={
                        "message": ("hold observation could not be retained: " + receipt.message)[
                            :512
                        ]
                    }
                )
            return ArtifactPublicationOutcome(worker, receipt)

    async def aclose(self) -> None:
        """Stop admission and drain actual admitted tasks, under repeated cancellation."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                # Cleanup owns the borrowed store/client until actual work finishes.
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self) -> None:
        if self._work:
            await asyncio.gather(*tuple(self._work), return_exceptions=True)
        self._executor.shutdown(wait=True)
