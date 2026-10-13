# SPDX-License-Identifier: MIT
"""One explicit MAIN historical prefix, with owned passive proof/save/ACK lifetime."""

# Trusted integration owns retained SessionSnapshot/client internals. No model/tool
# is given these file/configuration capabilities.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import stat
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Protocol, cast

from sandbox_science.contracts import (
    ExecutionContext,
    KernelLineageIdentity,
    KernelLineageObservation,
)

from .kernel_notice_bindings import BindingError, KernelBindings, WorkerConfiguration
from .kernel_notice_receipts import (
    MAX_DELIVERY_BYTES,
    NOTICE_PROSE,
    KernelNoticeDelivery,
    bounded_kernel_data,
)
from .kernel_notices import (
    KernelBirthPin,
    KernelClaim,
    KernelNoticeLimits,
    KernelNotices,
    MainNoticeRun,
    NoticeScope,
    TrustedKernelNotice,
)
from .projects import ProjectStore, SessionSnapshot
from .worker_client import WorkerClient

PROOF_TIMEOUT = 6.0
RENEW_SECONDS = 10.0
_INBOX = "kernel-notices.sqlite3"


class LineageClient(Protocol):
    async def observe_kernel_lineage(
        self, context: ExecutionContext
    ) -> KernelLineageObservation: ...

    async def close(self) -> None: ...


class _NotAdmittedError(Exception):
    pass


class _UnavailableLineageError(BindingError):
    def __init__(self, reason: Literal["unknown_lineage", "unknown_revision"]) -> None:
        super().__init__("notification lineage unavailable")
        self.reason: Literal["unknown_lineage", "unknown_revision"] = reason


class _Lane:
    """Private finite thread ownership; canceled callers drain real admitted work."""

    def __init__(self, name: str) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self.pending: set[asyncio.Future[object]] = set()
        self.closed = False

    async def call[T](
        self,
        work: Callable[[], T],
        *,
        cleanup: bool = False,
        cancelled: threading.Event | None = None,
    ) -> T:
        if self.closed or len(self.pending) >= 4:
            raise BindingError("notification work capacity unavailable")
        cancelled = cancelled or threading.Event()

        def admitted() -> T:
            if cancelled.is_set() and not cleanup:
                raise _NotAdmittedError
            return work()

        future = asyncio.get_running_loop().run_in_executor(self.executor, admitted)
        retained = cast(asyncio.Future[object], future)
        self.pending.add(retained)
        interruption: asyncio.CancelledError | None = None
        try:
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError as error:
                    interruption = interruption or error
                    cancelled.set()
                    if future.done():
                        try:
                            result = future.result()
                        except BaseException:
                            raise interruption from None
                        break
                except BaseException:
                    if interruption is not None:
                        raise interruption from None
                    raise
            if interruption is not None:
                if isinstance(result, SessionSnapshot):
                    result.close()
                raise interruption
            return result
        finally:
            self.pending.discard(retained)

    async def drain(self) -> None:
        await asyncio.gather(*tuple(self.pending), return_exceptions=True)

    def close(self) -> None:
        if any(not future.done() for future in self.pending):
            raise BindingError("notification work has not drained")
        self.closed = True
        self.executor.shutdown(wait=True)


class KernelDeliveryController:
    """Only a trusted explicitly started stateful MAIN owns this optional controller."""

    def __init__(
        self,
        projects: ProjectStore,
        context: ExecutionContext,
        run_id: str,
        source_client: WorkerClient,
        *,
        observer: LineageClient | None = None,
    ) -> None:
        self.projects, self.context, self.run_id = projects, context, run_id
        self.source_client = source_client
        self.client: LineageClient | None = observer
        self.bindings = KernelBindings(projects.root.parent)
        self._lane, self._save_lane = _Lane("kernel-notice"), _Lane("kernel-notice-save")
        self.lifetime: str | None = None
        self.binding: str | None = None
        self.inbox: KernelNotices | None = None
        self.actor: MainNoticeRun | None = None
        self.claim: KernelClaim | None = None
        self.delivered: KernelNoticeDelivery | None = None
        self._injected: KernelNoticeDelivery | None = None
        self._root = self._file = -1
        self._ready = self._closing = self._replaced = self._acked = False
        self._proof: asyncio.Task[KernelNoticeDelivery | None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._close_task: asyncio.Task[BaseException | None] | None = None

    def _configuration(self) -> WorkerConfiguration:
        if not self.source_client._owns_session:
            raise BindingError("custom notification transport is not bound")
        return WorkerConfiguration(
            self.source_client.base_url, self.source_client._headers["Authorization"]
        )

    def _initialize(self) -> None:
        session = self.projects.kernel_notice_baseline(
            self.context.project_id, self.context.session_id
        )
        if session.environment_id != self.context.environment_id:
            raise BindingError("notification context changed")
        self.lifetime = session.kernel_notice_lifetime
        pin = self.bindings.ensure(
            self.context.project_id, self.context.session_id, self._configuration()
        )
        self.binding = pin.binding_id
        self._root = os.open(
            self.projects.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        with self.bindings.inbox() as (descriptor, first):
            self._file = os.dup(descriptor)
            self._state_matches()
            self.inbox = KernelNotices(
                self.projects.root.parent / _INBOX,
                limits=KernelNoticeLimits(batch_bytes=MAX_DELIVERY_BYTES - len(NOTICE_PROSE) - 64),
                initialize=first,
            )
        self._state_matches()

    async def initialize(self) -> None:
        """Complete baseline before other MAIN controllers pin registry snapshots."""
        try:
            await self._lane.call(self._initialize)
            if self.client is None:
                config = self._configuration()
                client = WorkerClient(config.endpoint, "")
                client._headers = {"Authorization": config.authorization}
                self.client = client
            self._ready = True
        except Exception:
            # Missing/busy/legacy notification state never grants or vetoes tools.
            self._ready = False

    def _state_matches(self) -> None:
        before = os.fstat(self._file)
        after = os.stat(_INBOX, dir_fd=self._root, follow_symlinks=False)
        parent = os.stat(self.projects.root.parent, follow_symlinks=False)
        root = os.fstat(self._root)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or (
            parent.st_dev,
            parent.st_ino,
        ) != (root.st_dev, root.st_ino):
            raise BindingError("notification inbox identity changed")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                descriptor = KernelBindings._file(self._root, _INBOX + suffix)
            except FileNotFoundError:
                continue
            os.close(descriptor)

    def _snapshot(self) -> SessionSnapshot:
        snapshot = self.projects.session_snapshot(self.context.project_id, self.context.session_id)
        try:
            configuration = self._configuration()
            if isinstance(self.client, WorkerClient) and (
                self.client.base_url != configuration.endpoint
                or self.client._headers.get("Authorization") != configuration.authorization
            ):
                raise BindingError("passive notification client configuration changed")
            if (
                snapshot.context.environment_id != self.context.environment_id
                or snapshot.session.kernel_notice_lifetime != self.lifetime
                or self.bindings.ensure(
                    self.context.project_id, self.context.session_id, configuration
                ).binding_id
                != self.binding
            ):
                raise BindingError("notification control binding changed")
            self._state_matches()
            return snapshot
        except BaseException:
            snapshot.close()
            raise

    @contextlib.contextmanager
    def _control(self, original: SessionSnapshot | None = None) -> Generator[None]:
        root = descriptor = -1
        snapshot: SessionSnapshot | None = None
        try:
            root = os.open(self.projects.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptor = os.open(
                self.context.project_id + ".lock",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_nlink != 1:
                raise BindingError("notification project lock unavailable")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            snapshot = self._snapshot()
            if original is not None and not original.matches(snapshot):
                raise BindingError("notification session changed across proof")
            yield
        finally:
            if snapshot is not None:
                snapshot.close()
            if descriptor >= 0:
                os.close(descriptor)
            if root >= 0:
                os.close(root)

    def _converted(self, observation: KernelLineageObservation) -> tuple[TrustedKernelNotice, ...]:
        observed = KernelLineageObservation.model_validate_json(
            bounded_kernel_data(observation), strict=True
        )
        if self.lifetime is None or self.binding is None:
            raise BindingError("notification control binding unavailable")
        if (observed.project_id, observed.environment_id, observed.session_id) != (
            self.context.project_id,
            self.context.environment_id,
            self.context.session_id,
        ) or not observed.kernels:
            raise BindingError("notification lineage unavailable")
        if observed.current_revision is None:
            raise _UnavailableLineageError("unknown_revision")
        entries: list[TrustedKernelNotice] = []
        for entry in observed.kernels:
            if entry.lineage_status != "proved" or entry.birth is None:
                raise BindingError("notification lineage unavailable")
            identity = KernelLineageIdentity(birth=entry.birth, generation=entry.kernel.generation)
            birth = KernelBirthPin.model_validate(
                identity.birth.model_dump(mode="python"), strict=True
            )
            scope = NoticeScope(
                project_id=self.context.project_id,
                environment_id=self.context.environment_id,
                session_id=self.context.session_id,
                session_lifetime=self.lifetime,
                worker_binding=self.binding,
                sandbox=birth.sandbox,
                sandbox_id=birth.sandbox_id,
                incarnation=birth.incarnation,
            )
            entries.append(
                TrustedKernelNotice(
                    scope=scope,
                    birth=birth,
                    generation=identity.generation,
                    current_revision=observed.current_revision,
                    observed_at=observed.observed_at,
                )
            )
        if any(entry.scope != entries[0].scope for entry in entries):
            raise BindingError("notification lineage binding disagrees")
        return tuple(entries)

    async def _collect(self, *, acknowledge: bool = False) -> KernelNoticeDelivery | None:
        assert self.client is not None  # noqa: S101 - initialized owner
        original = await self._lane.call(self._snapshot)
        try:
            observed = await self.client.observe_kernel_lineage(self.context)
            entries = self._converted(observed)
            scope = entries[0].scope
            if self.actor is not None and self.actor.scope != scope:
                self._replaced = True
                raise BindingError("notification original binding changed")
            actor = self.actor or MainNoticeRun(scope=scope, run_id=self.run_id, role="main")

            def record() -> KernelNoticeDelivery | None:
                with self._control(original):
                    assert self.inbox is not None  # noqa: S101 - initialized owner
                    if acknowledge:
                        if self.claim is not None and self.delivered is not None:
                            self.inbox.acknowledge(actor, self.claim)
                            self._acked = True
                        return None
                    self.actor = actor
                    for entry in entries:
                        admission = self.inbox.record(actor, entry)
                        if admission.state == "held":
                            # A later positive language must not erase this hold.
                            # Capacity still permits the existing prefix to drain.
                            break
                    if self.claim is None:
                        self.claim = self.inbox.claim(actor)
                    if (
                        self.claim is None
                        or self.delivered is not None
                        or self._injected is not None
                    ):
                        return None
                    return KernelNoticeDelivery(version=1, claim=self.claim)

            return await self._lane.call(record)
        except Exception as error:
            reason: Literal["unknown_lineage", "unknown_revision"] = (
                error.reason if isinstance(error, _UnavailableLineageError) else "unknown_lineage"
            )
            with contextlib.suppress(Exception):
                await self._lane.call(lambda: self._unknown(reason))
            raise
        finally:
            await self._lane.call(original.close, cleanup=True)

    def _unknown(self, reason: Literal["unknown_lineage", "unknown_revision"]) -> None:
        with self._control():
            self.bindings.diagnostic(
                self.context.project_id,
                self.context.session_id,
                "identity_changed" if self._replaced else reason,
            )
            if self.actor is not None and self.inbox is not None and not self._replaced:
                self.inbox.hold(self.actor, reason)

    async def _observe(self, *, acknowledge: bool = False) -> KernelNoticeDelivery | None:
        if not self._ready or self._closing or (self._proof is not None and not self._proof.done()):
            return None
        self._proof = asyncio.create_task(self._collect(acknowledge=acknowledge))
        try:
            async with asyncio.timeout(PROOF_TIMEOUT):
                return await asyncio.shield(self._proof)
        except TimeoutError:
            self._proof.cancel()
            return None
        except asyncio.CancelledError:
            self._proof.cancel()
            await self._settle(self._proof)
            raise
        except Exception:
            return None

    @staticmethod
    async def _settle(task: asyncio.Task[object]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()

    async def before_model(self) -> KernelNoticeDelivery | None:
        """At most one newly injected prefix; old delivered context stays historical."""
        result = await self._observe()
        if self.claim is not None and self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self._renew())
        if result is not None:
            self._injected = result
        return result

    def model_delivered(self) -> None:
        if self._injected is not None:
            self.delivered = self._injected

    async def _renew(self) -> None:
        while not self._stop.is_set() and not self._replaced:
            try:
                await asyncio.wait_for(self._stop.wait(), RENEW_SECONDS)
                return
            except TimeoutError:
                pass
            if self._replaced:
                return
            try:

                def renew() -> None:
                    with self._control():
                        if self.inbox is None or self.actor is None or self.claim is None:
                            raise BindingError("notification lease unavailable")
                        self.claim = self.inbox.renew(self.actor, self.claim)

                await self._lane.call(renew)
            except Exception:
                return

    async def save[T](
        self, work: Callable[[], T], *, cancelled: threading.Event | None = None
    ) -> T:
        """Separate owned fsync lane: renewal never queues behind this save."""
        return await self._save_lane.call(work, cancelled=cancelled)

    async def acknowledge_saved(self) -> bool:
        if self.delivered is None or self._acked:
            return self._acked
        await self._observe(acknowledge=True)
        return self._acked

    async def _finish(self) -> None:
        self._stop.set()
        if self._proof is not None:
            self._proof.cancel()
            await self._settle(self._proof)
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await self._settle(self._heartbeat)
        await self._save_lane.drain()
        await self._lane.drain()
        try:

            def release() -> None:
                if (
                    not self._acked
                    and self.claim is not None
                    and self.actor is not None
                    and self.inbox is not None
                ):
                    with self._control():
                        self.inbox.release(self.actor, self.claim)

            with contextlib.suppress(Exception):
                await self._lane.call(release)
        finally:
            try:
                if self.client is not None:
                    await self.client.close()
            finally:

                def files() -> None:
                    if self._file >= 0:
                        os.close(self._file)
                        self._file = -1
                    if self._root >= 0:
                        os.close(self._root)
                        self._root = -1

                await self._lane.call(files, cleanup=True)
                self._lane.close()
                self._save_lane.close()

    async def close(self) -> None:
        self._closing = True
        if self._close_task is None:

            async def finish_result() -> BaseException | None:
                try:
                    await self._finish()
                except BaseException as error:
                    # Relay cleanup interruptions through the awaiting owner; a
                    # background SystemExit must not bypass its first-error guard.
                    return error
                return None

            self._close_task = asyncio.create_task(finish_result())
        interruption: asyncio.CancelledError | None = None
        while True:
            try:
                failure = await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError as error:
                interruption = interruption or error
                if self._close_task.done():
                    raise interruption from None
            except BaseException:
                if interruption is not None:
                    raise interruption from None
                raise
        if interruption is not None:
            raise interruption
        if failure is not None:
            raise failure
