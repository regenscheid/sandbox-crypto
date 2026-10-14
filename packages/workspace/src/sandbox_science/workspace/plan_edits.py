# SPDX-License-Identifier: MIT
"""Explicit USER plan-body revisions with owned admission and actual SQL lifetime."""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Final, Self, TypeVar

from aiohttp import hdrs, web
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, SessionSnapshot
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanModel,
    PlanReceipt,
    PlanRef,
    PlanScope,
    SessionPlans,
)

from .api import TIMEOUTS, Timeouts
from .plans import PATH, LoginEndedError, session_snapshot

ROUTE_NAME: Final = "workspace_session_plan_change"
MAX_REQUEST_BYTES: Final = 128 * 1024
MAX_RESPONSE_BYTES: Final = 8 * 1024
MAX_CALLS: Final = 8
MAX_LOGIN_CALLS: Final = 2
_IDENTIFIER: Final = TypeAdapter(Identifier)
T = TypeVar("T")


class BodyChange(PlanModel):
    """Body CAS only. USER identity, scope and all workflow authority are external."""

    operation_id: Identifier
    base: PlanRef
    body: PlanBody
    reason: str = Field(min_length=1, max_length=4096)


class EditReceipt(PlanModel):
    """A historical mutation acknowledgement, never a replacement current snapshot."""

    project_id: Identifier
    session_id: Identifier
    receipt: PlanReceipt

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if (
            self.receipt.scope != PlanScope(project_id=self.project_id, session_id=self.session_id)
            or self.receipt.action != "revise"
        ):
            raise ValueError("incoherent plan edit receipt")
        return self


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class PlanEditor:
    """Finite request owner; cancellation cannot abandon admitted file/SQL work."""

    def __init__(
        self, projects: ProjectStore, canonical: DocumentStore, *, timeout: float = TIMEOUTS.call
    ) -> None:
        self.projects = projects
        self.timeout = timeout
        # Only this lane uses the connection. A busy SQL lock refuses immediately;
        # no hidden wait separates final authentication from transaction admission.
        self.documents = DocumentStore(canonical.path, producer="workspace-plan-edit", timeout=0)
        self._files = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-edit")
        self._tasks: set[asyncio.Task[str]] = set()
        self._scopes: set[tuple[str, str]] = set()
        self._logins: dict[str, int] = {}
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._database = self._database_root = -1
        self._plans: SessionPlans | None = None

    async def _work(self, call: Callable[[], T], cancelled: threading.Event) -> T:
        future = asyncio.get_running_loop().run_in_executor(self._files, call)
        interrupted = False
        while True:
            try:
                value = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                interrupted = True
                cancelled.set()
            except BaseException:
                if interrupted:
                    raise asyncio.CancelledError from None
                raise
        if interrupted:
            raise asyncio.CancelledError
        return value

    def _database_matches(self) -> bool:
        path = self.documents.path
        return (
            _inode(os.stat(path.parent, follow_symlinks=False))
            == _inode(os.fstat(self._database_root))
            and _inode(os.stat(path.name, dir_fd=self._database_root, follow_symlinks=False))
            == _inode(os.fstat(self._database))
            and stat.S_ISREG(os.fstat(self._database).st_mode)
        )

    def _existing(self, authority: PlanAuthority) -> None:
        """An edit cannot bootstrap an absent plan namespace or hide a lost pointer."""
        inspector = DocumentStore(self.documents.path, readonly=True, timeout=0)
        try:
            plans = SessionPlans(inspector)
            if plans.summary(authority) is not None:
                return
            with inspector._read() as db:  # pyright: ignore[reportPrivateUsage]
                if (
                    db is not None
                    and db.execute(
                        "SELECT 1 FROM session_plan_records WHERE project=? AND session=? "
                        "UNION ALL SELECT 1 FROM session_plan_projections WHERE project=? "
                        "AND session=? LIMIT 1",
                        (authority.scope.project_id, authority.scope.session_id) * 2,
                    ).fetchone()
                    is not None
                ):
                    raise ProjectError(ErrorKind.UNAVAILABLE, "session plan is damaged")
            raise ProjectError(ErrorKind.NOT_FOUND, "this session has no plan to edit")
        finally:
            inspector.close()

    def _bind_database(self, authority: PlanAuthority) -> None:
        if self._database >= 0:
            if not self._database_matches():
                raise ProjectError(ErrorKind.UNAVAILABLE, "control store identity changed")
            self._existing(authority)
            return
        root = descriptor = -1
        try:
            path = self.documents.path
            root = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProjectError(ErrorKind.UNAVAILABLE, "control store is not a regular file")
            self._database, self._database_root = descriptor, root
            descriptor = root = -1
            self._existing(authority)
            self._plans = SessionPlans(self.documents)
            if not self._database_matches():
                raise ProjectError(ErrorKind.UNAVAILABLE, "control store identity changed")
        except BaseException:
            self.documents.close()
            self._close_database_fds()
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if root >= 0:
                os.close(root)

    def _close_database_fds(self) -> None:
        for name in ("_database", "_database_root"):
            descriptor = getattr(self, name)
            setattr(self, name, -1)
            if descriptor >= 0:
                os.close(descriptor)
        self._plans = None

    def _current(self, original: SessionSnapshot) -> None:
        current = session_snapshot(
            self.projects, original.context.project_id, original.context.session_id
        )
        try:
            if current.session.closed_at is not None or not original.matches(current):
                raise ProjectError(ErrorKind.CONFLICT, "the session changed during plan editing")
        finally:
            current.close()

    def _change(
        self,
        project: str,
        session: str,
        login: str,
        wanted: BodyChange,
        authorized: Callable[[], bool],
        cancelled: threading.Event,
    ) -> str:
        if not authorized():
            raise LoginEndedError
        if cancelled.is_set() or self._closing:
            raise ProjectError(ErrorKind.UNAVAILABLE, "plan edit was not admitted")
        original = session_snapshot(self.projects, project, session)
        lock = -1
        try:
            if original.session.closed_at is not None:
                raise ProjectError(ErrorKind.CONFLICT, "a closed session cannot be edited")
            # The same canonical lock name as ProjectStore, without following a
            # special file or queuing on another process's lifecycle operation.
            lock = os.open(
                project + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=original._root,  # pyright: ignore[reportPrivateUsage]
            )
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise ProjectError(ErrorKind.UNAVAILABLE, "project lock is not a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._current(original)
            authority = PlanAuthority(
                scope=PlanScope(project_id=project, session_id=session), role="user", user_id=login
            )
            self._bind_database(authority)
            self._current(original)
            if not self._database_matches() or _inode(os.fstat(lock)) != _inode(
                os.stat(
                    project + ".lock",
                    dir_fd=original._root,  # pyright: ignore[reportPrivateUsage]
                    follow_symlinks=False,
                )
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "control edit binding changed")
            # This is the final write admission. Logout/disconnect after here is
            # not rollback. Private connection + timeout0 avoids a further queue.
            if not authorized():
                raise LoginEndedError
            if cancelled.is_set() or self._closing:
                raise ProjectError(ErrorKind.UNAVAILABLE, "plan edit was not admitted")
            assert self._plans is not None  # noqa: S101 - binding constructed it
            receipt = self._plans.revise(
                authority, wanted.operation_id, wanted.base, wanted.body, wanted.reason
            )
            try:
                self._current(original)
                if not self._database_matches():
                    raise ValueError("control store changed after editing")
            except Exception:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "edit committed; its response binding is unavailable"
                ) from None
            try:
                result = EditReceipt(project_id=project, session_id=session, receipt=receipt)
                result = EditReceipt.model_validate(result.model_dump(), strict=True)
            except ValidationError:
                raise ProjectError(ErrorKind.UNAVAILABLE, "plan edit receipt is invalid") from None
            if (
                result.receipt.operation_id != wanted.operation_id
                or result.receipt.ref.plan_id != wanted.base.plan_id
                or result.receipt.ref.version != wanted.base.version + 1
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "plan edit receipt is inconsistent")
            answer = result.model_dump_json()
            if len(answer.encode()) > MAX_RESPONSE_BYTES:
                raise ProjectError(ErrorKind.UNAVAILABLE, "plan edit receipt exceeds its bound")
            return answer
        finally:
            if lock >= 0:
                os.close(lock)
            original.close()

    async def _perform(
        self,
        project: str,
        session: str,
        login: str,
        read: Callable[[], Awaitable[bytes]],
        authorized: Callable[[], bool],
    ) -> str:
        raw = await read()
        if len(raw) > MAX_REQUEST_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST_BYTES, actual_size=len(raw))
        wanted = BodyChange.model_validate_json(raw, strict=True)
        if not authorized():
            raise LoginEndedError
        cancelled = threading.Event()
        return await self._work(
            lambda: self._change(project, session, login, wanted, authorized, cancelled),
            cancelled,
        )

    async def change(
        self,
        project: str,
        session: str,
        login: str,
        read: Callable[[], Awaitable[bytes]],
        authorized: Callable[[], bool],
    ) -> str:
        """Own one complete bounded request, including its body and actual thread."""
        for value in (project, session, login):
            _IDENTIFIER.validate_python(value, strict=True)
        if not authorized():
            raise LoginEndedError
        scope = project, session
        if (
            self._closing
            or len(self._tasks) >= MAX_CALLS
            or self._logins.get(login, 0) >= MAX_LOGIN_CALLS
            or scope in self._scopes
        ):
            raise ProjectError(ErrorKind.UNAVAILABLE, "plan edit capacity is unavailable")
        self._scopes.add(scope)
        self._logins[login] = self._logins.get(login, 0) + 1
        task = asyncio.create_task(self._perform(project, session, login, read, authorized))
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
            async with asyncio.timeout(self.timeout):
                return await asyncio.shield(task)
        except TimeoutError:
            task.cancel()
            raise ProjectError(
                ErrorKind.UNAVAILABLE, "plan edit timed out; its outcome may be unknown"
            ) from None
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _drain(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        def close() -> None:
            try:
                self.documents.close()
            finally:
                self._close_database_fds()

        try:
            await self._work(close, threading.Event())
        finally:
            self._files.shutdown(wait=True)
            self._closed = True

    async def close(self) -> None:
        """Stop admission; actual work and descriptors drain even on repeated cancel."""
        self._closing = True
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        interrupted = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                interrupted = True
            except BaseException:
                if interrupted:
                    raise asyncio.CancelledError from None
                raise
        if interrupted:
            raise asyncio.CancelledError


EDITOR: Final = web.AppKey("session_plan_editor", PlanEditor)


def add_routes(
    app: web.Application,
    projects: ProjectStore,
    canonical: DocumentStore,
    principal: Callable[[web.Request], str],
    authorized: Callable[[web.Request], bool],
    timeouts: Timeouts = TIMEOUTS,
) -> None:
    """Add only a USER body edit. Creation, status/progress and approval are absent."""
    editor = PlanEditor(projects, canonical, timeout=timeouts.call)
    app[EDITOR] = editor

    async def change(request: web.Request) -> web.Response:
        def live() -> bool:
            transport = request.transport
            return authorized(request) and transport is not None and not transport.is_closing()

        def error(
            status: int, kind: str, message: str, current: PlanRef | None = None
        ) -> web.Response:
            value: dict[str, object] = {"kind": kind, "message": message}
            if current is not None:
                value["current"] = current.model_dump(mode="json")
            return web.json_response(
                {"error": value}, status=status, headers={hdrs.CACHE_CONTROL: "no-store"}
            )

        try:
            if request.query or request.content_type != "application/json":
                return error(400, "invalid_request", "plan edit needs path scope and JSON")
            answer = await editor.change(
                request.match_info["project"],
                request.match_info["session"],
                principal(request),
                request.clone(client_max_size=MAX_REQUEST_BYTES).read,
                live,
            )
            if not authorized(request):
                raise LoginEndedError
            return web.Response(
                text=answer,
                content_type="application/json",
                headers={hdrs.CACHE_CONTROL: "no-store"},
            )
        except web.HTTPRequestEntityTooLarge:
            return error(413, "invalid_request", "plan edit request exceeds its byte limit")
        except LoginEndedError:
            return error(401, "policy_denied", "login ended; an admitted edit may have committed")
        except ValidationError:
            return error(400, "invalid_request", "invalid plan edit fields or text")
        except DocumentError as caught:
            status = {
                "invalid_request": 400,
                "policy_denied": 403,
                "not_found": 404,
                "conflict": 409,
            }.get(caught.kind, 503)
            current = None
            if caught.kind == "conflict":
                with suppress(ValueError, TypeError):
                    current = PlanRef.model_validate(caught.details.get("current"), strict=True)
            return error(
                status, caught.kind, "plan edit refused; reread the recorded plan", current
            )
        except ProjectError as caught:
            status = {
                ErrorKind.NOT_FOUND: 404,
                ErrorKind.CONFLICT: 409,
                ErrorKind.INVALID_REQUEST: 400,
            }.get(caught.kind, 503)
            return error(status, caught.kind.value, "plan edit scope or capacity is unavailable")
        except Exception:
            # Do not log request text, SQLite values, or arbitrary exception bodies.
            return error(503, "unavailable", "plan edit unavailable; its outcome may be unknown")

    app.router.add_post(PATH + "/changes", change, name=ROUTE_NAME)
