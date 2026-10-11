# SPDX-License-Identifier: MIT
"""Executions: submission, dispatch to executors, events, cancellation and files.

docs/specs/tools.md defines the behavior. Every execution is persisted before
it is dispatched, and every event before it is delivered, so a client can
reconnect and replay, and a restarted worker reports unfinished attempts as
``lost`` instead of running them again.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import mimetypes
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ArtifactRef,
    Capabilities,
    CellRequest,
    ErrorInfo,
    ErrorKind,
    Event,
    Execution,
    ExecutionState,
    FileContent,
    FileEntry,
    FileListing,
    KernelInfo,
    KernelRestartRequest,
    ListFilesRequest,
    Location,
    OutputEvent,
    PublishRequest,
    ReadFileRequest,
    Result,
    ResultEvent,
    RichOutput,
    ShellRequest,
    StateEvent,
    StreamEvent,
    StreamOutput,
    WriteFileRequest,
    WriteFileResult,
    new_id,
)
from sandbox_science.contracts.worker import LANGUAGES, MAX_READ_BYTES, PROTOCOL_VERSION

from .artifacts import Blobs
from .backends import Backend
from .channel import ExecutorChannel, Message
from .errors import WorkerError
from .isolation import probe
from .store import KernelRecord, Store

_log = logging.getLogger(__name__)

WATCHDOG_GRACE = 30.0
"""Seconds past an execution's own limit before the worker cancels it itself."""
LOST_GRACE = 60.0
"""Seconds after that cancel before the worker gives the attempt up as lost."""
RETAIN_LIVE = 600.0
"""Seconds a finished execution stays in memory for clients still streaming it."""


def _now() -> datetime:
    return datetime.now(UTC)


def _epoch(value: object) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    return None


@dataclass
class _Live:
    execution: Execution
    environment_id: str
    changed: asyncio.Future[None]
    cancel_requested: bool = False
    dispatched: bool = False
    watchdog: asyncio.Task[None] | None = None


@dataclass
class _Environment:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    channel: ExecutorChannel | None = None
    location: Location | None = None
    languages: tuple[str, ...] = ()
    running: set[str] = field(default_factory=set[str])


class ExecutionManager:
    def __init__(
        self,
        store: Store,
        backend: Backend,
        blobs: Blobs,
        *,
        pool_id: str,
        max_artifact_size: int,
        state_dir: Path,
    ) -> None:
        self.store = store
        self.backend = backend
        self.blobs = blobs
        self.pool_id = pool_id
        self.max_artifact_size = max_artifact_size
        # The isolation probe's sentinel goes here, among the worker's own files.
        self.state_dir = state_dir
        self._live: dict[str, _Live] = {}
        self._environments: dict[str, _Environment] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Report attempts a previous run left unfinished as lost."""
        for execution in self.store.unfinished():
            live = self._track(execution)
            self._finish(
                live,
                ExecutionState.LOST,
                Result(
                    error=ErrorInfo(
                        kind=ErrorKind.INTERNAL,
                        message="the worker restarted during this attempt; its outcome is unknown",
                    )
                ),
            )

    async def release(self, environment_id: str) -> None:
        """Stop an environment's executor before its sandbox goes away.

        Attempts still running there end as the executor's channel closes.
        """
        environment = self._environments.get(environment_id)
        if environment is None:
            return
        async with environment.lock:
            if environment.channel is not None:
                await environment.channel.close()
            self._environments.pop(environment_id, None)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for environment in self._environments.values():
            if environment.channel is not None:
                await environment.channel.close()

    # Bookkeeping.

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _track(self, execution: Execution) -> _Live:
        live = _Live(
            execution, execution.context.environment_id, asyncio.get_running_loop().create_future()
        )
        self._live[execution.execution_id] = live
        return live

    def _save(self, live: _Live, **changes: Any) -> None:
        live.execution = live.execution.model_copy(update=changes)
        self.store.update_execution(live.execution)

    def _emit(self, live: _Live, make: Callable[[int, datetime], Event]) -> None:
        seq = live.execution.last_seq + 1
        event = make(seq, _now())
        self.store.add_event(event)
        live.execution = live.execution.model_copy(update={"last_seq": seq})
        changed, live.changed = live.changed, asyncio.get_running_loop().create_future()
        changed.set_result(None)

    def _state(self, live: _Live, state: ExecutionState) -> None:
        execution_id = live.execution.execution_id
        self._emit(
            live,
            lambda seq, time: StateEvent(
                execution_id=execution_id, seq=seq, time=time, state=state
            ),
        )

    def _finish(self, live: _Live, state: ExecutionState, result: Result) -> None:
        if live.execution.state in TERMINAL_STATES:
            return
        execution_id = live.execution.execution_id
        live.execution = live.execution.model_copy(
            update={"state": state, "ended_at": _now(), "result": result}
        )
        self._state(live, state)
        final = live.execution.model_copy(update={"last_seq": live.execution.last_seq + 1})
        self._emit(
            live,
            lambda seq, time: ResultEvent(
                execution_id=execution_id, seq=seq, time=time, execution=final
            ),
        )
        self.store.update_execution(live.execution)
        environment = self._environments.get(live.environment_id)
        if environment is not None:
            environment.running.discard(execution_id)
        if live.watchdog is not None and live.watchdog is not asyncio.current_task():
            live.watchdog.cancel()
        asyncio.get_running_loop().call_later(RETAIN_LIVE, self._live.pop, execution_id, None)

    # Environments.

    async def _environment(self, environment_id: str) -> _Environment:
        environment = self._environments.setdefault(environment_id, _Environment())
        async with environment.lock:
            if environment.channel is None or environment.channel.closed:
                channel = await self.backend.open(
                    environment_id,
                    partial(self._on_event, environment_id),
                    partial(self._on_closed, environment_id),
                )
                hello = channel.hello
                where = cast(dict[str, Any], hello.get("location") or {})
                declared = self.backend.isolation_tier
                isolation = await probe(channel, declared, self.state_dir)
                if isolation.tier < declared:
                    _log.warning(
                        "environment %s: isolation tier %d declared, %d in effect: %s",
                        environment_id,
                        declared,
                        isolation.tier,
                        "; ".join(isolation.evidence.findings),
                    )
                environment.location = Location(
                    pool_id=self.pool_id,
                    backend=self.backend.name,
                    sandbox_id=self.backend.sandbox_id(environment_id),
                    hostname=str(where.get("hostname", "unknown")),
                    isolation_tier=isolation.tier,
                    declared_isolation_tier=declared,
                    isolation_evidence=isolation.evidence,
                )
                environment.languages = tuple(str(name) for name in hello.get("languages", ()))
                environment.channel = channel
        return environment

    async def _channel(self, environment_id: str) -> ExecutorChannel:
        channel = (await self._environment(environment_id)).channel
        if channel is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"environment {environment_id} has no executor"
            )
        return channel

    async def _on_closed(self, environment_id: str, channel: ExecutorChannel) -> None:
        environment = self._environments.get(environment_id)
        if environment is None or environment.channel is not channel:
            return
        environment.channel = None
        for execution_id in list(environment.running):
            live = self._live.get(execution_id)
            if live is not None:
                self._finish(
                    live,
                    ExecutionState.LOST,
                    Result(
                        error=ErrorInfo(
                            kind=ErrorKind.INTERNAL,
                            message="the executor stopped during this attempt; "
                            "its outcome is unknown",
                        )
                    ),
                )

    # Submission and dispatch.

    async def submit(self, request: ShellRequest | CellRequest) -> tuple[Execution, bool]:
        """Return the execution for ``request.operation_id``, creating it if it is new."""
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        found = self.store.find_operation(request.operation_id)
        if found is not None:
            execution_id, existing = found
            if existing != digest:
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"operation {request.operation_id} was submitted with a different request",
                )
            return self.get(execution_id), False
        execution = Execution(
            execution_id=new_id("exe"),
            attempt_id=new_id("att"),
            operation_id=request.operation_id,
            kind=request.kind,
            context=request.context,
            state=ExecutionState.QUEUED,
            submitted_at=_now(),
        )
        self.store.insert_execution(execution, digest)
        live = self._track(execution)
        self._state(live, ExecutionState.QUEUED)
        self.store.update_execution(live.execution)
        self._spawn(self._dispatch(live, request))
        return live.execution, True

    async def _dispatch(self, live: _Live, request: ShellRequest | CellRequest) -> None:
        execution_id = live.execution.execution_id
        try:
            environment = await self._environment(request.context.environment_id)
        except WorkerError as error:
            self._finish(live, ExecutionState.FAILED, Result(error=error.info()))
            return
        if live.cancel_requested:
            self._finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
            return
        if isinstance(request, CellRequest) and request.language not in environment.languages:
            error = WorkerError(
                ErrorKind.UNSUPPORTED, f"no {request.language} kernel in this environment"
            )
            self._finish(live, ExecutionState.FAILED, Result(error=error.info()))
            return
        self._save(live, location=environment.location)
        fields: dict[str, Any] = {
            "execution": execution_id,
            "session": request.context.session_id,
            "timeout": request.timeout_seconds,
            "limit": request.output_limit_bytes,
        }
        if isinstance(request, ShellRequest):
            fields.update(cwd=request.cwd)
            if request.argv is not None:
                fields["argv"] = list(request.argv)
            else:
                fields["script"] = request.script
        else:
            fields.update(language=request.language, code=request.code)
        environment.running.add(execution_id)
        live.dispatched = True
        try:
            channel = environment.channel
            if channel is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
            await channel.request(request.kind, **fields)
        except WorkerError as error:
            environment.running.discard(execution_id)
            self._finish(live, ExecutionState.FAILED, Result(error=error.info()))
            return
        live.watchdog = self._spawn(self._watch(live, request.timeout_seconds))

    async def _watch(self, live: _Live, timeout: float) -> None:
        await asyncio.sleep(timeout + WATCHDOG_GRACE)
        if live.execution.state in TERMINAL_STATES:
            return
        _log.warning("execution %s overran its limit; cancelling it", live.execution.execution_id)
        with contextlib.suppress(WorkerError):
            await self._send_cancel(live)
        await asyncio.sleep(LOST_GRACE)
        self._finish(
            live,
            ExecutionState.LOST,
            Result(
                error=ErrorInfo(
                    kind=ErrorKind.INTERNAL, message="the executor never reported a result"
                )
            ),
        )

    async def _on_event(self, environment_id: str, message: Message) -> None:
        live = self._live.get(str(message.get("execution")))
        if live is None or live.execution.state in TERMINAL_STATES:
            return
        execution_id = live.execution.execution_id
        kind = message.get("event")
        if kind == "state" and message.get("state") == "running":
            if live.execution.state is ExecutionState.QUEUED:
                live.execution = live.execution.model_copy(
                    update={"state": ExecutionState.RUNNING, "started_at": _now()}
                )
                self._state(live, ExecutionState.RUNNING)
                self.store.update_execution(live.execution)
        elif kind == "stream":
            name = "stderr" if message.get("name") == "stderr" else "stdout"
            text = str(message.get("text", ""))
            self._emit(
                live,
                lambda seq, time: StreamEvent(
                    execution_id=execution_id, seq=seq, time=time, name=name, text=text
                ),
            )
        elif kind == "output":
            try:
                output = RichOutput.model_validate(message.get("output"))
            except ValidationError:
                _log.warning("execution %s: malformed output dropped", execution_id)
                return
            self._emit(
                live,
                lambda seq, time: OutputEvent(
                    execution_id=execution_id, seq=seq, time=time, output=output
                ),
            )
        elif kind == "end":
            try:
                state = ExecutionState(str(message.get("state")))
                if state not in TERMINAL_STATES:
                    raise ValueError(state)
                raw = message.get("result")
                fields = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
                result = self._result(environment_id, live, fields)
            except Exception as exc:  # the execution must end whatever the executor sent
                state = ExecutionState.FAILED
                result = Result(
                    error=ErrorInfo(
                        kind=ErrorKind.INTERNAL,
                        message=f"malformed result from the executor: {exc}",
                    )
                )
            self._finish(live, state, result)

    def _result(self, environment_id: str, live: _Live, raw: dict[str, Any]) -> Result:
        kernel, lost = self._kernel(
            environment_id,
            live.execution.context.session_id,
            raw.get("kernel"),
            bool(raw.get("state_lost")),
        )
        streams = {
            name: StreamOutput.model_validate(raw[name]) if raw.get(name) else None
            for name in ("stdout", "stderr")
        }
        return Result(
            exit_code=raw.get("exit_code"),
            signal=raw.get("signal"),
            status=raw.get("status"),
            execution_count=raw.get("execution_count"),
            stdout=streams["stdout"],
            stderr=streams["stderr"],
            outputs=tuple(RichOutput.model_validate(item) for item in raw.get("outputs", ())),
            error=ErrorInfo.model_validate(raw["error"]) if raw.get("error") else None,
            kernel=kernel,
            state_lost=lost,
            elapsed_seconds=max(0.0, float(raw.get("elapsed_seconds", 0.0))),
            sandbox_started_at=_epoch(raw.get("sandbox_started")),
            sandbox_ended_at=_epoch(raw.get("sandbox_ended")),
            written_files=tuple(str(path) for path in raw.get("written_files", ())),
            written_files_truncated=bool(raw.get("written_files_truncated")),
        )

    def _kernel(
        self, environment_id: str, session_id: str, raw: object, state_lost: bool
    ) -> tuple[KernelInfo | None, bool]:
        """Map the executor's kernel generation onto one that never goes back."""
        if not isinstance(raw, dict):
            return None, state_lost
        raw = cast(dict[str, Any], raw)
        language = str(raw.get("language", "python"))
        generation = int(raw.get("generation", 1))
        environment = self._environments.get(environment_id)
        channel = environment.channel if environment is not None else None
        incarnation = channel.incarnation if channel is not None else "unknown"
        record = self.store.kernel(environment_id, session_id, language)
        if record is None:
            base = 0
        elif record.incarnation != incarnation:
            # A new executor: its kernels start over, and earlier state is gone.
            base, state_lost = record.last, state_lost or record.last > 0
        else:
            base = record.base
        self.store.save_kernel(
            environment_id, session_id, language, KernelRecord(incarnation, base, base + generation)
        )
        if language not in LANGUAGES:
            return None, state_lost
        info = KernelInfo(
            kernel_id=str(raw.get("kernel_id")),
            language=language,
            generation=base + generation,
        )
        return info, state_lost

    # Queries and control.

    def get(self, execution_id: str) -> Execution:
        live = self._live.get(execution_id)
        if live is not None:
            return live.execution
        execution = self.store.execution(execution_id)
        if execution is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no execution {execution_id}")
        return execution

    async def events(self, execution_id: str, after: int) -> AsyncIterator[Event]:
        """Yield the events after ``after``, then new ones, ending with the result."""
        self.get(execution_id)
        while True:
            live = self._live.get(execution_id)
            changed = live.changed if live is not None else None
            for event in self.store.events_after(execution_id, after):
                yield event
                after = event.seq
                if isinstance(event, ResultEvent):
                    return
            if changed is None:
                return
            # Every reader shares the future: one that goes away must not cancel it.
            await asyncio.shield(changed)

    async def cancel(self, execution_id: str) -> Execution:
        live = self._live.get(execution_id)
        if live is None or live.execution.state in TERMINAL_STATES:
            return self.get(execution_id)
        live.cancel_requested = True
        if live.dispatched:
            await self._send_cancel(live)
        return live.execution

    async def _send_cancel(self, live: _Live) -> None:
        if live.execution.state not in (ExecutionState.CANCELLING, *TERMINAL_STATES):
            live.execution = live.execution.model_copy(update={"state": ExecutionState.CANCELLING})
            self._state(live, ExecutionState.CANCELLING)
            self.store.update_execution(live.execution)
        channel = await self._channel(live.environment_id)
        await channel.request("cancel", execution=live.execution.execution_id)

    async def restart_kernel(self, request: KernelRestartRequest) -> KernelInfo:
        context = request.context
        channel = await self._channel(context.environment_id)
        reply = await channel.request(
            "restart", session=context.session_id, language=request.language, reply_timeout=180
        )
        kernel, _ = self._kernel(
            context.environment_id, context.session_id, reply.get("kernel"), True
        )
        if kernel is None:
            raise WorkerError(ErrorKind.INTERNAL, "the executor did not describe the kernel")
        return kernel

    async def capabilities(self, environment_id: str) -> Capabilities:
        environment = await self._environment(environment_id)
        if environment.location is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"environment {environment_id} has no executor"
            )
        return Capabilities(
            protocol=PROTOCOL_VERSION,
            languages=tuple(name for name in environment.languages if name in LANGUAGES),
            location=environment.location,
        )

    # Files.

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        context = request.context
        channel = await self._channel(context.environment_id)
        reply = await channel.request(
            "read",
            session=context.session_id,
            path=request.path,
            offset=request.offset,
            length=request.length,
        )
        return FileContent(
            path=request.path,
            sha256=reply["sha256"],
            size=reply["size"],
            modified_at=datetime.fromtimestamp(float(reply["modified"]), UTC),
            offset=reply["offset"],
            content_b64=reply["content_b64"],
            eof=reply["eof"],
        )

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        context = request.context
        channel = await self._channel(context.environment_id)
        reply = await channel.request(
            "write",
            session=context.session_id,
            path=request.path,
            content_b64=request.content_b64,
            expected_sha256=request.expected_sha256,
            create_only=request.create_only,
        )
        return WriteFileResult(sha256=reply["sha256"], size=reply["size"])

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        context = request.context
        channel = await self._channel(context.environment_id)
        reply = await channel.request("list", session=context.session_id, path=request.path)
        entries = tuple(
            FileEntry(
                name=str(entry["name"]),
                type=entry["type"],
                size=int(entry["size"]),
                modified_at=datetime.fromtimestamp(float(entry["modified"]), UTC),
            )
            for entry in reply.get("entries", ())
        )
        return FileListing(
            path=request.path, entries=entries, truncated=bool(reply.get("truncated"))
        )

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        """Copy a workspace file into the artifact store, checking it did not change meanwhile."""
        context = request.context
        writer = self.blobs.writer()
        try:
            offset, first = 0, None
            while True:
                chunk = await self.read_file(
                    ReadFileRequest(
                        context=context, path=request.path, offset=offset, length=MAX_READ_BYTES
                    )
                )
                if first is None:
                    first = chunk
                    if chunk.size > self.max_artifact_size:
                        raise WorkerError(
                            ErrorKind.INVALID_REQUEST,
                            f"{request.path} is {chunk.size} bytes, "
                            f"over the {self.max_artifact_size}-byte limit",
                        )
                elif (chunk.sha256, chunk.size) != (first.sha256, first.size):
                    raise WorkerError(
                        ErrorKind.CONFLICT, f"{request.path} changed while it was published"
                    )
                data = base64.b64decode(chunk.content_b64)
                writer.write(data)
                offset += len(data)
                if chunk.eof or not data:
                    break
            if not writer.commit(first.sha256):
                raise WorkerError(
                    ErrorKind.CONFLICT, f"{request.path} changed while it was published"
                )
        except BaseException:
            writer.discard()
            raise
        media_type = (
            request.media_type
            or mimetypes.guess_type(request.path)[0]
            or "application/octet-stream"
        )
        artifact = ArtifactRef(
            artifact_id=new_id("art"),
            project_id=context.project_id,
            sha256=first.sha256,
            size=first.size,
            media_type=media_type,
            source_path=request.path,
            produced_by=self._producer(context.environment_id, context.session_id, request.path),
            created_at=_now(),
        )
        self.store.insert_artifact(artifact)
        return artifact

    def _producer(self, environment_id: str, session_id: str, path: str) -> str | None:
        """The last finished attempt that wrote ``path``, as the executor observed it.

        A session path names a file of that session only, so only its attempts
        count; any session's attempts count for the shared area.
        """
        shared = path == "shared" or path.startswith("shared/")
        executions = (
            self.store.environment_executions(environment_id)
            if shared
            else self.store.session_executions(environment_id, session_id)
        )
        writers = [
            execution
            for execution in executions
            if execution.result is not None
            and execution.ended_at is not None
            and path in execution.result.written_files
        ]
        if not writers:
            return None
        latest = max(writers, key=lambda execution: execution.ended_at or execution.submitted_at)
        return latest.attempt_id

    def artifact(self, artifact_id: str) -> ArtifactRef:
        artifact = self.store.artifact(artifact_id)
        if artifact is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no artifact {artifact_id}")
        return artifact
