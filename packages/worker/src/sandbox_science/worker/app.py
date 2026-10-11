# SPDX-License-Identifier: MIT
"""The worker API over HTTP (docs/WORKER.md).

Every route needs ``Authorization: Bearer <token>``. Errors are JSON
``WorkerErrorResponse`` bodies carrying an error kind from docs/specs/tools.md.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from aiohttp import web
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    EnvironmentRequest,
    ErrorInfo,
    ErrorKind,
    Event,
    ExecutionRequest,
    Identifier,
    KernelRestartRequest,
    ListFilesRequest,
    PolicyRevisionRequest,
    PublishRequest,
    ReadFileRequest,
    WorkerErrorResponse,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import EVENT_HEARTBEAT_SECONDS, MediaType

from .environments import Environments
from .errors import WorkerError
from .manager import ExecutionManager

_log = logging.getLogger(__name__)

MANAGER: Final = web.AppKey("manager", ExecutionManager)
ENVIRONMENTS: Final = web.AppKey("environments", Environments)
MAX_BODY: Final = 16 * 1024 * 1024
_REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
_EVENT = TypeAdapter[Event](Event)
_IDENTIFIER = TypeAdapter[str](Identifier)
_MEDIA_TYPE = TypeAdapter[str](MediaType)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _error(kind: ErrorKind, message: str, status: int) -> web.Response:
    body = WorkerErrorResponse(error=ErrorInfo(kind=kind, message=message))
    return web.json_response(text=body.model_dump_json(), status=status)


def _json(model: BaseModel, status: int = 200) -> web.Response:
    return web.json_response(text=model.model_dump_json(), status=status)


def create_app(
    manager: ExecutionManager, token: str, environments: Environments | None = None
) -> web.Application:
    expected = f"Bearer {token}".encode()

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        # Header bytes that are not UTF-8 arrive as surrogates; they must not raise.
        presented = request.headers.get("Authorization", "").encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(presented, expected):
            return _error(ErrorKind.POLICY_DENIED, "missing or wrong bearer token", 401)
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
    info, created = await _environments(request).create(body)
    return _json(info, 201 if created else 200)


async def get_environment(request: web.Request) -> web.Response:
    return _json(_environments(request).get(request.match_info["environment_id"]))


async def revise_environment(request: web.Request) -> web.Response:
    body = PolicyRevisionRequest.model_validate_json(await _body(request))
    environment_id = request.match_info["environment_id"]
    return _json(await _environments(request).revise(environment_id, body.policy))


async def delete_environment(request: web.Request) -> web.Response:
    return _json(await _environments(request).delete(request.match_info["environment_id"]))


async def capabilities(request: web.Request) -> web.Response:
    environment_id = request.query.get("environment_id", "")
    if not environment_id:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "environment_id is required")
    environment_id = _identifier(environment_id, "environment_id")
    return _json(await request.app[MANAGER].capabilities(environment_id))


async def submit(request: web.Request) -> web.Response:
    execution_request = _REQUEST.validate_json(await _body(request))
    execution, created = await request.app[MANAGER].submit(execution_request)
    return _json(execution, 201 if created else 200)


async def get_execution(request: web.Request) -> web.Response:
    return _json(request.app[MANAGER].get(request.match_info["execution_id"], _project(request)))


async def events(request: web.Request) -> web.StreamResponse:
    manager = request.app[MANAGER]
    execution_id = request.match_info["execution_id"]
    try:
        after = min(max(0, int(request.query.get("after", "0"))), 2**62)
    except ValueError as exc:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "after must be an integer") from exc
    manager.get(execution_id, _project(request))
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
    execution_id = request.match_info["execution_id"]
    return _json(await request.app[MANAGER].cancel(execution_id, _project(request)))


async def restart_kernel(request: web.Request) -> web.Response:
    body = KernelRestartRequest.model_validate_json(await _body(request))
    return _json(await request.app[MANAGER].restart_kernel(body))


async def read_file(request: web.Request) -> web.Response:
    body = ReadFileRequest.model_validate_json(await _body(request))
    return _json(await request.app[MANAGER].read_file(body))


async def write_file(request: web.Request) -> web.Response:
    body = WriteFileRequest.model_validate_json(await _body(request))
    return _json(await request.app[MANAGER].write_file(body))


async def list_files(request: web.Request) -> web.Response:
    body = ListFilesRequest.model_validate_json(await _body(request))
    return _json(await request.app[MANAGER].list_files(body))


async def publish(request: web.Request) -> web.Response:
    body = PublishRequest.model_validate_json(await _body(request))
    return _json(await request.app[MANAGER].publish(body), 201)


async def get_artifact(request: web.Request) -> web.Response:
    return _json(
        request.app[MANAGER].artifact(request.match_info["artifact_id"], _project(request))
    )


async def get_artifact_content(request: web.Request) -> web.StreamResponse:
    manager = request.app[MANAGER]
    artifact = manager.artifact(request.match_info["artifact_id"], _project(request))
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
