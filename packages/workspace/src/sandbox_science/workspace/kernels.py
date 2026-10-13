# SPDX-License-Identifier: MIT
"""Passive kernel observations for a recorded, authenticated workspace session.

No browser input selects a worker or environment. Bounded tasks own their registry
descriptors and requests through actual completion, including after a caller leaves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final, TypeVar

from aiohttp import hdrs, web
from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind, Identifier, KernelObservation
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, SessionSnapshot
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

MAX_CALLS: Final = 8
MAX_LOGIN_CALLS: Final = 2
CALL_TIMEOUT: Final = 6.0
MAX_RESPONSE_BYTES: Final = 8 * 1024
PATH: Final = "/api/v0/projects/{project}/sessions/{session}/kernels"
T = TypeVar("T")


class WorkspaceObservation(KernelObservation):
    """The observer response plus the server-owned project identity."""

    project_id: Identifier


class LoginEndedError(Exception):
    """The originally authenticated login was revoked before observation receipt."""


class KernelProxy:
    """Bounded passive proxy; close drains admitted work before the client may close."""

    def __init__(self, store: ProjectStore, worker: WorkerClient | None) -> None:
        self.store = store
        self.worker = worker
        self._files = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kernel-registry")
        self._tasks: set[asyncio.Task[str]] = set()
        self._scopes: set[tuple[str, str]] = set()
        self._logins: dict[str, int] = {}
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def _file(self, call: Callable[[], T]) -> T:
        future = asyncio.get_running_loop().run_in_executor(self._files, call)
        cancelled = False
        while True:
            try:
                value = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                # Canceling the wrapper cannot stop a running read or safely close
                # its descriptor. Keep its admission until the actual work returns.
                cancelled = True
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            if isinstance(value, SessionSnapshot):
                value.close()
            raise asyncio.CancelledError
        return value

    async def _observe(self, project: str, session: str, authorized: Callable[[], bool]) -> str:
        if not authorized():
            raise LoginEndedError
        snapshot = await self._file(lambda: self.store.session_snapshot(project, session))
        try:
            if not authorized():
                raise LoginEndedError
            worker = self.worker
            if worker is None:
                raise ProjectError(
                    ErrorKind.UNSUPPORTED, "kernel observations need a configured worker"
                )
            try:
                observed = await worker.observe_kernels(snapshot.context)
                # The trusted client checks its peer. Revalidate here too rather
                # than trusting a substituted object or an unvalidated model_copy.
                observed = KernelObservation.model_validate(observed.model_dump(), strict=True)
            except WorkerAPIError as error:
                kind = (
                    ErrorKind.UNSUPPORTED
                    if error.kind is ErrorKind.UNSUPPORTED
                    else ErrorKind.UNAVAILABLE
                )
                raise ProjectError(
                    kind, "kernel observation is unavailable or unsupported"
                ) from error
            except (ValidationError, ValueError, RecursionError) as error:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "kernel observation is invalid"
                ) from error
            if (observed.environment_id, observed.session_id) != (
                snapshot.context.environment_id,
                snapshot.context.session_id,
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "kernel observation has another scope")
            if not authorized():
                raise LoginEndedError

            def continuous() -> bool:
                try:
                    current = self.store.session_snapshot(project, session)
                except ProjectError:
                    return False
                try:
                    return snapshot.matches(current)
                finally:
                    current.close()

            matches = await self._file(continuous)
            if not authorized():
                raise LoginEndedError
            if not matches or self._closing or self.worker is not worker:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "session binding changed during observation"
                )
            answer = WorkspaceObservation(
                project_id=project, **observed.model_dump()
            ).model_dump_json()
            if len(answer.encode()) > MAX_RESPONSE_BYTES:
                raise ProjectError(ErrorKind.UNAVAILABLE, "kernel observation is too large")
            return answer
        finally:
            snapshot.close()

    async def observe(
        self, project: str, session: str, login: str, authorized: Callable[[], bool]
    ) -> str:
        """Read once under immediate admission; timeout retains unsettled ownership."""
        scope = project, session
        if (
            self._closing
            or len(self._tasks) >= MAX_CALLS
            or self._logins.get(login, 0) >= MAX_LOGIN_CALLS
            or scope in self._scopes
        ):
            raise ProjectError(ErrorKind.UNAVAILABLE, "kernel observation capacity is unavailable")
        # No await between reservation and owned-task publication.
        self._scopes.add(scope)
        self._logins[login] = self._logins.get(login, 0) + 1
        task = asyncio.create_task(self._observe(project, session, authorized))
        self._tasks.add(task)

        def finished(done: asyncio.Task[str]) -> None:
            self._tasks.discard(done)
            self._scopes.discard(scope)
            self._logins[login] -= 1
            if not self._logins[login]:
                del self._logins[login]
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                return await asyncio.shield(task)
        except TimeoutError:
            task.cancel()
            raise ProjectError(
                ErrorKind.UNAVAILABLE, "kernel observation timed out; cleanup is owned"
            ) from None
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _drain(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._files.shutdown(wait=True)
        self._closed = True

    async def close(self) -> None:
        """Stop admissions and own drain through repeated caller cancellation."""
        self._closing = True
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


PROXY: Final = web.AppKey("kernel_observations", KernelProxy)


def add_routes(
    app: web.Application,
    store: ProjectStore,
    worker: WorkerClient | None,
    principal: Callable[[web.Request], str],
    authorized: Callable[[web.Request], bool],
) -> None:
    """Register the sole passive route with app-owned login authority callbacks."""
    proxy = KernelProxy(store, worker)
    app[PROXY] = proxy

    async def observed(request: web.Request) -> web.Response:
        try:
            if request.query or request.can_read_body:
                raise ProjectError(
                    ErrorKind.INVALID_REQUEST, "kernel observation accepts only its path scope"
                )
            answer = await proxy.observe(
                request.match_info["project"],
                request.match_info["session"],
                principal(request),
                lambda: authorized(request),
            )
            if not authorized(request):
                raise LoginEndedError
            return web.Response(
                text=answer,
                content_type="application/json",
                headers={hdrs.CACHE_CONTROL: "no-store"},
            )
        except LoginEndedError:
            status, kind, message = 401, ErrorKind.POLICY_DENIED, "the workspace login has ended"
        except ProjectError as error:
            kind, message = error.kind, error.message
            status = {
                ErrorKind.INVALID_REQUEST: 400,
                ErrorKind.NOT_FOUND: 404,
                ErrorKind.UNSUPPORTED: 422,
            }.get(kind, 503)
            if not authorized(request):
                status, kind, message = (
                    401,
                    ErrorKind.POLICY_DENIED,
                    "the workspace login has ended",
                )
        return web.json_response(
            {"error": {"kind": kind.value, "message": message}},
            status=status,
            headers={hdrs.CACHE_CONTROL: "no-store"},
        )

    app.router.add_get(PATH, observed)
