# SPDX-License-Identifier: MIT
"""The worker API over HTTP (docs/WORKER.md).

Every route needs ``Authorization: Bearer <token>``: the pool's own token, which may act
for every project, or a client's, which may act only for the projects its settings
list. For such a client, the executions, artifacts and environments of other projects
do not exist, and only the policy revisions granted to its projects can be used. When
the worker manages environments, a request's environment must belong to the project
it names. Errors are JSON ``WorkerErrorResponse`` bodies carrying an error kind from
docs/specs/tools.md.
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
    AdmissionDecision,
    AdmissionRequest,
    AdmissionResult,
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
    SessionClosed,
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
CREATING: Final = web.AppKey("creating", dict[str, set[str]])
"""The environments each limited client is creating right now, which its limit counts too."""
MAX_BODY: Final = 16 * 1024 * 1024
_REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
_EVENT = TypeAdapter[Event](Event)
_IDENTIFIER = TypeAdapter[str](Identifier)
_MEDIA_TYPE = TypeAdapter[str](MediaType)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_CLIENT: Final = "sandbox_science.client"
_MAX_TOKEN: Final = 4096
_READS: Final = frozenset({"/v1/files/read", "/v1/files/list"})
"""Requests that change nothing, though sent with POST: not logged as changes."""


@dataclass(frozen=True, slots=True)
class Client:
    """Who is calling: ``projects`` is None for the pool's own token."""

    name: str
    projects: frozenset[str] | None
    max_environments: int | None = None

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
    by_digest = {
        c.token_sha256: Client(c.name, frozenset(c.projects), c.max_environments) for c in clients
    }
    if token_digest(token) in by_digest:
        raise ValueError("a client has the pool's own token")

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
            response = await handler(request)
        except WorkerError as error:
            response = _error(error.kind, error.message, error.status)
        except ValidationError as exc:
            response = _error(ErrorKind.INVALID_REQUEST, _summary(exc), 400)
        except web.HTTPException as exc:
            _audit(request, client, exc.status)
            raise
        except Exception:
            _log.exception("worker API request failed")
            response = _error(ErrorKind.INTERNAL, "internal worker error", 500)
        _audit(request, client, response.status)
        return response

    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[MANAGER] = manager
    app[CREATING] = {}
    if environments is not None:
        app[ENVIRONMENTS] = environments
    app.add_routes(
        [
            web.post("/v1/environments", create_environment),
            web.get("/v1/environments/{environment_id}", get_environment),
            web.post("/v1/environments/{environment_id}/policy", revise_environment),
            web.delete("/v1/environments/{environment_id}", delete_environment),
            web.post("/v1/environments/{environment_id}/admissions", admit_hosts),
            web.get("/v1/environments/{environment_id}/revisions", environment_revisions),
            web.get(
                "/v1/environments/{environment_id}/revisions/{revision}/lockfile",
                revision_lockfile,
            ),
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


def _audit(request: web.Request, client: Client, status: int) -> None:
    """Log a change a caller asked for, by route, never by the path it sent (review of D78)."""
    if request.method == "GET":
        return
    route = request.match_info.route.resource
    template = route.canonical if route is not None else "an unknown route"
    if template in _READS:
        return
    _log.info("%s %s by %s: %d", request.method, template, client.name, status)


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

    An environment ID the worker created stays with its project, deleted or not, so
    what passes here cannot reach another project's sandbox later (D78). A client
    limited to some projects may use only the environments the worker created for
    them; the pool's own token may also use the sandboxes made by hand.
    """
    _allowed(request, context.project_id)
    environments = request.app.get(ENVIRONMENTS)
    if environments is None:
        return  # no environment belongs to a project here, and no client is limited
    environment_id = context.environment_id
    info = environments.records.get(environment_id)
    limited = _client(request).projects is not None
    if info is None:
        if not limited and environments.handmade(environment_id):
            return
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    if info.project_id != context.project_id:
        if limited:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        raise WorkerError(
            ErrorKind.CONFLICT,
            f"environment {environment_id!r} belongs to project {info.project_id!r}, "
            f"not {context.project_id!r}",
        )
    if limited and info.state is EnvironmentState.DELETED:
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")


def _revision(
    request: web.Request, environments: Environments, project_id: str, policy: str
) -> None:
    """A limited client may use only the policy revisions granted to the project.

    A revision not granted gets the same answer as one never stored, so that a client
    learns nothing of other projects' revisions.
    """
    if _client(request).projects is not None and not environments.records.granted(
        project_id, policy
    ):
        raise WorkerError(
            ErrorKind.NOT_FOUND,
            f"no policy revision {policy} for project {project_id!r}; an operator grants "
            "revisions with `sandbox-science-worker policy add --project`",
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
    environments = _environments(request)
    _revision(request, environments, body.project_id, body.policy)
    client = _client(request)
    if client.projects is None or client.max_environments is None:
        # An environment of another project is a conflict that does not name the project.
        info, created = await environments.create(body)
        return _json(info, 201 if created else 200)
    # Counted and reserved with no await between, so concurrent requests cannot both pass.
    # A reservation is the ID itself, so one being created is not counted twice.
    live = set(environments.records.live(client.projects))
    creating = request.app[CREATING]
    pending = creating.setdefault(client.name, set())
    reserve = body.environment_id not in live | pending
    if reserve:
        if len(live | pending) >= client.max_environments:
            raise WorkerError(
                ErrorKind.POLICY_DENIED,
                f"this client's projects have {client.max_environments} environments, its "
                "limit; delete one first",
            )
        pending.add(body.environment_id)
    try:
        info, created = await environments.create(body)
    finally:
        if reserve:
            pending.discard(body.environment_id)
        if not pending and creating.get(client.name) is pending:
            del creating[client.name]
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
    environment_id = _environment(request, environments)
    project_id = environments.get(environment_id).project_id
    _revision(request, environments, project_id, body.policy)
    return _json(await environments.revise(environment_id, body.policy))


async def delete_environment(request: web.Request) -> web.Response:
    environments = _environments(request)
    return _json(await environments.delete(_environment(request, environments)))


async def admit_hosts(request: web.Request) -> web.Response:
    """Admit hosts before code contacts them, so its first connection need not fail (D69)."""
    body = AdmissionRequest.model_validate_json(await _body(request))
    environments = _environments(request)
    environment_id = _environment(request, environments)
    if not environments.settings.admission:
        raise WorkerError(ErrorKind.UNSUPPORTED, "this pool does not admit hosts on demand")
    await environments.resume(environment_id)
    decided = await environments.admit(
        environment_id, [(entry.host, entry.port) for entry in body.hosts]
    )
    if decided is None:  # admit waits for the environment's lock, so this cannot happen
        raise WorkerError(ErrorKind.UNAVAILABLE, f"environment {environment_id!r} is busy")
    return _json(
        AdmissionResult(
            decisions=tuple(
                AdmissionDecision(host=d.host, port=d.port, admitted=d.admitted, reason=d.reason)
                for d in decided
            ),
            environment=environments.get(environment_id),
        )
    )


def _revisions_of(request: web.Request) -> str:
    """The route's environment, once the client may see its revisions (Conda provisioning).

    The worker records revisions for every environment that ran something. Those it
    created belong to their project; only the pool's own token sees the others, as with
    executions.
    """
    environment_id = _identifier(request.match_info["environment_id"], "environment_id")
    environments = request.app.get(ENVIRONMENTS)
    info = environments.records.get(environment_id) if environments is not None else None
    limited = _client(request).projects is not None
    if info is None:
        if limited:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        return environment_id
    _visible(request, info.project_id, f"environment {environment_id!r}")
    if limited and info.state is EnvironmentState.DELETED:
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    return environment_id


async def environment_revisions(request: web.Request) -> web.Response:
    environment_id = _revisions_of(request)
    return _json(request.app[MANAGER].revisions.history(environment_id))


async def revision_lockfile(request: web.Request) -> web.StreamResponse:
    """A revision's lockfile, as the JSON of a ``Lockfile``, with ``X-Content-SHA256``."""
    environment_id = _revisions_of(request)
    text = request.match_info["revision"]
    if not text.isascii() or not text.isdigit() or len(text) > 18:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "the revision must be a number")
    path, lockfile = request.app[MANAGER].revisions.lockfile(environment_id, int(text))
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/json",
            "X-Content-SHA256": lockfile.sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


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
    offered = await request.app[MANAGER].capabilities(environment_id)
    if environments is not None and environments.settings.admission:
        info = environments.records.get(environment_id)
        if info is not None and info.state is not EnvironmentState.DELETED:
            # Hosts can be admitted ahead of use (POST .../admissions).
            features = tuple(dict.fromkeys([*offered.features, "admission"]))
            offered = offered.model_copy(update={"features": features})
    return _json(offered)


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
    try:
        _context(request, body.context)
    except WorkerError as error:
        if error.kind is not ErrorKind.NOT_FOUND:
            raise
        # No environment, so no kernels to stop: the session is closed as it is.
        return _json(SessionClosed())
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
