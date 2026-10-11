# SPDX-License-Identifier: MIT
"""The worker API over HTTP (docs/WORKER.md).

Every route needs ``Authorization: Bearer <token>``. Errors are JSON
``WorkerErrorResponse`` bodies carrying an error kind from docs/specs/tools.md.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from aiohttp import web
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    ErrorInfo,
    ErrorKind,
    Event,
    ExecutionRequest,
    KernelRestartRequest,
    ListFilesRequest,
    PublishRequest,
    ReadFileRequest,
    WorkerErrorResponse,
    WriteFileRequest,
)

from .errors import WorkerError
from .manager import ExecutionManager

_log = logging.getLogger(__name__)

MANAGER: Final = web.AppKey("manager", ExecutionManager)
MAX_BODY: Final = 16 * 1024 * 1024
_REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
_EVENT = TypeAdapter[Event](Event)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _error(kind: ErrorKind, message: str, status: int) -> web.Response:
    body = WorkerErrorResponse(error=ErrorInfo(kind=kind, message=message))
    return web.json_response(text=body.model_dump_json(), status=status)


def _json(model: BaseModel, status: int = 200) -> web.Response:
    return web.json_response(text=model.model_dump_json(), status=status)


def create_app(manager: ExecutionManager, token: str) -> web.Application:
    expected = f"Bearer {token}".encode()

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        presented = request.headers.get("Authorization", "").encode()
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
    app.add_routes(
        [
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


async def _body(request: web.Request) -> bytes:
    return await request.read()


async def capabilities(request: web.Request) -> web.Response:
    environment_id = request.query.get("environment_id", "")
    if not environment_id:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "environment_id is required")
    return _json(await request.app[MANAGER].capabilities(environment_id))


async def submit(request: web.Request) -> web.Response:
    execution_request = _REQUEST.validate_json(await _body(request))
    execution, created = await request.app[MANAGER].submit(execution_request)
    return _json(execution, 201 if created else 200)


async def get_execution(request: web.Request) -> web.Response:
    return _json(request.app[MANAGER].get(request.match_info["execution_id"]))


async def events(request: web.Request) -> web.StreamResponse:
    manager = request.app[MANAGER]
    execution_id = request.match_info["execution_id"]
    try:
        after = int(request.query.get("after", "0"))
    except ValueError as exc:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "after must be an integer") from exc
    manager.get(execution_id)
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await response.prepare(request)
    async for event in manager.events(execution_id, max(0, after)):
        await response.write(_EVENT.dump_json(event) + b"\n")
    await response.write_eof()
    return response


async def cancel(request: web.Request) -> web.Response:
    return _json(await request.app[MANAGER].cancel(request.match_info["execution_id"]))


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
    return _json(request.app[MANAGER].artifact(request.match_info["artifact_id"]))


async def get_artifact_content(request: web.Request) -> web.StreamResponse:
    manager = request.app[MANAGER]
    artifact = manager.artifact(request.match_info["artifact_id"])
    return web.FileResponse(
        manager.blobs.path(artifact.sha256),
        headers={
            "Content-Type": artifact.media_type,
            "X-Content-SHA256": artifact.sha256,
            "Content-Disposition": "attachment",
        },
    )
