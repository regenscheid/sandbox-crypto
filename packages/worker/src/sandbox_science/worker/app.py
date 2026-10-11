# SPDX-License-Identifier: MIT
"""The worker API over HTTP (docs/WORKER.md).

Every route needs ``Authorization: Bearer <token>``: the pool's own token, which may act
for every project, or a client's, which may act only for the projects its settings
list. For such a client, the executions, artifacts and environments of other projects
do not exist. When the worker manages environments, a request's environment must
belong to the project it names. Errors are JSON ``WorkerErrorResponse`` bodies
carrying an error kind from docs/specs/tools.md.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, cast

from aiohttp import web
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    ArtifactRef,
    EnvironmentRequest,
    EnvironmentState,
    ErrorInfo,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ExecutionRequest,
    Identifier,
    KernelRestartRequest,
    ListFilesRequest,
    PolicyRevisionRequest,
    PublishRequest,
    ReadFileRequest,
    SessionCloseRequest,
    WorkerErrorResponse,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import EVENT_HEARTBEAT_SECONDS, MediaType

from .environments import Environments
from .errors import WorkerError
from .manager import ExecutionManager
from .settings import ClientSettings, token_digest

_log = logging.getLogger(__name__)

MANAGER: Final = web.AppKey("manager", ExecutionManager)
ENVIRONMENTS: Final = web.AppKey("environments", Environments)
MAX_BODY: Final = 16 * 1024 * 1024
_REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
_EVENT = TypeAdapter[Event](Event)
_IDENTIFIER = TypeAdapter[str](Identifier)
_MEDIA_TYPE = TypeAdapter[str](MediaType)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_CLIENT: Final = "sandbox_science.client"
_MAX_TOKEN: Final = 4096


@dataclass(frozen=True, slots=True)
class Client:
    """Who is calling: ``projects`` is None for the pool's own token."""

    name: str
    projects: frozenset[str] | None

    def allows(self, project_id: str) -> bool:
        return self.projects is None or project_id in self.projects


def _error(kind: ErrorKind, message: str, status: int) -> web.Response:
    body = WorkerErrorResponse(error=ErrorInfo(kind=kind, message=message))
    return web.json_response(text=body.model_dump_json(), status=status)


def _json(model: BaseModel, status: int = 200) -> web.Response:
    return web.json_response(text=model.model_dump_json(), status=status)


def create_app(
    manager: ExecutionManager,
    token: str,
    environments: Environments | None = None,
    clients: Sequence[ClientSettings] = (),
) -> web.Application:
    if clients and environments is None:
        raise ValueError("clients need managed environments to keep their projects apart")
    expected = f"Bearer {token}".encode()
    pool = Client("pool", None)
    by_digest = {c.token_sha256: Client(c.name, frozenset(c.projects)) for c in clients}

    def identify(header: str) -> Client | None:
        # Header bytes that are not UTF-8 arrive as surrogates; they must not raise.
        presented = header.encode("utf-8", "surrogateescape")
        if hmac.compare_digest(presented, expected):
            return pool
        scheme, _, secret = header.partition(" ")
        if scheme != "Bearer" or not secret or len(secret) > _MAX_TOKEN:
            return None
        # A lookup by the token's hash compares no secret directly.
        return by_digest.get(token_digest(secret))

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        client = identify(request.headers.get("Authorization", ""))
        if client is None:
            return _error(ErrorKind.POLICY_DENIED, "missing or wrong bearer token", 401)
        request[_CLIENT] = client
        try:
            return await handler(request)
        except WorkerError as error:
            return _error(error.kind, error.message, error.status)
        except ValidationError as exc:
            return _error(ErrorKind.INVALID_REQUEST, _summary(exc), 400)
        except web.HTTPException:
            raise
        except Exception:
            _log.exception("worker API request failed")
            return _error(ErrorKind.INTERNAL, "internal worker error", 500)

    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[MANAGER] = manager
    if environments is not None:
        app[ENVIRONMENTS] = environments
    app.add_routes(
        [
            web.post("/v1/environments", create_environment),
            web.get("/v1/environments/{environment_id}", get_environment),
            web.post("/v1/environments/{environment_id}/policy", revise_environment),
            web.delete("/v1/environments/{environment_id}", delete_environment),
            web.get("/v1/capabilities", capabilities),
            web.post("/v1/executions", submit),
            web.get("/v1/executions/{execution_id}", get_execution),
            web.get("/v1/executions/{execution_id}/events", events),
            web.post("/v1/executions/{execution_id}/cancel", cancel),
            web.post("/v1/kernels/restart", restart_kernel),
            web.post("/v1/sessions/close", close_session),
            web.post("/v1/files/read", read_file),
            web.post("/v1/files/write", write_file),
            web.post("/v1/files/list", list_files),
            web.post("/v1/artifacts", publish),
            web.get("/v1/artifacts/{artifact_id}", get_artifact),
            web.get("/v1/artifacts/{artifact_id}/content", get_artifact_content),
        ]
    )
    return app


def _summary(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg')}" if where else str(first.get("msg"))


def _identifier(value: str, what: str) -> str:
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError as exc:
        raise WorkerError(ErrorKind.INVALID_REQUEST, f"{what} is not a valid identifier") from exc


def _client(request: web.Request) -> Client:
    return cast(Client, request[_CLIENT])


def _visible(request: web.Request, project_id: str, what: str) -> None:
    """Hide what belongs to a project the client may not act for, as if it did not exist."""
    if not _client(request).allows(project_id):
        raise WorkerError(ErrorKind.NOT_FOUND, f"no {what}")


def _allowed(request: web.Request, project_id: str) -> None:
    if not _client(request).allows(project_id):
        raise WorkerError(
            ErrorKind.POLICY_DENIED, f"this client may not act for project {project_id!r}"
        )


def _context(request: web.Request, context: ExecutionContext) -> None:
    """Check that the client may act for the project, and that the environment is the project's.

    A client limited to some projects may use only the environments the worker created
    for them. The pool's own token may also use sandboxes made by hand.
    """
    _allowed(request, context.project_id)
    environments = request.app.get(ENVIRONMENTS)
    if environments is None:
        return  # no environment belongs to a project here, and no client is limited
    environment_id = context.environment_id
    info = environments.records.get(environment_id)
    if _client(request).projects is not None:
        if (
            info is None
            or info.state is EnvironmentState.DELETED
            or info.project_id != context.project_id
        ):
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    elif (
        info is not None
        and info.state is not EnvironmentState.DELETED
        and info.project_id != context.project_id
    ):
        raise WorkerError(
            ErrorKind.CONFLICT,
            f"environment {environment_id!r} belongs to project {info.project_id!r}, "
            f"not {context.project_id!r}",
        )


def _project(request: web.Request) -> str | None:
    """The ``project_id`` query parameter: when given, the execution or artifact must be in it."""
    project_id = request.query.get("project_id")
    return None if project_id is None else _identifier(project_id, "project_id")


async def _body(request: web.Request) -> bytes:
    return await request.read()


def _environments(request: web.Request) -> Environments:
    environments = request.app.get(ENVIRONMENTS)
    if environments is None:
        raise WorkerError(ErrorKind.UNSUPPORTED, "this worker does not manage environments (D73)")
    return environments


async def create_environment(request: web.Request) -> web.Response:
    body = EnvironmentRequest.model_validate_json(await _body(request))
    _allowed(request, body.project_id)
    # An environment of another project is a conflict that does not name the project.
    info, created = await _environments(request).create(body)
    return _json(info, 201 if created else 200)


def _environment(request: web.Request, environments: Environments) -> str:
    """The route's environment ID, once it is known to belong to the client's projects."""
    environment_id = request.match_info["environment_id"]
    info = environments.get(environment_id)
    _visible(request, info.project_id, f"environment {environment_id!r}")
    return environment_id


async def get_environment(request: web.Request) -> web.Response:
    environments = _environments(request)
    return _json(environments.get(_environment(request, environments)))


async def revise_environment(request: web.Request) -> web.Response:
    body = PolicyRevisionRequest.model_validate_json(await _body(request))
    environments = _environments(request)
    return _json(await environments.revise(_environment(request, environments), body.policy))


async def delete_environment(request: web.Request) -> web.Response:
    environments = _environments(request)
    return _json(await environments.delete(_environment(request, environments)))


async def capabilities(request: web.Request) -> web.Response:
    environment_id = request.query.get("environment_id", "")
    if not environment_id:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "environment_id is required")
    environment_id = _identifier(environment_id, "environment_id")
    environments = request.app.get(ENVIRONMENTS)
    if environments is not None and _client(request).projects is not None:
        info = environments.records.get(environment_id)
        if info is None or info.state is EnvironmentState.DELETED:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        _visible(request, info.project_id, f"environment {environment_id!r}")
    return _json(await request.app[MANAGER].capabilities(environment_id))


async def submit(request: web.Request) -> web.Response:
    execution_request = _REQUEST.validate_json(await _body(request))
    _context(request, execution_request.context)
    execution, created = await request.app[MANAGER].submit(execution_request)
    return _json(execution, 201 if created else 200)


def _execution(request: web.Request) -> Execution:
    """The route's execution, once it is known to belong to the client's projects."""
    execution_id = request.match_info["execution_id"]
    execution = request.app[MANAGER].get(execution_id, _project(request))
    _visible(request, execution.context.project_id, f"execution {execution_id}")
    return execution


async def get_execution(request: web.Request) -> web.Response:
    return _json(_execution(request))


async def events(request: web.Request) -> web.StreamResponse:
    manager = request.app[MANAGER]
    try:
        after = min(max(0, int(request.query.get("after", "0"))), 2**62)
    except ValueError as exc:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "after must be an integer") from exc
    execution_id = _execution(request).execution_id
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await response.prepare(request)
    # The status line is sent: from here on, an error can only end the stream early.
    stream = manager.events(execution_id, after, heartbeat=EVENT_HEARTBEAT_SECONDS)
    try:
        async with contextlib.aclosing(stream):
            async for event in stream:
                # An empty line while nothing happens lets the client tell a quiet
                # execution from a dead connection.
                await response.write(b"\n" if event is None else _EVENT.dump_json(event) + b"\n")
        await response.write_eof()
    except ConnectionResetError:  # the client went away, as clients may
        _log.debug("a client left the event stream of %s", execution_id)
    except Exception:
        _log.exception("the event stream of %s failed", execution_id)
        if request.transport is not None:
            request.transport.close()  # the client sees the stream drop and asks again
    return response


async def cancel(request: web.Request) -> web.Response:
    return _json(await request.app[MANAGER].cancel(_execution(request).execution_id))


async def close_session(request: web.Request) -> web.Response:
    body = SessionCloseRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].close_session(body))


async def restart_kernel(request: web.Request) -> web.Response:
    body = KernelRestartRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].restart_kernel(body))


async def read_file(request: web.Request) -> web.Response:
    body = ReadFileRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].read_file(body))


async def write_file(request: web.Request) -> web.Response:
    body = WriteFileRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].write_file(body))


async def list_files(request: web.Request) -> web.Response:
    body = ListFilesRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].list_files(body))


async def publish(request: web.Request) -> web.Response:
    body = PublishRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].publish(body), 201)


def _artifact(request: web.Request) -> ArtifactRef:
    """The route's artifact, once it is known to belong to the client's projects."""
    artifact_id = request.match_info["artifact_id"]
    artifact = request.app[MANAGER].artifact(artifact_id, _project(request))
    _visible(request, artifact.project_id, f"artifact {artifact_id}")
    return artifact


async def get_artifact(request: web.Request) -> web.Response:
    return _json(_artifact(request))


async def get_artifact_content(request: web.Request) -> web.StreamResponse:
    manager = request.app[MANAGER]
    artifact = _artifact(request)
    try:
        media_type = _MEDIA_TYPE.validate_python(artifact.media_type)
    except ValidationError:  # recorded before media types were checked
        media_type = "application/octet-stream"
    return web.FileResponse(
        manager.blobs.path(artifact.sha256),
        headers={
            "Content-Type": media_type,
            "X-Content-SHA256": artifact.sha256,
            # Bytes to save, never a page to render: whatever type the agent chose.
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )
