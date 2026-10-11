# SPDX-License-Identifier: MIT
"""Research environments: OpenShell sandboxes the worker creates, revises and deletes (D73).

The worker is trusted (Z1) and holds the gateway's credentials; the orchestrator is
not. So the orchestrator names an environment, a base profile and a policy revision,
and the worker does the rest:

1. It loads the revision from the pool's policy store, where an operator put it
   (``sandbox-science-worker policy add``). The orchestrator cannot add one.
2. It compiles the policy for OpenShell under the pool ceiling, with the sandbox's
   filesystem section.
3. It checks that every provider the policy lists is registered in the gateway with a
   profile that names no endpoints, so that OpenShell binds each credential only
   where the compiled policy says (D57).
4. It creates the sandbox with exactly those providers, and binds the sandbox to the
   revision in the policy middleware's store.

A revision replaces the sandbox's network sections with ``openshell policy set``,
carrying the filesystem section the sandbox reports, then rebinds. Between the two
steps the middleware denies requests as ``policy_stale``, so the change fails closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
)
from sandbox_science.policy import LoadedPolicy, PolicyLoadError, load_ceiling
from sandbox_science.policy.openshell import (
    Compiled,
    CompileError,
    FilesystemPolicy,
    compile_sandbox,
    load_targets,
)
from sandbox_science.policy_middleware.errors import StoreError
from sandbox_science.policy_middleware.store import PolicyStore

from .errors import WorkerError
from .settings import EnvironmentSettings, OpenShellBackendSettings

_log = logging.getLogger(__name__)

SANDBOX_PREFIX: Final = "ss-"
_UNSAFE: Final = re.compile(r"[^a-z0-9]+")
_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SANDBOX_COMMAND: Final = ("sleep", "infinity")
_POLL_SECONDS: Final = 1.0
_POLICY_TIMEOUT: Final = 120

Release = Callable[[str], Awaitable[None]]


def sandbox_name(environment_id: str) -> str:
    """A stable OpenShell sandbox name for an environment: lowercase, short and unique."""
    slug = _UNSAFE.sub("-", environment_id.lower()).strip("-")[:40] or "env"
    digest = hashlib.sha256(environment_id.encode()).hexdigest()[:8]
    return f"{SANDBOX_PREFIX}{slug}-{digest}"


class OpenShellCli:
    """Runs ``openshell`` commands for the worker, with the backend's CLI environment."""

    def __init__(self, settings: OpenShellBackendSettings, env: dict[str, str]) -> None:
        self.cli = settings.cli
        self.env = env

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        """Run a command and return its output; raise ``unavailable`` if it fails."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.cli,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"cannot run {self.cli}: {exc}") from exc
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} took over {timeout:.0f} s"
            ) from exc
        if process.returncode != 0:
            text = _ANSI.sub("", (errors or output).decode("utf-8", "replace")).strip()
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} failed: {text[-500:]}"
            )
        return output.decode("utf-8", "replace")

    async def json(self, *args: str) -> object:
        text = await self.run(*args, "-o", "json")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} did not return JSON"
            ) from exc

    async def sandbox(self, name: str) -> dict[str, object] | None:
        """The sandbox's description, or None when the gateway has no sandbox by that name."""
        try:
            document = await self.json("sandbox", "get", name)
        except WorkerError as error:
            if "not found" in error.message:
                return None
            raise
        if not isinstance(document, dict):
            raise WorkerError(ErrorKind.UNAVAILABLE, "openshell sandbox get returned no object")
        return cast(dict[str, object], document)


class EnvironmentRecords:
    """The worker's record of the environments it manages, in its own SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS environments ("
            " environment_id TEXT PRIMARY KEY, sandbox TEXT NOT NULL, document TEXT NOT NULL)"
        )
        self._lock = threading.Lock()

    def get(self, environment_id: str) -> EnvironmentInfo | None:
        with self._lock:
            row = self._db.execute(
                "SELECT document FROM environments WHERE environment_id = ?", (environment_id,)
            ).fetchone()
        return None if row is None else EnvironmentInfo.model_validate_json(row[0])

    def save(self, info: EnvironmentInfo) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO environments (environment_id, sandbox, document) VALUES (?, ?, ?)"
                " ON CONFLICT (environment_id) DO UPDATE SET"
                " sandbox = excluded.sandbox, document = excluded.document",
                (info.environment_id, info.sandbox, info.model_dump_json()),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()


class Environments:
    """Creates, revises and deletes the sandboxes behind logical environments."""

    def __init__(
        self,
        backend: OpenShellBackendSettings,
        settings: EnvironmentSettings,
        state_dir: Path,
        cli_env: dict[str, str],
    ) -> None:
        self.settings = settings
        self.cli = OpenShellCli(backend, cli_env)
        self.store = PolicyStore(settings.policy_store)
        try:
            self.targets = load_targets(settings.targets)
            self.ceiling = load_ceiling(settings.ceiling)
        except PolicyLoadError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"environment settings: {exc}") from exc
        self.root = state_dir / "environments"
        self.records = EnvironmentRecords(state_dir / "environments.sqlite3")
        self.release: Release | None = None
        """Stops the environment's executor; the execution manager sets it."""
        self._locks: dict[str, asyncio.Lock] = {}

    def close(self) -> None:
        self.records.close()

    def sandbox(self, environment_id: str) -> str | None:
        """The sandbox a ready environment runs in, for the backend."""
        info = self.records.get(environment_id)
        return info.sandbox if info is not None and info.state is EnvironmentState.READY else None

    def get(self, environment_id: str) -> EnvironmentInfo:
        info = self.records.get(environment_id)
        if info is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        return info

    def _lock(self, environment_id: str) -> asyncio.Lock:
        return self._locks.setdefault(environment_id, asyncio.Lock())

    async def create(self, request: EnvironmentRequest) -> tuple[EnvironmentInfo, bool]:
        """Create the environment's sandbox; return the existing one for the same request."""
        async with self._lock(request.environment_id):
            existing = self.records.get(request.environment_id)
            if existing is not None and existing.state is EnvironmentState.READY:
                same = (existing.project_id, existing.profile, existing.policy) == (
                    request.project_id,
                    request.profile,
                    request.policy,
                )
                if same:
                    return existing, False
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"environment {request.environment_id!r} exists for another project, profile"
                    " or policy; revise its policy or delete it first",
                )
            image = self.settings.profiles.get(request.profile)
            if image is None:
                raise WorkerError(
                    ErrorKind.UNSUPPORTED,
                    f"this pool offers no profile {request.profile!r}"
                    f" (it offers {', '.join(sorted(self.settings.profiles))})",
                )
            policy = self._policy(request.policy)
            compiled = self._compile(policy)
            providers = await self._check_providers(policy)
            name = sandbox_name(request.environment_id)
            now = datetime.now(UTC)
            info = EnvironmentInfo(
                environment_id=request.environment_id,
                project_id=request.project_id,
                profile=request.profile,
                image=image,
                sandbox=name,
                policy=request.policy,
                providers=providers,
                state=EnvironmentState.CREATING,
                findings=_findings(compiled),
                created_at=now,
                updated_at=now,
            )
            # Recorded first, so that a sandbox left by a crash is known to be ours.
            self.records.save(info)
            if existing is not None or await self.cli.sandbox(name) is not None:
                await self.cli.run("sandbox", "delete", name)
            try:
                sandbox_id = await self._create_sandbox(name, image, compiled, providers)
                self.store.bind(sandbox_id, request.policy)
            except BaseException:
                await self._discard(name)
                self.records.save(
                    info.model_copy(
                        update={"state": EnvironmentState.DELETED, "updated_at": datetime.now(UTC)}
                    )
                )
                raise
            ready = info.model_copy(
                update={
                    "sandbox_id": sandbox_id,
                    "state": EnvironmentState.READY,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.records.save(ready)
            _log.info("created sandbox %s for environment %s", name, request.environment_id)
            return ready, True

    async def revise(self, environment_id: str, revision: str) -> EnvironmentInfo:
        """Move a ready environment to another policy revision."""
        async with self._lock(environment_id):
            info = self.get(environment_id)
            if info.state is not EnvironmentState.READY or info.sandbox_id is None:
                raise WorkerError(
                    ErrorKind.CONFLICT, f"environment {environment_id!r} is {info.state}"
                )
            if info.policy == revision:
                return info
            policy = self._policy(revision)
            compiled = self._compile(policy)
            providers = await self._check_providers(policy)
            if providers != info.providers:
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    "the revision attaches other providers than the sandbox has; OpenShell attaches"
                    " providers only when it creates a sandbox, so create a new environment",
                )
            live = await self._live_filesystem(info.sandbox)
            document = compiled.policy.with_filesystem(live)
            path = self._write(environment_id, revision, document.to_yaml())
            await self.cli.run(
                "policy", "set", info.sandbox, "--policy", str(path),
                "--wait", "--timeout", str(_POLICY_TIMEOUT),
                timeout=_POLICY_TIMEOUT + 30,
            )  # fmt: skip
            self.store.bind(info.sandbox_id, revision)
            revised = info.model_copy(
                update={
                    "policy": revision,
                    "findings": _findings(compiled),
                    "updated_at": datetime.now(UTC),
                }
            )
            self.records.save(revised)
            _log.info("environment %s now runs under %s", environment_id, revision)
            return revised

    async def delete(self, environment_id: str) -> EnvironmentInfo:
        """Stop the environment's executor and delete its sandbox and binding."""
        async with self._lock(environment_id):
            info = self.get(environment_id)
            if info.state is EnvironmentState.DELETED:
                return info
            if self.release is not None:
                await self.release(environment_id)
            await self.cli.run("sandbox", "delete", info.sandbox)
            if info.sandbox_id is not None:
                self.store.unbind(info.sandbox_id)
            deleted = info.model_copy(
                update={"state": EnvironmentState.DELETED, "updated_at": datetime.now(UTC)}
            )
            self.records.save(deleted)
            _log.info("deleted sandbox %s of environment %s", info.sandbox, environment_id)
            return deleted

    # Steps.

    def _policy(self, revision: str) -> LoadedPolicy:
        try:
            return self.store.policy(revision)
        except StoreError as exc:
            raise WorkerError(
                ErrorKind.NOT_FOUND,
                f"{exc}; an operator adds revisions with `sandbox-science-worker policy add`",
            ) from exc

    def _compile(self, policy: LoadedPolicy) -> Compiled:
        try:
            return compile_sandbox(policy, self.targets.sandbox, ceiling=self.ceiling)
        except CompileError as exc:
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, f"the policy cannot be compiled: {exc}"
            ) from exc

    async def _check_providers(self, policy: LoadedPolicy) -> tuple[str, ...]:
        """The policy's providers, each registered with a profile that names no endpoints."""
        wanted = policy.policy.credentials.providers
        if not wanted:
            return ()
        listing = await self.cli.json("provider", "list")
        registered = {
            str(entry.get("name")): str(entry.get("type"))
            for entry in _objects(listing, "providers")
        }
        profiles = {
            str(entry.get("id")): entry.get("endpoints")
            for entry in _objects(await self.cli.json("profile", "list"))
        }
        for ref in wanted:
            kind = registered.get(ref.name)
            if kind is None:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, f"provider {ref.name!r} is not registered in the gateway"
                )
            if kind != ref.profile:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"provider {ref.name!r} has profile {kind!r}; the policy says {ref.profile!r}",
                )
            if profiles.get(kind) != []:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"provider profile {kind!r} names endpoints, or is missing; OpenShell would"
                    " bind the credential there as well as where the policy binds it",
                )
        return tuple(sorted(ref.name for ref in wanted))

    async def _create_sandbox(
        self, name: str, image: str, compiled: Compiled, providers: tuple[str, ...]
    ) -> str:
        path = self._write(name, compiled.policy_digest, compiled.policy.to_yaml())
        args = ["sandbox", "create", "--name", name, "--from", image, "--policy", str(path)]
        for provider in providers:
            args += ["--provider", provider]
        args += ["--no-auto-providers", "--detach", "--", *_SANDBOX_COMMAND]
        await self.cli.run(*args, timeout=self.settings.create_timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.create_timeout
        while True:
            document = await self.cli.sandbox(name)
            if document is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, f"sandbox {name} vanished after creation")
            admission = document.get("configuration_admission")
            if isinstance(admission, dict):
                state = cast(dict[str, object], admission).get("state")
                if state not in (None, "", "accepted", "pending"):
                    error = cast(dict[str, object], admission).get("error")
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE,
                        f"OpenShell did not accept the policy: {error or state}",
                    )
            sandbox_id = document.get("id")
            if document.get("phase") == "Ready" and isinstance(sandbox_id, str) and sandbox_id:
                return sandbox_id
            if loop.time() > deadline:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"sandbox {name} was not ready after {self.settings.create_timeout:.0f} s",
                )
            await asyncio.sleep(_POLL_SECONDS)

    async def _live_filesystem(self, name: str) -> FilesystemPolicy:
        document = await self.cli.json("policy", "get", name, "--base")
        policy = (
            cast(dict[str, object], document).get("policy") if isinstance(document, dict) else None
        )
        section = (
            cast(dict[str, object], policy).get("filesystem_policy")
            if isinstance(policy, dict)
            else None
        )
        try:
            return FilesystemPolicy.model_validate(section)
        except ValidationError as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"sandbox {name} reported no usable filesystem section"
            ) from exc

    async def _discard(self, name: str) -> None:
        try:
            await self.cli.run("sandbox", "delete", name)
        except WorkerError as error:
            _log.warning(
                "could not delete sandbox %s after a failed creation: %s", name, error.message
            )

    def _write(self, owner: str, revision: str, text: str) -> Path:
        directory = self.root / owner
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"openshell-{revision.removeprefix('sha256:')[:16]}.yaml"
        path.write_text(text)
        return path


def _findings(compiled: Compiled) -> tuple[str, ...]:
    return tuple(
        f"{finding.severity}: {finding.subject}: {finding.message}" for finding in compiled.findings
    )


def _objects(document: object, key: str | None = None) -> list[dict[str, object]]:
    """The JSON objects in a list, or in ``document[key]``; anything else is skipped."""
    items = (
        cast(dict[str, object], document).get(key)
        if key and isinstance(document, dict)
        else document
    )
    if not isinstance(items, list):
        return []
    return [
        cast(dict[str, object], item)
        for item in cast(list[object], items)
        if isinstance(item, dict)
    ]
