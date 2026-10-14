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
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeVar

import aiohttp
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    AdmissionRequest,
    AdmissionResult,
    ArtifactRef,
    Capabilities,
    Clock,
    EnvironmentInfo,
    EnvironmentRequest,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ExecutionRequest,
    FileContent,
    FileListing,
    HostPort,
    JobListing,
    JobRequest,
    JobStatus,
    KernelInfo,
    KernelLineageObservation,
    KernelListing,
    KernelObservation,
    KernelRestartRequest,
    KernelStatusRequest,
    ListFilesRequest,
    Lockfile,
    PolicyRevisionRequest,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionRequest,
    PublishRequest,
    ReadFileRequest,
    ResultEvent,
    RevisionHistory,
    SessionClosed,
    SessionCloseRequest,
    WorkerErrorResponse,
    WriteFileRequest,
    WriteFileResult,
)
from sandbox_science.contracts.artifact_inventory import MAX_ARTIFACT_BYTES, ArtifactSnapshot
from sandbox_science.contracts.revisions import (
    MAX_LOCKFILE_BYTES,
    MAX_REVISION_PAGE,
    MAX_REVISION_PAGE_BYTES,
)
from sandbox_science.contracts.worker import EVENT_HEARTBEAT_SECONDS, MAX_EVENT_BYTES, Language

_log = logging.getLogger(__name__)
_EVENT: Final = TypeAdapter[Event](Event)
MAX_KERNEL_OBSERVATION_BYTES: Final = 8 * 1024
KERNEL_OBSERVATION_TIMEOUT: Final = 5.0
RECONNECT_ATTEMPTS: Final = 5
RECONNECT_DELAY: Final = 0.5
CALL_TIMEOUT: Final = 600.0
"""Seconds a request may go without data from the worker before it is given up."""
STREAM_TIMEOUT: Final = 4 * EVENT_HEARTBEAT_SECONDS
"""Seconds an event stream may go without even a heartbeat before it counts as dropped."""
TRANSFER_TIMEOUT: Final = 10.0
"""Seconds an artifact publication or content transfer may take as a whole."""
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


@dataclass(frozen=True)
class WorkerObservationTransport:
    """Private in-memory observer binding; the shared session is borrowed, never owned.

    This is not a browser/tool DTO. All configuration and session values are hidden
    from repr; consumers must not persist or log the copied authorization metadata.
    """

    base_url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    session: aiohttp.ClientSession = field(repr=False)


class WorkerClient:
    """The worker API. The keyword arguments set its timeouts and delays, in seconds;
    ``clock`` paces reconnection attempts."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: aiohttp.ClientSession | None = None,
        stream_timeout: float = STREAM_TIMEOUT,
        reconnect_delay: float = RECONNECT_DELAY,
        result_grace: float = RESULT_GRACE,
        observation_timeout: float = KERNEL_OBSERVATION_TIMEOUT,
        transfer_timeout: float = TRANSFER_TIMEOUT,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.stream_timeout = stream_timeout
        self.reconnect_delay = reconnect_delay
        self.result_grace = result_grace
        self.observation_timeout = observation_timeout
        self.transfer_timeout = transfer_timeout
        self.clock = clock
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session
        self._owns_session = session is None
        self._run_closing = False
        self._run_work: set[asyncio.Task[BaseModel]] = set()
        self._run_close_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> WorkerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Drain selected request owners before closing this client's owned session."""
        if self._run_close_task is None or (
            self._run_close_task.done() and self._owns_session and self._session is not None
        ):
            self._run_closing = True

            async def actual() -> None:
                work = tuple(self._run_work)
                for task in work:
                    task.cancel()
                if work:
                    await asyncio.gather(*work, return_exceptions=True)
                if self._owns_session and self._session is not None:
                    await self._session.close()
                    self._session = None

            self._run_close_task = asyncio.create_task(actual())
        await _selected_owned(self._run_close_task, cancel_on_interrupt=False)

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=10))
        return self._session

    def _observation_configuration(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        if (
            type(self.base_url) is not str
            or not 1 <= len(self.base_url) <= 4096
            or type(self._headers) is not dict
            or len(self._headers) != 1
            or set(self._headers) != {"Authorization"}
        ):
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker configuration is unavailable")
        authorization = self._headers["Authorization"]
        if type(authorization) is not str or not 1 <= len(authorization) <= 8192:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker configuration is unavailable")
        return self.base_url, (("Authorization", authorization),)

    def observation_transport(self) -> WorkerObservationTransport:
        """Copy bounded trusted configuration and borrow the existing HTTP session.

        Creating the HTTP client does not contact a worker or open an executor.
        The original WorkerClient still owns session shutdown; an observer owns
        only its individual request and must drain it before that shutdown.
        """
        endpoint, headers = self._observation_configuration()
        session = self._http()
        if session.closed:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker transport is closed")
        return WorkerObservationTransport(endpoint, headers, session)

    def matches_observation_transport(self, snapshot: WorkerObservationTransport) -> bool:
        """Recheck original client/config/session identity without creating anything."""
        return (
            type(snapshot) is WorkerObservationTransport
            and self._session is snapshot.session
            and not snapshot.session.closed
            and self._observation_configuration() == (snapshot.base_url, snapshot.headers)
        )

    async def _call(
        self,
        method: str,
        path: str,
        model: type[_Model],
        body: BaseModel | None = None,
        params: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> _Model:
        return (await self._request(method, path, model, body, params, max_bytes))[0]

    async def _request(
        self,
        method: str,
        path: str,
        model: type[_Model],
        body: BaseModel | None = None,
        params: dict[str, str] | None = None,
        max_bytes: int | None = None,
        *,
        total_timeout: float | None = None,
        allow_redirects: bool = True,
    ) -> tuple[_Model, int]:
        """The response as ``model``, and its status; a body over ``max_bytes`` is an error."""
        if isinstance(body, JobRequest):
            # Keep the union discriminator while omitting an unspecified time limit,
            # so the worker can choose the policy's configured default.
            body = body.model_copy(update={"kind": "job"})
        data = body.model_dump_json(exclude_unset=isinstance(body, JobRequest)) if body else None
        try:
            async with self._http().request(
                method,
                self.base_url + path,
                data=data,
                params=params,
                headers={**self._headers, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(
                    total=total_timeout, sock_connect=10, sock_read=CALL_TIMEOUT
                ),
                allow_redirects=allow_redirects,
            ) as response:
                if max_bytes is None:
                    payload = await response.read()
                else:
                    payload = await _bounded_body(response.content, max_bytes)
                if response.status >= 400 or (not allow_redirects and 300 <= response.status < 400):
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

    async def admit_hosts(self, environment_id: str, hosts: Sequence[HostPort]) -> AdmissionResult:
        """Have the worker admit hosts before code contacts them (D69)."""
        return await self._call(
            "POST",
            f"/v1/environments/{environment_id}/admissions",
            AdmissionResult,
            AdmissionRequest(hosts=tuple(hosts)),
        )

    async def revisions(
        self, environment_id: str, *, after: int = -1, limit: int = MAX_REVISION_PAGE
    ) -> RevisionHistory:
        """A page of the environment's revisions after revision ``after``, oldest first: how
        each change to its packages came, and its lockfile. The page's ``next_after`` is the
        ``after`` of the next page, while there is one."""
        return await self._call(
            "GET",
            f"/v1/environments/{environment_id}/revisions",
            RevisionHistory,
            params={"after": str(after), "limit": str(limit)},
            max_bytes=MAX_REVISION_PAGE_BYTES,
        )

    async def lockfile(self, environment_id: str, revision: int) -> Lockfile:
        """The packages of one revision of the environment (Conda provisioning, CP-18)."""
        return await self._call(
            "GET",
            f"/v1/environments/{environment_id}/revisions/{revision}/lockfile",
            Lockfile,
            max_bytes=MAX_LOCKFILE_BYTES,
        )

    # Provisioning operations (Conda provisioning, CP-1 to CP-25).

    async def provision(self, request: ProvisionRequest) -> tuple[ProvisionOperation, bool]:
        """Ask for a capability or packages; True when the worker created the operation,
        False when it had one from an earlier submission with the same operation ID."""
        operation, status = await self._request(
            "POST", "/v1/provisioning", ProvisionOperation, request
        )
        return operation, status == 201

    async def provision_status(
        self,
        provision_id: str,
        *,
        after: int = -1,
        wait: float = 0.0,
        project_id: str | None = None,
    ) -> ProvisionOperation:
        """The operation; with ``after``, once its record is newer than that ``seq``, it has
        finished, or ``wait`` seconds (at most 60) have passed."""
        params = {"after": str(after), "wait": str(max(0, min(60, int(wait))))}
        return await self._call(
            "GET",
            f"/v1/provisioning/{provision_id}",
            ProvisionOperation,
            params={**params, **_project(project_id)},
        )

    async def cancel_provision(
        self, provision_id: str, *, project_id: str | None = None
    ) -> ProvisionOperation:
        return await self._call(
            "POST",
            f"/v1/provisioning/{provision_id}/cancel",
            ProvisionOperation,
            params=_project(project_id),
        )

    async def provisioning_queue(
        self, environment_id: str, *, project_id: str | None = None
    ) -> ProvisioningQueue:
        """The environment's operations that have not finished, in request order; with
        ``project_id``, only if the environment is that project's."""
        return await self._call(
            "GET",
            f"/v1/environments/{environment_id}/provisioning",
            ProvisioningQueue,
            params=_project(project_id),
        )

    # Executions.

    async def submit(self, request: ExecutionRequest) -> Execution:
        return await self._call("POST", "/v1/executions", Execution, request)

    async def get(self, execution_id: str, *, project_id: str | None = None) -> Execution:
        return await self._call(
            "GET",
            f"/v1/executions/{execution_id}",
            Execution,
            params=_project(project_id),
            max_bytes=MAX_EVENT_BYTES,
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
                await self.clock.sleep(self.reconnect_delay * failures)
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
            async with asyncio.timeout(request.timeout_seconds + self.result_grace):
                async with contextlib.aclosing(events):
                    async for event in events:
                        if on_event is not None:
                            await on_event(event)
                        if isinstance(event, ResultEvent):
                            return event.execution
        except TimeoutError as exc:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                f"no result from the worker {self.result_grace:g} s after the execution's limit",
            ) from exc
        return await self.get(execution.execution_id, project_id=project_id)

    async def jobs(
        self, context: ExecutionContext, *, limit: int = 100, after: str | None = None
    ) -> JobListing:
        """List this session's jobs, never another session's or environment's."""
        return await self._call(
            "GET",
            "/v1/jobs",
            JobListing,
            params={
                "project_id": context.project_id,
                "session_id": context.session_id,
                "environment_id": context.environment_id,
                "limit": str(limit),
                **({"after": after} if after is not None else {}),
            },
            max_bytes=8 * 1024 * 1024,
        )

    async def job_status(self, execution_id: str, *, project_id: str) -> JobStatus:
        return await self._call(
            "GET",
            f"/v1/jobs/{execution_id}",
            JobStatus,
            params=_project(project_id),
            max_bytes=1024 * 1024,
        )

    # Kernels, files and artifacts.

    async def capabilities(self, environment_id: str) -> Capabilities:
        return await self._call(
            "GET", "/v1/capabilities", Capabilities, params={"environment_id": environment_id}
        )

    async def kernel_status(self, context: ExecutionContext) -> KernelListing:
        """This context's running kernels with durable birth/current package revisions."""
        result = await self._call(
            "POST", "/v1/kernels/status", KernelListing, KernelStatusRequest(context=context)
        )
        if (result.environment_id, result.session_id) != (
            context.environment_id,
            context.session_id,
        ):
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another kernel scope")
        return result

    async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
        """Observe an already connected executor; never fall back to waking status."""
        result, _ = await self._request(
            "POST",
            "/v1/kernels/observe",
            KernelObservation,
            KernelStatusRequest(context=context),
            max_bytes=MAX_KERNEL_OBSERVATION_BYTES,
            total_timeout=self.observation_timeout,
            allow_redirects=False,
        )
        if (result.environment_id, result.session_id) != (
            context.environment_id,
            context.session_id,
        ):
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another kernel scope")
        return result

    async def observe_kernel_lineage(self, context: ExecutionContext) -> KernelLineageObservation:
        """Read exact original birth evidence; unknown lineage grants no notice authority."""
        result, _ = await self._request(
            "POST",
            "/v1/kernels/lineage",
            KernelLineageObservation,
            KernelStatusRequest(context=context),
            max_bytes=MAX_KERNEL_OBSERVATION_BYTES,
            total_timeout=self.observation_timeout,
            allow_redirects=False,
        )
        if (result.project_id, result.environment_id, result.session_id) != (
            context.project_id,
            context.environment_id,
            context.session_id,
        ):
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another kernel scope")
        return result

    async def restart_kernel(
        self, context: ExecutionContext, language: Language = "python"
    ) -> KernelInfo:
        request = KernelRestartRequest(context=context, language=language)
        return await self._call("POST", "/v1/kernels/restart", KernelInfo, request)

    async def close_session(self, context: ExecutionContext) -> SessionClosed:
        """Stop the session's kernels; the next cell starts a new generation."""
        request = SessionCloseRequest(context=context)
        return await self._call("POST", "/v1/sessions/close", SessionClosed, request)

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        return await self._call("POST", "/v1/files/read", FileContent, request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        return await self._call("POST", "/v1/files/write", WriteFileResult, request)

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        return await self._call("POST", "/v1/files/list", FileListing, request)

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        return await self._call("POST", "/v1/artifacts", ArtifactRef, request)

    async def publish_snapshot(self, request: PublishRequest) -> ArtifactRef:
        """Bounded metadata publication for durable control ingestion; never redirect."""
        body = request.model_dump_json()
        if len(body.encode()) > 16 * 1024:
            raise WorkerAPIError(ErrorKind.INVALID_REQUEST, "artifact publish request is too large")
        try:
            async with self._http().post(
                f"{self.base_url}/v1/artifacts",
                data=body,
                headers={**self._headers, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self.transfer_timeout),
                allow_redirects=False,
            ) as response:
                payload = await _bounded_body(
                    response.content, 16 * 1024 if response.status == 201 else 8192
                )
                if response.status >= 400:
                    raise _error(payload, response.status)
                if response.status != 201:
                    raise WorkerAPIError(
                        ErrorKind.INTERNAL, "unexpected artifact publication status"
                    )
                return ArtifactSnapshot.model_validate_json(payload)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                "artifact publication transport failed; outcome may be unknown",
            ) from exc
        except (ValidationError, RecursionError, UnicodeError) as exc:
            raise WorkerAPIError(
                ErrorKind.INTERNAL, "invalid artifact publication metadata; outcome may be unknown"
            ) from exc

    async def verified_artifact_content(
        self, reference: ArtifactRef, *, max_bytes: int = MAX_ARTIFACT_BYTES
    ) -> bytes:
        """Fetch exact project/hash/size-bound bytes, with total deadline and hard cap."""
        try:
            snapshot = ArtifactSnapshot.model_validate(reference.model_dump())
        except (ValidationError, UnicodeError) as exc:
            raise WorkerAPIError(
                ErrorKind.INVALID_REQUEST, "invalid supplied artifact reference"
            ) from exc
        if (
            type(max_bytes) is not int
            or not 0 <= max_bytes <= MAX_ARTIFACT_BYTES
            or snapshot.size > max_bytes
        ):
            raise WorkerAPIError(
                ErrorKind.INVALID_REQUEST, "artifact exceeds control transfer limit"
            )
        try:
            async with self._http().get(
                f"{self.base_url}/v1/artifacts/{snapshot.artifact_id}/content",
                params={"project_id": snapshot.project_id},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=self.transfer_timeout),
                allow_redirects=False,
                auto_decompress=False,
            ) as response:
                if response.status >= 400:
                    raise _error(await _bounded_body(response.content, 8192), response.status)
                if response.status != 200:
                    raise WorkerAPIError(ErrorKind.INTERNAL, "unexpected artifact content status")
                if response.headers.get("X-Content-SHA256") != snapshot.sha256:
                    raise WorkerAPIError(ErrorKind.INTERNAL, "artifact content hash header differs")
                if response.content_length is not None and response.content_length != snapshot.size:
                    raise WorkerAPIError(ErrorKind.INTERNAL, "artifact content size header differs")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise WorkerAPIError(
                        ErrorKind.INTERNAL, "encoded artifact transfer is unsupported"
                    )
                data = await _bounded_body(response.content, snapshot.size)
                if (
                    len(data) != snapshot.size
                    or hashlib.sha256(data).hexdigest() != snapshot.sha256
                ):
                    raise WorkerAPIError(
                        ErrorKind.INTERNAL, "artifact content fails exact size/hash verification"
                    )
                return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "artifact content transport failed"
            ) from exc

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


async def _bounded_body(content: aiohttp.StreamReader, limit: int) -> bytes:
    """A response body of up to ``limit`` bytes; a longer one is an error."""
    body = bytearray()
    async for chunk in content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > limit:
            raise WorkerAPIError(
                ErrorKind.INTERNAL, f"a response from the worker is over {limit} bytes"
            )
    return bytes(body)


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


async def _selected_owned[T](work: asyncio.Task[T], *, cancel_on_interrupt: bool) -> T:
    """Retain the real HTTP/body owner through repeated caller cancellation."""
    first: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as error:
            if first is None:
                first = error
                if cancel_on_interrupt:
                    work.cancel()
        except BaseException:
            if first is None:
                raise
            break  # retrieve below without masking the original caller cancellation
    if first is not None:
        with contextlib.suppress(BaseException):
            work.result()
        raise first
    return work.result()
