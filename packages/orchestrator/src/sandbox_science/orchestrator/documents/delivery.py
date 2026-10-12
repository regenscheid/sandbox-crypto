# SPDX-License-Identifier: MIT
"""Manually invoked catalog delivery. No scheduler, sandbox writes or implicit clients.

One step obtains fresh status and performs at most one upload/commit. Its SQL and
bounded HTTP work remain owned after caller cancellation; close drains before closing
the journal. Catalog acknowledgement describes worker storage, never environment files.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.documents import (
    MAX_CATALOG_BYTES,
    MAX_CATALOG_ENTRIES,
    MAX_CATALOG_PAGE,
    MAX_CATALOG_PAGE_BYTES,
    MAX_CONTENT_CHUNK,
    CatalogContentChunk,
    CatalogDelta,
    CatalogSnapshot,
    CatalogSnapshotPage,
    CatalogStatus,
    generation_id,
)
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .delivery_journal import (
    JOURNAL_ERRORS,
    Claim,
    DeliveryBinding,
    DeliveryError,
    DeliveryLimits,
    Journal,
    Pin,
    endpoint_fingerprint,
    reset_progress,
)
from .errors import DocumentError
from .publication import Publications

__all__ = [
    "CatalogClient",
    "CatalogDelivery",
    "DeliveryBinding",
    "DeliveryError",
    "DeliveryLimits",
    "DeliveryStatus",
    "current",
    "endpoint_fingerprint",
]


class CatalogClient(Protocol):
    """An operator-approved bounded transport; credentials stay with its owner."""

    @property
    def base_url(self) -> str: ...

    async def catalog_status(self, project_id: str) -> CatalogStatus: ...
    async def catalog_begin(self, target: CatalogSnapshot) -> CatalogStatus: ...
    async def catalog_page(self, page: CatalogSnapshotPage) -> CatalogStatus: ...
    async def catalog_delta(self, delta: CatalogDelta) -> CatalogStatus: ...
    async def catalog_content(self, chunk: CatalogContentChunk) -> CatalogStatus: ...
    async def catalog_commit(self, target: CatalogSnapshot) -> CatalogStatus: ...


class DeliveryStatus(Contract):
    binding: DeliveryBinding
    state: Literal["progress", "acknowledged", "held", "retry", "busy"]
    target: CatalogSnapshot | None = None
    acknowledged: CatalogSnapshot | None = None
    observed_current: CatalogSnapshot | None = None
    message: Annotated[str, Field(max_length=512)] = ""
    publication: Literal["disabled"] = "disabled"


def current(status: CatalogStatus, project: str) -> CatalogSnapshot | None:
    """Validate the whole status before using any progress or receipt from it."""
    if status.project_id != project or status.publication != "disabled":
        raise DeliveryError("worker catalog status has a different project or publication mode")
    result = None
    if status.catalog_seq is None:
        if (
            status.generation_id is not None
            or status.manifest_sha256 is not None
            or status.entry_count
            or status.total_content_bytes
        ):
            raise DeliveryError("worker catalog status has an incomplete current identity")
    else:
        if status.generation_id is None or status.manifest_sha256 is None:
            raise DeliveryError("worker catalog status has an incomplete current identity")
        result = CatalogSnapshot(
            project_id=project,
            generation_id=status.generation_id,
            catalog_seq=status.catalog_seq,
            entry_count=status.entry_count,
            total_content_bytes=status.total_content_bytes,
            manifest_sha256=status.manifest_sha256,
        )
    stage = status.staging
    if stage is not None and (
        stage.generation_id
        != generation_id(stage.catalog_seq, stage.generation_id.rsplit("-", 1)[-1])
        or stage.entry_count > MAX_CATALOG_ENTRIES
        or stage.received_entries > stage.entry_count
        or stage.reserved_content_bytes > MAX_CATALOG_BYTES
        or stage.received_content_bytes > stage.reserved_content_bytes
        or stage.reserved_content_bytes > stage.entry_count * 1024 * 1024
        or (result is not None and stage.catalog_seq <= result.catalog_seq)
    ):
        raise DeliveryError("worker catalog staging status is incoherent")
    if status.acknowledged is not None:
        receipt = status.acknowledged
        if (
            receipt.project_id != project
            or result is None
            or receipt.catalog_seq > result.catalog_seq
        ):
            raise DeliveryError("worker catalog receipt is incoherent")
        if receipt.catalog_seq == result.catalog_seq and receipt != result:
            raise DeliveryError("worker receipt and current manifest disagree")
    return result


def _stage_matches(status: CatalogStatus, pin: Pin) -> bool:
    stage = status.staging
    if stage is None:
        return False
    target = pin.target
    if (
        stage.generation_id != target.generation_id
        or stage.catalog_seq != target.catalog_seq
        or stage.entry_count != target.entry_count
        or stage.reserved_content_bytes != target.total_content_bytes
        or stage.mode != pin.mode
        or (pin.mode == "delta" and stage.received_entries != target.entry_count)
    ):
        raise DeliveryError("worker has a foreign or conflicting staged target")
    return True


class CatalogDelivery:
    """Explicit manual step API. Own this object and the injected client until close."""

    def __init__(
        self,
        journal_path: Path,
        publications: Publications,
        *,
        limits: DeliveryLimits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if journal_path.resolve() == publications.documents.path.resolve():
            raise DeliveryError(
                "delivery journal must be separate from the canonical document store"
            )
        self.limits = limits or DeliveryLimits()
        self.publications = publications
        self._journal = Journal(journal_path, self.limits, clock)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-delivery")
        self._sql_work: set[asyncio.Task[object]] = set()
        self._steps: set[asyncio.Task[DeliveryStatus]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    async def _sql[T](self, operation: Callable[[], T]) -> T:
        if len(self._sql_work) >= self.limits.max_pending:
            raise DeliveryError("delivery SQL request capacity is occupied")

        async def execute() -> T:
            return await asyncio.get_running_loop().run_in_executor(self._executor, operation)

        task = asyncio.create_task(execute())
        self._sql_work.add(task)  # pyright: ignore[reportArgumentType]

        def finished(done: asyncio.Task[T]) -> None:
            self._sql_work.discard(done)  # pyright: ignore[reportArgumentType]
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def step(self, binding: DeliveryBinding, client: CatalogClient) -> DeliveryStatus:
        """Fresh status plus at most one upload; cancellation retains admitted work."""
        if self._closing:
            raise DeliveryError("catalog delivery is closing")
        if endpoint_fingerprint(client.base_url) != binding.endpoint_sha256:
            raise DeliveryError("client endpoint does not match the approved binding")
        if len(self._steps) >= self.limits.max_pending:
            return DeliveryStatus(
                binding=binding, state="busy", message="delivery capacity is occupied"
            )
        task = asyncio.create_task(self._step(binding, client))
        self._steps.add(task)

        def finished(done: asyncio.Task[DeliveryStatus]) -> None:
            self._steps.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    @staticmethod
    def _endpoint(binding: DeliveryBinding, client: CatalogClient) -> None:
        if endpoint_fingerprint(client.base_url) != binding.endpoint_sha256:
            raise DeliveryError("client endpoint changed during delivery")

    async def _step(self, binding: DeliveryBinding, client: CatalogClient) -> DeliveryStatus:
        claim: Claim | None = None
        pin: Pin | None = None
        observed: CatalogSnapshot | None = None
        try:
            claim = await self._sql(lambda: self._journal.claim(binding))
            if claim is None:
                return DeliveryStatus(
                    binding=binding, state="busy", message="binding has an active delivery lease"
                )
            pin = claim.pin
            self._endpoint(binding, client)
            status = await client.catalog_status(binding.project_id)
            self._endpoint(binding, client)
            observed = current(status, binding.project_id)
            if pin is None:
                # Only a freshly matched previous target is a trusted delta base.
                plan = await self._sql(
                    lambda: self.publications.plan(
                        binding.project_id,
                        after=observed.catalog_seq
                        if observed is not None and observed == claim.acknowledged
                        else None,
                    )
                )
                if observed is not None and (
                    observed.catalog_seq > plan.snapshot.catalog_seq
                    or (
                        observed.catalog_seq == plan.snapshot.catalog_seq
                        and observed != plan.snapshot
                    )
                ):
                    raise DeliveryError("worker cursor is ahead of or forked from this source")
                if status.staging is not None:
                    raise DeliveryError("worker has a foreign staged target without a local pin")
                pin = await self._sql(
                    lambda: self._journal.pin(claim, plan, delta=plan.delta is not None)
                )
            target = pin.target
            if observed is not None and observed.catalog_seq >= target.catalog_seq:
                if observed != target:
                    raise DeliveryError("worker current target is ahead of or forked from the pin")
                if status.staging is not None:
                    raise DeliveryError("worker has a foreign staged target")
                await self._sql(lambda: self._journal.acknowledge(claim, pin))
                return DeliveryStatus(
                    binding=binding,
                    state="acknowledged",
                    target=target,
                    acknowledged=target,
                    observed_current=observed,
                )
            if pin.delta is not None and (
                observed is None
                or observed.catalog_seq != pin.delta.after_seq
                or observed != claim.acknowledged
            ):
                raise DeliveryError(
                    "worker reset or delta base changed; explicit reconciliation required"
                )
            matches = _stage_matches(status, pin)
            if not matches:
                pin = reset_progress(pin)
                started = pin
                await self._sql(lambda: self._journal.progress(claim, started))
                await self._sql(lambda: self._journal.active(claim))
                self._endpoint(binding, client)
                reply = await (
                    client.catalog_delta(pin.delta)
                    if pin.delta is not None
                    else client.catalog_begin(target)
                )
                self._endpoint(binding, client)
                observed = current(reply, binding.project_id)
                if observed is not None and observed == target:
                    return await self._acknowledge(claim, pin, reply, observed)
                if not _stage_matches(reply, pin):
                    raise DeliveryError("worker did not establish the exact staged target")
                pin = replace(
                    pin,
                    phase="metadata" if pin.mode == "snapshot" else "content",
                    next_entry=0 if pin.mode == "snapshot" else len(pin.entries),
                )
            else:
                stage = status.staging
                assert stage is not None  # noqa: S101 - checked by _stage_matches
                if pin.phase == "begin":
                    pin = replace(
                        pin,
                        phase="metadata" if pin.mode == "snapshot" else "content",
                        next_entry=0 if pin.mode == "snapshot" else len(pin.entries),
                    )
                elif stage.received_entries < pin.next_entry:
                    pin = replace(
                        reset_progress(pin),
                        phase="metadata" if pin.mode == "snapshot" else "content",
                        next_entry=0 if pin.mode == "snapshot" else len(pin.entries),
                    )
                elif stage.received_content_bytes < pin.uploaded_bytes:
                    pin = replace(pin, phase="content", next_content=0, offset=0, uploaded_bytes=0)
                pin, reply = await self._upload(claim, pin, client)
                self._endpoint(binding, client)
                observed = current(reply, binding.project_id)
                if observed is not None and observed == target:
                    return await self._acknowledge(claim, pin, reply, observed)
                if not _stage_matches(reply, pin):
                    raise DeliveryError("worker lost the exact staged target")
            await self._sql(lambda: self._journal.progress(claim, pin))
            return DeliveryStatus(
                binding=binding,
                state="progress",
                target=target,
                acknowledged=claim.acknowledged,
                observed_current=observed,
            )
        except WorkerAPIError as error:
            return DeliveryStatus(
                binding=binding,
                state="retry" if error.kind == ErrorKind.UNAVAILABLE else "held",
                target=pin.target if pin is not None else None,
                acknowledged=claim.acknowledged if claim is not None else None,
                observed_current=observed,
                message=error.message[:512],
            )
        except (*JOURNAL_ERRORS, DocumentError) as error:
            return DeliveryStatus(
                binding=binding,
                state="held",
                target=pin.target if pin is not None else None,
                acknowledged=claim.acknowledged if claim is not None else None,
                observed_current=observed,
                message=str(error)[:512],
            )
        finally:
            if claim is not None:
                with contextlib.suppress(*JOURNAL_ERRORS):
                    await self._sql(lambda: self._journal.release(claim))

    async def _acknowledge(
        self, claim: Claim, pin: Pin, status: CatalogStatus, observed: CatalogSnapshot
    ) -> DeliveryStatus:
        if observed != pin.target or (
            status.acknowledged is not None and status.acknowledged != pin.target
        ):
            raise DeliveryError("worker acknowledgement does not match the exact pin")
        if status.staging is not None:
            raise DeliveryError("worker has a foreign staged target after acknowledgement")
        await self._sql(lambda: self._journal.acknowledge(claim, pin))
        return DeliveryStatus(
            binding=claim.binding,
            state="acknowledged",
            target=pin.target,
            acknowledged=pin.target,
            observed_current=observed,
        )

    async def _upload(
        self, claim: Claim, pin: Pin, client: CatalogClient
    ) -> tuple[Pin, CatalogStatus]:
        # Advancing empty phases is bounded by the four phases, with no HTTP loop.
        if pin.phase == "metadata" and pin.next_entry == len(pin.entries):
            pin = replace(pin, phase="content")
        if pin.phase == "content" and pin.next_content == len(pin.contents):
            pin = replace(pin, phase="commit")
        await self._sql(lambda: self._journal.active(claim))
        if pin.phase == "metadata":
            page: CatalogSnapshotPage | None = None
            end = min(len(pin.entries), pin.next_entry + MAX_CATALOG_PAGE)
            while end > pin.next_entry:
                page = CatalogSnapshotPage.model_construct(
                    project_id=pin.target.project_id,
                    generation_id=pin.target.generation_id,
                    start_index=pin.next_entry,
                    entries=pin.entries[pin.next_entry : end],
                )
                if len(page.model_dump_json().encode()) <= MAX_CATALOG_PAGE_BYTES:
                    break
                end -= 1
            if end == pin.next_entry or page is None:
                raise DeliveryError("one pinned metadata entry exceeds the page byte bound")
            page = CatalogSnapshotPage.model_validate(page.model_dump())
            self._endpoint(claim.binding, client)
            reply = await client.catalog_page(page)
            if reply.staging is not None and reply.staging.received_entries < end:
                raise DeliveryError("worker did not retain the complete metadata page")
            return replace(pin, next_entry=end), reply
        if pin.phase == "content":
            entry = pin.contents[pin.next_content]
            data = await self._sql(lambda: self.publications.content(pin.target.project_id, entry))
            await self._sql(lambda: self._journal.active(claim))
            part = data[pin.offset : pin.offset + MAX_CONTENT_CHUNK]
            if entry.content_sha256 is None:
                raise DeliveryError("pinned live content has no digest")
            chunk = CatalogContentChunk(
                project_id=pin.target.project_id,
                generation_id=pin.target.generation_id,
                content_sha256=entry.content_sha256,
                offset=pin.offset,
                content_b64=base64.b64encode(part).decode("ascii"),
            )
            self._endpoint(claim.binding, client)
            reply = await client.catalog_content(chunk)
            offset = pin.offset + len(part)
            return replace(
                pin,
                next_content=pin.next_content + (offset == entry.content_bytes),
                offset=0 if offset == entry.content_bytes else offset,
                uploaded_bytes=pin.uploaded_bytes + len(part),
            ), reply
        if pin.phase != "commit":
            raise DeliveryError("unreadable delivery phase")
        self._endpoint(claim.binding, client)
        return pin, await client.catalog_commit(pin.target)

    async def aclose(self) -> None:
        """Stop admission and drain actual work, even under repeated cancellation."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        while self._steps:
            await asyncio.gather(*tuple(self._steps), return_exceptions=True)
        while self._sql_work:
            await asyncio.gather(*tuple(self._sql_work), return_exceptions=True)
        await self._sql(self._journal.close)
        self._executor.shutdown(wait=True)
