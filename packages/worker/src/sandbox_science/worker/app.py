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

import asyncio
import contextlib
import hmac
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from aiohttp import web
from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    AdmissionDecision,
    AdmissionRequest,
    AdmissionResult,
    ArtifactRef,
    EnvironmentInfo,
    EnvironmentList,
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
    KernelStatusRequest,
    ListFilesRequest,
    PolicyRevisionRequest,
    ProvisionOperation,
    ProvisionRequest,
    PublishRequest,
    ReadFileRequest,
    SessionClosed,
    SessionCloseRequest,
    StageCheckRequest,
    StagePutRequest,
    SuspendResult,
    WorkerErrorResponse,
    WriteFileRequest,
)
from sandbox_science.contracts.artifact_retention import (
    MAX_RETENTION_ACK_BYTES,
    ArtifactRetentionAcknowledgement,
)
from sandbox_science.contracts.documents import (
    MAX_CATALOG_PAGE_BYTES,
    MAX_CONTENT_BODY,
    CatalogContentChunk,
    CatalogDelta,
    CatalogSnapshot,
    CatalogSnapshotPage,
    CatalogStatus,
)
from sandbox_science.contracts.environment_claims import EnvironmentClaim, EnvironmentClaimResult
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage, LockfileSelection
from sandbox_science.contracts.logs import log_context
from sandbox_science.contracts.permission_requests import (
    PermissionConfirmation,
    PermissionConfirmed,
    WorkerPermissionAnswer,
    WorkerPermissionListing,
)
from sandbox_science.contracts.provision_observations import MAX_OBSERVATION_BYTES
from sandbox_science.contracts.readiness import WorkerReadiness
from sandbox_science.contracts.recovery import EffectControl, OperationLookupRequest, RecoveryFence
from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.contracts.revisions import MAX_REVISION_PAGE
from sandbox_science.contracts.worker import (
    EVENT_HEARTBEAT_SECONDS,
    MAX_FILE_WRITE_BODY,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    MediaType,
)
from sandbox_science.policy import effective_resources

from .documents_catalog import DocumentsCatalog
from .environments import Environments
from .errors import WorkerError
from .job_sandboxes import RESERVED_OPERATION as RESERVED_JOB_OPERATION
from .job_sandboxes import offers
from .lockfile_diffs import LockfileDiffs, Pair
from .manager import ExecutionManager
from .operation_lookup import lookup
from .permission_requests import PermissionDesk, PermissionRequestError
from .permissions import PermissionStoreError
from .provisioner import MAX_WAIT, RESERVED_OPERATION, Provisioner
from .recovery_fence import operator_scope
from .settings import ClientSettings, token_digest
from .suspend import busy
from .timeouts import seconds
from .transfers import offload

_log = logging.getLogger(__name__)

MANAGER: Final = web.AppKey("manager", ExecutionManager)
ENVIRONMENTS: Final = web.AppKey("environments", Environments)
PROVISIONER: Final = web.AppKey("provisioner", Provisioner)
PERMISSION_DESK: Final = web.AppKey("permission_desk", PermissionDesk)
DOCUMENT_CATALOG: Final = web.AppKey("document_catalog", DocumentsCatalog)
CATALOG_WORK: Final = web.AppKey("catalog_work", set[asyncio.Task[CatalogStatus]])
CATALOG_EXECUTOR: Final = web.AppKey("catalog_executor", ThreadPoolExecutor)
CREATING: Final = web.AppKey("creating", dict[str, set[str]])
"""The environments each limited client is creating right now, which its limit counts too."""
LOCKFILE_DIFFS: Final = web.AppKey("lockfile_diffs", LockfileDiffs)
HEARTBEAT: Final = web.AppKey("heartbeat", float)
FILE_WRITE_TIMEOUT: Final = web.AppKey("file_write_timeout", float)
FILE_WRITES: Final = web.AppKey("file_writes", asyncio.Lock)
"""Admit one file body before materializing JSON/base64, across all clients/projects."""
MAX_BODY: Final = 16 * 1024 * 1024
_REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
_EVENT = TypeAdapter[Event](Event)
_IDENTIFIER = TypeAdapter[str](Identifier)
_MEDIA_TYPE = TypeAdapter[str](MediaType)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_CLIENT: Final = "sandbox_science.client"
_MAX_TOKEN: Final = 4096
_READS: Final = frozenset({"/v1/files/read", "/v1/files/list", "/v1/kernels/observe"})
"""Requests that change nothing, though sent with POST: not logged as changes."""


@dataclass(frozen=True, slots=True)
class Client:
    """Who is calling: ``projects`` is None for the pool's own token."""

    name: str
    projects: frozenset[str] | None
    max_environments: int | None = None
    document_catalog: bool = False
    recovery_control: bool = False
    operator: bool = False
    artifact_retention: bool = False

    def allows(self, project_id: str) -> bool:
        return self.projects is None or project_id in self.projects


def _error(kind: ErrorKind, message: str, status: int) -> web.Response:
    body = WorkerErrorResponse(error=ErrorInfo(kind=kind, message=message))
    return web.json_response(text=body.model_dump_json(), status=status)


def _json(model: BaseModel, status: int = 200) -> web.Response:
    return web.json_response(text=model.model_dump_json(), status=status)


async def _speak_protocol(request: web.Request, response: web.StreamResponse) -> None:
    """Every response names the worker's protocol version (docs/COMPATIBILITY.md)."""
    response.headers[PROTOCOL_HEADER] = str(PROTOCOL_VERSION)


def _check_protocol(request: web.Request) -> None:
    """Refuse a client that names another protocol version; releases are lockstep."""
    spoken = request.headers.get(PROTOCOL_HEADER)
    if spoken is not None and spoken != str(PROTOCOL_VERSION):
        raise WorkerError(
            ErrorKind.UNSUPPORTED,
            f"version mismatch: the client speaks worker protocol {spoken[:16]!r}, this "
            f"worker {PROTOCOL_VERSION}; upgrade the orchestrator, workspace and worker together",
        )


def create_app(
    manager: ExecutionManager,
    token: str,
    environments: Environments | None = None,
    clients: Sequence[ClientSettings] = (),
    provisioner: Provisioner | None = None,
    document_catalog: DocumentsCatalog | None = None,
    *,
    permission_desk: PermissionDesk | None = None,
    heartbeat: float = EVENT_HEARTBEAT_SECONDS,
    file_write_timeout: float = 300.0,
) -> web.Application:
    """The worker's HTTP API; event streams carry an empty line after ``heartbeat`` quiet
    seconds."""
    seconds("heartbeat", heartbeat)
    seconds("file_write_timeout", file_write_timeout)
    if clients and environments is None:
        raise ValueError("clients need managed environments to keep their projects apart")
    expected = f"Bearer {token}".encode()
    pool = Client(
        "pool",
        None,
        document_catalog=True,
        recovery_control=True,
        operator=True,
        artifact_retention=True,
    )
    by_digest = {
        c.token_sha256: Client(
            c.name,
            frozenset(c.projects),
            c.max_environments,
            c.document_catalog,
            c.recovery_control,
            c.operator,
            c.artifact_retention,
        )
        for c in clients
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

    def read_observation(request: web.Request) -> bool:
        resource = request.match_info.route.resource
        return resource is not None and "/provisioning/observations" in resource.canonical

    @web.middleware
    async def tagged(request: web.Request, handler: Handler) -> web.StreamResponse:
        """Put a request ID and the project and environment its route names on every log
        record the request makes. They are labels for finding records, set before the
        caller is known, so they say nothing about what the caller may do."""
        names = request.match_info
        with log_context(
            request=secrets.token_hex(6),
            project=names.get("project_id"),
            environment=names.get("environment_id"),
        ):
            return await handler(request)

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        client = identify(request.headers.get("Authorization", ""))
        if client is None:
            response = _error(ErrorKind.POLICY_DENIED, "missing or wrong bearer token", 401)
            if read_observation(request):
                response.headers["Cache-Control"] = "no-store"
            return response
        request[_CLIENT] = client
        try:
            _check_protocol(request)
            with operator_scope(client.operator):
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
        if read_observation(request):
            response.headers["Cache-Control"] = "no-store"
        _audit(request, client, response.status)
        return response

    app = web.Application(middlewares=[tagged, guard], client_max_size=MAX_BODY)
    app.on_response_prepare.append(_speak_protocol)
    app[FILE_WRITES] = asyncio.Lock()
    app[FILE_WRITE_TIMEOUT] = file_write_timeout
    app[MANAGER] = manager
    app[HEARTBEAT] = heartbeat
    app[CREATING] = {}
    app[LOCKFILE_DIFFS] = LockfileDiffs()
    app.on_shutdown.append(_end_streams)
    app.on_cleanup.append(_drain_lockfile_diffs)
    if document_catalog is not None:
        app[DOCUMENT_CATALOG] = document_catalog
        app[CATALOG_WORK] = set()
        app[CATALOG_EXECUTOR] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog")
        app.on_cleanup.append(_drain_catalog)
        app.add_routes(
            [
                web.get("/v1/document-catalog/{project_id}", document_catalog_status),
                web.post("/v1/document-catalog/{project_id}/snapshots", document_catalog_begin),
                web.post("/v1/document-catalog/{project_id}/deltas", document_catalog_delta),
                web.post("/v1/document-catalog/{project_id}/pages", document_catalog_page),
                web.post("/v1/document-catalog/{project_id}/content", document_catalog_content),
                web.post("/v1/document-catalog/{project_id}/commit", document_catalog_commit),
            ]
        )
    if environments is not None:
        app[ENVIRONMENTS] = environments
        environments.deleted.append(manager.forget_revisions)
        environments.required_deleting.append(manager.guard_package_deletion)
        environments.confirmed_deleted.append(manager.forget_package_gate)
        environments.reusing.append(manager.guard_package_reuse)

        async def confirmed_capture_cleanup(environment_id: str) -> None:
            info = environments.get(environment_id)
            if info.sandbox_id and environments.records.deletion_confirmed(info):
                await manager.confirm_capture_cleanup(environment_id, info.sandbox, info.sandbox_id)

        environments.confirmed_deleted.append(confirmed_capture_cleanup)
    if provisioner is not None:
        app[PROVISIONER] = provisioner
        if environments is not None:
            environments.required_deleted.append(provisioner.forget)
    if permission_desk is not None:
        # The user's answers to network and package requests, relayed (D97).
        app[PERMISSION_DESK] = permission_desk
        app.add_routes(
            [
                web.get("/v1/permission-requests", permission_requests),
                web.post("/v1/permission-requests/{seq}/answer", answer_permission_request),
                web.post("/v1/permission-requests/{seq}/confirm", confirm_permission_request),
            ]
        )
    app.add_routes(
        [
            web.get("/v1/ready", readiness),
            web.post("/v1/environments", create_environment),
            web.post("/v1/environment-claims", environment_claim),
            web.get("/v1/environments", list_environments),
            web.get("/v1/environments/{environment_id}", get_environment),
            web.post("/v1/environments/{environment_id}/policy", revise_environment),
            web.delete("/v1/environments/{environment_id}", delete_environment),
            web.post("/v1/environments/{environment_id}/admissions", admit_hosts),
            web.post("/v1/environments/{environment_id}/suspend", suspend_environment),
            web.get("/v1/environments/{environment_id}/revisions", environment_revisions),
            web.get(
                "/v1/environments/{environment_id}/revisions/{revision}/lockfile",
                revision_lockfile,
            ),
            web.get("/v1/environments/{environment_id}/provisioning", provisioning_queue),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations", observe_provisioning
            ),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations/operations/{provision_id}",
                observe_provision_detail,
            ),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations/operations/{provision_id}/files",
                observe_provision_files,
            ),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations/revisions",
                observe_provision_revisions,
            ),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations/revisions/{revision}/probes",
                observe_revision_probes,
            ),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations/lockfile-diff",
                observe_lockfile_diff,
            ),
            web.get(
                "/v1/environments/{environment_id}/provisioning/observations/revisions/{revision}/packages",
                observe_revision_packages,
            ),
            web.post("/v1/provisioning", submit_provision),
            web.get("/v1/provisioning/{provision_id}", get_provision),
            web.post("/v1/provisioning/{provision_id}/cancel", cancel_provision),
            web.get("/v1/capabilities", capabilities),
            web.post("/v1/executions", submit),
            web.post("/v1/operations/lookup", lookup_operation),
            web.post("/v1/recovery/fence", install_recovery_fence),
            web.get("/v1/jobs", list_jobs),
            web.get("/v1/jobs/{execution_id}", job_status),
            web.get("/v1/executions/{execution_id}", get_execution),
            web.get("/v1/executions/{execution_id}/events", events),
            web.post("/v1/executions/{execution_id}/cancel", cancel),
            web.post("/v1/kernels/restart", restart_kernel),
            web.post("/v1/kernels/status", kernel_status),
            web.post("/v1/kernels/observe", observe_kernels),
            web.post("/v1/sessions/close", close_session),
            web.post("/v1/files/read", read_file),
            web.post("/v1/files/write", write_file),
            web.post("/v1/files/list", list_files),
            web.post("/v1/skills/check", skills_check),
            web.post("/v1/skills/put", skills_put),
            web.post("/v1/artifacts", publish),
            web.get("/v1/artifacts/{artifact_id}", get_artifact),
            web.get("/v1/artifacts/{artifact_id}/content", get_artifact_content),
            web.post("/v1/artifacts/{artifact_id}/retention", acknowledge_artifact_retention),
        ]
    )

    return app


async def _drain_lockfile_diffs(app: web.Application) -> None:
    await app[LOCKFILE_DIFFS].close()


async def _end_streams(app: web.Application) -> None:
    """At shutdown, end the event streams instead of waiting for them to end."""
    app[MANAGER].end_streams()


def _catalog(request: web.Request) -> tuple[DocumentsCatalog, str]:
    if not _client(request).document_catalog:
        raise WorkerError(ErrorKind.POLICY_DENIED, "document catalog needs control permission")
    project = _identifier(request.match_info["project_id"], "project_id")
    _visible(request, project, "document catalog")
    return request.app[DOCUMENT_CATALOG], project


async def _drain_catalog(app: web.Application) -> None:
    # A disconnected HTTP caller must not leave an admitted commit behind the
    # database close. Shielded SQL tasks keep their receipts and drain on cleanup.
    while app[CATALOG_WORK]:
        await asyncio.gather(*tuple(app[CATALOG_WORK]), return_exceptions=True)
    app[CATALOG_EXECUTOR].shutdown(wait=True)


async def _catalog_call[T](
    request: web.Request, operation: Callable[[T], CatalogStatus], body: T
) -> CatalogStatus:
    work = request.app[CATALOG_WORK]
    if len(work) >= request.app[DOCUMENT_CATALOG].limits.max_pending_requests:
        raise WorkerError(ErrorKind.UNAVAILABLE, "catalog request capacity is occupied")

    async def execute() -> CatalogStatus:
        return await asyncio.get_running_loop().run_in_executor(
            request.app[CATALOG_EXECUTOR], operation, body
        )

    task = asyncio.create_task(execute())
    work.add(task)

    def finished(completed: asyncio.Task[CatalogStatus]) -> None:
        work.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def _catalog_body[T: BaseModel](request: web.Request, model: type[T], limit: int) -> T:
    # Bound even chunked bodies before Pydantic/JSON/base64 decoding allocates them.
    data = bytearray()
    while part := await request.content.read(min(16_384, limit + 1 - len(data))):
        data.extend(part)
        if len(data) > limit:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "request exceeds byte limit")
    return model.model_validate_json(bytes(data))


async def document_catalog_status(request: web.Request) -> web.Response:
    catalog, project = _catalog(request)
    return _json(await _catalog_call(request, catalog.status, project))


async def document_catalog_begin(request: web.Request) -> web.Response:
    catalog, project = _catalog(request)
    body = await _catalog_body(request, CatalogSnapshot, MAX_CATALOG_PAGE_BYTES)
    if body.project_id != project:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "catalog project does not match route")
    return _json(await _catalog_call(request, catalog.begin, body))


async def document_catalog_page(request: web.Request) -> web.Response:
    catalog, project = _catalog(request)
    body = await _catalog_body(request, CatalogSnapshotPage, MAX_CATALOG_PAGE_BYTES)
    if body.project_id != project:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "catalog project does not match route")
    return _json(await _catalog_call(request, catalog.page, body))


async def document_catalog_delta(request: web.Request) -> web.Response:
    catalog, project = _catalog(request)
    body = await _catalog_body(request, CatalogDelta, MAX_CATALOG_PAGE_BYTES)
    if body.target.project_id != project:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "catalog project does not match route")
    return _json(await _catalog_call(request, catalog.delta, body))


async def document_catalog_content(request: web.Request) -> web.Response:
    catalog, project = _catalog(request)
    body = await _catalog_body(request, CatalogContentChunk, MAX_CONTENT_BODY)
    if body.project_id != project:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "catalog project does not match route")
    return _json(await _catalog_call(request, catalog.content, body))


async def document_catalog_commit(request: web.Request) -> web.Response:
    catalog, project = _catalog(request)
    body = await _catalog_body(request, CatalogSnapshot, MAX_CATALOG_PAGE_BYTES)
    if body.project_id != project:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "catalog project does not match route")
    return _json(await _catalog_call(request, catalog.commit, body))


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
    _log.info(
        "%s %s by %s: %d",
        request.method,
        template,
        client.name,
        status,
        extra={"event": "worker_request", "status": status},
    )


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


def _context(request: web.Request, context: ExecutionContext, *, hide: bool = False) -> None:
    """Check that the client may act for the project, and that the environment is the project's.

    An environment ID the worker created stays with its project, deleted or not, so
    what passes here cannot reach another project's sandbox later (D78). A client
    limited to some projects may use only the environments the worker created for
    them; the pool's own token may also use the sandboxes made by hand. With ``hide``,
    another project's environment is ``not_found`` for every client, as for an agent
    that names the environment itself: the answer must not name the other project.
    """
    _allowed(request, context.project_id)
    environments = request.app.get(ENVIRONMENTS)
    if environments is None:
        return  # no environment belongs to a project here, and no client is limited
    environment_id = context.environment_id
    info = environments.records.get(environment_id)
    limited = _client(request).projects is not None
    if info is not None and info.kind == "job":
        # A job sandbox runs its job only; no client names it (D93, JS-1).
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    if info is None:
        if not limited and environments.handmade(environment_id):
            return
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    if info.project_id != context.project_id:
        if limited or hide:
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
        info, created = await _create(request, environments, body)
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
        info, created = await _create(request, environments, body)
    finally:
        if reserve:
            pending.discard(body.environment_id)
        if not pending and creating.get(client.name) is pending:
            del creating[client.name]
    return _json(info, 201 if created else 200)


async def _create(
    request: web.Request, environments: Environments, body: EnvironmentRequest
) -> tuple[EnvironmentInfo, bool]:
    """Create the environment; a new one starts a history of its own (D78).

    Revisions recorded under its ID before, by a sandbox made by hand, a worker that did
    not manage environments yet, or an earlier sandbox of the ID, are forgotten.
    """
    info, created = await environments.create(body)
    if created:
        request.app[MANAGER].revisions.forget(body.environment_id)
    return info, created


def _environment(request: web.Request, environments: Environments) -> str:
    """The route's environment ID, once it is known to belong to the client's projects."""
    environment_id = request.match_info["environment_id"]
    info = environments.get(environment_id)
    _visible(request, info.project_id, f"environment {environment_id!r}")
    if info.kind == "job":
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    return environment_id


async def get_environment(request: web.Request) -> web.Response:
    environments = _environments(request)
    return _json(environments.get(_environment(request, environments)))


async def list_environments(request: web.Request) -> web.Response:
    """The environments of the client's projects, so an operator can see what runs."""
    environments = _environments(request)
    listed = environments.records.listed(_client(request).projects)
    return _json(
        EnvironmentList(environments=tuple(info for info in listed if info.kind == "session"))
    )


async def suspend_environment(request: web.Request) -> web.Response:
    """Stop the environment's sandbox now, as idle suspension would (W3).

    The same rule holds as for an idle environment: one in use keeps running, its
    kernels' CPU sampled twice a second apart included (``suspend.busy``), and its next
    use starts a stopped one again, with its files and installed tools. ``suspended`` is
    OpenShell's word on the sandbox, not the worker's record (``Environments.stop_now``).
    """
    environments = _environments(request)
    environment_id = _environment(request, environments)
    manager = request.app[MANAGER]
    if environments.protected(environment_id) or (
        environments.get(environment_id).state is EnvironmentState.READY
        and await busy(manager, environment_id, manager.clock)
    ):
        suspended = False
    else:
        suspended = await environments.stop_now(environment_id, manager.clock.monotonic())
    return _json(SuspendResult(environment=environments.get(environment_id), suspended=suspended))


async def readiness(request: web.Request) -> web.Response:
    """Authenticated protocol readiness without an environment/backend lookup."""
    return _json(WorkerReadiness(protocol=PROTOCOL_VERSION))


async def environment_claim(request: web.Request) -> web.Response:
    """A bounded claim for active research; never a request to create or resume a VM."""
    body = EnvironmentClaim.model_validate_json(await _body(request))
    _context(request, body.context, hide=True)
    environments = request.app.get(ENVIRONMENTS)
    if environments is None or environments.records.get(body.context.environment_id) is None:
        return _json(
            EnvironmentClaimResult(environment_id=body.context.environment_id, state="unmanaged")
        )
    with request.app[MANAGER].store.recovery_fences.bind(body.context):
        return _json(await environments.claim(body))


async def revise_environment(request: web.Request) -> web.Response:
    body = PolicyRevisionRequest.model_validate_json(await _body(request))
    environments = _environments(request)
    environment_id = _environment(request, environments)
    project_id = environments.get(environment_id).project_id
    _revision(request, environments, project_id, body.policy)
    return _json(await environments.revise(environment_id, body.policy))


async def delete_environment(request: web.Request) -> web.Response:
    """Delete the environment; its revisions and lockfiles go with it (forget_revisions)."""
    environments = _environments(request)
    return _json(await environments.delete(_environment(request, environments)))


async def admit_hosts(request: web.Request) -> web.Response:
    """Admit hosts before code contacts them, so its first connection need not fail (D69)."""
    body = AdmissionRequest.model_validate_json(await _body(request))
    environments = _environments(request)
    environment_id = _environment(request, environments)
    if body.context is not None:
        _context(request, body.context, hide=True)
        if body.context.environment_id != environment_id:
            raise WorkerError(ErrorKind.NOT_FOUND, "admission is outside the supplied context")
        with request.app[MANAGER].store.recovery_fences.bind(body.context):
            return await _admit_hosts(request, environments, environment_id, body)
    if _client(request).operator or _client(request).recovery_control:
        return await _admit_hosts(request, environments, environment_id, body)
    project = environments.get(environment_id).project_id
    with request.app[MANAGER].store.recovery_fences.bind_project(project):
        return await _admit_hosts(request, environments, environment_id, body)


async def _admit_hosts(
    request: web.Request, environments: Environments, environment_id: str, body: AdmissionRequest
) -> web.Response:
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


def _revisions_of(request: web.Request) -> tuple[str, datetime | None]:
    """The route's environment, once the client may see its revisions (Conda provisioning),
    and since when.

    The worker records revisions for every environment that ran something. Those it
    created belong to their project, and their history starts with their record (D78):
    revisions recorded under the ID before are not theirs. Only the pool's own token sees
    the others, as with executions.
    """
    environment_id = _identifier(request.match_info["environment_id"], "environment_id")
    environments = request.app.get(ENVIRONMENTS)
    info = environments.records.get(environment_id) if environments is not None else None
    limited = _client(request).projects is not None
    if info is None:
        if limited:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        return environment_id, None
    _visible(request, info.project_id, f"environment {environment_id!r}")
    if limited and info.state is EnvironmentState.DELETED:
        raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    return environment_id, info.created_at


def _number(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    text = request.query.get(name)
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise WorkerError(
            ErrorKind.INVALID_REQUEST, f"{name} must be a number from {low} to {high}"
        )
    return value


async def environment_revisions(request: web.Request) -> web.Response:
    """A page of the environment's revisions: ``after`` a revision, at most ``limit``."""
    environment_id, since = _revisions_of(request)
    after = _number(request, "after", -1, -1, 2**62)
    limit = _number(request, "limit", MAX_REVISION_PAGE, 1, MAX_REVISION_PAGE)
    history = request.app[MANAGER].revisions.history(
        environment_id, after=after, limit=limit, since=since
    )
    return _json(history)


async def revision_lockfile(request: web.Request) -> web.StreamResponse:
    """A revision's lockfile, as the JSON of a ``Lockfile``, with ``X-Content-SHA256``."""
    environment_id, since = _revisions_of(request)
    text = request.match_info["revision"]
    if not text.isascii() or not text.isdigit() or len(text) > 18:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "the revision must be a number")
    path, lockfile = request.app[MANAGER].revisions.lockfile(environment_id, int(text), since=since)
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
    if environments is not None:
        info = environments.records.get(environment_id)
        if info is not None and info.kind == "job":
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    if environments is not None and _client(request).projects is not None:
        info = environments.records.get(environment_id)
        if info is None or info.state is EnvironmentState.DELETED:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        _visible(request, info.project_id, f"environment {environment_id!r}")
    manager = request.app[MANAGER]
    offered = await manager.capabilities(environment_id)
    sandboxes = manager.job_sandboxes
    if sandboxes is not None and environments is not None:
        governing = environments.governing(environment_id)
        if governing is not None:
            resources = effective_resources(governing.policy.policy, governing.ceiling)[0]
            profiles = offers(environments.settings, resources)
            if profiles:
                features = tuple(dict.fromkeys([*offered.features, "job_sandboxes"]))
                offered = offered.model_copy(
                    update={"features": features, "job_profiles": profiles}
                )
    if environments is not None and environments.settings.admission:
        info = environments.records.get(environment_id)
        if info is not None and info.state is not EnvironmentState.DELETED:
            # Hosts can be admitted ahead of use (POST .../admissions).
            features = tuple(dict.fromkeys([*offered.features, "admission"]))
            offered = offered.model_copy(update={"features": features})
    provisioner = request.app.get(PROVISIONER)
    if provisioner is not None and provisioner.supports(environment_id):
        # Capabilities and packages can be installed on request (POST /v1/provisioning).
        features = tuple(dict.fromkeys([*offered.features, "provisioning"]))
        offered = offered.model_copy(update={"features": features})
    return _json(offered)


async def install_recovery_fence(request: web.Request) -> web.Response:
    if not _client(request).recovery_control:
        raise WorkerError(ErrorKind.POLICY_DENIED, "client lacks recovery control permission")
    body = RecoveryFence.model_validate_json(await _body(request))
    _allowed(request, body.project_id)
    return _json(request.app[MANAGER].store.recovery_fences.install(body))


async def lookup_operation(request: web.Request) -> web.Response:
    body = OperationLookupRequest.model_validate_json(await _body(request))
    _allowed(request, body.context.project_id)
    return _json(lookup(request.app[MANAGER].store, body))


async def submit(request: web.Request) -> web.Response:
    execution_request = _REQUEST.validate_json(await _body(request))
    _context(request, execution_request.context)
    if RESERVED_JOB_OPERATION.fullmatch(execution_request.operation_id):
        raise WorkerError(
            ErrorKind.INVALID_REQUEST,
            "operation IDs of the form jsb-<24 hex digits>.<step> are the worker's own, for"
            " job sandboxes' steps",
        )
    if RESERVED_OPERATION.fullmatch(execution_request.operation_id):
        raise WorkerError(
            ErrorKind.INVALID_REQUEST,
            "operation IDs of the form prv-<24 hex digits>.<step> are the worker's own, for "
            "provisioning operations' steps",
        )
    execution, created = await request.app[MANAGER].submit(execution_request)
    return _json(execution, 201 if created else 200)


async def list_jobs(request: web.Request) -> web.Response:
    context = ExecutionContext(
        project_id=request.query.get("project_id", ""),
        session_id=request.query.get("session_id", ""),
        environment_id=request.query.get("environment_id", ""),
    )
    _context(request, context, hide=True)
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError as exc:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid job listing limit") from exc
    return _json(request.app[MANAGER].jobs(context, limit, request.query.get("after")))


async def job_status(request: web.Request) -> web.Response:
    execution = _execution(request)
    return _json(await request.app[MANAGER].job_status(execution.execution_id, _project(request)))


# Provisioning operations (Conda provisioning, CP-1 to CP-25).


def _desk(request: web.Request) -> PermissionDesk:
    """Only an operator client (the workspace) lists and answers permission requests: the
    run service's token never answers its own sandbox's requests."""
    if not _client(request).operator:
        raise WorkerError(ErrorKind.POLICY_DENIED, "permission requests need an operator client")
    return request.app[PERMISSION_DESK]


async def permission_requests(request: web.Request) -> web.Response:
    """A project's unanswered network and package permission requests (D97)."""
    desk = _desk(request)
    if set(request.query) != {"project"}:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "name exactly one project")
    project_id = _identifier(request.query["project"], "project")
    _allowed(request, project_id)
    now = desk.clock.now()
    try:
        found = await asyncio.to_thread(desk.listing, project_id, now)
    except PermissionStoreError as exc:
        raise WorkerError(ErrorKind.UNAVAILABLE, str(exc)) from None
    return _json(WorkerPermissionListing(requests=found))


async def answer_permission_request(request: web.Request) -> web.Response:
    """The user's answer to one, relayed by the workspace (D97)."""
    desk = _desk(request)
    raw = request.match_info["seq"]
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid permission request")
    answer = WorkerPermissionAnswer.model_validate_json(await _body(request))
    client = _client(request)
    try:
        answered = await asyncio.to_thread(
            desk.answer, int(raw), client.allows, answer, desk.clock.now()
        )
    except PermissionRequestError as exc:
        if exc.missing:
            raise WorkerError(ErrorKind.NOT_FOUND, str(exc)) from None
        raise WorkerError(ErrorKind.CONFLICT, str(exc)) from None
    except PermissionStoreError as exc:
        raise WorkerError(ErrorKind.UNAVAILABLE, str(exc)) from None
    return _json(answered, status=201)


async def confirm_permission_request(request: web.Request) -> web.Response:
    """Whether MCP request ``seq`` is a call's and stands as its refusal said (D98): any
    client, for its own projects. The harness asks before it trusts a refusal that names a
    request; the answer is yes or no, never the request's record."""
    desk = request.app[PERMISSION_DESK]
    raw = request.match_info["seq"]
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid permission request")
    asked = PermissionConfirmation.model_validate_json(await _body(request))
    _allowed(request, asked.project_id)
    try:
        confirmed = await asyncio.to_thread(
            desk.confirm, int(raw), _client(request).allows, asked, desk.clock.now()
        )
    except PermissionStoreError as exc:
        raise WorkerError(ErrorKind.UNAVAILABLE, str(exc)) from None
    return _json(PermissionConfirmed(confirmed=confirmed))


def _provisioner(request: web.Request) -> Provisioner:
    provisioner = request.app.get(PROVISIONER)
    if provisioner is None:
        raise WorkerError(ErrorKind.UNSUPPORTED, "this worker does not provision environments")
    return provisioner


def _provision(request: web.Request) -> ProvisionOperation:
    """The route's operation, once it is known to belong to the client's projects."""
    provision_id = request.match_info["provision_id"]
    operation = _provisioner(request).get(provision_id, _project(request))
    _visible(
        request, operation.request.context.project_id, f"provisioning operation {provision_id}"
    )
    _not_job_sandbox(
        request,
        operation.request.context.environment_id,
        f"provisioning operation {provision_id}",
    )
    return operation


async def submit_provision(request: web.Request) -> web.Response:
    """Ask for a capability or packages in an environment: one operation, queued."""
    body = ProvisionRequest.model_validate_json(await _body(request))
    _context(request, body.context, hide=True)  # the agent may name the environment
    if RESERVED_JOB_OPERATION.fullmatch(body.operation_id):
        raise WorkerError(
            ErrorKind.INVALID_REQUEST,
            "operation IDs of the form jsb-<24 hex digits>.<step> are the worker's own",
        )
    operation, created = await _provisioner(request).submit(body)
    return _json(operation, 201 if created else 200)


async def get_provision(request: web.Request) -> web.Response:
    """The operation; with ``after``, once its record is newer than that ``seq``, it has
    finished, or ``wait`` seconds (at most 60) have passed."""
    operation = _provision(request)
    after = _number(request, "after", -1, -1, 2**62)
    wait = _number(request, "wait", 0, 0, int(MAX_WAIT))
    if after < 0 or wait == 0:
        return _json(operation)
    waited = await _provisioner(request).wait(
        operation.provision_id, after, float(wait), operation.request.context.project_id
    )
    return _json(waited)


async def cancel_provision(request: web.Request) -> web.Response:
    operation = _provision(request)
    context = await _effect_control(request)
    return _json(await _provisioner(request).cancel(operation.provision_id, context=context))


async def provisioning_queue(request: web.Request) -> web.Response:
    """The environment's operations that have not finished, in request order (CP-11). With
    ``project_id``, an environment that is not that project's is ``not_found``."""
    environment_id, _ = _revisions_of(request)
    project_id = _project(request)
    if project_id is not None:
        environments = request.app.get(ENVIRONMENTS)
        info = environments.records.get(environment_id) if environments is not None else None
        if info is None or info.project_id != project_id:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
    return _json(_provisioner(request).queue(environment_id))


def _observation_scope(request: web.Request, queries: frozenset[str]) -> tuple[str, str, float]:
    if set(request.query) - queries - {"project_id"} or any(
        len(request.query.getall(key)) != 1 for key in request.query
    ):
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid observation query")
    project = _project(request)
    if project is None:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "project_id is required")
    environment, _ = _revisions_of(request)
    info = _environments(request).records.get(environment)
    if info is None or info.state is EnvironmentState.DELETED or info.project_id != project:
        raise WorkerError(ErrorKind.NOT_FOUND, "no selected managed environment")
    _provisioner(request)  # Capability check only; do not call get/queue/wait or a backend.
    return project, environment, info.created_at.timestamp()


def _observation_json(model: BaseModel) -> web.Response:
    text = model.model_dump_json()
    if len(text.encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise WorkerError(ErrorKind.UNAVAILABLE, "observation response exceeds its bound")
    return web.json_response(text=text, headers={"Cache-Control": "no-store"})


async def observe_provisioning(request: web.Request) -> web.Response:
    project, environment, since = _observation_scope(request, frozenset())
    return _observation_json(
        request.app[MANAGER].store.provision_observations.summary(project, environment, since=since)
    )


async def observe_provision_detail(request: web.Request) -> web.Response:
    project, environment, since = _observation_scope(request, frozenset({"seq"}))
    seq = _number(request, "seq", -1, 0, 2**62) if "seq" in request.query else None
    return _observation_json(
        request.app[MANAGER].store.provision_observations.detail(
            project,
            environment,
            _identifier(request.match_info["provision_id"], "provision_id"),
            since=since,
            seq=seq,
        )
    )


async def observe_provision_files(request: web.Request) -> web.Response:
    project, environment, since = _observation_scope(request, frozenset({"seq", "offset", "limit"}))
    return _observation_json(
        request.app[MANAGER].store.provision_observations.files(
            project,
            environment,
            _identifier(request.match_info["provision_id"], "provision_id"),
            since=since,
            seq=_number(request, "seq", -1, 0, 2**62),
            offset=_number(request, "offset", 0, 0, 2000),
            limit=_number(request, "limit", 8, 1, 8),
        )
    )


async def observe_provision_revisions(request: web.Request) -> web.Response:
    project, environment, since = _observation_scope(
        request, frozenset({"after", "limit", "target"})
    )
    return _observation_json(
        request.app[MANAGER].store.provision_observations.revisions(
            project,
            environment,
            since=since,
            after=_number(request, "after", -1, -1, 2**62),
            limit=_number(request, "limit", 8, 1, 8),
            target=_number(request, "target", 0, 0, 2**62) if "target" in request.query else None,
        )
    )


async def observe_revision_probes(request: web.Request) -> web.Response:
    project, environment, since = _observation_scope(request, frozenset({"pin", "offset", "limit"}))
    revision = _identifier(request.match_info["revision"], "revision")
    if (
        not revision.isascii()
        or not revision.isdigit()
        or len(revision) > 19
        or int(revision) > 2**62
    ):
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid revision selection")
    return _observation_json(
        request.app[MANAGER].store.provision_observations.probes(
            project,
            environment,
            int(revision),
            since=since,
            pin=request.query.get("pin", ""),
            offset=_number(request, "offset", 0, 0, 64),
            limit=_number(request, "limit", 8, 1, 8),
        )
    )


async def observe_lockfile_diff(request: web.Request) -> web.Response:
    """Two explicitly pinned retained blobs, with no active environment operation."""
    queries = frozenset({"before", "before_pin", "after", "after_pin", "offset", "limit"})
    scope = _observation_scope(request, queries)

    def number(name: str, maximum: int, default: str | None = None) -> int:
        text = request.query.get(name, default)
        if text is None or re.fullmatch(r"0|[1-9][0-9]{0,18}", text) is None or int(text) > maximum:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid diff selection")
        return int(text)

    before = (number("before", 2**62), request.query.get("before_pin", ""))
    after = (number("after", 2**62), request.query.get("after_pin", ""))
    for _, pin in (before, after):
        if re.fullmatch(r"[0-9a-f]{64}", pin) is None:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid diff revision pin")
    offset, limit = number("offset", 80_000, "0"), number("limit", 8, "8")

    def prepare() -> Pair:
        current = _observation_scope(request, queries)
        if current != scope:
            raise WorkerError(ErrorKind.CONFLICT, "selected environment changed")
        p, e, since = current
        rows = request.app[MANAGER].store.provision_observations.selected_pair(
            p, e, since=since, before=before, after=after
        )
        selections = tuple(
            LockfileSelection(
                revision=row.revision, record_sha256=row.record_sha256, lockfile=row.lockfile
            )
            for row in rows
        )
        return selections[0], selections[1]

    def verify(pair: Pair) -> None:
        if prepare() != pair:
            raise WorkerError(ErrorKind.CONFLICT, "selected lockfile inputs changed")

    result = await request.app[LOCKFILE_DIFFS].read(
        scope[1], request.app[MANAGER].revisions.blobs.root, prepare, verify, offset, limit
    )
    return _observation_json(
        LockfileDiffPage.model_validate(
            {
                "project_id": scope[0],
                "environment_id": scope[1],
                "observed_at": request.app[MANAGER].clock.now(),
                **result,
            },
            strict=True,
        )
    )


async def observe_revision_packages(request: web.Request) -> web.Response:
    """One exactly pinned recorded inventory, sharing comparison's actual read owners."""
    queries = frozenset({"pin", "offset", "limit"})
    scope = _observation_scope(request, queries)
    text = request.match_info["revision"]
    if re.fullmatch(r"0|[1-9][0-9]{0,18}", text) is None or int(text) > 2**62:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid package revision selection")
    revision = int(text)
    pin = request.query.get("pin", "")
    if re.fullmatch(r"[0-9a-f]{64}", pin) is None:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid package revision pin")

    def number(name: str, maximum: int, default: str) -> int:
        value = request.query.get(name, default)
        if re.fullmatch(r"0|[1-9][0-9]{0,18}", value) is None or int(value) > maximum:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid package page selection")
        return int(value)

    offset, limit = number("offset", 40_000, "0"), number("limit", 8, "8")

    def prepare() -> LockfileSelection:
        current = _observation_scope(request, queries)
        if current != scope:
            raise WorkerError(ErrorKind.CONFLICT, "selected environment changed")
        project, environment, since = current
        row = request.app[MANAGER].store.provision_observations.selected_revision(
            project, environment, since=since, revision=revision, pin=pin
        )
        return LockfileSelection(
            revision=row.revision, record_sha256=row.record_sha256, lockfile=row.lockfile
        )

    def verify(selection: LockfileSelection) -> None:
        if prepare() != selection:
            raise WorkerError(ErrorKind.CONFLICT, "selected lockfile input changed")

    result = await request.app[LOCKFILE_DIFFS].read_packages(
        scope[1], request.app[MANAGER].revisions.blobs.root, prepare, verify, offset, limit
    )
    return _observation_json(
        RevisionPackagePage.model_validate(
            {
                "project_id": scope[0],
                "environment_id": scope[1],
                "observed_at": request.app[MANAGER].clock.now(),
                **result,
            },
            strict=True,
        )
    )


def _execution(request: web.Request) -> Execution:
    """The route's execution, once it is known to belong to the client's projects."""
    execution_id = request.match_info["execution_id"]
    execution = request.app[MANAGER].get(execution_id, _project(request))
    _visible(request, execution.context.project_id, f"execution {execution_id}")
    _not_job_sandbox(request, execution.context.environment_id, f"execution {execution_id}")
    return execution


def _not_job_sandbox(request: web.Request, environment_id: str, what: str) -> None:
    """A job sandbox's own steps are the worker's: no client reads them (D93, JS-1)."""
    environments = request.app.get(ENVIRONMENTS)
    info = environments.records.get(environment_id) if environments is not None else None
    if info is not None and info.kind == "job":
        raise WorkerError(ErrorKind.NOT_FOUND, f"no {what}")


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
    stream = manager.events(execution_id, after, heartbeat=request.app[HEARTBEAT])
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
    execution = _execution(request)
    context = await _effect_control(request)
    return _json(await request.app[MANAGER].cancel(execution.execution_id, context=context))


async def _effect_control(request: web.Request) -> ExecutionContext | None:
    payload = await _body(request)
    if not payload:
        return None
    context = EffectControl.model_validate_json(payload).context
    _context(request, context, hide=True)
    return context


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
    try:
        async with asyncio.timeout(request.app[FILE_WRITE_TIMEOUT]), request.app[FILE_WRITES]:
            raw = await request.clone(client_max_size=MAX_FILE_WRITE_BODY).read()
            body = await offload(WriteFileRequest.model_validate_json, raw)
            del raw
            _context(request, body.context)
            return _json(await request.app[MANAGER].write_file(body))
    except TimeoutError:
        raise WorkerError(
            ErrorKind.UNAVAILABLE, "file transfer timed out; retry the write"
        ) from None


async def skills_check(request: web.Request) -> web.Response:
    """Whether a skill pack is staged in the environment as expected (SK-16)."""
    body = StageCheckRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].stage_check(body))


async def skills_put(request: web.Request) -> web.Response:
    """Write a batch of a skill pack's files where its snapshot is staged (SK-16)."""
    body = StagePutRequest.model_validate_json(await _body(request))
    _context(request, body.context)
    return _json(await request.app[MANAGER].stage_put(body))


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


async def acknowledge_artifact_retention(request: web.Request) -> web.Response:
    """Authenticate and scope before accepting the exact durable control receipt."""
    if not _client(request).artifact_retention:
        raise WorkerError(ErrorKind.POLICY_DENIED, "artifact retention needs control permission")
    artifact_id = _identifier(request.match_info["artifact_id"], "artifact_id")
    retention = request.app[MANAGER].store.artifact_retention
    artifact = retention.original(artifact_id)
    _visible(request, artifact.project_id, "artifact")
    project = _project(request)
    if project is not None and project != artifact.project_id:
        raise WorkerError(ErrorKind.NOT_FOUND, "no artifact")
    body = await _catalog_body(request, ArtifactRetentionAcknowledgement, MAX_RETENTION_ACK_BYTES)
    if body.snapshot.artifact_id != artifact.artifact_id:
        raise WorkerError(ErrorKind.CONFLICT, "retention snapshot differs from route artifact")
    _visible(request, body.snapshot.project_id, "artifact")
    return _json(retention.acknowledge(body))


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


async def kernel_status(request: web.Request) -> web.Response:
    """Running kernels only in the caller's authenticated project/session/environment."""
    body = KernelStatusRequest.model_validate_json(await _body(request))
    _context(request, body.context, hide=True)
    return _json(await request.app[MANAGER].kernel_status(body))


async def observe_kernels(request: web.Request) -> web.Response:
    """Inspect an existing connection without invoking the environment lifecycle."""
    body = KernelStatusRequest.model_validate_json(await _body(request))
    _context(request, body.context, hide=True)
    return _json(await request.app[MANAGER].observe_kernels(body))
