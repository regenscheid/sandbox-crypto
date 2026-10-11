# SPDX-License-Identifier: MIT
"""A client for the worker API (docs/WORKER.md).

It resumes an execution's event stream after a dropped connection, asking
for the events after the last one it saw, so each event arrives once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final, TypeVar

import aiohttp
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    ArtifactRef,
    Capabilities,
    CellRequest,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    FileContent,
    FileListing,
    KernelInfo,
    KernelRestartRequest,
    ListFilesRequest,
    PublishRequest,
    ReadFileRequest,
    ResultEvent,
    ShellRequest,
    WorkerErrorResponse,
    WriteFileRequest,
    WriteFileResult,
)

_log = logging.getLogger(__name__)
_EVENT: Final = TypeAdapter[Event](Event)
RECONNECT_ATTEMPTS: Final = 5
RECONNECT_DELAY: Final = 0.5
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
        self, base_url: str, token: str, *, session: aiohttp.ClientSession | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
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
        self, method: str, path: str, model: type[_Model], body: BaseModel | None = None
    ) -> _Model:
        data = body.model_dump_json() if body is not None else None
        try:
            async with self._http().request(
                method,
                self.base_url + path,
                data=data,
                headers={**self._headers, "Content-Type": "application/json"},
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise _error(text, response.status)
        except aiohttp.ClientError as exc:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, f"cannot reach the worker: {exc}") from exc
        try:
            return model.model_validate_json(text)
        except ValidationError as exc:
            raise WorkerAPIError(ErrorKind.INTERNAL, f"unexpected worker response: {exc}") from exc

    # Executions.

    async def submit(self, request: ShellRequest | CellRequest) -> Execution:
        return await self._call("POST", "/v1/executions", Execution, request)

    async def get(self, execution_id: str) -> Execution:
        return await self._call("GET", f"/v1/executions/{execution_id}", Execution)

    async def cancel(self, execution_id: str) -> Execution:
        return await self._call("POST", f"/v1/executions/{execution_id}/cancel", Execution)

    async def events(self, execution_id: str, after: int = 0) -> AsyncIterator[Event]:
        """Yield the execution's events after ``after``, reconnecting as needed."""
        failures = 0
        while True:
            try:
                async with self._http().get(
                    f"{self.base_url}/v1/executions/{execution_id}/events",
                    params={"after": str(after)},
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                ) as response:
                    if response.status >= 400:
                        raise _error(await response.text(), response.status)
                    async for line in response.content:
                        if not line.strip():
                            continue
                        event = _EVENT.validate_json(line)
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
        request: ShellRequest | CellRequest,
        on_event: Callable[[Event], Awaitable[None]] | None = None,
    ) -> Execution:
        """Submit ``request`` and follow it to its end."""
        execution = await self.submit(request)
        async for event in self.events(execution.execution_id):
            if on_event is not None:
                await on_event(event)
            if isinstance(event, ResultEvent):
                return event.execution
        return await self.get(execution.execution_id)

    # Kernels, files and artifacts.

    async def capabilities(self, environment_id: str) -> Capabilities:
        return await self._call(
            "GET", f"/v1/capabilities?environment_id={environment_id}", Capabilities
        )

    async def restart_kernel(self, context: ExecutionContext) -> KernelInfo:
        return await self._call(
            "POST", "/v1/kernels/restart", KernelInfo, KernelRestartRequest(context=context)
        )

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        return await self._call("POST", "/v1/files/read", FileContent, request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        return await self._call("POST", "/v1/files/write", WriteFileResult, request)

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        return await self._call("POST", "/v1/files/list", FileListing, request)

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        return await self._call("POST", "/v1/artifacts", ArtifactRef, request)

    async def artifact_content(self, artifact_id: str) -> bytes:
        try:
            async with self._http().get(
                f"{self.base_url}/v1/artifacts/{artifact_id}/content", headers=self._headers
            ) as response:
                if response.status >= 400:
                    raise _error(await response.text(), response.status)
                return await response.read()
        except aiohttp.ClientError as exc:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, f"cannot reach the worker: {exc}") from exc


def _error(text: str, status: int) -> WorkerAPIError:
    try:
        error = WorkerErrorResponse.model_validate_json(text).error
    except ValidationError:
        return WorkerAPIError(ErrorKind.INTERNAL, f"worker returned {status}: {text[:200]}", status)
    return WorkerAPIError(error.kind, error.message, status)


def dump(value: Any) -> str:
    """Compact JSON, for tool results the model reads."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
