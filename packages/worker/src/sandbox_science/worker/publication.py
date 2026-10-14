# SPDX-License-Identifier: MIT
"""Publishing a file from an environment as an artifact, for the execution manager.

``Publication`` admits at most four publications, two for each environment, each
with a durable capture lease in the store. A publication reads the file through the
executor's bounded capture protocol, in ranges, into a blob, and records the
artifact. A lease is released only once the executor confirms that its capture
cleaned up, or the original sandbox is proven gone: neither a timeout nor a new
executor proves it. Blob writes run in a thread pool of their own, and every
cancellation waits for the thread work it started.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import math
import mimetypes
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    ArtifactRef,
    Clock,
    ErrorKind,
    PublishRequest,
    ReadFileRequest,
    new_id,
)
from sandbox_science.contracts.worker import MAX_READ_BYTES

from . import capture
from .artifacts import Blobs, BlobWriter
from .channel import ExecutorChannel
from .connections import EnvironmentConnections
from .errors import WorkerError
from .replies import Range, epoch, integer
from .store import CaptureLease, Store
from .usability import Usability

_log = logging.getLogger(__name__)


@dataclass
class _Owner:
    """A publication's lease, and whether its executor's cleanup is known."""

    lease: CaptureLease
    certain: bool = True


class Publication:
    """The artifact publications in progress, and their capture leases."""

    def __init__(
        self,
        store: Store,
        blobs: Blobs,
        connections: EnvironmentConnections,
        usability: Usability,
        *,
        clock: Clock,
        max_artifact_size: int,
        timeout: float,
        ack_timeout: float,
    ) -> None:
        self.store = store
        self.blobs = blobs
        self.connections = connections
        self.usability = usability
        self.clock = clock
        self.max_artifact_size = max_artifact_size
        """The largest artifact, in bytes."""
        self.timeout = timeout
        """Seconds one publication may take, its captures and blob writes included."""
        self.ack_timeout = ack_timeout
        """Seconds the executor gets to confirm it cleaned up a cancelled capture."""
        self._owners: dict[asyncio.Task[ArtifactRef], _Owner] = {}
        """Each publication's task, with its lease, until its work on the worker's side ends."""
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="artifact-blob")

    def running(self) -> int:
        """Publications whose work on the worker's side has not ended."""
        return len(self._owners)

    def holds(self, environment_id: str) -> bool:
        """Whether a publication from the environment is running."""
        return any(owner.lease.environment_id == environment_id for owner in self._owners.values())

    async def cancel(self, environment_id: str) -> None:
        """Cancel the environment's publications and wait until their work drained."""
        tasks = [
            task
            for task, owner in self._owners.items()
            if owner.lease.environment_id == environment_id
        ]
        for task in tasks:
            task.cancel()
        # VM cleanup cannot substitute for actual host blob/FD ownership. Keep
        # release pending through repeated cancellation until these tasks drain.
        _, cancelled = await capture.owned(asyncio.gather(*tasks, return_exceptions=True))
        if cancelled:
            raise asyncio.CancelledError

    async def close(self) -> None:
        """Cancel every publication, wait for it, then stop the blob threads."""
        tasks = list(self._owners)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Admitted blob work has drained before the pool/FDs or channels can close.
        self._pool.shutdown(wait=True)

    def sandbox_stopped(
        self, environment_id: str, incarnation: str, sandbox: str, sandbox_id: str
    ) -> None:
        """The original sandbox of that incarnation is proven stopped: its captures' guest
        cleanup no longer holds admission."""
        self.store.fenced_captures(environment_id, incarnation, sandbox, sandbox_id)

    def confirm_cleanup(self, environment_id: str, sandbox: str, sandbox_id: str) -> None:
        """Trusted lifecycle callback after proof of this exact original VM's deletion.

        Active host owners retain admission even after guest cleanup is proved.
        Unknown/other bindings and corrupt state never inherit the proof.
        """
        for lease in self.store.capture_leases():
            if lease.environment_id == environment_id and (lease.sandbox, lease.sandbox_id) == (
                sandbox,
                sandbox_id,
            ):
                self.store.fenced_captures(environment_id, lease.incarnation, sandbox, sandbox_id)

    async def reconcile(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Only held host-drained leases can receive same-incarnation cleanup ACKs.

        Active rows left by an abruptly lost worker remain conservative holds:
        another process may still own host I/O. Neither time nor a new executor
        incarnation proves that ownership ended.
        """
        protocol = channel.hello.get("artifact_capture_protocol")
        if type(protocol) is not int or protocol != capture.PROTOCOL:
            return
        try:
            leases = self.store.capture_leases()
        except ValueError:
            return
        for lease in leases:
            if (
                lease.phase != "held"
                or lease.environment_id != environment_id
                or lease.incarnation != channel.incarnation
                or not lease.capture_id
            ):
                continue
            try:
                async with asyncio.timeout(self.ack_timeout):
                    reply = await channel.request("capture_cancel", capture_id=lease.capture_id)
                if reply.get("capture_id") == lease.capture_id and reply.get("cleaned") is True:
                    self.store.acknowledge_capture(lease)
            except (WorkerError, TimeoutError, ValueError):
                _log.warning("capture cleanup remains unknown; durable admission held")

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        """Admit four durable publications, two per environment, without queued work.

        Unknown guest cleanup holds admission even after actual host work drains;
        this is a byte observation, never proof of writer closure.
        """
        environment_id = request.context.environment_id
        if self.usability.closing or self.usability.releasing(environment_id):
            raise WorkerError(ErrorKind.UNAVAILABLE, "artifact capture admission limit reached")
        deadline = self.clock.monotonic() + self.timeout
        try:
            lease = self.store.reserve_capture(new_id("pub"), environment_id)
        except ValueError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, str(exc)) from exc
        owner = _Owner(lease)
        task = asyncio.create_task(self._publish(request, deadline, owner))

        def finished(done: asyncio.Task[ArtifactRef]) -> None:
            self._owners.pop(done)
            try:
                self.store.finish_capture(owner.lease, certain=owner.certain)
            except (ValueError, sqlite3.Error):
                _log.error("capture completion cannot release displaced admission; held")
            if not done.cancelled():
                done.exception()

        # Owned from the moment it exists: nothing may fail between here and the
        # callback that releases its lease, or the environment could never suspend.
        self._owners[task] = owner
        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture.owned(task)
            raise

    async def _publish(
        self, request: PublishRequest, deadline: float, owner: _Owner
    ) -> ArtifactRef:
        context = request.context
        writer: BlobWriter | None = None
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + max(0, deadline - self.clock.monotonic())
            ):
                async with self.connections.using(context.environment_id) as channel:
                    protocol = channel.hello.get("artifact_capture_protocol")
                    if type(protocol) is not int or protocol != capture.PROTOCOL:
                        raise WorkerError(
                            ErrorKind.UNSUPPORTED,
                            "this executor lacks bounded artifact capture protocol 1",
                        )
                    reading = ReadFileRequest(context=context, path=request.path)
                    changed = WorkerError(
                        ErrorKind.CONFLICT, f"{request.path} changed while it was published"
                    )
                    creating = asyncio.get_running_loop().run_in_executor(
                        self._pool, self.blobs.writer
                    )
                    writer, cancelled = await capture.owned(creating)
                    if cancelled:
                        raise asyncio.CancelledError
                    found = await self._capture_range(
                        channel, reading, 0, deadline, owner, hashed=True
                    )
                    size, sha256, stamp = found.size, found.sha256, found.stamp
                    offset = 0
                    while True:
                        await self._blob_io(writer.write, found.data)
                        offset += len(found.data)
                        if offset == size:
                            break
                        if len(found.data) < MAX_READ_BYTES or found.eof:
                            raise changed
                        found = await self._capture_range(
                            channel, reading, offset, deadline, owner, hashed=False
                        )
                        if found.size != size or found.stamp != stamp:
                            raise changed
                    if (
                        sha256 is None
                        or writer.size != size
                        or not await self._blob_io(writer.commit, sha256)
                    ):
                        raise changed
                    media_type = (
                        request.media_type
                        or mimetypes.guess_type(request.path)[0]
                        or "application/octet-stream"
                    )
                    produced_by = await self._blob_io(
                        self.store.producer_hint,
                        context.project_id,
                        context.environment_id,
                        context.session_id,
                        request.path,
                        deadline,
                    )
                    if self.clock.monotonic() >= deadline:
                        raise TimeoutError
                    artifact = ArtifactRef(
                        artifact_id=new_id("art"),
                        project_id=context.project_id,
                        sha256=sha256,
                        size=size,
                        media_type=media_type,
                        source_path=request.path,
                        produced_by=produced_by,
                        created_at=self.clock.now(),
                    )
                    self.store.insert_artifact(artifact)
                    return artifact
        except TimeoutError as exc:
            raise WorkerError(
                ErrorKind.TIMED_OUT, "artifact capture operation budget expired; cleanup completed"
            ) from exc
        finally:
            if writer is not None:
                await self._blob_io(writer.discard)

    async def _blob_io(self, function: Callable[..., Any], *args: object) -> Any:
        """Keep the actual thread owned before cancellation can close/reuse its FD."""
        future = asyncio.get_running_loop().run_in_executor(self._pool, partial(function, *args))
        result, cancelled = await capture.owned(future)
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _capture_range(
        self,
        channel: ExecutorChannel,
        reading: ReadFileRequest,
        offset: int,
        deadline: float,
        owner: _Owner,
        *,
        hashed: bool,
    ) -> Range:
        remaining = deadline - self.clock.monotonic()
        if remaining <= 0:
            raise TimeoutError
        capture_id = new_id("cap")
        try:
            owner.lease = self.store.dispatch_capture(
                owner.lease,
                channel.incarnation,
                capture_id,
                self.store.capture_binding(reading.context.environment_id, channel.incarnation)
                if channel.daemon_token is not None
                else None,
            )
        except ValueError as exc:
            owner.certain = False
            raise WorkerError(ErrorKind.UNAVAILABLE, str(exc)) from exc
        owner.certain = False
        # Keep the request pending so cancellation waits for the original reader as
        # well as the explicit cleanup acknowledgement. A mere HTTP timeout cannot
        # cancel a filesystem operation in the sandbox.
        task = asyncio.create_task(
            channel.request(
                "capture_read",
                capture_id=capture_id,
                session=reading.context.session_id,
                path=reading.path,
                offset=offset,
                length=MAX_READ_BYTES,
                max_bytes=min(capture.MAX_BYTES, self.max_artifact_size),
                hash=hashed,
                timeout=remaining,
                reply_timeout=None,
            )
        )
        try:
            async with asyncio.timeout(remaining):
                reply = await asyncio.shield(task)
            if reply.get("capture_id") != capture_id or reply.get("cleaned") is not True:
                raise WorkerError(ErrorKind.INTERNAL, "capture reply lacks exact cleanup proof")
            owner.certain = True
        except BaseException:

            async def cleanup() -> None:
                try:
                    async with asyncio.timeout(self.ack_timeout):
                        cancelled = await channel.request(
                            "capture_cancel",
                            capture_id=capture_id,
                            reply_timeout=self.ack_timeout,
                        )
                    if (
                        cancelled.get("capture_id") == capture_id
                        and cancelled.get("cleaned") is True
                    ):
                        owner.certain = True
                finally:
                    # This task only owns a protocol reply, not guest filesystem
                    # work. No ACK means its durable lease stays held; the guest
                    # pool still owns the actual helper until observed exit.
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    if not task.cancelled() and task.exception() is None:
                        result = task.result()
                        if result.get("capture_id") == capture_id and result.get("cleaned") is True:
                            owner.certain = True

            with contextlib.suppress(WorkerError, TimeoutError):
                await capture.owned(asyncio.create_task(cleanup()))
            if not owner.certain:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "capture cleanup outcome is unknown; durable admission remains held",
                ) from None
            raise
        error: object = reply.get("capture_error")
        if error is not None:
            record = cast(dict[str, Any], error) if isinstance(error, dict) else {}
            kind, message = record.get("kind"), record.get("message")
            if (
                not isinstance(kind, str)
                or len(kind) > 32
                or not isinstance(message, str)
                or len(message) > 512
            ):
                raise WorkerError(ErrorKind.INTERNAL, "invalid capture failure metadata")
            try:
                error_kind = ErrorKind(kind)
            except ValueError:
                error_kind = ErrorKind.INTERNAL
            raise WorkerError(error_kind, message)
        try:
            encoded = reply.get("content_b64")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_READ_BYTES + 2) // 3):
                raise ValueError("range encoding exceeds its bound")
            data = base64.b64decode(encoded, validate=True)
            limit = min(capture.MAX_BYTES, self.max_artifact_size)
            size = integer(reply.get("size"), 0, limit)
            start = integer(reply.get("offset"), 0, capture.MAX_BYTES)
            sha256, stamp = reply.get("sha256"), reply.get("stamp")
            eof = reply.get("eof")
            modified = reply.get("modified")
            if (
                size is None
                or start != offset
                or len(data) > MAX_READ_BYTES
                or offset + len(data) > size
                or type(eof) is not bool
                or eof != (offset + len(data) == size)
                or not isinstance(stamp, str)
                or re.fullmatch(r"[0-9a-f]{64}", stamp) is None
                or (
                    hashed
                    and (
                        not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                    )
                )
                or (not hashed and sha256 is not None)
                or not isinstance(modified, int | float)
                or isinstance(modified, bool)
                or not math.isfinite(modified)
            ):
                raise ValueError("invalid capture metadata")
        except (TypeError, ValueError) as exc:
            raise WorkerError(
                ErrorKind.INTERNAL, "the executor sent a malformed capture reply"
            ) from exc
        return Range(size, sha256, epoch(modified), offset, data, encoded, eof, stamp)

    def artifact(self, artifact_id: str, project_id: str | None = None) -> ArtifactRef:
        try:
            artifact = self.store.artifact(artifact_id)
        except ValidationError as exc:
            raise WorkerError(
                ErrorKind.INTERNAL, f"the stored record of {artifact_id} is unreadable"
            ) from exc
        if artifact is None or (project_id is not None and artifact.project_id != project_id):
            raise WorkerError(ErrorKind.NOT_FOUND, f"no artifact {artifact_id}")
        return artifact
