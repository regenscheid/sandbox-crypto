# SPDX-License-Identifier: MIT
"""A client for the worker API (docs/WORKER.md).

It resumes an execution's event stream after a dropped connection, asking
for the events after the last one it saw, so each event arrives once. Every
request has a read timeout, and an event stream's heartbeats tell a quiet
execution from a dead connection, so the client never waits forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, Final, TypeVar

import aiohttp
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    ArtifactRef,
    Capabilities,
    EnvironmentInfo,
    EnvironmentRequest,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ExecutionRequest,
    FileContent,
    FileListing,
    KernelInfo,
    KernelRestartRequest,
    ListFilesRequest,
    PolicyRevisionRequest,
    PublishRequest,
    ReadFileRequest,
    ResultEvent,
    WorkerErrorResponse,
    WriteFileRequest,
    WriteFileResult,
)
from sandbox_science.contracts.worker import EVENT_HEARTBEAT_SECONDS, MAX_EVENT_BYTES, Language

_log = logging.getLogger(__name__)
_EVENT: Final = TypeAdapter[Event](Event)
RECONNECT_ATTEMPTS: Final = 5
RECONNECT_DELAY: Final = 0.5
CALL_TIMEOUT: Final = 600.0
"""Seconds a request may go without data from the worker before it is given up."""
STREAM_TIMEOUT: Final = 4 * EVENT_HEARTBEAT_SECONDS
"""Seconds an event stream may go without even a heartbeat before it counts as dropped."""
RESULT_GRACE: Final = 300.0
"""Seconds past an execution's own limit that ``run`` waits for its result. The worker's
watchdog ends an overdue execution within 90 seconds of its limit."""
_Model = TypeVar("_Model", bound=BaseModel)


class WorkerAPIError(Exception):
    """The worker answered with an error, or could not be reached (``unavailable``)."""

    def __init__(self, kind: ErrorKind, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


class WorkerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: aiohttp.ClientSession | None = None,
        stream_timeout: float = STREAM_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.stream_timeout = stream_timeout
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> WorkerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=10))
        return self._session

    async def _call(
        self,
        method: str,
        path: str,
        model: type[_Model],
        body: BaseModel | None = None,
        params: dict[str, str] | None = None,
    ) -> _Model:
        return (await self._request(method, path, model, body, params))[0]

    async def _request(
        self,
        method: str,
        path: str,
        model: type[_Model],
        body: BaseModel | None = None,
        params: dict[str, str] | None = None,
    ) -> tuple[_Model, int]:
        """The response as ``model``, and its status."""
        data = body.model_dump_json() if body is not None else None
        try:
            async with self._http().request(
                method,
                self.base_url + path,
                data=data,
                params=params,
                headers={**self._headers, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(sock_connect=10, sock_read=CALL_TIMEOUT),
            ) as response:
                payload = await response.read()
                if response.status >= 400:
                    raise _error(payload, response.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, f"cannot reach the worker: {exc}") from exc
        try:
            return model.model_validate_json(payload), response.status
        except ValidationError as exc:
            raise WorkerAPIError(
                ErrorKind.INTERNAL, f"unexpected worker response: {str(exc)[:500]}"
            ) from exc

    # Environments (D73): the worker creates sandboxes under stored policy revisions.

    async def create_environment(self, request: EnvironmentRequest) -> EnvironmentInfo:
        return await self._call("POST", "/v1/environments", EnvironmentInfo, request)

    async def environment(self, environment_id: str) -> EnvironmentInfo:
        return await self._call("GET", f"/v1/environments/{environment_id}", EnvironmentInfo)

    async def revise_environment(self, environment_id: str, policy: str) -> EnvironmentInfo:
        return await self._call(
            "POST",
            f"/v1/environments/{environment_id}/policy",
            EnvironmentInfo,
            PolicyRevisionRequest(policy=policy),
        )

    async def delete_environment(self, environment_id: str) -> EnvironmentInfo:
        return await self._call("DELETE", f"/v1/environments/{environment_id}", EnvironmentInfo)

    # Executions.

    async def submit(self, request: ExecutionRequest) -> Execution:
        return await self._call("POST", "/v1/executions", Execution, request)

    async def get(self, execution_id: str, *, project_id: str | None = None) -> Execution:
        return await self._call(
            "GET", f"/v1/executions/{execution_id}", Execution, params=_project(project_id)
        )

    async def cancel(self, execution_id: str, *, project_id: str | None = None) -> Execution:
        return await self._call(
            "POST",
            f"/v1/executions/{execution_id}/cancel",
            Execution,
            params=_project(project_id),
        )

    async def events(
        self, execution_id: str, after: int = 0, *, project_id: str | None = None
    ) -> AsyncGenerator[Event, None]:
        """Yield the execution's events after ``after``, reconnecting as needed.

        The worker sends an empty line while an execution is quiet; a stream that
        carries nothing at all for ``stream_timeout`` seconds counts as dropped.
        """
        failures = 0
        while True:
            try:
                async with self._http().get(
                    f"{self.base_url}/v1/executions/{execution_id}/events",
                    params={"after": str(after), **_project(project_id)},
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(
                        total=None, sock_connect=10, sock_read=self.stream_timeout
                    ),
                ) as response:
                    if response.status >= 400:
                        raise _error(await response.read(), response.status)
                    async for line in _lines(response.content, MAX_EVENT_BYTES):
                        if not line.strip():
                            continue  # a heartbeat
                        try:
                            event = _EVENT.validate_json(line)
                        except ValidationError as exc:
                            raise WorkerAPIError(
                                ErrorKind.INTERNAL, f"unexpected event from the worker: {exc}"
                            ) from exc
                        failures = 0
                        after = event.seq
                        yield event
                        if isinstance(event, ResultEvent):
                            return
            except (TimeoutError, aiohttp.ClientError) as exc:
                failures += 1
                if failures > RECONNECT_ATTEMPTS:
                    raise WorkerAPIError(
                        ErrorKind.UNAVAILABLE, f"lost the worker's event stream: {exc}"
                    ) from exc
                _log.info("event stream dropped (%s); resuming after %d", exc, after)
                await asyncio.sleep(RECONNECT_DELAY * failures)
                continue
            # The stream ended without a result: ask again from where it stopped.
            failures += 1
            if failures > RECONNECT_ATTEMPTS:
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "the event stream kept ending early")

    async def run(
        self,
        request: ExecutionRequest,
        on_event: Callable[[Event], Awaitable[None]] | None = None,
        *,
        on_submitted: Callable[[Execution, bool], None] | None = None,
    ) -> Execution:
        """Submit ``request`` and follow it to its end.

        ``on_submitted`` learns whether the worker created the execution (True) or
        had one from an earlier submission with the same operation ID (False).
        """
        execution, status = await self._request("POST", "/v1/executions", Execution, request)
        created = status == 201
        if not created:
            _log.warning(
                "operation %s was submitted before: following its execution %s",
                request.operation_id,
                execution.execution_id,
            )
        if on_submitted is not None:
            on_submitted(execution, created)
        project_id = request.context.project_id
        events = self.events(execution.execution_id, project_id=project_id)
        try:
            async with asyncio.timeout(request.timeout_seconds + RESULT_GRACE):
                async with contextlib.aclosing(events):
                    async for event in events:
                        if on_event is not None:
                            await on_event(event)
                        if isinstance(event, ResultEvent):
                            return event.execution
        except TimeoutError as exc:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                f"no result from the worker {RESULT_GRACE:g} s after the execution's limit",
            ) from exc
        return await self.get(execution.execution_id, project_id=project_id)

    # Kernels, files and artifacts.

    async def capabilities(self, environment_id: str) -> Capabilities:
        return await self._call(
            "GET", "/v1/capabilities", Capabilities, params={"environment_id": environment_id}
        )

    async def restart_kernel(
        self, context: ExecutionContext, language: Language = "python"
    ) -> KernelInfo:
        request = KernelRestartRequest(context=context, language=language)
        return await self._call("POST", "/v1/kernels/restart", KernelInfo, request)

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        return await self._call("POST", "/v1/files/read", FileContent, request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        return await self._call("POST", "/v1/files/write", WriteFileResult, request)

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        return await self._call("POST", "/v1/files/list", FileListing, request)

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        return await self._call("POST", "/v1/artifacts", ArtifactRef, request)

    async def artifact_content(self, artifact_id: str, *, project_id: str | None = None) -> bytes:
        try:
            async with self._http().get(
                f"{self.base_url}/v1/artifacts/{artifact_id}/content",
                params=_project(project_id),
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(sock_connect=10, sock_read=CALL_TIMEOUT),
            ) as response:
                if response.status >= 400:
                    raise _error(await response.read(), response.status)
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, f"cannot reach the worker: {exc}") from exc


async def _lines(content: aiohttp.StreamReader, limit: int) -> AsyncIterator[bytes]:
    """A response body's lines, each up to ``limit`` bytes; a longer one is an error.

    aiohttp's own line reader stops at a few hundred KiB, and a result event, which
    carries the output, can be as long as MAX_EVENT_BYTES.
    """
    buffer = bytearray()
    async for chunk in content.iter_any():
        start = len(buffer)  # the buffer before this chunk holds no newline
        buffer += chunk
        begin = 0
        while (end := buffer.find(b"\n", start)) != -1:
            yield bytes(buffer[begin:end])
            begin = start = end + 1
        del buffer[:begin]
        if len(buffer) > limit:
            raise WorkerAPIError(
                ErrorKind.INTERNAL, f"an event from the worker is over {limit} bytes"
            )
    if buffer:
        yield bytes(buffer)


def _project(project_id: str | None) -> dict[str, str]:
    return {"project_id": project_id} if project_id is not None else {}


def _error(payload: bytes, status: int) -> WorkerAPIError:
    try:
        error = WorkerErrorResponse.model_validate_json(payload).error
    except ValidationError:
        text = payload[:200].decode("utf-8", "replace")
        return WorkerAPIError(ErrorKind.INTERNAL, f"worker returned {status}: {text}", status)
    return WorkerAPIError(error.kind, error.message, status)


def dump(value: Any) -> str:
    """Compact JSON, for tool results the model reads."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
