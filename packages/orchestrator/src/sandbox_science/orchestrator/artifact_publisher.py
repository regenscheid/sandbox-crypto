# SPDX-License-Identifier: MIT
"""Cancellation-owned explicit publication, with a durable uncertain-outcome fence."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import ValidationError

from sandbox_science.contracts import ArtifactRef, ExecutionContext, PublishRequest
from sandbox_science.contracts.artifact_inventory import (
    ArtifactPublicationSource,
    ArtifactReceipt,
    ArtifactRevision,
    ArtifactRevisionRef,
    ArtifactSnapshot,
)

from .aio import OwnedExecutor, owned
from .artifact_blobs import Blob, BlobWriter
from .artifact_retention import RetentionDrain, RetentionOutbox, drain
from .artifacts import ArtifactInventory, ArtifactOperation, ExecutionReplay, Room, scoped_path
from .documents.errors import DocumentError
from .worker_client import WorkerAPIError, WorkerClient

_T = TypeVar("_T")
_log = logging.getLogger(__name__)


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
        self._owner = OwnedExecutor(8, "artifact-inventory")

    async def drain_retention(self, *, limit: int = 8) -> RetentionDrain:
        """Retry bounded pending cleanup delivery without selecting new research work."""
        if not self._owner.admits:
            return RetentionDrain(failed=1)
        return await self._owner.submit(
            drain(
                RetentionOutbox(self.inventory.documents.control),
                self.client,
                self.context.project_id,
                limit=limit,
            )
        )

    async def _acknowledge(self, operation: str) -> None:
        try:
            await drain(
                RetentionOutbox(self.inventory.documents.control),
                self.client,
                self.context.project_id,
                limit=1,
                operation=(self.context.session_id, operation),
            )
        except Exception as error:
            _log.error("retained artifact ACK remains pending (%s)", type(error).__name__)

    async def _sql(self, call: Callable[[], _T]) -> _T:
        return await owned(self._owner.thread(call))

    def source(
        self, tool_call: str, model: str | None = None, **attribution: Any
    ) -> ArtifactPublicationSource:
        """The trusted attribution of this run's call; ``attribution`` adds a recorded
        file's kind, role and execution (RA-34), never a project or author."""
        return ArtifactPublicationSource.model_validate(
            {
                **attribution,
                "context": self.context,
                "run_id": self.run_id,
                "tool_call": tool_call,
                "alias": self.alias,
                "model": model,
                "worker_endpoint_sha256": hashlib.sha256(self.endpoint.encode()).hexdigest(),
            }
        )

    async def room(self, size: int, *, requires_operation: bool = False) -> Room:
        """What the project's budget leaves for a revision of ``size`` bytes."""
        return await self._sql(
            lambda: self.inventory.room(
                self.context.project_id, size, requires_operation=requires_operation
            )
        )

    async def prior_execution(
        self, operation: str, path: str, source: ArtifactPublicationSource
    ) -> ExecutionReplay | None:
        """Read a prior capture of the same worker execution without republishing it."""
        replay = await self._sql(lambda: self.inventory.prior_execution(source, operation, path))
        if replay is not None and replay.content_retained and self._owner.admits:
            await self._owner.submit(self._acknowledge(operation))
        return replay

    async def begin_execution_record(
        self, source: ArtifactPublicationSource, operation: str, path: str, media_type: str | None
    ) -> ArtifactOperation:
        return await self._sql(lambda: self.inventory.begin(source, operation, path, media_type))

    async def finish_execution_metadata(
        self,
        source: ArtifactPublicationSource,
        operation: str,
        path: str,
        *,
        sha256: str,
        size: int,
        media_type: str,
        note: Literal["too_large", "quota"],
    ) -> ArtifactReceipt:
        return await self._sql(
            lambda: self.inventory.finish_execution_metadata(
                source,
                operation,
                path,
                sha256=sha256,
                size=size,
                media_type=media_type,
                note=note,
            )
        )

    async def finish_execution_deletion(
        self, source: ArtifactPublicationSource, operation: str, path: str
    ) -> ArtifactReceipt:
        return await self._sql(
            lambda: self.inventory.finish_execution_deletion(source, operation, path)
        )

    async def find(
        self,
        *,
        path: str | None = None,
        contains: str | None = None,
        media_type: str | None = None,
        results_only: bool = False,
        before: int | None = None,
    ) -> tuple[tuple[ArtifactRevision, ...], int | None, set[str]]:
        """The project's artifacts at their latest revision, ten at a time, the most
        recently changed first, the next page's cursor, and which are results (RA-37).
        ``path`` is one as this session's tools name it, or as the listing names it,
        ``sessions/<session>/…``: an agent gives back the path it was shown (lab 39)."""
        project = self.context.project_id
        scoped = None if path is None else scoped_path(path, self.context.session_id)

        def read() -> tuple[tuple[ArtifactRevision, ...], int | None, set[str]]:
            found, after = self.inventory.recent(
                project,
                before=before,
                group="results" if results_only else None,
                contains=contains,
                at_path=scoped,
                media_type=media_type,
            )
            results = self.inventory.results(project, [e.reference.artifact_id for e in found])
            return found, after, results

        return await self._sql(read)

    async def record_metadata(
        self,
        source: ArtifactPublicationSource,
        path: str,
        *,
        sha256: str,
        size: int,
        media_type: str,
        note: Literal["too_large", "quota"],
    ) -> tuple[ArtifactRevisionRef, bool]:
        """A revision with the file's hash and size, without its bytes."""
        return await self._sql(
            lambda: self.inventory.record_metadata(
                source, path, sha256=sha256, size=size, media_type=media_type, note=note
            )
        )

    async def record_deletion(
        self, source: ArtifactPublicationSource, path: str
    ) -> ArtifactRevisionRef | None:
        """A tombstone, when a live artifact is at ``path``."""
        return await self._sql(lambda: self.inventory.record_deletion(source, path))

    async def publish(
        self,
        request: PublishRequest,
        operation: str,
        *,
        tool_call: str,
        model: str | None = None,
        attribution: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> ArtifactPublicationOutcome:
        """Reserve, snapshot once, pin the known outcome, then verify/retain exact bytes,
        at most ``max_bytes`` of them (the inventory's limit when lower or not given)."""
        if request.context != self.context:
            raise ValueError("artifact publisher context differs")
        if not self._owner.admits:
            return ArtifactPublicationOutcome(
                None,
                ArtifactReceipt(
                    state="held",
                    operation_id=operation,
                    message="artifact publisher is closing or at its admission limit",
                ),
            )
        source = self.source(tool_call, model, **(attribution or {}))
        limit = self.inventory.limits.artifact_bytes
        bound = limit if max_bytes is None else max(0, min(max_bytes, limit))
        return await self._owner.submit(self._publish(request, operation, source, bound))

    def _binding(self) -> None:
        if self.client.base_url != self.endpoint:
            raise ValueError("artifact worker endpoint changed during publication")

    async def _publish(
        self,
        request: PublishRequest,
        operation: str,
        source: ArtifactPublicationSource,
        bound: int,
    ) -> ArtifactPublicationOutcome:
        outcome = await self._capture(request, operation, source, bound)
        # The successful scientific receipt is final before best-effort delivery.
        # No ACK exception can enter the capture's held-outcome handler below.
        if outcome.receipt.state == "recorded":
            await self._acknowledge(operation)
        return outcome

    async def _capture(
        self, request: PublishRequest, operation: str, source: ArtifactPublicationSource, bound: int
    ) -> ArtifactPublicationOutcome:
        worker: ArtifactRef | None = None
        transfer: str | None = None
        staged: BlobWriter | None = None
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
            if snapshot.size > bound:
                raise ValueError("artifact exceeds retained byte allowance")
            transfer = await self._sql(
                lambda: self.inventory.reserve_transfer(
                    self.context.project_id, snapshot.size, snapshot.sha256
                )
            )
            staged = await self._sql(
                lambda: self.inventory.blobs.writer(Blob(snapshot.sha256, snapshot.size))
            )
            writer = staged

            async def write(chunk: bytes) -> None:
                await self._sql(lambda: writer.write(chunk))

            await self.client.stream_artifact_content(pinned_worker, write, max_bytes=bound)
            self._binding()
            blob = await self._sql(writer.commit)
            reservation = transfer
            receipt = await self._sql(
                lambda: self.inventory.finish(
                    source,
                    operation,
                    request.path,
                    request.media_type,
                    pinned_worker,
                    b"",
                    blob=blob,
                    transfer=reservation,
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
            OSError,
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
        finally:
            if staged is not None:
                await self._sql(staged.discard)
            # An installed blob without a committed reference stays charged. A
            # crash or DB failure is not permission to remove possibly used bytes.
            if transfer is not None and (staged is None or not staged.installed):
                await self._sql(
                    lambda: self.inventory.release_transfer(self.context.project_id, transfer)
                )

    async def aclose(self) -> None:
        """Stop admission and drain actual admitted tasks, under repeated cancellation.
        Cleanup owns the borrowed store and client until the work finishes."""
        await self._owner.aclose()
