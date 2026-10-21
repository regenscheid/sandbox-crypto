# SPDX-License-Identifier: MIT
"""Trusted host control service: scoped capabilities and durable stores, no model loop."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import stat
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from aiohttp import WSCloseCode, web
from pydantic import Field, JsonValue, field_validator, model_validator

from sandbox_science.contracts.model import Contract, Identifier
from sandbox_science.orchestrator.connectors import ProxyClient
from sandbox_science.orchestrator.consistency.models import REVIEWER_MAX_RESPONSE_BYTES
from sandbox_science.orchestrator.control.migrations import VERSION
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.figurecheck import FigureChecker
from sandbox_science.orchestrator.harness import (
    Admitted,
    Harness,
    McpAccess,
    Role,
    Settings,
    Stores,
)
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.mathcheck import MathChecker
from sandbox_science.orchestrator.model_profiles import Effort, ModelRole
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.skills.derive import ANSWER_TOKENS
from sandbox_science.orchestrator.skills.embeddings import SkillVectors
from sandbox_science.orchestrator.skills.store import SkillStore
from sandbox_science.orchestrator.worker_client import WorkerClient

from .embeddings import CallbackEmbedder
from .endpoints import ServerTLS, bind_address, loopback
from .host import HostLoops
from .lifecycle import ControlLifecycle
from .metadata import Metadata
from .models import CallbackGateway, CallbackModel
from .transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    MAX_FRAME_BYTES,
    Capability,
    Channel,
    TransportError,
    serve_socket,
)
from .wire import (
    Hello,
    ModelAlias,
    Queue,
    Ready,
    ResumeBranch,
    ResumeReady,
    Waiting,
    payload,
    typed,
)

REVIEWER_MAX_OUTPUT = 4096
"""Reviewer output tokens, kept in sync with the host-mode reviewer."""


class ProxySettings(Contract):
    url: str = Field(min_length=1, max_length=2048)
    token_file: Path
    project: Identifier
    servers: tuple[Identifier, ...]
    about: dict[str, str] = Field(default_factory=dict[str, str])
    index: tuple[str, ...] | None = None


class CheckSettings(Contract):
    node: Path
    math_launcher: Path
    math_bundle: Path
    figure_launcher: Path
    figure_bundle: Path


class ControlSettings(Contract):
    state_dir: Path
    listen: str = "127.0.0.1:8202"
    tls: ServerTLS | None = None
    worker: str = Field(min_length=1, max_length=2048)
    worker_token_file: Path
    worker_ca_file: Path | None = None
    runtime_token_file: Path
    generation: Identifier
    generation_file: Path | None = None
    projects: tuple[Identifier, ...] = Field(min_length=1, max_length=128)
    apis: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: dict[ModelRole, Effort] = Field(default_factory=dict[ModelRole, Effort])
    main: ModelAlias
    reviewer: ModelAlias | None = None
    watcher: ModelAlias | None = None
    delegated: ModelAlias | None = None
    agent_models: tuple[ModelAlias, ...] = Field(default_factory=tuple, max_length=64)
    settings: Settings = Field(default_factory=Settings)
    proxy: ProxySettings | None = None
    embedding: ModelAlias | None = None
    metadata: ModelAlias | None = None
    checks: CheckSettings | None = None
    image_revision: str = Field(min_length=1, max_length=128)

    @field_validator("listen")
    @classmethod
    def valid_listen(cls, value: str) -> str:
        bind_address(value)
        return value

    @model_validator(mode="after")
    def remote_tls(self) -> ControlSettings:
        if not loopback(self.listen) and self.tls is None:
            raise ValueError("a non-loopback control bind requires TLS")
        return self


def private_credential(path: Path, *, minimum: int = 1, maximum: int = 4096) -> str:
    mode = path.stat().st_mode
    if path.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
        raise ValueError("capability credential must be a private regular file")
    if path.stat().st_size > maximum + 2:
        raise ValueError("capability credential exceeds its size bound")
    token = path.read_text(encoding="utf-8").strip()
    if not minimum <= len(token) <= maximum or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("capability credential has invalid shape")
    return token


def private_token(path: Path) -> str:
    return private_credential(path, minimum=32, maximum=256)


def create_app(settings: ControlSettings) -> web.Application:
    """Create a service that admits only the launcher's current scoped runtime token."""
    app = web.Application(client_max_size=MAX_FRAME_BYTES)
    lifecycle = ControlLifecycle()

    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            lifecycle.health(),
            headers={CONTROL_HEADER: str(CONTROL_PROTOCOL)},
        )

    async def ready(request: web.Request) -> web.Response:
        response = await health(request)
        response.set_status(503 if lifecycle.draining else 200)
        return response

    async def live(request: web.Request) -> web.Response:
        return web.json_response({"process_alive": True, "draining": lifecycle.draining})

    async def control(request: web.Request) -> web.WebSocketResponse:
        header = request.headers.get("Authorization", "")
        expected = private_token(settings.runtime_token_file)
        supplied = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, expected):
            raise web.HTTPNotFound()
        if request.headers.get(CONTROL_HEADER) != str(CONTROL_PROTOCOL):
            raise web.HTTPUnprocessableEntity(text="control protocol mismatch")
        if lifecycle.draining:
            raise web.HTTPServiceUnavailable(text="control service is draining")
        if lifecycle.occupied:
            raise web.HTTPConflict(text="a runtime already owns this control connection")
        # Admission ownership outlives the socket while admitted effects drain.
        # Health must not present that cleanup period as a connected runtime.
        token_digest = hashlib.sha256(supplied.encode()).digest()
        socket = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=15)
        lifecycle.claim(socket)
        projects: ProjectStore | None = None
        documents: DocumentStore | None = None
        proxy: ProxyClient | None = None
        loops: HostLoops | None = None
        service: RunService | None = None
        hello = False
        try:
            projects = ProjectStore(settings.state_dir)
            documents = DocumentStore(store_path(settings.state_dir), producer="harness")
            documents.initialize()
            async with WorkerClient(
                settings.worker,
                private_token(settings.worker_token_file),
                ca_file=settings.worker_ca_file,
            ) as worker:

                async def handle(kind: Capability, value: JsonValue) -> JsonValue:
                    nonlocal hello
                    # Rotation revokes established connections as well as new handshakes.
                    current = hashlib.sha256(
                        private_token(settings.runtime_token_file).encode()
                    ).digest()
                    if not hmac.compare_digest(current, token_digest):
                        raise TransportError("denied", "runtime capability was revoked")
                    if kind is Capability.HANDSHAKE:
                        request = typed(Hello, value)
                        if hello or request.image_revision != settings.image_revision:
                            raise TransportError("conflict", "runtime image or handshake mismatch")
                        await worker.readiness()
                        hello = True
                        lifecycle.handshaken = True
                        return payload(
                            Ready(
                                generation=private_credential(settings.generation_file, maximum=128)
                                if settings.generation_file is not None
                                else settings.generation,
                                projects=settings.projects,
                                control_schema=VERSION,
                            )
                        )
                    if not hello or service is None or loops is None:
                        raise TransportError("denied", "runtime handshake required")
                    if kind is Capability.QUEUE:
                        if value is not None:
                            raise TransportError("invalid_request", "queue accepts no caller scope")
                        await service.maintenance_tick()
                        waiting = [
                            Waiting(project=project, session=session, request=request)
                            for project, session, request in await service.waiting_resumes()
                            if project in settings.projects
                        ]
                        waiting.extend(
                            Waiting(project=project, session=session)
                            for project, session in await service.waiting_work()
                            if project in settings.projects
                        )
                        return payload(Queue(entries=tuple(waiting[:128]), more=len(waiting) > 128))
                    if kind in (Capability.ADMIT, Capability.RESUME_NEXT, Capability.RESUME_BRANCH):
                        selected = typed(
                            ResumeBranch if kind is Capability.RESUME_BRANCH else Waiting, value
                        )
                        if selected.project not in settings.projects:
                            raise TransportError("denied", "project is outside this capability")
                        if kind is Capability.ADMIT:
                            return await service.start_selected(
                                selected.project, selected.session, request=selected.request
                            )
                        if selected.request is None:
                            raise TransportError(
                                "invalid_request", "Resume requires its explicit request"
                            )
                        if kind is Capability.RESUME_NEXT:
                            return payload(
                                ResumeReady(
                                    work=await service.resume_next(
                                        selected.project, selected.session, selected.request
                                    )
                                )
                            )
                        branch = typed(ResumeBranch, value)
                        return await service.resume_branch(
                            branch.project, branch.session, branch.request, branch.work
                        )
                    return await loops.capability(kind, value)

                channel = Channel(socket.send_str, handle)
                agent_models = frozenset(settings.agent_models)
                fold_models = agent_models or frozenset(
                    alias for alias in (settings.main, settings.delegated) if alias
                )
                if settings.settings.fold_alias:
                    fold_models |= frozenset((settings.settings.fold_alias,))
                roles: dict[str, frozenset[str]] = {
                    "fold": fold_models,
                    "reviewer": agent_models
                    or (frozenset((settings.reviewer,)) if settings.reviewer else frozenset()),
                    "watcher": frozenset((settings.watcher,)) if settings.watcher else frozenset(),
                    "skills": frozenset((settings.metadata,)) if settings.metadata else frozenset(),
                    "embedding": frozenset((settings.embedding,))
                    if settings.embedding
                    else frozenset(),
                }
                loops = HostLoops(
                    channel,
                    frozenset(settings.projects),
                    control=documents.control,
                    model_roles=roles,
                )
                scope = loops.model_capability
                gateway = CallbackGateway(
                    channel, scope, apis=settings.apis, reasoning_effort=settings.reasoning_effort
                )
                stores = Stores(
                    projects,
                    documents,
                    JobState(settings.state_dir, control=documents.control),
                    RunState(projects.control),
                )
                access = None
                if settings.proxy is not None:
                    spec = settings.proxy
                    if spec.project not in settings.projects:
                        raise ValueError("proxy project is outside runtime scope")
                    proxy = ProxyClient(spec.url, private_token(spec.token_file))
                    access = McpAccess(proxy, spec.servers, spec.project, spec.about, spec.index)
                metadata = (
                    Metadata(
                        SkillStore(documents.control),
                        CallbackModel(
                            channel,
                            settings.metadata,
                            scope=scope,
                            kind="skills",
                            role="skills_metadata",
                            profile=gateway.model(
                                settings.metadata, role="skills_metadata"
                            ).requested_profile,
                            max_output=ANSWER_TOKENS,
                        ),
                        settings.metadata,
                    )
                    if settings.metadata
                    else None
                )
                metadata_lock = asyncio.Lock()

                @asynccontextmanager
                async def prepared(admitted: Admitted) -> AsyncGenerator[None]:
                    async with loops.model_scope():
                        if metadata is not None:
                            async with metadata_lock:
                                await metadata.prepare(admitted)
                        yield

                checks = settings.checks
                figures = (
                    FigureChecker(checks.node, checks.figure_launcher, checks.figure_bundle)
                    if checks
                    else None
                )
                vectors = (
                    SkillVectors(CallbackEmbedder(channel, settings.embedding, scope))
                    if settings.embedding
                    else None
                )
                harness = Harness(
                    worker,
                    Role(gateway.model(settings.main, role="main"), settings.main),
                    stores=stores,
                    gateway=gateway,
                    settings=settings.settings,
                    mcp=access,
                    watcher=Role(
                        CallbackModel(
                            channel,
                            settings.watcher,
                            scope=scope,
                            kind="watcher",
                            role="watcher",
                            profile=gateway.model(
                                settings.watcher, role="watcher"
                            ).requested_profile,
                            max_output=300,
                            max_response_bytes=16384,
                        ),
                        settings.watcher,
                    )
                    if settings.watcher
                    else None,
                    reviewer=Role(
                        CallbackModel(
                            channel,
                            settings.reviewer,
                            scope=scope,
                            kind="reviewer",
                            role="reviewer",
                            profile=gateway.model(
                                settings.reviewer, role="reviewer"
                            ).requested_profile,
                            max_output=REVIEWER_MAX_OUTPUT,
                            max_response_bytes=REVIEWER_MAX_RESPONSE_BYTES,
                        ),
                        settings.reviewer,
                    )
                    if settings.reviewer
                    else None,
                    delegated=Role(
                        gateway.model(settings.delegated, role="delegated"), settings.delegated
                    )
                    if settings.delegated
                    else None,
                    loop_runner=loops.run,
                    prepare_attempt=prepared,
                    skills_metadata=settings.metadata,
                    vectors=vectors,
                    checker=MathChecker(checks.node, checks.math_launcher, checks.math_bundle)
                    if checks
                    else None,
                )
                service = RunService(harness, figures=figures)
                if lifecycle.draining:
                    raise web.HTTPServiceUnavailable(text="control service is draining")
                await socket.prepare(request)
                lifecycle.channel = channel
                # Response preparation can await hooks after the first admission fence.
                # Shutdown must not miss a socket that became prepared during its drain.
                if lifecycle.draining:
                    await socket.close(code=WSCloseCode.GOING_AWAY, message=b"control stopping")
                else:
                    await serve_socket(socket, channel)
        finally:
            lifecycle.channel = None
            async with AsyncExitStack() as cleanup:
                cleanup.callback(lifecycle.release)
                if projects is not None:
                    cleanup.callback(projects.close)
                if documents is not None:
                    cleanup.callback(documents.close)
                if proxy is not None:
                    cleanup.push_async_callback(proxy.close)
                if service is not None and service.harness.vectors is not None:
                    cleanup.push_async_callback(service.harness.vectors.close)
                if service is not None:
                    cleanup.push_async_callback(service.close)
        return socket

    app.router.add_get("/health", health)
    app.router.add_get("/live", live)
    app.router.add_get("/ready", ready)
    app.router.add_get("/control", control)
    app.on_shutdown.append(lifecycle.shutdown)
    return app


def _ready(message: str) -> None:
    print("control capability service ready", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    settings = ControlSettings.model_validate_json(args.settings.read_text(encoding="utf-8"))
    host, port = bind_address(settings.listen)
    web.run_app(
        create_app(settings),
        host=host,
        port=port,
        ssl_context=settings.tls.context() if settings.tls is not None else None,
        print=_ready,
    )


if __name__ == "__main__":
    main()
