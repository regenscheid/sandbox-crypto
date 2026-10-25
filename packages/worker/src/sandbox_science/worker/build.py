# SPDX-License-Identifier: MIT
"""Building a worker from its settings: ``build_worker``.

``serve`` builds the worker here and runs it, so that what it is made of, and how
its parts know each other, is decided in one place, when they are built: the store,
the backend, the managed environments and the lifecycle answers the execution
manager needs from them, the provisioner and the HTTP app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial

from aiohttp import web

from sandbox_science.contracts import SYSTEM_CLOCK, Clock

from .app import create_app
from .artifacts import Blobs
from .backends import Backend, LocalBackend, OpenShellBackend
from .catalog import CatalogError, load_catalog
from .classifications import ClassificationDesk, project_policy
from .documents_catalog import DocumentsCatalog
from .environments import Environments, OpenShellCli, confirmed_daemon_loss
from .job_sandboxes import JobSandboxes, ManagerPort
from .lifecycle import UNMANAGED, EnvironmentLifecycle
from .manager import ExecutionManager
from .package_clients import cache_endpoint
from .permission_requests import PermissionDesk
from .permissions import Permissions
from .provisioner import Provisioner
from .settings import Settings, SettingsError
from .shared_sync import SharedRecords, SharedSync
from .state_lock import StateLock
from .store import Store

_log = logging.getLogger("sandbox_science.worker")


def executor_args(settings: Settings) -> list[str]:
    """Options for every executor, from the worker's settings."""
    if settings.kernel_idle_timeout is None:
        return []
    return ["--kernel-idle", repr(settings.kernel_idle_timeout)]


def build_environments(settings: Settings, clock: Clock = SYSTEM_CLOCK) -> Environments | None:
    openshell = settings.openshell
    if openshell is None or openshell.environments is None:
        return None
    cli_env = OpenShellBackend(openshell, settings.state_dir).cli_env()
    environments = Environments(
        openshell, openshell.environments, settings.state_dir, cli_env, clock=clock
    )
    target = environments.targets.sandbox.package_cache
    if openshell.package_cache is not None and cache_endpoint(openshell.package_cache) != (
        target.host,
        target.port,
    ):
        environments.close()
        # The policies compiled for these sandboxes admit the cache only there.
        raise SettingsError(
            f"package_cache {openshell.package_cache} is not where the targets file puts "
            f"the package cache ({target.host}:{target.port})"
        )
    return environments


def build_provisioner(
    settings: Settings, manager: ExecutionManager, environments: Environments | None
) -> Provisioner:
    """Provisioning for the environments the worker creates: their policies decide, and
    permission requests go to the policy middleware's event store (Conda provisioning)."""
    try:
        catalog = load_catalog()
    except CatalogError as exc:
        raise SettingsError(str(exc)) from exc
    openshell = settings.openshell
    managed = openshell.environments if openshell is not None else None
    return Provisioner(
        manager,
        catalog=catalog,
        policies=environments,
        permissions=Permissions(managed.event_store_path) if managed is not None else None,
        cache=openshell.package_cache if openshell is not None else None,
        clock=manager.clock,
    )


def build_backend(settings: Settings, environments: Environments | None) -> Backend:
    if settings.openshell is not None:
        return OpenShellBackend(
            settings.openshell,
            settings.state_dir,
            environments.sandbox if environments is not None else None,
            executor_args=executor_args(settings),
            prepare=environments.resume if environments is not None else None,
            manages=environments.manages if environments is not None else None,
            requires_workspace_prefix=(
                environments.requires_workspace_prefix if environments is not None else None
            ),
        )
    if settings.local is None:
        raise SettingsError("no backend is configured")
    _log.warning("the local backend runs research code without any isolation (tier 0)")
    return LocalBackend(settings.local, settings.state_dir, executor_args(settings))


def build_lifecycle(
    settings: Settings, backend: Backend, environments: Environments | None, store: Store
) -> EnvironmentLifecycle:
    """What the execution manager needs from the sandboxes the worker manages.

    On OpenShell, recovery may conclude that a sandbox is gone; with managed
    environments, the manager also gets their bindings, recycles, admission notices
    and job limits. The local backend manages nothing.
    """
    if settings.openshell is None or not isinstance(backend, OpenShellBackend):
        return UNMANAGED
    loss = partial(
        confirmed_daemon_loss,
        OpenShellCli(settings.openshell, backend.cli_env()),
        store,
        backend.sandbox_id,
        environments,
    )
    if environments is None:
        return EnvironmentLifecycle(confirmed_daemon_loss=loss)
    return EnvironmentLifecycle(
        daemon_identity=environments.daemon_identity,
        confirmed_daemon_loss=loss,
        recycle_daemon=environments.recycle_executor,
        notices=environments.notices,
        job_resources=environments.job_resources,
        requires_workspace_prefix=environments.requires_workspace_prefix,
    )


@dataclass
class WorkerParts:
    """A worker built from its settings, ready to start."""

    lock: StateLock
    store: Store
    documents: DocumentsCatalog
    shared: SharedSync | None
    environments: Environments | None
    manager: ExecutionManager
    provisioner: Provisioner
    app: web.Application

    async def close(self) -> None:
        """Stop the provisioner and the manager, then close every store, after the app."""
        try:
            await self.provisioner.close()
            await self.manager.close()
            if self.provisioner.permissions is not None:
                self.provisioner.permissions.close()
            self.store.close()
            self.documents.close()
            if self.shared is not None:
                self.shared.records.close()
            if self.environments is not None:
                self.environments.close()
        finally:
            self.lock.release()


def build_worker(settings: Settings, token: str, *, clock: Clock = SYSTEM_CLOCK) -> WorkerParts:
    """The worker the settings describe; ``token`` is the pool's own API token. It
    holds the state directory's lock until it is closed."""
    lock = StateLock(settings.state_dir)
    try:
        return _build(settings, token, clock, lock)
    except BaseException:
        lock.release()
        raise


def _build(settings: Settings, token: str, clock: Clock, lock: StateLock) -> WorkerParts:
    environments = build_environments(settings, clock)
    store = Store(settings.state_dir / "worker.sqlite3")
    documents = DocumentsCatalog(
        settings.state_dir / "documents-catalog.sqlite3", settings.document_catalog
    )
    # The shared files' blobs are kept apart from published artifacts, by project (D79).
    shared = (
        SharedSync(
            SharedRecords(settings.state_dir / "shared.sqlite3"),
            settings.state_dir / "shared",
            clock=clock,
        )
        if settings.shared_sync
        else None
    )
    backend = build_backend(settings, environments)
    manager = ExecutionManager(
        store,
        backend,
        Blobs(settings.state_dir / "artifacts"),
        pool_id=settings.pool_id,
        max_artifact_size=settings.max_artifact_size,
        state_dir=settings.state_dir,
        shared=shared,
        package_cache=settings.openshell.package_cache if settings.openshell else None,
        lifecycle=build_lifecycle(settings, backend, environments, store),
        clock=clock,
    )
    if environments is not None:
        environments.release = manager.release
        environments.release_idle = manager.release_if_idle
        if shared is not None:
            environments.deleted.append(shared.forget)
    provisioner = build_provisioner(settings, manager, environments)
    if environments is not None and environments.settings.job_profiles:
        sandboxes = JobSandboxes(environments, store, clock=clock)
        sandboxes.port = ManagerPort(manager, provisioner)
        manager.job_sandboxes = sandboxes
    desk = (
        PermissionDesk(
            provisioner.permissions, lambda: environments.records.listed(None), manager.clock
        )
        if provisioner.permissions is not None and environments is not None
        else None
    )
    review = (
        ClassificationDesk(
            environments.store,
            provisioner.permissions,
            partial(project_policy, environments),
            store.classification_receipts,
            manager.clock,
        )
        if provisioner.permissions is not None and environments is not None
        else None
    )
    app = create_app(
        manager, token, environments, settings.clients, provisioner, documents,
        permission_desk=desk, classification_desk=review,
    )  # fmt: skip
    return WorkerParts(lock, store, documents, shared, environments, manager, provisioner, app)
