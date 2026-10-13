# SPDX-License-Identifier: MIT
"""Authenticated selected-run ingress; canonical control DB proof remains trusted."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from aiohttp import web
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentState,
    ErrorKind,
    Event,
    ExecutionContext,
)
from sandbox_science.contracts.run_api import (
    MAX_RUN_API_REQUEST,
    MAX_RUN_API_RESPONSE,
    RunAttemptRequest,
    RunBindingRequest,
    RunCloseRequest,
    RunControlResult,
    RunEnrollmentRequest,
    RunEventPage,
    RunEventsRequest,
    RunRestartRequest,
    check_run_execution,
    strict_run_json,
)
from sandbox_science.contracts.run_cells import RunCellExecution, RunCellRequest, run_cell_bytes
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelEnrollment,
    RunKernelScope,
    run_kernel_binding_sha256,
)

from .environments import Environments
from .errors import WorkerError
from .manager import ExecutionManager, _Environment

# The worker Store is borrowed on its existing event-loop SQL owner. No thread,
# connection, executor or caller store is created/closed by this ingress.
# pyright: reportPrivateUsage=false
_EVENT = TypeAdapter[Event](Event)
_MODELS: dict[str, type[BaseModel]] = {
    "enroll": RunEnrollmentRequest,
    "submit": RunCellRequest,
    "get": RunAttemptRequest,
    "events": RunEventsRequest,
    "cancel": RunAttemptRequest,
    "observe": RunBindingRequest,
    "close": RunCloseRequest,
    "restart": RunRestartRequest,
}
MAX_OWNERS = 1024
MAX_OWNER_BYTES = 8 * 1024 * 1024
MAX_RECORD = 4096


def unavailable() -> WorkerError:
    return WorkerError(ErrorKind.UNAVAILABLE, "selected controller state is unavailable")


def managed_run_context(environments: Environments, context: ExecutionContext) -> None:
    """Selected-only bounded managed record read, without the legacy full reader."""
    records = environments.records
    with records._lock:
        row = records._db.execute(
            "SELECT CASE WHEN typeof(document)='text' AND length(CAST(document AS BLOB))<=? "
            "THEN CAST(document AS BLOB) END FROM environments WHERE environment_id=?",
            (MAX_RUN_API_RESPONSE, context.environment_id),
        ).fetchone()
    if row is None:
        raise WorkerError(ErrorKind.NOT_FOUND, "no selected managed environment")
    try:
        strict_run_json(row[0], MAX_RUN_API_RESPONSE)
        info = EnvironmentInfo.model_validate_json(row[0], strict=True)
        if info.environment_id != context.environment_id:
            raise ValueError
    except (ValueError, TypeError, ValidationError, RecursionError):
        raise unavailable() from None
    if info.project_id != context.project_id or info.state is EnvironmentState.DELETED:
        raise WorkerError(ErrorKind.NOT_FOUND, "no selected managed environment")


@dataclass(frozen=True, slots=True)
class RunPrincipal:
    """Private authenticated configuration identity, never returned in a DTO."""

    identity: str
    restart: bool


Authorize = Callable[[web.Request, ExecutionContext | None], RunPrincipal]


class RunAPI:
    """One bounded physical request lane, with retained original enrollment owners."""

    def __init__(self, manager: ExecutionManager, authorize: Authorize, *, enabled: bool) -> None:
        self.manager, self.authorize = manager, authorize
        self._closing = False
        self._active = 0
        self._principals: dict[str, int] = {}
        self._scopes: set[str] = set()
        self._work: set[asyncio.Task[web.Response]] = set()
        if enabled:
            # Hold selected ingress without taking down ordinary MAIN routes.
            # No automatic repair is attempted on this process or restart.
            with contextlib.suppress(WorkerError, sqlite3.Error):
                self._initialize()

    def _initialize(self) -> None:
        store, db = self.manager.store, self.manager.store._db
        with store._transaction():
            count = db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('run_api_meta','run_api_owners')"
            ).fetchone()[0]
            if count == 0:
                # Existing selections are prior-state evidence, never permission
                # to recreate a lost ingress registry under a different caller.
                if (
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='run_kernel_bindings'"
                    ).fetchone()
                    and db.execute("SELECT 1 FROM run_kernel_bindings LIMIT 1").fetchone()
                ):
                    raise unavailable()
                db.execute(
                    "CREATE TABLE run_api_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                    "ready INTEGER NOT NULL,count INTEGER NOT NULL) STRICT"
                )
                db.execute("INSERT INTO run_api_meta VALUES(1,0,0)")
                db.execute(
                    "CREATE TABLE run_api_owners(binding TEXT PRIMARY KEY,owner TEXT NOT NULL,"
                    "record TEXT NOT NULL) STRICT"
                )
            elif count != 2:
                raise unavailable()
            else:
                self._require()
                return
        # The committed incomplete marker deliberately holds a crash/partial
        # initialization. A later process must not repair it as fresh history.
        with store._transaction():
            self.manager.run_dispatcher.bindings._schema(db, initialize=True)
        self.manager.run_dispatcher.initialize()
        with store._transaction():
            db.execute("UPDATE run_api_meta SET ready=1 WHERE singleton=1 AND ready=0")
            self._require()

    def _require(self) -> None:
        db = self.manager.store._db
        try:
            if (
                db.execute(
                    "SELECT count(*) FROM (SELECT 1 FROM run_api_owners LIMIT 1025)"
                ).fetchone()[0]
                > MAX_OWNERS
            ):
                raise unavailable()
            meta = db.execute(
                "SELECT CASE WHEN typeof(singleton)='integer' THEN singleton END,"
                "CASE WHEN typeof(ready)='integer' THEN ready END,"
                "CASE WHEN typeof(count)='integer' THEN count END FROM run_api_meta LIMIT 2"
            ).fetchall()
            row = db.execute(
                "SELECT count(*),COALESCE(sum(length(CAST(binding AS BLOB))+"
                "length(CAST(owner AS BLOB))+length(CAST(record AS BLOB))),0),"
                "COALESCE(sum(typeof(binding)!='text' OR length(CAST(binding AS BLOB))!=64 "
                "OR typeof(owner)!='text' OR length(CAST(owner AS BLOB))!=64 "
                "OR typeof(record)!='text' OR length(CAST(record AS BLOB))>4096),0) "
                "FROM run_api_owners"
            ).fetchone()
            retained_bindings = db.execute(
                "SELECT count(*) FROM (SELECT 1 FROM run_kernel_bindings LIMIT 1025)"
            ).fetchone()[0]
            if (
                len(meta) != 1
                or any(type(v) is not int for v in meta[0])
                or meta[0][:2] != (1, 1)
                or any(type(v) is not int for v in row)
                or meta[0][2] != row[0]
                or row[0] != retained_bindings
                or not 0 <= row[0] <= MAX_OWNERS
                or not 0 <= row[1] <= MAX_OWNER_BYTES
                or row[2] != 0
            ):
                raise unavailable()
        except sqlite3.Error:
            raise unavailable() from None

    def _owner(self, binding: RunKernelBinding, principal: RunPrincipal) -> RunKernelEnrollment:
        self._require()
        row = self.manager.store._db.execute(
            "SELECT CAST(owner AS BLOB),substr(CAST(record AS BLOB),1,4097) "
            "FROM run_api_owners WHERE binding=?",
            (run_kernel_binding_sha256(binding),),
        ).fetchone()
        if row is None or row[0] != principal.identity.encode("ascii"):
            raise WorkerError(ErrorKind.NOT_FOUND, "no original controller selection")
        try:
            strict_run_json(row[1], MAX_RECORD)
            receipt = RunKernelEnrollment.model_validate_json(row[1], strict=True)
            if (
                receipt.binding != binding
                or self.manager.run_dispatcher.bindings.get(binding.scope) != receipt
            ):
                raise ValueError
            return receipt
        except (ValueError, TypeError, ValidationError, RecursionError):
            raise unavailable() from None

    def _live(self, scope: RunKernelScope) -> tuple[RunKernelBinding, _Environment, object]:
        m = self.manager
        env = m._environments.get(scope.environment_id)
        channel = env.channel if env is not None else None
        endpoint = m.run_dispatcher.bindings.endpoint(scope.environment_id)
        identity = m.daemon_identity(scope.environment_id) if m.daemon_identity else None
        if (
            self._closing
            or m._closing
            or env is None
            or channel is None
            or channel.closed
            or channel.detaching
            or env.lock.locked()
            or scope.environment_id in m._daemon_fences
            or scope.environment_id in m._quarantined
            or scope.environment_id in m._released_environments
            or scope.environment_id in m._recovering
            or (scope.project_id, scope.environment_id, scope.session_id) in m._closing_sessions
            or m.store.recycle_needed(scope.environment_id)
            or endpoint is None
            or identity != (endpoint[1], endpoint[2])
            or channel.incarnation != endpoint[0]
            or channel.daemon_token is None
        ):
            raise unavailable()
        if (
            type(channel.hello.get("run_kernel_protocol")) is not int
            or channel.hello.get("run_kernel_protocol") != 1
        ):
            raise WorkerError(ErrorKind.UNSUPPORTED, "worker peer lacks selected-run support")
        return (
            RunKernelBinding(
                scope=scope, incarnation=endpoint[0], sandbox=endpoint[1], sandbox_id=endpoint[2]
            ),
            env,
            channel,
        )

    def _enroll(
        self, value: RunEnrollmentRequest, principal: RunPrincipal, admitted: Callable[[], bool]
    ) -> RunKernelEnrollment:
        self._require()
        try:
            previous = self.manager.run_dispatcher.bindings.get(value.scope)
        except WorkerError as error:
            if error.kind is not ErrorKind.NOT_FOUND:
                raise
        else:
            receipt = self._owner(previous.binding, principal)
            if receipt.operation_id != value.operation_id:
                raise WorkerError(ErrorKind.CONFLICT, "original enrollment operation differs")
            return receipt  # historical receipt; it cannot repair or rebind a live endpoint
        binding, env, channel = self._live(value.scope)
        count = 0

        def validate() -> None:
            nonlocal count
            if admitted() is not True:
                raise unavailable()
            current, live_env, live_channel = self._live(value.scope)
            if current != binding or live_env is not env or live_channel is not channel:
                raise unavailable()
            count += 1
            if count == 2:
                receipt = self.manager.run_dispatcher.bindings.get(value.scope)
                db = self.manager.store._db
                db.execute(
                    "INSERT INTO run_api_owners VALUES(?,?,?)",
                    (
                        run_kernel_binding_sha256(binding),
                        principal.identity,
                        receipt.model_dump_json(),
                    ),
                )
                db.execute("UPDATE run_api_meta SET count=count+1 WHERE singleton=1")
                self._require()

        return self.manager.run_dispatcher.bindings.enroll(
            binding, value.operation_id, validate=validate
        )

    def _get(self, value: RunAttemptRequest) -> RunCellExecution:
        # Reject an oversized/corrupt execution before the older Store reader
        # can materialize its larger general execution allowance.
        row = self.manager.store._db.execute(
            "SELECT typeof(record)='text' AND length(CAST(record AS BLOB))<=? "
            "FROM executions WHERE execution_id=?",
            (MAX_RUN_API_RESPONSE, value.execution_id),
        ).fetchone()
        if row is not None and row != (1,):
            raise unavailable()
        if row is None:
            raise WorkerError(ErrorKind.NOT_FOUND, "no original selected attempt")
        result = self.manager.run_dispatcher.get(
            value.binding, value.execution_id, value.attempt_id
        )
        try:
            check_run_execution(result)
        except ValueError:
            raise unavailable() from None
        return result

    def _events(self, value: RunEventsRequest) -> RunEventPage:
        selected = self._get(value)
        db = self.manager.store._db
        last = db.execute(
            "SELECT CASE WHEN typeof(seq)='integer' AND seq BETWEEN 1 AND 4611686018427387904 "
            "THEN seq END FROM events WHERE execution_id=? ORDER BY seq DESC LIMIT 1",
            (value.execution_id,),
        ).fetchone()
        if last is not None and type(last[0]) is not int:
            raise unavailable()
        highest = last[0] if last is not None else 0
        if selected.execution.last_seq > highest:
            raise unavailable()
        if value.after > highest:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "selected event cursor is ahead")
        events: list[Event] = []
        after = value.after
        with contextlib.closing(
            db.execute(
                "SELECT seq,CASE WHEN typeof(event)='text' THEN "
                "substr(CAST(event AS BLOB),1,?) END FROM events "
                "WHERE execution_id=? AND seq>? ORDER BY seq LIMIT ?",
                (MAX_RUN_API_RESPONSE + 1, value.execution_id, after, value.limit),
            )
        ) as rows:
            for sequence, raw in rows:
                try:
                    strict_run_json(raw, MAX_RUN_API_RESPONSE)
                    event = _EVENT.validate_json(raw, strict=True)
                    page = RunEventPage(
                        selected=selected,
                        after=value.after,
                        next_after=sequence,
                        last_seq=highest,
                        events=(*events, event),
                    )
                except (ValueError, TypeError, ValidationError, RecursionError):
                    raise unavailable() from None
                if len(page.model_dump_json().encode()) > MAX_RUN_API_RESPONSE:
                    if not events:
                        raise unavailable()
                    break
                events.append(event)
                after = sequence
        return RunEventPage(
            selected=selected,
            after=value.after,
            next_after=after,
            last_seq=highest,
            events=tuple(events),
        )

    async def handle(self, request: web.Request) -> web.Response:
        principal = self.authorize(request, None)  # permission before body parsing
        if self._closing or self._active >= 8 or self._principals.get(principal.identity, 0) >= 2:
            raise unavailable()
        self._active += 1
        self._principals[principal.identity] = self._principals.get(principal.identity, 0) + 1
        withdrawn = False
        scope_key: str | None = None

        def admitted(context: ExecutionContext) -> bool:
            return (
                not withdrawn
                and not self._closing
                and self.authorize(request, context) == principal
            )

        async def actual() -> web.Response:
            nonlocal scope_key
            if request.query:
                raise WorkerError(ErrorKind.INVALID_REQUEST, "selected routes take no query")
            action = request.match_info["action"]
            model = _MODELS[action]
            body = bytearray()
            async with asyncio.timeout(5):
                async for chunk in request.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_RUN_API_REQUEST:
                        raise WorkerError(
                            ErrorKind.INVALID_REQUEST, "selected request exceeds its bound"
                        )
            try:
                raw = bytes(body)
                strict_run_json(raw, MAX_RUN_API_REQUEST)
                value = model.model_validate_json(raw, strict=True)
                if type(value) is RunCellRequest:
                    run_cell_bytes(value)
                scope = (
                    cast(RunEnrollmentRequest, value).scope
                    if action == "enroll"
                    else cast(RunBindingRequest, value).binding.scope
                )
                context = ExecutionContext(
                    project_id=scope.project_id,
                    session_id=scope.session_id,
                    environment_id=scope.environment_id,
                )
            except (ValueError, TypeError, ValidationError, RecursionError):
                raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid selected request") from None
            if (
                admitted(context) is not True
                or request.transport is None
                or request.transport.is_closing()
            ):
                raise unavailable()
            scope_key = json.dumps(scope.model_dump(mode="json"), sort_keys=True)
            if scope_key in self._scopes:
                scope_key = None
                raise unavailable()
            self._scopes.add(scope_key)

            def callback() -> bool:
                return admitted(context)

            if action == "enroll":
                result: BaseModel = self._enroll(
                    cast(RunEnrollmentRequest, value), principal, callback
                )
                status = 200
            else:
                binding = cast(RunBindingRequest, value).binding
                self._owner(binding, principal)

                def selected_admission() -> bool:
                    if callback() is not True:
                        return False
                    self._owner(binding, principal)
                    return True

                dispatcher = self.manager.run_dispatcher
                status = 200
                if action == "submit":
                    result, created = await dispatcher.submit(
                        cast(RunCellRequest, value), admitted=selected_admission
                    )
                    status = 201 if created else 200
                elif action == "get":
                    result = self._get(cast(RunAttemptRequest, value))
                elif action == "events":
                    result = self._events(cast(RunEventsRequest, value))
                elif action == "cancel":
                    original = cast(RunAttemptRequest, value)
                    self._get(original)
                    result = await dispatcher.cancel(
                        binding,
                        original.execution_id,
                        original.attempt_id,
                        admitted=selected_admission,
                    )
                elif action == "observe":
                    result = await dispatcher.observe(binding)
                elif action == "restart":
                    if not principal.restart:
                        raise WorkerError(
                            ErrorKind.POLICY_DENIED, "selected restart is not granted"
                        )
                    restart = cast(RunRestartRequest, value)
                    result = RunControlResult.model_validate_json(
                        (
                            await dispatcher.restart(
                                binding,
                                restart.operation_id,
                                restart.language,
                                admitted=selected_admission,
                            )
                        ).model_dump_json(),
                        strict=True,
                    )
                else:
                    close = cast(RunCloseRequest, value)
                    result = RunControlResult.model_validate_json(
                        (
                            await dispatcher.close(
                                binding, close.operation_id, admitted=selected_admission
                            )
                        ).model_dump_json(),
                        strict=True,
                    )
                if selected_admission() is not True:
                    raise unavailable()
                if isinstance(result, RunControlResult) and (
                    result.request.binding != binding
                    or result.request.operation_id != cast(RunCloseRequest, value).operation_id
                    or result.request.action != action
                    or result.request.language
                    != (cast(RunRestartRequest, value).language if action == "restart" else None)
                ):
                    raise unavailable()
            payload = result.model_dump_json()
            if len(payload.encode()) > MAX_RUN_API_RESPONSE:
                raise unavailable()
            return web.json_response(
                text=payload, status=status, headers={"Cache-Control": "no-store"}
            )

        task = asyncio.create_task(actual())
        self._work.add(task)
        first: asyncio.CancelledError | None = None
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    first = first or error
                    withdrawn = True
                except BaseException:
                    # The owned task has settled. Classify its failure below,
                    # or preserve a prior caller interruption, after draining.
                    break
            if first is not None:
                with contextlib.suppress(BaseException):
                    task.result()
                raise first
            try:
                return task.result()
            except (TimeoutError, sqlite3.Error, ValidationError):
                raise unavailable() from None
        finally:
            self._work.discard(task)
            if scope_key is not None:
                self._scopes.discard(scope_key)
            self._active -= 1
            remaining = self._principals[principal.identity] - 1
            if remaining:
                self._principals[principal.identity] = remaining
            else:
                self._principals.pop(principal.identity)

    async def close(self) -> None:
        """Withdraw new/queued admission and retain owners until actual work drains."""
        self._closing = True
        work = asyncio.gather(*tuple(self._work), return_exceptions=True)
        first: asyncio.CancelledError | None = None
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError as error:
                first = first or error
        work.result()
        if first is not None:
            raise first


RUN_API = web.AppKey("run_api", RunAPI)


def register_run_api(app: web.Application, api: RunAPI) -> None:
    """Register controller routes and actual owned cleanup; never a legacy fallback."""
    app[RUN_API] = api
    app.router.add_post(
        "/v1/run-kernels/{action:enroll|submit|get|events|cancel|observe|close|restart}", api.handle
    )

    async def close(_: web.Application) -> None:
        await api.close()

    app.on_cleanup.append(close)
