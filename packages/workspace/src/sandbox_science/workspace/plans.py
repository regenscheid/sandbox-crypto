# SPDX-License-Identifier: MIT
"""Read-only, cancellation-owned compact plan views for recorded workspace sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Final, Self, TypeVar

from aiohttp import hdrs, web
from pydantic import AwareDatetime, ConfigDict, TypeAdapter, ValidationError, model_validator

from sandbox_science.contracts import Contract, ErrorKind, ExecutionContext, Identifier
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.projects import (
    MAX_SESSION_RECORD_BYTES,
    ProjectError,
    ProjectRecord,
    ProjectStore,
    SessionSnapshot,
)
from sandbox_science.orchestrator.session_plans import (
    CompactPlan,
    PlanAuthority,
    PlanScope,
    SessionPlans,
)

from .api import TIMEOUTS, Timeouts

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/plan"
ROUTE_NAME: Final = "workspace_session_plan"
MAX_CALLS: Final = 8
MAX_LOGIN_CALLS: Final = 2
MAX_RESPONSE_BYTES: Final = 256 * 1024
_IDENTIFIER: Final = TypeAdapter(Identifier)
T = TypeVar("T")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def session_snapshot(store: ProjectStore, project: str, session: str) -> SessionSnapshot:
    """Bounded allow-closed registry read, retaining exact file/root inode ownership."""
    root = descriptor = -1
    try:
        _IDENTIFIER.validate_python(project, strict=True)
        _IDENTIFIER.validate_python(session, strict=True)
        root = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            project + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= MAX_SESSION_RECORD_BYTES:
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry is not a bounded record")
        data = bytearray()
        while len(data) <= MAX_SESSION_RECORD_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_SESSION_RECORD_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if (
            len(data) > MAX_SESSION_RECORD_BYTES
            or len(data) != before.st_size
            or _identity(os.fstat(descriptor)) != _identity(before)
        ):
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry changed during read")
        record = ProjectRecord.model_validate_json(data, strict=True)
        found = record.sessions.get(session)
        if record.project_id != project or (found is not None and found.session_id != session):
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry has inconsistent scope")
        if found is None:
            raise ProjectError(ErrorKind.NOT_FOUND, "no recorded session in this project")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (found.opened_at, found.closed_at)
            if value is not None
        ) or (found.closed_at is not None and found.closed_at < found.opened_at):
            raise ProjectError(ErrorKind.UNAVAILABLE, "session registry time is invalid")
        if not found.dedicated and found.environment_id != record.default_environment_id:
            raise ProjectError(ErrorKind.UNAVAILABLE, "session registry binding is inconsistent")
        result = SessionSnapshot(
            descriptor,
            root,
            _identity(before),
            hashlib.sha256(data).digest(),
            ExecutionContext(
                project_id=project, session_id=session, environment_id=found.environment_id
            ),
            found,
        )
        descriptor = root = -1
        return result
    except FileNotFoundError:
        raise ProjectError(ErrorKind.NOT_FOUND, "no recorded project") from None
    except (OSError, ValidationError, ValueError, RecursionError) as error:
        raise ProjectError(ErrorKind.UNAVAILABLE, "project registry is unreadable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if root >= 0:
            os.close(root)


class WorkspacePlan(Contract):
    """Exact compact control-state snapshot; status and evidence remain assertions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project_id: Identifier
    session_id: Identifier
    observed_at: AwareDatetime
    session_closed_at: AwareDatetime | None
    plan: CompactPlan | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        plan = self.plan
        if plan is None:
            return self
        scope = PlanScope(project_id=self.project_id, session_id=self.session_id)
        if (
            plan.head.scope != scope
            or plan.version.scope != scope
            or plan.version.author.scope != scope
            or plan.head.ref != plan.version.ref
            or plan.version.base
            != (None if plan.version.ref.version == 1 else plan.version.ref.version - 1)
        ):
            raise ValueError("compact plan scope/version is incoherent")
        if plan.head.outcome is not None and any(
            evidence.project_id != self.project_id for evidence in plan.head.outcome.evidence
        ):
            raise ValueError("compact outcome evidence names another project")
        expected = tuple(step for phase in plan.version.body.phases for step in phase.steps)
        if tuple(step.step_id for step in expected) != tuple(step.step_id for step in plan.steps):
            raise ValueError("compact plan omits or duplicates current steps")
        # This is the core's version-1 current-step hash domain. It binds display
        # status to exact body fields rather than trusting an unvalidated copy.
        fingerprints = {
            step.step_id: hashlib.sha256(
                json.dumps(
                    {"phase_id": phase.phase_id, "step": step.model_dump(mode="json")},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode()
            ).hexdigest()
            for phase in plan.version.body.phases
            for step in phase.steps
        }
        for step in plan.steps:
            latest = step.latest
            if step.step_sha256 != fingerprints[step.step_id] or step.matches_current_step != (
                latest is not None and latest.step_sha256 == step.step_sha256
            ):
                raise ValueError("compact current-step fingerprint is incoherent")
            if latest is not None and (
                latest.author.scope != scope
                or latest.ref.plan_id != plan.head.ref.plan_id
                or latest.ref.version > plan.head.ref.version
                or latest.sequence > plan.head.progress_cursor
            ):
                raise ValueError("compact assertion scope/cursor is incoherent")
            if plan.head.status == "completed" and (
                not step.matches_current_step
                or latest is None
                or latest.status not in {"completed", "blocked", "skipped"}
                or (
                    plan.head.outcome is not None
                    and plan.head.outcome.status == "achieved"
                    and latest.status != "completed"
                )
            ):
                raise ValueError("compact completion does not match current step assertions")
        return self


class LoginEndedError(Exception):
    """The original request's authenticated cookie was revoked before receipt."""


class PlanReader:
    """Bounded private read owner; no writable store or worker authority is retained."""

    def __init__(
        self, projects: ProjectStore, canonical: DocumentStore, *, timeout: float = TIMEOUTS.call
    ) -> None:
        self.projects = projects
        self.timeout = timeout
        # Even injected writable app stores are never used to initialize plan SQL.
        self.documents = DocumentStore(canonical.path, producer="workspace-plans", readonly=True)
        self._files = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-view")
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

    def _plan(self, scope: PlanScope, login: str) -> CompactPlan | None:
        try:
            plans = SessionPlans(self.documents)
        except DocumentError as error:
            # Only constructor's wholly absent namespace means no plan. Missing
            # canonical records during compact_current are damaged state instead.
            if error.kind == "not_found":
                return None
            raise
        result = plans.compact_current(PlanAuthority(scope=scope, role="user", user_id=login))
        if result is None:
            with self.documents._read() as db:  # pyright: ignore[reportPrivateUsage]
                if (
                    db is not None
                    and db.execute(
                        "SELECT 1 FROM session_plan_records WHERE project=? AND session=? "
                        "UNION ALL SELECT 1 FROM session_plan_projections WHERE "
                        "project=? AND session=? LIMIT 1",
                        (scope.project_id, scope.session_id, scope.project_id, scope.session_id),
                    ).fetchone()
                    is not None
                ):
                    raise ProjectError(ErrorKind.UNAVAILABLE, "session plan pointer is missing")
        return result

    async def _read(
        self, project: str, session: str, login: str, authorized: Callable[[], bool]
    ) -> str:
        if not authorized():
            raise LoginEndedError
        snapshot = await self._file(lambda: session_snapshot(self.projects, project, session))
        try:
            if not authorized():
                raise LoginEndedError
            try:
                plan = await self._file(
                    lambda: self._plan(PlanScope(project_id=project, session_id=session), login)
                )
            except (
                DocumentError,
                sqlite3.Error,
                OSError,
                ValidationError,
                ValueError,
                RecursionError,
            ) as error:
                raise ProjectError(ErrorKind.UNAVAILABLE, "session plan is unavailable") from error
            if not authorized():
                raise LoginEndedError

            def continuous() -> bool:
                try:
                    current = session_snapshot(self.projects, project, session)
                except ProjectError:
                    return False
                try:
                    return snapshot.matches(current)
                finally:
                    current.close()

            matches = await self._file(continuous)
            if not authorized():
                raise LoginEndedError
            if not matches or self._closing:
                raise ProjectError(ErrorKind.UNAVAILABLE, "session changed during plan read")
            try:
                result = WorkspacePlan(
                    project_id=project,
                    session_id=session,
                    observed_at=datetime.now(UTC),
                    session_closed_at=snapshot.session.closed_at,
                    plan=plan,
                )
                # Pydantic instances may have bypassed validation via model_copy.
                result = WorkspacePlan.model_validate(result.model_dump(), strict=True)
                answer = result.model_dump_json()
            except (ValidationError, ValueError, RecursionError) as error:
                raise ProjectError(ErrorKind.UNAVAILABLE, "session plan is invalid") from error
            if len(answer.encode()) > MAX_RESPONSE_BYTES:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "session plan exceeds the view byte limit"
                )
            return answer
        finally:
            snapshot.close()

    async def read(
        self, project: str, session: str, login: str, authorized: Callable[[], bool]
    ) -> str:
        """Read once; caller timeout retains ownership until actual file/SQL drain."""
        try:
            _IDENTIFIER.validate_python(project, strict=True)
            _IDENTIFIER.validate_python(session, strict=True)
            _IDENTIFIER.validate_python(login, strict=True)
        except ValidationError as error:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid session plan scope") from error
        scope = project, session
        if (
            self._closing
            or len(self._tasks) >= MAX_CALLS
            or self._logins.get(login, 0) >= MAX_LOGIN_CALLS
            or scope in self._scopes
        ):
            raise ProjectError(ErrorKind.UNAVAILABLE, "session plan capacity is unavailable")
        self._scopes.add(scope)
        self._logins[login] = self._logins.get(login, 0) + 1
        task = asyncio.create_task(self._read(project, session, login, authorized))
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
                ErrorKind.UNAVAILABLE, "session plan timed out; cleanup remains owned"
            ) from None
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _drain(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self.documents.close()
        self._files.shutdown(wait=True)
        self._closed = True

    async def close(self) -> None:
        """Stop admissions and drain actual owned work before closing the readonly store."""
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


READER: Final = web.AppKey("session_plan_reader", PlanReader)


def add_routes(
    app: web.Application,
    projects: ProjectStore,
    canonical: DocumentStore,
    principal: Callable[[web.Request], str],
    authorized: Callable[[web.Request], bool],
    timeouts: Timeouts = TIMEOUTS,
) -> None:
    """Register an authenticated read only; no plans schema initialization or writer."""
    reader = PlanReader(projects, canonical, timeout=timeouts.call)
    app[READER] = reader

    async def read(request: web.Request) -> web.Response:
        try:
            if request.query or request.can_read_body:
                raise ProjectError(
                    ErrorKind.INVALID_REQUEST, "session plan read accepts only path scope"
                )
            answer = await reader.read(
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
            status = (
                404
                if error.kind is ErrorKind.NOT_FOUND
                else 400
                if error.kind is ErrorKind.INVALID_REQUEST
                else 503
            )
            kind, message = error.kind, error.message
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

    app.router.add_get(PATH, read, name=ROUTE_NAME)
